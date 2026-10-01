"""
Tests for collecting the user's session environment (issue #7, hardened in #2).

NetworkManager gives the service no session context, so DISPLAY and friends are
read from the user's running processes. A reporter on a desktop whose session
leader is not on our list ended up with no display at all - hence the fallback
that scans every process the user owns.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import subprocess
import types

import pytest

HAS_SESSION = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
needs_session = pytest.mark.skipif(
    not HAS_SESSION, reason="no graphical session in this environment"
)


class TestSessionEnv:
    @needs_session
    def test_finds_a_display_on_this_machine(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(os.getuid(), os.path.expanduser("~"))

        assert env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
        # These two have fallbacks that do not need a session process at all
        assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
        assert "DBUS_SESSION_BUS_ADDRESS" in env

    @needs_session
    def test_falls_back_to_scanning_all_processes(self, service_module, monkeypatch):
        # Pretend we know no session leaders at all: the display must still be
        # found by scanning the user's processes
        monkeypatch.setattr(
            service_module, "SESSION_LEADER_PROCESSES", ("no-such-process",)
        )
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(os.getuid(), os.path.expanduser("~"))

        assert env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")

    def test_scan_ignores_processes_without_a_display(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        # A process of another user, and one of ours without any display
        found = plugin._scan_session_env(-1, {})

        assert found == {}

    def test_survives_an_unreadable_proc(self, service_module, monkeypatch):
        def boom(_path):
            raise OSError("nope")

        monkeypatch.setattr(service_module.os, "listdir", boom)
        plugin = service_module.GpclientVPNPlugin()

        assert plugin._scan_session_env(os.getuid(), {}) == {}

    @pytest.fixture
    def fake_uid_env(self, service_module, monkeypatch, tmp_path):
        """A host with no session leader, no display and a fake uid whose
        runtime dir and bus socket exist only as far as the service can tell.

        Nothing here depends on who runs the tests: pgrep, the /proc scan and
        the filesystem checks are all replaced.
        """
        uid = 4242
        runtime = f"/run/user/{uid}"
        present = {runtime, f"{runtime}/bus"}
        real_isdir, real_exists = os.path.isdir, os.path.exists

        monkeypatch.setattr(
            service_module,
            "SESSION_LEADER_PROCESSES",
            ("no-such-process",),
        )
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_scan_session_env",
            lambda self, uid, found: {},
        )
        monkeypatch.setattr(
            service_module.os.path,
            "isdir",
            lambda path: path in present or (path not in {runtime} and real_isdir(path)),
        )
        monkeypatch.setattr(
            service_module.os.path,
            "exists",
            lambda path: path in present or real_exists(path),
        )
        return types.SimpleNamespace(
            uid=uid, runtime=runtime, present=present, home=str(tmp_path)
        )

    def test_runtime_dir_fallback_without_any_session(
        self, service_module, fake_uid_env
    ):
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(fake_uid_env.uid, fake_uid_env.home)

        # No display, but the runtime dir and bus address are still derivable
        assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
        assert env["XDG_RUNTIME_DIR"] == fake_uid_env.runtime
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={fake_uid_env.runtime}/bus"

    def test_runtime_dir_not_invented_when_absent(
        self, service_module, fake_uid_env
    ):
        # The user has no runtime dir (not logged in): nothing may be made up
        fake_uid_env.present.clear()
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(fake_uid_env.uid, fake_uid_env.home)

        assert "XDG_RUNTIME_DIR" not in env
        assert "DBUS_SESSION_BUS_ADDRESS" not in env

    def test_bus_address_not_invented_without_a_socket(
        self, service_module, fake_uid_env
    ):
        # The runtime dir exists but there is no bus socket in it
        fake_uid_env.present.discard(f"{fake_uid_env.runtime}/bus")
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(fake_uid_env.uid, fake_uid_env.home)

        assert env["XDG_RUNTIME_DIR"] == fake_uid_env.runtime
        assert "DBUS_SESSION_BUS_ADDRESS" not in env

    def test_scan_not_used_when_a_leader_provides_a_display(
        self, service_module, monkeypatch, fake_uid_env
    ):
        # A known session leader exposes a display: the expensive scan of every
        # process must not run (and must not be able to override anything)
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "123\n", ""),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: {"WAYLAND_DISPLAY": "wayland-0"}),
        )

        def scan_must_not_run(self, uid, found):
            raise AssertionError("_scan_session_env must not run")

        monkeypatch.setattr(
            service_module.GpclientVPNPlugin, "_scan_session_env", scan_must_not_run
        )
        plugin = service_module.GpclientVPNPlugin()

        env = plugin._get_session_env(fake_uid_env.uid, fake_uid_env.home)

        assert env["WAYLAND_DISPLAY"] == "wayland-0"

    @pytest.fixture
    def fake_proc(self, service_module, monkeypatch, tmp_path):
        """A /proc with processes of the current user, made by the test"""
        proc = tmp_path / "proc"
        proc.mkdir()
        monkeypatch.setattr(service_module, "PROC_PATH", str(proc))

        def add(pid, environ, comm="app"):
            directory = proc / str(pid)
            directory.mkdir()
            (directory / "environ").write_bytes(
                b"".join(f"{k}={v}".encode() + b"\0" for k, v in environ.items())
            )
            (directory / "comm").write_text(comm + "\n")

        return add

    def test_scan_does_not_override_already_found_keys(
        self, service_module, fake_proc
    ):
        fake_proc(100, {"DISPLAY": ":0", "XDG_RUNTIME_DIR": "/x"})
        plugin = service_module.GpclientVPNPlugin()

        found = plugin._scan_session_env(
            os.getuid(), {"XDG_RUNTIME_DIR": "/run/user/1"}
        )

        assert found.get("DISPLAY") == ":0"
        assert "XDG_RUNTIME_DIR" not in found

    def test_scan_ignores_processes_of_other_users(self, service_module, fake_proc):
        fake_proc(100, {"DISPLAY": ":0"})
        plugin = service_module.GpclientVPNPlugin()

        assert plugin._scan_session_env(os.getuid() + 1, {}) == {}

    def test_scan_ignores_processes_without_a_display_in_fake_proc(
        self, service_module, fake_proc
    ):
        fake_proc(100, {"XDG_RUNTIME_DIR": "/x"})
        plugin = service_module.GpclientVPNPlugin()

        assert plugin._scan_session_env(os.getuid(), {}) == {}

"""
Tests for GpclientVPNPlugin._get_real_user and the small branches of the
session environment helpers (_read_proc_environ, _scan_session_env,
_get_session_env).

loginctl and pwd are faked: nothing here reads the real users of the machine,
and nothing depends on the uid of the process that runs the tests.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import pwd
import subprocess
import types

import pytest

FALLBACK_UID = 4711


@pytest.fixture
def plugin(service_module):
    return service_module.GpclientVPNPlugin()


@pytest.fixture
def users(monkeypatch):
    """Fake pwd.getpwuid: {uid: home}. Unknown uids raise KeyError."""
    table = {}

    def getpwuid(uid):
        if uid not in table:
            raise KeyError(f"getpwuid(): uid not found: {uid}")
        return types.SimpleNamespace(pw_dir=table[uid])

    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    return table


@pytest.fixture
def clean_env(service_module, monkeypatch):
    for name in ("SUDO_UID", "SUDO_USER", "USER", "HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service_module.os, "getuid", lambda: FALLBACK_UID)


@pytest.fixture
def loginctl(service_module, monkeypatch):
    """Fake the loginctl call. Set .stdout / .returncode / .error on the result."""
    state = types.SimpleNamespace(
        stdout="", returncode=0, error=None, commands=[]
    )

    def run(cmd, **kwargs):
        state.commands.append(cmd)
        if state.error is not None:
            raise state.error
        return subprocess.CompletedProcess(cmd, state.returncode, state.stdout, "")

    monkeypatch.setattr(service_module.subprocess, "run", run)
    return state


@pytest.mark.usefixtures("clean_env")
class TestSudoVariables:
    def test_sudo_uid_and_user_are_used(self, plugin, users, loginctl, monkeypatch):
        users[1000] = "/home/alice-real"
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_USER", "alice")

        assert plugin._get_real_user() == (1000, "alice", "/home/alice-real")
        # loginctl is not needed
        assert loginctl.commands == []

    def test_unknown_uid_gets_default_home(self, plugin, users, loginctl, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1001")
        monkeypatch.setenv("SUDO_USER", "bob")

        assert plugin._get_real_user() == (1001, "bob", "/home/bob")

    @pytest.mark.parametrize(
        "variables",
        [
            {"SUDO_UID": "1000"},
            {"SUDO_USER": "alice"},
            {"SUDO_UID": "", "SUDO_USER": "alice"},
            {"SUDO_UID": "1000", "SUDO_USER": ""},
        ],
    )
    def test_incomplete_sudo_variables_are_not_taken(
        self, plugin, users, loginctl, monkeypatch, variables
    ):
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        users[1000] = "/home/alice"
        loginctl.returncode = 1

        uid, user, _home = plugin._get_real_user()

        assert (uid, user) == (FALLBACK_UID, "root")
        assert loginctl.commands, "loginctl must be asked instead"


@pytest.mark.usefixtures("clean_env")
class TestLoginctl:
    def test_first_non_root_user_is_taken(self, plugin, users, loginctl):
        users[1000] = "/home/alice"
        users[1001] = "/home/bob"
        loginctl.stdout = "0 root\n1000 alice\n1001 bob\n"

        assert plugin._get_real_user() == (1000, "alice", "/home/alice")
        assert loginctl.commands == [["loginctl", "list-users", "--no-legend"]]

    def test_root_and_short_lines_are_skipped(self, plugin, users, loginctl):
        users[1001] = "/home/bob"
        loginctl.stdout = "0 root\n\n42\n1001 bob\n"

        assert plugin._get_real_user() == (1001, "bob", "/home/bob")

    def test_user_without_passwd_entry_is_skipped(self, plugin, users, loginctl):
        users[1001] = "/home/bob"
        loginctl.stdout = "1000 ghost\n1001 bob\n"

        assert plugin._get_real_user() == (1001, "bob", "/home/bob")

    @pytest.mark.parametrize(
        "stdout",
        [
            "0 root\n",
            "42\n",
            "1000 ghost\n",
            "",
            "   \n",
        ],
    )
    def test_no_usable_line_falls_back(self, plugin, users, loginctl, stdout):
        loginctl.stdout = stdout

        assert plugin._get_real_user() == (FALLBACK_UID, "root", "/home/root")

    def test_non_zero_exit_falls_back_even_with_output(self, plugin, users, loginctl):
        users[1000] = "/home/alice"
        loginctl.stdout = "1000 alice\n"
        loginctl.returncode = 1

        assert plugin._get_real_user() == (FALLBACK_UID, "root", "/home/root")

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError("loginctl"),
            subprocess.TimeoutExpired(["loginctl"], 2),
            PermissionError("denied"),
        ],
    )
    def test_loginctl_failure_falls_back(self, plugin, users, loginctl, error):
        users[1000] = "/home/alice"
        loginctl.error = error

        assert plugin._get_real_user() == (FALLBACK_UID, "root", "/home/root")

    def test_non_numeric_uid_falls_back(self, plugin, users, loginctl):
        # int() fails inside the try block: handled like any loginctl failure
        loginctl.stdout = "abc alice\n"

        assert plugin._get_real_user() == (FALLBACK_UID, "root", "/home/root")


@pytest.mark.usefixtures("clean_env")
class TestFallbackToCurrentUser:
    def test_uses_user_and_home_from_environment(
        self, plugin, users, loginctl, monkeypatch
    ):
        loginctl.returncode = 1
        monkeypatch.setenv("USER", "carol")
        monkeypatch.setenv("HOME", "/srv/carol")

        assert plugin._get_real_user() == (FALLBACK_UID, "carol", "/srv/carol")

    def test_home_defaults_from_user(self, plugin, users, loginctl, monkeypatch):
        loginctl.returncode = 1
        monkeypatch.setenv("USER", "carol")

        assert plugin._get_real_user() == (FALLBACK_UID, "carol", "/home/carol")

    def test_everything_defaults_without_environment(self, plugin, users, loginctl):
        loginctl.returncode = 1

        assert plugin._get_real_user() == (FALLBACK_UID, "root", "/home/root")


class TestReadProcEnviron:
    @pytest.fixture
    def proc(self, service_module, monkeypatch, tmp_path):
        monkeypatch.setattr(service_module, "PROC_PATH", str(tmp_path))

        def write(pid, raw):
            directory = tmp_path / str(pid)
            directory.mkdir(exist_ok=True)
            (directory / "environ").write_bytes(raw)

        return write

    def test_parses_entries(self, plugin, proc):
        proc(5, b"A=1\0B=two=2\0C=\0")

        assert plugin._read_proc_environ("5") == {"A": "1", "B": "two=2", "C": ""}

    def test_entries_without_equals_and_empty_entries_are_ignored(self, plugin, proc):
        proc(5, b"\0NOEQUALS\0\0A=1\0")

        assert plugin._read_proc_environ("5") == {"A": "1"}

    def test_invalid_utf8_is_replaced(self, plugin, proc):
        proc(5, b"K\xff=v\xfe\0")

        assert plugin._read_proc_environ("5") == {"K�": "v�"}

    def test_empty_file_gives_empty_dict(self, plugin, proc):
        proc(5, b"")

        assert plugin._read_proc_environ("5") == {}

    def test_missing_process_gives_empty_dict(self, plugin, proc):
        assert plugin._read_proc_environ("99999") == {}


class TestScanSessionEnvBranches:
    @pytest.fixture
    def fake_proc(self, service_module, monkeypatch, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        monkeypatch.setattr(service_module, "PROC_PATH", str(proc))
        # The owner of the fake processes is whoever runs the test: make the
        # check independent of that by faking the uid we ask for.
        owner = os.stat(proc).st_uid

        def add(pid, environ, comm="app"):
            directory = proc / str(pid)
            directory.mkdir()
            (directory / "environ").write_bytes(
                b"".join(f"{k}={v}".encode() + b"\0" for k, v in environ.items())
            )
            if comm is not None:
                (directory / "comm").write_text(comm + "\n")

        add.owner = owner
        add.root = proc
        return add

    def test_non_numeric_entries_of_proc_are_ignored(
        self, plugin, fake_proc
    ):
        fake_proc.root.joinpath("self").mkdir()
        (fake_proc.root / "self" / "environ").write_bytes(b"DISPLAY=:9\0")

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}

    def test_first_process_with_a_display_wins(self, plugin, fake_proc, caplog):
        fake_proc(100, {"XDG_RUNTIME_DIR": "/x"})
        fake_proc(200, {"DISPLAY": ":1", "XDG_SESSION_TYPE": "x11"}, comm="xterm")
        fake_proc(300, {"DISPLAY": ":2", "XDG_CURRENT_DESKTOP": "Other"})
        caplog.set_level("INFO")

        found = plugin._scan_session_env(fake_proc.owner, {})

        # Values come from one process only, never mixed; pid 100 has no display
        assert found in (
            {"DISPLAY": ":1", "XDG_SESSION_TYPE": "x11"},
            {"DISPLAY": ":2", "XDG_CURRENT_DESKTOP": "Other"},
        )
        taken = "xterm (200)" if found["DISPLAY"] == ":1" else "app (300)"
        assert taken in caplog.text

    def test_wayland_display_alone_counts(self, plugin, fake_proc):
        fake_proc(100, {"WAYLAND_DISPLAY": "wayland-0"})

        assert plugin._scan_session_env(fake_proc.owner, {}) == {
            "WAYLAND_DISPLAY": "wayland-0"
        }

    def test_process_that_cannot_be_stat_ed_is_skipped(
        self, service_module, plugin, fake_proc, monkeypatch
    ):
        fake_proc(100, {"DISPLAY": ":0"})

        def boom(_path):
            raise FileNotFoundError("gone")

        monkeypatch.setattr(service_module.os, "stat", boom)

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}

    def test_unreadable_comm_is_logged_as_unknown(self, plugin, fake_proc, caplog):
        fake_proc(100, {"DISPLAY": ":0"}, comm=None)
        caplog.set_level("INFO")

        found = plugin._scan_session_env(fake_proc.owner, {})

        assert found == {"DISPLAY": ":0"}
        assert "? (100)" in caplog.text

    def test_unreadable_environ_is_skipped(self, plugin, fake_proc):
        fake_proc(100, {"DISPLAY": ":0"})
        (fake_proc.root / "100" / "environ").unlink()

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}


class TestGetSessionEnvBranches:
    @pytest.fixture
    def no_extras(self, service_module, monkeypatch, tmp_path):
        """No fallbacks from the filesystem and no scan of all processes"""
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_scan_session_env",
            lambda self, uid, found: {},
        )
        monkeypatch.setattr(service_module.os.path, "isdir", lambda p: False)
        monkeypatch.setattr(service_module.os.path, "exists", lambda p: False)
        return str(tmp_path)

    def test_stops_asking_once_every_key_is_known(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        full = {key: f"v-{key}" for key in service_module.SESSION_ENV_KEYS}
        asked = []

        def run(cmd, **kw):
            asked.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "11 12\n", "")

        monkeypatch.setattr(service_module.subprocess, "run", run)
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: dict(full)),
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == full
        assert asked == [service_module.SESSION_LEADER_PROCESSES[0]]

    def test_keeps_asking_when_keys_are_missing(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        asked = []

        def run(cmd, **kw):
            asked.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "11\n", "")

        monkeypatch.setattr(service_module.subprocess, "run", run)
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: {"DISPLAY": ":0"}),
        )

        plugin._get_session_env(FALLBACK_UID, no_extras)

        assert asked == list(service_module.SESSION_LEADER_PROCESSES)

    def test_first_process_wins_for_each_key(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        environs = {
            "11": {"DISPLAY": ":0"},
            "12": {"DISPLAY": ":9", "XDG_SESSION_TYPE": "x11", "XAUTHORITY": ""},
        }
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0 if cmd[-1] == "gnome-shell" else 1, "11 12\n", ""
            ),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: environs[pid]),
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError("pgrep"),
            subprocess.TimeoutExpired(["pgrep"], 2),
        ],
    )
    def test_pgrep_failure_is_survived(
        self, service_module, plugin, monkeypatch, no_extras, error
    ):
        def run(cmd, **kw):
            raise error

        monkeypatch.setattr(service_module.subprocess, "run", run)

        assert plugin._get_session_env(FALLBACK_UID, no_extras) == {}

    def test_unreadable_process_environment_gives_nothing(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "11\n", ""),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: {}),
        )

        assert plugin._get_session_env(FALLBACK_UID, no_extras) == {}

    @pytest.mark.parametrize("present", [True, False])
    def test_xauthority_in_home_is_used_when_present(
        self, service_module, plugin, monkeypatch, no_extras, present
    ):
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""),
        )
        xauthority = f"{no_extras}/.Xauthority"
        monkeypatch.setattr(
            service_module.os.path, "exists", lambda p: present and p == xauthority
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == ({"XAUTHORITY": xauthority} if present else {})

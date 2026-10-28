"""
Tests for the vpnc-script disconnect after gpclient was killed (issue #162).

On SIGTERM gpclient logs out and openconnect runs vpnc-script with
reason=disconnect. After a SIGKILL that never happens, so the service runs the
script itself, with the environment the notify hook reported at connect.

Fake scripts in a temp directory; no real gpclient or vpnc-script. The notify
hook passes the path of the script; the service checks it before it runs it as
root (the owner check is faked when the tests do not run as root).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import logging
import os
import subprocess

import pytest
from test_terminate import FakeGpclient, GonePid, fast_clock  # noqa: F401
from vpnc_notify_helpers import active_plugin, notify, vpnc_env

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HOOK = os.path.join(ROOT, "config", "gpclient-nm-notify")
NM_SCRIPT = os.path.join(ROOT, "config", "gpclient-nm-script")
pytestmark = pytest.mark.usefixtures("fast_clock")
EXCLUDES = {
    "CISCO_SPLIT_EXC": "1",
    "CISCO_SPLIT_EXC_0_ADDR": "192.0.2.0",
    "CISCO_SPLIT_EXC_0_MASK": "255.255.255.0",
    "CISCO_SPLIT_EXC_0_MASKLEN": "24",
    "CISCO_IPV6_SPLIT_EXC": "1",
    "CISCO_IPV6_SPLIT_EXC_0_ADDR": "2001:db8::",
    "CISCO_IPV6_SPLIT_EXC_0_MASKLEN": "32",
}


@pytest.fixture
def make_script(tmp_path, monkeypatch, service_module):
    """Create an executable script that writes its environment to out.txt.
    Without root, the stat of the script says root owns it."""
    real_stat = os.stat

    class AsRoot:
        def __init__(self, result):
            self._result = result
            self.st_uid = 0

        def __getattr__(self, name):
            return getattr(self._result, name)

    def make(body="env > {out}\n", mode=0o755):
        path = tmp_path / "vpnc-script"
        path.write_text("#!/bin/sh\n" + body.format(out=tmp_path / "out.txt"))
        path.chmod(mode)
        return str(path)

    if os.getuid() != 0:
        monkeypatch.setattr(
            service_module.os,
            "stat",
            lambda p, *a, **k: AsRoot(real_stat(p, *a, **k))
            if str(p).startswith(str(tmp_path))
            else real_stat(p, *a, **k),
        )
    make.out = tmp_path / "out.txt"
    return make


def _ran(make_script):
    if not make_script.out.exists():
        return None
    return dict(
        line.split("=", 1) for line in make_script.out.read_text().splitlines() if "=" in line
    )


def _connected(service_module, script, **extra):
    plugin = active_plugin(service_module)
    env = vpnc_env(VPNPID="777", **EXCLUDES, **extra)
    if script is not None:
        env["GPCLIENT_VPNC_SCRIPT"] = script
    notify(plugin, env)
    return plugin


def _stop(plugin, process):
    plugin.gpclient_process = process
    asyncio.run(plugin._stop_gpclient())


class TestRunAfterKill:
    def test_the_script_runs_with_reason_disconnect_and_its_environment(
        self, service_module, make_script, monkeypatch
    ):
        monkeypatch.setenv("GPCLIENT_NM_TOKEN", "secret-token")
        plugin = _connected(service_module, make_script())
        process = FakeGpclient(exit_after=None)

        _stop(plugin, process)

        got = _ran(make_script)
        assert process.signals[-1] == 9
        assert got["reason"] == "disconnect"
        assert got["PATH"] == service_module.VPNC_DISCONNECT_PATH
        for name, value in {"TUNDEV": "tun0", "VPNGATEWAY": "203.0.113.9",
                            "VPNPID": "777", **EXCLUDES}.items():
            assert got[name] == value
        assert "GPCLIENT_VPNC_SCRIPT" not in got
        assert "GPCLIENT_NM_TOKEN" not in got
        assert "secret-token" not in make_script.out.read_text()
        assert plugin._vpnc_disconnect is None

    def test_it_runs_once(self, service_module, make_script):
        plugin = _connected(service_module, make_script())
        _stop(plugin, FakeGpclient(exit_after=None))
        make_script.out.unlink()

        _stop(plugin, FakeGpclient(exit_after=None))

        assert _ran(make_script) is None

    def test_a_script_that_hangs_is_killed_and_the_shutdown_goes_on(
        self, service_module, make_script, monkeypatch
    ):
        monkeypatch.setattr(service_module, "VPNC_DISCONNECT_TIMEOUT", 4)
        plugin = _connected(service_module, make_script("echo started > {out}\nexec sleep 30\n"))
        process = FakeGpclient(exit_after=None)

        _stop(plugin, process)  # returns: the script was killed at the timeout

        assert make_script.out.read_text() == "started\n"
        assert process.returncode == -9

    def test_a_failing_script_is_logged_and_does_not_raise(
        self, service_module, make_script, caplog
    ):
        plugin = _connected(service_module, make_script("echo broken >&2\nexit 3\n"))

        with caplog.at_level(logging.DEBUG):
            _stop(plugin, FakeGpclient(exit_after=None))

        assert any(
            r.levelno == logging.WARNING and "exited 3" in r.message and "broken" in r.message
            for r in caplog.records
        )

    def test_a_script_that_cannot_start_does_not_raise(
        self, service_module, make_script, caplog, monkeypatch
    ):
        plugin = _connected(service_module, make_script())

        async def broken(*args, **kwargs):
            raise OSError("exec format error")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", broken)

        _stop(plugin, FakeGpclient(exit_after=None))

        assert "Error running vpnc-script disconnect" in caplog.text

    def test_the_warning_names_the_script(self, service_module, make_script, caplog):
        script = make_script()
        plugin = _connected(service_module, script)

        _stop(plugin, FakeGpclient(exit_after=None))

        assert f"running {script} with reason=disconnect" in caplog.text


class TestNoRun:
    def test_sigterm_was_enough(self, service_module, make_script):
        plugin = _connected(service_module, make_script())

        _stop(plugin, FakeGpclient(exit_after=3))

        assert _ran(make_script) is None

    def test_no_notify_no_context(self, service_module, make_script):
        make_script()
        plugin = active_plugin(service_module)

        _stop(plugin, FakeGpclient(exit_after=None))

        assert _ran(make_script) is None

    def test_a_notify_without_the_path_remembers_nothing(
        self, service_module, make_script
    ):
        # nm-routing: gpclient-nm-script does not send it
        make_script()
        plugin = _connected(service_module, None)

        _stop(plugin, FakeGpclient(exit_after=None))

        assert plugin._vpnc_disconnect is None
        assert _ran(make_script) is None

    def test_the_teardown_clears_the_context(
        self, service_module, make_script, monkeypatch
    ):
        plugin = _connected(service_module, make_script())

        async def noop():
            pass

        monkeypatch.setattr(plugin, "_remember_gpauth", noop)
        monkeypatch.setattr(plugin, "_stop_remembered_gpauth", noop)
        plugin.gpclient_process = FakeGpclient(exit_after=3)
        asyncio.run(plugin._teardown())
        assert plugin._vpnc_disconnect is None

        _stop(plugin, FakeGpclient(exit_after=None))

        assert _ran(make_script) is None

    def test_a_process_that_is_gone_is_not_a_kill(self, service_module, make_script):

        plugin = _connected(service_module, make_script())

        _stop(plugin, GonePid(exit_after=None))

        assert _ran(make_script) is None


class TestContext:
    def test_a_reconnect_keeps_the_connect_environment(
        self, service_module, make_script
    ):
        # the reconnect carries the gateway's raw lists (the routing hook does
        # not run for it)
        plugin = _connected(service_module, make_script())
        notify(
            plugin,
            vpnc_env(
                VPNPID="777",
                GPCLIENT_VPNC_SCRIPT=plugin._vpnc_disconnect[0],
                CISCO_SPLIT_EXC="1",
                CISCO_SPLIT_EXC_0_ADDR="198.51.100.0",
                CISCO_SPLIT_EXC_0_MASKLEN="24",
            ),
            reason="reconnect",
        )

        _stop(plugin, FakeGpclient(exit_after=None))

        got = _ran(make_script)
        assert got["CISCO_SPLIT_EXC_0_ADDR"] == "192.0.2.0"

    def test_a_reconnect_alone_remembers_nothing(self, service_module, make_script):
        plugin = _connected(service_module, None)
        notify(plugin, vpnc_env(GPCLIENT_VPNC_SCRIPT=make_script()), reason="reconnect")

        assert plugin._vpnc_disconnect is None

    @pytest.mark.parametrize(
        "name, value",
        [("PATH", "/tmp/evil"), ("reason", "connect"), ("LD_PRELOAD", "/tmp/x.so"),
         ("BASH_ENV", "/tmp/x"), ("ENV", "/tmp/x"), ("HOME", "/tmp")],
    )
    def test_only_the_listed_variables_reach_the_script(
        self, service_module, make_script, name, value
    ):
        plugin = active_plugin(service_module)
        notify(plugin, vpnc_env(GPCLIENT_VPNC_SCRIPT=make_script(), **{name: value}))

        _stop(plugin, FakeGpclient(exit_after=None))

        got = _ran(make_script)
        assert got["reason"] == "disconnect"
        assert got["PATH"] == service_module.VPNC_DISCONNECT_PATH
        assert got.get(name) != value or name in ("PATH", "reason")

    @pytest.mark.parametrize("change", ["writable", "removed"])
    def test_a_script_that_changed_since_the_notification_is_not_run(
        self, service_module, make_script, caplog, change
    ):
        script = make_script()
        plugin = _connected(service_module, script)
        if change == "removed":
            os.remove(script)
        else:
            os.chmod(script, 0o777)

        _stop(plugin, FakeGpclient(exit_after=None))

        assert _ran(make_script) is None
        assert "unsafe now" in caplog.text


class TestPathCheck:
    def _bad(self, service_module, script, caplog):
        plugin = _connected(service_module, script)
        assert plugin._vpnc_disconnect is None
        assert "unsafe" in caplog.text
        assert caplog.text.count("unsafe") == 1  # once per notification

    def test_a_good_script_is_remembered(self, service_module, make_script):
        script = make_script()

        plugin = _connected(service_module, script)

        path, env = plugin._vpnc_disconnect
        assert path == script
        assert "GPCLIENT_VPNC_SCRIPT" not in env
        assert env["VPNPID"] == "777" and env["CISCO_SPLIT_EXC_0_ADDR"] == "192.0.2.0"

    def test_a_bad_vpnpid_is_dropped(self, service_module, make_script):
        plugin = active_plugin(service_module)
        notify(plugin, vpnc_env(VPNPID="../x", GPCLIENT_VPNC_SCRIPT=make_script()))

        assert "VPNPID" not in plugin._vpnc_disconnect[1]

    @pytest.mark.parametrize("path", ["", "vpnc-script", "./vpnc-script", "/nonexistent/vpnc"])
    def test_a_relative_or_missing_path(self, service_module, make_script, caplog, path):
        self._bad(service_module, path, caplog)

    @pytest.mark.parametrize("mode", [0o644, 0o775, 0o757, 0o777])
    def test_not_executable_or_writable_by_others(
        self, service_module, make_script, caplog, mode
    ):
        self._bad(service_module, make_script(mode=mode), caplog)

    def test_a_directory(self, service_module, make_script, caplog, tmp_path):
        self._bad(service_module, str(tmp_path), caplog)

    def test_not_owned_by_root(self, service_module, tmp_path, caplog):
        # No faked owner here: the file is the test user's. As root it is given away.
        script = tmp_path / "vpnc-script"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        if os.getuid() == 0:
            os.chown(script, 65534, 65534)

        self._bad(service_module, str(script), caplog)

    def test_a_link_is_judged_by_its_target(self, service_module, make_script, tmp_path, caplog):
        target = make_script(mode=0o777)
        link = tmp_path / "link"
        link.symlink_to(target)

        self._bad(service_module, str(link), caplog)

    def test_a_new_gpclient_clears_the_context(self, service_module, make_script):
        plugin = _connected(service_module, make_script())
        assert plugin._vpnc_disconnect is not None
        # the reset next to the token of every start of gpclient
        source = open(service_module.__file__, encoding="utf-8").read()
        assert "self._notify_token = secrets.token_hex(16)\n            self._vpnc_disconnect = None" in source


class TestEnvironmentList:
    def test_the_list_has_what_the_disconnect_reads(self, service_module):
        names = set(service_module.VPNC_NOTIFY_ENV)

        assert {"VPNPID", "GPCLIENT_VPNC_SCRIPT", "CISCO_SPLIT_EXC", "CISCO_IPV6_SPLIT_EXC",
                "CISCO_SPLIT_EXC_255_MASKLEN", "CISCO_IPV6_SPLIT_EXC_0_MASKLEN"} <= names
        assert "CISCO_SPLIT_EXC_256_ADDR" not in names  # VPNC_SPLIT_MAX

    def test_vpnc_notify_env_forwards_them(self, service_module):
        environ = {"reason": "connect", "GPCLIENT_VPNC_SCRIPT": "/x", "VPNPID": "5",
                   "HOME": "/root", **EXCLUDES}

        _, _, env = service_module.vpnc_notify_env(environ)

        assert env == {k: v for k, v in environ.items() if k not in ("reason", "HOME")}


class TestHooks:
    def _run(self, hook, tmp_path):
        notify_prog = tmp_path / "notify"
        notify_prog.write_text(
            f'#!/bin/sh\necho "${{GPCLIENT_VPNC_SCRIPT-unset}}" > {tmp_path}/got\n'
        )
        notify_prog.chmod(0o755)
        fake = tmp_path / "vpnc-script"  # sources the hook like the real one
        fake.write_text(f'#!/bin/sh\nreason=connect\n. "{hook}"\n')
        fake.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "GPCLIENT_NM_TOKEN": "t",
               "GPCLIENT_NM_NOTIFY": str(notify_prog), "reason": "connect"}
        subprocess.run([str(fake)], env=env, check=True, capture_output=True)
        return (tmp_path / "got").read_text().strip(), str(fake)

    def test_the_notify_hook_passes_the_path_of_vpnc_script(self, tmp_path):
        got, fake = self._run(HOOK, tmp_path)

        assert got == fake

    def test_nm_routing_script_does_not(self, tmp_path):
        got, _ = self._run(NM_SCRIPT, tmp_path)

        assert got == "unset"

    def test_the_hook_does_not_leak_it_into_vpnc_script(self, tmp_path):
        notify_prog = tmp_path / "notify"
        notify_prog.write_text("#!/bin/sh\nexit 0\n")
        notify_prog.chmod(0o755)
        fake = tmp_path / "vpnc-script"
        fake.write_text(f'#!/bin/sh\n. "{HOOK}"\necho "${{GPCLIENT_VPNC_SCRIPT-unset}}"\n')
        fake.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "GPCLIENT_NM_TOKEN": "t",
               "GPCLIENT_NM_NOTIFY": str(notify_prog), "reason": "connect"}

        out = subprocess.run([str(fake)], env=env, capture_output=True, text=True,
                             check=True).stdout

        assert out.strip() == "unset"

"""
Tests for stopping gpclient (issue #34).

gpclient 3.x logs out of every gateway session when it gets SIGTERM, each with
its own 5 s timeout. The service must wait longer than that before it SIGKILLs
gpclient, or the logout is cut and the gateway keeps a stale lock.

The tests use a fake process and a fast clock: every timeout the service passes
to asyncio.wait_for is divided by SCALE, so no test waits for 10 s.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import signal
import sys

import pytest

SCALE = 20  # one second of the service is 1/20 s here


class FakeGpclient:
    """Stand-in for the asyncio process: exits `exit_after` seconds after
    SIGTERM (None: never, unless it is killed)"""

    pid = 4242

    def __init__(self, exit_after, events=None):
        self.exit_after = exit_after
        self.events = events if events is not None else []
        self.signals = []
        self.returncode = None
        self._exited = None

    def _exit(self, code):
        self.events.append("exited")
        self.returncode = code
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(None)

    def terminate(self):
        self.events.append("terminate")
        self.signals.append(signal.SIGTERM)
        if self.exit_after is not None:
            asyncio.get_running_loop().call_later(
                self.exit_after / SCALE, self._exit, 0
            )

    def kill(self):
        self.events.append("kill")
        self.signals.append(signal.SIGKILL)
        self._exit(-9)

    async def wait(self):
        if self._exited is None:
            self._exited = asyncio.get_running_loop().create_future()
        if self.returncode is None:
            await self._exited
        return self.returncode


class FakeDisconnect:
    async def wait(self):
        return 0


@pytest.fixture
def fast_clock(monkeypatch):
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout / SCALE)

    async def no_disconnect(*_args, **_kwargs):
        return FakeDisconnect()

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    # `gpclient disconnect` after the stop must not run the real binary
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_disconnect)


def _disconnect(service_module, process):
    plugin = service_module.GpclientVPNPlugin()
    plugin.gpclient_process = process
    asyncio.run(plugin.Disconnect())


def test_terminate_waits_for_gateway_logout(service_module, fast_clock, dbus_signals):
    # Logs out for 7 s: longer than the old 5 s, shorter than the new timeout
    process = FakeGpclient(exit_after=7)

    _disconnect(service_module, process)

    assert process.signals == [signal.SIGTERM]
    assert process.returncode == 0


def test_terminate_kills_after_timeout(service_module, fast_clock, dbus_signals):
    process = FakeGpclient(exit_after=None)

    _disconnect(service_module, process)

    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.returncode == -9


def test_the_timeout_is_longer_than_the_logout_of_gpclient(service_module):
    # 5 s is the logout timeout of gpclient 3.x for each gateway session
    assert service_module.GPCLIENT_TERMINATE_TIMEOUT > 5


# --- the order: stop gpclient first, close the PTY after --------------------
#
# gpclient is the session leader of the PTY. Closing the master sends it SIGHUP,
# which gpclient 3.x does not handle: it would die at once and skip the logout.


def _recording_plugin(service_module, monkeypatch, process):
    plugin = service_module.GpclientVPNPlugin()
    plugin.gpclient_process = process
    monkeypatch.setattr(
        plugin, "_close_pty", lambda: process.events.append("close_pty")
    )
    return plugin


def test_disconnect_stops_gpclient_before_it_closes_the_pty(
    service_module, fast_clock, dbus_signals, monkeypatch
):
    process = FakeGpclient(exit_after=7)
    plugin = _recording_plugin(service_module, monkeypatch, process)

    asyncio.run(plugin.Disconnect())

    assert process.events == ["terminate", "exited", "close_pty"]


def test_disconnect_closes_the_pty_after_the_kill_too(
    service_module, fast_clock, dbus_signals, monkeypatch
):
    process = FakeGpclient(exit_after=None)
    plugin = _recording_plugin(service_module, monkeypatch, process)

    asyncio.run(plugin.Disconnect())

    assert process.events == ["terminate", "kill", "exited", "close_pty"]


def test_disconnect_without_gpclient_still_closes_the_pty(
    service_module, fast_clock, dbus_signals, monkeypatch
):
    events = []
    plugin = service_module.GpclientVPNPlugin()
    monkeypatch.setattr(plugin, "_close_pty", lambda: events.append("close_pty"))

    asyncio.run(plugin.Disconnect())

    assert events == ["close_pty"]


# gpclient stand-in: handles SIGTERM only (as gpclient 3.x does) and needs
# 0.5 s for the "logout". SIGHUP is not handled: it kills the process.
LOGS_OUT_ON_SIGTERM = r"""
import pathlib, signal, sys, time

def on_term(*_):
    time.sleep(0.5)
    sys.exit(0)

signal.signal(signal.SIGTERM, on_term)
pathlib.Path(sys.argv[1]).touch()
time.sleep(60)
"""


def test_gpclient_on_a_real_pty_is_stopped_by_sigterm_not_by_sighup(
    service_module, monkeypatch, tmp_path, dbus_signals
):
    ready = tmp_path / "ready"
    script = tmp_path / "gpclient.py"
    script.write_text(LOGS_OUT_ON_SIGTERM)
    launcher = tmp_path / "gpclient.sh"
    launcher.write_text(
        f'#!/bin/bash\nexec "{sys.executable}" "{script}" "{ready}"\n'
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(launcher))

    async def no_disconnect(*_args, **_kwargs):
        return FakeDisconnect()

    async def run():
        plugin = service_module.GpclientVPNPlugin()
        plugin.gateway = "portal.example.com"
        plugin.browser = "/bin/true"
        assert await plugin._start_gpclient()
        process = plugin.gpclient_process
        try:
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(0.05)
            assert ready.exists()
            monkeypatch.setattr(asyncio, "create_subprocess_exec", no_disconnect)
            await plugin.Disconnect()
            return process.returncode
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    # 0: gpclient finished its SIGTERM handler. -1 would mean that the PTY
    # was closed first and SIGHUP killed it in the middle of the logout.
    assert asyncio.run(run()) == 0


# --- _stop_gpclient: the stop sequence of Disconnect and of the shutdown ------


class GonePid(FakeGpclient):
    def terminate(self):
        self.events.append("terminate")
        raise ProcessLookupError


def _stop(service_module, process):
    plugin = service_module.GpclientVPNPlugin()
    plugin.gpclient_process = process
    asyncio.run(plugin._stop_gpclient())


def test_stop_gpclient_does_not_kill_a_process_that_exits_in_time(
    service_module, fast_clock
):
    process = FakeGpclient(exit_after=7)

    _stop(service_module, process)

    assert process.signals == [signal.SIGTERM]
    assert process.returncode == 0


def test_stop_gpclient_kills_a_process_that_does_not_exit(service_module, fast_clock):
    process = FakeGpclient(exit_after=None)

    _stop(service_module, process)

    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.returncode == -9


def test_stop_gpclient_ignores_a_process_that_is_gone(service_module, fast_clock):
    process = GonePid(exit_after=None)

    _stop(service_module, process)

    assert process.events == ["terminate"]


def test_stop_gpclient_without_a_process_does_nothing(service_module, fast_clock):
    _stop(service_module, None)

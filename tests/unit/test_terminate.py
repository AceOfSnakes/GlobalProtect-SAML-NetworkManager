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

import pytest

SCALE = 20  # one second of the service is 1/20 s here


class FakeGpclient:
    """Stand-in for the asyncio process: exits `exit_after` seconds after
    SIGTERM (None: never, unless it is killed)"""

    pid = 4242

    def __init__(self, exit_after):
        self.exit_after = exit_after
        self.signals = []
        self.returncode = None
        self._exited = None

    def _exit(self, code):
        self.returncode = code
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(None)

    def terminate(self):
        self.signals.append(signal.SIGTERM)
        if self.exit_after is not None:
            asyncio.get_running_loop().call_later(
                self.exit_after / SCALE, self._exit, 0
            )

    def kill(self):
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

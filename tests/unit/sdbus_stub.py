"""
The `sdbus` stub for the unit tests of the nm-gpclient D-Bus service.

Its own module (not conftest.py) so that a test can import it by an
unambiguous name: there is a conftest.py in tests/ and another one in
tests/unit/, and `from conftest import ...` may pick either.

The service imports the `sdbus` module at import time, which is not available
(and not needed) for unit-testing the pure parsing helpers.
"""

import contextlib
import sys
import types


@contextlib.contextmanager
def sdbus_stubbed():
    """Make `sdbus` the stub, whatever the host has installed or imported.

    Installed even when a real sdbus is available: a real one has no signal
    recording, so the tests would depend on the machine they run on. The
    previous `sys.modules` entry (or its absence) comes back on exit, so the
    stub does not outlive the test session.
    """
    missing = object()
    previous = sys.modules.get("sdbus", missing)
    stub = _build_sdbus_stub()
    sys.modules["sdbus"] = stub
    try:
        yield stub
    finally:
        if previous is missing:
            sys.modules.pop("sdbus", None)
        else:
            sys.modules["sdbus"] = previous


def _build_sdbus_stub():
    stub = types.ModuleType("sdbus")

    class DbusInterfaceCommonAsync:
        def __init_subclass__(cls, **kwargs):
            pass

    # D-Bus signals are emitted as `self.SignalName.emit(payload)`. Give the
    # decorated functions an `emit` attribute (bound methods expose the
    # function's attributes) that records the call, so tests can assert what the
    # service reported to NetworkManager.
    signal_calls = []

    def _decorator_factory(*_args, **_kwargs):
        def decorator(func):
            func.emit = lambda *payload: signal_calls.append(
                (func.__name__, payload[0] if len(payload) == 1 else payload)
            )
            return func

        return decorator

    stub.DbusInterfaceCommonAsync = DbusInterfaceCommonAsync
    stub.dbus_method_async = _decorator_factory
    stub.dbus_property_async = _decorator_factory
    stub.dbus_signal_async = _decorator_factory

    async def _noop_async(*_args, **_kwargs):
        return None

    stub.request_default_bus_name_async = _noop_async
    stub.sd_bus_open_system = lambda: None
    stub.set_default_bus = lambda bus: None
    stub.SIGNAL_CALLS = signal_calls
    return stub

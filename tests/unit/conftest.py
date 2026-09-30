"""
Fixtures for unit tests of the nm-gpclient D-Bus service.

The service imports the `sdbus` module at import time, which is not
available (and not needed) for unit-testing the pure parsing helpers.
A minimal stub is injected before the service module is loaded.
"""

import contextlib
import importlib.util
import os
import sys
import types

import pytest

SERVICE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "service", "nm-gpclient-service.py"
)


_SDBUS_STUB = None


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


@pytest.fixture(scope="session")
def service_module():
    """Import service/nm-gpclient-service.py as a module (with sdbus stubbed).

    The service binds its signal decorators to the stub at import time, so the
    stub stays in place for the whole session and is removed at teardown.
    """
    global _SDBUS_STUB
    with sdbus_stubbed() as stub:
        _SDBUS_STUB = stub
        try:
            spec = importlib.util.spec_from_file_location(
                "nm_gpclient_service", os.path.abspath(SERVICE_PATH)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield module
        finally:
            _SDBUS_STUB = None


@pytest.fixture
def dbus_signals(service_module):
    """D-Bus signals the service emitted, as (name, payload) - cleared per test"""
    calls = _SDBUS_STUB.SIGNAL_CALLS
    calls.clear()
    yield calls
    calls.clear()


@pytest.fixture(autouse=True)
def dns_state_sandbox(service_module, monkeypatch, tmp_path):
    """Keep the vpnc hook's DNS state file out of /run and make tunnel
    detection not wait for it unless a test asks to (issue #15)."""
    monkeypatch.setattr(service_module, "DNS_STATE_FILE", str(tmp_path / "dns-state"))
    monkeypatch.setattr(service_module, "DNS_STATE_WAIT_ROUNDS", 0)
    yield tmp_path / "dns-state"

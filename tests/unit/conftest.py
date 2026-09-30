"""
Fixtures for unit tests of the nm-gpclient D-Bus service.

The service imports the `sdbus` module at import time, which is not
available (and not needed) for unit-testing the pure parsing helpers.
A minimal stub is injected before the service module is loaded.
"""

import importlib.util
import os

import pytest
from sdbus_stub import sdbus_stubbed

SERVICE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "service", "nm-gpclient-service.py"
)


_SDBUS_STUB = None


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

"""
The unit tests must not depend on the host having (or lacking) python-sdbus.

conftest.py installs a stub `sdbus` that records emitted D-Bus signals. With a
real sdbus already imported the stub used to be skipped, and every test using
`dbus_signals` broke on such a machine.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import sys
import types


def _fake_real_sdbus():
    """What a genuine sdbus looks like to the tests: no recording hooks"""
    module = types.ModuleType("sdbus")
    module.__file__ = "/usr/lib/python3/dist-packages/sdbus/__init__.py"
    return module


class TestSdbusStub:
    def test_stub_replaces_a_real_sdbus(self, request, monkeypatch, service_module):
        real = _fake_real_sdbus()
        monkeypatch.setitem(sys.modules, "sdbus", real)

        signals = request.getfixturevalue("dbus_signals")

        assert sys.modules["sdbus"] is not real
        assert not hasattr(real, "SIGNAL_CALLS")
        # The service was imported once with the stub: signals it emits must
        # still end up in the list the fixture hands out
        plugin = service_module.GpclientVPNPlugin()
        plugin._fail_login("no way to ask for the token")
        assert signals == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED),
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
        ]

    def test_signals_start_empty_for_every_test(self, dbus_signals):
        # Nothing leaks in from the test above
        assert dbus_signals == []

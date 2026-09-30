"""
The unit tests must not depend on the host having (or lacking) python-sdbus,
and must not change what other code in the same process sees as `sdbus`.

conftest.py installs a stub `sdbus` that records emitted D-Bus signals while
the `service_module` fixture is alive and puts the previous entry back at the
end of the session. With a real sdbus already imported the stub used to be
skipped, and every test using `dbus_signals` broke on such a machine.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import sys
import types

from sdbus_stub import sdbus_stubbed


def _fake_real_sdbus():
    """What a genuine sdbus looks like to the tests: no recording hooks"""
    module = types.ModuleType("sdbus")
    module.__file__ = "/usr/lib/python3/dist-packages/sdbus/__init__.py"
    return module


class TestSdbusStub:
    def test_stub_replaces_a_real_sdbus(self, monkeypatch):
        real = _fake_real_sdbus()
        monkeypatch.setitem(sys.modules, "sdbus", real)

        with sdbus_stubbed() as stub:
            assert sys.modules["sdbus"] is stub
            assert stub is not real
            assert hasattr(stub, "SIGNAL_CALLS")
        assert not hasattr(real, "SIGNAL_CALLS")

    def test_a_real_sdbus_is_put_back(self, monkeypatch):
        real = _fake_real_sdbus()
        monkeypatch.setitem(sys.modules, "sdbus", real)

        with sdbus_stubbed():
            pass

        assert sys.modules["sdbus"] is real

    def test_no_sdbus_at_all_is_put_back_as_none(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "sdbus", raising=False)

        with sdbus_stubbed():
            assert "sdbus" in sys.modules

        assert "sdbus" not in sys.modules

    def test_the_stub_is_removed_even_when_the_body_fails(self, monkeypatch):
        real = _fake_real_sdbus()
        monkeypatch.setitem(sys.modules, "sdbus", real)

        try:
            with sdbus_stubbed():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert sys.modules["sdbus"] is real

    def test_session_stub_feeds_the_signal_fixture(
        self, service_module, dbus_signals
    ):
        # Even if the host had a real sdbus, the service was imported with the
        # stub: signals it emits must end up in the list the fixture hands out
        plugin = service_module.GpclientVPNPlugin()
        plugin._fail_login("no way to ask for the token")
        assert dbus_signals == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED),
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
        ]

    def test_signals_start_empty_for_every_test(self, dbus_signals):
        # Nothing leaks in from the test above
        assert dbus_signals == []

"""
Tests for the service entry points main_async() and main().

No real D-Bus, no real signals: the bus functions, the plugin class and the
loop's add_signal_handler are replaced. The signal callbacks that the service
registers are called by the test itself.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import logging
import signal
import types

import pytest


class FakePlugin:
    """Stands in for GpclientVPNPlugin: records what main_async does to it"""

    instances = []

    def __init__(self):
        self.exported = []
        self.stopped = 0
        self.idle_callback = None
        FakePlugin.instances.append(self)

    def export_to_dbus(self, path):
        self.exported.append(path)

    def enable_idle_exit(self, callback):
        self.idle_callback = callback

    async def _stop_gpclient(self):
        self.stopped += 1


@pytest.fixture
def env(service_module, monkeypatch):
    """Fake bus functions and plugin; the logger level is restored afterwards"""
    logger = service_module.logger
    saved_level = logger.level
    logger.setLevel(logging.INFO)
    FakePlugin.instances = []

    calls = types.SimpleNamespace(
        opened=[], default_bus=[], names=[], name_error=None
    )
    bus = object()

    def open_system():
        calls.opened.append(True)
        return bus

    async def request_name(name):
        calls.names.append(name)
        if calls.name_error is not None:
            raise calls.name_error

    monkeypatch.setattr(service_module, "sd_bus_open_system", open_system)
    monkeypatch.setattr(
        service_module, "set_default_bus", lambda b: calls.default_bus.append(b)
    )
    monkeypatch.setattr(service_module, "request_default_bus_name_async", request_name)
    monkeypatch.setattr(service_module, "GpclientVPNPlugin", FakePlugin)
    monkeypatch.setattr(service_module.sys, "argv", ["nm-gpclient-service"])
    monkeypatch.delenv("GPCLIENT_DEBUG", raising=False)
    monkeypatch.delenv("NM_VPN_LOG_LEVEL", raising=False)
    calls.bus = bus
    yield calls
    logger.setLevel(saved_level)


def run_service(service_module, fire=True):
    """Run main_async with a fake add_signal_handler.

    Returns (result, handlers). When `fire` is true, the SIGTERM callback is
    called once both handlers are registered.
    """
    handlers = {}

    async def driver():
        loop = asyncio.get_running_loop()
        loop.add_signal_handler = lambda sig, cb: handlers.__setitem__(sig, cb)
        task = asyncio.ensure_future(service_module.main_async())
        while len(handlers) < 2 and not task.done():  # noqa: ASYNC110  # yields to the loop until the service registers its handlers
            await asyncio.sleep(0)
        if fire and not task.done():
            handlers[signal.SIGTERM]()
        return await task

    return asyncio.run(driver()), handlers


class TestStartup:
    def test_returns_zero_and_sets_up_the_bus(self, service_module, env):
        result, _ = run_service(service_module)

        assert result == 0
        assert env.opened == [True]
        assert env.default_bus == [env.bus]
        assert env.names == [service_module.NM_DBUS_SERVICE_GPCLIENT]

    def test_exports_plugin_on_its_path(self, service_module, env):
        run_service(service_module)

        (plugin,) = FakePlugin.instances
        assert plugin.exported == [service_module.NM_DBUS_PATH_GPCLIENT]

    def test_persist_flag_is_accepted(self, service_module, env, monkeypatch):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--persist"])

        result, _ = run_service(service_module)

        assert result == 0

    def test_unknown_argument_is_rejected(self, service_module, env, monkeypatch):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--bogus"])

        with pytest.raises(SystemExit) as exc:
            run_service(service_module)

        assert exc.value.code == 2
        # Nothing was set up
        assert env.opened == []
        assert FakePlugin.instances == []


class TestBusName:
    def test_name_owned_elsewhere_exits_with_message(
        self, service_module, env, capsys
    ):
        class SdBusRequestNameExistsError(Exception):
            pass

        env.name_error = SdBusRequestNameExistsError("exists")

        with pytest.raises(SystemExit) as exc:
            run_service(service_module)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert service_module.NM_DBUS_SERVICE_GPCLIENT in err
        assert "already owned" in err
        # The plugin is not created when the name is not ours
        assert FakePlugin.instances == []

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("boom"), PermissionError("denied"), ValueError("x")],
    )
    def test_other_errors_are_re_raised(self, service_module, env, capsys, error):
        env.name_error = error

        with pytest.raises(type(error)):
            run_service(service_module)

        assert "already owned" not in capsys.readouterr().err
        assert FakePlugin.instances == []


class TestDebugMode:
    def test_default_is_not_debug(self, service_module, env):
        run_service(service_module)

        assert service_module.logger.level != logging.DEBUG

    def test_debug_flag(self, service_module, env, monkeypatch):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--debug"])

        run_service(service_module)

        assert service_module.logger.level == logging.DEBUG

    def test_env_variable_enables_debug(self, service_module, env, monkeypatch):
        monkeypatch.setenv("GPCLIENT_DEBUG", "1")

        run_service(service_module)

        assert service_module.logger.level == logging.DEBUG

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
    def test_other_env_values_do_not_enable_debug(
        self, service_module, env, monkeypatch, value
    ):
        monkeypatch.setenv("GPCLIENT_DEBUG", value)

        run_service(service_module)

        assert service_module.logger.level != logging.DEBUG

    def test_debug_logs_version_info(self, service_module, env, monkeypatch, caplog):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--debug"])
        caplog.set_level(logging.DEBUG, logger=service_module.logger.name)

        run_service(service_module)

        text = caplog.text
        assert "Script path:" in text
        assert "Script MD5:" in text
        assert "Python version:" in text

    def test_no_version_info_without_debug(self, service_module, env, caplog):
        caplog.set_level(logging.INFO, logger=service_module.logger.name)

        run_service(service_module)

        assert "Script MD5:" not in caplog.text

    def test_version_info_failure_is_not_fatal(
        self, service_module, env, monkeypatch, caplog
    ):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--debug"])

        def boom(_path):
            raise OSError("no mtime")

        monkeypatch.setattr(service_module.os.path, "getmtime", boom)
        caplog.set_level(logging.DEBUG, logger=service_module.logger.name)

        result, _ = run_service(service_module)

        assert result == 0
        assert "Failed to compute script hash" in caplog.text
        assert "Script MD5:" not in caplog.text


class TestShutdown:
    def test_registers_sigterm_and_sigint(self, service_module, env):
        _, handlers = run_service(service_module)

        assert set(handlers) == {signal.SIGTERM, signal.SIGINT}

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
    def test_signal_ends_the_service_and_stops_gpclient(
        self, service_module, env, sig
    ):
        handlers = {}

        async def driver():
            loop = asyncio.get_running_loop()
            loop.add_signal_handler = lambda s, cb: handlers.__setitem__(s, cb)
            task = asyncio.ensure_future(service_module.main_async())
            while len(handlers) < 2:  # noqa: ASYNC110  # same
                await asyncio.sleep(0)
            # Still running until a signal arrives
            await asyncio.sleep(0)
            assert not task.done()
            assert FakePlugin.instances[0].stopped == 0
            handlers[sig]()
            return await task

        assert asyncio.run(driver()) == 0
        assert FakePlugin.instances[0].stopped == 1

    def test_error_while_waiting_returns_one_and_still_stops_gpclient(
        self, service_module, env, monkeypatch
    ):
        class BrokenEvent:
            def set(self):
                pass

            async def wait(self):
                raise RuntimeError("wait failed")

        real_asyncio = asyncio
        fake_asyncio = types.SimpleNamespace(
            Event=BrokenEvent,
            get_event_loop=lambda: types.SimpleNamespace(
                add_signal_handler=lambda *a: None
            ),
        )
        monkeypatch.setattr(service_module, "asyncio", fake_asyncio)

        result = real_asyncio.run(service_module.main_async())

        assert result == 1
        assert FakePlugin.instances[0].stopped == 1

    def test_stop_gpclient_error_is_not_swallowed(
        self, service_module, env, monkeypatch
    ):
        class FailingPlugin(FakePlugin):
            async def _stop_gpclient(self):
                raise RuntimeError("stop failed")

        monkeypatch.setattr(service_module, "GpclientVPNPlugin", FailingPlugin)

        with pytest.raises(RuntimeError, match="stop failed"):
            run_service(service_module)


class TestMain:
    def test_main_runs_main_async_and_returns_its_result(
        self, service_module, monkeypatch
    ):
        async def fake_main_async():
            return 7

        monkeypatch.setattr(service_module, "main_async", fake_main_async)

        assert service_module.main() == 7

    def test_main_passes_errors_through(self, service_module, monkeypatch):
        async def fake_main_async():
            raise RuntimeError("fail")

        monkeypatch.setattr(service_module, "main_async", fake_main_async)

        with pytest.raises(RuntimeError, match="fail"):
            service_module.main()


class TestIdleExit:
    def test_idle_callback_ends_the_service(self, service_module, env):
        async def driver():
            loop = asyncio.get_running_loop()
            loop.add_signal_handler = lambda sig, cb: None
            task = asyncio.ensure_future(service_module.main_async())
            while not (FakePlugin.instances and FakePlugin.instances[0].idle_callback):  # noqa: ASYNC110  # yield until main_async wires the plugin
                await asyncio.sleep(0)
            FakePlugin.instances[0].idle_callback()  # no signal arrives
            return await asyncio.wait_for(task, 1)

        result = asyncio.run(driver())

        assert result == 0
        assert FakePlugin.instances[0].stopped == 1

    def test_persist_keeps_the_service_running(
        self, service_module, env, monkeypatch
    ):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--persist"])

        run_service(service_module)

        (plugin,) = FakePlugin.instances
        assert plugin.idle_callback is None

    def test_idle_exit_is_on_by_default(self, service_module, env):
        run_service(service_module)

        (plugin,) = FakePlugin.instances
        assert plugin.idle_callback is not None


class TestLogLevelFromNetworkManager:
    @pytest.mark.parametrize("value", ["7", "8", " 7 "])
    def test_debug_level_enables_debug(self, service_module, env, monkeypatch, value):
        monkeypatch.setenv("NM_VPN_LOG_LEVEL", value)

        run_service(service_module)

        assert service_module.logger.level == logging.DEBUG

    @pytest.mark.parametrize("value", ["6", "0", "-1", "junk", ""])
    def test_other_values_keep_info(self, service_module, env, monkeypatch, value):
        monkeypatch.setenv("NM_VPN_LOG_LEVEL", value)

        run_service(service_module)

        assert service_module.logger.level == logging.INFO

    def test_unset_keeps_info(self, service_module, env):
        run_service(service_module)

        assert service_module.logger.level == logging.INFO

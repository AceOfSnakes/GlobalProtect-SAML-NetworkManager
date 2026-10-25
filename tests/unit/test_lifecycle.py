"""
Tests for the session lifecycle of GpclientVPNPlugin (issue #52): Connect and
Disconnect do not interleave, a crashed session task fails the connection, the
service exits when it is idle, and invalid settings give D-Bus error names.

Everything that touches the system is replaced by a fake.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio

import pytest

CONNECTION = {"vpn": {"data": {"gateway": "vpn.example.com"}}}


class FakeProc:
    """A gpclient process; wait() blocks until `release` is set"""

    def __init__(self, release=None):
        self.pid = 4242
        self.terminated = 0
        self.release = release

    def terminate(self):
        self.terminated += 1

    async def wait(self):
        if self.release is not None:
            await self.release.wait()
        return 0


def _plugin(service_module, monkeypatch, tunnel_loop=None):
    """A plugin whose system-facing methods are fakes.

    `plugin.spawned` lists the processes the fake _start_gpclient created;
    `plugin.closed` counts _close_pty calls.
    """
    plugin = service_module.GpclientVPNPlugin()
    plugin.spawned = []
    plugin.closed = 0
    plugin.blocker = None  # an Event that _snapshot_tunnel_interfaces waits for
    plugin.stop_called = []

    async def noop():
        pass

    async def snapshot():
        if plugin.blocker is not None:
            await plugin.blocker.wait()
        return {}

    async def start():
        proc = FakeProc()
        plugin.spawned.append(proc)
        plugin.gpclient_process = proc
        plugin._pty_master = 1000 + len(plugin.spawned)
        return True

    async def forever():
        await asyncio.sleep(3600)

    def close_pty():
        plugin.closed += 1
        plugin._pty_master = None

    monkeypatch.setattr(plugin, "_cleanup_stale_gpd0", noop)
    monkeypatch.setattr(plugin, "_snapshot_tunnel_interfaces", snapshot)
    monkeypatch.setattr(plugin, "_start_gpclient", start)
    monkeypatch.setattr(plugin, "_check_tunnel_loop", tunnel_loop or forever)
    monkeypatch.setattr(plugin, "_remember_gpauth", noop)
    monkeypatch.setattr(plugin, "_stop_remembered_gpauth", noop)
    monkeypatch.setattr(plugin, "_close_pty", close_pty)
    return plugin


async def _settle(rounds=5):
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _finish(plugin):
    """Stop the tasks of a test session"""
    for task in (plugin.tunnel_check_task, plugin.stdout_monitor_task):
        if task:
            task.cancel()
    await _settle()


def _failures(dbus_signals):
    return [call for call in dbus_signals if call[0] == "Failure"]


# --- Connect and Disconnect do not interleave --------------------------------


class TestQuickToggle:
    def test_connect_waits_for_a_running_disconnect(
        self, service_module, monkeypatch, dbus_signals
    ):
        plugin = _plugin(service_module, monkeypatch)
        old = FakeProc()

        async def go():
            old.release = asyncio.Event()
            plugin.gpclient_process = old
            plugin._pty_master = 7
            disconnect = asyncio.create_task(plugin.Disconnect())
            await _settle()  # Disconnect waits in _stop_gpclient
            connect = asyncio.create_task(plugin._do_connect(CONNECTION, False))
            await _settle()
            assert plugin.spawned == []  # Connect waits for the lock
            old.release.set()
            await asyncio.gather(disconnect, connect)
            result = (
                plugin.gpclient_process,
                plugin._pty_master,
                plugin._state,
                plugin.closed,
            )
            await _finish(plugin)
            return result

        process, pty_master, state, closed = asyncio.run(go())

        assert old.terminated == 1
        assert len(plugin.spawned) == 1
        # The new session is intact: Disconnect did not touch it
        assert process is plugin.spawned[0]
        assert pty_master == 1001
        assert closed == 1  # the old PTY only
        assert state == service_module.NM_VPN_SERVICE_STATE_STARTING
        assert _failures(dbus_signals) == []

    def test_connect_stops_a_session_that_is_still_active(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)
        old = FakeProc()

        async def go():
            plugin.gpclient_process = old
            plugin._pty_master = 7
            await plugin._do_connect(CONNECTION, False)
            result = plugin.gpclient_process
            await _finish(plugin)
            return result

        process = asyncio.run(go())

        assert old.terminated == 1
        assert plugin.closed == 1
        assert process is plugin.spawned[0]

    def test_connect_without_a_session_stops_nothing(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            await plugin._do_connect(CONNECTION, False)
            await _finish(plugin)

        asyncio.run(go())

        assert plugin.closed == 0
        assert len(plugin.spawned) == 1


class TestDisconnectDuringConnect:
    @pytest.mark.parametrize(
        "step", ["_cleanup_stale_gpd0", "_snapshot_tunnel_interfaces", "_start_gpclient"]
    )
    def test_gpclient_is_not_started(
        self, service_module, monkeypatch, dbus_signals, step
    ):
        plugin = _plugin(service_module, monkeypatch)
        blocker = asyncio.Event()

        async def blocked(*_args):
            await blocker.wait()
            # What the real _start_gpclient does when it was superseded
            return False if step == "_start_gpclient" else {}

        monkeypatch.setattr(plugin, step, blocked)

        async def go():
            connect = asyncio.create_task(plugin._do_connect(CONNECTION, False))
            await _settle()  # Connect waits in `step`
            disconnect = asyncio.create_task(plugin.Disconnect())
            await _settle()
            blocker.set()
            await asyncio.gather(connect, disconnect)

        asyncio.run(go())

        assert plugin.spawned == []
        assert plugin.tunnel_check_task is None
        assert _failures(dbus_signals) == []
        assert plugin._state == service_module.NM_VPN_SERVICE_STATE_STOPPED
        assert dbus_signals[-1] == (
            "StateChanged",
            service_module.NM_VPN_SERVICE_STATE_STOPPED,
        )

    def test_start_gpclient_does_not_spawn_after_a_disconnect(
        self, service_module, monkeypatch
    ):
        plugin = service_module.GpclientVPNPlugin()
        spawned = []

        async def spawn(*args, **kwargs):
            spawned.append(args)
            return FakeProc()

        async def disconnect_arrives():
            plugin._generation += 1

        monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(plugin, "_get_real_user", lambda: (1000, "user", "x"))
        monkeypatch.setattr(plugin, "_stop_remembered_gpauth", disconnect_arrives)

        assert asyncio.run(plugin._start_gpclient()) is False
        assert spawned == []

    def test_start_gpclient_spawns_without_a_disconnect(
        self, service_module, monkeypatch
    ):
        plugin = service_module.GpclientVPNPlugin()
        plugin.gateway = "vpn.example.com"
        plugin.browser = "firefox"
        spawned = []

        async def spawn(*args, **kwargs):
            spawned.append(args)
            return FakeProc()

        async def noop():
            pass

        async def monitor():
            pass

        monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(plugin, "_get_real_user", lambda: (1000, "user", "x"))
        monkeypatch.setattr(plugin, "_stop_remembered_gpauth", noop)
        monkeypatch.setattr(plugin, "_monitor_gpclient_output", monitor)

        async def go():
            result = await plugin._start_gpclient()
            await _settle()
            plugin._close_pty()
            return result

        assert asyncio.run(go()) is True
        assert len(spawned) == 1

    def test_gpclient_is_started_when_no_disconnect_comes(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            plugin.blocker = asyncio.Event()
            connect = asyncio.create_task(plugin._do_connect(CONNECTION, False))
            await _settle()
            plugin.blocker.set()
            await connect
            await _finish(plugin)

        asyncio.run(go())

        assert len(plugin.spawned) == 1

    def test_disconnect_after_the_spawn_stops_gpclient(
        self, service_module, monkeypatch, dbus_signals
    ):
        # The lock is not held during the authentication
        plugin = _plugin(service_module, monkeypatch)

        async def stop():
            plugin.stop_called.append(plugin.gpclient_process)

        monkeypatch.setattr(plugin, "_stop_gpclient", stop)

        async def go():
            await plugin._do_connect(CONNECTION, False)
            await asyncio.wait_for(plugin.Disconnect(), 1)

        asyncio.run(go())

        assert plugin.stop_called == plugin.spawned
        assert plugin._state == service_module.NM_VPN_SERVICE_STATE_STOPPED


# --- supervision of the session tasks ----------------------------------------


class TestTaskCrash:
    def test_crash_fails_the_connection_once(
        self, service_module, monkeypatch, dbus_signals, caplog
    ):
        async def crash():
            raise RuntimeError("boom")

        plugin = _plugin(service_module, monkeypatch, tunnel_loop=crash)

        async def go():
            with caplog.at_level("ERROR", logger=service_module.logger.name):
                await plugin._do_connect(CONNECTION, False)
                await _settle()
            await _finish(plugin)

        asyncio.run(go())

        assert _failures(dbus_signals) == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED)
        ]
        assert plugin.spawned[0].terminated == 1
        assert "internal error in tunnel monitor: boom" in caplog.text
        assert "Traceback" in caplog.text

    def test_cancelled_task_reports_nothing(
        self, service_module, monkeypatch, dbus_signals
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            await plugin._do_connect(CONNECTION, False)
            plugin.tunnel_check_task.cancel()
            await _settle()

        asyncio.run(go())

        assert _failures(dbus_signals) == []
        assert plugin.spawned[0].terminated == 0

    def test_task_that_ends_normally_reports_nothing(
        self, service_module, monkeypatch, dbus_signals
    ):
        async def finish():
            return None

        plugin = _plugin(service_module, monkeypatch, tunnel_loop=finish)

        async def go():
            await plugin._do_connect(CONNECTION, False)
            await _settle()

        asyncio.run(go())

        assert _failures(dbus_signals) == []

    def test_crash_of_an_old_session_reports_nothing(
        self, service_module, monkeypatch, dbus_signals, caplog
    ):
        release = None

        async def crash_later():
            await release.wait()
            raise RuntimeError("late")

        plugin = _plugin(service_module, monkeypatch, tunnel_loop=crash_later)

        async def go():
            nonlocal release
            release = asyncio.Event()
            await plugin._do_connect(CONNECTION, False)
            plugin._generation += 1  # a newer session or a Disconnect
            with caplog.at_level("ERROR", logger=service_module.logger.name):
                release.set()
                await _settle()

        asyncio.run(go())

        assert _failures(dbus_signals) == []
        assert plugin.spawned[0].terminated == 0
        assert "late" in caplog.text  # still logged

    @pytest.mark.parametrize("state_name", ["STOPPED", "INIT"])
    def test_crash_without_a_running_session_reports_nothing(
        self, service_module, monkeypatch, dbus_signals, state_name
    ):
        async def crash():
            raise RuntimeError("boom")

        plugin = _plugin(service_module, monkeypatch)

        async def go():
            task = plugin._create_task(crash(), "test task")
            plugin._state = getattr(
                service_module, f"NM_VPN_SERVICE_STATE_{state_name}"
            )
            await _settle()
            assert task.done()

        asyncio.run(go())

        assert _failures(dbus_signals) == []

    def test_disconnect_awaits_the_prompt_task(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        finished = []

        async def prompt():
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0)  # cleanup that needs one more round
                finished.append(True)

        async def go():
            plugin._prompt_task = asyncio.create_task(prompt())
            await _settle()
            await plugin.Disconnect()

        asyncio.run(go())

        assert finished == [True]
        assert plugin._prompt_task is None

    def test_disconnect_survives_a_failed_prompt_task(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def prompt():
            raise RuntimeError("prompt died")

        async def go():
            plugin._prompt_task = asyncio.create_task(prompt())
            await _settle()
            await plugin.Disconnect()

        asyncio.run(go())  # must not raise

        assert plugin._prompt_task is None


# --- idle exit ---------------------------------------------------------------


class TestIdleExit:
    @pytest.fixture(autouse=True)
    def short_timeout(self, service_module, monkeypatch):
        monkeypatch.setattr(service_module, "IDLE_EXIT_TIMEOUT", 0.05)

    def test_fires_without_a_session(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        fired = []

        async def go():
            plugin.enable_idle_exit(lambda: fired.append(True))
            await asyncio.sleep(0.15)

        asyncio.run(go())

        assert fired == [True]

    def test_is_off_until_enabled(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            plugin._set_state(service_module.NM_VPN_SERVICE_STATE_STOPPED)
            await asyncio.sleep(0.15)

        asyncio.run(go())  # nothing to call, nothing raised

        assert plugin._idle_handle is None

    def test_does_not_fire_while_gpclient_runs(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        fired = []

        async def go():
            plugin.gpclient_process = FakeProc()
            plugin.enable_idle_exit(lambda: fired.append(True))
            await asyncio.sleep(0.15)

        asyncio.run(go())

        assert fired == []

    def test_does_not_fire_in_a_running_state(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        fired = []

        async def go():
            plugin._state = service_module.NM_VPN_SERVICE_STATE_STARTED
            plugin.enable_idle_exit(lambda: fired.append(True))
            await asyncio.sleep(0.15)

        asyncio.run(go())

        assert fired == []

    @pytest.mark.parametrize("method", ["NeedSecrets", "Connect", "NewSecrets"])
    def test_a_call_cancels_the_timer(self, service_module, monkeypatch, method):
        plugin = _plugin(service_module, monkeypatch)
        fired = []
        arguments = {
            "NeedSecrets": ({},),
            "Connect": (CONNECTION,),
            "NewSecrets": ({},),
        }[method]

        async def go():
            plugin.enable_idle_exit(lambda: fired.append(True))
            task = asyncio.create_task(getattr(plugin, method)(*arguments))
            await asyncio.sleep(0)  # the method runs up to its first await
            assert plugin._idle_handle is None
            await task
            await asyncio.sleep(0.15)
            await _finish(plugin)

        asyncio.run(go())

        assert fired == []

    def test_connect_interactive_cancels_the_timer(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            plugin.enable_idle_exit(lambda: None)
            task = asyncio.create_task(plugin.ConnectInteractive(CONNECTION, {}))
            await asyncio.sleep(0)
            assert plugin._idle_handle is None
            await task
            await _finish(plugin)

        asyncio.run(go())

    def test_restarts_after_stopped(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        fired = []

        async def go():
            plugin.enable_idle_exit(lambda: fired.append(True))
            await plugin.NeedSecrets({})  # cancels the timer
            await asyncio.sleep(0.1)
            assert fired == []
            await plugin.Disconnect()  # STOPPED starts it again
            await asyncio.sleep(0.15)

        asyncio.run(go())

        assert fired == [True]

    def test_failure_restarts_the_timer(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        fired = []

        async def go():
            plugin.enable_idle_exit(lambda: fired.append(True))
            plugin._cancel_idle_timer()
            plugin._emit_failure(service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED)
            await asyncio.sleep(0.15)

        asyncio.run(go())

        assert fired == [True]


# --- D-Bus error names -------------------------------------------------------


class TestErrorNames:
    @pytest.mark.parametrize(
        "connection",
        [
            {},
            {"vpn": {"data": {}}},
            {"vpn": {"data": {"gateway": "-oops"}}},
            {"vpn": {"data": {"gateway": "a b"}}},
            {"vpn": {"data": {"gateway": "g", "username": "-x"}}},
            {"vpn": {"data": {"gateway": "g", "hip-script": "relative/path"}}},
        ],
    )
    def test_invalid_settings_are_bad_arguments(
        self, service_module, monkeypatch, connection
    ):
        plugin = _plugin(service_module, monkeypatch)

        with pytest.raises(service_module.BadArgumentsError) as exc:
            asyncio.run(plugin._do_connect(connection, False))

        assert (
            exc.value.dbus_error_name
            == "org.freedesktop.NetworkManager.VPN.Error.BadArguments"
        )
        assert plugin.spawned == []

    def test_spawn_failure_is_launch_failed(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        async def start():
            return False

        monkeypatch.setattr(plugin, "_start_gpclient", start)

        with pytest.raises(service_module.LaunchFailedError) as exc:
            asyncio.run(plugin._do_connect(CONNECTION, False))

        assert (
            exc.value.dbus_error_name
            == "org.freedesktop.NetworkManager.VPN.Error.LaunchFailed"
        )

    def test_valid_settings_raise_nothing(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            await plugin._do_connect(CONNECTION, False)
            await _finish(plugin)

        asyncio.run(go())

    def test_the_two_names_differ(self, service_module):
        assert (
            service_module.BadArgumentsError.dbus_error_name
            != service_module.LaunchFailedError.dbus_error_name
        )

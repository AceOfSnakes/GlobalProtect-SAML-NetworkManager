"""
Tests for the tunnel notification (issue #53).

vpnc-script configures the tunnel; our vpnc hook runs `nm-gpclient-service
--vpnc-notify`, which calls Notify() of the service with the vpnc environment.
The service takes the call only with the token it gave gpclient, and reports
the tunnel to NetworkManager from that environment: no polling of interfaces.

No real D-Bus, gpclient or vpnc-script: the signals are recorded, the bus proxy
is a fake.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import ipaddress
import json
import os
import types

import pytest
from test_upstream_interfaces import start_env  # noqa: F401
from vpnc_notify_helpers import (
    TOKEN,
    active_plugin,
    ip4_config,
    notify,
    signals_named,
    vpnc_env,
)


def _names(signals):
    return [name for name, _ in signals]


def _rejected(service_module, plugin, **kwargs):
    with pytest.raises(service_module.NotifyRejectedError):
        notify(plugin, **kwargs)


# ---------------------------------------------------------------------------
# What a good notification does
# ---------------------------------------------------------------------------


class TestAccepted:
    def test_connect_reports_the_tunnel_in_order(self, service_module, dbus_signals):
        plugin = active_plugin(service_module)

        notify(plugin)

        assert _names(dbus_signals) == ["Config", "Ip4Config", "StateChanged"]
        assert dbus_signals[2][1] == service_module.NM_VPN_SERVICE_STATE_STARTED
        assert plugin._tunnel_up is True
        assert plugin._state == service_module.NM_VPN_SERVICE_STATE_STARTED
        # what the login taught us goes to the profile, after STARTED
        assert plugin.persisted == ["gateways", "openssl"]

    def test_connect_reports_address_gateway_and_prefix(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)
        nm = service_module.ipv4_to_nm_uint32

        notify(
            plugin,
            vpnc_env(
                INTERNAL_IP4_ADDRESS="10.8.0.5", INTERNAL_IP4_NETMASK="255.255.255.0"
            ),
        )

        config = ip4_config(dbus_signals)
        assert config == {
            "tundev": ("s", "tun0"),
            "address": ("u", nm("10.8.0.5")),
            "prefix": ("u", 32),
            "gateway": ("u", nm("203.0.113.9")),
        }
        assert signals_named(dbus_signals, "Config")[0] == {
            "tundev": ("s", "tun0"),
            "gateway": ("u", nm("203.0.113.9")),
            "has-ip4": ("b", True),
            "has-ip6": ("b", False),
        }

    def test_a_dual_stack_tunnel_also_reports_ip6config(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)

        notify(
            plugin,
            vpnc_env(
                INTERNAL_IP6_ADDRESS="fd00::5",
                INTERNAL_IP6_NETMASK="fd00::5/64",
                INTERNAL_IP6_DNS="fd00::53",
            ),
        )

        assert _names(dbus_signals) == [
            "Config",
            "Ip4Config",
            "Ip6Config",
            "StateChanged",
        ]
        assert signals_named(dbus_signals, "Config")[0]["has-ip6"] == ("b", True)
        config6 = signals_named(dbus_signals, "Ip6Config")[0]
        assert config6["address"] == ("ay", ipaddress.IPv6Address("fd00::5").packed)
        assert config6["prefix"] == ("u", 64)

    def test_dns_and_domains_come_from_the_environment(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module, dns_domains=["extra.example"])

        notify(
            plugin,
            vpnc_env(
                INTERNAL_IP4_DNS="10.1.1.1 10.1.1.2",
                CISCO_DEF_DOMAIN="corp.example",
                CISCO_SPLIT_DNS="lab.example",
            ),
        )

        nm = service_module.ipv4_to_nm_uint32
        config = ip4_config(dbus_signals)
        assert config["dns"] == ("au", [nm("10.1.1.1"), nm("10.1.1.2")])
        assert config["domains"] == (
            "as",
            ["corp.example", "~lab.example", "extra.example"],
        )

    def test_reconnect_reports_again_without_started_or_persisting(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)
        notify(plugin)
        dbus_signals.clear()
        plugin.persisted.clear()

        notify(
            plugin,
            vpnc_env(INTERNAL_IP4_ADDRESS="10.100.7.99", INTERNAL_IP4_NETMASKLEN="24"),
            reason="reconnect",
        )

        assert _names(dbus_signals) == ["Config", "Ip4Config"]
        assert ip4_config(dbus_signals)["address"] == (
            "u",
            service_module.ipv4_to_nm_uint32("10.100.7.99"),
        )
        assert plugin.persisted == []
        assert plugin.profile_task is not None  # the one of the first connect

    def test_reconnect_of_a_dual_stack_tunnel_reports_ip6config(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)
        notify(plugin)
        dbus_signals.clear()

        notify(
            plugin,
            vpnc_env(INTERNAL_IP6_ADDRESS="fd00::7"),
            reason="reconnect",
        )

        assert _names(dbus_signals) == ["Config", "Ip4Config", "Ip6Config"]

    def test_the_token_is_never_logged(self, service_module, dbus_signals, caplog):
        plugin = active_plugin(service_module)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            notify(plugin)
            with pytest.raises(service_module.NotifyRejectedError):
                notify(plugin, token=TOKEN + "x")

        assert TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# What it takes: every other call changes nothing
# ---------------------------------------------------------------------------


class TestRejected:
    def _assert_nothing_happened(self, service_module, plugin, dbus_signals, state):
        assert dbus_signals == []
        assert plugin._tunnel_up is False
        assert plugin._state == state
        assert plugin.profile_task is None
        assert plugin.persisted == []

    @pytest.mark.parametrize(
        "token", ["", "wrong", TOKEN[:-1], TOKEN + "0", TOKEN.upper(), "tökén"]
    )
    def test_a_wrong_or_empty_token(self, service_module, dbus_signals, token, caplog):
        plugin = active_plugin(service_module)

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _rejected(service_module, plugin, token=token)

        self._assert_nothing_happened(
            service_module, plugin, dbus_signals, plugin._state
        )
        assert "wrong token" in caplog.text

    def test_no_session_at_all(self, service_module, dbus_signals):
        plugin = service_module.GpclientVPNPlugin()
        plugin.persisted = []

        _rejected(service_module, plugin)

        assert dbus_signals == []

    @pytest.mark.parametrize(
        "attributes",
        [
            {"_notify_token": ""},  # no token was given out: no gpclient started
            {"gpclient_process": None},
            {"gpclient_process": types.SimpleNamespace(returncode=1)},  # exited
            {"gpclient_process": types.SimpleNamespace(returncode=0)},
        ],
    )
    def test_no_running_gpclient(self, service_module, dbus_signals, attributes):
        plugin = active_plugin(service_module, **attributes)

        _rejected(service_module, plugin, token=TOKEN if plugin._notify_token else "")

        self._assert_nothing_happened(
            service_module, plugin, dbus_signals, plugin._state
        )

    @pytest.mark.parametrize(
        "reason",
        ["", "disconnect", "pre-init", "post-disconnect", "Connect", "connect ", "x"],
    )
    def test_an_unknown_reason(self, service_module, dbus_signals, reason, caplog):
        plugin = active_plugin(service_module)

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _rejected(service_module, plugin, reason=reason)

        self._assert_nothing_happened(
            service_module, plugin, dbus_signals, plugin._state
        )
        assert "unknown reason" in caplog.text

    def test_a_reconnect_before_the_first_connect(self, service_module, dbus_signals):
        plugin = active_plugin(service_module)

        _rejected(service_module, plugin, reason="reconnect")

        self._assert_nothing_happened(
            service_module, plugin, dbus_signals, plugin._state
        )

    @pytest.mark.parametrize("state_name", ["STOPPED", "STOPPING", "STARTING"])
    def test_a_reconnect_after_a_failure(self, service_module, dbus_signals, state_name):
        # the tunnel was up, then the session failed: gpclient is not reaped yet
        state = getattr(service_module, f"NM_VPN_SERVICE_STATE_{state_name}")
        plugin = active_plugin(service_module, _state=state, _tunnel_up=True)

        _rejected(service_module, plugin, reason="reconnect")

        assert dbus_signals == []
        assert plugin._state == state
        assert plugin.profile_task is None

    def test_a_second_connect_sends_no_second_started(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)
        notify(plugin)
        before = list(dbus_signals)
        plugin.persisted.clear()

        _rejected(service_module, plugin)

        assert dbus_signals == before
        assert _names(dbus_signals).count("StateChanged") == 1
        assert plugin.persisted == []

    @pytest.mark.parametrize(
        "state_name", ["STOPPED", "INIT", "STOPPING", "UNKNOWN", "SHUTDOWN"]
    )
    def test_a_session_that_is_not_starting(
        self, service_module, dbus_signals, state_name
    ):
        # for example after the failure of the login was reported
        state = getattr(service_module, f"NM_VPN_SERVICE_STATE_{state_name}")
        plugin = active_plugin(service_module, _state=state)

        _rejected(service_module, plugin)

        self._assert_nothing_happened(service_module, plugin, dbus_signals, state)

    def test_the_error_has_its_own_d_bus_name(self, service_module):
        assert service_module.NotifyRejectedError.dbus_error_name == (
            "org.freedesktop.NetworkManager.gpclient.Tunnel.Error.Rejected"
        )


# ---------------------------------------------------------------------------
# The lifecycle: a Disconnect() or a new Connect() wins
# ---------------------------------------------------------------------------


def _quiet_teardown(monkeypatch, plugin, stop=None):
    async def noop():
        pass

    monkeypatch.setattr(plugin, "_stop_gpclient", stop or noop)
    monkeypatch.setattr(plugin, "_remember_gpauth", noop)
    monkeypatch.setattr(plugin, "_stop_remembered_gpauth", noop)
    monkeypatch.setattr(plugin, "_close_pty", lambda: None)


class TestLifecycle:
    def test_after_disconnect(self, service_module, monkeypatch, dbus_signals):
        plugin = active_plugin(service_module)
        _quiet_teardown(monkeypatch, plugin)

        asyncio.run(plugin.Disconnect())
        dbus_signals.clear()

        assert plugin._notify_token == ""
        _rejected(service_module, plugin)
        assert dbus_signals == []

    def test_while_disconnect_is_still_tearing_down(
        self, service_module, monkeypatch, dbus_signals
    ):
        # The token is still there, but the generation moved on: nothing may
        # be emitted for a session that is going away
        plugin = active_plugin(service_module)
        release = asyncio.Event()

        async def slow_stop():
            await release.wait()

        _quiet_teardown(monkeypatch, plugin, stop=slow_stop)

        async def scenario():
            disconnect = asyncio.create_task(plugin.Disconnect())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert plugin._notify_token == TOKEN
            with pytest.raises(service_module.NotifyRejectedError):
                await plugin.Notify("connect", TOKEN, vpnc_env())
            release.set()
            await disconnect

        asyncio.run(scenario())

        assert _names(dbus_signals) == ["StateChanged"]  # STOPPED, nothing else
        assert dbus_signals[0][1] == service_module.NM_VPN_SERVICE_STATE_STOPPED

    def test_during_a_new_connect(self, service_module, dbus_signals):
        # _do_connect() bumped the generation and tears the old session down
        plugin = active_plugin(service_module)
        plugin._generation += 1

        _rejected(service_module, plugin)

        assert dbus_signals == []

    def test_a_session_that_began_after_the_bump_is_accepted(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module)
        plugin._generation += 1
        plugin._session_gen = plugin._generation  # _setup_session() of the new one

        notify(plugin)

        assert "Ip4Config" in _names(dbus_signals)

    def test_disconnect_cancels_the_profile_update(
        self, service_module, monkeypatch, dbus_signals
    ):
        plugin = active_plugin(service_module)
        _quiet_teardown(monkeypatch, plugin)
        started = []

        async def hang():
            started.append(True)
            await asyncio.Event().wait()

        plugin._persist_gateway_list = hang

        async def scenario():
            await plugin.Notify("connect", TOKEN, vpnc_env())
            task = plugin.profile_task
            await asyncio.sleep(0)
            await plugin.Disconnect()
            return task, task.cancelled()  # before the loop ends: it cancels every task

        task, cancelled_by_disconnect = asyncio.run(scenario())

        assert started == [True]
        assert cancelled_by_disconnect
        assert plugin.profile_task is None
        assert plugin._notify_token == ""

    def test_a_crash_of_the_profile_update_fails_the_connection(
        self, service_module, dbus_signals, caplog
    ):
        plugin = active_plugin(service_module)

        async def boom():
            raise RuntimeError("boom")

        plugin._persist_gateway_list = boom

        with caplog.at_level("ERROR", logger=service_module.logger.name):
            with pytest.raises(RuntimeError, match="boom"):  # the helper awaits it
                notify(plugin)

        assert "internal error in profile update: boom" in caplog.text
        assert ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED) in (
            dbus_signals
        )


# ---------------------------------------------------------------------------
# The environment as the configuration
# ---------------------------------------------------------------------------


class TestIpv4FromEnv:
    ADDRESS = "10.8.0.5"

    @pytest.mark.parametrize(
        "extra",
        [
            {"INTERNAL_IP4_NETMASK": "255.255.255.0"},
            {"INTERNAL_IP4_NETMASK": "255.255.0.0"},
            {"INTERNAL_IP4_NETMASK": "0.0.0.0"},
            {"INTERNAL_IP4_NETMASKLEN": "24"},
            {"INTERNAL_IP4_NETMASKLEN": "0"},
            {"INTERNAL_IP4_NETMASKLEN": "20", "INTERNAL_IP4_NETMASK": "255.0.0.0"},
        ],
    )
    def test_a_netmask_that_is_not_32_still_gives_prefix_32(
        self, service_module, extra
    ):
        # vpnc-script adds "ADDRESS/32 peer ADDRESS": a wider prefix would make
        # NetworkManager add a second address and a prefix route
        env = {"INTERNAL_IP4_ADDRESS": self.ADDRESS, **extra}

        assert service_module.tunnel_ipv4_from_env(env) == (
            ipaddress.IPv4Address(self.ADDRESS),
            32,
        )

    def test_ip4config_has_prefix_32_for_a_wider_netmask(
        self, service_module, dbus_signals
    ):
        notify(
            active_plugin(service_module),
            vpnc_env(INTERNAL_IP4_NETMASK="255.255.255.0", INTERNAL_IP4_NETMASKLEN="24"),
        )

        assert ip4_config(dbus_signals)["prefix"] == ("u", 32)

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"INTERNAL_IP4_NETMASK": ""},
            {"INTERNAL_IP4_NETMASK": "garbage"},
            {"INTERNAL_IP4_NETMASK": "0.0.0.255"},  # a host mask, not a netmask
            {"INTERNAL_IP4_NETMASK": "255.0.255.0"},  # not contiguous
            {"INTERNAL_IP4_NETMASK": "24"},
            {"INTERNAL_IP4_NETMASK": "ffff:ffff::"},
            {"INTERNAL_IP4_NETMASKLEN": ""},
            {"INTERNAL_IP4_NETMASKLEN": "٢٤"},  # not ASCII digits
        ],
    )
    def test_a_missing_or_unusable_netmask_is_a_host_address(self, service_module, extra):
        env = {"INTERNAL_IP4_ADDRESS": self.ADDRESS, **extra}

        assert service_module.tunnel_ipv4_from_env(env) == (
            ipaddress.IPv4Address(self.ADDRESS),
            32,
        )

    @pytest.mark.parametrize(
        "address", [None, "", "garbage", "10.8.0", "1", "0x7f.0.0.1", "fd00::5"]
    )
    def test_no_address_without_an_ipv4_address(self, service_module, address):
        env = {"INTERNAL_IP4_NETMASKLEN": "24"}
        if address is not None:
            env["INTERNAL_IP4_ADDRESS"] = address

        assert service_module.tunnel_ipv4_from_env(env) is None

    @pytest.mark.parametrize("address", [None, "garbage", "fd00::5"])
    def test_an_ipv6_only_tunnel_has_no_ip4config(
        self, service_module, dbus_signals, caplog, address
    ):
        plugin = active_plugin(service_module)
        v6 = {"INTERNAL_IP6_ADDRESS": "fd00::5", "INTERNAL_IP6_NETMASK": "fd00::5/64"}

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=address, **v6))

        assert _names(dbus_signals) == ["Config", "Ip6Config", "StateChanged"]
        assert dbus_signals[2][1] == service_module.NM_VPN_SERVICE_STATE_STARTED
        config = signals_named(dbus_signals, "Config")[0]
        assert config["has-ip4"] == ("b", False)
        assert config["has-ip6"] == ("b", True)
        assert signals_named(dbus_signals, "Ip6Config")[0]["tundev"] == ("s", "tun0")
        assert plugin._tunnel_up is True
        if address:
            assert "Not adding address" in caplog.text

    def test_an_ipv6_only_tunnel_with_an_ipv4_server(self, service_module, dbus_signals):
        env = vpnc_env(
            INTERNAL_IP4_ADDRESS=None,
            VPNGATEWAY="198.51.100.7",
            INTERNAL_IP6_ADDRESS="fd00::5",
        )
        notify(active_plugin(service_module), env)

        config = signals_named(dbus_signals, "Config")[0]
        assert config["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("198.51.100.7"),
        )
        assert config["has-ip4"] == ("b", False)
        assert ip4_config(dbus_signals) is None

    @pytest.mark.parametrize(
        "method", ["manual", "link-local", "dhcp", "shared", "disabled", "ignore"]
    )
    def test_an_ipv6_only_tunnel_fails_when_the_profile_has_no_ipv6(
        self, service_module, dbus_signals, method
    ):
        plugin = active_plugin(service_module, ipv6_method=method)

        notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=None, INTERNAL_IP6_ADDRESS="fd00::5"))

        assert _names(dbus_signals) == ["Failure", "StateChanged"]
        assert dbus_signals[0][1] == service_module.NM_VPN_PLUGIN_FAILURE_BAD_IP_CONFIG

    def test_an_ipv6_only_tunnel_starts_with_ipv6_method_auto(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module, ipv6_method="auto")

        notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=None, INTERNAL_IP6_ADDRESS="fd00::5"))

        assert _names(dbus_signals) == ["Config", "Ip6Config", "StateChanged"]

    @pytest.mark.parametrize("method", ["manual", "disabled", "ignore"])
    def test_an_ipv4_tunnel_does_not_care_about_ipv6_method(
        self, service_module, dbus_signals, method
    ):
        notify(active_plugin(service_module, ipv6_method=method))

        assert _names(dbus_signals) == ["Config", "Ip4Config", "StateChanged"]

    def test_a_reconnect_without_any_address_fails(self, service_module, dbus_signals):
        plugin = active_plugin(service_module)
        notify(plugin)
        dbus_signals.clear()

        notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=None), reason="reconnect")

        assert _names(dbus_signals) == ["Failure", "StateChanged"]
        assert dbus_signals[0][1] == service_module.NM_VPN_PLUGIN_FAILURE_BAD_IP_CONFIG
        assert dbus_signals[1][1] == service_module.NM_VPN_SERVICE_STATE_STOPPED

    def test_an_ipv4_tunnel_has_ip4_in_config(self, service_module, dbus_signals):
        notify(active_plugin(service_module))

        assert signals_named(dbus_signals, "Config")[0]["has-ip4"] == ("b", True)
        assert ip4_config(dbus_signals) is not None

    @pytest.mark.parametrize("address", [None, "", "garbage", "fd00::5"])
    def test_a_tunnel_without_any_address_fails(
        self, service_module, dbus_signals, caplog, address
    ):
        plugin = active_plugin(service_module)

        with caplog.at_level("ERROR", logger=service_module.logger.name):
            notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=address, VPNGATEWAY=None))

        assert _names(dbus_signals) == ["Failure", "StateChanged"]
        assert dbus_signals[0][1] == service_module.NM_VPN_PLUGIN_FAILURE_BAD_IP_CONFIG
        assert dbus_signals[1][1] == service_module.NM_VPN_SERVICE_STATE_STOPPED
        assert plugin._tunnel_up is False
        assert "no usable tunnel address" in caplog.text


class TestGatewayFromEnv:
    def test_the_vpn_server_is_the_gateway(self, service_module, dbus_signals):
        notify(active_plugin(service_module), vpnc_env(VPNGATEWAY="198.51.100.7"))

        assert ip4_config(dbus_signals)["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("198.51.100.7"),
        )

    @pytest.mark.parametrize("gateway", [None, "", "gw.example.com", "198.51.100"])
    def test_without_an_ipv4_server_the_tunnel_address_is_used(
        self, service_module, dbus_signals, gateway
    ):
        notify(active_plugin(service_module), vpnc_env(VPNGATEWAY=gateway))

        assert ip4_config(dbus_signals)["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("10.100.7.42"),
        )

    def test_an_ipv6_server_is_an_ay_gateway_and_not_in_ip4config(
        self, service_module, dbus_signals
    ):
        notify(active_plugin(service_module), vpnc_env(VPNGATEWAY="2001:db8::1"))

        packed = ipaddress.IPv6Address("2001:db8::1").packed
        assert signals_named(dbus_signals, "Config")[0]["gateway"] == ("ay", packed)
        # Ip4Config keeps the point-to-point address: no IPv6 there
        assert ip4_config(dbus_signals)["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("10.100.7.42"),
        )

    def test_an_ipv4_server_is_a_u_gateway_in_config(
        self, service_module, dbus_signals
    ):
        notify(active_plugin(service_module), vpnc_env(VPNGATEWAY="198.51.100.7"))

        assert signals_named(dbus_signals, "Config")[0]["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("198.51.100.7"),
        )

    def test_an_ipv6_only_tunnel_without_a_server_uses_its_own_address(
        self, service_module, dbus_signals
    ):
        env = vpnc_env(
            INTERNAL_IP4_ADDRESS=None, VPNGATEWAY=None, INTERNAL_IP6_ADDRESS="fd00::5"
        )
        notify(active_plugin(service_module), env)

        packed = ipaddress.IPv6Address("fd00::5").packed
        assert signals_named(dbus_signals, "Config")[0]["gateway"] == ("ay", packed)

    @pytest.mark.parametrize("never_default", [True, False])
    def test_never_default_only_changes_the_log(
        self, service_module, dbus_signals, caplog, never_default
    ):
        plugin = active_plugin(service_module, never_default=never_default)

        with caplog.at_level("INFO", logger=service_module.logger.name):
            notify(plugin, vpnc_env(VPNGATEWAY="198.51.100.7"))

        assert "gateway" in ip4_config(dbus_signals)
        assert ("never-default is set" in caplog.text) is never_default
        assert ("Added gateway: 198.51.100.7" in caplog.text) is not never_default


class TestMtu:
    @pytest.mark.parametrize(
        "env, expected",
        [
            ({"INTERNAL_IP4_MTU": "1300"}, 1300),
            ({"INTERNAL_IP4_MTU": "68"}, 68),
            ({"INTERNAL_IP4_MTU": "65535"}, 65535),
        ],
    )
    def test_the_tunnel_mtu(self, service_module, env, expected):
        assert service_module.tunnel_mtu_from_env(env) == expected

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"INTERNAL_IP4_MTU": ""},
            {"MTU": "1400"},  # vpnc-script does not export it: not read
            {"INTERNAL_IP4_MTU": "67"},
            {"INTERNAL_IP4_MTU": "65536"},
            {"INTERNAL_IP4_MTU": "-1400"},
            {"INTERNAL_IP4_MTU": "14.0"},
            {"INTERNAL_IP4_MTU": "1400 "},
            {"INTERNAL_IP4_MTU": "１４００"},  # full-width digits
        ],
    )
    def test_no_mtu_when_there_is_none_or_it_is_invalid(self, service_module, env):
        assert service_module.tunnel_mtu_from_env(env) is None

    def test_config_has_the_tunnel_mtu_before_the_profile_one(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module, mtu=1300)

        notify(plugin, vpnc_env(INTERNAL_IP4_MTU="1400"))

        assert signals_named(dbus_signals, "Config")[0]["mtu"] == ("u", 1400)

    @pytest.mark.parametrize(
        "mtu, expected", [(1300, ("u", 1300)), (None, None)]
    )
    def test_config_falls_back_to_the_profile_mtu(
        self, service_module, dbus_signals, mtu, expected
    ):
        plugin = active_plugin(service_module, mtu=mtu)

        notify(plugin, vpnc_env(INTERNAL_IP4_MTU="bad"))

        assert signals_named(dbus_signals, "Config")[0].get("mtu") == expected


# ---------------------------------------------------------------------------
# What the service gives gpclient
# ---------------------------------------------------------------------------


class TestGpclientEnvironment:
    def _env(self, start_env):  # noqa: F811
        assert start_env.start() is True
        return json.loads(start_env.dump.read_text())["env"]

    def test_the_program_and_a_token_are_passed(self, start_env, service_module):  # noqa: F811
        env = self._env(start_env)

        assert env["GPCLIENT_NM_NOTIFY"] == service_module.SERVICE_PROGRAM
        assert env["GPCLIENT_NM_NOTIFY"].startswith("/")
        assert len(env["GPCLIENT_NM_TOKEN"]) == 32
        assert env["GPCLIENT_NM_TOKEN"] == start_env.plugin._notify_token

    def test_every_start_gets_a_new_token(self, start_env):  # noqa: F811
        first = self._env(start_env)["GPCLIENT_NM_TOKEN"]
        second = self._env(start_env)["GPCLIENT_NM_TOKEN"]

        assert first != second
        assert second == start_env.plugin._notify_token

    def test_the_old_state_file_is_not_passed_any_more(self, start_env):  # noqa: F811
        assert "GPCLIENT_NM_DNS_STATE" not in self._env(start_env)

    def test_a_token_without_a_gpclient_process_is_not_valid(
        self, service_module, dbus_signals
    ):
        # _notify_token is set before gpclient is spawned; until the process
        # exists nothing is accepted
        plugin = active_plugin(service_module, gpclient_process=None)

        _rejected(service_module, plugin)


# ---------------------------------------------------------------------------
# nm-gpclient-service --vpnc-notify
# ---------------------------------------------------------------------------


class FakeProxy:
    calls = []
    error = None

    @classmethod
    def new_proxy(cls, service, path):
        cls.calls.append(("new_proxy", service, path))
        return cls()

    async def Notify(self, reason, token, env):  # noqa: N802  # the D-Bus name
        FakeProxy.calls.append(("Notify", reason, token, env))
        if FakeProxy.error is not None:
            raise FakeProxy.error


@pytest.fixture
def proxy(service_module, monkeypatch):
    FakeProxy.calls = []
    FakeProxy.error = None
    monkeypatch.setattr(service_module, "GpclientTunnelInterface", FakeProxy)
    return FakeProxy


class TestVpncNotifyMode:
    ENVIRON = {
        "reason": "connect",
        "GPCLIENT_NM_TOKEN": TOKEN,
        "TUNDEV": "tun0",
        "INTERNAL_IP4_ADDRESS": "10.8.0.5",
        "VPNGATEWAY": "203.0.113.9",
        "CISCO_SPLIT_DNS": "a.example",
        "PATH": "/usr/bin",
        "HOME": "/root",
        "GPCLIENT_NM_NOTIFY": "/usr/lib/NetworkManager/nm-gpclient-service",
        "INTERNAL_IP4_NETMASKLEN": "24",
        "CISCO_SPLIT_INC_0_ADDR": "10.0.0.0",
        "MTU": "1400",
        "CISCO_BANNER": "hello",
        "SUDO_USER": "alice",
        "reasoning": "x",
    }

    def test_the_call_has_the_reason_the_token_and_the_listed_variables_only(
        self, service_module, proxy
    ):
        assert asyncio.run(service_module.vpnc_notify(dict(self.ENVIRON))) == 0

        assert proxy.calls == [
            (
                "new_proxy",
                "org.freedesktop.NetworkManager.gpclient",
                "/org/freedesktop/NetworkManager/VPN/Plugin",
            ),
            (
                "Notify",
                "connect",
                TOKEN,
                {
                    "TUNDEV": "tun0",
                    "INTERNAL_IP4_ADDRESS": "10.8.0.5",
                    "VPNGATEWAY": "203.0.113.9",
                    "CISCO_SPLIT_DNS": "a.example",
                    "INTERNAL_IP4_NETMASKLEN": "24",
                },
            ),
        ]

    @pytest.mark.parametrize(
        "name",
        ["PATH", "HOME", "SUDO_USER", "GPCLIENT_NM_NOTIFY", "GPCLIENT_NM_TOKEN",
         "CISCO_SPLIT_INC_0_ADDR", "reason", "reasoning", "MTU", "CISCO_BANNER"],
    )
    def test_a_variable_that_is_not_listed_is_not_sent(
        self, service_module, proxy, name
    ):
        asyncio.run(service_module.vpnc_notify(dict(self.ENVIRON)))

        sent = proxy.calls[1][3]
        assert name not in sent
        assert name not in service_module.VPNC_NOTIFY_ENV

    def test_the_listed_variables_are_what_the_service_reads(self, service_module):
        # The allow-list holds nothing the service never looks at, and the
        # indexed split routes (it does not use them) are not in it
        assert "CISCO_SPLIT_INC" not in service_module.VPNC_NOTIFY_ENV
        assert len(set(service_module.VPNC_NOTIFY_ENV)) == len(
            service_module.VPNC_NOTIFY_ENV
        )

    @pytest.mark.parametrize(
        "environ, reason, token",
        [({}, "", ""), ({"reason": "reconnect"}, "reconnect", "")],
    )
    def test_missing_values_are_sent_empty_and_the_service_decides(
        self, service_module, proxy, environ, reason, token
    ):
        asyncio.run(service_module.vpnc_notify(environ))

        assert proxy.calls[1][:3] == ("Notify", reason, token)

    def test_a_rejected_call_exits_non_zero_with_a_message(
        self, service_module, proxy, capsys
    ):
        proxy.error = service_module.NotifyRejectedError("wrong token")

        assert asyncio.run(service_module.vpnc_notify(dict(self.ENVIRON))) == 1

        err = capsys.readouterr().err
        assert "--vpnc-notify" in err and "wrong token" in err
        assert TOKEN not in err

    @pytest.mark.parametrize(
        "error", [OSError("no bus"), RuntimeError("service not running")]
    )
    def test_no_bus_or_no_service_exits_non_zero_with_a_message(
        self, service_module, proxy, monkeypatch, capsys, error
    ):
        def no_bus():
            raise error

        monkeypatch.setattr(service_module, "sd_bus_open_system", no_bus)

        assert asyncio.run(service_module.vpnc_notify(dict(self.ENVIRON))) == 1

        assert str(error) in capsys.readouterr().err
        assert proxy.calls == []

    def test_main_runs_the_mode_without_the_bus_name_or_the_plugin(
        self, service_module, proxy, monkeypatch
    ):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--vpnc-notify"])
        monkeypatch.setattr(service_module.os, "environ", dict(self.ENVIRON))
        names = []

        async def request_name(name):
            names.append(name)

        monkeypatch.setattr(service_module, "request_default_bus_name_async", request_name)
        monkeypatch.setattr(
            service_module,
            "GpclientVPNPlugin",
            lambda: pytest.fail("the service object is not created"),
        )

        assert service_module.main() == 0

        assert names == []
        assert proxy.calls[1][0] == "Notify"

    def test_main_returns_the_failure_of_the_mode(
        self, service_module, proxy, monkeypatch
    ):
        monkeypatch.setattr(service_module.sys, "argv", ["svc", "--vpnc-notify"])
        monkeypatch.setattr(service_module.os, "environ", dict(self.ENVIRON))
        proxy.error = RuntimeError("no service")

        assert service_module.main() == 1


class TestRoutingHook:
    HOOK = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "90-gpclient-routing"
    )

    def test_it_no_longer_writes_a_state_file(self):
        with open(self.HOOK, encoding="utf-8") as handle:
            text = handle.read()

        assert "GPCLIENT_NM_DNS_STATE" not in text
        assert "dns-state" not in text
        # the route policy stays
        for name in (
            "GPCLIENT_NM_IGNORE_AUTO_ROUTES",
            "GPCLIENT_NM_NEVER_DEFAULT",
            "GPCLIENT_CUSTOM_DNS_DOMAINS",
        ):
            assert name in text


class TestInterfaces:
    def test_the_notification_has_its_own_interface(self, service_module):
        assert service_module.NM_DBUS_INTERFACE_TUNNEL == (
            "org.freedesktop.NetworkManager.gpclient.Tunnel"
        )
        assert issubclass(
            service_module.GpclientVPNPlugin, service_module.GpclientTunnelInterface
        )
        assert service_module.NM_DBUS_INTERFACE_VPN == (
            "org.freedesktop.NetworkManager.VPN.Plugin"
        )

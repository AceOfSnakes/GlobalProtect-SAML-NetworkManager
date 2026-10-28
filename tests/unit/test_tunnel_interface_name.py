"""
Tests for naming the tunnel (issue #53, item 1.1).

The service asks gpclient for `--interface=gp-<first 8 hex digits of the
connection UUID>`, so it knows the tunnel by name and takes a notification of
the vpnc hook for no other interface. Without a usable UUID it takes any
interface name the hook reports.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json

import pytest
from test_upstream_interfaces import fake_exec, net_sysfs, start_env  # noqa: F401
from vpnc_notify_helpers import (
    NAME,
    UUID,
    active_plugin,
    ip4_config,
    notify,
    vpnc_env,
)


class TestName:
    @pytest.mark.parametrize(
        "uuid",
        [UUID, UUID.upper(), UUID.replace("-", "")],
    )
    def test_is_gp_and_the_first_eight_hex_digits(self, service_module, uuid):
        assert service_module.tunnel_interface_name(uuid) == NAME
        assert len(NAME) <= 15  # the kernel limit

    @pytest.mark.parametrize(
        "uuid",
        [None, "", "1a2b3c4", "1a2b-3c4", "zzzzzzzz-0000", "1a2b3c4g-5e6f"],
    )
    def test_is_none_without_eight_hex_digits(self, service_module, uuid):
        assert service_module.tunnel_interface_name(uuid) is None


class TestCommandLine:
    def test_interface_follows_connect_and_comes_before_the_gateway(
        self, start_env  # noqa: F811
    ):
        start_env.plugin._connection_uuid = UUID

        assert start_env.start() is True

        argv = json.loads(start_env.dump.read_text())["argv"]
        assert f"--interface={NAME}" in argv  # one argument
        assert argv.index("connect") < argv.index(f"--interface={NAME}")
        assert argv.index(f"--interface={NAME}") < argv.index("--")
        assert argv[argv.index("--") + 1] == "portal.example.com"

    @pytest.mark.parametrize("uuid", ["", "1a2b3c4", "not-a-uuid"])
    def test_no_interface_without_a_usable_uuid(self, start_env, uuid):  # noqa: F811
        start_env.plugin._connection_uuid = uuid

        assert start_env.start() is True

        argv = json.loads(start_env.dump.read_text())["argv"]
        assert not [a for a in argv if a.startswith("--interface")]


class TestNotifiedInterface:
    def test_takes_the_named_interface(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, uuid=UUID)

        notify(plugin, vpnc_env(TUNDEV=NAME))

        assert ip4_config(dbus_signals)["tundev"] == ("s", NAME)

    @pytest.mark.parametrize("tundev", ["gpd0", "tun0", "gp-ffffffff", "", None])
    def test_rejects_any_other_interface_when_the_tunnel_has_a_name(
        self, service_module, dbus_signals, tundev
    ):
        plugin = active_plugin(service_module, uuid=UUID)

        with pytest.raises(service_module.NotifyRejectedError):
            notify(plugin, vpnc_env(TUNDEV=tundev))

        assert dbus_signals == []
        assert plugin._tunnel_up is False

    @pytest.mark.parametrize("tundev", ["gpd0", "tun0", "tun12", "vpn.test", NAME])
    def test_takes_the_reported_interface_without_a_name(
        self, service_module, dbus_signals, tundev
    ):
        plugin = active_plugin(service_module, uuid="")

        notify(plugin, vpnc_env(TUNDEV=tundev))

        assert ip4_config(dbus_signals)["tundev"] == ("s", tundev)

    @pytest.mark.parametrize(
        "tundev", ["", None, "a" * 16, "tun 0", "tun0\n", "../tun0", "tun0;reboot"]
    )
    def test_rejects_an_interface_that_is_no_name_without_a_name(
        self, service_module, dbus_signals, tundev
    ):
        plugin = active_plugin(service_module, uuid="")

        with pytest.raises(service_module.NotifyRejectedError):
            notify(plugin, vpnc_env(TUNDEV=tundev))

        assert dbus_signals == []


class TestStaleInterface:
    def test_stale_named_interface_is_removed_and_gpd0_kept(
        self, service_module, fake_exec, net_sysfs  # noqa: F811
    ):
        (net_sysfs / NAME).mkdir()
        (net_sysfs / "gpd0").mkdir()
        fake_exec.codes["-x"] = 1  # pgrep: no process

        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = UUID
        asyncio.run(plugin._cleanup_stale_gpd0())

        assert fake_exec.commands == [
            ["pgrep", "-x", "gpclient"],
            ["ip", "link", "del", NAME],
        ]

    def test_named_interface_is_kept_while_gpclient_runs(
        self, service_module, fake_exec, net_sysfs  # noqa: F811
    ):
        (net_sysfs / NAME).mkdir()
        fake_exec.codes["-x"] = 0  # pgrep: a process runs

        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = UUID
        asyncio.run(plugin._cleanup_stale_gpd0())

        assert fake_exec.commands == [["pgrep", "-x", "gpclient"]]

    @pytest.mark.parametrize("other", ["tun0", "gpd0", "gp-ffffffff"])
    def test_an_interface_of_another_name_is_left_alone(
        self, service_module, fake_exec, net_sysfs, other  # noqa: F811
    ):
        (net_sysfs / other).mkdir()
        fake_exec.codes["-x"] = 1

        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = UUID
        asyncio.run(plugin._cleanup_stale_gpd0())

        assert fake_exec.commands == []

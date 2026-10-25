"""
Tests for naming the tunnel (issue #53, item 1.1).

The service asks gpclient for `--interface=gp-<first 8 hex digits of the
connection UUID>`, so it knows the tunnel by name instead of guessing it from
gpd0/tunN, a snapshot and the file descriptors of gpclient. Without a usable
UUID the old heuristics stay (their tests are in test_tunnel_detection.py).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json

import pytest
from test_tunnel_detection import (
    _FakeProcess,
    _fake_proc,
    _ip4_config,
    _plugin_with_tunnels,
    _run_loop,
)
from test_upstream_interfaces import fake_exec, net_sysfs, start_env  # noqa: F401

UUID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
NAME = "gp-1a2b3c4d"


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


class TestDetection:
    def test_picks_the_named_interface_among_other_tunnels(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"gpd0": "10.9.9.9", "tun0": "10.8.0.3", NAME: "10.100.7.42"},
            preexisting={},
        )
        plugin._connection_uuid = UUID

        assert _run_loop(plugin) is True

        assert _ip4_config(dbus_signals)["tundev"] == ("s", NAME)

    def test_ignores_gpd0_and_tun0_when_the_named_one_is_missing(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"gpd0": "10.9.9.9", "tun0": "10.8.0.3"},
            preexisting={},
        )
        plugin._connection_uuid = UUID

        assert _run_loop(plugin) is False
        assert dbus_signals == []

    def test_waits_while_the_named_interface_has_no_address(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={NAME: None, "tun0": "10.8.0.3"},
            preexisting={},
        )
        plugin._connection_uuid = UUID

        assert _run_loop(plugin) is False
        assert dbus_signals == []

    def test_ignores_the_snapshot_and_the_fd_ownership(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={NAME: "10.100.7.42", "tun0": "10.8.0.3"},
            # Heuristics would reject NAME (same IP before the start), and
            # gpclient holds tun0 only
            preexisting={NAME: "10.100.7.42"},
        )
        monkeypatch.setattr(
            service_module,
            "PROC_PATH",
            _fake_proc(tmp_path, {4242: ("gpclient", 1, ["tun0"])}),
        )
        plugin.gpclient_process = _FakeProcess(4242)
        plugin._connection_uuid = UUID

        assert _run_loop(plugin) is True
        assert _ip4_config(dbus_signals)["tundev"] == ("s", NAME)

    def test_without_a_uuid_the_old_candidate_list_applies(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={NAME: "10.100.7.42", "tun0": "10.8.0.3"},
            preexisting={"tun0": "10.8.0.3"},
        )
        plugin._connection_uuid = ""

        # NAME is no candidate; tun0 pre-existed unchanged
        assert _run_loop(plugin) is False
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

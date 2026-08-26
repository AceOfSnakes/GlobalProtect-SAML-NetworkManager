"""
Tests for tunnel interface detection (issue #13, building on issue #7).

Detection used to poll a fixed ["gpd0", "tun0", "tun1"] list. With two other
tun-based VPNs already up, our own tunnel lands on tun2 - a name that list
never contained - so the connection stayed in "activating" until
NetworkManager's vpn.timeout killed it. The candidates are now discovered from
/sys/class/net, and the tunnel is attributed to gpclient by the file
descriptors it holds.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import os
import socket
import struct


def _fake_net(tmp_path, names):
    """A stand-in for /sys/class/net containing `names`."""
    net = tmp_path / "sys-class-net"
    net.mkdir(exist_ok=True)
    for name in names:
        (net / name).mkdir(exist_ok=True)
    return str(net)


def _fake_proc(tmp_path, processes):
    """A stand-in for /proc.

    `processes` maps pid -> (comm, ppid, [interface names held via a tun fd]).
    """
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    for pid, (comm, ppid, ifaces) in processes.items():
        entry = proc / str(pid)
        entry.mkdir(exist_ok=True)
        (entry / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0 -1 0\n")
        fdinfo = entry / "fdinfo"
        fdinfo.mkdir(exist_ok=True)
        # fd 0 is always something that is not a tun device
        (fdinfo / "0").write_text("pos:\t0\nflags:\t02\nmnt_id:\t24\n")
        for number, iface in enumerate(ifaces, start=1):
            (fdinfo / str(number)).write_text(
                f"pos:\t0\nflags:\t0104002\nmnt_id:\t16\niff:\t{iface}\n"
            )
    return str(proc)


class _FakeProcess:
    """Just enough of asyncio.subprocess.Process for the ownership lookup."""

    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode


def _plugin_with_tunnels(service_module, monkeypatch, tmp_path, ips, preexisting):
    """A plugin whose interface list and IPs come from `ips` (iface -> IP)."""
    monkeypatch.setattr(
        service_module, "NET_SYSFS_PATH", _fake_net(tmp_path, ips.keys())
    )

    async def fake_ipv4(_self, iface):
        return ips.get(iface), 24

    monkeypatch.setattr(
        service_module.GpclientVPNPlugin, "_get_iface_ipv4", fake_ipv4
    )
    plugin = service_module.GpclientVPNPlugin()
    plugin._preexisting_ifaces = dict(preexisting)
    return plugin


def _run_loop(plugin, timeout=1.5):
    """Run the detection loop; True when it accepted a tunnel and returned."""

    async def scenario():
        task = asyncio.create_task(plugin._check_tunnel_loop())
        try:
            await asyncio.wait_for(task, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return False

    return asyncio.run(scenario())


def _ip4_config(signals):
    for name, payload in signals:
        if name == "Ip4Config":
            return payload
    return None


class TestCandidateDiscovery:
    def test_finds_every_tunnel_not_just_the_first_two(
        self, service_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            service_module,
            "NET_SYSFS_PATH",
            _fake_net(tmp_path, ["lo", "eth0", "tun0", "tun1", "tun2", "gpd0"]),
        )

        assert service_module.list_tunnel_candidates() == [
            "gpd0",
            "tun0",
            "tun1",
            "tun2",
        ]

    def test_orders_tunnels_numerically(
        self, service_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            service_module,
            "NET_SYSFS_PATH",
            _fake_net(tmp_path, ["tun10", "tun2", "tun1"]),
        )

        # Lexical sorting would put tun10 before tun2
        assert service_module.list_tunnel_candidates() == ["tun1", "tun2", "tun10"]

    def test_ignores_look_alike_devices(
        self, service_module, monkeypatch, tmp_path
    ):
        # tunl0 is the always-present IPIP tunnel device, not a VPN tunnel
        monkeypatch.setattr(
            service_module,
            "NET_SYSFS_PATH",
            _fake_net(tmp_path, ["tunl0", "tunnelmon", "gpdX", "tun", "tun3"]),
        )

        assert service_module.list_tunnel_candidates() == ["tun3"]

    def test_survives_an_unreadable_sysfs(self, service_module, monkeypatch):
        monkeypatch.setattr(service_module, "NET_SYSFS_PATH", "/no/such/path")

        assert service_module.list_tunnel_candidates() == []


class TestSnapshot:
    def test_records_all_foreign_tunnels(
        self, service_module, monkeypatch, tmp_path
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun0": "192.168.1.5", "tun1": "10.8.0.3", "tun2": "10.9.0.3"},
            preexisting={},
        )

        snapshot = asyncio.run(plugin._snapshot_tunnel_interfaces())

        # The old 3-name list could only ever record tun0 and tun1
        assert snapshot == {
            "tun0": "192.168.1.5",
            "tun1": "10.8.0.3",
            "tun2": "10.9.0.3",
        }


class TestDetectionLoop:
    def test_detects_a_tunnel_beyond_tun1(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        """The issue #13 scenario: two foreign VPNs hold tun0 and tun1."""
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={
                "tun0": "192.168.1.5",
                "tun1": "10.8.0.3",
                "tun2": "10.100.7.42",  # ours, brought up by gpclient
            },
            preexisting={"tun0": "192.168.1.5", "tun1": "10.8.0.3"},
        )

        assert _run_loop(plugin) is True

        config = _ip4_config(dbus_signals)
        assert config is not None
        assert config["tundev"] == ("s", "tun2")
        assert config["address"] == (
            "u",
            struct.unpack("!I", socket.inet_aton("10.100.7.42"))[0],
        )
        assert config["prefix"] == ("u", 24)
        assert (
            "StateChanged",
            service_module.NM_VPN_SERVICE_STATE_STARTED,
        ) in dbus_signals

    def test_still_ignores_pre_existing_tunnels(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        """Issue #7 must not regress: a foreign tunnel is never adopted."""
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun0": "192.168.1.5", "tun1": "10.8.0.3"},
            preexisting={"tun0": "192.168.1.5", "tun1": "10.8.0.3"},
        )

        assert _run_loop(plugin) is False
        assert dbus_signals == []

    def test_accepts_a_pre_existing_interface_that_changed_ip(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"gpd0": "10.100.7.42"},
            preexisting={"gpd0": "10.0.0.9"},
        )

        assert _run_loop(plugin) is True
        assert _ip4_config(dbus_signals)["tundev"] == ("s", "gpd0")

    def test_waits_for_an_interface_without_an_ip(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun2": None},
            preexisting={},
        )

        assert _run_loop(plugin) is False
        assert dbus_signals == []

    def test_prefers_the_tunnel_gpclient_holds(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        """Another VPN coming up mid-connect must not be adopted."""
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun2": "10.8.9.1", "tun3": "10.100.7.42"},
            preexisting={},
        )
        monkeypatch.setattr(
            service_module,
            "PROC_PATH",
            _fake_proc(
                tmp_path,
                {
                    4242: ("gpclient", 1, ["tun3"]),
                    4243: ("openvpn", 1, ["tun2"]),
                },
            ),
        )
        plugin.gpclient_process = _FakeProcess(4242)

        assert _run_loop(plugin) is True
        # tun2 sorts first and is equally new, but it is not ours
        assert _ip4_config(dbus_signals)["tundev"] == ("s", "tun3")

    def test_falls_back_when_ownership_is_unknown(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        """No fd information (gpclient already gone) - the snapshot decides."""
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun2": "10.100.7.42"},
            preexisting={},
        )
        monkeypatch.setattr(service_module, "PROC_PATH", _fake_proc(tmp_path, {}))
        plugin.gpclient_process = _FakeProcess(4242)

        assert _run_loop(plugin) is True
        assert _ip4_config(dbus_signals)["tundev"] == ("s", "tun2")


class TestOwnershipLookup:
    def test_reads_the_interface_out_of_fdinfo(
        self, service_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            service_module,
            "PROC_PATH",
            _fake_proc(tmp_path, {77: ("gpclient", 1, ["tun7"])}),
        )

        assert service_module.tunnel_ifaces_held_by([77]) == {"tun7"}

    def test_returns_nothing_for_an_unknown_pid(
        self, service_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(service_module, "PROC_PATH", _fake_proc(tmp_path, {}))

        assert service_module.tunnel_ifaces_held_by([77]) == set()

    def test_includes_descendants(self, service_module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            service_module,
            "PROC_PATH",
            _fake_proc(
                tmp_path,
                {
                    100: ("gpclient", 1, []),
                    101: ("openconnect", 100, ["tun4"]),
                    102: ("unrelated", 1, ["tun5"]),
                },
            ),
        )

        tree = service_module.process_tree(100)

        assert sorted(tree) == [100, 101]
        assert service_module.tunnel_ifaces_held_by(tree) == {"tun4"}

    def test_handles_a_command_name_with_spaces_and_brackets(
        self, service_module, monkeypatch, tmp_path
    ):
        proc = _fake_proc(tmp_path, {200: ("x", 1, []), 201: ("y", 200, ["tun8"])})
        # A comm the naive "split on whitespace" parse would trip over
        (tmp_path / "proc" / "201" / "stat").write_text(
            "201 (weird ) name) S 200 0 0 0 -1 0\n"
        )
        monkeypatch.setattr(service_module, "PROC_PATH", proc)

        assert sorted(service_module.process_tree(200)) == [200, 201]

    def test_reports_nothing_once_gpclient_exited(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin.gpclient_process = _FakeProcess(os.getpid(), returncode=0)

        assert plugin._tunnel_ifaces_owned_by_gpclient() == set()

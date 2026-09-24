"""
Tests for reporting the gateway's DNS to NetworkManager (issue #15).

vpnc-script applies the DNS servers and search domains the gateway pushes
straight to systemd-resolved. NetworkManager never learned about them, so on
its next DNS recalculation (an IPv6 router advertisement on the Wi-Fi
interface was enough) it overwrote tun0's resolver state with its own, empty
one - VPN DNS gone, tunnel still up. The vpnc hook now records what the
gateway pushed and the service reports it in Ip4Config, which makes
NetworkManager own (and reapply) the VPN resolver configuration.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import socket
import struct
import subprocess

import pytest

from test_tunnel_detection import _ip4_config, _plugin_with_tunnels, _run_loop

HOOK = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "90-gpclient-routing"
)


def _nm_u32(address):
    return struct.unpack("=I", socket.inet_aton(address))[0]


def _write_state(path, **fields):
    path.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))


def _detect(service_module, monkeypatch, tmp_path, dbus_signals, **plugin_attrs):
    plugin = _plugin_with_tunnels(
        service_module,
        monkeypatch,
        tmp_path,
        ips={"tun0": "10.100.7.42"},
        preexisting={},
    )
    for name, value in plugin_attrs.items():
        setattr(plugin, name, value)
    assert _run_loop(plugin) is True
    return _ip4_config(dbus_signals)


class TestStateParsing:
    def test_parses_key_value_lines_and_ignores_noise(self, service_module):
        state = service_module.parse_dns_state(
            "TUNDEV=tun0\n"
            "# a comment\n"
            "\n"
            "INTERNAL_IP4_DNS=10.0.0.1 10.0.0.2\n"
            "CISCO_DEF_DOMAIN=corp.example\n"
            "garbage line without equals\n"
            "not a var=1\n"
        )

        assert state == {
            "TUNDEV": "tun0",
            "INTERNAL_IP4_DNS": "10.0.0.1 10.0.0.2",
            "CISCO_DEF_DOMAIN": "corp.example",
        }

    def test_learned_dns_splits_servers_and_domains(self, service_module):
        learned = service_module.learned_dns_from_state(
            {
                "TUNDEV": "tun0",
                "INTERNAL_IP4_DNS": "10.0.0.1 10.0.0.2",
                "INTERNAL_IP6_DNS": "fd00::53",
                "CISCO_DEF_DOMAIN": "corp.example extra.example",
                # openconnect builds this comma-separated
                "CISCO_SPLIT_DNS": "corp.example,lab.example",
            },
            "tun0",
        )

        assert learned == (
            ["10.0.0.1", "10.0.0.2"],
            ["corp.example", "extra.example", "lab.example"],
            ["fd00::53"],
        )

    def test_state_for_another_tunnel_is_rejected(self, service_module):
        state = {"TUNDEV": "tun1", "INTERNAL_IP4_DNS": "10.0.0.1"}

        assert service_module.learned_dns_from_state(state, "tun0") is None

    def test_state_without_tundev_is_accepted(self, service_module):
        learned = service_module.learned_dns_from_state(
            {"INTERNAL_IP4_DNS": "10.0.0.1"}, "tun0"
        )

        assert learned == (["10.0.0.1"], [], [])

    def test_nm_uint32_is_the_raw_in_addr(self, service_module):
        # NetworkManager reads the value as in_addr_t: inet_aton() bytes in
        # native order, not a big-endian integer
        assert service_module.ipv4_to_nm_uint32("10.0.0.1") == struct.unpack(
            "=I", bytes([10, 0, 0, 1])
        )[0]


class TestDetectionReportsDns:
    def test_gateway_dns_lands_in_ip4config(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        """The issue #15 scenario: no `dns` in the profile, gateway pushes two
        servers and a domain - NetworkManager must hear about them."""
        _write_state(
            dns_state_sandbox,
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.0.0.1 10.0.0.2",
            INTERNAL_IP6_DNS="",
            CISCO_DEF_DOMAIN="corp.example",
            CISCO_SPLIT_DNS="",
        )

        config = _detect(service_module, monkeypatch, tmp_path, dbus_signals)

        assert config["tundev"] == ("s", "tun0")
        assert config["dns"] == ("au", [_nm_u32("10.0.0.1"), _nm_u32("10.0.0.2")])
        assert config["domains"] == ("as", ["corp.example"])

    def test_profile_dns_overrides_gateway_servers_but_keeps_domains(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        _write_state(
            dns_state_sandbox,
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.0.0.1",
            CISCO_DEF_DOMAIN="corp.example",
        )

        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=["192.168.1.53"],
            dns_domains=["extra.example", "corp.example"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])
        assert config["domains"] == ("as", ["corp.example", "extra.example"])

    def test_without_state_file_the_tunnel_is_still_reported(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        # No hook installed: behave as before (no DNS in Ip4Config)
        config = _detect(service_module, monkeypatch, tmp_path, dbus_signals)

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config
        assert "domains" not in config

    def test_profile_dns_alone_still_works_without_state_file(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=["192.168.1.53"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])

    def test_state_for_a_foreign_tunnel_is_ignored(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun7", INTERNAL_IP4_DNS="10.9.9.9")

        config = _detect(service_module, monkeypatch, tmp_path, dbus_signals)

        assert "dns" not in config

    def test_detection_waits_for_a_late_state_file(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        """The interface has its IP a round before the hook's file shows up."""
        monkeypatch.setattr(service_module, "DNS_STATE_WAIT_ROUNDS", 2)
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun0": "10.100.7.42"},
            preexisting={},
        )
        real_read = plugin._read_learned_dns
        calls = []

        def late_read(iface):
            calls.append(iface)
            if len(calls) == 2:
                _write_state(
                    dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.1"
                )
            return real_read(iface)

        monkeypatch.setattr(plugin, "_read_learned_dns", late_read)

        assert _run_loop(plugin) is True
        assert len(calls) == 2
        assert _ip4_config(dbus_signals)["dns"] == ("au", [_nm_u32("10.0.0.1")])

    def test_gives_up_waiting_and_reports_without_dns(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        monkeypatch.setattr(service_module, "DNS_STATE_WAIT_ROUNDS", 1)

        config = _detect(service_module, monkeypatch, tmp_path, dbus_signals)

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config


class TestConnectionLifecycle:
    def test_gpclient_gets_the_state_path_and_stale_file_is_removed(
        self, service_module, dns_state_sandbox
    ):
        assert service_module.DNS_STATE_ENV == "GPCLIENT_NM_DNS_STATE"
        dns_state_sandbox.write_text("TUNDEV=tun0\nINTERNAL_IP4_DNS=10.0.0.1\n")
        plugin = service_module.GpclientVPNPlugin()

        plugin._clear_dns_state()

        assert not dns_state_sandbox.exists()
        # Idempotent: a missing file is not an error
        plugin._clear_dns_state()


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs /bin/sh")
class TestVpncHook:
    """The hook is sourced by vpnc-script (POSIX sh) with openconnect's
    environment; it must leave the gateway's DNS for the service to read."""

    def _source_hook(self, tmp_path, env):
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        full_env.update(env)
        # `. hook; env` - the hook is sourced, exactly like vpnc-script does
        return subprocess.run(
            ["/bin/sh", "-c", f". {HOOK}"],
            env=full_env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_writes_dns_state_when_started_by_the_service(self, tmp_path):
        state = tmp_path / "run" / "dns-state"

        result = self._source_hook(
            tmp_path,
            {
                "GPCLIENT_NM_DNS_STATE": str(state),
                "GPCLIENT_CUSTOM_DNS_DOMAINS": "extra.example",
                "TUNDEV": "tun0",
                "INTERNAL_IP4_DNS": "10.0.0.1 10.0.0.2",
                "CISCO_DEF_DOMAIN": "corp.example",
                "CISCO_SPLIT_DNS": "corp.example,lab.example",
            },
        )

        assert state.exists(), result.stderr
        lines = state.read_text().splitlines()
        assert "TUNDEV=tun0" in lines
        assert "INTERNAL_IP4_DNS=10.0.0.1 10.0.0.2" in lines
        assert "INTERNAL_IP6_DNS=" in lines
        # The profile's dns-domains are merged in before the file is written
        assert "CISCO_DEF_DOMAIN=corp.example extra.example" in lines
        assert "CISCO_SPLIT_DNS=corp.example,lab.example" in lines
        assert not list((tmp_path / "run").glob("*.tmp.*"))

    def test_leaves_nothing_behind_for_a_manual_gpclient(self, tmp_path):
        self._source_hook(
            tmp_path,
            {"TUNDEV": "tun0", "INTERNAL_IP4_DNS": "10.0.0.1"},
        )

        assert list(tmp_path.iterdir()) == []

    def test_a_missing_state_is_read_back_as_empty_dns(self, service_module, tmp_path):
        """Round trip: what the hook writes for a gateway without DNS."""
        state = tmp_path / "dns-state"
        self._source_hook(
            tmp_path,
            {"GPCLIENT_NM_DNS_STATE": str(state), "TUNDEV": "gpd0"},
        )

        parsed = service_module.parse_dns_state(state.read_text())

        assert service_module.learned_dns_from_state(parsed, "gpd0") == ([], [], [])

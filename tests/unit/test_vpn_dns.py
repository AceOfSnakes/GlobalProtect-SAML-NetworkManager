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


    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   \n\n",
            "1BAD=x\nA-B=2\n=3\n#K=1\n",
        ],
    )
    def test_parse_dns_state_rejects_invalid_and_empty(self, service_module, text):
        # Keys must be shell variable names; nothing else becomes state
        assert service_module.parse_dns_state(text) == {}

    @pytest.mark.parametrize(
        "address",
        [
            "192.168.1",  # inet_aton() completes this to 192.168.0.1
            "1",  # ... and this to 0.0.0.1
            "10.0.0.256",
            "1.2.3.4.5",
            "0x7f.0.0.1",  # inet_aton() accepts hex parts
            "",
        ],
    )
    def test_ipv4_to_nm_uint32_rejects_malformed_addresses(
        self, service_module, address
    ):
        with pytest.raises((OSError, ValueError)):
            service_module.ipv4_to_nm_uint32(address)


class TestParseIpAddress:
    @pytest.mark.parametrize(
        "text, version",
        [("10.0.0.1", 4), ("0.0.0.0", 4), ("fd00::53", 6), ("::1", 6)],
    )
    def test_addresses_of_either_family_are_parsed_once(
        self, service_module, text, version
    ):
        assert service_module.parse_ip_address(text).version == version

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "10.1",
            "192.168.1",
            "1",
            "10.0.0.256",
            "0x7f.0.0.1",
            "host.example",
            "10.0.0.1:53",
        ],
    )
    def test_anything_else_is_none(self, service_module, text):
        assert service_module.parse_ip_address(text) is None

    def test_parsed_address_converts_like_the_string(self, service_module):
        address = service_module.parse_ip_address("10.0.0.1")
        assert service_module.ipv4_to_nm_uint32(address) == _nm_u32("10.0.0.1")
        assert service_module.ipv4_to_nm_uint32("10.0.0.1") == _nm_u32("10.0.0.1")


class TestProfileDnsParsing:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("10.0.0.1", ["10.0.0.1"]),
            ("10.0.0.1,10.0.0.2", ["10.0.0.1", "10.0.0.2"]),
            ("10.0.0.1, 10.0.0.2", ["10.0.0.1", "10.0.0.2"]),
            ("10.0.0.1 10.0.0.2", ["10.0.0.1", "10.0.0.2"]),
            ("10.0.0.1;10.0.0.2", ["10.0.0.1", "10.0.0.2"]),
            ("10.0.0.1 ;\t10.0.0.2,  fd00::53", ["10.0.0.1", "10.0.0.2", "fd00::53"]),
            (",10.0.0.1,,;", ["10.0.0.1"]),
            ("", []),
            (" , ; ", []),
        ],
    )
    def test_entries_are_split_on_commas_semicolons_and_whitespace(
        self, service_module, text, expected
    ):
        assert service_module.parse_dns_servers(text) == expected

    def test_an_address_is_not_cut_apart(self, service_module):
        # Only the separators split: "10.0.0.1:53" stays one (invalid) entry
        assert service_module.parse_dns_servers("10.0.0.1:53") == ["10.0.0.1:53"]
        assert service_module.parse_dns_servers("fd00::53") == ["fd00::53"]


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

    def test_invalid_profile_dns_entries_are_skipped(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        # One bad entry in the profile must not cost the good ones (or crash
        # the detection loop)
        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=["not-an-ip", "192.168.1", "10.0.0.1"],
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.1")])

    def test_profile_dns_without_a_valid_entry_falls_back_to_the_gateway(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox,
        caplog,
    ):
        # A typo in the only profile entry ("10.1" is not an address) must not
        # leave the tunnel without DNS while the gateway pushed a server
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=["10.1"],
            )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.53")])
        assert "None of the DNS servers in the profile is a valid IP" in caplog.text
        assert "10.0.0.53" in caplog.text  # what is used instead

    def test_profile_dns_with_a_valid_entry_does_not_fall_back(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox,
        caplog,
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=["10.1", "192.168.1.53"],
            )

        # Only the valid profile entry: the gateway's server is not added
        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])
        assert "None of the DNS servers in the profile" not in caplog.text

    def test_invalid_profile_dns_without_gateway_dns_reports_none(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=["10.1"],
            )

        assert "dns" not in config
        assert "None of the DNS servers in the profile is a valid IP" in caplog.text
        # Nothing pushed by the gateway: say so, do not print an empty list
        assert "[]" not in caplog.text
        assert "no DNS servers from the gateway" in caplog.text

    def test_fallback_warning_names_the_gateway_servers_it_uses(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox,
        caplog,
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=["10.1"],
            )

        assert "using the ones learned from the gateway: ['10.0.0.53']" in caplog.text
        assert "no DNS servers from the gateway" not in caplog.text

    @pytest.mark.parametrize(
        "profile",
        [["fd00::53"], ["10.1", "fd00::53"], ["fd00::53", "2001:db8::1"]],
    )
    def test_ipv6_override_is_not_replaced_by_the_gateway_servers(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox,
        caplog, profile,
    ):
        # The profile asked for IPv6 resolvers: no IPv4 DNS goes to NetworkManager
        # (Ip4Config cannot carry IPv6), and it is not a typo to fall back from
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=profile,
            )

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config
        assert "None of the DNS servers in the profile" not in caplog.text

    @pytest.mark.parametrize(
        "profile",
        [["fd00::53"], ["fd00::53", "2001:db8::1"], ["10.1", "fd00::53"]],
    )
    def test_ipv6_only_override_warns_that_no_dns_is_applied(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog, profile
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=profile,
            )

        assert "dns" not in config
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "IPv6 only" in w
            and "IPv6 DNS servers are not applied" in w
            and "no IPv4 DNS servers will be configured" in w
            for w in warnings
        ), warnings

    @pytest.mark.parametrize(
        "profile",
        [
            ["fd00::53", "192.168.1.53"],  # an IPv4 server is applied
            ["192.168.1.53"],
            ["10.1"],  # a typo: the fallback warning says it, not this one
            [],
        ],
    )
    def test_override_with_an_ipv4_server_or_none_has_no_ipv6_warning(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog, profile
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=profile,
            )

        assert "IPv6 only" not in caplog.text
        assert "IPv6 DNS servers are not applied" not in caplog.text

    def test_ipv6_entry_is_no_conversion_failure(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        # An IPv6 address is valid, only not reportable: no warning about it
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                monkeypatch,
                tmp_path,
                dbus_signals,
                dns_servers=["fd00::53"],
            )

        assert "Failed to convert" not in caplog.text

    def test_ipv6_entry_next_to_an_ipv4_one_keeps_only_the_ipv4_server(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=["fd00::53", "192.168.1.53"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])

    @pytest.mark.parametrize("profile", [["not-an-ip"], ["10.1", "10.0.0"], ["1.2.3"]])
    def test_entries_that_are_no_address_of_any_family_fall_back(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox,
        profile,
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=profile,
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.53")])

    def test_whitespace_around_a_profile_dns_entry_is_ignored(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        # The profile text goes through parse_dns_servers(), which drops the
        # whitespace; the conversion itself no longer strips (it used to, a
        # second time, and the test fed it padded entries directly)
        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=service_module.parse_dns_servers(" 10.0.0.1 ;\t10.0.0.2\n"),
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.1"), _nm_u32("10.0.0.2")])

    def test_a_padded_entry_is_no_address(self, service_module):
        # Nothing strips behind parse_dns_servers(): padding is not an address
        assert service_module.parse_ip_address(" 10.0.0.1") is None
        assert service_module.parse_ip_address("10.0.0.1 ") is None

    def test_ipv6_only_dns_is_not_reported(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        # Ip4Config has no room for IPv6 servers
        config = _detect(
            service_module,
            monkeypatch,
            tmp_path,
            dbus_signals,
            dns_servers=["fd00::53"],
        )

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config

    def test_ipv6_gateway_dns_alone_is_not_reported(
        self, service_module, monkeypatch, tmp_path, dbus_signals, dns_state_sandbox
    ):
        _write_state(dns_state_sandbox, TUNDEV="tun0", INTERNAL_IP6_DNS="fd00::53")

        config = _detect(service_module, monkeypatch, tmp_path, dbus_signals)

        assert "dns" not in config

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

    def test_clear_dns_state_survives_unremovable_path(
        self, service_module, dns_state_sandbox, caplog
    ):
        # A directory (not empty, so even root cannot unlink it) sits where
        # the state file should be
        dns_state_sandbox.mkdir()
        (dns_state_sandbox / "keep").write_text("x")
        plugin = service_module.GpclientVPNPlugin()

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            plugin._clear_dns_state()

        assert "Could not remove DNS state file" in caplog.text
        assert (dns_state_sandbox / "keep").exists()

    def test_unreadable_state_file_is_read_as_unknown(
        self, service_module, dns_state_sandbox, caplog
    ):
        dns_state_sandbox.mkdir()  # reading a directory fails, even as root
        plugin = service_module.GpclientVPNPlugin()

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            assert plugin._read_learned_dns("tun0") is None

        assert "Could not read DNS state file" in caplog.text


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

    def test_hook_state_for_another_tundev_is_not_learned(
        self, service_module, dns_state_sandbox, tmp_path
    ):
        self._source_hook(
            tmp_path,
            {
                "GPCLIENT_NM_DNS_STATE": str(dns_state_sandbox),
                "TUNDEV": "gpd0",
                "INTERNAL_IP4_DNS": "10.0.0.1",
            },
        )
        parsed = service_module.parse_dns_state(dns_state_sandbox.read_text())

        assert service_module.learned_dns_from_state(parsed, "gpd0") == (
            ["10.0.0.1"],
            [],
            [],
        )
        assert service_module.learned_dns_from_state(parsed, "tun0") is None
        plugin = service_module.GpclientVPNPlugin()
        assert plugin._read_learned_dns("gpd0") is not None
        assert plugin._read_learned_dns("tun0") is None

    def test_an_unwritable_state_location_is_reported_not_fatal(self, tmp_path):
        # The "directory" is a regular file, so it cannot be created even by
        # root; the hook must still let vpnc-script carry on (exit status 0)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        state = blocker / "dns-state"

        result = self._source_hook(
            tmp_path,
            {"GPCLIENT_NM_DNS_STATE": str(state), "TUNDEV": "tun0"},
        )

        assert result.returncode == 0
        assert "could not write DNS state" in result.stderr
        assert "written to" not in result.stderr
        assert sorted(p.name for p in tmp_path.iterdir()) == ["blocker"]

"""
Tests for reporting the gateway's DNS to NetworkManager (issue #15).

vpnc-script applies the DNS servers and search domains the gateway pushes
straight to systemd-resolved. NetworkManager never learned about them, so on
its next DNS recalculation (an IPv6 router advertisement on the Wi-Fi
interface was enough) it overwrote tun0's resolver state with its own, empty
one - VPN DNS gone, tunnel still up. The vpnc hook now tells the service what
the gateway pushed (Notify, issue #53) and the service reports it in Ip4Config,
which makes NetworkManager own (and reapply) the VPN resolver configuration.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ipaddress
import socket
import struct

import pytest
from vpnc_notify_helpers import active_plugin, ip4_config, notify, signals_named, vpnc_env


def _nm_u32(address):
    return struct.unpack("=I", socket.inet_aton(address))[0]


def _detect(service_module, dbus_signals, env=None, **plugin_attrs):
    """The vpnc hook reports tun0 (10.100.7.42) with the DNS in `env`"""
    plugin = active_plugin(service_module, **plugin_attrs)
    notify(plugin, vpnc_env(**{"VPNGATEWAY": None, **(env or {})}))
    return ip4_config(dbus_signals)


class TestVpncEnvParsing:
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
            # the split-DNS only domains are routing-only; corp.example is a
            # search domain already
            ["corp.example", "extra.example", "~lab.example"],
            ["fd00::53"],
        )

    @pytest.mark.parametrize(
        "split, expected",
        [
            ("Corp.Example", ["corp.example"]),  # a search domain routes too
            ("lab.example,lab.example", ["corp.example", "~lab.example"]),
            (",,", ["corp.example"]),  # no bare "~"
            ("", ["corp.example"]),
        ],
    )
    def test_split_dns_is_routing_only_unless_a_search_domain(
        self, service_module, split, expected
    ):
        learned = service_module.learned_dns_from_state(
            {"CISCO_DEF_DOMAIN": "corp.example", "CISCO_SPLIT_DNS": split}, "tun0"
        )

        assert learned[1] == expected

    def test_search_domains_never_get_the_routing_prefix(self, service_module):
        learned = service_module.learned_dns_from_state(
            {"CISCO_DEF_DOMAIN": "a.example b.example"}, "tun0"
        )

        assert learned[1] == ["a.example", "b.example"]

    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({"INTERNAL_IP6_ADDRESS": "fd00::5", "INTERNAL_IP6_NETMASK": "fd00::5/64"},
             ("fd00::5", 64)),
            ({"INTERNAL_IP6_NETMASK": "fd00::6/48"}, ("fd00::6", 48)),
            ({"INTERNAL_IP6_ADDRESS": "fd00::7"}, ("fd00::7", 128)),
            ({"INTERNAL_IP6_ADDRESS": "fd00::7", "INTERNAL_IP6_NETMASK": ""},
             ("fd00::7", 128)),
        ],
    )
    def test_tunnel_ipv6_from_state(self, service_module, fields, expected):
        found = service_module.tunnel_ipv6_from_state(dict(fields, TUNDEV="tun0"), "tun0")

        assert (str(found[0]), found[1]) == expected

    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"INTERNAL_IP6_ADDRESS": "", "INTERNAL_IP6_NETMASK": ""},
            {"INTERNAL_IP6_ADDRESS": "garbage"},
            {"INTERNAL_IP6_ADDRESS": "10.1.2.3"},
            {"INTERNAL_IP6_NETMASK": "fd00::5/200"},
            {"INTERNAL_IP6_NETMASK": "fd00::5/x"},
            {"INTERNAL_IP6_NETMASK": "/64"},
            {"INTERNAL_IP6_ADDRESS": "fd00::5", "TUNDEV": "tun7"},  # another tunnel
        ],
    )
    def test_tunnel_ipv6_from_state_rejects_bad_state(self, service_module, state):
        state = {"TUNDEV": "tun0", **state}

        assert service_module.tunnel_ipv6_from_state(state, "tun0") is None

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


class TestVpnGatewayFromState:
    @pytest.mark.parametrize(
        "state, expected",
        [
            ({"TUNDEV": "tun0", "VPNGATEWAY": "203.0.113.9"}, "203.0.113.9"),
            ({"VPNGATEWAY": "203.0.113.9"}, "203.0.113.9"),
            ({"TUNDEV": "tun0", "VPNGATEWAY": "2001:db8::1"}, "2001:db8::1"),
        ],
    )
    def test_ipv4_gateway_of_this_tunnel(self, service_module, state, expected):
        got = service_module.vpn_gateway_from_state(state, "tun0")
        assert str(got) == expected

    @pytest.mark.parametrize(
        "state",
        [
            {"TUNDEV": "tun1", "VPNGATEWAY": "203.0.113.9"},
            {"TUNDEV": "tun0"},
            {"TUNDEV": "tun0", "VPNGATEWAY": ""},
            {"TUNDEV": "tun0", "VPNGATEWAY": "vpn.example.com"},
            {"TUNDEV": "tun0", "VPNGATEWAY": "203.0.113"},
            {"TUNDEV": "tun0", "VPNGATEWAY": "203.0.113.9 1.2.3.4"},
        ],
    )
    def test_anything_else_is_none(self, service_module, state):
        assert service_module.vpn_gateway_from_state(state, "tun0") is None


class TestNotifySendsVpnGateway:
    def test_gateway_is_the_vpn_server_address(self, service_module, dbus_signals):
        config = _detect(service_module, dbus_signals, {"VPNGATEWAY": "203.0.113.9"})
        assert config["gateway"] == ("u", _nm_u32("203.0.113.9"))

    @pytest.mark.parametrize(
        "gateway",
        [None, "", "2001:db8::1", "garbage", "203.0.113", "203.0.113.9 1.2.3.4"],
    )
    def test_fallback_is_the_tunnel_address(
        self, service_module, dbus_signals, gateway
    ):
        config = _detect(service_module, dbus_signals, {"VPNGATEWAY": gateway})
        assert config["gateway"] == ("u", _nm_u32("10.100.7.42"))


class TestDetectionReportsDns:
    def test_gateway_dns_lands_in_ip4config(
        self, service_module, dbus_signals
    ):
        """The issue #15 scenario: no `dns` in the profile, gateway pushes two
        servers and a domain - NetworkManager must hear about them."""
        env = dict(
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.0.0.1 10.0.0.2",
            INTERNAL_IP6_DNS="",
            CISCO_DEF_DOMAIN="corp.example",
            CISCO_SPLIT_DNS="",
        )

        config = _detect(service_module, dbus_signals, env)

        assert config["tundev"] == ("s", "tun0")
        assert config["dns"] == ("au", [_nm_u32("10.0.0.1"), _nm_u32("10.0.0.2")])
        assert config["domains"] == ("as", ["corp.example"])

    def test_profile_dns_overrides_gateway_servers_but_keeps_domains(
        self, service_module, dbus_signals
    ):
        env = dict(
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.0.0.1",
            CISCO_DEF_DOMAIN="corp.example",
        )

        config = _detect(
            service_module,
            dbus_signals,
            env,
            dns_servers=["192.168.1.53"],
            dns_domains=["extra.example", "corp.example"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])
        assert config["domains"] == ("as", ["corp.example", "extra.example"])

    def test_split_dns_domains_go_to_networkmanager_as_routing_only(
        self, service_module, dbus_signals
    ):
        env = dict(
            TUNDEV="tun0",
            CISCO_DEF_DOMAIN="corp.example",
            CISCO_SPLIT_DNS="corp.example,lab.example,extra.example",
        )

        config = _detect(
            service_module,
            dbus_signals,
            env,
            dns_domains=["extra.example"],
        )

        assert config["domains"] == (
            "as",
            ["corp.example", "~lab.example", "extra.example"],
        )

    def test_without_state_file_the_tunnel_is_still_reported(
        self, service_module, dbus_signals
    ):
        # No hook installed: behave as before (no DNS in Ip4Config)
        config = _detect(service_module, dbus_signals)

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config
        assert "domains" not in config

    def test_profile_dns_alone_still_works_without_state_file(
        self, service_module, dbus_signals
    ):
        config = _detect(
            service_module,
            dbus_signals,
            dns_servers=["192.168.1.53"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])

    def test_invalid_profile_dns_entries_are_skipped(
        self, service_module, dbus_signals
    ):
        # One bad entry in the profile must not cost the good ones (or crash
        # the detection loop)
        config = _detect(
            service_module,
            dbus_signals,
            dns_servers=["not-an-ip", "192.168.1", "10.0.0.1"],
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.1")])

    def test_profile_dns_without_a_valid_entry_falls_back_to_the_gateway(
        self, service_module, dbus_signals,
        caplog,
    ):
        # A typo in the only profile entry ("10.1" is not an address) must not
        # leave the tunnel without DNS while the gateway pushed a server
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                dbus_signals,
                env,
                dns_servers=["10.1"],
            )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.53")])
        assert "None of the DNS servers in the profile is a valid IP" in caplog.text
        assert "10.0.0.53" in caplog.text  # what is used instead

    def test_profile_dns_with_a_valid_entry_does_not_fall_back(
        self, service_module, dbus_signals,
        caplog,
    ):
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                dbus_signals,
                env,
                dns_servers=["10.1", "192.168.1.53"],
            )

        # Only the valid profile entry: the gateway's server is not added
        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])
        assert "None of the DNS servers in the profile" not in caplog.text

    def test_invalid_profile_dns_without_gateway_dns_reports_none(
        self, service_module, dbus_signals, caplog
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                dbus_signals,
                dns_servers=["10.1"],
            )

        assert "dns" not in config
        assert "None of the DNS servers in the profile is a valid IP" in caplog.text
        # Nothing pushed by the gateway: say so, do not print an empty list
        assert "[]" not in caplog.text
        assert "no DNS servers from the gateway" in caplog.text

    def test_fallback_warning_names_the_gateway_servers_it_uses(
        self, service_module, dbus_signals,
        caplog,
    ):
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                dbus_signals,
                env,
                dns_servers=["10.1"],
            )

        assert "using the ones learned from the gateway: ['10.0.0.53']" in caplog.text
        assert "no DNS servers from the gateway" not in caplog.text

    @pytest.mark.parametrize(
        "profile",
        [["fd00::53"], ["10.1", "fd00::53"], ["fd00::53", "2001:db8::1"]],
    )
    def test_ipv6_override_is_not_replaced_by_the_gateway_servers(
        self, service_module, dbus_signals,
        caplog, profile,
    ):
        # The profile asked for IPv6 resolvers: no IPv4 DNS goes to NetworkManager
        # (Ip4Config cannot carry IPv6), and it is not a typo to fall back from
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
                dbus_signals,
                env,
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
        self, service_module, dbus_signals, caplog, profile
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            config = _detect(
                service_module,
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
        self, service_module, dbus_signals, caplog, profile
    ):
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                dbus_signals,
                dns_servers=profile,
            )

        assert "IPv6 only" not in caplog.text
        assert "IPv6 DNS servers are not applied" not in caplog.text

    def test_ipv6_entry_is_no_conversion_failure(
        self, service_module, dbus_signals, caplog
    ):
        # An IPv6 address is valid, only not reportable: no warning about it
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module,
                dbus_signals,
                dns_servers=["fd00::53"],
            )

        assert "Failed to convert" not in caplog.text

    def test_ipv6_entry_next_to_an_ipv4_one_keeps_only_the_ipv4_server(
        self, service_module, dbus_signals
    ):
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        config = _detect(
            service_module,
            dbus_signals,
            env,
            dns_servers=["fd00::53", "192.168.1.53"],
        )

        assert config["dns"] == ("au", [_nm_u32("192.168.1.53")])

    @pytest.mark.parametrize("profile", [["not-an-ip"], ["10.1", "10.0.0"], ["1.2.3"]])
    def test_entries_that_are_no_address_of_any_family_fall_back(
        self, service_module, dbus_signals,
        profile,
    ):
        env = dict(TUNDEV="tun0", INTERNAL_IP4_DNS="10.0.0.53")

        config = _detect(
            service_module,
            dbus_signals,
            env,
            dns_servers=profile,
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.53")])

    def test_whitespace_around_a_profile_dns_entry_is_ignored(
        self, service_module, dbus_signals
    ):
        # The profile text goes through parse_dns_servers(), which drops the
        # whitespace; the conversion itself no longer strips (it used to, a
        # second time, and the test fed it padded entries directly)
        config = _detect(
            service_module,
            dbus_signals,
            dns_servers=service_module.parse_dns_servers(" 10.0.0.1 ;\t10.0.0.2\n"),
        )

        assert config["dns"] == ("au", [_nm_u32("10.0.0.1"), _nm_u32("10.0.0.2")])

    def test_a_padded_entry_is_no_address(self, service_module):
        # Nothing strips behind parse_dns_servers(): padding is not an address
        assert service_module.parse_ip_address(" 10.0.0.1") is None
        assert service_module.parse_ip_address("10.0.0.1 ") is None

    def test_ipv6_only_dns_is_not_reported(
        self, service_module, dbus_signals
    ):
        # Ip4Config has no room for IPv6 servers
        config = _detect(
            service_module,
            dbus_signals,
            dns_servers=["fd00::53"],
        )

        assert config["tundev"] == ("s", "tun0")
        assert "dns" not in config

    def test_ipv6_gateway_dns_alone_is_not_reported(
        self, service_module, dbus_signals
    ):
        env = dict(TUNDEV="tun0", INTERNAL_IP6_DNS="fd00::53")

        config = _detect(service_module, dbus_signals, env)

        assert "dns" not in config


def _signal(signals, name):
    return [payload for n, payload in signals if n == name]


class TestDetectionReportsIp6Config:
    """A dual-stack tunnel: has-ip6 and Ip6Config go together (issue #53)"""

    DUAL = dict(
        TUNDEV="tun0",
        INTERNAL_IP4_DNS="10.0.0.1",
        INTERNAL_IP6_DNS="fd00::53 fd00::54",
        CISCO_DEF_DOMAIN="corp.example",
        CISCO_SPLIT_DNS="lab.example",
        INTERNAL_IP6_ADDRESS="fd00::5",
        INTERNAL_IP6_NETMASK="fd00::5/64",
    )

    def test_dual_stack_emits_ip6config_after_ip4config(
        self, service_module, dbus_signals
    ):
        env = dict(self.DUAL)

        _detect(service_module, dbus_signals, env)

        assert [n for n, _ in dbus_signals][:3] == ["Config", "Ip4Config", "Ip6Config"]
        assert signals_named(dbus_signals, "Config")[0]["has-ip6"] == ("b", True)
        ip = ipaddress.IPv6Address
        assert signals_named(dbus_signals, "Ip6Config")[0] == {
            "tundev": ("s", "tun0"),
            "address": ("ay", ip("fd00::5").packed),
            "prefix": ("u", 64),
            "dns": ("aay", [ip("fd00::53").packed, ip("fd00::54").packed]),
            "domains": ("as", ["corp.example", "~lab.example"]),
        }

    def test_profile_dns_with_a_valid_address_replaces_gateway_ipv6_dns(
        self, service_module, dbus_signals
    ):
        env = dict(self.DUAL)

        _detect(
            service_module,
            dbus_signals,
            env,
            dns_servers=["2001:db8::1", "192.168.1.53"],
        )

        dns = signals_named(dbus_signals, "Ip6Config")[0]["dns"]
        assert dns == ("aay", [ipaddress.IPv6Address("2001:db8::1").packed])

    def test_ipv4_only_profile_dns_leaves_ip6config_without_dns(
        self, service_module, dbus_signals
    ):
        env = dict(self.DUAL)

        _detect(
            service_module, dbus_signals, env,
            dns_servers=["192.168.1.53"],
        )

        assert "dns" not in signals_named(dbus_signals, "Ip6Config")[0]

    def test_invalid_profile_dns_falls_back_to_gateway_ipv6_dns(
        self, service_module, dbus_signals
    ):
        env = dict(self.DUAL)

        _detect(
            service_module, dbus_signals, env,
            dns_servers=["not-an-ip"],
        )

        assert len(signals_named(dbus_signals, "Ip6Config")[0]["dns"][1]) == 2

    @pytest.mark.parametrize("never_default", [True, False])
    def test_never_default_covers_ipv6(
        self, service_module, dbus_signals,
        never_default,
    ):
        env = dict(self.DUAL)

        _detect(
            service_module, dbus_signals, env,
            never_default=never_default,
        )

        config6 = signals_named(dbus_signals, "Ip6Config")[0]
        assert config6.get("never-default") == (
            ("b", True) if never_default else None
        )

    def test_ipv6_only_profile_dns_on_dual_stack_is_applied_without_warning(
        self, service_module, dbus_signals,
        caplog,
    ):
        env = dict(self.DUAL)

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            _detect(
                service_module, dbus_signals, env,
                dns_servers=["2001:db8::1"],
            )

        dns = signals_named(dbus_signals, "Ip6Config")[0]["dns"]
        assert dns == ("aay", [ipaddress.IPv6Address("2001:db8::1").packed])
        assert "IPv6 DNS servers are not applied" not in caplog.text

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"INTERNAL_IP6_ADDRESS": "", "INTERNAL_IP6_NETMASK": ""},
            {"INTERNAL_IP6_ADDRESS": "garbage", "INTERNAL_IP6_NETMASK": ""},
        ],
    )
    def test_no_ipv6_address_means_no_ip6config_and_no_has_ip6(
        self, service_module, dbus_signals,
        caplog, extra,
    ):
        fields = dict(self.DUAL, INTERNAL_IP6_ADDRESS="", INTERNAL_IP6_NETMASK="")
        env = dict(**{**fields, **extra})

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, dbus_signals, env)

        assert signals_named(dbus_signals, "Ip6Config") == []
        assert signals_named(dbus_signals, "Config")[0]["has-ip6"] == ("b", False)
        # the gateway's IPv6 DNS is only logged for tunnels without IPv6
        assert "no IPv6 config" in caplog.text

    def test_the_log_line_about_missing_ipv6_config_is_gone_with_ip6config(
        self, service_module, dbus_signals,
        caplog,
    ):
        env = dict(self.DUAL)

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, dbus_signals, env)

        assert "no IPv6 config" not in caplog.text

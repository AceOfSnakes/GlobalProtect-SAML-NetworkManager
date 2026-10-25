"""
Tests for the tunnel settings lines of gpclient (issue #37).

Since GlobalProtect-openconnect 3.1.0 gpclient logs what the tunnel negotiated
("VPN interface: tun0", "VPN DNS: a, b", ...; gptunnel log_configuration). The
service reads them as a cross-check and a fallback for the DNS state of the
vpnc hook. The hook stays the source for routing: the log never changes the
interface or the address, and never replaces what the hook recorded.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import dataclasses
import sys

import pytest

from test_dbus_methods import _conn, _plugin, connect, fake_exec  # noqa: F401
from test_tunnel_detection import _ip4_config, _plugin_with_tunnels, _run_loop

PREFIX = "[2026-01-01T00:00:02Z INFO  gptunnel::configuration] "


def _lines(**overrides):
    """The block gpclient logs for a tunnel, as it prints it"""
    values = {
        "interface": "tun0",
        "IPv4": "10.100.7.42/24",
        "IPv6": "none",
        "gateway IP": "198.51.100.7",
        "DNS": "10.1.1.1, 10.1.1.2",
        "search domains": "corp.example, lab.example",
        "split DNS": "none",
        "routes": "3 included, 0 excluded",
        "MTU": "1400",
    }
    values.update(overrides)
    return [f"{PREFIX}VPN {key}: {value}" for key, value in values.items()]


def _parse(service_module, lines):
    settings = service_module.TunnelSettings()
    for line in lines:
        settings = service_module.parse_tunnel_settings_line(line, settings)
    return settings


class TestParseLines:
    def test_a_whole_block(self, service_module):
        settings = _parse(service_module, _lines(**{"split DNS": "vpn.example"}))

        assert settings == service_module.TunnelSettings(
            interface="tun0",
            ipv4="10.100.7.42",
            prefix=24,
            dns=("10.1.1.1", "10.1.1.2"),
            search_domains=("corp.example", "lab.example"),
            split_dns=("vpn.example",),
            mtu=1400,
        )

    @pytest.mark.parametrize(
        "line, field, expected",
        [
            ("VPN interface: tun0", "interface", "tun0"),
            ("VPN interface: gpd0", "interface", "gpd0"),
            ("VPN IPv4: 10.0.0.2/24", "ipv4", "10.0.0.2"),
            ("VPN IPv4: 10.0.0.2", "ipv4", "10.0.0.2"),
            ("VPN IPv4: 10.0.0.2 (mask 255.0.255.0)", "ipv4", "10.0.0.2"),
            ("VPN DNS: 10.1.1.1", "dns", ("10.1.1.1",)),
            ("VPN DNS: 10.1.1.1, 2001:db8::53", "dns", ("10.1.1.1", "2001:db8::53")),
            ("VPN DNS: 10.1.1.1, 10.1.1.1", "dns", ("10.1.1.1",)),
            ("VPN DNS: none", "dns", ()),
            ("VPN search domains: corp.example", "search_domains", ("corp.example",)),
            ("VPN search domains: none", "search_domains", ()),
            (
                "VPN split DNS: a.example, b.example",
                "split_dns",
                ("a.example", "b.example"),
            ),
            ("VPN MTU: 1400", "mtu", 1400),
        ],
    )
    def test_each_line_kind(self, service_module, line, field, expected):
        settings = _parse(service_module, [PREFIX + line])

        assert getattr(settings, field) == expected

    @pytest.mark.parametrize(
        "prefix",
        [
            "[2026-01-01T00:00:02Z INFO  gptunnel::configuration] ",
            "[2026-01-01T00:00:02Z INFO gpclient::connect] ",
            "[INFO  gpclient::connect] ",
        ],
    )
    def test_log_prefixes(self, service_module, prefix):
        settings = service_module.parse_tunnel_settings_line(
            prefix + "VPN DNS: 10.1.1.1", service_module.TunnelSettings()
        )

        assert settings.dns == ("10.1.1.1",)

    def test_the_prefix_is_ignored_with_trailing_blanks(self, service_module):
        settings = _parse(service_module, [PREFIX + "VPN MTU: 1400   "])

        assert settings.mtu == 1400

    def test_the_interface_line_starts_a_new_record(self, service_module):
        first = _parse(service_module, _lines())
        second = service_module.parse_tunnel_settings_line(
            PREFIX + "VPN interface: tun1", first
        )

        assert second == service_module.TunnelSettings(interface="tun1")
        assert first.dns == ("10.1.1.1", "10.1.1.2")  # the old record is not changed

    def test_the_record_is_immutable(self, service_module):
        settings = service_module.TunnelSettings()

        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.dns = ("10.1.1.1",)


class TestRejectedValues:
    @pytest.mark.parametrize(
        "line",
        [
            "VPN DNS: 10.1.1.300",
            "VPN DNS: not-an-ip",
            "VPN DNS: 10.1.1.1, bogus",  # one bad item: the list is not trusted
            "VPN DNS: 10.1.1.1,10.1.1.2",  # not joined by ", "
            "VPN DNS: 10.1.1.1, ",
            "VPN DNS: fe80::1%eth0",
            "VPN DNS: 10.1.1.1\\n10.1.1.2",  # escaped by upstream: not reversed
            "VPN DNS: 10.1.1.1 (1 of 40 shown; truncated)",
            "VPN DNS: 10.1.1.1, 10.1.1.2 (2 of 40 shown; truncated)",
            "VPN DNS: none (0 of 4 shown; truncated)",
        ],
    )
    def test_dns_that_cannot_be_trusted_is_not_used(self, service_module, line):
        settings = _parse(service_module, [PREFIX + line])

        assert settings.dns is None

    @pytest.mark.parametrize("key", ["search domains", "split DNS"])
    @pytest.mark.parametrize(
        "value",
        [
            "corp.example, bad domain",
            "corp\\u{1b}.example",  # escaped
            "corp.example, a\\\\b",
            "-corp.example",
            "corp..example",
            "corp.example, ",
            "corp.example, lab.example (1 of 9 shown; truncated)",
            "a" * 64 + ".example",
        ],
    )
    def test_domains_that_cannot_be_trusted_are_not_used(
        self, service_module, key, value
    ):
        settings = _parse(service_module, [f"{PREFIX}VPN {key}: {value}"])

        assert settings.search_domains is None
        assert settings.split_dns is None

    @pytest.mark.parametrize(
        "value",
        [
            "10.0.0.300/24",
            "none",
            "unavailable",
            "garbage",
            "10.0.0.2\\n",
            "2001:db8::2/64",  # the line for IPv4 never holds an IPv6 address
            "010.000.000.002/24",
        ],
    )
    def test_ipv4_that_is_no_address_is_not_used(self, service_module, value):
        settings = _parse(service_module, [f"{PREFIX}VPN IPv4: {value}"])

        assert settings.ipv4 is None
        assert settings.prefix is None

    @pytest.mark.parametrize("value", ["10.0.0.2/33", "10.0.0.2/x", "10.0.0.2/-1"])
    def test_ipv4_with_a_bad_prefix_has_no_prefix(self, service_module, value):
        settings = _parse(service_module, [f"{PREFIX}VPN IPv4: {value}"])

        assert settings.ipv4 == "10.0.0.2"
        assert settings.prefix is None

    @pytest.mark.parametrize(
        "value", ["unavailable", "none", "-5", "0", "70000", "14x0", "١٤٠٠", ""]
    )
    def test_mtu_that_is_no_usable_number_is_not_used(self, service_module, value):
        settings = _parse(service_module, [f"{PREFIX}VPN MTU: {value}"])

        assert settings.mtu is None

    @pytest.mark.parametrize(
        "value", ["none", "tun0\\n", "a" * 16, "tun 0", "tun0, tun1", ""]
    )
    def test_interface_that_is_not_a_name_is_not_used(self, service_module, value):
        settings = _parse(service_module, [f"{PREFIX}VPN interface: {value}"])

        assert settings.interface is None

    def test_a_bad_value_clears_the_earlier_one_of_the_same_block(self, service_module):
        settings = _parse(
            service_module,
            [PREFIX + "VPN interface: tun0", PREFIX + "VPN DNS: 10.1.1.1"]
            + [PREFIX + "VPN DNS: bogus"],
        )

        assert settings.interface == "tun0"
        assert settings.dns is None


class TestOtherLines:
    @pytest.mark.parametrize(
        "line",
        [
            "VPN DNS: 10.1.1.1",  # no log prefix
            "? Username: VPN DNS: 10.1.1.1",
            "[2026-01-01T00:00:02Z WARN  gptunnel::configuration] VPN DNS: 10.1.1.1",
            "[2026-01-01T00:00:02Z DEBUG gptunnel::configuration] VPN DNS: 10.1.1.1",
            PREFIX + "Applying VPN DNS: 10.1.1.1",
            PREFIX + "The VPN DNS: 10.1.1.1",
            PREFIX + "VPN DNS 10.1.1.1",
            PREFIX + "VPN DNSX: 10.1.1.1",
            PREFIX + "VPN interface up: tun0",
            PREFIX + "VPN gateway IP: 198.51.100.7",
            PREFIX + "VPN IPv6: 2001:db8::2/64",
            PREFIX + "VPN routes: 3 included, 0 excluded",
            PREFIX + "VPN configuration: unavailable",
            PREFIX + "VPN configuration: truncated",
            "",
        ],
    )
    def test_the_record_is_not_changed(self, service_module, line):
        before = service_module.TunnelSettings(interface="tun0", dns=("10.9.9.9",))

        after = service_module.parse_tunnel_settings_line(line, before)

        assert after is before

    def test_the_regex_names_the_key(self, service_module):
        match = service_module.TUNNEL_SETTINGS_RE.match(
            PREFIX + "VPN search domains: a.example"
        )

        assert match.group("key") == "search domains"
        assert match.group("value") == "a.example"


# A gpclient that prints the lines the test gives it, then exits
FAKE_GPCLIENT = r"""
import sys
sys.stdout.buffer.write(
    ("[2026-01-01T00:00:00Z INFO  gpclient::cli] started\r\n").encode()
)
for line in __LINES__:
    sys.stdout.buffer.write((line + "\r\n").encode("utf-8"))
sys.stdout.flush()
sys.exit(0)
"""


def _plugin_running(service_module, monkeypatch, tmp_path, lines):
    script = tmp_path / "gpclient.py"
    script.write_text(FAKE_GPCLIENT.replace("__LINES__", repr(list(lines))))
    launcher = tmp_path / "gpclient.sh"
    launcher.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(launcher))
    plugin = service_module.GpclientVPNPlugin()
    plugin.gateway = "portal.example.com"
    plugin.browser = "/bin/true"
    return plugin


def _read_output(plugin, timeout=10):
    async def scenario():
        assert await plugin._start_gpclient()
        process = plugin.gpclient_process
        try:
            await asyncio.wait_for(asyncio.shield(plugin.stdout_monitor_task), timeout)
            await asyncio.wait_for(process.wait(), timeout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())


def _state(service_module, **fields):
    with open(service_module.DNS_STATE_FILE, "w", encoding="utf-8") as f:
        f.write("".join(f"{k}={v}\n" for k, v in fields.items()))


def _disagreements(caplog):
    """The warnings about the hook and the log disagreeing"""
    return [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "disagree" in r.getMessage()
    ]


def _nm(service_module, *addresses):
    return [service_module.ipv4_to_nm_uint32(a) for a in addresses]


def _detect(service_module, monkeypatch, tmp_path, plugin):
    """Run the tunnel detection of `plugin` against a tun0 with 10.100.7.42"""
    plugin2 = _plugin_with_tunnels(
        service_module, monkeypatch, tmp_path, {"tun0": "10.100.7.42"}, {}
    )
    # the plugin that read the output is the one that detects the tunnel
    plugin2._tunnel_settings = plugin._tunnel_settings
    assert _run_loop(plugin2) is True


class TestOutputPath:
    def test_a_transcript_fills_the_record(self, service_module, monkeypatch, tmp_path):
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, _lines())

        _read_output(plugin)

        assert plugin._tunnel_settings.interface == "tun0"
        assert plugin._tunnel_settings.dns == ("10.1.1.1", "10.1.1.2")
        assert plugin._tunnel_settings.mtu == 1400

    def test_a_transcript_without_the_lines_leaves_the_record_empty(
        self, service_module, monkeypatch, tmp_path
    ):
        plugin = _plugin_running(
            service_module,
            monkeypatch,
            tmp_path,
            [PREFIX + "Applying VPN DNS: 10.1.1.1", "VPN DNS: 10.1.1.1"],
        )

        _read_output(plugin)

        assert plugin._tunnel_settings == service_module.TunnelSettings()

    def test_the_record_is_reset_when_a_connection_starts(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)
        plugin._tunnel_settings = _parse(service_module, _lines())

        connect(plugin, _conn())

        assert plugin._tunnel_settings == service_module.TunnelSettings()

    def test_the_record_is_reset_on_disconnect(
        self, service_module, monkeypatch, fake_exec  # noqa: F811  # the fixture imported above
    ):
        plugin = _plugin(service_module, monkeypatch)
        plugin._tunnel_settings = _parse(service_module, _lines())

        asyncio.run(plugin.Disconnect())

        assert plugin._tunnel_settings == service_module.TunnelSettings()

    def test_the_record_is_reset_for_a_retry_of_gpclient(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._tunnel_settings = _parse(service_module, _lines())

        plugin._reset_output_state()

        assert plugin._tunnel_settings == service_module.TunnelSettings()


class TestFallbackAndCrossCheck:
    def test_log_dns_is_used_when_the_hook_wrote_none(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_running(
            service_module,
            monkeypatch,
            tmp_path,
            _lines(**{"split DNS": "vpn.example"}),
        )
        _read_output(plugin)
        _state(service_module, TUNDEV="tun0", VPNGATEWAY="198.51.100.7")

        _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["dns"] == ("au", _nm(service_module, "10.1.1.1", "10.1.1.2"))
        assert config["domains"] == (
            "as",
            ["corp.example", "lab.example", "vpn.example"],
        )

    def test_log_dns_is_used_when_the_hook_wrote_no_state_file(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, _lines())
        _read_output(plugin)

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["dns"] == ("au", _nm(service_module, "10.1.1.1", "10.1.1.2"))
        assert "No DNS servers from the vpnc hook - using" in caplog.text
        assert _disagreements(caplog) == []

    def test_the_hook_wins_on_a_mismatch_with_one_warning(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, _lines())
        _read_output(plugin)
        _state(
            service_module,
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.2.2.2",
            CISCO_DEF_DOMAIN="other.example",
        )

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["dns"] == ("au", _nm(service_module, "10.2.2.2"))
        assert config["domains"] == ("as", ["other.example"])
        warnings = _disagreements(caplog)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        for text in ("10.2.2.2", "10.1.1.1", "other.example", "corp.example"):
            assert text in message

    def test_a_matching_log_is_silent(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        plugin = _plugin_running(
            service_module,
            monkeypatch,
            tmp_path,
            _lines(DNS="10.1.1.1", **{"search domains": "Corp.Example"}),
        )
        _read_output(plugin)
        _state(
            service_module,
            TUNDEV="tun0",
            INTERNAL_IP4_DNS="10.1.1.1",
            CISCO_DEF_DOMAIN="corp.example",
        )

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["dns"] == ("au", _nm(service_module, "10.1.1.1"))
        assert config["domains"] == ("as", ["corp.example"])
        assert _disagreements(caplog) == []

    def test_the_hook_only_fills_what_it_has_and_the_log_the_rest(
        self, service_module, monkeypatch, tmp_path, dbus_signals, caplog
    ):
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, _lines())
        _read_output(plugin)
        _state(service_module, TUNDEV="tun0", INTERNAL_IP4_DNS="10.1.1.1 10.1.1.2")

        with caplog.at_level("INFO", logger=service_module.logger.name):
            _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["dns"] == ("au", _nm(service_module, "10.1.1.1", "10.1.1.2"))
        assert config["domains"] == ("as", ["corp.example", "lab.example"])

    @pytest.mark.parametrize(
        "overrides",
        [
            {"DNS": "10.1.1.1, 10.1.1.2 (2 of 40 shown; truncated)"},
            {"DNS": "10.1.1.1, bogus"},
            {"DNS": "10.1.1.1\\n"},
            {"DNS": "none"},
            {"interface": "tun1"},  # settings of another interface
            {"interface": "none"},
        ],
    )
    def test_dns_that_is_not_complete_or_not_ours_is_no_fallback(
        self, service_module, monkeypatch, tmp_path, dbus_signals, overrides
    ):
        lines = _lines(**{"search domains": "none"}, **overrides)
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, lines)
        _read_output(plugin)
        _state(service_module, TUNDEV="tun0", VPNGATEWAY="198.51.100.7")

        _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert "dns" not in config
        assert "domains" not in config

    @pytest.mark.parametrize(
        "overrides",
        [
            {"search domains": "corp.example (1 of 9 shown; truncated)"},
            {"split DNS": "a.example (1 of 9 shown; truncated)"},
            {"search domains": "bad domain"},
        ],
    )
    def test_domains_that_are_not_complete_are_no_fallback(
        self, service_module, monkeypatch, tmp_path, dbus_signals, overrides
    ):
        plugin = _plugin_running(
            service_module, monkeypatch, tmp_path, _lines(**overrides)
        )
        _read_output(plugin)

        _detect(service_module, monkeypatch, tmp_path, plugin)

        assert "domains" not in _ip4_config(dbus_signals)

    def test_the_log_never_changes_the_interface_or_the_address(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_running(
            service_module,
            monkeypatch,
            tmp_path,
            _lines(IPv4="10.9.9.9/16", **{"gateway IP": "203.0.113.9"}),
        )
        _read_output(plugin)
        _state(service_module, TUNDEV="tun0", VPNGATEWAY="198.51.100.7")

        _detect(service_module, monkeypatch, tmp_path, plugin)

        config = _ip4_config(dbus_signals)
        assert config["tundev"] == ("s", "tun0")
        assert config["address"] == (
            "u",
            service_module.ipv4_to_nm_uint32("10.100.7.42"),
        )
        assert config["prefix"] == ("u", 24)
        assert config["gateway"] == (
            "u",
            service_module.ipv4_to_nm_uint32("198.51.100.7"),
        )

    def test_a_reset_record_gives_no_fallback(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_running(service_module, monkeypatch, tmp_path, _lines())
        _read_output(plugin)
        plugin._reset_output_state()

        _detect(service_module, monkeypatch, tmp_path, plugin)

        assert "dns" not in _ip4_config(dbus_signals)

    def test_ipv6_servers_are_not_reported_in_ip4config(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        plugin = _plugin_running(
            service_module,
            monkeypatch,
            tmp_path,
            _lines(DNS="2001:db8::53, 10.1.1.1"),
        )
        _read_output(plugin)

        _detect(service_module, monkeypatch, tmp_path, plugin)

        assert _ip4_config(dbus_signals)["dns"] == (
            "au",
            _nm(service_module, "10.1.1.1"),
        )

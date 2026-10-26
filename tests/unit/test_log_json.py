"""
Tests for the JSON log records of gpclient (issue #53).

The service runs `gpclient --log-format json`. Every `log` record (and the
openconnect messages that go through it) is then one JSON line with the strings
level, target and message, and the service matches the message instead of the
text prefix. println! output and the prompts stay plain text and take the old
path, as does any line that is not a record.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json
import logging

import pytest
from test_service_gaps import FakeProcess, _log_text, _monitor, _plugin

PREFIX = "[2026-01-01T00:00:02Z INFO  gptunnel::configuration] "


def record(message, level="INFO", target="gpclient::connect"):
    """A line as `gpclient --log-format json` writes it"""
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "level": level,
            "target": target,
            "message": message,
        }
    )


def _run(plugin, *lines):
    asyncio.run(
        _monitor(plugin, [("\r\n".join(lines) + "\r\n").encode()], FakeProcess())
    )


class TestParseLogRecord:
    def test_a_record(self, service_module):
        assert service_module.parse_log_record(record("Found 2 gateways")) == {
            "level": "INFO",
            "target": "gpclient::connect",
            "message": "Found 2 gateways",
        }

    def test_a_newline_in_the_message_is_escaped(self, service_module):
        parsed = service_module.parse_log_record(record("one\ntwo\rthree"))

        assert parsed["message"] == "one\\ntwo\\rthree"

    @pytest.mark.parametrize(
        "message", ["gw\x1b[31mX\x07", "g\x00w\x7fX", "\x1b]0;title\x07gwX"]
    )
    def test_escape_sequences_and_control_characters_are_removed(
        self, service_module, message
    ):
        assert service_module.parse_log_record(record(message))["message"] == "gwX"

    def test_a_tab_is_kept(self, service_module):
        assert service_module.parse_log_record(record("a\tb"))["message"] == "a\tb"

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "plain text",
            "{",
            '{"level": "INFO", "target": "t", "message": "cut',
            "[]",
            '["level", "target", "message"]',
            '"x"',
            "42",
            "null",
            "{}",
            '{"target": "t", "message": "m"}',
            '{"level": "INFO", "message": "m"}',
            '{"level": "INFO", "target": "t"}',
            '{"level": "INFO", "target": "t", "message": 5}',
            '{"level": 3, "target": "t", "message": "m"}',
            '{"level": "INFO", "target": null, "message": "m"}',
            PREFIX + "VPN interface: tun0",
            "? Enter your 6 digit passcode:",
            "> gw-warsaw (gw1.example.com)",
        ],
    )
    def test_anything_else_is_a_text_line(self, service_module, line):
        assert service_module.parse_log_record(line) is None


class TestTunnelSettingsRecord:
    def test_info_record_is_read(self, service_module):
        settings = service_module.parse_tunnel_settings_line(
            record("VPN DNS: 10.1.1.1, 10.1.1.2", target="gptunnel::configuration"),
            service_module.TunnelSettings(),
        )

        assert settings.dns == ("10.1.1.1", "10.1.1.2")

    @pytest.mark.parametrize("level", ["WARN", "ERROR", "DEBUG", "TRACE", "info", ""])
    def test_other_levels_are_ignored(self, service_module, level):
        before = service_module.TunnelSettings(interface="tun0")

        after = service_module.parse_tunnel_settings_line(
            record("VPN interface: tun9", level=level), before
        )

        assert after is before

    @pytest.mark.parametrize(
        "message",
        ["VPN routes: 3 included", "the VPN DNS: 10.1.1.1"],
    )
    def test_other_messages_are_ignored(self, service_module, message):
        before = service_module.TunnelSettings(interface="tun0")

        after = service_module.parse_tunnel_settings_line(record(message), before)

        assert after is before

    def test_a_newline_in_a_value_is_not_trusted(self, service_module):
        before = service_module.TunnelSettings(dns=("10.9.9.9",))

        after = service_module.parse_tunnel_settings_line(
            record("VPN DNS: 10.1.1.1\n10.6.6.6"), before
        )

        assert after.dns is None

    def test_text_prefix_still_works_for_a_text_line(self, service_module):
        settings = service_module.parse_tunnel_settings_line(
            PREFIX + "VPN MTU: 1400", service_module.TunnelSettings()
        )

        assert settings.mtu == 1400


class TestMatchersThroughRecords:
    def test_gateway_count_and_the_only_gateway(self, service_module, dbus_signals):
        plugin = _plugin(service_module)

        _run(
            plugin,
            record("Found 1 gateways in portal config", target="gpapi::portal"),
            record("Connecting to the only available gateway: gw-a (a.example.com)"),
        )

        assert plugin._gateway_count == 1
        assert plugin._gateway_list == ["gw-a (a.example.com)"]
        assert plugin._lap_entries

    def test_selected_gateway_is_not_the_whole_list(self, service_module, dbus_signals):
        plugin = _plugin(service_module)

        _run(
            plugin,
            record("Found 1 gateways in portal config"),
            record("Connecting to the selected gateway: gw-a (a.example.com)"),
        )

        assert plugin._gateway_list == ["gw-a (a.example.com)"]
        assert not plugin._lap_entries

    def test_a_gateway_name_is_stored_without_escape_sequences(
        self, service_module, dbus_signals, caplog
    ):
        plugin = _plugin(service_module)

        with caplog.at_level(logging.INFO, logger=service_module.logger.name):
            _run(
                plugin,
                record(
                    "Connecting to the selected gateway: gw\x1b[31mX\x07 (a.example.com)"
                ),
            )

        assert plugin._gateway_list == ["gwX (a.example.com)"]
        assert "\x1b" not in caplog.text and "\x07" not in caplog.text

    def test_tunnel_settings(self, service_module, dbus_signals):
        plugin = _plugin(service_module)

        _run(
            plugin,
            record("VPN interface: tun0"),
            record("VPN MTU: 1400"),
            record("VPN MTU: 9", level="DEBUG"),
        )

        assert plugin._tunnel_settings.interface == "tun0"
        assert plugin._tunnel_settings.mtu == 1400

    def test_openssl_legacy_error(self, service_module, dbus_signals):
        plugin = _plugin(service_module)

        _run(
            plugin,
            record(
                "unsafe legacy renegotiation disabled",
                level="ERROR",
                target="gpclient::cli",
            ),
        )

        assert plugin._openssl_error_seen is True

    def test_internal_network(self, service_module, dbus_signals, monkeypatch):
        plugin = _plugin(service_module)
        seen = []
        monkeypatch.setattr(plugin, "_fail_internal_network", lambda: seen.append(1))

        _run(plugin, record("Internal network — authenticated: gw (a.example.com)"))

        assert seen == [1]

    def test_as_gateway_hint(self, service_module, dbus_signals, caplog):
        plugin = _plugin(service_module)

        with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
            _run(plugin, record("try again with --as-gateway", level="ERROR"))

        assert "may be a gateway" in caplog.text

    def test_a_record_is_not_a_text_line(self, service_module, dbus_signals, monkeypatch):
        """The JSON syntax around a message never matches a matcher by itself:
        only the message does. A key named like a matcher is no match."""
        plugin = _plugin(service_module)
        seen = []
        monkeypatch.setattr(plugin, "_fail_internal_network", lambda: seen.append(1))

        _run(
            plugin,
            '{"level": "INFO", "target": "Internal network — authenticated:", '
            '"message": "hello"}',
            record("hello", target="Found 3 gateways in portal config"),
        )

        assert seen == []
        assert plugin._gateway_count is None

    def test_text_lines_take_the_old_path(self, service_module, dbus_signals):
        plugin = _plugin(service_module)

        _run(
            plugin,
            "Found 4 gateways in portal config",
            PREFIX + "VPN interface: tun0",
            "[2026-01-01T00:00:00Z ERROR gpclient::cli] --fix-openssl",
        )

        assert plugin._gateway_count == 4
        assert plugin._tunnel_settings.interface == "tun0"
        assert plugin._openssl_error_seen is True

    def test_a_prompt_line_with_matcher_text_is_still_a_text_line(
        self, service_module, dbus_signals
    ):
        plugin = _plugin(service_module)

        _run(plugin, "? Found 9 gateways in portal config")

        assert plugin._gateway_count == 9  # as before: the text is searched


class TestLogging:
    def test_a_record_is_logged_with_level_and_target(
        self, service_module, dbus_signals, caplog
    ):
        plugin = _plugin(service_module)

        with caplog.at_level(logging.INFO, logger=service_module.logger.name):
            _run(plugin, record("Gateway login succeeded", target="gpapi::login"))

        text = _log_text(caplog, service_module)
        assert "gpclient output: [INFO] gpapi::login: Gateway login succeeded" in text
        assert '"message"' not in text

    def test_a_written_secret_is_masked(self, service_module, dbus_signals, caplog):
        plugin = _plugin(service_module)
        secret = 'hunter"2\\x'
        plugin._written_secrets = {secret}

        with caplog.at_level(logging.DEBUG, logger=service_module.logger.name):
            _run(plugin, record(f"login failed for password {secret}", level="WARN"))

        assert "login failed for password <hidden>" in caplog.text
        assert secret not in caplog.text
        assert "hunter" not in caplog.text

    def test_a_secret_in_a_text_line_is_masked(self, service_module, dbus_signals, caplog):
        plugin = _plugin(service_module)
        plugin._written_secrets = {"s3cret-value"}

        with caplog.at_level(logging.DEBUG, logger=service_module.logger.name):
            _run(plugin, "token s3cret-value")

        assert "s3cret-value" not in caplog.text

    def test_a_multiline_message_is_one_log_line(
        self, service_module, dbus_signals, caplog
    ):
        plugin = _plugin(service_module)

        with caplog.at_level(logging.INFO, logger=service_module.logger.name):
            _run(plugin, record("first\nsecond"))

        assert "first\\nsecond" in caplog.text

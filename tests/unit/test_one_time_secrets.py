"""
Tests for one-time codes and for escape sequences split across reads.

Both come from the #2 report, where a stored passcode was handed back without
asking anyone (so the gateway rejected the login) and a prompt label arrived as
"[39m Password" because a colour sequence straddled a read boundary.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import os
import subprocess
import sys

import pytest

DIALOG = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-dialog", "nm-gpclient-auth-dialog.py"
    )
)
SERVICE_NAME = "org.freedesktop.NetworkManager.gpclient"


def run_dialog(hints, secrets, interaction=False):
    args = [
        sys.executable,
        DIALOG,
        "-u",
        "e5b3e5b3-0000-0000-0000-000000000000",
        "-n",
        "Work VPN",
        "-s",
        SERVICE_NAME,
    ]
    if interaction:
        args.append("-i")
    for hint in hints:
        args.extend(["-t", hint])

    lines = ["DATA_KEY=gateway", "DATA_VAL=vpn.example.com"]
    for key, value in secrets.items():
        lines.append(f"SECRET_KEY={key}")
        lines.append(f"SECRET_VAL={value}")
    lines += ["DONE", "QUIT"]

    return subprocess.run(
        args, input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=20
    )


class TestStoredOneTimeCode:
    def test_stored_passcode_is_never_handed_back(self):
        """The bug behind "worked once, then every connection failed": the
        dialog answered from the saved code, so the gateway saw a reused one."""
        result = run_dialog(
            ["x-vpn-message:Enter Your 6 Digit Passcode", "otp"],
            {"otp": "498874"},
        )

        assert result.returncode == 1
        assert "498874" not in result.stdout

    def test_stored_password_is_still_reused(self):
        result = run_dialog(["password"], {"password": "s3cret"})

        assert result.returncode == 0
        assert result.stdout == "password\ns3cret\n\n\n"


class TestOneTimeFlagsAreWrittenLate:
    """Issue #120: NetworkManager rejects every secrets request of an
    activation whose profile changed, so the profile is written only after the
    tunnel is up, the activation failed or a Disconnect ended it."""

    def _plugin(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = "e5b3e5b3-0000-0000-0000-000000000000"

        calls = []

        async def record(data, drop_secrets=()):
            calls.append((data, list(drop_secrets)))
            return True

        plugin._update_profile = record
        return plugin, calls

    ONE_WRITE = ({"otp-flags": "2"}, ["otp"])

    def _prompt(self, plugin, answers=1):
        """Run `answers` one-time prompts through _handle_prompt"""
        asked = []

        async def ask(key, label, banner, extra_hints=()):
            asked.append(key)
            return "123456"

        plugin._interactive = True
        plugin._request_secret_interactive = ask
        plugin._write_keys = lambda data, description: None
        for _ in range(answers):
            asyncio.run(plugin._handle_prompt("Enter the RSA token"))
        return asked

    def test_no_write_between_the_prompt_and_the_request(self, service_module):
        plugin, calls = self._plugin(service_module)

        asked = self._prompt(plugin)

        assert asked == ["otp"]
        assert calls == []
        assert plugin._pending_flags == {"otp-flags": "2"}

    def test_a_second_prompt_in_the_activation_writes_nothing_either(
        self, service_module
    ):
        plugin, calls = self._plugin(service_module)

        self._prompt(plugin, answers=2)

        assert calls == []
        assert plugin._pending_drop == ["otp"]  # noted once

    def test_flags_are_written_after_the_tunnel_is_up(self, service_module):
        plugin, calls = self._plugin(service_module)
        self._prompt(plugin)

        asyncio.run(plugin._persist_profile())

        assert calls == [self.ONE_WRITE]
        assert plugin._pending_flags == {}

    def test_nothing_is_written_when_the_profile_has_the_flag(self, service_module):
        plugin, calls = self._plugin(service_module)
        plugin._otp_flags_written = True  # read from the profile at Connect
        self._prompt(plugin)

        asyncio.run(plugin._persist_profile())

        assert calls == []

    def test_nothing_is_noted_without_a_uuid(self, service_module):
        plugin, calls = self._plugin(service_module)
        plugin._connection_uuid = ""
        self._prompt(plugin)

        asyncio.run(plugin._persist_profile())

        assert calls == []

    def test_nothing_is_written_without_a_one_time_prompt(self, service_module):
        plugin, calls = self._plugin(service_module)

        asyncio.run(plugin._persist_profile())

        assert calls == []

    def test_flags_are_written_after_a_failed_activation(
        self, service_module, dbus_signals
    ):
        plugin, calls = self._plugin(service_module)
        self._prompt(plugin)

        async def go():
            plugin._emit_failure(1)
            assert calls == []  # the failure is out first
            await plugin._flags_task

        asyncio.run(go())

        assert calls == [self.ONE_WRITE]

    def test_a_failure_without_pending_flags_starts_no_write(
        self, service_module, dbus_signals
    ):
        plugin, calls = self._plugin(service_module)

        async def go():
            plugin._emit_failure(1)
            assert plugin._flags_task is None

        asyncio.run(go())

        assert calls == []

    def test_teardown_waits_for_the_write_after_a_failure(
        self, service_module, dbus_signals, monkeypatch
    ):
        """Disconnect follows the failure at once and resets the UUID: it must
        not cancel the write, nor let it run with an empty UUID."""
        plugin, calls = self._plugin(service_module)
        self._prompt(plugin)

        async def noop():
            pass

        for name in ("_remember_gpauth", "_stop_gpclient", "_stop_remembered_gpauth"):
            monkeypatch.setattr(plugin, name, noop)

        async def go():
            plugin._emit_failure(1)
            await plugin._teardown()

        asyncio.run(go())

        assert calls == [self.ONE_WRITE]
        assert plugin._flags_task is None

    def test_a_failed_write_is_not_retried(self, service_module):
        plugin, calls = self._plugin(service_module)

        async def failing(data, drop_secrets=()):
            calls.append((data, list(drop_secrets)))
            return False

        plugin._update_profile = failing
        self._prompt(plugin)

        asyncio.run(plugin._persist_profile())
        asyncio.run(plugin._persist_profile())

        assert len(calls) == 1


class TestSplitEscapeSequences:
    def test_sequence_split_across_reads_does_not_leak(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        # "\x1b[39m? Password: " arriving in two reads, cut inside the sequence
        assert plugin._consume_output("\x1b[3") == []
        lines = plugin._consume_output("9m? Password: \r\n")

        assert lines == ["? Password: "]
        assert service_module.detect_prompt(lines[0]) == "Password"

    def test_label_is_clean_for_the_real_sequence_from_the_report(
        self, service_module
    ):
        plugin = service_module.GpclientVPNPlugin()

        # The report shows "?[39m Password" - the prefix and the colour code
        # arriving separately
        plugin._consume_output("? \x1b[")
        lines = plugin._consume_output("39mPassword: \r\n")

        assert lines == ["? Password: "]
        assert service_module.detect_prompt(lines[0]) == "Password"

    def test_complete_sequence_leaves_nothing_carried(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        lines = plugin._consume_output("\x1b[39m? Password: \r\n")

        assert lines == ["? Password: "]
        assert plugin._ansi_carry == ""

    def test_split_sequence_is_carried_until_completed(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("\x1b[3")
        assert plugin._ansi_carry == "\x1b[3"

        plugin._consume_output("9m")
        assert plugin._ansi_carry == ""

    def test_complete_sequence_is_not_held_back(self, service_module):
        text = "\x1b[39m? Password: "
        assert service_module.INCOMPLETE_ANSI_RE.search(text) is None

    def test_lone_escape_is_held_back(self, service_module):
        match = service_module.INCOMPLETE_ANSI_RE.search("hello\x1b")
        assert match and match.group(0) == "\x1b"


class TestSplitEscapeSequencesExtended:
    def test_osc_split_across_reads_does_not_leak(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        assert plugin._consume_output("a\x1b]0;ti") == []
        assert plugin._ansi_carry == "\x1b]0;ti"
        lines = plugin._consume_output("tle\x07b\r\n")

        assert lines == ["ab"]
        assert plugin._screen.lines()[0] == "ab"
        assert plugin._ansi_carry == ""

    def test_osc_waiting_for_its_string_terminator_is_held_back(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("a\x1b]0;title\x1b")
        lines = plugin._consume_output("\\b\r\n")

        assert lines == ["ab"]
        assert plugin._screen.lines()[0] == "ab"

    def test_csi_with_intermediate_bytes_split_across_reads(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("a\x1b[1 ")
        assert plugin._ansi_carry == "\x1b[1 "
        lines = plugin._consume_output("qb\r\n")

        assert lines == ["ab"]
        assert plugin._screen.lines()[0] == "ab"

    def test_lone_escape_split_from_its_csi(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("a\x1b")
        lines = plugin._consume_output("[31mb\r\n")

        assert lines == ["ab"]
        assert plugin._screen.lines()[0] == "ab"

    def test_overlong_unterminated_osc_is_not_held_forever(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("\x1b]0;" + "x" * 300)

        assert plugin._ansi_carry == ""

    def test_short_unterminated_osc_is_held(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        plugin._consume_output("\x1b]0;" + "x" * 100)

        assert plugin._ansi_carry != ""

    @pytest.mark.parametrize(
        "text", ["\x1b]0;title\x07", "\x1b]0;title\x1b\\", "\x1b[1 q", "\x1b[39m", "x"]
    )
    def test_complete_sequences_are_not_held_back(self, service_module, text):
        assert service_module.INCOMPLETE_ANSI_RE.search("a" + text) is None


class TestTunnelUpStopsScreenWork:
    def test_screen_is_fed_before_the_tunnel_is_up(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        version = plugin._screen.version

        plugin._consume_output("hello\r\n")

        assert plugin._screen.version != version

    def test_screen_is_not_fed_once_the_tunnel_is_up(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._tunnel_up = True
        version = plugin._screen.version

        lines = plugin._consume_output("hello\r\n")

        assert plugin._screen.version == version
        assert lines == ["hello"]

    def test_no_select_check_once_the_tunnel_is_up(self, service_module, monkeypatch):
        calls = []
        monkeypatch.setattr(
            service_module,
            "detect_select_prompt",
            lambda lines: calls.append(lines) or None,
        )
        plugin = service_module.GpclientVPNPlugin()

        plugin._schedule_prompt_check()
        assert len(calls) == 1

        plugin._tunnel_up = True
        plugin._schedule_prompt_check()
        assert len(calls) == 1


class TestUtf8SplitAcrossReads:
    def test_character_split_inside_its_bytes_stays_whole(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        data = "gw-zürich".encode("utf-8")
        cut = data.index(b"\xc3") + 1  # inside the two bytes of 'ü'

        text = plugin._decode_output(data[:cut]) + plugin._decode_output(data[cut:])

        assert text == "gw-zürich"
        assert "\ufffd" not in text

    def test_split_character_reaches_the_screen_intact(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        data = "gw-zürich\r\n".encode("utf-8")
        cut = data.index(b"\xc3") + 1

        plugin._consume_output(plugin._decode_output(data[:cut]))
        plugin._consume_output(plugin._decode_output(data[cut:]))

        assert list(plugin._screen.lines())[0] == "gw-zürich"

    @pytest.mark.parametrize(
        "data", [b"a\xffb", b"a\xc3(b", b"a\x80b", b"\xf0\x28\x8c\x28"]
    )
    def test_invalid_bytes_still_give_a_replacement_character(
        self, service_module, data
    ):
        plugin = service_module.GpclientVPNPlugin()

        text = plugin._decode_output(data)

        assert "\ufffd" in text

    def test_decoder_is_reset_for_a_new_attempt(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._decode_output(b"a\xc3")  # half of a character is pending

        plugin._reset_output_state()

        assert plugin._decode_output(b"b") == "b"


class TestNewSecretsUsername:
    """NewSecrets() takes a username with the rules of _do_connect"""

    def _new_secrets(self, service_module, username):
        plugin = service_module.GpclientVPNPlugin()
        connection = {"vpn": {"data": ("a{ss}", {"username": username}), "secrets": {}}}
        asyncio.run(plugin.NewSecrets(connection))
        return plugin

    def test_valid_username_is_stored(self, service_module):
        plugin = self._new_secrets(service_module, "jdoe@corp.example")

        assert plugin.vpn_username == "jdoe@corp.example"

    @pytest.mark.parametrize(
        "username",
        ["-x", "--script=/tmp/x", "bob\nhunter2", "a\x00b"],
        ids=["dash", "option", "newline", "nul"],
    )
    def test_unsafe_username_is_ignored_and_not_logged(
        self, service_module, caplog, username
    ):
        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            plugin = self._new_secrets(service_module, username)

        assert plugin.vpn_username == ""
        assert "ignoring an invalid username" in caplog.text
        assert "hunter2" not in caplog.text
        assert "--script" not in caplog.text

    def test_unsafe_username_keeps_the_stored_one(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin.vpn_username = "jdoe"
        connection = {"vpn": {"data": ("a{ss}", {"username": "-x"}), "secrets": {}}}

        asyncio.run(plugin.NewSecrets(connection))

        assert plugin.vpn_username == "jdoe"

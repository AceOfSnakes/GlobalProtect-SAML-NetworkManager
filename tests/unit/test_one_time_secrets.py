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


class TestForgetOneTimeSecret:
    def _plugin(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = "e5b3e5b3-0000-0000-0000-000000000000"

        calls = []

        async def record(*arguments):
            calls.append(arguments)
            return True

        plugin._nmcli_modify = record
        return plugin, calls

    def test_marks_not_saved_and_drops_the_value(self, service_module):
        plugin, calls = self._plugin(service_module)

        asyncio.run(plugin._forget_one_time_secret())

        assert calls == [
            ("+vpn.data", "otp-flags=2"),
            ("-vpn.secrets", "otp"),
        ]

    def test_only_done_once_per_connection(self, service_module):
        plugin, calls = self._plugin(service_module)

        asyncio.run(plugin._forget_one_time_secret())
        asyncio.run(plugin._forget_one_time_secret())

        assert len(calls) == 2  # from the first call only

    def test_nothing_without_a_uuid(self, service_module):
        plugin, calls = self._plugin(service_module)
        plugin._connection_uuid = ""

        asyncio.run(plugin._forget_one_time_secret())

        assert calls == []

    def test_failed_nmcli_is_retried_on_the_next_prompt(self, service_module):
        """A failed write must not count as done: the code would stay in the
        profile and the agent would hand it back without asking."""
        plugin, calls = self._plugin(service_module)
        results = [False, True, True]  # the first attempt stops after one call

        async def flaky(*arguments):
            calls.append(arguments)
            return results.pop(0)

        plugin._nmcli_modify = flaky

        asyncio.run(plugin._forget_one_time_secret())
        assert plugin._otp_flags_written is False

        asyncio.run(plugin._forget_one_time_secret())
        assert plugin._otp_flags_written is True
        assert calls[1:] == [
            ("+vpn.data", "otp-flags=2"),
            ("-vpn.secrets", "otp"),
        ]

        # Done for real now: no third round
        asyncio.run(plugin._forget_one_time_secret())
        assert len(calls) == 3

    def test_dropping_the_stored_value_failing_is_retried(self, service_module):
        plugin, calls = self._plugin(service_module)
        results = [True, False, True, True]

        async def flaky(*arguments):
            calls.append(arguments)
            return results.pop(0)

        plugin._nmcli_modify = flaky

        asyncio.run(plugin._forget_one_time_secret())
        asyncio.run(plugin._forget_one_time_secret())

        assert len(calls) == 4
        assert plugin._otp_flags_written is True

    def test_failing_flag_write_does_not_try_to_drop_the_value(self, service_module):
        """Without the not-saved flag the secret cannot be dropped for good:
        the second nmcli call is not run, but the attempt still counts."""
        plugin, calls = self._plugin(service_module)

        async def first_fails(*arguments):
            calls.append(arguments)
            return False

        plugin._nmcli_modify = first_fails

        asyncio.run(plugin._forget_one_time_secret())

        assert calls == [("+vpn.data", "otp-flags=2")]
        assert plugin._otp_flag_attempts == 1
        assert plugin._otp_flags_written is False

    def test_flag_written_but_drop_failing_runs_both_calls(self, service_module):
        plugin, calls = self._plugin(service_module)
        results = [True, False]

        async def second_fails(*arguments):
            calls.append(arguments)
            return results.pop(0)

        plugin._nmcli_modify = second_fails

        asyncio.run(plugin._forget_one_time_secret())

        assert calls == [("+vpn.data", "otp-flags=2"), ("-vpn.secrets", "otp")]
        assert plugin._otp_flag_attempts == 1
        assert plugin._otp_flags_written is False

    def test_failure_then_success_on_retry_is_done(self, service_module):
        plugin, calls = self._plugin(service_module)
        results = [False, True, True]

        async def flaky(*arguments):
            calls.append(arguments)
            return results.pop(0)

        plugin._nmcli_modify = flaky

        asyncio.run(plugin._forget_one_time_secret())
        assert plugin._otp_flags_written is False

        asyncio.run(plugin._forget_one_time_secret())
        assert plugin._otp_flags_written is True

        asyncio.run(plugin._forget_one_time_secret())
        assert len(calls) == 3  # nothing after success

    def test_permanent_failure_stops_after_two_attempts(
        self, service_module, caplog
    ):
        """Every nmcli call may wait 10 s: a profile that cannot be changed
        must not cost 20 s at every one-time prompt."""
        plugin, calls = self._plugin(service_module)

        async def failing(*arguments):
            calls.append(arguments)
            return False

        plugin._nmcli_modify = failing

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            for _ in range(5):
                asyncio.run(plugin._forget_one_time_secret())

        # Two attempts; the first call of each failed, so no second call
        assert calls == [("+vpn.data", "otp-flags=2")] * 2
        assert plugin._otp_flags_written is False
        # Said once, when giving up
        assert caplog.text.count("Giving up on marking the one-time code") == 1

    def test_first_failure_does_not_give_up_yet(self, service_module, caplog):
        plugin, calls = self._plugin(service_module)

        async def failing(*arguments):
            calls.append(arguments)
            return False

        plugin._nmcli_modify = failing

        with caplog.at_level("WARNING", logger=service_module.logger.name):
            asyncio.run(plugin._forget_one_time_secret())

        assert len(calls) == 1
        assert "Giving up" not in caplog.text

    def test_attempts_start_at_zero_for_a_new_connection(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        assert plugin._otp_flag_attempts == 0


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

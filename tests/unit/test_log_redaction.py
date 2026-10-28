"""
Tests that secrets (password, one-time codes) never reach the service log
(issue #51 item 5): --debug dumps of NM settings, and the gpclient output that
echoes an answered prompt.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import gc
import os
import pty

import pytest

PASSWORD = "Pa55-w0rd-Z9"
OTP = "498874"


def _settings(password=PASSWORD):
    return {
        "connection": {"id": ("s", "Work VPN")},
        "vpn": {
            "service-type": ("s", "org.freedesktop.NetworkManager.gpclient"),
            "data": (
                "a{ss}",
                {"gateway": "vpn.example.com", "auth-mode": "credentials"},
            ),
            "secrets": ("a{ss}", {"password": password, "otp": OTP}),
        },
    }


class TestRedactSettings:
    def test_data_is_kept_and_secrets_are_hidden(self, service_module):
        result = service_module.redact_settings(_settings())

        assert result["vpn"]["data"][1] == {
            "gateway": "vpn.example.com",
            "auth-mode": "credentials",
        }
        assert result["vpn"]["secrets"] == "<hidden>"
        assert result["connection"] == {"id": ("s", "Work VPN")}

    def test_plain_dict_without_tuples(self, service_module):
        result = service_module.redact_settings(
            {"vpn": {"data": {"a": "b"}, "secrets": {"password": PASSWORD}}}
        )

        assert result == {"vpn": {"data": {"a": "b"}, "secrets": "<hidden>"}}

    def test_secrets_key_at_any_depth(self, service_module):
        result = service_module.redact_settings(
            {"x": [("a{sv}", {"secrets": {"password": PASSWORD}})]}
        )

        assert result == {"x": [("a{sv}", {"secrets": "<hidden>"})]}

    def test_input_is_not_modified(self, service_module):
        settings = _settings()

        service_module.redact_settings(settings)

        assert settings["vpn"]["secrets"][1]["password"] == PASSWORD

    @pytest.mark.parametrize("secret", [PASSWORD, OTP])
    def test_secret_values_are_never_in_the_result(self, service_module, secret):
        settings = _settings()
        settings["vpn"]["secrets"] = ("a{ss}", {"password": secret, "otp": secret})

        assert secret not in str(service_module.redact_settings(settings)["vpn"]["secrets"])
        assert secret not in str(
            service_module.redact_settings({"vpn": {"secrets": {"otp": secret}}})
        )

    @pytest.mark.parametrize("value", [None, 5, "text", True])
    def test_scalars_pass_through(self, service_module, value):
        assert service_module.redact_settings(value) == value


class TestMaskSecrets:
    def test_value_is_replaced_everywhere_in_the_line(self, service_module):
        masked = service_module.mask_secrets(f"? Passcode: {OTP} {OTP}", {OTP})

        assert masked == "? Passcode: <hidden> <hidden>"

    @pytest.mark.parametrize("secrets", [set(), {""}, {"other"}])
    def test_line_stays_without_a_matching_non_empty_secret(
        self, service_module, secrets
    ):
        assert service_module.mask_secrets("? Username: jdoe", secrets) == (
            "? Username: jdoe"
        )


    @pytest.mark.parametrize(
        "line",
        ["? Continue: yes", "Found 1 gateways in portal config", "key=value"],
    )
    def test_short_secret_is_not_masked_inside_other_words(
        self, service_module, line
    ):
        assert service_module.mask_secrets(line, {"y", "1", "e"}) == line.replace(
            " 1 ", " <hidden> "
        )

    @pytest.mark.parametrize(
        "secret, line, masked",
        [
            ("#Hunter2", "x#Hunter2", "x<hidden>"),
            ("abc!", "abc!def", "<hidden>def"),
            ("#Hunter2", "? Pw: #Hunter2", "? Pw: <hidden>"),
        ],
    )
    def test_secret_with_a_symbol_at_the_edge_is_masked_next_to_a_word(
        self, service_module, secret, line, masked
    ):
        assert service_module.mask_secrets(line, {secret}) == masked

    @pytest.mark.parametrize(
        "secret, line",
        [("Hunter2", "xHunter2"), ("Hunter2", "Hunter2x"), ("abc!", "xabc!")],
    )
    def test_word_edge_of_a_secret_still_needs_a_word_boundary(
        self, service_module, secret, line
    ):
        assert service_module.mask_secrets(line, {secret}) == line

    def test_longest_secret_wins_when_one_contains_another(self, service_module):
        masked = service_module.mask_secrets("pw: abc abcdef", ["abc", "abcdef"])

        assert masked == "pw: <hidden> <hidden>"

    def test_the_pattern_is_built_once_for_the_same_secrets(self, service_module):
        service_module.mask_secrets("a", ["cache-check-1", "cache-check-2"])
        before = service_module._secrets_pattern.cache_info()

        service_module.mask_secrets("b", ["cache-check-2", "cache-check-1"])
        after = service_module._secrets_pattern.cache_info()

        assert (after.hits, after.misses) == (before.hits + 1, before.misses)

    @pytest.mark.parametrize("secret", ["1", "y"])
    def test_short_secret_is_masked_as_a_whole_word(self, service_module, secret):
        masked = service_module.mask_secrets(f"? Answer: {secret}", {secret})

        assert masked == "? Answer: <hidden>"


class TestMaskSecretPrompts:
    @pytest.mark.parametrize(
        "line, shown",
        [
            ("? Code: h", "? Code: <hidden>"),
            ("? Code: hunter", "? Code: <hidden>"),
            ("? Code: hunter2 extra", "? Code: <hidden>"),
            ("? Code:h", "? Code: <hidden>"),
        ],
        ids=["first-key", "partial", "whole-line", "no-blank"],
    )
    def test_everything_after_the_label_is_hidden(self, service_module, line, shown):
        assert service_module.mask_secret_prompts(line, {"Code:"}) == shown

    @pytest.mark.parametrize(
        "line, labels",
        [
            ("? Code:", {"Code:"}),
            ("? Code: ", {"Code:"}),
            ("? Username: jdoe", {"Code:"}),
            ("? Code: h", set()),
            ("? Code: h", {""}),
        ],
        ids=["label-only", "label-and-blank", "other-label", "no-labels", "empty-label"],
    )
    def test_line_stays_without_a_matching_label_or_text(
        self, service_module, line, labels
    ):
        assert service_module.mask_secret_prompts(line, labels) == line

    def test_label_without_colon_keeps_the_colon(self, service_module):
        assert service_module.mask_secret_prompts(
            "? Enter your passcode: 1", {"Enter your passcode"}
        ) == "? Enter your passcode: <hidden>"


class TestDebugLogging:
    def _plugin(self, service_module):
        return service_module.GpclientVPNPlugin()

    def test_need_secrets_does_not_log_the_password(self, service_module, caplog):
        plugin = self._plugin(service_module)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(plugin.NeedSecrets(_settings()))

        assert "Settings data" in caplog.text
        assert "vpn.example.com" in caplog.text
        assert PASSWORD not in caplog.text
        assert OTP not in caplog.text

    def test_connect_does_not_log_the_password(self, service_module, caplog):
        plugin = self._plugin(service_module)

        async def stop(*args, **kwargs):
            return False

        plugin._start_gpclient = stop
        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            with pytest.raises(Exception, match="Failed to start gpclient"):
                asyncio.run(plugin.Connect(_settings()))

        assert "Full connection data" in caplog.text
        assert PASSWORD not in caplog.text
        assert OTP not in caplog.text


class FakeProcess:
    async def wait(self):
        return 0


async def _monitor(plugin, text):
    """Run the output monitor over `text` as gpclient would print it"""
    gc.collect()  # see test_service_gaps._monitor: no earlier leak may close our fds
    master, slave = pty.openpty()
    os.write(slave, text.encode())
    os.close(slave)
    plugin._pty_master = master
    plugin.gpclient_process = FakeProcess()
    try:
        await asyncio.wait_for(plugin._monitor_gpclient_output(), timeout=10)
    finally:
        if plugin._prompt_task and not plugin._prompt_task.done():
            plugin._prompt_task.cancel()
        # Close the PTY now: left to the garbage collector, the transport
        # closes its fd number later, maybe after a later test reused it
        plugin._close_pty()


class TestEchoedAnswers:
    def _plugin(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._pty_master = os.open(os.devnull, os.O_WRONLY)
        return plugin

    def _answer(self, plugin, label, secret_value):
        async def ask(key, label, banner):
            return secret_value

        plugin._interactive = True
        plugin._request_secret_interactive = ask

        asyncio.run(plugin._handle_prompt(label))

    def test_answered_otp_is_masked_in_the_output(self, service_module, caplog):
        plugin = self._plugin(service_module)
        self._answer(plugin, "Enter your 6 digit passcode", OTP)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"? Enter your 6 digit passcode: {OTP}\r\n"))

        assert "gpclient output: ? Enter your 6 digit passcode: <hidden>" in caplog.text
        assert OTP not in caplog.text

    def test_answered_password_is_masked_in_the_output(self, service_module, caplog):
        plugin = self._plugin(service_module)
        plugin._answered_username = True
        self._answer(plugin, "Password", PASSWORD)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"? Password: {PASSWORD}\r\n"))

        assert PASSWORD not in caplog.text
        assert "? Password: <hidden>" in caplog.text

    def test_stored_password_is_masked_in_the_output(self, service_module, caplog):
        plugin = self._plugin(service_module)
        plugin._answered_username = True
        plugin.vpn_password = PASSWORD
        asyncio.run(plugin._handle_prompt("Password"))

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"? Password: {PASSWORD}\r\n"))

        assert PASSWORD not in caplog.text

    @pytest.mark.parametrize("typed", ["h", "hu", "hunter", "hunter2"])
    def test_partial_echo_of_a_secret_is_masked(self, service_module, caplog, typed):
        """inquire redraws the prompt after every keystroke"""
        plugin = self._plugin(service_module)
        self._answer(plugin, "Code:", "hunter2")

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"? Code: {typed}\r\n"))

        assert "gpclient output: ? Code: <hidden>" in caplog.text
        assert f"Code: {typed}" not in caplog.text

    def test_partial_echo_without_a_secret_answer_is_not_masked(
        self, service_module, caplog
    ):
        plugin = self._plugin(service_module)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, "? Code: hu\r\n"))

        assert "gpclient output: ? Code: hu" in caplog.text

    def test_partial_echo_of_the_stored_username_is_still_logged(
        self, service_module, caplog
    ):
        plugin = self._plugin(service_module)
        plugin.vpn_username = "jdoe"
        asyncio.run(plugin._handle_prompt("Username"))

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, "? Username: jd\r\n? Username: jdoe\r\n"))

        assert "gpclient output: ? Username: jd\n" in caplog.text
        assert "gpclient output: ? Username: jdoe" in caplog.text
        assert "<hidden>" not in caplog.text

    def test_password_equal_to_the_stored_username_is_masked(
        self, service_module, caplog
    ):
        """Only the answer to the username prompt may show the stored name"""
        plugin = self._plugin(service_module)
        plugin.vpn_username = "jdoe"
        plugin._answered_username = True
        self._answer(plugin, "Password", "jdoe")

        assert "jdoe" in plugin._written_secrets
        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, "? Password: jdoe\r\n"))

        assert "gpclient output: ? Password: <hidden>" in caplog.text

    def test_stored_password_is_masked_without_any_prompt(
        self, service_module, caplog
    ):
        plugin = self._plugin(service_module)

        async def nothing(*args, **kwargs):
            return None

        async def started(*args, **kwargs):
            return True

        plugin._start_gpclient = started
        plugin._cleanup_stale_gpd0 = nothing
        asyncio.run(
            plugin._do_connect(
                {
                    "vpn": {
                        "data": {"gateway": "vpn.example.com"},
                        "secrets": {"password": PASSWORD},
                    }
                },
                interactive=False,
            )
        )
        assert PASSWORD in plugin._written_secrets

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"error: bad {PASSWORD} here\r\n"))

        assert PASSWORD not in caplog.text
        assert "gpclient output: error: bad <hidden> here" in caplog.text

    def test_stored_username_is_still_logged(self, service_module, caplog):
        plugin = self._plugin(service_module)
        plugin.vpn_username = "jdoe"
        asyncio.run(plugin._handle_prompt("Username"))

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, "? Username: jdoe\r\n"))

        assert "gpclient output: ? Username: jdoe" in caplog.text

    @pytest.mark.parametrize("stored", ["", "other-user"], ids=["none", "different"])
    def test_typed_answer_to_a_username_prompt_is_masked(
        self, service_module, caplog, stored
    ):
        """A prompt classified as "username" (also by position, for a label we
        do not know) can ask for a secret: only the stored username is shown."""
        plugin = self._plugin(service_module)
        plugin.vpn_username = stored
        plugin._answered_username = bool(stored)  # the stored name was used already
        self._answer(plugin, "Username", PASSWORD)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"? Username: {PASSWORD}\r\n"))

        assert PASSWORD not in caplog.text
        assert "? Username: <hidden>" in caplog.text

    def test_output_without_an_answer_is_not_masked(self, service_module, caplog):
        plugin = self._plugin(service_module)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            asyncio.run(_monitor(plugin, f"code {OTP} seen\r\n"))

        assert f"gpclient output: code {OTP} seen" in caplog.text

    def test_secrets_are_forgotten_for_the_next_connection(self, service_module):
        plugin = self._plugin(service_module)
        self._answer(plugin, "Enter your 6 digit passcode", OTP)
        assert plugin._written_secrets == {OTP}

        async def stop(*args, **kwargs):
            return False

        plugin._start_gpclient = stop
        with pytest.raises(Exception, match="No gateway"):
            asyncio.run(plugin._do_connect({"vpn": {}}, interactive=False))

        assert plugin._written_secrets == set()
        assert plugin._secret_prompt_labels == set()

    def test_write_answer_never_logs_the_answer(self, service_module, caplog):
        plugin = self._plugin(service_module)

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            plugin._write_answer(OTP, secret=True)

        assert OTP not in caplog.text

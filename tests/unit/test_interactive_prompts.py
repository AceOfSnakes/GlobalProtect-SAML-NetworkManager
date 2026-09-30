"""
Unit tests for interactive prompt detection in the nm-gpclient service
(issue #6: portals with RSA token / standard login challenges).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import pytest


class TestStripAnsi:
    def test_plain_text_unchanged(self, service_module):
        assert service_module.strip_ansi("Please enter RSA token") == (
            "Please enter RSA token"
        )

    def test_csi_color_sequences_removed(self, service_module):
        raw = "\x1b[32m?\x1b[0m \x1b[1mUsername:\x1b[0m "
        assert service_module.strip_ansi(raw) == "? Username: "

    def test_cursor_and_clear_sequences_removed(self, service_module):
        raw = "\x1b[2K\x1b[1G? Password: \x1b[?25l"
        assert service_module.strip_ansi(raw) == "? Password: "

    def test_newlines_preserved(self, service_module):
        raw = "line1\r\nline2\n"
        assert service_module.strip_ansi(raw) == "line1\r\nline2\n"

    @pytest.mark.parametrize("control", ["\x00", "\x07", "\x08", "\x0b", "\x7f"])
    def test_control_characters_are_dropped(self, service_module, control):
        assert service_module.strip_ansi(f"ab{control}cd") == "abcd"

    def test_tab_survives_control_character_filter(self, service_module):
        assert service_module.strip_ansi("a\tb") == "a\tb"

    @pytest.mark.parametrize(
        "raw",
        [
            "\x1b]0;window title\x07? Username: ",  # OSC terminated by BEL
            "\x1b]0;window title\x1b\\? Username: ",  # OSC terminated by ST
        ],
    )
    def test_osc_sequences_removed(self, service_module, raw):
        assert service_module.strip_ansi(raw) == "? Username: "


class TestAuthBanner:
    def test_rsa_token_banner(self, service_module):
        # Exact line from issue #6
        banner = service_module.parse_auth_banner(
            "Please enter RSA token (Portal: vpneast.comtechtel.com)"
        )
        assert banner == {
            "message": "Please enter RSA token",
            "kind": "Portal",
            "server": "vpneast.comtechtel.com",
        }

    def test_gateway_banner(self, service_module):
        banner = service_module.parse_auth_banner(
            "Please enter the login credentials (Gateway: gw.example.com)"
        )
        assert banner["kind"] == "Gateway"
        assert banner["server"] == "gw.example.com"

    def test_non_banner_lines(self, service_module):
        assert service_module.parse_auth_banner("gpclient started: 2.5.1") is None
        assert (
            service_module.parse_auth_banner("[INFO] connecting to portal") is None
        )

    @pytest.mark.parametrize(
        "line",
        [
            "Please enter RSA token (Proxy: proxy.example.com)",  # unknown kind
            "Please enter RSA token (portal: vpn.example.com)",  # kinds are exact
            "(Portal: vpn.example.com)",  # no message
            "",
        ],
    )
    def test_banner_with_unknown_kind_or_missing_parts_is_ignored(
        self, service_module, line
    ):
        assert service_module.parse_auth_banner(line) is None


class TestDetectPrompt:
    def test_username_prompt(self, service_module):
        assert service_module.detect_prompt("? Username: ") == "Username"

    def test_password_prompt(self, service_module):
        assert service_module.detect_prompt("? Password: ") == "Password"

    def test_custom_label_from_server(self, service_module):
        assert service_module.detect_prompt("? Passcode: ") == "Passcode"

    def test_mfa_prompt_without_colon(self, service_module):
        # Gateway MFA challenges use a server-provided message with no colon
        assert (
            service_module.detect_prompt("? Enter the next tokencode ")
            == "Enter the next tokencode"
        )

    def test_regular_output_is_not_a_prompt(self, service_module):
        assert service_module.detect_prompt("Connecting to gateway...") is None
        assert service_module.detect_prompt("") is None

    def test_masked_echo_is_not_a_prompt(self, service_module):
        # While the user "types", inquire renders masked characters
        assert service_module.detect_prompt("? Password: ******") is None

    def test_finalized_text_answer_is_not_a_prompt(self, service_module):
        assert service_module.detect_prompt("? Username: jdoe") is None

    @pytest.mark.parametrize("tail", ["?", "? ", "? : ", "  ?  ", "?:"])
    def test_prompt_marker_without_a_label_is_not_a_prompt(
        self, service_module, tail
    ):
        assert service_module.detect_prompt(tail) is None

    def test_echo_of_our_answer_is_skipped(self, service_module):
        assert (
            service_module.detect_prompt(
                "? Enter the next tokencode 123456", last_answer="123456"
            )
            is None
        )


class TestClassifyPrompt:
    def test_username_labels(self, service_module):
        for label in ("Username", "User", "Login", "Email", "E-mail address"):
            assert service_module.classify_prompt(label) == "username"

    def test_secret_labels(self, service_module):
        for label in ("Password", "Passcode", "PIN", "Enter the next tokencode"):
            assert service_module.classify_prompt(label) == "password"

    @pytest.mark.parametrize(
        "label",
        [
            "Password for user jdoe",
            "Enter login password",
            "User password",
            "Login Password",
        ],
    )
    def test_password_label_with_a_username_word_is_a_password(
        self, service_module, label
    ):
        assert service_module.classify_prompt(label) == "password"

    @pytest.mark.parametrize(
        "label",
        [
            "Username (not your password)",
            "Login or email (Pass ID)",
            "Username [not the passcode]",
            "E-mail (Passwort nicht hier)",
        ],
    )
    def test_password_word_in_parentheses_does_not_make_a_password(
        self, service_module, label
    ):
        # The hint in brackets is not the thing being asked for
        assert service_module.classify_prompt(label) == "username"

    @pytest.mark.parametrize(
        "label",
        [
            "Password (your user name is not needed)",
            "Secret (login)",
            "(user) Password",
            "Enter code (email)",
        ],
    )
    def test_username_word_in_parentheses_does_not_make_a_username(
        self, service_module, label
    ):
        assert service_module.classify_prompt(label) == "password"

    @pytest.mark.parametrize(
        "label",
        [
            "Domain password",
            "Network password",
            "Passwort",
            "Passwd",
            "Passphrase",
            "Kennwort",
            "Passport ID",  # no username word: a secret, as it always was
        ],
    )
    def test_password_words_are_whole_words(self, service_module, label):
        assert service_module.classify_prompt(label) == "password"

    @pytest.mark.parametrize(
        "label",
        [
            "Username/Password",
            "Username or password",
            "Login / Password",
            "User ID or Passkey",  # "Passkey" is not a password word
            "User Passport",  # nor is "Passport"
            "Email or passphrase",
        ],
    )
    def test_username_first_with_a_password_word_further_on_is_a_username(
        self, service_module, label
    ):
        # A word that merely starts with "pass" is not the password, and a
        # username word that is not directly followed by "password" names the
        # first of two things asked for
        assert service_module.classify_prompt(label) == "username"


class TestOneTimeSecret:
    def test_rsa_banner_is_one_time(self, service_module):
        assert service_module.is_one_time_secret("Please enter RSA token")

    def test_otp_labels_are_one_time(self, service_module):
        for text in ("Passcode", "PIN", "OTP", "Enter the next tokencode"):
            assert service_module.is_one_time_secret(text)

    def test_plain_password_is_not_one_time(self, service_module):
        assert not service_module.is_one_time_secret("Password")
        assert not service_module.is_one_time_secret(
            "Please enter the login credentials"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Please enter RSA token",
            "Enter your RSA SecurID PIN",
            "Enter TAN",
            "TAN:",
            "Enter the PIN",
            "One-time OTP",
            "Enter Your 6 Digit Passcode",
            "Enter your login code",
            "Enter the next tokencode",
            "Security challenge",
            "Enter the codes",
            "otp_code",
            "TOTP",
        ],
    )
    def test_one_time_words_are_found_as_whole_words(self, service_module, text):
        assert service_module.is_one_time_secret(text)

    @pytest.mark.parametrize(
        "text",
        [
            "mTAN",
            "Enter your pushTAN",
            "chipTAN",
            "PINcode",
            "authcode",
            "Sicherheitscode",
            "Sicherheitscodes",
            "Enter the SecurToken",
            "Hardwaretoken",
            "Enter TOTP code",
        ],
    )
    def test_one_time_words_at_the_end_of_a_compound_are_found(
        self, service_module, text
    ):
        assert service_module.is_one_time_secret(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Password for Pakistan gateway",  # "tan" at the end of a name
            "Kingpin password",  # "pin" at the end of a word
            "Ursa password",  # "rsa" at the end of a word
            "Postcode password",  # "code" at the end of a word
            "Zipcode",
            "Barcodes",
            "Password for Tanaka",
            "Password for mantan",  # not a known TAN compound
        ],
    )
    def test_compounds_that_are_not_one_time_secrets_do_not_count(
        self, service_module, text
    ):
        assert not service_module.is_one_time_secret(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Password for instance db1",  # "tan" inside "instance"
            "Pinnacle password",  # "pin" at the start of "Pinnacle"
            "Enter password (encoded)",  # "code" inside "encoded"
            "Stand-alone password",  # "tan" inside "Stand"
            "Password for the Tanaka account",  # "tan" at the start of a name
            "Spinning wheel password",  # "pin" inside "Spinning"
            "Barcode password",  # "code" at the end of a word
            "Cotangent",  # "tan" inside a word
            "",
        ],
    )
    def test_one_time_words_inside_other_words_do_not_count(
        self, service_module, text
    ):
        assert not service_module.is_one_time_secret(text)


class TestOneTimeSecretSubWords:
    """Labels are split into sub-words before the keywords are looked for:
    at non-alphanumerics and underscores, at letter/digit changes, at
    lower->Upper and at ACRONYM->Word ("OTPPassword" is OTP + Password)."""

    @pytest.mark.parametrize(
        "text",
        [
            "OTPPassword",
            "PIN1",
            "Token2",
            "Enter PIN2",
            "Security code_",
            "TokenPassword",
            "PINPassword",
            "myPIN",
            "pushTAN",
            "mTANs",
            "PIN_",
            "_otp_",
            "RSA1",
            "Code2",
            "OTP-1",
            "Challenge3",
            "OTPcode",
            "SicherheitscodeNr2",
        ],
    )
    def test_keyword_sub_words_are_found(self, service_module, text):
        assert service_module.is_one_time_secret(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Pakistan1",
            "Pakistan_",
            "Kingpin2",
            "Kingpin_",
            "Ursa1",
            "Postcode_",
            "Postcode2",
            "PostCode",  # camel case must not undo the Postcode exclusion
            "ZipCode",
            "ZIPCode",
            "BarCode1",
            "Barcodes2",
            "Tanaka2",
            "mantan1",
            "Instance7",
            "Pinnacle3",
            "encoded_",
            "Stand-alone1",
            "PasswordField",
            "UserName2",
            "Password2",
        ],
    )
    def test_other_words_with_digits_or_underscores_do_not_count(
        self, service_module, text
    ):
        assert not service_module.is_one_time_secret(text)


class TestClassifyPromptKind:
    """The positional + keyword classifier used to answer prompts (issue #6).

    Covers the review findings: one-time must win over a username keyword,
    and prompt ORDER must classify localized labels the English word lists
    don't match.
    """

    def _plugin(self, service_module, prefilled=False):
        plugin = service_module.GpclientVPNPlugin()
        plugin._username_prefilled = prefilled
        plugin._reset_phase_state()
        return plugin

    def test_one_time_wins_over_username_keyword(self, service_module):
        p = self._plugin(service_module)
        # "login code" contains a username word AND a one-time word
        assert p._classify_prompt_kind("Enter your login code", "") == "otp"

    def test_first_prompt_is_username_even_if_localized(self, service_module):
        p = self._plugin(service_module)
        # German username label matches no English keyword -> positional
        assert p._classify_prompt_kind("Benutzername", "") == "username"

    def test_password_after_username(self, service_module):
        p = self._plugin(service_module)
        p._answered_username = True
        assert p._classify_prompt_kind("Passwort", "") == "password"

    def test_prompt_after_password_is_one_time(self, service_module):
        # Localized MFA prompt ("Enter the SMS code" in Polish) after the
        # password is classified one-time by position, not vocabulary
        p = self._plugin(service_module)
        p._answered_username = True
        p._answered_password = True
        assert p._classify_prompt_kind("Wprowadź kod z SMS", "") == "otp"

    def test_prefilled_username_makes_first_prompt_password(self, service_module):
        # With --user passed, gpclient does not prompt username; the first
        # prompt is the password
        p = self._plugin(service_module, prefilled=True)
        assert p._classify_prompt_kind("Password", "") == "password"

    def test_rsa_banner_marks_password_prompt_one_time(self, service_module):
        p = self._plugin(service_module)
        p._answered_username = True
        assert (
            p._classify_prompt_kind("Passcode", "Please enter RSA token") == "otp"
        )

    def test_username_keyword_with_neutral_banner_stays_username(
        self, service_module
    ):
        p = self._plugin(service_module)
        assert (
            p._classify_prompt_kind("Login", "Please enter the login credentials")
            == "username"
        )

    def test_phase_reset_forgets_answered_flags(self, service_module):
        p = self._plugin(service_module)
        p._answered_username = True
        p._answered_password = True

        p._reset_phase_state()

        assert p._answered_username is False
        assert p._answered_password is False
        # Nothing answered any more: the first prompt is the username again
        assert p._classify_prompt_kind("Benutzername", "") == "username"

    def test_phase_reset_keeps_a_prefilled_username_answered(self, service_module):
        p = self._plugin(service_module, prefilled=True)
        p._answered_password = True

        p._reset_phase_state()

        assert p._answered_username is True
        assert p._answered_password is False

    @pytest.mark.parametrize(
        "label", ["Password for user jdoe", "Enter login password"]
    )
    def test_password_label_containing_user_or_login_word_is_password(
        self, service_module, label
    ):
        p = self._plugin(service_module)
        p._answered_username = True
        assert p._classify_prompt_kind(label, "") == "password"

    def test_localized_prompt_before_password_is_not_otp(self, service_module):
        # The one-time-by-position rule only applies AFTER the password
        p = self._plugin(service_module)
        p._answered_username = True
        p._answered_password = False
        assert p._classify_prompt_kind("Wprowadź kod z SMS", "") == "password"

    def test_first_prompt_without_prefill_is_username_even_if_labelled_password(
        self, service_module
    ):
        # Documents the positional rule: the label does not matter for the
        # first prompt when no username was pre-filled
        p = self._plugin(service_module, prefilled=False)
        assert p._classify_prompt_kind("Password", "") == "username"

    def test_username_prompt_under_otp_banner_is_username(self, service_module):
        # Issue #6: the RSA banner precedes the Username prompt too
        p = self._plugin(service_module)
        assert (
            p._classify_prompt_kind("Username", "Please enter RSA token")
            == "username"
        )

    def test_localized_username_prompt_under_otp_banner_is_otp(self, service_module):
        # Restored behaviour from before the positional rule: a label with no
        # username keyword ("Benutzername") under an RSA banner counts as the
        # token prompt. The banner is only overridden by a KEYWORD username
        # label; making it positional too turned a Gateway phase's bare
        # "Password" into a username (see the Gateway phase test below).
        p = self._plugin(service_module)
        assert (
            p._classify_prompt_kind("Benutzername", "Please enter RSA token")
            == "otp"
        )

    def test_localized_username_prompt_under_neutral_banner_is_username(
        self, service_module
    ):
        # Counterpart: without a one-time banner the position still decides
        p = self._plugin(service_module)
        assert (
            p._classify_prompt_kind(
                "Benutzername", "Please enter the login credentials"
            )
            == "username"
        )

    def test_gateway_phase_password_under_rsa_banner_is_otp(self, service_module):
        # The portal phase asked username and password; the Gateway phase has
        # no stored username to pre-fill, so the state resets to "username not
        # answered" and gpclient asks only "Password" under the RSA banner.
        p = self._plugin(service_module, prefilled=False)
        p._answered_username = True
        p._answered_password = True
        p._reset_phase_state()

        assert (
            p._classify_prompt_kind("Password", "Please enter RSA token (Gateway)")
            == "otp"
        )

    def test_gateway_phase_password_under_neutral_banner_is_still_positional(
        self, service_module
    ):
        # Counterpart: no one-time banner, no username answered yet -> the
        # first prompt of the phase is taken for the username
        p = self._plugin(service_module, prefilled=False)
        p._answered_username = True
        p._answered_password = True
        p._reset_phase_state()

        assert (
            p._classify_prompt_kind("Password", "Please enter the login credentials")
            == "username"
        )

    def test_password_prompt_under_neutral_banner_is_password(self, service_module):
        p = self._plugin(service_module)
        p._answered_username = True
        assert (
            p._classify_prompt_kind("Password", "Please enter the login credentials")
            == "password"
        )

    @pytest.mark.parametrize(
        "label",
        [
            "Username (not your PIN)",
            "Login [token sent separately]",
            "User (RSA token is asked later)",
            "Email [OTP not needed here]",
        ],
    )
    def test_one_time_word_in_a_hint_does_not_make_a_username_an_otp(
        self, service_module, label
    ):
        p = self._plugin(service_module)
        assert p._classify_prompt_kind(label, "") == "username"

    @pytest.mark.parametrize(
        "label",
        [
            "Password (RSA token)",
            "Password (PIN + token code)",
            "Secret [Enter your passcode]",
            "Password (login token)",
        ],
    )
    def test_one_time_word_in_a_hint_of_a_secret_is_still_an_otp(
        self, service_module, label
    ):
        p = self._plugin(service_module)
        p._answered_username = True
        assert p._classify_prompt_kind(label, "") == "otp"

    @pytest.mark.parametrize(
        "label",
        [
            "Password (your user name is not needed)",
            "Secret (login)",
            "Password (info)",
        ],
    )
    def test_hints_without_a_one_time_word_leave_a_secret_a_password(
        self, service_module, label
    ):
        p = self._plugin(service_module)
        p._answered_username = True
        assert p._classify_prompt_kind(label, "") == "password"

    def test_username_hint_prompt_under_a_one_time_banner_is_a_username(
        self, service_module
    ):
        p = self._plugin(service_module)
        assert (
            p._classify_prompt_kind("Username (not your PIN)", "Please enter RSA token")
            == "username"
        )

    def test_one_time_word_in_the_main_part_beats_a_username_word_in_a_hint(
        self, service_module
    ):
        p = self._plugin(service_module)
        assert p._classify_prompt_kind("Passcode (login)", "") == "otp"

    def test_username_flow_under_otp_banner_end_to_end(self, service_module):
        # The whole issue #6 sequence: username, then the token as password
        p = self._plugin(service_module)
        banner = "Please enter RSA token"

        assert p._classify_prompt_kind("Username", banner) == "username"
        p._answered_username = True
        assert p._classify_prompt_kind("Password", banner) == "otp"


class TestOutputScanner:
    def test_complete_lines_and_tail(self, service_module):
        scanner = service_module.OutputScanner()
        lines = scanner.feed("line1\r\nline2\n? Password: ")
        assert lines == ["line1", "line2"]
        assert scanner.tail == "? Password: "

    def test_tail_accumulates_across_chunks(self, service_module):
        scanner = service_module.OutputScanner()
        scanner.feed("? Pass")
        lines = scanner.feed("word: ")
        assert lines == []
        assert scanner.tail == "? Password: "

    @pytest.mark.parametrize("text", ["\r\n \r\n\t\n", "\n\n\n", "\r\r"])
    def test_blank_and_whitespace_lines_are_dropped(self, service_module, text):
        scanner = service_module.OutputScanner()
        assert scanner.feed(text) == []
        assert scanner.tail == ""

    def test_unterminated_text_is_never_returned_as_a_line(self, service_module):
        scanner = service_module.OutputScanner()

        assert scanner.feed("? Password: ") == []
        assert scanner.tail == "? Password: "

        # Nothing new arrived: still no line, tail unchanged
        assert scanner.feed("") == []
        assert scanner.tail == "? Password: "

    def test_carriage_return_redraw_splits_lines(self, service_module):
        # inquire redraws the prompt line using \r
        scanner = service_module.OutputScanner()
        scanner.feed("? Username: \r? Username: j\r? Username: jd")
        assert scanner.tail == "? Username: jd"

    def test_full_rsa_flow_from_issue_6(self, service_module):
        scanner = service_module.OutputScanner()
        raw = (
            "[2026-07-16T23:25:20Z INFO  gpclient::cli] gpclient started: 2.5.1\r\n"
            "Please enter RSA token (Portal: vpneast.comtechtel.com)\r\n"
            "\x1b[32m?\x1b[0m Username: "
        )
        lines = scanner.feed(service_module.strip_ansi(raw))

        banners = [
            b
            for b in (service_module.parse_auth_banner(line) for line in lines)
            if b
        ]
        assert len(banners) == 1
        assert banners[0]["message"] == "Please enter RSA token"

        label = service_module.detect_prompt(scanner.tail)
        assert label == "Username"
        assert service_module.classify_prompt(label) == "username"

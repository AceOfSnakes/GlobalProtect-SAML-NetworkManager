"""
Unit tests for the external UI mode of the NetworkManager auth dialog.

With --external-ui-mode the dialog prints a GKeyFile ([VPN Plugin UI] plus one
group per secret) that GNOME Shell and "nmcli --ask" read. No GTK is needed.
"""

import configparser
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
DIALOG = os.path.abspath(
    os.path.join(HERE, "..", "..", "auth-dialog", "nm-gpclient-auth-dialog.py")
)
NAME_FILE = os.path.join(
    HERE, "..", "..", "plugins", "gnome", "nm-gpclient-service.name"
)
SERVICE = "org.freedesktop.NetworkManager.gpclient"
CRED = {"auth-mode": "credentials"}


def run(data=None, secrets=None, hints=(), interaction=True, reprompt=False, ext=True):
    args = [sys.executable, DIALOG, "-u", "uuid", "-n", "Work VPN", "-s", SERVICE]
    if ext:
        args.append("--external-ui-mode")
    if interaction:
        args.append("-i")
    if reprompt:
        args.append("-r")
    for hint in hints:
        args += ["-t", hint]
    lines = []
    for key, value in (data or {}).items():
        lines += [f"DATA_KEY={key}", f"DATA_VAL={value}"]
    for key, value in (secrets or {}).items():
        lines += [f"SECRET_KEY={key}", f"SECRET_VAL={value}"]
    lines += ["DONE", "QUIT"]
    return subprocess.run(
        args, input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=20
    )


def external(**kw):
    """Returns (stdout, [VPN Plugin UI] section, {secret group: section})"""
    result = run(**kw)
    assert result.returncode == 0, result.stderr
    cp = configparser.RawConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string(result.stdout)
    # nmcli requires [VPN Plugin UI] to be the first group
    assert cp.sections()[0] == "VPN Plugin UI"
    return (
        result.stdout,
        dict(cp["VPN Plugin UI"]),
        {s: dict(cp[s]) for s in cp.sections()[1:]},
    )


def test_name_file_enables_external_ui():
    assert "supports-external-ui-mode=true" in open(NAME_FILE).read().split()


def test_credentials_asks_for_password():
    _, head, groups = external(data=CRED)
    assert head["Version"] == "2"
    assert head["Title"] and head["Description"]
    assert groups == {
        "password": {
            "Value": "",
            "Label": "Password",
            "IsSecret": "true",
            "ShouldAsk": "true",
            "ForceEcho": "false",
        }
    }


@pytest.mark.parametrize("data", [{}, {"auth-mode": "saml"}])
def test_saml_asks_nothing(data):
    _, head, groups = external(data=data)
    assert head["Version"] == "2"
    assert groups == {}


def test_otp_hint_with_message():
    _, head, groups = external(hints=["x-vpn-message:Enter the RSA token", "otp"])
    assert head["Description"] == "Enter the RSA token"
    assert groups["otp"]["ShouldAsk"] == "true"
    assert groups["otp"]["IsSecret"] == "true"  # nmcli skips groups without it
    assert groups["otp"]["ForceEcho"] == "true"
    assert groups["otp"]["Label"] == "Token / one-time code"


def test_otp_is_never_prefilled():
    _, _, groups = external(secrets={"otp": "123456"}, hints=["otp"])
    assert groups["otp"]["Value"] == ""
    assert groups["otp"]["ShouldAsk"] == "true"


def test_stored_password_is_prefilled_and_not_asked():
    _, _, groups = external(
        data={**CRED, "password-flags": "1"}, secrets={"password": "s3cret"}
    )
    assert groups["password"]["Value"] == "s3cret"
    assert groups["password"]["ShouldAsk"] == "false"


def test_not_saved_password_is_always_asked():
    _, _, groups = external(
        data={**CRED, "password-flags": "2"}, secrets={"password": "s3cret"}
    )
    assert groups["password"]["ShouldAsk"] == "true"


def test_cert_pass_hint_is_a_masked_labelled_secret():
    _, _, groups = external(
        hints=["x-vpn-message:Enter the PEM passphrase", "cert-pass"]
    )
    assert groups["cert-pass"]["Label"] == "Certificate passphrase"
    assert groups["cert-pass"]["IsSecret"] == "true"
    assert groups["cert-pass"]["ForceEcho"] == "false"  # masked, unlike a username
    assert groups["cert-pass"]["ShouldAsk"] == "true"


def test_stored_cert_pass_is_prefilled_and_not_asked():
    _, _, groups = external(
        data={"cert-pass-flags": "1"}, secrets={"cert-pass": "pp"}, hints=["cert-pass"]
    )
    assert groups["cert-pass"]["Value"] == "pp"
    assert groups["cert-pass"]["ShouldAsk"] == "false"


def test_not_saved_cert_pass_is_always_asked():
    _, _, groups = external(
        data={"cert-pass-flags": "2"}, secrets={"cert-pass": "pp"}, hints=["cert-pass"]
    )
    assert groups["cert-pass"]["ShouldAsk"] == "true"


def test_password_flags_do_not_apply_to_cert_pass():
    # The password is not saved, the passphrase is: only the password is asked
    _, _, groups = external(
        data={**CRED, "password-flags": "2"},
        secrets={"password": "pw", "cert-pass": "pp"},
        hints=["password", "cert-pass"],
    )
    assert groups["password"]["ShouldAsk"] == "true"
    assert groups["cert-pass"]["ShouldAsk"] == "false"


def test_reprompt_asks_again_without_prefill():
    _, _, groups = external(data=CRED, secrets={"password": "old"}, reprompt=True)
    assert groups["password"]["Value"] == ""
    assert groups["password"]["ShouldAsk"] == "true"


def test_no_interaction_does_not_ask():
    _, _, groups = external(data=CRED, interaction=False)
    assert groups["password"]["ShouldAsk"] == "false"


def test_special_characters_use_keyfile_escaping():
    _, _, groups = external(data=CRED, secrets={"password": " a\\b"}, interaction=False)
    assert groups["password"]["Value"] == "\\sa\\\\b"  # leading space = \s


def test_a_newline_and_a_tab_are_escaped_on_their_own_line():
    stdout, _, _ = external(
        data=CRED, hints=["x-vpn-message:line1\nline2\tx", "otp"]
    )
    lines = stdout.splitlines()
    assert "Description=line1\\nline2\\tx" in lines  # one line: GKeyFile reads both back
    assert "Value=" in lines  # the otp group, never pre-filled


def test_the_username_is_named_in_the_description():
    _, ui, _ = external(data={**CRED, "username": "jdoe"})
    assert ui["Description"].endswith("\\nUser: jdoe")


# Negatives


def test_without_a_username_the_description_names_no_user():
    _, ui, _ = external(data=CRED)
    assert "User:" not in ui["Description"]


def test_a_junk_password_flags_value_counts_as_0():
    _, _, groups = external(data={**CRED, "password-flags": "junk"}, secrets={"password": "s3cret"})
    assert groups["password"]["ShouldAsk"] == "false"  # stored, and not "not saved"
    assert groups["password"]["Value"] == "s3cret"


def test_without_flag_no_keyfile_is_printed():
    result = run(data=CRED, secrets={"password": "s3cret"}, ext=False)
    assert result.stdout == "password\ns3cret\n\n\n"
    assert "[VPN Plugin UI]" not in result.stdout


def test_not_required_password_gets_no_group():
    _, _, groups = external(data={**CRED, "password-flags": "4"})
    assert groups == {}


@pytest.mark.parametrize("hint", ["", "[evil]", "a]b", "x\x01y", "  "])
def test_unusable_hint_is_ignored(hint):
    _, _, groups = external(hints=[hint, "otp"])
    assert list(groups) == ["otp"]

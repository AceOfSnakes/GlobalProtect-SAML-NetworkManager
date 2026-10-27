"""
Tests for the client certificate (issue #134): the `usercert` and `userkey` file
paths of the profile and the `cert-pass` secret.

The passphrase never goes on the command line (argv is world-readable). The
service answers the passphrase prompt of gpclient ("Enter the PEM passphrase:",
"Enter the PKCS#12 passphrase:") from the secret, and never types the VPN
password there.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio

import pytest

from test_gpclient_command import _command, _drive_connect
from test_log_redaction import _monitor

PASSPHRASE_PROMPTS = ["Enter the PEM passphrase", "Enter the PKCS#12 passphrase"]
VPN_PASSWORD = "Vp#Password1"
CERT_PASS = "Cert#Pass2"


# --- command line ------------------------------------------------------------


def test_certificate_and_key_are_single_tokens(service_module, monkeypatch, tmp_path):
    argv, _ = _command(
        service_module,
        monkeypatch,
        tmp_path,
        usercert="/etc/ssl/me.pem",
        userkey="/etc/ssl/me.key",
    )

    assert "--certificate=/etc/ssl/me.pem" in argv
    assert "--sslkey=/etc/ssl/me.key" in argv
    assert argv.index("--certificate=/etc/ssl/me.pem") < argv.index("--")


def test_no_key_option_without_a_key(service_module, monkeypatch, tmp_path):
    argv, _ = _command(service_module, monkeypatch, tmp_path, usercert="/a/me.p12")

    assert "--certificate=/a/me.p12" in argv
    assert not any(a.startswith(("--sslkey", "-k")) for a in argv)


def test_no_certificate_options_when_not_set(service_module, monkeypatch, tmp_path):
    argv, _ = _command(service_module, monkeypatch, tmp_path)

    assert not any(a.startswith(("--certificate", "--sslkey")) for a in argv)


@pytest.mark.parametrize("pass_attr", ["cert_pass", "vpn_password"])
def test_a_passphrase_is_never_on_the_command_line(
    service_module, monkeypatch, tmp_path, pass_attr
):
    argv, _ = _command(
        service_module,
        monkeypatch,
        tmp_path,
        usercert="/a/me.p12",
        **{pass_attr: CERT_PASS},
    )

    assert not any("key-password" in a or a == "-p" for a in argv)
    assert CERT_PASS not in " ".join(argv)


# --- the profile -------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"usercert": "me.pem"},
        {"usercert": "~/me.pem"},
        {"usercert": "/a/me\n.pem"},
        {"usercert": "/a/me\x00.pem"},
        {"usercert": "/a/me.pem", "userkey": "key.pem"},
        {"usercert": "/a/me.pem", "userkey": "/a/k\r.pem"},
    ],
)
def test_bad_paths_are_rejected(service_module, monkeypatch, data):
    error, starts = _drive_connect(
        service_module, monkeypatch, {"gateway": "portal.example.com", **data}
    )

    assert isinstance(error, service_module.BadArgumentsError)
    assert starts == []


def test_good_paths_are_accepted(service_module, monkeypatch):
    error, starts = _drive_connect(
        service_module,
        monkeypatch,
        {
            "gateway": "portal.example.com",
            "usercert": " /a/me.pem ",
            "userkey": "/a/my key.pem",
        },
    )

    assert error is None
    assert starts == ["portal.example.com"]


@pytest.mark.parametrize(
    "path", ["/a/me.pem", "/a/with space/me.p12", "/a/ü.pem"]
)
def test_validate_cert_path_accepts(service_module, path):
    assert service_module.validate_cert_path("usercert", path) == path


@pytest.mark.parametrize("path", ["", "me.pem", "./me.pem", "/a/\x1b.pem", "/a/\x7f"])
def test_validate_cert_path_rejects(service_module, path):
    with pytest.raises(ValueError, match="usercert must be an absolute path"):
        service_module.validate_cert_path("usercert", path)


def test_key_without_certificate_is_ignored(service_module):
    plugin = service_module.GpclientVPNPlugin()
    connection = {
        "vpn": {
            "data": {"gateway": "g.example.com", "userkey": "/a/k.pem"},
            "secrets": {"cert-pass": CERT_PASS},
        }
    }

    async def run():
        async def nothing(*args, **kwargs):
            return None

        plugin._start_gpclient = lambda: asyncio.sleep(0, True)
        plugin._cleanup_stale_gpd0 = nothing
        await plugin._do_connect(connection, interactive=False)

    asyncio.run(run())

    assert plugin.userkey == ""
    assert plugin.cert_pass == CERT_PASS
    assert CERT_PASS in plugin._written_secrets


# --- prompts -----------------------------------------------------------------


def _answers(service_module, plugin, label):
    """The keys written for the prompt `label`, and the secrets requested"""
    written, asked = [], []

    async def ask(key, label, banner):
        asked.append(key)
        return "from-user"

    plugin._interactive = True
    plugin._answered_username = True
    plugin._request_secret_interactive = ask
    plugin._write_keys = lambda data, description: written.append(data)
    asyncio.run(plugin._handle_prompt(label))
    return written, asked


@pytest.mark.parametrize("label", PASSPHRASE_PROMPTS)
def test_passphrase_prompt_is_the_certificate_passphrase(service_module, label):
    plugin = service_module.GpclientVPNPlugin()
    plugin._answered_username = True

    assert plugin._classify_prompt_kind(label, "") == "cert-pass"
    # also after the password: not an MFA prompt
    plugin._answered_password = True
    assert plugin._classify_prompt_kind(label, "") == "cert-pass"


@pytest.mark.parametrize(
    "label",
    [
        "Password",
        "Enter the passphrase",
        "Enter the PEM passphrase for the VPN",
        "Passphrase",
        "Enter the PEM password",
    ],
)
def test_other_prompts_are_not_the_certificate_passphrase(service_module, label):
    plugin = service_module.GpclientVPNPlugin()
    plugin._answered_username = True

    assert plugin._classify_prompt_kind(label, "") != "cert-pass"


@pytest.mark.parametrize("label", PASSPHRASE_PROMPTS)
def test_stored_vpn_password_is_never_typed_at_the_passphrase_prompt(
    service_module, label
):
    plugin = service_module.GpclientVPNPlugin()
    plugin.vpn_password = VPN_PASSWORD

    written, asked = _answers(service_module, plugin, label)

    assert asked == ["cert-pass"]
    assert written == [b"from-user\r"]
    assert VPN_PASSWORD.encode() not in b"".join(written)
    assert plugin._answered_password is False


def test_password_prompt_is_still_answered_from_the_password(service_module):
    plugin = service_module.GpclientVPNPlugin()
    plugin.vpn_password = VPN_PASSWORD
    plugin.cert_pass = CERT_PASS

    written, asked = _answers(service_module, plugin, "Password")

    assert asked == []
    assert written == [VPN_PASSWORD.encode() + b"\r"]


@pytest.mark.parametrize("label", PASSPHRASE_PROMPTS)
def test_stored_passphrase_answers_once_then_the_user_is_asked(service_module, label):
    plugin = service_module.GpclientVPNPlugin()
    plugin.cert_pass = CERT_PASS

    written, asked = _answers(service_module, plugin, label)
    assert written == [CERT_PASS.encode() + b"\r"]
    assert asked == []

    # gpclient asks again: the stored one was wrong
    written, asked = _answers(service_module, plugin, label)
    assert asked == ["cert-pass"]
    assert written == [b"from-user\r"]


def test_the_passphrase_is_masked_in_the_output(service_module, caplog):
    import os

    plugin = service_module.GpclientVPNPlugin()
    plugin._pty_master = os.open(os.devnull, os.O_WRONLY)
    plugin.cert_pass = CERT_PASS
    plugin._written_secrets.add(CERT_PASS)
    asyncio.run(plugin._handle_prompt("Enter the PEM passphrase"))

    with caplog.at_level("DEBUG", logger=service_module.logger.name):
        asyncio.run(_monitor(plugin, f"? Enter the PEM passphrase: {CERT_PASS}\r\n"))

    assert CERT_PASS not in caplog.text
    assert "? Enter the PEM passphrase: <hidden>" in caplog.text

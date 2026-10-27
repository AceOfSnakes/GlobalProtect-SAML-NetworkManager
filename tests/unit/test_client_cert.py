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
import os

import pytest

from test_gpclient_command import _command, _drive_connect
from test_log_redaction import _monitor

PASSPHRASE_PROMPTS = ["Enter the PEM passphrase", "Enter the PKCS#12 passphrase"]
VPN_PASSWORD = "Vp#Password1"
CERT_PASS = "Cert#Pass2"


@pytest.fixture
def me(service_module, monkeypatch):
    """The desktop user is the current user"""
    monkeypatch.setattr(
        service_module.GpclientVPNPlugin,
        "_get_real_user",
        lambda self: (os.getuid(), "me", "/home/me"),
    )
    return os.getuid()


@pytest.fixture
def cert_files(tmp_path):
    cert = tmp_path / "me.pem"
    key = tmp_path / "me.key"
    cert.write_text("cert")
    key.write_text("key")
    return str(cert), str(key)


# --- command line ------------------------------------------------------------


def test_certificate_and_key_are_single_tokens(
    service_module, monkeypatch, tmp_path, me, cert_files
):
    cert, key = cert_files
    argv, _ = _command(
        service_module, monkeypatch, tmp_path, usercert=cert, userkey=key
    )

    assert f"--certificate={cert}" in argv
    assert f"--sslkey={key}" in argv
    assert argv.index(f"--certificate={cert}") < argv.index("--")


def test_no_key_option_without_a_key(
    service_module, monkeypatch, tmp_path, me, cert_files
):
    argv, _ = _command(
        service_module, monkeypatch, tmp_path, usercert=cert_files[0]
    )

    assert f"--certificate={cert_files[0]}" in argv
    assert not any(a.startswith(("--sslkey", "-k")) for a in argv)


def test_no_certificate_options_when_not_set(service_module, monkeypatch, tmp_path):
    argv, _ = _command(service_module, monkeypatch, tmp_path)

    assert not any(a.startswith(("--certificate", "--sslkey")) for a in argv)


@pytest.mark.parametrize("pass_attr", ["cert_pass", "vpn_password"])
def test_neither_the_passphrase_nor_the_vpn_password_is_on_the_command_line(
    service_module, monkeypatch, tmp_path, me, cert_files, pass_attr
):
    argv, _ = _command(
        service_module,
        monkeypatch,
        tmp_path,
        usercert=cert_files[0],
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


# --- files read by root (gpclient) ------------------------------------------


def _started(service_module, monkeypatch, tmp_path, **settings):
    """Whether _start_gpclient spawned gpclient (the fake writes argv.json)"""
    try:
        _command(service_module, monkeypatch, tmp_path, **settings)
    except (AssertionError, FileNotFoundError):
        return False
    return True


def test_a_file_of_the_desktop_user_is_accepted(service_module, cert_files):
    service_module.check_cert_file("usercert", cert_files[0], os.getuid())


def test_a_bad_file_is_not_passed_to_gpclient(
    service_module, monkeypatch, tmp_path, me, cert_files
):
    # Positive twin: the same file of the desktop user starts gpclient
    assert _started(service_module, monkeypatch, tmp_path, usercert=cert_files[0])
    other = tmp_path / "other"
    other.mkdir()
    assert not _started(service_module, monkeypatch, other, usercert=str(other))


def _fifo(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    return str(path)


def _big(tmp_path):
    path = tmp_path / "big.pem"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    return str(path)


def _file_of_another_owner(tmp_path):
    link = tmp_path / "link.pem"
    link.write_text("x")
    return str(link)


@pytest.mark.parametrize(
    "make, uid_shift, word",
    [
        (lambda t: str(t), 0, "not a regular file"),  # a directory
        (_fifo, 0, "not a regular file"),
        (lambda t: "/dev/zero", 0, "not a regular file"),
        (_big, 0, "larger than"),
        (_file_of_another_owner, 1, "not owned"),  # another owner than the desktop user
        (lambda t: str(t / "missing"), 0, "cannot read"),
    ],
)
def test_check_cert_file_rejects(service_module, tmp_path, make, uid_shift, word):
    path = make(tmp_path)

    with pytest.raises(ValueError, match=word):
        service_module.check_cert_file("usercert", path, os.getuid() + uid_shift)


def test_a_symlink_to_a_file_of_another_user_is_rejected(service_module, tmp_path):
    target = tmp_path / "t.pem"
    target.write_text("x")
    link = tmp_path / "l.pem"
    link.symlink_to(target)

    service_module.check_cert_file("usercert", str(link), os.getuid())  # own target
    with pytest.raises(ValueError, match="not owned"):
        service_module.check_cert_file("usercert", str(link), os.getuid() + 1)


# --- a wrong passphrase must not loop ---------------------------------------


def test_an_agent_that_returns_the_same_passphrase_fails_the_login(service_module):
    plugin = service_module.GpclientVPNPlugin()
    plugin.cert_pass = CERT_PASS
    plugin._interactive = True
    plugin._answered_username = True
    asked, written, failures = [], [], []

    async def agent(key, label, banner):
        asked.append(key)
        return CERT_PASS  # the agent hands back the stored, wrong value

    plugin._request_secret_interactive = agent
    plugin._write_keys = lambda data, description: written.append(data)
    plugin._fail_login = failures.append

    async def run():
        for _ in range(10):  # gpclient asks again after every wrong passphrase
            before = len(written)
            await plugin._handle_prompt("Enter the PEM passphrase")
            if len(written) == before:
                break

    asyncio.run(run())

    assert failures == ["wrong certificate passphrase"]
    assert len(written) <= 1  # the stored value once, never again


def test_different_passphrases_are_asked_at_most_three_times(service_module):
    plugin = service_module.GpclientVPNPlugin()
    plugin._interactive = True
    plugin._answered_username = True
    answers = iter(["a1", "a2", "a3", "a4", "a5"])
    written, failures = [], []

    async def agent(key, label, banner):
        return next(answers)

    plugin._request_secret_interactive = agent
    plugin._write_keys = lambda data, description: written.append(data)
    plugin._fail_login = failures.append

    async def run():
        for _ in range(5):
            await plugin._handle_prompt("Enter the PEM passphrase")

    asyncio.run(run())

    assert len(written) == 3
    assert failures and set(failures) == {"wrong certificate passphrase"}


# --- the lone value of an agent ----------------------------------------------


def _ask(service_module, secret_key, returned):
    plugin = service_module.GpclientVPNPlugin()
    plugin._interactive = True

    async def run():
        async def deliver():
            await asyncio.sleep(0)
            plugin._secret_future.set_result(returned)

        asyncio.get_running_loop().create_task(deliver())
        return await plugin._request_secret_interactive(secret_key, "label", "")

    return asyncio.run(run())


@pytest.mark.parametrize(
    "asked, returned_key",
    [
        ("cert-pass", "password"),
        ("password", "cert-pass"),
        ("otp", "password"),
        ("otp", "cert-pass"),
        ("cert-pass", "otp"),
    ],
)
def test_another_known_secret_is_never_taken_for_the_one_asked(
    service_module, asked, returned_key
):
    with pytest.raises(Exception, match="did not provide"):
        _ask(service_module, asked, {returned_key: VPN_PASSWORD})


@pytest.mark.parametrize("asked", ["cert-pass", "password", "otp"])
def test_a_lone_value_under_an_unknown_key_is_still_taken(service_module, asked):
    assert _ask(service_module, asked, {"legacy": "v"}) == "v"
    assert _ask(service_module, asked, {asked: "v"}) == "v"


# --- the screen text of the prompt -------------------------------------------


@pytest.mark.parametrize("label", PASSPHRASE_PROMPTS)
def test_the_screen_text_of_the_prompt_is_the_certificate_passphrase(
    service_module, label
):
    row = f"? {label}: "

    assert service_module.detect_prompt(row) == label
    assert service_module.detect_screen_prompt([row], 0) == (0, label)
    plugin = service_module.GpclientVPNPlugin()
    plugin._answered_username = True
    assert plugin._classify_prompt_kind(label, "") == "cert-pass"

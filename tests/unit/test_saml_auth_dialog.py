"""
Tests of saml-auth=dialog in the auth dialog (auth-dialog/nm-gpclient-auth-dialog.py).

With the hint "x-gp-saml:<JSON>" the dialog runs gpauth in the user's session
and returns its result line as the secret "gpauth-result". gpauth is a fake
here: a script that records its arguments and plays one outcome.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import configparser
import json
import os
import signal
import subprocess
import sys
import time

import pytest

DIALOG = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-dialog", "nm-gpclient-auth-dialog.py"
    )
)
SERVICE = "org.freedesktop.NetworkManager.gpclient"
SUCCESS = (
    '{"success":{"username":"alice","preloginCookie":"abcdef123456",'
    '"portalUserauthcookie":null,"token":null,"hostId":"h1"}}'
)

# Plays the outcome named by FAKE_MODE and records argv and GP_BROWSER
FAKE_GPAUTH = r"""
import json, os, sys, time
with open(os.environ["FAKE_LOG"], "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "gp_browser": os.environ.get("GP_BROWSER"),
                          "pid": os.getpid()}) + "\n")
mode = os.environ["FAKE_MODE"]
if mode == "legacy" and "--fix-openssl" not in sys.argv:
    sys.stderr.write('{"level":"ERROR","message":"The server requires unsafe legacy TLS renegotiation: x"}\n')
    sys.exit(1)
if mode == "legacy-always":
    sys.stderr.write("error:0A000152:SSL routines:final_renegotiate:unsafe legacy renegotiation disabled\n")
    sys.exit(1)
if mode in ("success", "legacy"):
    print(os.environ["FAKE_SUCCESS"])
elif mode == "cancelled":
    print('"cancelled"')
elif mode == "failure":
    print('{"failure":"nope"}')
elif mode == "tls":
    print('{"serverTlsError":{"error":"x"}}')
elif mode == "nonsaml":
    sys.stderr.write("Error: Received non-SAML prelogin response\n")
    sys.exit(1)
elif mode == "broken":
    sys.stderr.write("Error: something else\n")
    sys.exit(1)
elif mode == "silent":
    pass
elif mode == "garbage":
    print("not json")
elif mode == "hang":
    time.sleep(60)
"""

# Runs the dialog with GPAUTH_BINARY pointing to the fake
RUNNER = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('dialog', sys.argv.pop(1))\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "module.GPAUTH_BINARY = sys.argv.pop(1)\n"
    "sys.exit(module.main())\n"
)

SAML = {
    "server": "vpn.example.com",
    "gateway": False,
    "fix_openssl": "auto",
    "browser": "/usr/libexec/gpclient/browser-wrapper",
    "browser_target": "edge",
}


def hint(**changes):
    return "x-gp-saml:" + json.dumps({**SAML, **changes})


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "gpauth.py"
    script.write_text(FAKE_GPAUTH)
    shell = tmp_path / "gpauth"
    shell.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
    shell.chmod(0o755)
    log = tmp_path / "gpauth.log"

    class Fake:
        path = str(shell)

        @staticmethod
        def runs():
            return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    Fake.log = log
    return Fake


def start(fake, mode, hints, ext=False, interaction=True, extra_env=None):
    env = dict(
        os.environ,
        FAKE_MODE=mode,
        FAKE_LOG=str(fake.log),
        FAKE_SUCCESS=SUCCESS,
        **(extra_env or {}),
    )
    env.pop("GP_BROWSER", None)
    args = [sys.executable, "-c", RUNNER, DIALOG, fake.path]
    args += ["-u", "uuid", "-n", "Work VPN", "-s", SERVICE]
    if ext:
        args.append("--external-ui-mode")
    if interaction:
        args.append("-i")
    for item in hints:
        args += ["-t", item]
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    proc.stdin.write("DONE\n")  # stdin stays open: the end of it stops gpauth
    proc.stdin.flush()
    return proc


def finish(proc):
    """Wait for the exit; (status, stdout, stderr). Closes stdin only after."""
    status = proc.wait(timeout=30)
    out, err = proc.stdout.read(), proc.stderr.read()
    proc.stdin.close()
    return status, out, err


def gtk_answer(fake, mode, hints, **kw):
    """The GTK path: the answer pairs on stdout, then QUIT ends the dialog"""
    proc = start(fake, mode, hints, **kw)
    key = proc.stdout.readline().rstrip("\n")
    value = proc.stdout.readline().rstrip("\n")
    proc.stdin.write("QUIT\n")
    proc.stdin.flush()
    status, _, err = finish(proc)
    return status, key, value, err


def secret_hints(**changes):
    return ["x-vpn-message:Sign in to vpn.example.com", hint(**changes), "gpauth-result"]


class TestSuccess:
    def test_the_gtk_path_returns_the_line_without_a_window(self, fake):
        status, key, value, _ = gtk_answer(fake, "success", secret_hints())

        assert (status, key, value) == (0, "gpauth-result", SUCCESS)
        (run,) = fake.runs()
        assert run["argv"] == [
            "--log-format",
            "json",
            "--browser=/usr/libexec/gpclient/browser-wrapper",
            "--",
            "vpn.example.com",
        ]
        assert run["gp_browser"] == "edge"

    def test_the_external_ui_asks_nobody(self, fake):
        status, out, _ = finish(start(fake, "success", secret_hints(), ext=True))

        assert status == 0
        cp = configparser.RawConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read_string(out)
        assert cp.sections() == ["VPN Plugin UI", "gpauth-result"]
        group = cp["gpauth-result"]
        assert group["Value"] == SUCCESS
        assert group["ShouldAsk"] == "false"
        assert group["IsSecret"] == "true"

    def test_the_options_follow_the_request(self, fake):
        gtk_answer(
            fake, "success",
            secret_hints(gateway=True, fix_openssl="true", browser_target=None,
                         browser="/opt/my-wrapper"),
        )

        (run,) = fake.runs()
        assert run["argv"] == [
            "--log-format", "json", "--gateway", "--fix-openssl",
            "--browser=/opt/my-wrapper", "--", "vpn.example.com",
        ]
        assert run["gp_browser"] is None  # nothing from the caller's environment

    def test_a_browser_from_the_environment_is_replaced(self, fake):
        gtk_answer(fake, "success", secret_hints(browser_target="firefox"))

        assert fake.runs()[0]["gp_browser"] == "firefox"


class TestNoAnswer:
    @pytest.mark.parametrize(
        "mode", ["cancelled", "failure", "tls", "broken", "silent", "garbage"]
    )
    @pytest.mark.parametrize("ext", [False, True])
    def test_a_login_that_did_not_succeed_exits_with_1(self, fake, mode, ext):
        status, out, err = finish(start(fake, mode, secret_hints(), ext=ext))

        assert status == 1
        assert out == ""  # no secret: NetworkManager sees a cancel
        assert fake.runs()

    def test_no_interaction_means_no_gpauth(self, fake):
        status, out, err = finish(start(fake, "success", secret_hints(), interaction=False))

        assert status == 1
        assert out == ""
        assert fake.runs() == []

    def test_a_missing_gpauth_exits_with_1(self, fake, tmp_path):
        fake.path = str(tmp_path / "missing")
        proc = start(fake, "success", secret_hints())

        status, out, err = finish(proc)

        assert status == 1
        assert "Cannot run" in err
        assert "Traceback" not in err
        assert out == ""


class TestNonSamlPortal:
    @pytest.mark.parametrize("ext", [False, True])
    def test_standard_is_the_answer(self, fake, ext):
        if ext:
            status, out, _ = finish(start(fake, "nonsaml", secret_hints(), ext=True))
            assert status == 0
            assert "Value=standard" in out
            assert "ShouldAsk=false" in out
        else:
            status, key, value, _ = gtk_answer(fake, "nonsaml", secret_hints())
            assert (status, key, value) == (0, "gpauth-result", "standard")

    def test_another_error_is_not_standard(self, fake):
        status, out, _ = finish(start(fake, "broken", secret_hints()))

        assert status == 1
        assert "standard" not in out


class TestLegacyTls:
    def test_auto_retries_once_with_the_flag(self, fake):
        status, key, value, _ = gtk_answer(fake, "legacy", secret_hints())

        assert (status, value) == (0, SUCCESS)
        first, second = fake.runs()
        assert "--fix-openssl" not in first["argv"]
        assert "--fix-openssl" in second["argv"]

    @pytest.mark.parametrize("mode", ["false", "true"])
    def test_other_modes_do_not_retry(self, fake, mode):
        status, out, _ = finish(
            start(fake, "legacy-always", secret_hints(fix_openssl=mode))
        )

        assert status == 1
        assert len(fake.runs()) == 1

    def test_the_retry_happens_only_once(self, fake):
        status, out, _ = finish(start(fake, "legacy-always", secret_hints()))

        assert status == 1
        assert len(fake.runs()) == 2


class TestRequestIsChecked:
    @pytest.mark.parametrize(
        "text",
        [
            "x-gp-saml:not json",
            "x-gp-saml:[]",
            "x-gp-saml:" + json.dumps({**SAML, "server": "-oProxyCommand=x"}),
            "x-gp-saml:" + json.dumps({**SAML, "server": "a b"}),
            "x-gp-saml:" + json.dumps({**SAML, "server": "a\nb"}),
            "x-gp-saml:" + json.dumps({**SAML, "server": ""}),
            "x-gp-saml:" + json.dumps({**SAML, "server": 5}),
            "x-gp-saml:" + json.dumps({**SAML, "gateway": "yes"}),
            "x-gp-saml:" + json.dumps({**SAML, "fix_openssl": "maybe"}),
            "x-gp-saml:" + json.dumps({**SAML, "browser": "a b"}),
            "x-gp-saml:" + json.dumps({**SAML, "browser": "-x"}),
            "x-gp-saml:" + json.dumps({**SAML, "browser": None}),
            "x-gp-saml:" + json.dumps({**SAML, "browser_target": 5}),
            "x-gp-saml:" + json.dumps({**SAML, "browser_target": "a;b"}),
        ],
    )
    def test_a_bad_request_runs_nothing(self, fake, text):
        status, out, err = finish(start(fake, "success", [text, "gpauth-result"]))

        assert status == 1
        assert out == ""
        assert "Invalid request" in err
        assert fake.runs() == []

    def test_a_good_request_runs_gpauth(self, fake):
        status, _, value, _ = gtk_answer(fake, "success", secret_hints())

        assert (status, value) == (0, SUCCESS)
        assert len(fake.runs()) == 1


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_gpauth(fake):
    for _ in range(200):
        if fake.runs():
            return fake.runs()[0]["pid"]
        time.sleep(0.05)
    raise AssertionError("gpauth did not start")


def wait_until_dead(pid):
    for _ in range(100):
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestStopsGpauth:
    def test_sigterm_stops_gpauth(self, fake):
        proc = start(fake, "hang", secret_hints())
        pid = wait_for_gpauth(fake)

        proc.send_signal(signal.SIGTERM)
        status, out, _ = finish(proc)

        assert status == 1
        assert out == ""
        assert wait_until_dead(pid)

    def test_the_end_of_stdin_stops_gpauth(self, fake):
        proc = start(fake, "hang", secret_hints())
        pid = wait_for_gpauth(fake)

        proc.stdin.close()
        status = proc.wait(timeout=30)

        assert status == 1
        assert wait_until_dead(pid)

    def test_gpauth_keeps_running_while_the_agent_waits(self, fake):
        proc = start(fake, "hang", secret_hints())
        pid = wait_for_gpauth(fake)
        time.sleep(1)

        try:
            assert proc.poll() is None
            assert alive(pid)
        finally:
            proc.send_signal(signal.SIGTERM)
            finish(proc)

"""
Tests of the service side of saml-auth=dialog (issue #125).

The auth dialog runs gpauth as the desktop user and sends its JSON line as the
secret "gpauth-result"; the service checks it (parse_auth_result) and gives it
to `gpclient connect --cookie-on-stdin` on the PTY. With saml-auth=service
(the default) nothing changes.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json
import logging
import sys

import pytest

COOKIE = "abcdef123456"
GOOD = {
    "success": {
        "username": "alice",
        "preloginCookie": COOKIE,
        "portalUserauthcookie": None,
        "token": None,
        "hostId": "host-1",
    }
}


def line(**changes):
    """A gpauth result; a value of None for a key given here removes the key"""
    data = {**GOOD["success"], **changes}
    return json.dumps({"success": data}, separators=(",", ":"))


class TestParseAuthResult:
    def test_a_success_is_returned_as_one_compact_line(self, service_module):
        text = json.dumps(GOOD, indent=2)  # whitespace, newlines

        result = service_module.parse_auth_result(text + "\n")

        assert result == json.dumps(GOOD, separators=(",", ":"))
        assert "\n" not in result

    @pytest.mark.parametrize(
        "data",
        [
            {"username": "a", "portalUserauthcookie": "123456"},
            {"username": "a", "preloginCookie": "123456", "token": "t"},
            {"username": "a", "preloginCookie": "123456"},
        ],
    )
    def test_the_fields_upstream_sends_are_accepted(self, service_module, data):
        text = json.dumps({"success": data})

        assert json.loads(service_module.parse_auth_result(text)) == {"success": data}

    def test_a_non_ascii_name_stays_on_one_line(self, service_module):
        result = service_module.parse_auth_result(line(username="zoë x"))

        assert " " not in result
        assert json.loads(result)["success"]["username"] == "zoë x"

    @pytest.mark.parametrize(
        "text",
        [
            '"cancelled"',
            '{"cancelled":null}',
            '{"failure":"bad"}',
            '{"serverTlsError":{"error":"x"}}',
            "",
            "not json",
            "[]",
            "null",
            '{"success":"x"}',
            '{"success":null}',
            '{"success":{"username":"a","preloginCookie":"123456"},"failure":"x"}',
            '{"success":{"username":"a","preloginCookie":"123456","extra":"x"}}',
            # upstream reads "un" as an alias of username; we never pass it on
            '{"success":{"un":"a","preloginCookie":"123456"}}',
            "[" * 100000,
        ],
    )
    def test_anything_but_a_plain_success_is_rejected(self, service_module, text):
        with pytest.raises(ValueError):
            service_module.parse_auth_result(text)

    @pytest.mark.parametrize(
        "changes",
        [
            {"username": "a\nb"},
            {"username": "a\x00"},
            {"username": "-x"},
            {"username": ""},
            {"username": None},
            {"username": 5},
            {"preloginCookie": COOKIE + "\n{}"},
            {"preloginCookie": COOKIE + "\r"},
            {"preloginCookie": 123456789},
            {"preloginCookie": ["a"]},
            {"hostId": "h\x7f"},
            {"hostId": 1},
            {"preloginCookie": "12345"},  # short cookie
            {"preloginCookie": None},  # no cookie at all
            {"preloginCookie": None, "token": "tokentokentoken"},
        ],
    )
    def test_bad_values_are_rejected(self, service_module, changes):
        with pytest.raises(ValueError):
            service_module.parse_auth_result(line(**changes))

    def test_a_result_over_16_kib_is_rejected(self, service_module):
        with pytest.raises(ValueError, match="too long"):
            service_module.parse_auth_result(line(token="x" * 16384))

    def test_a_result_just_under_the_limit_is_accepted(self, service_module):
        text = line(token="x" * 16000)

        assert service_module.parse_auth_result(text) == text


# Reports what gpclient sees: argv, the environment, the terminal modes and
# the first stdin line (read the way gpclient reads it)
FAKE_GPCLIENT = r"""
import json, os, sys, termios
lflag = termios.tcgetattr(0)[3]
stdin_line = sys.stdin.readline() if "--cookie-on-stdin" in sys.argv else None
with open("__LOG__", "a") as out:
    out.write(json.dumps({
        "argv": sys.argv[1:],
        "env": {k: os.environ.get(k) for k in ("SUDO_UID", "SUDO_USER", "GP_BROWSER")},
        "echo": bool(lflag & termios.ECHO),
        "canonical": bool(lflag & termios.ICANON),
        "stdin": stdin_line,
    }) + "\n")
__AFTER__
"""


@pytest.fixture
def launcher(service_module, monkeypatch, tmp_path):
    """A plugin whose gpclient is a fake that logs one launch per start"""
    log = tmp_path / "launches.jsonl"

    def make(after="sys.exit(0)", **settings):
        script = tmp_path / "gpclient.py"
        script.write_text(
            FAKE_GPCLIENT.replace("__LOG__", str(log)).replace("__AFTER__", after)
        )
        shell = tmp_path / "gpclient.sh"
        shell.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
        shell.chmod(0o755)
        monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(shell))
        plugin = service_module.GpclientVPNPlugin()
        plugin.gateway = "portal.example.com"
        plugin.browser = "/usr/libexec/gpclient/browser-wrapper"
        plugin.browser_target = "edge"
        plugin._get_real_user = lambda: (4321, "alice", "/home/alice")
        for name, value in settings.items():
            setattr(plugin, name, value)
        return plugin

    def launches():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    make.launches = launches
    return make


def run_to_the_end(plugin):
    async def go():
        assert await plugin._start_gpclient()
        while True:
            task = plugin.stdout_monitor_task
            await asyncio.wait_for(asyncio.shield(task), 30)
            if plugin.stdout_monitor_task is task:  # a restart replaces it
                return

    asyncio.run(go())


class TestGpclientCommand:
    def test_the_result_goes_to_stdin_and_there_is_no_browser(
        self, launcher, caplog
    ):
        result = json.dumps(GOOD, separators=(",", ":"))
        plugin = launcher(
            _auth_result=result, as_gateway=True, hip_enabled=True, fix_openssl=True
        )

        with caplog.at_level(logging.DEBUG):
            run_to_the_end(plugin)

        (launch,) = launcher.launches()
        argv = launch["argv"]
        assert argv == [
            "--log-format",
            "json",
            "--fix-openssl",
            "connect",
            "--hip",
            "--as-gateway",
            "--cookie-on-stdin",
            "--",
            "portal.example.com",
        ]
        assert launch["stdin"] == result + "\n"
        # gpclient would start gpauth as root otherwise, and the wrapper with it
        assert launch["env"] == {"SUDO_UID": None, "SUDO_USER": None, "GP_BROWSER": None}
        assert result not in caplog.text
        assert COOKIE not in caplog.text

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            (
                {"_auth_result": json.dumps(GOOD, separators=(",", ":"))},
                {"SUDO_UID": None, "SUDO_USER": None, "GP_BROWSER": None},
            ),
            # service mode: the service sets its own values over inherited ones
            ({}, {"SUDO_UID": "4321", "SUDO_USER": "alice", "GP_BROWSER": "edge"}),
        ],
    )
    def test_inherited_sudo_and_browser_variables(
        self, launcher, monkeypatch, settings, expected
    ):
        monkeypatch.setenv("SUDO_UID", "0")
        monkeypatch.setenv("SUDO_USER", "root")
        monkeypatch.setenv("GP_BROWSER", "inherited")

        run_to_the_end(launcher(**settings))

        (launch,) = launcher.launches()
        assert launch["env"] == expected

    def test_the_terminal_neither_echoes_nor_buffers_lines(self, launcher):
        plugin = launcher(_auth_result=json.dumps(GOOD, separators=(",", ":")))

        run_to_the_end(plugin)

        (launch,) = launcher.launches()
        assert launch["echo"] is False
        assert launch["canonical"] is False

    def test_a_long_line_arrives_whole(self, launcher):
        result = line(token="t" * 12000)
        plugin = launcher(_auth_result=result)

        run_to_the_end(plugin)

        (launch,) = launcher.launches()
        assert launch["stdin"] == result + "\n"

    def test_the_line_is_written_again_for_a_restart(self, launcher):
        result = json.dumps(GOOD, separators=(",", ":"))
        plugin = launcher(
            after=(
                "sys.stdout.write(json.dumps({'timestamp': 't', 'level': 'ERROR', "
                "'target': 'x', 'message': 'unsafe legacy renegotiation disabled'}) "
                "+ '\\r\\n'); sys.stdout.flush(); sys.exit(1)"
            ),
            _auth_result=result,
        )

        run_to_the_end(plugin)

        first, second = launcher.launches()
        assert "--fix-openssl" not in first["argv"]
        assert "--fix-openssl" in second["argv"]
        assert first["stdin"] == second["stdin"] == result + "\n"

    def test_the_old_command_without_a_result(self, launcher):
        plugin = launcher(_auth_result="")

        run_to_the_end(plugin)

        (launch,) = launcher.launches()
        assert launch["argv"] == [
            "--log-format",
            "json",
            "connect",
            "--hip",
            "--browser=/usr/libexec/gpclient/browser-wrapper",
            "--",
            "portal.example.com",
        ]
        assert launch["stdin"] is None
        assert launch["env"] == {
            "SUDO_UID": "4321",
            "SUDO_USER": "alice",
            "GP_BROWSER": "edge",
        }
        assert launch["echo"] is True  # the terminal is left alone


def _connection(**data):
    return {
        "connection": {"uuid": "e5b3e5b3-0000-0000-0000-000000000000"},
        "vpn": {"data": {"gateway": "vpn.example.com", **data}},
    }


@pytest.fixture
def session(service_module, monkeypatch, dbus_signals):
    """A plugin set up through _setup_session, with the system parts replaced.

    `asked` collects the secret requests; `started` the gpclient starts. The
    reply to the request is set by `reply`."""
    plugin = service_module.GpclientVPNPlugin()
    plugin.asked = []
    plugin.started = []
    plugin.profile_writes = []
    plugin.reply = line()

    async def noop():
        pass

    async def start():
        plugin.started.append(plugin._auth_result)
        return True

    real_request = plugin._request_secret_interactive
    plugin.real_request = False

    async def request(key, label, banner, extra_hints=()):
        if plugin.real_request:
            return await real_request(key, label, banner, extra_hints)
        plugin.asked.append((key, label, banner, list(extra_hints)))
        return plugin.reply

    async def update(data, drop_secrets=()):
        plugin.profile_writes.append((data, list(drop_secrets)))
        return True

    monkeypatch.setattr(plugin, "_cleanup_stale_gpd0", noop)
    monkeypatch.setattr(plugin, "_start_gpclient", start)
    monkeypatch.setattr(plugin, "_request_secret_interactive", request)
    monkeypatch.setattr(plugin, "_update_profile", update)
    monkeypatch.setattr(plugin, "_stop_gpclient", noop)
    monkeypatch.setattr(plugin, "_remember_gpauth", noop)
    monkeypatch.setattr(plugin, "_stop_remembered_gpauth", noop)
    monkeypatch.setattr(plugin, "_close_pty", lambda: None)
    monkeypatch.setattr(plugin, "_get_real_user", lambda: (4321, "alice", "/home/alice"))
    return plugin


def connect(plugin, data, interactive=True):
    async def go():
        await plugin._do_connect(_connection(**data), interactive=interactive)
        if plugin._auth_task:
            await plugin._auth_task

    asyncio.run(go())


def failures(dbus_signals):
    return [s for s in dbus_signals if s[0] == "Failure"]


class TestSetupSession:
    def test_the_dialog_is_asked_before_gpclient_starts(
        self, session, dbus_signals, service_module, monkeypatch
    ):
        monkeypatch.setattr(service_module, "BROWSER_WRAPPER", sys.executable)
        connect(
            session,
            {"saml-auth": "dialog", "as-gateway": "true", "browser": "firefox"},
        )

        ((key, label, banner, hints),) = session.asked
        assert key == "gpauth-result"
        assert label == "Sign in to vpn.example.com"
        (hint,) = hints
        assert hint.startswith("x-gp-saml:")
        assert json.loads(hint[len("x-gp-saml:") :]) == {
            "server": "vpn.example.com",
            "gateway": True,
            "fix_openssl": "auto",
            "browser": sys.executable,
            "browser_target": "firefox",
        }
        # Issue #120: no profile write while the activation can still ask
        assert session.profile_writes == []
        assert session._pending_flags == {"gpauth-result-flags": "2"}
        assert session._pending_drop == ["gpauth-result"]
        assert session.started == [json.dumps(GOOD, separators=(",", ":"))]
        assert failures(dbus_signals) == []

    def test_the_service_mode_asks_nobody(self, session):
        connect(session, {})

        assert session.asked == []
        assert session.profile_writes == []
        assert session.started == [""]
        assert session._auth_task is None

    @pytest.mark.parametrize(
        "data",
        [
            {"saml-auth": "service"},
            {"saml-auth": "bogus"},
            {"saml-auth": "Dialog"},  # exact match only, like the editors
            {"saml-auth": " dialog "},
            {"saml-auth": "dialog", "auth-mode": "credentials"},
        ],
    )
    def test_the_dialog_is_not_used_otherwise(self, session, data):
        connect(session, data)

        assert session.asked == []
        assert session.started == [""]

    @pytest.mark.parametrize(
        ("flags", "writes"), [({"gpauth-result-flags": "2"}, 0), ({}, 1)]
    )
    def test_profile_flags_written_only_when_missing(self, session, flags, writes):
        session.reply = "standard"

        connect(session, {"saml-auth": "dialog", **flags})
        assert session.profile_writes == []  # never during the activation
        asyncio.run(session._persist_profile())  # the tunnel is up

        assert len(session.profile_writes) == writes
        if writes:
            assert session.profile_writes == [
                ({"gpauth-result-flags": "2"}, ["gpauth-result"])
            ]

    def test_profile_flags_written_after_a_failed_activation(
        self, session, dbus_signals
    ):
        session.reply = "standard"
        connect(session, {"saml-auth": "dialog"})

        async def go():
            session._emit_failure(1)
            assert session.profile_writes == []
            await session._flags_task

        asyncio.run(go())

        assert session.profile_writes == [
            ({"gpauth-result-flags": "2"}, ["gpauth-result"])
        ]

    def test_standard_runs_the_old_command(self, session, dbus_signals):
        session.reply = "standard"

        connect(session, {"saml-auth": "dialog"})

        assert session.started == [""]  # no result: the old command
        assert session._saml_dialog is False
        assert failures(dbus_signals) == []

    @pytest.mark.parametrize(
        "reply",
        [
            '{"cancelled":null}',
            "cancelled",
            "",
            "standard\n{}",
            '{"success":{"username":"a","preloginCookie":"123456"},"failure":"x"}',
            line(preloginCookie="12345"),
            line(username="a\nb"),
            line(token="x" * 20000),
        ],
    )
    def test_an_unusable_reply_fails_the_login_and_starts_nothing(
        self, session, dbus_signals, service_module, reply, caplog
    ):
        session.reply = reply

        with caplog.at_level(logging.DEBUG):
            connect(session, {"saml-auth": "dialog"})

        assert session.started == []
        assert failures(dbus_signals) == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED)
        ]
        assert "x" * 100 not in caplog.text

    def test_a_connection_without_an_agent_fails_clearly(
        self, session, dbus_signals, service_module, caplog
    ):
        with caplog.at_level(logging.ERROR):
            connect(session, {"saml-auth": "dialog"}, interactive=False)

        assert session.asked == []
        assert session.started == []
        assert "saml-auth=service" in caplog.text
        assert len(failures(dbus_signals)) == 1

    def test_a_failed_start_is_reported(
        self, session, dbus_signals, service_module, monkeypatch
    ):
        async def start():
            return False

        monkeypatch.setattr(session, "_start_gpclient", start)

        connect(session, {"saml-auth": "dialog"})

        assert failures(dbus_signals) == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED)
        ]

    def test_a_disconnect_during_the_request_starts_nothing(
        self, session, dbus_signals
    ):
        session.real_request = True  # nobody answers it

        async def go():
            await session._do_connect(
                _connection(**{"saml-auth": "dialog"}), interactive=True
            )
            for _ in range(100):
                if any(s[0] == "SecretsRequired" for s in dbus_signals):
                    break
                await asyncio.sleep(0.05)
            task = session._auth_task
            await session.Disconnect()
            assert task.cancelled()

        asyncio.run(go())

        (request,) = [s for s in dbus_signals if s[0] == "SecretsRequired"]
        message, hints = request[1]
        assert hints[-1] == "gpauth-result"
        assert hints[1].startswith("x-gp-saml:")
        assert message == "Sign in to vpn.example.com"
        assert session.started == []
        assert failures(dbus_signals) == []
        assert session._auth_task is None
        assert session._auth_result == ""


class TestGatewaySamlLogin:
    """gpclient starts gpauth itself when the gateway wants its own SAML login:
    as root, without a session, that cannot work - fail at once"""

    RECORD = (
        "import json; sys.stdout.write(json.dumps({'timestamp': 't', "
        "'level': 'INFO', 'target': 'gpclient::connect::credential', "
        "'message': __MESSAGE__}) + '\\r\\n'); sys.stdout.flush(); "
        "import time; time.sleep(30)"
    )

    def _run(self, launcher, dbus_signals, message, wait=100, **settings):
        plugin = launcher(
            after=self.RECORD.replace("__MESSAGE__", repr(message)), **settings
        )

        async def go():
            assert await plugin._start_gpclient()
            for _ in range(wait):
                if failures(dbus_signals):
                    break
                await asyncio.sleep(0.1)
            await plugin._stop_gpclient()
            plugin._close_pty()

        asyncio.run(go())
        return plugin

    def test_the_record_fails_the_login(
        self, launcher, dbus_signals, service_module, caplog
    ):
        result = json.dumps(GOOD, separators=(",", ":"))

        with caplog.at_level(logging.ERROR):
            self._run(
                launcher,
                dbus_signals,
                "SAML auth launch: gateway=true, clean=false, browser=external",
                _auth_result=result,
            )

        assert failures(dbus_signals) == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED)
        ]
        assert "second SAML login" in caplog.text
        assert "saml-auth=service" in caplog.text

    @pytest.mark.parametrize(
        "message",
        [
            "SAML auth launch: gateway=false, clean=false, browser=external",
            "SAML auth launch: gateway=truely",
            "gateway=true",
        ],
    )
    def test_other_records_do_not(self, launcher, dbus_signals, message, caplog):
        with caplog.at_level(logging.ERROR):
            self._run(
                launcher,
                dbus_signals,
                message,
                wait=10,
                _auth_result=json.dumps(GOOD, separators=(",", ":")),
            )

        assert "second SAML login" not in caplog.text

    def test_without_a_result_the_service_mode_goes_on(
        self, launcher, dbus_signals, caplog
    ):
        with caplog.at_level(logging.ERROR):
            self._run(
                launcher,
                dbus_signals,
                "SAML auth launch: gateway=true, clean=false, browser=external",
                wait=10,
                _auth_result="",
            )

        assert "second SAML login" not in caplog.text

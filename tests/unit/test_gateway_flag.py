"""
Tests for gpclient's --gateway (issue #53, item 1.4).

--gateway=<preferred-gateway> is passed only when the portal offered that name
or address before (it is in the cached gateway-list); a stale cache makes
gpclient fail with "Cannot find gateway specified", and the service restarts it
once without the option. The editors store the whole cached entry
"name (address)"; the service passes the name of the entry in every case.

The real command is captured: a stand-in for gpclient records argv and stdin.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json
import logging
import sys

import pytest

# Records argv and the first stdin line (read only with stdin_wait) per launch;
# fails with the upstream error when "not-found" is set: with a --gateway given
# ("flag") or on every launch ("always")
FAKE_GPCLIENT = r'''
import json, sys
args = sys.argv[1:]
import select
stdin_line = ""
if __PROMPT__:
    sys.stdout.write(__PROMPT__)
    sys.stdout.flush()
if __STDIN_WAIT__ and select.select([sys.stdin], [], [], __STDIN_WAIT__)[0]:
    stdin_line = sys.stdin.readline()
with open("__LOG__", "a") as out:
    out.write(json.dumps({"argv": args, "stdin": stdin_line}) + "\n")
if __NOT_FOUND__ == "always" or (
    __NOT_FOUND__ == "flag" and any(a.startswith("--gateway=") for a in args)
):
    message = "Cannot find gateway specified: " + [
        a for a in args if a.startswith("--gateway=")
    ][0][10:]
    if __AS_TEXT__:
        sys.stdout.write("Error: " + message + "\r\n")
    else:  # upstream's error report is a log record
        sys.stdout.write(json.dumps({"timestamp": "t", "level": "ERROR",
            "target": "gpapi::clap", "message": message}) + "\r\n")
    sys.stdout.flush()
    sys.exit(1)
sys.exit(0)
'''

STORED = "gw-a (a.example.com);gw-b (b.example.com)"


def _launches(service_module, monkeypatch, tmp_path, not_found="", as_text=False,
              stdin_wait="0", prompt="", **settings):
    """Run gpclient to its end (with restarts); return the recorded launches"""
    log = tmp_path / "launches.jsonl"
    script = tmp_path / "gpclient.py"
    script.write_text(
        FAKE_GPCLIENT.replace("__LOG__", str(log))
        .replace("__NOT_FOUND__", repr(not_found))
        .replace("__AS_TEXT__", repr(as_text))
        .replace("__STDIN_WAIT__", stdin_wait)
        .replace("__PROMPT__", repr(prompt))
    )
    launcher = tmp_path / "gpclient.sh"
    launcher.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(launcher))

    plugin = service_module.GpclientVPNPlugin()
    plugin.gateway = "portal.example.com"
    plugin.browser = "/bin/true"
    for name, value in settings.items():
        setattr(plugin, name, value)

    async def run():
        assert await plugin._start_gpclient()
        while True:
            task = plugin.stdout_monitor_task
            await asyncio.wait_for(asyncio.shield(task), 30)
            if plugin.stdout_monitor_task is task:  # a restart replaces it
                return

    asyncio.run(run())
    lines = log.read_text().splitlines() if log.exists() else []
    return plugin, [json.loads(line) for line in lines]


def _gateway_args(launch):
    return [a for a in launch["argv"] if a.startswith("--gateway")]


class TestGatewayFlag:
    @pytest.mark.parametrize(
        "preferred",
        ["gw-b", "b.example.com", "gw-b (b.example.com)"],  # name, address, whole entry
    )
    def test_passed_when_the_portal_offered_it(
        self, service_module, monkeypatch, tmp_path, preferred
    ):
        _, launches = _launches(
            service_module, monkeypatch, tmp_path,
            preferred_gateway=preferred, _stored_gateway_list=STORED,
        )

        (launch,) = launches
        # always the name of the entry (what the portal lists)
        assert _gateway_args(launch) == ["--gateway=gw-b"]
        argv = launch["argv"]
        assert argv.index("connect") < argv.index("--gateway=gw-b")
        assert argv.index("--gateway=gw-b") < argv.index("--")

    @pytest.mark.parametrize(
        ("preferred", "stored", "expected"),
        [
            ("Branch (HQ) (hq.example.com)", "Branch (HQ) (hq.example.com)", "Branch (HQ)"),
            ("hq.example.com", "Branch (HQ) (hq.example.com)", "Branch (HQ)"),
            ("Branch (HQ)", "Branch (HQ) (hq.example.com)", "Branch (HQ)"),
            ("US East (a.example.com)", "x (x.example.com);US East (a.example.com)", "US East"),
            ("plain", "plain", "plain"),  # an entry without an address
            ("gw-b (b.example.com) ", STORED, ""),  # not exact
            ("(b.example.com)", STORED, ""),
            ("gw-b (other.example.com)", STORED, ""),
            ("gw-b", "(b.example.com)", ""),  # an entry without a name
        ],
    )
    def test_cached_gateway_arg(self, service_module, preferred, stored, expected):
        plugin = service_module.GpclientVPNPlugin()
        plugin.preferred_gateway = preferred
        plugin._stored_gateway_list = stored
        assert plugin._cached_gateway_arg() == expected

    def test_dropped_flag_gives_nothing(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin.preferred_gateway = "gw-b"
        plugin._stored_gateway_list = STORED
        plugin._gateway_flag_dropped = True
        assert plugin._cached_gateway_arg() == ""

    @pytest.mark.parametrize(
        "settings",
        [
            dict(preferred_gateway="", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw-c", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw-b", _stored_gateway_list=""),
            dict(preferred_gateway="gw-b", _stored_gateway_list="gw-bb (x.example.com)"),
            # gpclient compares exactly, so the cache must too
            dict(preferred_gateway="GW-B", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw-b (c.example.com)", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw-b", _stored_gateway_list=STORED, as_gateway=True),
            dict(preferred_gateway="gw-b (b.example.com)", _stored_gateway_list=STORED,
                 as_gateway=True),
            dict(preferred_gateway="gw-b", _stored_gateway_list=STORED,
                 _gateway_flag_dropped=True),
        ],
    )
    def test_left_out_otherwise(self, service_module, monkeypatch, tmp_path, settings):
        _, launches = _launches(service_module, monkeypatch, tmp_path, **settings)

        (launch,) = launches
        assert _gateway_args(launch) == []


class TestGatewayNotFoundRestart:
    def test_restarts_once_without_the_flag(
        self, service_module, monkeypatch, tmp_path, caplog
    ):
        with caplog.at_level(logging.WARNING):
            _, launches = _launches(
                service_module, monkeypatch, tmp_path, not_found="flag",
                preferred_gateway="gw-b", _stored_gateway_list=STORED,
            )

        assert [_gateway_args(x) for x in launches] == [["--gateway=gw-b"], []]
        assert "no longer offered" in caplog.text

    def test_the_error_as_a_plain_line_does_not_restart(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        # Only a log record counts: a plain line cannot trigger the restart
        _, launches = _launches(
            service_module, monkeypatch, tmp_path, not_found="flag", as_text=True,
            preferred_gateway="gw-b", _stored_gateway_list=STORED,
        )

        assert [_gateway_args(x) for x in launches] == [["--gateway=gw-b"]]

    def test_a_second_failure_is_not_retried(
        self, service_module, monkeypatch, tmp_path, dbus_signals
    ):
        _, launches = _launches(
            service_module, monkeypatch, tmp_path, not_found="always",
            preferred_gateway="gw-b", _stored_gateway_list=STORED,
        )

        assert len(launches) == 2
        assert ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED) in (
            dbus_signals
        )

    def test_no_restart_without_the_flag(self, service_module):
        # No --gateway was passed (not cached), so the error is none of ours
        plugin = service_module.GpclientVPNPlugin()
        plugin.preferred_gateway = "gw-b"
        plugin._stored_gateway_list = ""
        plugin._gateway_not_found_seen = True
        assert asyncio.run(plugin._retry_without_gateway()) is False

    def test_no_restart_for_another_error(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin.preferred_gateway = "gw-b"
        plugin._stored_gateway_list = STORED
        assert asyncio.run(plugin._retry_without_gateway()) is False

    @pytest.mark.parametrize(
        "line", ["Cannot find gateway specified: gw-b", "Error: Cannot find gateway specified: x"]
    )
    def test_error_regex_matches_upstream_text(self, service_module, line):
        assert service_module.GATEWAY_NOT_FOUND_RE.search(line)

    @pytest.mark.parametrize("line", ["Cannot find gateway", "No gateways available"])
    def test_error_regex_rejects_other_text(self, service_module, line):
        assert not service_module.GATEWAY_NOT_FOUND_RE.search(line)


class TestStoredPasswordAtThePrompt:
    """No --passwd-on-stdin (it fed the password to the next prompt of a SAML
    login): the stored password is typed only when gpclient asks for it"""

    def test_nothing_is_written_before_a_prompt(
        self, service_module, monkeypatch, tmp_path
    ):
        _, launches = _launches(
            service_module, monkeypatch, tmp_path, stdin_wait="0.5",
            vpn_username="user", vpn_password="s3cr3t-pw", _answered_username=True,
        )

        (launch,) = launches
        assert "--passwd-on-stdin" not in launch["argv"]
        assert launch["stdin"] == ""  # no input queued at spawn

    def test_typed_at_the_password_prompt(
        self, service_module, monkeypatch, tmp_path
    ):
        _, launches = _launches(
            service_module, monkeypatch, tmp_path, stdin_wait="5",
            prompt="? Password: ", vpn_username="user", vpn_password="s3cr3t-pw",
            _answered_username=True, _interactive=True,
        )

        (launch,) = launches
        assert "--passwd-on-stdin" not in launch["argv"]
        assert launch["stdin"] == "s3cr3t-pw\n"

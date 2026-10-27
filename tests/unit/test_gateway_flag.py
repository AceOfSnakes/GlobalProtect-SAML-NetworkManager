"""
Tests for gpclient's --gateway and --passwd-on-stdin (issue #53, item 1.4).

--gateway=<preferred-gateway> is passed only when the portal offered that name
or address before (it is in the cached gateway-list); a stale cache makes
gpclient fail with "Cannot find gateway specified", and the service restarts it
once without the option. A stored password of a standard login goes to
gpclient's stdin (--passwd-on-stdin) instead of the PTY prompt.

The real command is captured: a stand-in for gpclient records argv and stdin.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json
import logging
import sys

import pytest

# Records argv (and the first stdin line with --passwd-on-stdin) per launch;
# fails with the upstream error when "not-found" is set: with a --gateway given
# ("flag") or on every launch ("always")
FAKE_GPCLIENT = r'''
import json, sys
args = sys.argv[1:]
stdin_line = None
if "--passwd-on-stdin" in args:
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


def _launches(service_module, monkeypatch, tmp_path, not_found="", as_text=False, **settings):
    """Run gpclient to its end (with restarts); return the recorded launches"""
    log = tmp_path / "launches.jsonl"
    script = tmp_path / "gpclient.py"
    script.write_text(
        FAKE_GPCLIENT.replace("__LOG__", str(log))
        .replace("__NOT_FOUND__", repr(not_found))
        .replace("__AS_TEXT__", repr(as_text))
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
    @pytest.mark.parametrize("preferred", ["gw-b", "b.example.com"])
    def test_passed_when_the_portal_offered_it(
        self, service_module, monkeypatch, tmp_path, preferred
    ):
        _, launches = _launches(
            service_module, monkeypatch, tmp_path,
            preferred_gateway=preferred, _stored_gateway_list=STORED,
        )

        (launch,) = launches
        assert _gateway_args(launch) == [f"--gateway={preferred}"]
        argv = launch["argv"]
        assert argv.index("connect") < argv.index(f"--gateway={preferred}")
        assert argv.index(f"--gateway={preferred}") < argv.index("--")

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
            dict(preferred_gateway="gw-b (b.example.com)", _stored_gateway_list=STORED),
            dict(preferred_gateway="gw-b", _stored_gateway_list=STORED, as_gateway=True),
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


class TestPasswordOnStdin:
    def test_password_goes_to_stdin_once(
        self, service_module, monkeypatch, tmp_path, caplog
    ):
        with caplog.at_level(logging.DEBUG):
            plugin, launches = _launches(
                service_module, monkeypatch, tmp_path,
                vpn_password="s3cr3t-pw", _password_on_stdin=True,
            )

        (launch,) = launches
        assert "--passwd-on-stdin" in launch["argv"]
        assert launch["argv"].index("--passwd-on-stdin") > launch["argv"].index("connect")
        assert launch["stdin"] == "s3cr3t-pw\n"
        assert "s3cr3t-pw" not in caplog.text
        # The prompt handler must not answer a password prompt again
        assert plugin._classify_prompt_kind("Password:", "") != "password"
        plugin._reset_phase_state()  # a new auth round
        assert plugin._answered_password is True

    @pytest.mark.parametrize(
        "settings",
        [
            dict(vpn_password="", _password_on_stdin=False),
            dict(vpn_password="s3cr3t-pw", _password_on_stdin=False),
        ],
    )
    def test_not_used_otherwise(self, service_module, monkeypatch, tmp_path, settings):
        _, launches = _launches(service_module, monkeypatch, tmp_path, **settings)

        (launch,) = launches
        assert "--passwd-on-stdin" not in launch["argv"]
        assert launch["stdin"] is None

    @pytest.mark.parametrize(
        ("data", "secrets", "expected"),
        [
            ({"auth-mode": "credentials"}, {"password": "pw"}, True),
            ({"auth-mode": "credentials"}, {}, False),
            ({"auth-mode": "credentials"}, {"password": ""}, False),
            ({"auth-mode": "saml"}, {"password": "pw"}, False),
            ({}, {"password": "pw"}, False),
        ],
    )
    def test_decided_when_connecting(
        self, service_module, monkeypatch, data, secrets, expected
    ):
        plugin = service_module.GpclientVPNPlugin()
        monkeypatch.setattr(plugin, "_cleanup_stale_gpd0", lambda: asyncio.sleep(0))
        monkeypatch.setattr(plugin, "_stop_gpclient", lambda: asyncio.sleep(0))
        monkeypatch.setattr(plugin, "_start_gpclient", lambda: asyncio.sleep(0, True))
        conn = {"vpn": {"data": {"gateway": "vpn.example.com", **data}, "secrets": secrets}}

        async def go():
            await plugin._do_connect(conn, interactive=False)
            task = plugin.profile_task
            if task:
                task.cancel()

        asyncio.run(go())

        assert plugin._password_on_stdin is expected

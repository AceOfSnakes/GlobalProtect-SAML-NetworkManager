"""
Tests for gateways that authenticate without a tunnel (issue #34).

With internal host detection on the portal, a gpclient 3.x gateway can answer
in non-tunnel mode: gpclient stays in the foreground, creates no tun device and
logs "Internal network - authenticated". Waiting for a tunnel would hang, so
the service fails the activation and stops gpclient.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import sys

import pytest

# Stand-in for gpclient; prints __LINE__ (UTF-8, as gpclient does) and then
# either stays in the foreground (__HANG__ is True) or exits cleanly
FAKE_GPCLIENT = r'''
import sys, time

line = __LINE__
sys.stdout.buffer.write(("[2026-01-01T00:00:00Z INFO  gpclient::cli] started\r\n").encode())
sys.stdout.buffer.write((line + "\r\n").encode("utf-8"))
sys.stdout.flush()
if __HANG__:
    time.sleep(60)
sys.exit(0)
'''

INTERNAL_LINE = (
    "[2026-01-01T00:00:01Z INFO  gpclient::connect::internal] "
    "Internal network — authenticated: gw.example.com "
    "(IPv4: 10.0.0.1, IPv6: ::1)"
)


def _plugin(service_module, monkeypatch, tmp_path, line, hang):
    script = tmp_path / "gpclient.py"
    script.write_text(
        FAKE_GPCLIENT.replace("__LINE__", repr(line)).replace("__HANG__", repr(hang))
    )
    launcher = tmp_path / "gpclient.sh"
    launcher.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)

    monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(launcher))
    plugin = service_module.GpclientVPNPlugin()
    plugin.gateway = "portal.example.com"
    plugin.browser = "/bin/true"
    return plugin


async def _run(plugin, timeout=10):
    """Start gpclient and wait until the monitor has finished"""
    assert await plugin._start_gpclient()
    process = plugin.gpclient_process
    try:
        await asyncio.wait_for(asyncio.shield(plugin.stdout_monitor_task), timeout)
        await asyncio.wait_for(process.wait(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process


def test_internal_network_session_fails_activation(
    service_module, monkeypatch, tmp_path, dbus_signals
):
    plugin = _plugin(service_module, monkeypatch, tmp_path, INTERNAL_LINE, hang=True)

    process = asyncio.run(_run(plugin))

    # gpclient was stopped (SIGTERM), it did not exit by itself
    assert process.returncode == -15
    # One failure: not a login failure, and not repeated when gpclient exits
    assert dbus_signals == [
        ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED),
        ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "[INFO  gpclient::connect] VPN interface: tun0",
        "Connected to gw.example.com",
        "ESP tunnel connected",
        "[INFO  gpclient::connect::internal] Internal host detection: no match",
        "[WARN  gpclient::connect::internal] Failed to resolve internal host",
        "Internal network detected, authenticated by the portal",
    ],
)
def test_tunnel_log_lines_do_not_count_as_internal_network(
    service_module, monkeypatch, tmp_path, dbus_signals, line
):
    plugin = _plugin(service_module, monkeypatch, tmp_path, line, hang=False)

    process = asyncio.run(_run(plugin))

    assert process.returncode == 0
    assert dbus_signals == []
    assert plugin._login_failed is False


@pytest.mark.parametrize("dash", ["—", "–", "-"])
def test_internal_network_line_matches_with_any_dash(service_module, dash):
    line = f"Internal network {dash} authenticated: gw (IPv4: 10.0.0.1)"
    assert service_module.INTERNAL_NETWORK_RE.search(line)


@pytest.mark.parametrize(
    "line",
    ["Internal network authenticated: gw", "Internal network: unreachable"],
)
def test_internal_network_line_needs_the_dash_and_the_colon(service_module, line):
    assert not service_module.INTERNAL_NETWORK_RE.search(line)

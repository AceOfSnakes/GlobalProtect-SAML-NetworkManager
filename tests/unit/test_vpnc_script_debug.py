"""
Tests for scripts/vpnc-script-debug, the opt-in wrapper that logs to the journal and runs the
real vpnc-script. The real script and `logger` are fakes on PATH.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WRAPPER = os.path.join(ROOT, "scripts", "vpnc-script-debug")
TUNNEL_ENV = {"reason": "connect", "TUNDEV": "tun7", "VPNGATEWAY": "198.51.100.10",
              "INTERNAL_IP4_ADDRESS": "10.0.0.5", "INTERNAL_IP6_ADDRESS": "fd00::5",
              "CISCO_SPLIT_INC": "1", "MTU": "1400", "CISCO_DEF_DOMAIN": "corp.example"}


def run_wrapper(tmp_path, status=0, real="exec", reason="connect"):
    """real: "exec" (executable fake script), "plain" (not executable) or "none" (missing)"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    logger = bin_dir / "logger"
    logger.write_text('#!/bin/bash\nwhile IFS= read -r l; do echo "$*: $l" >> "$FAKE_LOG"; done\n')
    logger.chmod(0o755)
    script = tmp_path / "real-vpnc-script"
    if real != "none":
        script.write_text(f'#!/bin/sh\necho "real script ran: $1"\nexit {status}\n')
        script.chmod(0o755 if real == "exec" else 0o644)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(tmp_path / "log"),
           "VPNC_SCRIPT_REAL": str(script), "SECRET_TOKEN": "hunter2", **TUNNEL_ENV, "reason": reason}
    result = subprocess.run(["bash", WRAPPER, "arg1"], env=env, capture_output=True, text=True, timeout=30)
    log = (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    return result, log


@pytest.mark.parametrize("status", [0, 1, 5])
def test_the_exit_status_of_the_real_script_is_passed_through(tmp_path, status):
    result, _ = run_wrapper(tmp_path, status=status)

    assert result.returncode == status


def test_the_tunnel_variables_and_the_trace_go_to_the_journal_and_nothing_else(tmp_path):
    result, log = run_wrapper(tmp_path)

    for name, value in TUNNEL_ENV.items():
        assert f"-t vpnc-script-debug: {name}={value}" in log
    assert "real script ran: arg1" in log
    assert "+ echo" in log  # the -x trace
    assert "hunter2" not in log and "SECRET_TOKEN" not in log


@pytest.mark.parametrize("real", ["none", "plain"])
def test_a_missing_or_not_executable_real_script_fails_with_a_clear_message(tmp_path, real):
    result, log = run_wrapper(tmp_path, real=real)

    assert result.returncode == 1
    assert "the real script" in result.stderr and "missing or not executable" in result.stderr
    assert "real script ran" not in log

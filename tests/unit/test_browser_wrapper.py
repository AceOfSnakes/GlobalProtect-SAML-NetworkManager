"""
Tests for how browser-wrapper identifies the real user.

The user is identified by UID, never by name: a name like first.last or
first.last@corp.example used to be rejected before the browser was started, and
a name taken from the environment must never reach a command line.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import shutil
import subprocess

import pytest

WRAPPER = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "browser-wrapper.sh"
)
URL = "https://portal.example.com/saml"
# The wrapper keeps its files in /tmp/edge-wrapper-<uid>; stay clear of real ones
FAKE_UID = 48213
UNKNOWN_UID = 48214

IS_ROOT = os.geteuid() == 0


def _script(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _fake_browser(tmp_path):
    _script(tmp_path / "browser", f'echo "$@" > {tmp_path}/opened\n')


def _run_wrapper(env):
    try:
        return subprocess.run(["bash", WRAPPER, URL], env=env, timeout=30)
    finally:
        shutil.rmtree(f"/tmp/edge-wrapper-{FAKE_UID}", ignore_errors=True)
        if os.path.exists(f"/tmp/edge-wrapper-{FAKE_UID}.log"):
            os.remove(f"/tmp/edge-wrapper-{FAKE_UID}.log")


def _fake_root_tools(tmp_path):
    """Fake sudo/getent so the root code path can run without a real user."""
    _fake_browser(tmp_path)
    _script(
        tmp_path / "getent",
        f'if [ "$1" = passwd ] && [ "$2" = {FAKE_UID} ]; then\n'
        f"    echo 'first.last@corp.example:x:{FAKE_UID}:{FAKE_UID}::{tmp_path}/home:/bin/bash'\n"
        "else\n"
        "    exit 2\n"
        "fi\n",
    )
    # Record the args, drop "-u <user>" and run the rest as the current user
    _script(
        tmp_path / "sudo",
        f'printf "%s\\n" "$@" > {tmp_path}/sudo_args\n' 'shift 2\n' 'exec "$@"\n',
    )


def _root_env(tmp_path, **extra):
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GP_BROWSER": str(tmp_path / "browser"),
    }
    env.update(extra)
    return env


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_accepts_username_with_a_dot(tmp_path):
    (tmp_path / "id").write_text(f"#!/bin/sh\necho {FAKE_UID}\n")
    (tmp_path / "browser").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/opened\n')
    (tmp_path / "id").chmod(0o755)
    (tmp_path / "browser").chmod(0o755)

    try:
        subprocess.run(
            ["bash", WRAPPER, URL],
            env={
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "USER": "first.last",
                "GP_BROWSER": str(tmp_path / "browser"),
            },
            timeout=30,
        )
    finally:
        shutil.rmtree(f"/tmp/edge-wrapper-{FAKE_UID}", ignore_errors=True)
        if os.path.exists(f"/tmp/edge-wrapper-{FAKE_UID}.log"):
            os.remove(f"/tmp/edge-wrapper-{FAKE_UID}.log")

    opened = tmp_path / "opened"
    assert opened.exists(), "the browser was not started"
    assert opened.read_text() == URL + "\n"


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_non_root_ignores_sudo_variables(tmp_path):
    """Without root we cannot switch user, so SUDO_* must not matter at all."""
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    pwned = tmp_path / "pwned"

    result = _run_wrapper(
        _root_env(
            tmp_path,
            USER="first.last",
            SUDO_UID="0",
            SUDO_USER=f"x; touch {pwned}",
        )
    )

    assert not pwned.exists(), "SUDO_USER reached a command"
    assert result.returncode == 0
    opened = tmp_path / "opened"
    assert opened.exists(), "the browser was not started"
    assert opened.read_text() == URL + "\n"


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_drops_privileges_by_numeric_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_UID=str(FAKE_UID), SUDO_USER="ignored; touch /x")
    )

    assert result.returncode == 0
    sudo_args = (tmp_path / "sudo_args").read_text().splitlines()
    assert sudo_args[:2] == ["-u", f"#{FAKE_UID}"]
    opened = tmp_path / "opened"
    assert opened.exists(), "the browser was not started"
    assert opened.read_text() == URL + "\n"


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
def test_root_rejects_non_numeric_sudo_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID="abc"))

    assert result.returncode != 0
    assert not (tmp_path / "opened").exists()
    assert not (tmp_path / "sudo_args").exists()


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
def test_root_rejects_unknown_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(UNKNOWN_UID)))

    assert result.returncode != 0
    assert not (tmp_path / "opened").exists()
    assert not (tmp_path / "sudo_args").exists()

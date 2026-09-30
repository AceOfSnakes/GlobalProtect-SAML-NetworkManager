"""
Tests for how browser-wrapper identifies the real user.

The user is identified by UID, never by name: a name like first.last or
first.last@corp.example used to be rejected before the browser was started, and
a name taken from the environment must never reach a command line.

Every positive test has negative counterparts. A rejection must leave no trace:
non-zero exit, browser not started, sudo not called, injected command not run.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import shlex
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
OTHER_UID = FAKE_UID + 5
# Made-up uids: whatever the wrapper leaves behind for them is always removed
RESERVED_UIDS = (FAKE_UID, UNKNOWN_UID, OTHER_UID)
# The real root may already own files there: remove only what a test created
ROOT_UID = 0
FAKE_SUDO_USER = "first.last@corp.example"

IS_ROOT = os.geteuid() == 0


def _script(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _fake_browser(tmp_path):
    _script(tmp_path / "browser", f'echo "$@" > {tmp_path}/opened\n')


def _fake_sudo(tmp_path):
    """Record the args, drop "-u <user>" and run the rest as the current user."""
    _script(
        tmp_path / "sudo",
        f'printf "%s\\n" "$@" > {tmp_path}/sudo_args\n' "shift 2\n" 'exec "$@"\n',
    )


def _wrapper_files(uid):
    return f"/tmp/edge-wrapper-{uid}", f"/tmp/edge-wrapper-{uid}.log"


def _remove_wrapper_files(uid):
    directory, log_file = _wrapper_files(uid)
    shutil.rmtree(directory, ignore_errors=True)
    if os.path.lexists(log_file):
        os.remove(log_file)


def _run_wrapper(env):
    root_files_before = [p for p in _wrapper_files(ROOT_UID) if os.path.lexists(p)]
    try:
        return subprocess.run(
            ["bash", WRAPPER, URL],
            env=env,
            timeout=30,
            capture_output=True,
            text=True,
        )
    finally:
        for uid in RESERVED_UIDS:
            _remove_wrapper_files(uid)
        directory, log_file = _wrapper_files(ROOT_UID)
        if directory not in root_files_before:
            shutil.rmtree(directory, ignore_errors=True)
        if log_file not in root_files_before and os.path.lexists(log_file):
            os.remove(log_file)


def _fake_root_tools(tmp_path):
    """Fake sudo/getent so the root code path can run without a real user."""
    _fake_browser(tmp_path)
    _fake_sudo(tmp_path)
    _script(
        tmp_path / "getent",
        f'if [ "$1" = passwd ] && [ "$2" = {FAKE_UID} ]; then\n'
        f"    echo '{FAKE_SUDO_USER}:x:{FAKE_UID}:{FAKE_UID}::{tmp_path}/home:/bin/bash'\n"
        "else\n"
        "    exit 2\n"
        "fi\n",
    )


def _fake_id_by_name(tmp_path, known_user=FAKE_SUDO_USER, known_output=FAKE_UID):
    """Fake id: `id -u -- <known_user>` prints known_output, `id -u` prints 0,
    anything else fails."""
    _script(
        tmp_path / "id",
        f'if [ "$*" = "-u -- {known_user}" ]; then\n'
        f"    printf '%s\\n' {shlex.quote(str(known_output))}\n"
        'elif [ "$*" = "-u" ]; then\n'
        "    echo 0\n"
        "else\n"
        "    exit 1\n"
        "fi\n",
    )


def _root_env(tmp_path, **extra):
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GP_BROWSER": str(tmp_path / "browser"),
        # Never wait long for a gpauth that may exist on the machine
        "GP_AUTH_TIMEOUT": "5",
    }
    env.update(extra)
    return env


def _assert_browser_started(tmp_path, result):
    assert result.returncode == 0, result.stderr
    opened = tmp_path / "opened"
    assert opened.exists(), "the browser was not started"
    assert opened.read_text() == URL + "\n"
    assert not (tmp_path / "pwned").exists(), "an injected command was run"


def _assert_rejected(tmp_path, result, reason):
    assert result.returncode != 0
    assert reason in result.stderr
    assert not (tmp_path / "opened").exists(), "the browser was started"
    assert not (tmp_path / "sudo_args").exists(), "sudo was called"
    assert not (tmp_path / "pwned").exists(), "an injected command was run"


def _sudo_args(tmp_path):
    return (tmp_path / "sudo_args").read_text().splitlines()


# --- Not root: we cannot switch user, the uid comes from `id -u` ---------------


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_accepts_username_with_a_dot(tmp_path):
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, USER="first.last"))

    _assert_browser_started(tmp_path, result)


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
@pytest.mark.parametrize(
    "id_output",
    [None, "", "abc", "48213; touch {pwned}", "-1"],
    ids=["id-fails-no-output", "empty-line", "letters", "shell-injection", "negative"],
)
def test_non_root_rejects_bad_id_output(tmp_path, id_output):
    if id_output is None:
        _script(tmp_path / "id", "exit 1\n")
    else:
        text = id_output.format(pwned=tmp_path / "pwned")
        _script(tmp_path / "id", f"printf '%s\\n' {shlex.quote(text)}\n")
    _fake_browser(tmp_path)
    _fake_sudo(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, USER="first.last"))

    _assert_rejected(tmp_path, result, "cannot determine the real user's UID")


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

    _assert_browser_started(tmp_path, result)


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_non_root_never_calls_sudo(tmp_path):
    """A valid-looking SUDO_UID must not trigger the privilege drop."""
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    _fake_sudo(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(OTHER_UID)))

    _assert_browser_started(tmp_path, result)
    assert not (tmp_path / "sudo_args").exists(), "sudo was called"


# --- Root: the desktop user comes from SUDO_UID (or SUDO_USER) -----------------


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_drops_privileges_by_numeric_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_UID=str(FAKE_UID), SUDO_USER="ignored; touch /x")
    )

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


@pytest.mark.skipif(not IS_ROOT, reason="needs root to run the browser as root")
@pytest.mark.parametrize(
    "env",
    [{"SUDO_UID": "0"}, {}],
    ids=["sudo-uid-is-root", "no-sudo-variables"],
)
def test_root_without_a_desktop_user_runs_browser_directly(tmp_path, env):
    """Real user is root itself: nothing to drop to, so sudo must not be used."""
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, **env))

    _assert_browser_started(tmp_path, result)
    assert not (tmp_path / "sudo_args").exists(), "sudo was called"


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_USER path")
def test_root_falls_back_to_sudo_user(tmp_path):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_USER=FAKE_SUDO_USER))

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_USER path")
@pytest.mark.parametrize(
    "sudo_user",
    ["unknown.user", "x; touch {pwned}"],
    ids=["unknown-user", "shell-injection"],
)
def test_root_rejects_unknown_sudo_user(tmp_path, sudo_user):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)  # fails for every name except the known one

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_USER=sudo_user.format(pwned=tmp_path / "pwned"))
    )

    _assert_rejected(tmp_path, result, "Cannot get UID for user")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_USER path")
@pytest.mark.parametrize(
    "id_output",
    ["abc", "-1", "48213; touch {pwned}"],
    ids=["letters", "negative", "shell-injection"],
)
def test_root_rejects_bad_id_output_for_sudo_user(tmp_path, id_output):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(
        tmp_path, known_output=id_output.format(pwned=tmp_path / "pwned")
    )

    result = _run_wrapper(_root_env(tmp_path, SUDO_USER=FAKE_SUDO_USER))

    _assert_rejected(tmp_path, result, "cannot determine the real user's UID")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
@pytest.mark.parametrize(
    "sudo_uid",
    [
        "abc",
        "",
        "-1",
        "48213 ",
        "1e3",
        "48213; touch {pwned}",
        "$(touch {pwned})",
        "0x10",
    ],
    ids=[
        "letters",
        "set-but-empty",
        "negative",
        "trailing-space",
        "exponent",
        "semicolon-injection",
        "command-substitution",
        "hex",
    ],
)
def test_root_rejects_non_numeric_sudo_uid(tmp_path, sudo_uid):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_UID=sudo_uid.format(pwned=tmp_path / "pwned"))
    )

    _assert_rejected(tmp_path, result, "SUDO_UID is not a number")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
def test_root_rejects_unknown_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(UNKNOWN_UID)))

    _assert_rejected(tmp_path, result, "no passwd entry")

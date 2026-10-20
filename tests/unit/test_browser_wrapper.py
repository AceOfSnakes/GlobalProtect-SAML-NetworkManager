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
import glob
import shutil
import subprocess
import sys
from collections import Counter

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
    """Record the URL, the environment and which wrapper log files exist (the
    wrapper removes its own files only after the browser is done)."""
    _script(
        tmp_path / "browser",
        f'echo "$@" > {tmp_path}/opened\n'
        f"env > {tmp_path}/browser_env\n"
        f'stat -c "%a %u" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" '
        f'"$(dirname "$XDG_CACHE_HOME")" > {tmp_path}/base_stat 2>/dev/null\n'
        # What sits in the well-known /tmp path (the wrapper removes it later)
        f"ls -A /tmp/edge-wrapper-{FAKE_UID}/ > {tmp_path}/planted_listing 2>/dev/null\n"
        f"ls /tmp/edge-wrapper-*.log > {tmp_path}/wrapper_logs 2>/dev/null\n"
        # The log file inside the private base directory: mode, owner
        f'stat -c "%a %u" "$(dirname "$XDG_CACHE_HOME")/wrapper.log" '
        f"> {tmp_path}/log_stat 2>/dev/null\n"
        # What the planted old log path holds when the browser runs (the wrapper
        # removes it later)
        f"cat /tmp/edge-wrapper-{FAKE_UID}.log > {tmp_path}/old_log_at_browser 2>/dev/null\n"
        "echo browser-stderr-marker >&2\n",
    )


def _fake_sudo(tmp_path, reset_env=False):
    """Record the args, drop "-u <user>" and run the rest as the current user.

    reset_env mimics real sudo, which does not pass our environment on."""
    run = 'exec env -i "PATH=$PATH" "$@"\n' if reset_env else 'exec "$@"\n'
    _script(
        tmp_path / "sudo",
        f'printf "%s\\n" "$@" > {tmp_path}/sudo_args\n' "shift 2\n" + run,
    )


def _wrapper_files(uid):
    return f"/tmp/edge-wrapper-{uid}", f"/tmp/edge-wrapper-{uid}.log"


def _remove_path(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _remove_wrapper_files(uid):
    directory, log_file = _wrapper_files(uid)
    if os.path.lexists(directory):
        _remove_path(directory)
    if os.path.lexists(log_file):
        os.remove(log_file)
    # Fresh private directories the wrapper takes when the usual one is unusable
    for fallback in glob.glob(f"{directory}.*"):
        _remove_path(fallback)


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


def _fake_root_tools(tmp_path, reset_env=False):
    """Fake sudo/getent so the root code path can run without a real user."""
    _fake_browser(tmp_path)
    _fake_sudo(tmp_path, reset_env)
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


def _fake_getent(tmp_path, entry_uid, home, name="entry.user"):
    """Fake getent: every uid lookup returns an entry with the given uid field."""
    _script(
        tmp_path / "getent",
        f"echo '{name}:x:{entry_uid}:{entry_uid}::{home}:/bin/bash'\n",
    )


def _fake_logger(tmp_path):
    """The wrapper also logs early errors to syslog: keep tests off the real one."""
    _script(tmp_path / "logger", f'printf "%s\\n" "$*" >> {tmp_path}/syslog\n')


def _root_env(tmp_path, **extra):
    _fake_logger(tmp_path)
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
    # Messages logged before the log file exists (and, as root, every message
    # from the root process) go to syslog. A good run has no error in there.
    syslog = tmp_path / "syslog"
    text = syslog.read_text() if syslog.exists() else ""
    assert "ERROR" not in text


def _assert_rejected(tmp_path, result, reason):
    assert result.returncode != 0
    assert reason in result.stderr
    assert not (tmp_path / "opened").exists(), "the browser was started"
    assert not (tmp_path / "sudo_args").exists(), "sudo was called"
    assert not (tmp_path / "pwned").exists(), "an injected command was run"


def _assert_in_syslog(tmp_path, reason):
    """gpauth runs the wrapper detached, so stderr is lost: the error must be in
    syslog too."""
    syslog = tmp_path / "syslog"
    assert syslog.exists(), "nothing reached syslog"
    text = syslog.read_text()
    assert "-t gpclient-browser-wrapper --" in text
    assert reason in text


def _sudo_args(tmp_path):
    return (tmp_path / "sudo_args").read_text().splitlines()


def _browser_env(tmp_path):
    lines = (tmp_path / "browser_env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


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
    _assert_in_syslog(tmp_path, "cannot determine the real user's UID")


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


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
@pytest.mark.parametrize(
    "entry_uid, home_is_used",
    [(FAKE_UID, True), (OTHER_UID, False)],
    ids=["entry-matches", "entry-for-another-uid"],
)
def test_non_root_trusts_passwd_entry_only_for_the_same_uid(
    tmp_path, entry_uid, home_is_used
):
    """An entry for a different uid (glibc wraps big numbers) must not be used:
    the wrapper falls back to $HOME instead of failing."""
    own_home = tmp_path / "own-home"
    entry_home = tmp_path / "entry-home"
    own_home.mkdir()
    entry_home.mkdir()
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    _fake_getent(tmp_path, entry_uid, entry_home)

    result = _run_wrapper(_root_env(tmp_path, USER="first.last", HOME=str(own_home)))

    _assert_browser_started(tmp_path, result)
    expected = entry_home if home_is_used else own_home
    assert _browser_env(tmp_path)["HOME"] == str(expected)


# --- Root: the desktop user comes from SUDO_UID, PKEXEC_UID or a name ---------


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_drops_privileges_by_numeric_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_UID=str(FAKE_UID), SUDO_USER="ignored; touch /x")
    )

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


REFUSED_AS_ROOT = "refusing to run the browser as root"


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the root path")
@pytest.mark.parametrize(
    "env",
    [
        {"SUDO_UID": "0"},
        {"PKEXEC_UID": "0"},
        {"SUDO_UID": "000"},
        # A set SUDO_UID wins even when it says root; PKEXEC_UID is not consulted
        {"SUDO_UID": "0", "PKEXEC_UID": str(FAKE_UID)},
        {"USER": "root"},
        {"SUDO_USER": "root"},
        {},
    ],
    ids=[
        "sudo-uid-is-root",
        "pkexec-uid-is-root",
        "sudo-uid-zeros",
        "sudo-uid-root-beats-pkexec-uid",
        "user-root",
        "sudo-user-root",
        "no-variables",
    ],
)
def test_root_never_runs_browser_as_root(tmp_path, env):
    """Nothing points to a non-root user: refuse, do not run the browser as root."""
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path, known_user="root", known_output=0)

    result = _run_wrapper(_root_env(tmp_path, **env))

    _assert_rejected(tmp_path, result, REFUSED_AS_ROOT)
    _assert_in_syslog(tmp_path, REFUSED_AS_ROOT)


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_normalizes_leading_zeros_in_sudo_uid(tmp_path):
    """000000048213 must mean uid 48213 everywhere: sudo, log file and temp dir,
    however many zeros come first (more than 10 characters in all)."""
    _fake_root_tools(tmp_path)
    padded = f"0000000{FAKE_UID}"
    assert len(padded) > 10

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=padded))

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]
    # The log file is in the private base directory, never a /tmp/...log file
    assert (tmp_path / "log_stat").read_text().split() == ["600", str(os.geteuid())]
    assert (tmp_path / "wrapper_logs").read_text() == ""
    assert _browser_env(tmp_path)["XDG_CACHE_HOME"] == (
        f"/tmp/edge-wrapper-{FAKE_UID}/cache"
    )


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_rejects_passwd_entry_for_another_uid(tmp_path):
    """glibc may map an out-of-range number onto another user (even root)."""
    _fake_root_tools(tmp_path)
    _fake_getent(tmp_path, ROOT_UID, "/root", name="root")

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(FAKE_UID)))

    _assert_rejected(tmp_path, result, f"passwd entry for uid {FAKE_UID} is for uid")


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

    _assert_rejected(tmp_path, result, "cannot determine the real user's UID from SUDO_UID")
    _assert_in_syslog(tmp_path, "cannot determine the real user's UID from SUDO_UID")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
@pytest.mark.parametrize(
    "sudo_uid",
    [
        "99999999999999999999",
        "4294967295",
        "4294967296",
        "00004294967295",
        "000000004294967296",
    ],
    ids=[
        "overflow",
        "uid-t-minus-one",
        "wraps-to-root-in-glibc",
        "zeros-then-uid-t-minus-one",
        "zeros-then-wraps-to-root",
    ],
)
def test_root_rejects_out_of_range_sudo_uid(tmp_path, sudo_uid):
    """Only digits, but not a valid uid: must never reach sudo or the browser
    (bash would fail the numeric test and skip the privilege drop)."""
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=sudo_uid))

    _assert_rejected(tmp_path, result, "cannot determine the real user's UID")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the USER path")
def test_root_falls_back_to_user_without_sudo_variables(tmp_path):
    """The old wrapper dropped to $USER when no SUDO_* was set."""
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, USER=FAKE_SUDO_USER))

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the PKEXEC_UID path")
def test_root_uses_pkexec_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, PKEXEC_UID=str(FAKE_UID)))

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the PKEXEC_UID path")
@pytest.mark.parametrize(
    "env, reason",
    [
        ({"PKEXEC_UID": "abc"}, "cannot determine the real user's UID from PKEXEC_UID"),
        ({"PKEXEC_UID": ""}, "cannot determine the real user's UID from PKEXEC_UID"),
        # A set but invalid variable is an error, never a reason to try the next
        ({"SUDO_UID": "abc", "PKEXEC_UID": str(FAKE_UID)}, "cannot determine the real user's UID from SUDO_UID"),
        (
            {"PKEXEC_UID": "abc", "DOAS_USER": FAKE_SUDO_USER},
            "cannot determine the real user's UID from PKEXEC_UID",
        ),
    ],
    ids=["letters", "set-but-empty", "bad-sudo-uid-no-fallthrough", "bad-pkexec-uid-no-fallthrough"],
)
def test_root_rejects_invalid_uid_variable(tmp_path, env, reason):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, **env))

    _assert_rejected(tmp_path, result, reason)


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the DOAS_USER path")
def test_root_uses_doas_user_by_id_lookup(tmp_path):
    """The name is never parsed: only `id -u -- <name>` turns it into a number."""
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, DOAS_USER=FAKE_SUDO_USER))

    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the DOAS_USER path")
def test_root_rejects_unknown_doas_user(tmp_path):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, DOAS_USER="unknown.user"))

    _assert_rejected(tmp_path, result, "Cannot get UID for user")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the lookup order path")
def test_root_lookup_order_uid_before_names_and_doas_before_sudo_user(tmp_path):
    """SUDO_UID, PKEXEC_UID, DOAS_USER, SUDO_USER, USER: the first one that is
    set decides (names the fake id does not know would fail if they were tried)."""
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)

    # A uid variable wins over every name
    result = _run_wrapper(
        _root_env(
            tmp_path,
            PKEXEC_UID=str(FAKE_UID),
            DOAS_USER="unknown.user",
            SUDO_USER="unknown.user",
        )
    )
    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]
    (tmp_path / "opened").unlink()
    (tmp_path / "sudo_args").unlink()

    # DOAS_USER wins over SUDO_USER and USER
    result = _run_wrapper(
        _root_env(
            tmp_path,
            DOAS_USER=FAKE_SUDO_USER,
            SUDO_USER="unknown.user",
            USER="unknown.user",
        )
    )
    _assert_browser_started(tmp_path, result)
    assert _sudo_args(tmp_path)[:2] == ["-u", f"#{FAKE_UID}"]
    (tmp_path / "opened").unlink()
    (tmp_path / "sudo_args").unlink()

    # ... and not the other way round: the unknown DOAS_USER decides and fails
    result = _run_wrapper(
        _root_env(tmp_path, DOAS_USER="unknown.user", SUDO_USER=FAKE_SUDO_USER)
    )
    _assert_rejected(tmp_path, result, "Cannot get UID for user")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
@pytest.mark.parametrize(
    "sudo_uid, message",
    [
        (str(FAKE_UID), None),
        ("abc", "cannot determine the real user's UID from SUDO_UID"),
    ],
    ids=["valid-uid", "bad-uid"],
)
def test_root_ignores_inherited_log_file(tmp_path, sudo_uid, message):
    """Messages logged as root must not land in a file named by the environment:
    the wrapper's own log file is chosen only once the real user is known."""
    _fake_root_tools(tmp_path)
    evil = tmp_path / "evil.log"

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=sudo_uid, LOG_FILE=str(evil)))

    assert not evil.exists(), "the inherited LOG_FILE was written to"
    if message is None:
        _assert_browser_started(tmp_path, result)
    else:
        _assert_rejected(tmp_path, result, message)


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the USER path")
@pytest.mark.parametrize(
    "user",
    ["unknown.user", "x; touch {pwned}"],
    ids=["unknown-user", "shell-injection"],
)
def test_root_rejects_unknown_user(tmp_path, user):
    _fake_root_tools(tmp_path)
    _fake_id_by_name(tmp_path)  # fails for every name except the known one

    result = _run_wrapper(_root_env(tmp_path, USER=user.format(pwned=tmp_path / "pwned")))

    _assert_rejected(tmp_path, result, "Cannot get UID for user")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
@pytest.mark.parametrize(
    "var, value",
    [
        ("XDG_CURRENT_DESKTOP", "KDE"),
        ("KDE_FULL_SESSION", "true"),
        ("KDE_SESSION_VERSION", "6"),
    ],
)
def test_root_passes_desktop_variables_through_sudo(tmp_path, var, value):
    """sudo resets the environment, but xdg-open needs these to detect KDE. No
    session process is running for the fake uid, so the value must come from the
    wrapper's own environment and be handed to the browser explicitly."""
    _fake_root_tools(tmp_path, reset_env=True)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(FAKE_UID), **{var: value}))

    _assert_browser_started(tmp_path, result)
    assert _browser_env(tmp_path).get(var) == value


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
def test_root_sets_no_desktop_variables_that_it_does_not_know(tmp_path):
    _fake_root_tools(tmp_path, reset_env=True)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(FAKE_UID)))

    _assert_browser_started(tmp_path, result)
    env = _browser_env(tmp_path)
    for var in ("XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION", "KDE_SESSION_VERSION"):
        assert var not in env


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the SUDO_UID path")
def test_root_rejects_unknown_uid(tmp_path):
    _fake_root_tools(tmp_path)

    result = _run_wrapper(_root_env(tmp_path, SUDO_UID=str(UNKNOWN_UID)))

    _assert_rejected(tmp_path, result, "no passwd entry")


def test_missing_logger_is_not_an_error(tmp_path):
    """Without logger the error still reaches stderr and the exit code is the
    same. PATH holds only date (plus the fake id for the non-root path)."""
    for tool in ("date",):
        (tmp_path / tool).symlink_to(shutil.which(tool))
    _script(tmp_path / "id", "echo abc\n")
    env = {"PATH": str(tmp_path), "GP_AUTH_TIMEOUT": "5", "SUDO_UID": "abc"}

    result = subprocess.run(
        [shutil.which("bash"), WRAPPER, URL],
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cannot determine the real user's UID" in result.stderr
    assert "not found" not in result.stderr


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_session_processes_are_looked_up_once_per_name(tmp_path):
    """session_env_var is called for many variables: pgrep must not run again
    for each of them (one lookup per session process name, plus gpauth)."""
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    # Prints nothing, so no /proc/<pid>/environ is read either
    _script(tmp_path / "pgrep", f'echo "$*" >> {tmp_path}/pgrep_calls\nexit 1\n')

    result = _run_wrapper(_root_env(tmp_path, USER="first.last"))

    _assert_browser_started(tmp_path, result)
    calls = (tmp_path / "pgrep_calls").read_text().splitlines()
    per_name = Counter(call.split()[-1] for call in calls)
    assert all(count == 1 for count in per_name.values()), per_name
    assert set(per_name) == {
        "plasmashell",
        "gnome-shell",
        "gnome-session-binary",
        "kwin_wayland",
        "xfce4-session",
        "cinnamon-session",
        "mate-session",
        "sway",
        "gpauth",
    }


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
@pytest.mark.parametrize("home_has_xauthority", [True, False], ids=["file-exists", "no-file"])
def test_non_root_xauthority_fallback_needs_a_home(tmp_path, home_has_xauthority):
    """~/.Xauthority is the fallback for XAUTHORITY; an unknown home must never
    turn into /.Xauthority."""
    home = tmp_path / "own-home"
    home.mkdir()
    if home_has_xauthority:
        (home / ".Xauthority").write_text("")
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    _script(tmp_path / "getent", "exit 2\n")  # no passwd entry: HOME decides

    result = _run_wrapper(_root_env(tmp_path, HOME=str(home)))

    _assert_browser_started(tmp_path, result)
    expected = str(home / ".Xauthority") if home_has_xauthority else None
    assert _browser_env(tmp_path).get("XAUTHORITY") == expected


@pytest.mark.skipif(IS_ROOT, reason="as root the wrapper runs sudo")
def test_non_root_without_home_sets_no_xauthority(tmp_path):
    _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    _fake_browser(tmp_path)
    _script(tmp_path / "getent", "exit 2\n")

    result = _run_wrapper(_root_env(tmp_path))  # no HOME in the environment

    _assert_browser_started(tmp_path, result)
    assert "XAUTHORITY" not in _browser_env(tmp_path)


# --- The browser profile directory must be private ------------------------------
#
# /tmp is shared: another user could pre-create /tmp/edge-wrapper-<uid> (or a
# symlink) and plant a browser profile (proxy, CA) to attack the SAML login.


def _run_for_profile(tmp_path, **extra):
    """Run the wrapper as the fake user; return (result, base dir used)."""
    _fake_logger(tmp_path)
    _fake_browser(tmp_path)
    if IS_ROOT:
        _fake_root_tools(tmp_path)
        extra["SUDO_UID"] = str(FAKE_UID)
    else:
        _script(tmp_path / "id", f"echo {FAKE_UID}\n")
    result = _run_wrapper(_root_env(tmp_path, **extra))
    _assert_browser_started(tmp_path, result)
    env = _browser_env(tmp_path)
    base = os.path.dirname(env["XDG_CACHE_HOME"])
    assert env["XDG_DATA_HOME"] == base + "/data"
    return result, base


def _profile_stat(tmp_path):
    """(mode, uid) of cache, data and the base directory, taken by the browser."""
    return [line.split() for line in (tmp_path / "base_stat").read_text().splitlines()]


def test_profile_dir_is_private_and_owned_by_the_user(tmp_path):
    _, base = _run_for_profile(tmp_path)

    assert base == f"/tmp/edge-wrapper-{FAKE_UID}"
    assert _profile_stat(tmp_path) == [["700", str(os.geteuid())]] * 3


def test_profile_dir_prefers_a_valid_runtime_dir(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)

    _, base = _run_for_profile(tmp_path, XDG_RUNTIME_DIR=str(runtime))

    assert base == f"{runtime}/edge-wrapper"
    assert _profile_stat(tmp_path) == [["700", str(os.geteuid())]] * 3
    assert not os.path.lexists(f"/tmp/edge-wrapper-{FAKE_UID}")


def _plant_symlink(tmp_path, path):
    target = tmp_path / "planted"
    target.mkdir()
    os.symlink(target, path)
    return target


def _plant_open_dir(tmp_path, path):
    os.mkdir(path, 0o755)
    os.chmod(path, 0o755)


def _plant_file(tmp_path, path):
    with open(path, "w") as handle:
        handle.write("not a directory")


def _plant_foreign_dir(tmp_path, path):
    os.mkdir(path, 0o700)
    os.chown(path, OTHER_UID, OTHER_UID)


PLANTERS = [
    pytest.param(_plant_symlink, id="symlink"),
    pytest.param(_plant_open_dir, id="mode-755"),
    pytest.param(_plant_file, id="regular-file"),
    pytest.param(
        _plant_foreign_dir,
        id="other-owner",
        marks=pytest.mark.skipif(not IS_ROOT, reason="needs root to chown"),
    ),
]


@pytest.mark.parametrize("plant", PLANTERS)
def test_planted_temp_base_is_not_used(tmp_path, plant):
    base_path = f"/tmp/edge-wrapper-{FAKE_UID}"
    assert not os.path.lexists(base_path)
    target = plant(tmp_path, base_path)

    _, base = _run_for_profile(tmp_path)

    assert base != base_path
    assert base.startswith(base_path + ".")
    assert _profile_stat(tmp_path) == [["700", str(os.geteuid())]] * 3
    # Nothing was written into the planted directory or the symlink target
    assert (tmp_path / "planted_listing").read_text() == ""
    if target is not None:
        assert os.listdir(target) == []


@pytest.mark.parametrize(
    "make_runtime",
    [
        pytest.param(lambda tmp: tmp / "missing", id="missing"),
        pytest.param(lambda tmp: _runtime_symlink(tmp), id="symlink"),
        pytest.param(lambda tmp: _runtime_dir(tmp, 0o755), id="mode-755"),
        pytest.param(lambda tmp: _runtime_file(tmp), id="regular-file"),
        pytest.param(
            lambda tmp: _runtime_dir(tmp, 0o700, owner=OTHER_UID),
            id="other-owner",
            marks=pytest.mark.skipif(not IS_ROOT, reason="needs root to chown"),
        ),
    ],
)
def test_bad_runtime_dir_is_not_used(tmp_path, make_runtime):
    runtime = make_runtime(tmp_path)

    _, base = _run_for_profile(tmp_path, XDG_RUNTIME_DIR=str(runtime))

    assert base == f"/tmp/edge-wrapper-{FAKE_UID}"
    assert not os.path.lexists(f"{runtime}/edge-wrapper")
    if os.path.isdir(runtime):
        assert os.listdir(runtime) == []


def test_unusable_runtime_subdir_falls_back_to_tmp_base(tmp_path):
    """A runtime dir we cannot use (read-only under ProtectHome, or blocked)
    falls back to the usual /tmp base, not to a new random directory."""
    runtime = _runtime_dir(tmp_path, 0o700)
    (runtime / "edge-wrapper").write_text("not a directory")

    _, base = _run_for_profile(tmp_path, XDG_RUNTIME_DIR=str(runtime))

    assert base == f"/tmp/edge-wrapper-{FAKE_UID}"
    assert _profile_stat(tmp_path) == [["700", str(os.geteuid())]] * 3


def _runtime_dir(tmp_path, mode, owner=None):
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(mode)
    if owner is not None:
        os.chown(runtime, owner, owner)
    return runtime


def _runtime_symlink(tmp_path):
    real = _runtime_dir(tmp_path, 0o700)
    link = tmp_path / "run-link"
    link.symlink_to(real)
    return link


def _runtime_file(tmp_path):
    path = tmp_path / "run-file"
    path.write_text("")
    return path


# --- The log file lives in the private base directory ---------------------------
#
# /tmp is shared: a log file there could be a symlink planted by another user
# (root would then write into any file) or a file that others can read.


def _run_with_runtime(tmp_path, **extra):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    _run_for_profile(tmp_path, XDG_RUNTIME_DIR=str(runtime), **extra)
    return runtime / "edge-wrapper" / "wrapper.log"


def test_log_file_is_in_the_private_base_and_not_readable_by_others(tmp_path):
    # The wrapper deletes nothing under XDG_RUNTIME_DIR, so the log stays
    log = _run_with_runtime(tmp_path)

    assert (tmp_path / "log_stat").read_text().split() == ["600", str(os.geteuid())]
    assert log.stat().st_mode & 0o077 == 0
    text = log.read_text()
    # The messages from before the base directory exists are in syslog instead
    assert "called with args" not in text
    assert "called with args" in (tmp_path / "syslog").read_text()
    assert "started browser with PID" in text
    assert "done:" in text
    # The browser's stderr goes to the same file
    assert "browser-stderr-marker" in text
    assert not glob.glob("/tmp/edge-wrapper-*.log")


def test_auth_url_query_is_never_logged(tmp_path, monkeypatch):
    # The query can carry the SAML request or a one-time token; the browser
    # still gets the whole URL
    monkeypatch.setattr(
        sys.modules[__name__], "URL", URL + "?SAMLRequest=topsecret&x=1"
    )
    log = _run_with_runtime(tmp_path)

    logged = log.read_text() + (tmp_path / "syslog").read_text()
    assert "topsecret" not in logged
    assert "/saml?<hidden>" in logged


def test_url_without_query_is_logged_unchanged(tmp_path):
    log = _run_with_runtime(tmp_path)

    logged = log.read_text() + (tmp_path / "syslog").read_text()
    assert URL in logged
    assert "<hidden>" not in logged


def _plant_log_symlink(tmp_path, path):
    victim = tmp_path / "victim"
    victim.write_text("original")
    os.symlink(victim, path)
    return victim


def _plant_log_foreign_file(tmp_path, path):
    with open(path, "w") as handle:
        handle.write("original")
    os.chmod(path, 0o666)
    os.chown(path, OTHER_UID, OTHER_UID)
    return None


LOG_PLANTERS = [
    pytest.param(_plant_log_symlink, id="symlink-to-victim"),
    pytest.param(
        _plant_log_foreign_file,
        id="other-owner",
        marks=pytest.mark.skipif(not IS_ROOT, reason="needs root to chown"),
    ),
]


@pytest.mark.parametrize("plant", LOG_PLANTERS)
def test_planted_tmp_log_file_is_not_written(tmp_path, plant):
    old_log = f"/tmp/edge-wrapper-{FAKE_UID}.log"
    assert not os.path.lexists(old_log)
    victim = plant(tmp_path, old_log)

    log = _run_with_runtime(tmp_path)

    assert (tmp_path / "old_log_at_browser").read_text() == "original", (
        "the planted log path was written to"
    )
    if victim is not None:
        assert victim.read_text() == "original"
    assert "started browser with PID" in log.read_text()


@pytest.mark.skipif(not IS_ROOT, reason="needs root to take the privilege-drop path")
@pytest.mark.parametrize("plant", LOG_PLANTERS)
def test_root_never_opens_the_log_file(tmp_path, plant):
    """Every time root calls sudo, it has not touched the planted /tmp path and
    has not created the log file: only the user-side code does that."""
    old_log = f"/tmp/edge-wrapper-{FAKE_UID}.log"
    victim = plant(tmp_path, old_log)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    _fake_root_tools(tmp_path)
    # Replace the fake sudo: before it runs the "user" code, note what root did
    _script(
        tmp_path / "sudo",
        f'printf "%s\\n" "$@" > {tmp_path}/sudo_args\n'
        f"cat {old_log} >> {tmp_path}/planted_at_sudo\n"
        f"[ -e {runtime}/edge-wrapper/wrapper.log ] && "
        f"echo exists >> {tmp_path}/log_at_sudo\n"
        "shift 2\n"
        'exec "$@"\n',
    )

    result = _run_wrapper(
        _root_env(tmp_path, SUDO_UID=str(FAKE_UID), XDG_RUNTIME_DIR=str(runtime))
    )

    _assert_browser_started(tmp_path, result)
    # Two sudo calls: the base directory and the browser
    assert (tmp_path / "planted_at_sudo").read_text() == "original" * 2
    assert not (tmp_path / "log_at_sudo").exists(), "root created the log file"
    assert (tmp_path / "old_log_at_browser").read_text() == "original"
    if victim is not None:
        assert victim.read_text() == "original"
    # Root's own messages went to stderr and syslog instead
    assert "dropping privileges" in result.stderr
    assert "dropping privileges" in (tmp_path / "syslog").read_text()
    assert "called with args" not in (runtime / "edge-wrapper" / "wrapper.log").read_text()

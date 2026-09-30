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
    """Record the URL, the environment and which wrapper log files exist (the
    wrapper removes its own files only after the browser is done)."""
    _script(
        tmp_path / "browser",
        f'echo "$@" > {tmp_path}/opened\n'
        f"env > {tmp_path}/browser_env\n"
        f"ls /tmp/edge-wrapper-*.log > {tmp_path}/wrapper_logs 2>/dev/null\n",
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
    logs = (tmp_path / "wrapper_logs").read_text().splitlines()
    assert f"/tmp/edge-wrapper-{FAKE_UID}.log" in logs
    assert f"/tmp/edge-wrapper-{padded}.log" not in logs
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

    _assert_rejected(tmp_path, result, "SUDO_UID is not a number")


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
        ({"PKEXEC_UID": "abc"}, "PKEXEC_UID is not a number"),
        ({"PKEXEC_UID": ""}, "PKEXEC_UID is not a number"),
        # A set but invalid variable is an error, never a reason to try the next
        ({"SUDO_UID": "abc", "PKEXEC_UID": str(FAKE_UID)}, "SUDO_UID is not a number"),
        (
            {"PKEXEC_UID": "abc", "DOAS_USER": FAKE_SUDO_USER},
            "PKEXEC_UID is not a number",
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
        ("abc", "SUDO_UID is not a number"),
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

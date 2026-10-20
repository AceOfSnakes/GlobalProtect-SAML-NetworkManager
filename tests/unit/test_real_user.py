"""
Tests for finding the desktop user (issue #51).

The service used to take the first non-root user of `loginctl list-users`, so
with fast user switching, a lingering user or a gdm greeter user the browser
opened in another user's session. Now: SUDO_UID, the single user of the
connection's permissions, the active seat0 session, the only user with an
active session (xrdp, remote desktop, VM), else the current user.

`loginctl` is faked through PATH. System users from the container stand in
for desktop users: daemon (1), bin (2) and nobody (65534).
"""

import os
import pwd
import stat
import subprocess
import types

import pytest

FALLBACK_UID = 4711
SEAT_USER = "daemon"
OTHER_USER = "bin"


@pytest.fixture
def plugin(service_module, monkeypatch, tmp_path):
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("USER", "current-user")
    monkeypatch.setenv("HOME", "/home/current-user")
    return service_module.GpclientVPNPlugin()


@pytest.fixture
def fake_loginctl(monkeypatch, tmp_path):
    """Install a fake `loginctl`; returns a function that configures it"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def install(active="c1", session=None, fail=False, sessions=None):
        """sessions: {id: {"User": uid, "Class": ..., "State": ...}} for
        `list-sessions` (the seat0 session c1 is only listed when given)"""
        session = session or {}
        sessions = sessions or {}
        props = {
            "User": str(pwd.getpwnam(SEAT_USER).pw_uid),
            "Name": SEAT_USER,
            "Class": "user",
            "Remote": "no",
            "State": "active",
        }
        props.update(session)
        lines = "\n".join(f"{k}={v}" for k, v in props.items())
        exit_code = 1 if fail else 0
        listing = "\n".join(f"{sid} {p['User']} name seat9" for sid, p in sessions.items())
        script = bindir / "loginctl"
        script.write_text(
            "#!/bin/sh\n"
            f"[ {exit_code} -ne 0 ] && exit 1\n"
            'case "$1" in\n'
            f"  show-seat) echo '{active}' ;;\n"
            f"  list-sessions) cat <<'EOT'\n{listing}\nEOT\n ;;\n"
            f"  show-session) case \"$2\" in\n"
            f"  c1) cat <<'EOT'\n{lines}\nEOT\n ;;\n"
            + "".join(
                f"  {sid}) cat <<'EOT'\n"
                + "\n".join(
                    f"{k}={v}"
                    for k, v in {"Class": "user", "State": "active", **p}.items()
                )
                + "\nEOT\n ;;\n"
                for sid, p in sessions.items()
            )
            + "  esac ;;\n"
            f"  list-users) echo '{pwd.getpwnam(OTHER_USER).pw_uid} {OTHER_USER}' ;;\n"
            "esac\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    return install


def seat_user():
    pw = pwd.getpwnam(SEAT_USER)
    return pw.pw_uid, pw.pw_name, pw.pw_dir


def current_user():
    return os.getuid(), "current-user", "/home/current-user"


class TestSeat0Session:
    def test_active_seat0_user_wins_over_list_users(self, plugin, fake_loginctl):
        fake_loginctl()

        assert plugin._get_real_user() == seat_user()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"session": {"Class": "greeter"}},
            {"session": {"Remote": "yes"}},
            {"session": {"State": "online"}},
            {"session": {"State": "closing"}},
            {"session": {"User": "0", "Name": "root"}},
            {"session": {"User": "not-a-number"}},
            {"active": ""},
            {"fail": True},
        ],
        ids=[
            "greeter",
            "remote",
            "online",
            "closing",
            "root",
            "bad-uid",
            "no-active-session",
            "loginctl-fails",
        ],
    )
    def test_unusable_session_gives_current_user(self, plugin, fake_loginctl, kwargs):
        fake_loginctl(**kwargs)

        # Never another logged-in user, such as the one list-users shows
        assert plugin._get_real_user() == current_user()

    def test_missing_loginctl_gives_current_user(self, plugin, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))

        assert plugin._get_real_user() == current_user()


class TestPermissionUser:
    @pytest.mark.parametrize(
        "permissions",
        [
            ["user:bin:"],
            ["user:bin"],
            ("as", ["user:bin:"]),
        ],
    )
    def test_single_permission_user_wins_over_seat0(
        self, plugin, fake_loginctl, permissions
    ):
        fake_loginctl()
        plugin._permission_user = plugin._parse_permission_user(permissions)

        pw = pwd.getpwnam(OTHER_USER)
        assert plugin._get_real_user() == (pw.pw_uid, pw.pw_name, pw.pw_dir)

    @pytest.mark.parametrize(
        "permissions",
        [
            ["user:bin:", "user:daemon:"],
            [],
            ["group:wheel:"],
            ["user:no-such-user-xyz:"],
            ["user:root:"],
            "user:bin:",
            None,
        ],
        ids=["two-users", "none", "no-user-entry", "nonexistent", "root", "str", "none-type"],
    )
    def test_other_permissions_fall_through_to_seat0(
        self, plugin, fake_loginctl, permissions
    ):
        fake_loginctl()
        plugin._permission_user = plugin._parse_permission_user(permissions)

        assert plugin._get_real_user() == seat_user()

    @pytest.mark.parametrize(
        "permissions, expected",
        [
            (["user:bin:"], "bin"),
            (["user:bin", "user:bin:"], "bin"),
            (["user:bin:", "user:daemon:"], ""),
            ([], ""),
            ("user:bin:", ""),
        ],
    )
    def test_parse_permission_user(self, plugin, permissions, expected):
        assert plugin._parse_permission_user(permissions) == expected


class TestSudoUser:
    def test_sudo_uid_still_wins(self, plugin, fake_loginctl, monkeypatch):
        fake_loginctl()
        pw = pwd.getpwnam(OTHER_USER)
        plugin._permission_user = SEAT_USER
        monkeypatch.setenv("SUDO_UID", str(pw.pw_uid))
        monkeypatch.setenv("SUDO_USER", OTHER_USER)

        assert plugin._get_real_user() == (pw.pw_uid, OTHER_USER, pw.pw_dir)

    def test_sudo_uid_needs_both_variables(self, plugin, fake_loginctl, monkeypatch):
        fake_loginctl()
        monkeypatch.setenv("SUDO_UID", "2")

        assert plugin._get_real_user() == seat_user()


OTHER_UID = pwd.getpwnam(OTHER_USER).pw_uid
SEAT_UID = pwd.getpwnam(SEAT_USER).pw_uid
NOBODY_UID = pwd.getpwnam("nobody").pw_uid


def other_user():
    pw = pwd.getpwnam(OTHER_USER)
    return pw.pw_uid, pw.pw_name, pw.pw_dir


class TestOtherActiveSession:
    """seat0 has no usable session (xrdp, remote desktop, VM)"""

    def test_single_active_remote_user_is_chosen(self, plugin, fake_loginctl):
        fake_loginctl(
            active="", sessions={"c5": {"User": OTHER_UID, "Remote": "yes"}}
        )

        assert plugin._get_real_user() == other_user()

    def test_the_same_user_in_two_sessions_is_one_user(self, plugin, fake_loginctl):
        fake_loginctl(
            active="",
            sessions={"c5": {"User": OTHER_UID}, "c6": {"User": OTHER_UID}},
        )

        assert plugin._get_real_user() == other_user()

    def test_seat0_wins_over_other_sessions(self, plugin, fake_loginctl):
        fake_loginctl(sessions={"c5": {"User": OTHER_UID}})

        assert plugin._get_real_user() == seat_user()

    def test_remote_seat0_session_alone_is_chosen_too(self, plugin, fake_loginctl):
        fake_loginctl(
            session={"Remote": "yes"},
            sessions={"c1": {"User": SEAT_UID, "Remote": "yes"}},
        )

        assert plugin._get_real_user() == seat_user()

    @pytest.mark.parametrize(
        "sessions",
        [
            {"c5": {"User": OTHER_UID}, "c6": {"User": NOBODY_UID}},
            {"c5": {"User": OTHER_UID, "Remote": "yes"}, "c6": {"User": SEAT_UID}},
        ],
        ids=["two-local-users", "remote-and-local-users"],
    )
    def test_two_different_users_give_current_user(
        self, plugin, fake_loginctl, sessions
    ):
        fake_loginctl(active="", sessions=sessions)

        # Never one of them: we cannot tell which session is meant
        assert plugin._get_real_user() == current_user()

    @pytest.mark.parametrize(
        "sessions",
        [
            {"c5": {"User": OTHER_UID, "Class": "greeter"}},
            {"c5": {"User": OTHER_UID, "State": "closing"}},
            {"c5": {"User": OTHER_UID, "State": "online"}},
            {"c5": {"User": 0}},
            {"c5": {"User": "not-a-number"}},
            {},
        ],
        ids=["greeter", "closing", "online", "root", "bad-uid", "no-sessions"],
    )
    def test_no_usable_session_gives_current_user(
        self, plugin, fake_loginctl, sessions
    ):
        fake_loginctl(active="", sessions=sessions)

        assert plugin._get_real_user() == current_user()

    def test_unusable_sessions_do_not_hide_the_one_good_user(
        self, plugin, fake_loginctl
    ):
        fake_loginctl(
            active="",
            sessions={
                "c4": {"User": SEAT_UID, "Class": "greeter"},
                "c5": {"User": NOBODY_UID, "State": "closing"},
                "c6": {"User": 0},
                "c7": {"User": OTHER_UID, "Remote": "yes"},
            },
        )

        assert plugin._get_real_user() == other_user()


# --- Session environment helpers (_read_proc_environ, _scan_session_env,
# _get_session_env) ---


class TestReadProcEnviron:
    @pytest.fixture
    def proc(self, service_module, monkeypatch, tmp_path):
        monkeypatch.setattr(service_module, "PROC_PATH", str(tmp_path))

        def write(pid, raw):
            directory = tmp_path / str(pid)
            directory.mkdir(exist_ok=True)
            (directory / "environ").write_bytes(raw)

        return write

    def test_parses_entries(self, plugin, proc):
        proc(5, b"A=1\0B=two=2\0C=\0")

        assert plugin._read_proc_environ("5") == {"A": "1", "B": "two=2", "C": ""}

    def test_entries_without_equals_and_empty_entries_are_ignored(self, plugin, proc):
        proc(5, b"\0NOEQUALS\0\0A=1\0")

        assert plugin._read_proc_environ("5") == {"A": "1"}

    def test_invalid_utf8_is_replaced(self, plugin, proc):
        proc(5, b"K\xff=v\xfe\0")

        assert plugin._read_proc_environ("5") == {"K�": "v�"}

    def test_empty_file_gives_empty_dict(self, plugin, proc):
        proc(5, b"")

        assert plugin._read_proc_environ("5") == {}

    def test_missing_process_gives_empty_dict(self, plugin, proc):
        assert plugin._read_proc_environ("99999") == {}


class TestScanSessionEnvBranches:
    @pytest.fixture
    def fake_proc(self, service_module, monkeypatch, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        monkeypatch.setattr(service_module, "PROC_PATH", str(proc))
        # The owner of the fake processes is whoever runs the test: make the
        # check independent of that by faking the uid we ask for.
        owner = os.stat(proc).st_uid

        def add(pid, environ, comm="app"):
            directory = proc / str(pid)
            directory.mkdir()
            (directory / "environ").write_bytes(
                b"".join(f"{k}={v}".encode() + b"\0" for k, v in environ.items())
            )
            if comm is not None:
                (directory / "comm").write_text(comm + "\n")

        add.owner = owner
        add.root = proc
        return add

    def test_non_numeric_entries_of_proc_are_ignored(
        self, plugin, fake_proc
    ):
        fake_proc.root.joinpath("self").mkdir()
        (fake_proc.root / "self" / "environ").write_bytes(b"DISPLAY=:9\0")

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}

    def test_first_process_with_a_display_wins(self, plugin, fake_proc, caplog):
        fake_proc(100, {"XDG_RUNTIME_DIR": "/x"})
        fake_proc(200, {"DISPLAY": ":1", "XDG_SESSION_TYPE": "x11"}, comm="xterm")
        fake_proc(300, {"DISPLAY": ":2", "XDG_CURRENT_DESKTOP": "Other"})
        caplog.set_level("INFO")

        found = plugin._scan_session_env(fake_proc.owner, {})

        # Values come from one process only, never mixed; pid 100 has no display
        assert found in (
            {"DISPLAY": ":1", "XDG_SESSION_TYPE": "x11"},
            {"DISPLAY": ":2", "XDG_CURRENT_DESKTOP": "Other"},
        )
        taken = "xterm (200)" if found["DISPLAY"] == ":1" else "app (300)"
        assert taken in caplog.text

    def test_wayland_display_alone_counts(self, plugin, fake_proc):
        fake_proc(100, {"WAYLAND_DISPLAY": "wayland-0"})

        assert plugin._scan_session_env(fake_proc.owner, {}) == {
            "WAYLAND_DISPLAY": "wayland-0"
        }

    def test_process_that_cannot_be_stat_ed_is_skipped(
        self, service_module, plugin, fake_proc, monkeypatch
    ):
        fake_proc(100, {"DISPLAY": ":0"})

        def boom(_path):
            raise FileNotFoundError("gone")

        monkeypatch.setattr(service_module.os, "stat", boom)

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}

    def test_unreadable_comm_is_logged_as_unknown(self, plugin, fake_proc, caplog):
        fake_proc(100, {"DISPLAY": ":0"}, comm=None)
        caplog.set_level("INFO")

        found = plugin._scan_session_env(fake_proc.owner, {})

        assert found == {"DISPLAY": ":0"}
        assert "? (100)" in caplog.text

    def test_unreadable_environ_is_skipped(self, plugin, fake_proc):
        fake_proc(100, {"DISPLAY": ":0"})
        (fake_proc.root / "100" / "environ").unlink()

        assert plugin._scan_session_env(fake_proc.owner, {}) == {}


class TestGetSessionEnvBranches:
    @pytest.fixture
    def no_extras(self, service_module, monkeypatch, tmp_path):
        """No fallbacks from the filesystem and no scan of all processes"""
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_scan_session_env",
            lambda self, uid, found: {},
        )
        monkeypatch.setattr(service_module.os.path, "isdir", lambda p: False)
        monkeypatch.setattr(service_module.os.path, "exists", lambda p: False)
        return str(tmp_path)

    def test_stops_asking_once_every_key_is_known(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        full = {key: f"v-{key}" for key in service_module.SESSION_ENV_KEYS}
        asked = []

        def run(cmd, **kw):
            asked.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "11 12\n", "")

        monkeypatch.setattr(service_module.subprocess, "run", run)
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: dict(full)),
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == full
        assert asked == [service_module.SESSION_LEADER_PROCESSES[0]]

    def test_keeps_asking_when_keys_are_missing(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        asked = []

        def run(cmd, **kw):
            asked.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "11\n", "")

        monkeypatch.setattr(service_module.subprocess, "run", run)
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: {"DISPLAY": ":0"}),
        )

        plugin._get_session_env(FALLBACK_UID, no_extras)

        assert asked == list(service_module.SESSION_LEADER_PROCESSES)

    def test_first_process_wins_for_each_key(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        environs = {
            "11": {"DISPLAY": ":0"},
            "12": {"DISPLAY": ":9", "XDG_SESSION_TYPE": "x11", "XAUTHORITY": ""},
        }
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0 if cmd[-1] == "gnome-shell" else 1, "11 12\n", ""
            ),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: environs[pid]),
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError("pgrep"),
            subprocess.TimeoutExpired(["pgrep"], 2),
        ],
    )
    def test_pgrep_failure_is_survived(
        self, service_module, plugin, monkeypatch, no_extras, error
    ):
        def run(cmd, **kw):
            raise error

        monkeypatch.setattr(service_module.subprocess, "run", run)

        assert plugin._get_session_env(FALLBACK_UID, no_extras) == {}

    def test_unreadable_process_environment_gives_nothing(
        self, service_module, plugin, monkeypatch, no_extras
    ):
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "11\n", ""),
        )
        monkeypatch.setattr(
            service_module.GpclientVPNPlugin,
            "_read_proc_environ",
            staticmethod(lambda pid: {}),
        )

        assert plugin._get_session_env(FALLBACK_UID, no_extras) == {}

    @pytest.mark.parametrize("present", [True, False])
    def test_xauthority_in_home_is_used_when_present(
        self, service_module, plugin, monkeypatch, no_extras, present
    ):
        monkeypatch.setattr(
            service_module.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""),
        )
        xauthority = f"{no_extras}/.Xauthority"
        monkeypatch.setattr(
            service_module.os.path, "exists", lambda p: present and p == xauthority
        )

        env = plugin._get_session_env(FALLBACK_UID, no_extras)

        assert env == ({"XAUTHORITY": xauthority} if present else {})

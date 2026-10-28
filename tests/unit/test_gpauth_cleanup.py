"""
Tests for stopping only the gpauth of OUR gpclient (issue #52).

The service used to run `pkill -9 -u <uid> gpauth`, which also killed the gpauth
of a gpclient the user started from a terminal or of the GUI. It now notes the
gpauth descendants of its own gpclient (with their start times) and signals
exactly those, only while the PID still has the noted start time.

A fake /proc tree (PROC_PATH) and a recording send_signal replace the real
system: no test signals a real process.
"""

import asyncio
import signal

import pytest

OUR_GPCLIENT = 100
STARTED = 5000


@pytest.fixture
def proc_root(service_module, monkeypatch, tmp_path):
    """A fake /proc; add processes with add(pid, comm, ppid, starttime)"""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(service_module, "PROC_PATH", str(root))

    class Tree:
        path = root

        @staticmethod
        def stat_line(pid, comm, ppid, starttime, state="S"):
            # fields 4..21 are filler: ppid is field 4, starttime field 22
            filler = " ".join(["0"] * 17)
            return f"{pid} ({comm}) {state} {ppid} {filler} {starttime} 0 0\n"

        def add(self, pid, comm, ppid, starttime=STARTED, state="S"):
            (root / str(pid)).mkdir(exist_ok=True)
            (root / str(pid) / "stat").write_text(
                self.stat_line(pid, comm, ppid, starttime, state)
            )

        def remove(self, pid):
            (root / str(pid) / "stat").unlink()
            (root / str(pid)).rmdir()

    return Tree()


@pytest.fixture
def kills(service_module, monkeypatch, proc_root):
    """Recorder for send_signal(); a process 'exits' on SIGTERM unless its pid
    is in `survivors`, and always on SIGKILL"""
    class Sent(list):
        survivors = set()

    sent = Sent()
    survivors = sent.survivors

    def send(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL or pid not in survivors:
            proc_root.remove(pid)

    monkeypatch.setattr(service_module, "send_signal", send)
    return sent


@pytest.fixture
def our_tree(proc_root):
    """gpclient (100) -> gpauth (101) -> browser (102); gpclient -> sh (103)
    -> gpauth (104). Foreign: CLI gpclient (200) -> gpauth (201); GUI
    gpservice (300) -> gpauth (301); a gpauth of init (1 -> 400)."""
    proc_root.add(1, "systemd", 0)
    proc_root.add(OUR_GPCLIENT, "gpclient", 1)
    proc_root.add(101, "gpauth", OUR_GPCLIENT)
    proc_root.add(102, "msedge", 101)
    proc_root.add(103, "sh", OUR_GPCLIENT)
    proc_root.add(104, "gpauth", 103)
    proc_root.add(200, "gpclient", 1)
    proc_root.add(201, "gpauth", 200)
    proc_root.add(300, "gpservice", 1)
    proc_root.add(301, "gpauth", 300)
    proc_root.add(400, "gpauth", 1)
    return proc_root


# --- parse_proc_stat ------------------------------------------------------------


def _line(comm, ppid=7, starttime=99, state="S"):
    return f"5 ({comm}) {state} {ppid} " + " ".join(["0"] * 17) + f" {starttime} 0\n"


class TestParseProcStat:
    @pytest.mark.parametrize(
        "comm", ["gpauth", "my prog", "a) b", "x (y)", "((", ")", "a b c d"]
    )
    def test_comm_with_spaces_and_parentheses(self, service_module, comm):
        assert service_module.parse_proc_stat(_line(comm)) == (comm, "S", 7, 99)

    def test_state_is_returned(self, service_module):
        assert service_module.parse_proc_stat(_line("x", state="Z"))[1] == "Z"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "garbage",
            "5 gpauth S 7 0 0",  # no parentheses
            "5 ) gpauth (S 7 " + " ".join(["0"] * 20),  # parentheses reversed
            "5 (gpauth) S 7 0 0 0",  # too few fields
            "5 (gpauth) S x " + " ".join(["0"] * 18),  # ppid is not a number
            "5 (gpauth) S 7 " + " ".join(["0"] * 17) + " y 0",  # starttime
        ],
    )
    def test_malformed_lines_are_skipped(self, service_module, text):
        assert service_module.parse_proc_stat(text) is None


# --- gpauth_descendants ---------------------------------------------------------


class TestGpauthDescendants:
    def test_finds_gpauth_of_our_gpclient_at_any_depth(
        self, service_module, our_tree
    ):
        found = service_module.gpauth_descendants(OUR_GPCLIENT)
        assert sorted(found) == [(101, STARTED), (104, STARTED)]

    @pytest.mark.parametrize("root", [200, 300])
    def test_foreign_trees_are_not_ours(self, service_module, our_tree, root):
        # The CLI gpclient and the GUI gpservice have gpauth of their own,
        # none of their gpauth is in our result
        ours = {pid for pid, _ in service_module.gpauth_descendants(OUR_GPCLIENT)}
        theirs = {pid for pid, _ in service_module.gpauth_descendants(root)}
        assert not ours & theirs

    def test_gpauth_of_other_parents_is_never_returned(
        self, service_module, our_tree
    ):
        pids = {pid for pid, _ in service_module.gpauth_descendants(OUR_GPCLIENT)}
        assert not pids & {201, 301, 400}

    def test_other_descendants_are_not_returned(self, service_module, our_tree):
        pids = {pid for pid, _ in service_module.gpauth_descendants(OUR_GPCLIENT)}
        assert not pids & {102, 103, OUR_GPCLIENT}

    def test_comm_must_be_exactly_gpauth(self, service_module, proc_root):
        proc_root.add(OUR_GPCLIENT, "gpclient", 1)
        for pid, comm in ((101, "gpauth-helper"), (102, "xgpauth"), (103, "GPAUTH")):
            proc_root.add(pid, comm, OUR_GPCLIENT)
        assert service_module.gpauth_descendants(OUR_GPCLIENT) == []

    def test_malformed_and_unreadable_entries_are_skipped(
        self, service_module, proc_root
    ):
        proc_root.add(OUR_GPCLIENT, "gpclient", 1)
        proc_root.add(101, "gpauth", OUR_GPCLIENT)
        (proc_root.path / "102").mkdir()
        (proc_root.path / "102" / "stat").write_text("garbage")
        (proc_root.path / "103").mkdir()  # no stat at all
        (proc_root.path / "self").mkdir()  # not a PID
        assert service_module.gpauth_descendants(OUR_GPCLIENT) == [(101, STARTED)]

    def test_a_parent_cycle_ends(self, service_module, proc_root):
        proc_root.add(OUR_GPCLIENT, "gpclient", 101)
        proc_root.add(101, "gpauth", OUR_GPCLIENT)
        assert service_module.gpauth_descendants(OUR_GPCLIENT) == [(101, STARTED)]

    def test_a_missing_proc_gives_nothing(
        self, service_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(service_module, "PROC_PATH", str(tmp_path / "none"))
        assert service_module.gpauth_descendants(OUR_GPCLIENT) == []


# --- is_same_process and signal_if_same -----------------------------------------


class TestSameProcessGuard:
    def test_same_start_time_is_the_same_process(self, service_module, proc_root):
        proc_root.add(101, "gpauth", 1, starttime=STARTED)
        assert service_module.is_same_process(101, STARTED)

    @pytest.mark.parametrize("state", ["Z", "X"])
    def test_a_dead_process_is_gone(self, service_module, proc_root, state):
        proc_root.add(101, "gpauth", 1, starttime=STARTED, state=state)
        assert not service_module.is_same_process(101, STARTED)

    def test_a_reused_pid_is_another_process(self, service_module, proc_root):
        proc_root.add(101, "gpauth", 1, starttime=STARTED + 1)
        assert not service_module.is_same_process(101, STARTED)

    def test_a_missing_pid_is_gone(self, service_module, proc_root):
        assert not service_module.is_same_process(101, STARTED)

    def test_signal_is_sent_to_the_same_process(
        self, service_module, proc_root, kills
    ):
        proc_root.add(101, "gpauth", 1)
        assert service_module.signal_if_same(101, STARTED, signal.SIGTERM)
        assert kills == [(101, signal.SIGTERM)]

    def test_no_signal_for_a_reused_pid(self, service_module, proc_root, kills):
        proc_root.add(101, "msedge", 1, starttime=STARTED + 1)
        assert not service_module.signal_if_same(101, STARTED, signal.SIGKILL)
        assert kills == []

    def test_a_process_that_vanished_in_between_is_not_an_error(
        self, service_module, proc_root, monkeypatch
    ):
        proc_root.add(101, "gpauth", 1)

        def gone(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(service_module, "send_signal", gone)
        assert not service_module.signal_if_same(101, STARTED, signal.SIGTERM)


# --- _remember_gpauth / _stop_remembered_gpauth ---------------------------------


class _Gpclient:
    def __init__(self, pid=OUR_GPCLIENT, returncode=None):
        self.pid = pid
        self.returncode = returncode


def _plugin(service_module, returncode=None):
    plugin = service_module.GpclientVPNPlugin()
    plugin.gpclient_process = _Gpclient(returncode=returncode)
    return plugin


@pytest.fixture
def quick(service_module, monkeypatch):
    monkeypatch.setattr(service_module, "GPAUTH_TERMINATE_TIMEOUT", 0.3)


class TestRemember:
    def test_notes_only_the_gpauth_of_our_gpclient(self, service_module, our_tree):
        plugin = _plugin(service_module)
        asyncio.run(plugin._remember_gpauth())
        assert sorted(plugin._gpauth_leftovers) == [(101, STARTED), (104, STARTED)]

    def test_notes_each_process_once(self, service_module, our_tree):
        plugin = _plugin(service_module)

        async def twice():
            await plugin._remember_gpauth()
            await plugin._remember_gpauth()

        asyncio.run(twice())
        assert len(plugin._gpauth_leftovers) == 2

    @pytest.mark.parametrize("process", [None, object(), _Gpclient(returncode=0)])
    def test_notes_nothing_without_a_running_gpclient(
        self, service_module, our_tree, process
    ):
        plugin = service_module.GpclientVPNPlugin()
        plugin.gpclient_process = process
        asyncio.run(plugin._remember_gpauth())
        assert plugin._gpauth_leftovers == []

    def test_a_failing_proc_walk_is_not_fatal(
        self, service_module, our_tree, monkeypatch
    ):
        def boom(_pid):
            raise RuntimeError("boom")

        monkeypatch.setattr(service_module, "gpauth_descendants", boom)
        plugin = _plugin(service_module)
        asyncio.run(plugin._remember_gpauth())
        assert plugin._gpauth_leftovers == []

    def test_the_proc_walk_runs_off_the_event_loop(
        self, service_module, our_tree, monkeypatch
    ):
        threads = []
        real = service_module.gpauth_descendants

        def spy(pid):
            import threading

            threads.append(threading.current_thread())
            return real(pid)

        monkeypatch.setattr(service_module, "gpauth_descendants", spy)
        import threading

        asyncio.run(_plugin(service_module)._remember_gpauth())
        assert threads and threads[0] is not threading.main_thread()


class TestStopRemembered:
    def test_terminates_our_gpauth_and_nothing_else(
        self, service_module, our_tree, kills, quick
    ):
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())

        assert sorted(kills) == [(101, signal.SIGTERM), (104, signal.SIGTERM)]
        # not the foreign gpauth, nor the browser, nor any gpclient/gpservice
        assert {pid for pid, _ in kills}.isdisjoint(
            {102, 103, OUR_GPCLIENT, 200, 201, 300, 301, 400}
        )
        assert plugin._gpauth_leftovers == []

    def test_survivors_get_sigkill_after_the_timeout(
        self, service_module, our_tree, kills, quick
    ):
        kills.survivors.add(101)
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())

        assert (101, signal.SIGTERM) in kills
        assert (101, signal.SIGKILL) in kills
        assert (104, signal.SIGKILL) not in kills  # it exited on SIGTERM

    def test_no_sigkill_when_everything_exits(
        self, service_module, our_tree, kills, quick
    ):
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())
        assert all(sig == signal.SIGTERM for _, sig in kills)

    def test_a_reused_pid_is_not_signalled(
        self, service_module, our_tree, kills, quick
    ):
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            # gpauth 101 exited and another process took its PID
            our_tree.add(101, "gpauth", 1, starttime=STARTED + 77)
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())

        assert kills == [(104, signal.SIGTERM)]

    def test_a_pid_reused_while_waiting_is_not_killed(
        self, service_module, our_tree, monkeypatch, quick
    ):
        sent = []

        def send(pid, sig):
            sent.append((pid, sig))
            # ignores SIGTERM, then dies and its PID is reused at once
            our_tree.add(pid, "other", 1, starttime=STARTED + 5)

        monkeypatch.setattr(service_module, "send_signal", send)
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())
        assert all(sig == signal.SIGTERM for _, sig in sent)

    def test_a_zombie_counts_as_gone(
        self, service_module, our_tree, monkeypatch, quick
    ):
        sent = []

        def send(pid, sig):
            sent.append((pid, sig))
            our_tree.add(pid, "gpauth", 103, starttime=STARTED, state="Z")

        monkeypatch.setattr(service_module, "send_signal", send)
        plugin = _plugin(service_module)

        async def go():
            await plugin._remember_gpauth()
            await plugin._stop_remembered_gpauth()

        asyncio.run(go())
        assert all(sig == signal.SIGTERM for _, sig in sent)

    def test_nothing_remembered_signals_nothing(
        self, service_module, our_tree, kills
    ):
        plugin = _plugin(service_module)
        asyncio.run(plugin._stop_remembered_gpauth())
        assert kills == []

    def test_a_signal_error_is_not_fatal(
        self, service_module, our_tree, monkeypatch, quick
    ):
        def denied(pid, sig):
            raise PermissionError(pid)

        monkeypatch.setattr(service_module, "send_signal", denied)
        plugin = _plugin(service_module)
        plugin._gpauth_leftovers = [(101, STARTED)]
        asyncio.run(plugin._stop_remembered_gpauth())
        assert plugin._gpauth_leftovers == []

    def test_an_unexpected_error_is_logged_not_raised(
        self, service_module, our_tree, monkeypatch, caplog
    ):
        def boom(*_args):
            raise RuntimeError("boom")

        monkeypatch.setattr(service_module, "signal_if_same", boom)
        plugin = _plugin(service_module)
        plugin._gpauth_leftovers = [(101, STARTED)]
        with caplog.at_level("ERROR", logger=service_module.logger.name):
            asyncio.run(plugin._stop_remembered_gpauth())
        assert "Error stopping gpauth" in caplog.text


# --- Disconnect and _start_gpclient ---------------------------------------------


def test_disconnect_terminates_the_gpauth_of_our_gpclient(
    service_module, our_tree, kills, quick, dbus_signals, monkeypatch
):
    plugin = _plugin(service_module)

    async def stop():
        # gpclient ends and its children go to init, as in real life
        for pid in (101, 103):
            our_tree.add(pid, "gpauth" if pid == 101 else "sh", 1)
        plugin.gpclient_process.returncode = 0

    monkeypatch.setattr(plugin, "_stop_gpclient", stop)

    asyncio.run(plugin.Disconnect())

    assert (101, signal.SIGTERM) in kills
    assert (104, signal.SIGTERM) in kills
    assert {pid for pid, _ in kills}.isdisjoint({102, 103, 201, 301, 400})


def test_disconnect_without_gpauth_signals_nothing(
    service_module, proc_root, kills, dbus_signals
):
    proc_root.add(OUR_GPCLIENT, "gpclient", 1)
    proc_root.add(201, "gpauth", 1)  # somebody else's
    plugin = _plugin(service_module)

    asyncio.run(plugin.Disconnect())

    assert kills == []


class TestStartGpclientCleanup:
    @pytest.fixture
    def start(self, service_module, monkeypatch, tmp_path):
        """_start_gpclient up to the point where it would start gpclient"""
        monkeypatch.setattr(
            service_module, "GPCLIENT_BINARY", str(tmp_path / "does-not-exist")
        )
        calls = []
        monkeypatch.setattr(
            service_module.subprocess, "run", lambda *a, **k: calls.append(a)
        )

        def go(plugin):
            monkeypatch.setattr(
                plugin, "_get_real_user", lambda: (0, "root", "/root")
            )
            plugin.gateway = "portal.example.com"
            plugin.browser = "/bin/true"
            asyncio.run(plugin._start_gpclient())
            return calls

        return go

    def test_no_blanket_pkill_of_gpauth(
        self, service_module, our_tree, kills, start
    ):
        calls = start(service_module.GpclientVPNPlugin())
        assert calls == []
        assert kills == []  # no leftovers noted: nothing is signalled

    def test_leftovers_of_an_earlier_session_are_cleaned_with_the_guard(
        self, service_module, our_tree, kills, quick, start
    ):
        plugin = service_module.GpclientVPNPlugin()
        # remembered: 101 is still the same, 104's PID was reused since
        our_tree.add(104, "gpauth", 1, starttime=STARTED + 9)
        plugin._gpauth_leftovers = [(101, STARTED), (104, STARTED)]

        start(plugin)

        assert kills == [(101, signal.SIGTERM)]
        assert plugin._gpauth_leftovers == []

    def test_foreign_gpauth_survives_the_start(
        self, service_module, our_tree, kills, quick, start
    ):
        plugin = service_module.GpclientVPNPlugin()
        plugin._gpauth_leftovers = [(101, STARTED)]

        start(plugin)

        assert {pid for pid, _ in kills}.isdisjoint({201, 301, 400})

"""
PTY-level tests for answering gpclient's gateway list (issue #7).

These drive the real output pipeline - OutputScanner, the raw line buffer, the
debounced prompt check and the keystrokes written back - against a stand-in for
gpclient that renders an inquire Select frame and reacts to arrow keys.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import os
import pty
import sys

import pytest

FAKE_GPCLIENT = r'''
import os, sys, tty

OPTIONS = [
    "gw-warsaw (gw1.example.com)",
    "gw-frankfurt (gw2.example.com)",
    "gw-london (gw3.example.com)",
]


def render(cursor):
    lines = ["? Which gateway do you want to connect to?"]
    for index, option in enumerate(OPTIONS):
        lines.append(("> " if index == cursor else "  ") + option)
    lines.append("[↑↓ to move, enter to select, type to filter]")
    sys.stdout.write("\r\n".join(lines) + "\r\n")
    sys.stdout.flush()


tty.setraw(0)
sys.stdout.write("[INFO  gpclient::cli] gpclient started: fake\r\n")
sys.stdout.flush()

cursor = 0
render(cursor)

pending = b""
while True:
    chunk = os.read(0, 16)
    if not chunk:
        break
    pending += chunk
    while pending:
        if pending.startswith(b"\x1b[B"):
            pending = pending[3:]
            cursor = (cursor + 1) % len(OPTIONS)
            render(cursor)
        elif pending[:1] in (b"\r", b"\n"):
            pending = pending[1:]
            sys.stdout.write(
                "[INFO  gpclient::connect] Connecting to the selected gateway: %s\r\n"
                % OPTIONS[cursor]
            )
            sys.stdout.flush()
            sys.exit(0)
        else:
            pending = pending[1:]
'''


# Stand-in that renders the way inquire 0.9.4 really does (FrameRenderer): the
# frame is redrawn INCREMENTALLY. The cursor goes back to the top of the frame
# with relative moves, only rows that changed are rewritten (followed by
# "erase to end of line"), rows that vanished are erased, and the cursor is
# parked on the prompt row. After a Down key the help row is NOT sent again, so
# the new frame cannot be read from the stream of lines (issue #25).
FAKE_INCREMENTAL_GPCLIENT = r"""
import os, sys, tty

PAGE = 7
OPTIONS = ["gw-%02d (gw%d.example.com)" % (i, i) for i in range(20)]
QUESTION = "? Which gateway do you want to connect to?"
HELP = "[↑↓ to move, enter to select, type to filter]"

screen = []  # rows currently on screen
cursor_row = 0
cursor_col = 0


def frame_rows(start, cursor):
    rows = [QUESTION + " "]
    for index in range(start, start + PAGE):
        if index == cursor:
            marker = "\x1b[36m> "
            text = OPTIONS[index] + "\x1b[0m"
        else:
            if index == start and start > 0:
                marker = "^ "
            elif index == start + PAGE - 1 and index < len(OPTIONS) - 1:
                marker = "v "
            else:
                marker = "  "
            text = OPTIONS[index]
        rows.append(marker + text)
    rows.append(HELP)
    return rows


def plain(row):
    out, escape = "", False
    for char in row:
        if char == "\x1b":
            escape = True
        elif escape:
            escape = char != "m"
        else:
            out += char
    return out


def redraw(rows, parked_col):
    global screen, cursor_row, cursor_col
    out = "\x1b[?25l"
    if cursor_row:
        out += "\x1b[%dA" % cursor_row
    if cursor_col:
        out += "\x1b[%dD" % cursor_col
    height = max(len(rows), len(screen))
    for index in range(height):
        if index >= len(rows):
            out += "\x1b[2K"
        elif index >= len(screen) or screen[index] != rows[index]:
            out += rows[index] + "\x1b[K"
        out += "\r"
        if index < height - 1:
            out += "\n"
    out += "\x1b[%dA" % (height - 1)
    out += "\x1b[%dC" % parked_col
    out += "\x1b[?25h"
    sys.stdout.write(out)
    sys.stdout.flush()
    screen = rows
    cursor_row = 0
    cursor_col = parked_col


# FAKE_FOUND: the count in gpapi's "Found N gateways in portal config" line
# ("" prints none); FAKE_STALL_AFTER: stop reacting to Down after that many
FOUND = os.environ.get("FAKE_FOUND", "20")
STALL_AFTER = int(os.environ.get("FAKE_STALL_AFTER", "-1"))
downs = 0

tty.setraw(0)
sys.stdout.write("[INFO  gpclient::cli] gpclient started: fake\r\n")
if FOUND:
    sys.stdout.write(
        "[2026-07-20T12:44:26Z INFO  gpapi::portal::config] "
        "Found %s gateways in portal config\r\n" % FOUND
    )
sys.stdout.flush()

start = 0
cursor = 0
redraw(frame_rows(start, cursor), len(QUESTION) + 1)

pending = b""
while True:
    chunk = os.read(0, 16)
    if not chunk:
        break
    pending += chunk
    while pending:
        if pending.startswith(b"\x1b[B"):
            pending = pending[3:]
            if downs == STALL_AFTER:
                continue
            downs += 1
            cursor = (cursor + 1) % len(OPTIONS)
            if cursor == 0:
                start = 0
            elif cursor >= start + PAGE:
                start = cursor - PAGE + 1
            redraw(frame_rows(start, cursor), len(QUESTION) + 1)
        elif pending[:1] in (b"\r", b"\n"):
            pending = pending[1:]
            final = QUESTION + " " + OPTIONS[cursor]
            redraw([final], len(final))
            sys.stdout.write("\r\n")
            sys.stdout.write("fake gpclient: %d Down keys received\r\n" % downs)
            sys.stdout.write(
                "[INFO  gpclient::connect] Connecting to the selected gateway: %s\r\n"
                % OPTIONS[cursor]
            )
            sys.stdout.flush()
            sys.exit(0)
        else:
            pending = pending[1:]
"""


                                                                    # noqa: E501
# Stand-in that renders prompts the way inquire really does: every backend ends
# its rendered line with new_line(), so the prompt arrives as a COMPLETE line
# and nothing is left in the output tail. The service used to look only at the
# tail, so it never saw a prompt from a real gpclient (issue #2 log).
FAKE_TERMINATED_PROMPTS_GPCLIENT = r'''
import os, sys, tty


def read_answer():
    value = b""
    while True:
        chunk = os.read(0, 16)
        if not chunk:
            break
        for byte in chunk:
            if byte in (13, 10):
                return value.decode()
            value += bytes([byte])
    return value.decode()


tty.setraw(0)
sys.stdout.write("[INFO  gpclient::cli] gpclient started: fake\r\n")
sys.stdout.write("Enter login credentials (Portal: portal.example.com)\r\n")
# The prompt line is terminated, exactly like inquire renders it
sys.stdout.write("? Username: \r\n")
sys.stdout.flush()
user = read_answer()
sys.stdout.write("? Username: %s\r\n" % user)
sys.stdout.write("? Password: \r\n")
sys.stdout.flush()
password = read_answer()
sys.stdout.write("? Password: %s\r\n" % ("*" * len(password)))
sys.stdout.flush()

if user == "jdoe" and password == "s3cret":
    sys.stdout.write(
        "[INFO  gpclient::connect] Connecting to the only available gateway: "
        "gw-a (a.example.com)\r\n"
    )
else:
    sys.stdout.write("Authentication failure: got %r / %r\r\n" % (user, password))
sys.stdout.flush()
sys.exit(0)
'''

FAKE_CREDENTIALS_GPCLIENT = r'''
import os, sys, tty


def read_answer():
    value = b""
    while True:
        chunk = os.read(0, 16)
        if not chunk:
            break
        for byte in chunk:
            if byte in (13, 10):
                return value.decode()
            value += bytes([byte])
    return value.decode()


def ask(label, secret):
    sys.stdout.write("? %s: " % label)
    sys.stdout.flush()
    value = read_answer()
    # inquire finalises the line after the answer is confirmed
    shown = "*" * len(value) if secret else value
    sys.stdout.write("\r? %s: %s\r\n" % (label, shown))
    sys.stdout.flush()
    return value


tty.setraw(0)
sys.stdout.write("[INFO  gpclient::cli] gpclient started: fake\r\n")
sys.stdout.write("Please enter the login credentials (Portal: vpn.example.com)\r\n")
sys.stdout.flush()

user = ask("Username", False)
password = ask("Password", True)

if user == "jdoe" and password == "s3cret":
    sys.stdout.write(
        "[INFO  gpclient::connect] Connecting to the only available gateway: "
        "gw-a (a.example.com)\r\n"
    )
else:
    sys.stdout.write("Authentication failure: got %r / %r\r\n" % (user, password))
sys.stdout.flush()
sys.exit(0)
'''


ALL_GATEWAYS = [f"gw-{i:02d} (gw{i}.example.com)" for i in range(20)]


async def _run_against_fake(
    service_module, fake_path, preferred, stored_list="", stored_count=None
):
    plugin = service_module.GpclientVPNPlugin()
    plugin.preferred_gateway = preferred
    plugin._stored_gateway_list = stored_list
    plugin._stored_gateway_count = stored_count

    master, slave = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fake_path),
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    plugin._pty_master = master
    plugin.gpclient_process = process

    monitor = asyncio.create_task(plugin._monitor_gpclient_output())
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
        await asyncio.wait_for(monitor, timeout=10)
    finally:
        if not monitor.done():
            monitor.cancel()
        if plugin._prompt_task and not plugin._prompt_task.done():
            plugin._prompt_task.cancel()

    return plugin


def _write_fake(tmp_path):
    fake = tmp_path / "fake-gpclient.py"
    fake.write_text(FAKE_GPCLIENT)
    return fake


class TestGatewaySelectionOverPty:
    def test_preferred_gateway_is_selected(self, service_module, tmp_path):
        plugin = asyncio.run(
            _run_against_fake(service_module, _write_fake(tmp_path), "gw-london")
        )

        # The fake echoes what it was told to connect to
        assert any(
            "Connecting to the selected gateway: gw-london (gw3.example.com)" in line
            for line in plugin._recent_lines
        )
        # ...and the whole list ends up in the cache for the profile
        assert plugin._gateway_list[:3] == [
            "gw-warsaw (gw1.example.com)",
            "gw-frankfurt (gw2.example.com)",
            "gw-london (gw3.example.com)",
        ]

    def test_no_preference_takes_the_first_proposal(self, service_module, tmp_path):
        plugin = asyncio.run(
            _run_against_fake(service_module, _write_fake(tmp_path), "")
        )

        assert any(
            "Connecting to the selected gateway: gw-warsaw (gw1.example.com)" in line
            for line in plugin._recent_lines
        )

    def test_unknown_preference_falls_back_to_the_first(self, service_module, tmp_path):
        plugin = asyncio.run(
            _run_against_fake(service_module, _write_fake(tmp_path), "gw-tokyo")
        )

        assert any(
            "Connecting to the selected gateway: gw-warsaw (gw1.example.com)" in line
            for line in plugin._recent_lines
        )
        assert plugin.preferred_gateway == "gw-tokyo"


class TestIncrementalRedrawOverPty:
    """Issue #25: inquire redraws only the changed rows, so the walk through a
    list longer than one page must read the frames from the screen."""

    @staticmethod
    def _run(service_module, tmp_path, preferred, **stored):
        fake = tmp_path / "fake-gpclient-incremental.py"
        fake.write_text(FAKE_INCREMENTAL_GPCLIENT)
        return asyncio.run(_run_against_fake(service_module, fake, preferred, **stored))

    @staticmethod
    def _connected_to(plugin):
        found = [
            line.split("gateway: ", 1)[1].strip()
            for line in plugin._recent_lines
            if "Connecting to the selected gateway: " in line
        ]
        assert len(found) == 1
        return found[0]

    @pytest.mark.parametrize(
        "preferred, expected",
        [
            ("gw-12 (gw12.example.com)", "gw-12 (gw12.example.com)"),
            ("gw12.example.com", "gw-12 (gw12.example.com)"),
            ("gw-19", "gw-19 (gw19.example.com)"),
        ],
    )
    def test_preferred_gateway_beyond_the_first_page_is_selected(
        self, service_module, tmp_path, caplog, preferred, expected
    ):
        plugin = self._run(service_module, tmp_path, preferred)

        assert self._connected_to(plugin) == expected
        assert "stopped redrawing" not in caplog.text
        # The whole list is cached, not just the part walked to the gateway
        assert plugin._gateway_list == ALL_GATEWAYS

    @pytest.mark.parametrize("preferred", ["gw-tokyo", "gw13.example.org"])
    def test_unknown_preference_walks_the_list_and_takes_the_first(
        self, service_module, tmp_path, caplog, preferred
    ):
        plugin = self._run(service_module, tmp_path, preferred)

        # The lap saw the whole list and nothing matches: the first proposal,
        # not whatever happened to be highlighted at some point
        assert self._connected_to(plugin) == "gw-00 (gw0.example.com)"
        assert "stopped redrawing" not in caplog.text
        assert "is not offered by the portal" in caplog.text
        assert len(plugin._gateway_list) == 20

    def test_no_preference_takes_the_first_proposal(
        self, service_module, tmp_path, caplog
    ):
        plugin = self._run(service_module, tmp_path, "")

        assert self._connected_to(plugin) == "gw-00 (gw0.example.com)"
        assert "stopped redrawing" not in caplog.text
        assert plugin._gateway_list == ALL_GATEWAYS


class TestCollectionLapOverPty:
    """Issue #25 (second report): gpclient found 60 gateways, the profile got 8.
    A preferred gateway on the first page needs no walk, so only that page was
    cached. A paged list the profile does not hold completely is walked once."""

    FIRST = ALL_GATEWAYS[0]

    @staticmethod
    def _run(service_module, tmp_path, preferred, monkeypatch, env=None, **stored):
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        fake = tmp_path / "fake-gpclient-lap.py"
        fake.write_text(FAKE_INCREMENTAL_GPCLIENT)
        return asyncio.run(_run_against_fake(service_module, fake, preferred, **stored))

    @staticmethod
    def _connected_to(plugin):
        return TestIncrementalRedrawOverPty._connected_to(plugin)

    @staticmethod
    def _downs(plugin):
        found = [
            int(line.split(": ", 1)[1].split()[0])
            for line in plugin._recent_lines
            if "Down keys received" in line
        ]
        assert len(found) == 1
        return found[0]

    @staticmethod
    def _persisted(plugin):
        """What _persist_gateway_list writes to the profile: [(key, value)]"""
        writes = []

        async def record(key, value):
            writes.append((key, value))
            return True

        plugin._write_vpn_data = record
        asyncio.run(plugin._persist_gateway_list())
        return writes

    @pytest.mark.parametrize(
        "preferred, expected, selection_downs",
        [
            # The reported case: the gateway is on the first page
            ("gw-02", ALL_GATEWAYS[2], 2),
            ("gw-00", ALL_GATEWAYS[0], 0),
            # No preference: the first proposal, the cursor is back on it
            ("", ALL_GATEWAYS[0], 0),
            # The same selection as without the lap
            ("gw-12", ALL_GATEWAYS[12], 12),
            ("gw12.example.com", ALL_GATEWAYS[12], 12),
            ("gw-19", ALL_GATEWAYS[19], 19),
            # Substring only, beyond the first page
            ("w15.ex", ALL_GATEWAYS[15], 15),
            # Not offered: the first proposal, no second lap
            ("gw-tokyo", ALL_GATEWAYS[0], 0),
        ],
    )
    def test_empty_profile_walks_the_whole_list_and_selects_as_before(
        self, service_module, tmp_path, monkeypatch, caplog,
        preferred, expected, selection_downs,
    ):
        plugin = self._run(service_module, tmp_path, preferred, monkeypatch)

        assert self._connected_to(plugin) == expected
        assert "stopped redrawing" not in caplog.text
        # One lap (20 Down keys) plus the way to the gateway, no second lap
        assert self._downs(plugin) == 20 + selection_downs
        assert plugin._gateway_list == ALL_GATEWAYS
        assert plugin._gateway_count == 20
        assert self._persisted(plugin) == [
            ("gateway-list", ";".join(ALL_GATEWAYS)),
            ("gateway-list-count", "20"),
        ]

    def test_without_a_found_line_the_lap_length_is_the_count(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch, env={"FAKE_FOUND": ""}
        )

        assert plugin._gateway_count is None
        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._persisted(plugin)[-1] == ("gateway-list-count", "20")

    @pytest.mark.parametrize(
        "preferred, expected, downs",
        [("gw-02", ALL_GATEWAYS[2], 2), ("", ALL_GATEWAYS[0], 0)],
    )
    def test_complete_profile_list_is_not_walked_again(
        self, service_module, tmp_path, monkeypatch, preferred, expected, downs
    ):
        plugin = self._run(
            service_module, tmp_path, preferred, monkeypatch,
            stored_list=";".join(ALL_GATEWAYS), stored_count=20,
        )

        assert self._connected_to(plugin) == expected
        # Only the Down keys the selection itself needs
        assert self._downs(plugin) == downs
        # Nothing new to say to the profile
        assert self._persisted(plugin) == []

    def test_complete_profile_list_needs_no_found_line(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_FOUND": ""},
            stored_list=";".join(ALL_GATEWAYS), stored_count=20,
        )

        assert self._downs(plugin) == 2

    @pytest.mark.parametrize(
        "stored_list, stored_count",
        [
            # The portal has another number of gateways now
            (";".join(ALL_GATEWAYS), 19),
            (";".join(ALL_GATEWAYS), 21),
            # A gateway of the visible page is not in the list
            (";".join(ALL_GATEWAYS[1:]), 20),
            (";".join(ALL_GATEWAYS[:6]), 20),
            # Never walked (a list from before the count was stored)
            (";".join(ALL_GATEWAYS), None),
            ("", 20),
        ],
    )
    def test_incomplete_profile_list_is_walked(
        self, service_module, tmp_path, monkeypatch, stored_list, stored_count
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            stored_list=stored_list, stored_count=stored_count,
        )

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._downs(plugin) == 22
        assert plugin._gateway_list == ALL_GATEWAYS

    def test_stalled_redraw_selects_the_highlighted_entry_and_stores_no_count(
        self, service_module, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.3)
        stored = ALL_GATEWAYS[:3] + ["gw-old (old.example.com)"]
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_STALL_AFTER": "10"},
            stored_list=";".join(stored), stored_count=4,
        )

        # Today's behaviour: the entry on screen when gpclient went quiet
        assert self._connected_to(plugin) == ALL_GATEWAYS[10]
        assert "stopped redrawing" in caplog.text
        assert plugin._lap_entries == []
        # The part seen comes first, nothing stored is lost, no count is claimed
        assert self._persisted(plugin) == [
            ("gateway-list", ";".join(ALL_GATEWAYS[:11] + stored[3:])),
        ]

    def test_lap_beyond_the_step_limit_is_abandoned(
        self, service_module, tmp_path, monkeypatch, caplog
    ):
        # A list longer than the step limit: the lap stops at gw-15, and the
        # walk to the preferred gateway goes on from there instead of
        # selecting the entry the lap stopped on. Without gpclient's count the
        # step limit is all there is to go by.
        monkeypatch.setattr(service_module, "SELECT_MAX_STEPS", 15)
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch, env={"FAKE_FOUND": ""}
        )

        assert "Gave up walking the whole gateway list after 15 steps" in caplog.text
        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._downs(plugin) == 22
        assert plugin._lap_entries == []
        assert [key for key, _ in self._persisted(plugin)] == ["gateway-list"]

    def test_known_count_lets_the_lap_run_past_the_step_limit(
        self, service_module, tmp_path, monkeypatch, caplog
    ):
        # The same limit, but gpclient said 20: the lap gets 2 * 20 + 1 steps
        monkeypatch.setattr(service_module, "SELECT_MAX_STEPS", 15)
        plugin = self._run(service_module, tmp_path, "gw-02", monkeypatch)

        assert "Gave up walking" not in caplog.text
        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._downs(plugin) == 22
        assert plugin._lap_entries == ALL_GATEWAYS


class TestStoredCredentialsOverPty:
    """Regression for issue #6: the text-prompt flow must still work now that
    the list-prompt check runs first in _schedule_prompt_check()."""

    def _run(self, service_module, fake_path):
        async def scenario():
            plugin = service_module.GpclientVPNPlugin()
            plugin.vpn_username = "jdoe"
            plugin.vpn_password = "s3cret"
            plugin._reset_phase_state()

            master, slave = pty.openpty()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(fake_path),
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            plugin._pty_master = master
            plugin.gpclient_process = process

            monitor = asyncio.create_task(plugin._monitor_gpclient_output())
            try:
                await asyncio.wait_for(process.wait(), timeout=20)
                await asyncio.wait_for(monitor, timeout=10)
            finally:
                if not monitor.done():
                    monitor.cancel()
                if plugin._prompt_task and not plugin._prompt_task.done():
                    plugin._prompt_task.cancel()
            return plugin

        return asyncio.run(scenario())

    def test_prompts_terminated_by_inquire_are_answered(
        self, service_module, tmp_path
    ):
        """The real case from the #2 log: inquire ends the prompt line, so the
        prompt is a complete line and the tail is empty."""
        fake = tmp_path / "fake-gpclient-terminated.py"
        fake.write_text(FAKE_TERMINATED_PROMPTS_GPCLIENT)

        plugin = self._run(service_module, fake)

        lines = list(plugin._recent_lines)
        assert not any("Authentication failure" in line for line in lines), (
            "the credentials never reached gpclient - the prompt was not detected"
        )
        assert any("Connecting to the only available gateway" in line for line in lines)

    def test_username_and_password_are_typed_from_the_profile(
        self, service_module, tmp_path
    ):
        fake = tmp_path / "fake-gpclient-credentials.py"
        fake.write_text(FAKE_CREDENTIALS_GPCLIENT)

        async def scenario():
            plugin = service_module.GpclientVPNPlugin()
            plugin.vpn_username = "jdoe"
            plugin.vpn_password = "s3cret"
            plugin._reset_phase_state()

            master, slave = pty.openpty()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(fake),
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            plugin._pty_master = master
            plugin.gpclient_process = process

            monitor = asyncio.create_task(plugin._monitor_gpclient_output())
            try:
                await asyncio.wait_for(process.wait(), timeout=20)
                await asyncio.wait_for(monitor, timeout=10)
            finally:
                if not monitor.done():
                    monitor.cancel()
                if plugin._prompt_task and not plugin._prompt_task.done():
                    plugin._prompt_task.cancel()
            return plugin

        plugin = asyncio.run(scenario())

        lines = list(plugin._recent_lines)
        assert not any("Authentication failure" in line for line in lines)
        assert any("Connecting to the only available gateway" in line for line in lines)
        # The gateway from the log line is cached too, even without a list prompt
        assert plugin._gateway_list == ["gw-a (a.example.com)"]


class TestPressListDown:
    def test_down_key_is_sent_and_the_redraw_is_awaited(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        sent = []
        plugin._write_keys = lambda data, description: sent.append(data)

        first = [
            "? Which gateway do you want to connect to?",
            "> gw-a (a.example.com)",
            "  gw-b (b.example.com)",
            "[to move, to select]",
        ]
        second = [
            "? Which gateway do you want to connect to?",
            "  gw-a (a.example.com)",
            "> gw-b (b.example.com)",
            "[to move, to select]",
        ]
        plugin._screen.feed("\r\n".join(first) + "\r\n")

        previous = service_module.detect_select_prompt(plugin._screen.lines())

        async def scenario():
            async def redraw_later():
                await asyncio.sleep(0.1)
                # Incremental redraw like inquire: back up three rows and
                # rewrite only the two changed option rows
                plugin._screen.feed("\x1b[3A\r")
                plugin._screen.feed("  gw-a (a.example.com)\x1b[K\r\n")
                plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n")
                plugin._screen.feed("\r\n")

            asyncio.create_task(redraw_later())
            return await plugin._press_list_down(previous)

        frame = asyncio.run(scenario())

        assert sent == [service_module.KEY_DOWN]
        assert frame["cursor"] == 1

    def test_frame_in_the_middle_of_a_redraw_is_not_accepted(
        self, service_module, monkeypatch
    ):
        """Old highlighted row already rewritten (no marker), new one not yet:
        no frame at all must be returned, and not the stale one either."""
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.3)
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "  gw-b (b.example.com)\r\n"
            "[to move, to select]\r\n"
        )

        previous = service_module.detect_select_prompt(plugin._screen.lines())

        async def scenario():
            async def half_redraw():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")

            asyncio.create_task(half_redraw())
            return await plugin._press_list_down(previous)

        assert asyncio.run(scenario()) is None

    def test_frame_is_taken_only_after_the_screen_settled(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "  gw-b (b.example.com)\r\n"
            "[to move, to select]\r\n"
        )

        previous = service_module.detect_select_prompt(plugin._screen.lines())

        async def scenario():
            async def redraw_in_two_steps():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")
                await asyncio.sleep(0.02)
                plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n\r\n")

            asyncio.create_task(redraw_in_two_steps())
            return await plugin._press_list_down(previous)

        frame = asyncio.run(scenario())

        assert frame["options"][frame["cursor"]] == "gw-b (b.example.com)"

    def test_no_redraw_gives_up(self, service_module, monkeypatch):
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.2)
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "[to move, to select]\r\n"
        )

        previous = service_module.detect_select_prompt(plugin._screen.lines())

        assert asyncio.run(plugin._press_list_down(previous)) is None

    def test_unchanged_frame_is_not_taken_for_a_move(
        self, service_module, monkeypatch
    ):
        """The frame the walk is on comes from the caller: a screen that shows
        it unchanged after Down is no redraw, even when the screen could not be
        read before the key was sent."""
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.2)
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "  gw-b (b.example.com)\r\n"
            "[to move, to select]\r\n"
        )
        previous = service_module.detect_select_prompt(plugin._screen.lines())
        # The screen is mid-redraw (no readable frame) when the key goes out
        # and then settles on the very same frame
        plugin._screen = service_module.ScreenBuffer()
        plugin._screen.feed("garbage\r\n")

        async def scenario():
            async def settle_unchanged():
                await asyncio.sleep(0.1)
                plugin._screen.feed(
                    "? Which gateway do you want to connect to?\r\n"
                    "> gw-a (a.example.com)\r\n"
                    "  gw-b (b.example.com)\r\n"
                    "[to move, to select]\r\n"
                )

            asyncio.create_task(settle_unchanged())
            return await plugin._press_list_down(previous)

        assert asyncio.run(scenario()) is None


class TestPressListDownStability:
    FRAME = (
        "? Which gateway do you want to connect to?\r\n"
        "> gw-a (a.example.com)\r\n"
        "  gw-b (b.example.com)\r\n"
        "[to move, to select]\r\n"
    )

    def test_unrelated_output_does_not_hold_the_walk_up(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(self.FRAME)
        previous = service_module.detect_select_prompt(plugin._screen.lines())

        async def scenario():
            stop = False

            async def noise():
                while not stop:
                    # Bumps the screen version, changes nothing on the screen
                    plugin._screen.feed("\x1b[?25h")
                    await asyncio.sleep(0.01)

            async def redraw_later():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")
                plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n\r\n")

            tasks = [asyncio.create_task(noise()), asyncio.create_task(redraw_later())]
            try:
                return await plugin._press_list_down(previous)
            finally:
                stop = True
                await asyncio.gather(*tasks)

        frame = asyncio.run(scenario())

        assert frame is not None
        assert frame["options"][frame["cursor"]] == "gw-b (b.example.com)"

    def test_a_frame_that_changes_between_polls_is_not_accepted(
        self, service_module, monkeypatch
    ):
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.3)
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(self.FRAME)
        previous = service_module.detect_select_prompt(plugin._screen.lines())

        def frame_with_cursor_on(index):
            names = ["gw-a", "gw-b", "gw-c"]
            return tuple(
                ["? Which gateway do you want to connect to?"]
                + [("> " if i == index else "  ") + n for i, n in enumerate(names)]
                + ["[to move, to select]"]
            )

        class FlippingScreen:
            version = 0
            calls = 0

            def lines(self):
                self.calls += 1
                return frame_with_cursor_on(1 + self.calls % 2)

        plugin._screen = FlippingScreen()

        assert asyncio.run(plugin._press_list_down(previous)) is None

    def test_the_same_frame_twice_is_accepted(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(self.FRAME)
        previous = service_module.detect_select_prompt(plugin._screen.lines())
        plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")
        plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n\r\n")

        frame = asyncio.run(plugin._press_list_down(previous))

        assert frame["cursor"] == 1


async def _run_with_credentials(service_module, fake_path, username, password):
    """Run a fake gpclient with stored credentials and no way to ask the user"""
    plugin = service_module.GpclientVPNPlugin()
    plugin.vpn_username = username
    plugin.vpn_password = password
    plugin._interactive = False
    plugin._reset_phase_state()

    master, slave = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fake_path),
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    plugin._pty_master = master
    plugin.gpclient_process = process

    monitor = asyncio.create_task(plugin._monitor_gpclient_output())
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
        await asyncio.wait_for(monitor, timeout=10)
    finally:
        if not monitor.done():
            monitor.cancel()
        if plugin._prompt_task and not plugin._prompt_task.done():
            plugin._prompt_task.cancel()
    return plugin


class TestBadCredentialsOverPty:
    """Negative counterparts of TestStoredCredentialsOverPty."""

    def test_no_stored_password_without_interaction_fails_login(
        self, service_module, tmp_path, dbus_signals
    ):
        fake = tmp_path / "fake-gpclient-terminated.py"
        fake.write_text(FAKE_TERMINATED_PROMPTS_GPCLIENT)

        plugin = asyncio.run(_run_with_credentials(service_module, fake, "jdoe", ""))

        # Nobody can be asked for the password: report it, don't hang or guess
        assert plugin._login_failed is True
        assert dbus_signals == [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED),
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
        ]
        assert not any(
            "Connecting to the only available gateway" in line
            for line in plugin._recent_lines
        )

    def test_wrong_stored_password_is_not_accepted(
        self, service_module, tmp_path, dbus_signals
    ):
        fake = tmp_path / "fake-gpclient-credentials.py"
        fake.write_text(FAKE_CREDENTIALS_GPCLIENT)

        plugin = asyncio.run(
            _run_with_credentials(service_module, fake, "jdoe", "wrong")
        )

        lines = list(plugin._recent_lines)
        assert any("Authentication failure" in line for line in lines)
        assert not any(
            "Connecting to the only available gateway" in line for line in lines
        )
        assert plugin._gateway_list == []

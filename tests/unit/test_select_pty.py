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
# Keys as in inquire 0.9.4: Down wraps, PageDown moves by a page without
# wrapping and stops at the last entry, Home goes to the first entry; a key that
# does not change the cursor causes no redraw at all.
FAKE_INCREMENTAL_GPCLIENT = r"""
import os, sys, tty

PAGE = 7
OPTIONS = ["gw-%02d (gw%d.example.com)" % (i, i) for i in range(20)]
if os.environ.get("FAKE_DUPLICATE"):
    OPTIONS[10] = OPTIONS[5]  # two identical entries: 19 different ones
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
# ("" prints none); FAKE_STALL_AFTER / FAKE_STALL_PAGE_AFTER: stop reacting to
# Down / PageDown after that many; FAKE_NO_HOME: Home is never redrawn;
# FAKE_DUPLICATE: entry 10 is the same as entry 5
FOUND = os.environ.get("FAKE_FOUND", "20")
STALL_AFTER = int(os.environ.get("FAKE_STALL_AFTER", "-1"))
STALL_PAGE_AFTER = int(os.environ.get("FAKE_STALL_PAGE_AFTER", "-1"))
NO_HOME = bool(os.environ.get("FAKE_NO_HOME"))
downs = pages = homes = 0


def move_to(new):
    global start, cursor
    if new == cursor:
        return
    cursor = new
    if cursor == 0:
        start = 0
    elif cursor >= start + PAGE:
        start = cursor - PAGE + 1
    elif cursor < start:
        start = cursor
    redraw(frame_rows(start, cursor), len(QUESTION) + 1)


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
            move_to((cursor + 1) % len(OPTIONS))
        elif pending.startswith(b"\x1b[6~"):
            pending = pending[4:]
            if pages == STALL_PAGE_AFTER:
                continue
            pages += 1
            move_to(min(cursor + PAGE, len(OPTIONS) - 1))
        elif pending.startswith(b"\x1b[H"):
            pending = pending[3:]
            homes += 1
            if not NO_HOME:
                move_to(0)
        elif pending[:1] in (b"\r", b"\n"):
            pending = pending[1:]
            final = QUESTION + " " + OPTIONS[cursor]
            redraw([final], len(final))
            sys.stdout.write("\r\n")
            sys.stdout.write(
                "fake gpclient: keys received down=%d pagedown=%d home=%d\r\n"
                % (downs, pages, homes)
            )
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
        # The profile holds the whole list: no reading, so the walk laps it
        plugin = self._run(
            service_module, tmp_path, preferred,
            stored_list=";".join(ALL_GATEWAYS), stored_count=20,
        )

        # The walk saw the whole list and nothing matches: the first proposal,
        # not whatever happened to be highlighted at some point
        assert self._connected_to(plugin) == "gw-00 (gw0.example.com)"
        assert "stopped redrawing" not in caplog.text
        assert "Walked the whole list" in caplog.text
        assert len(plugin._gateway_list) == 20

    def test_no_preference_takes_the_first_proposal(
        self, service_module, tmp_path, caplog
    ):
        plugin = self._run(service_module, tmp_path, "")

        assert self._connected_to(plugin) == "gw-00 (gw0.example.com)"
        assert "stopped redrawing" not in caplog.text
        assert plugin._gateway_list == ALL_GATEWAYS


class TestPagedCollectionOverPty:
    """Issue #25 (second report): gpclient found 60 gateways, the profile got 8.
    A preferred gateway on the first page needs no walk, so only that page was
    cached. A paged list the profile does not hold completely is read page by
    page (PageDown), then Home puts the cursor back on the first proposal."""

    @pytest.fixture(autouse=True)
    def _quick_redraw_timeout(self, service_module, monkeypatch):
        # A key that causes no redraw (end of the list) costs this much
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.3)

    @staticmethod
    def _run(service_module, tmp_path, preferred, monkeypatch, env=None, **stored):
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        fake = tmp_path / "fake-gpclient-paged.py"
        fake.write_text(FAKE_INCREMENTAL_GPCLIENT)
        return asyncio.run(_run_against_fake(service_module, fake, preferred, **stored))

    @staticmethod
    def _connected_to(plugin):
        return TestIncrementalRedrawOverPty._connected_to(plugin)

    @staticmethod
    def _keys(plugin):
        """The keys the fake received: {"down": n, "pagedown": n, "home": n}"""
        found = [
            dict(pair.split("=") for pair in line.split("received ", 1)[1].split())
            for line in plugin._recent_lines
            if "keys received" in line
        ]
        assert len(found) == 1
        return {key: int(value) for key, value in found[0].items()}

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

    def test_preferred_on_the_first_page_with_an_empty_profile(
        self, service_module, tmp_path, monkeypatch, caplog
    ):
        # The reported case: the gateway is on the first page, the profile
        # holds nothing, and all 20 gateways must end up in the profile
        caplog.set_level("INFO")
        plugin = self._run(service_module, tmp_path, "gw-00", monkeypatch)

        assert self._connected_to(plugin) == ALL_GATEWAYS[0]
        # 0 -> 7 -> 14 -> 19 (clamped): every entry seen, then back to the top
        assert self._keys(plugin) == {"down": 0, "pagedown": 3, "home": 1}
        assert plugin._gateway_list == ALL_GATEWAYS
        assert plugin._lap_entries == ALL_GATEWAYS
        assert "Reading the whole gateway list page by page" in caplog.text
        assert "20 entries" in caplog.text
        assert self._persisted(plugin) == [
            ("gateway-list", ";".join(ALL_GATEWAYS)),
            ("gateway-list-count", "20"),
        ]

    def test_preferred_near_the_top_needs_only_the_downs_to_it(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(service_module, tmp_path, "gw-02", monkeypatch)

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._keys(plugin) == {"down": 2, "pagedown": 3, "home": 1}
        assert plugin._lap_entries == ALL_GATEWAYS

    def test_no_preference_takes_the_first_proposal_and_caches_all(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(service_module, tmp_path, "", monkeypatch)

        assert self._connected_to(plugin) == ALL_GATEWAYS[0]
        assert self._keys(plugin) == {"down": 0, "pagedown": 3, "home": 1}
        assert plugin._lap_entries == ALL_GATEWAYS

    @pytest.mark.parametrize(
        "preferred, expected, downs",
        [
            ("gw-12", ALL_GATEWAYS[12], 12),
            ("gw12.example.com", ALL_GATEWAYS[12], 12),
            ("gw-19", ALL_GATEWAYS[19], 19),
            # Substring only: the first entry that contains it, as without paging
            ("w15.ex", ALL_GATEWAYS[15], 15),
            ("gw-1", ALL_GATEWAYS[10], 10),
            # Not offered: the first proposal, no lap
            ("gw-tokyo", ALL_GATEWAYS[0], 0),
        ],
    )
    def test_selection_is_the_same_as_without_reading_the_pages(
        self, service_module, tmp_path, monkeypatch, caplog,
        preferred, expected, downs,
    ):
        plugin = self._run(service_module, tmp_path, preferred, monkeypatch)

        assert self._connected_to(plugin) == expected
        assert self._keys(plugin) == {"down": downs, "pagedown": 3, "home": 1}
        assert "stopped redrawing" not in caplog.text
        assert plugin._lap_entries == ALL_GATEWAYS

    @pytest.mark.parametrize(
        "stored",
        [
            {},
            {"stored_list": ";".join(ALL_GATEWAYS), "stored_count": 20},
        ],
    )
    def test_unknown_count_reads_no_pages_and_only_merges(
        self, service_module, tmp_path, monkeypatch, stored
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_FOUND": ""}, **stored,
        )

        assert plugin._gateway_count is None
        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        # Not a single PageDown: a stalled gpclient looks like the end
        assert self._keys(plugin) == {"down": 2, "pagedown": 0, "home": 0}
        assert plugin._lap_entries == []
        # What was seen (the first page) is merged into what is stored
        writes = self._persisted(plugin)
        assert all(key != "gateway-list-count" for key, _ in writes)
        if stored:
            assert writes == []
        else:
            assert writes == [("gateway-list", ";".join(ALL_GATEWAYS[:7]))]

    @pytest.mark.parametrize("found", ["25", "100"])
    def test_count_above_the_list_is_not_complete(
        self, service_module, tmp_path, monkeypatch, caplog, found
    ):
        stored = ["gw-old (old.example.com)"]
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_FOUND": found}, stored_list=";".join(stored),
        )

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert "Could not read the whole gateway list" in caplog.text
        assert plugin._lap_entries == []
        # Merged, no count claimed; the next connect reads the pages again
        assert self._persisted(plugin) == [
            ("gateway-list", ";".join(ALL_GATEWAYS + stored))
        ]

    @pytest.mark.parametrize("after", [1, 2])
    def test_stalled_paging_is_not_complete_but_selects_the_preferred(
        self, service_module, tmp_path, monkeypatch, caplog, after
    ):
        stored = ALL_GATEWAYS[:3] + ["gw-old (old.example.com)"]
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_STALL_PAGE_AFTER": str(after)},
            stored_list=";".join(stored), stored_count=4,
        )

        # The cursor is back at the top, not wherever the paging stopped
        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._keys(plugin) == {"down": 2, "pagedown": after, "home": 1}
        # Fewer redraws than ceil((20 - 1) / 7) = 3: not the end of the list
        assert after < 3
        assert plugin._lap_entries == []
        writes = self._persisted(plugin)
        assert [key for key, _ in writes] == ["gateway-list"]
        # Seen first, the stored entries not seen after them, nothing lost
        assert writes[0][1].endswith("gw-old (old.example.com)")
        assert writes[0][1].startswith(";".join(ALL_GATEWAYS[:3]))

    def test_stalled_first_page_down_selects_from_the_first_page(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_STALL_PAGE_AFTER": "0"},
        )

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        # The cursor never moved (the fake counts only keys it acted on), so
        # there is nothing to put back
        assert self._keys(plugin) == {"down": 2, "pagedown": 0, "home": 0}
        assert plugin._lap_entries == []

    @pytest.mark.parametrize(
        "preferred, expected",
        [
            ("gw-02", ALL_GATEWAYS[2]),  # walks on, wrapping around the end
            ("gw-19", ALL_GATEWAYS[19]),  # the cursor is on it already
            ("gw-12", ALL_GATEWAYS[12]),
            ("gw-1", ALL_GATEWAYS[10]),  # substring only: as with Home
            ("", ALL_GATEWAYS[0]),  # the first proposal, by name
            ("gw-tokyo", ALL_GATEWAYS[0]),
        ],
    )
    def test_home_not_redrawn_selects_from_where_the_cursor_is(
        self, service_module, tmp_path, monkeypatch, caplog, preferred, expected
    ):
        plugin = self._run(
            service_module, tmp_path, preferred, monkeypatch,
            env={"FAKE_NO_HOME": "1"},
        )

        assert self._connected_to(plugin) == expected
        assert self._keys(plugin)["home"] == 1
        assert "did not redraw the list after Home" in caplog.text
        # The pages were read all the same
        assert plugin._lap_entries == ALL_GATEWAYS

    @pytest.mark.parametrize(
        "preferred, expected, downs",
        [("gw-02", ALL_GATEWAYS[2], 2), ("", ALL_GATEWAYS[0], 0)],
    )
    def test_complete_profile_list_is_not_read_again(
        self, service_module, tmp_path, monkeypatch, preferred, expected, downs
    ):
        plugin = self._run(
            service_module, tmp_path, preferred, monkeypatch,
            stored_list=";".join(ALL_GATEWAYS), stored_count=20,
        )

        assert self._connected_to(plugin) == expected
        assert self._keys(plugin) == {"down": downs, "pagedown": 0, "home": 0}
        assert plugin._lap_entries == []
        assert self._persisted(plugin) == []

    def test_entry_shown_twice_is_complete_at_the_end_and_not_read_again(
        self, service_module, tmp_path, monkeypatch
    ):
        # Two identical entries: 19 different ones, gpclient found 20
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_DUPLICATE": "1"},
        )

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        # 0 -> 7 -> 14 -> 19, and the fourth key finds the cursor at the end
        assert self._keys(plugin) == {"down": 2, "pagedown": 4, "home": 1}
        different = list(dict.fromkeys(plugin._lap_entries))
        assert len(different) == 19
        writes = self._persisted(plugin)
        assert writes == [
            ("gateway-list", ";".join(different)),
            ("gateway-list-count", "20"),
        ]

        # The next connection finds the profile complete and reads nothing
        again = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            env={"FAKE_DUPLICATE": "1"},
            stored_list=dict(writes)["gateway-list"],
            stored_count=int(dict(writes)["gateway-list-count"]),
        )

        assert self._connected_to(again) == ALL_GATEWAYS[2]
        assert self._keys(again) == {"down": 2, "pagedown": 0, "home": 0}
        assert self._persisted(again) == []

    @pytest.mark.parametrize(
        "stored_list, stored_count",
        [
            # The portal has another number of gateways now
            (";".join(ALL_GATEWAYS), 19),
            (";".join(ALL_GATEWAYS), 21),
            # A gateway of the visible page is not in the list
            (";".join(ALL_GATEWAYS[1:]), 20),
            (";".join(ALL_GATEWAYS[:6]), 20),
            # Never read (a list from before the count was stored)
            (";".join(ALL_GATEWAYS), None),
            ("", 20),
        ],
    )
    def test_incomplete_profile_list_is_read(
        self, service_module, tmp_path, monkeypatch, stored_list, stored_count
    ):
        plugin = self._run(
            service_module, tmp_path, "gw-02", monkeypatch,
            stored_list=stored_list, stored_count=stored_count,
        )

        assert self._connected_to(plugin) == ALL_GATEWAYS[2]
        assert self._keys(plugin) == {"down": 2, "pagedown": 3, "home": 1}
        assert plugin._lap_entries == ALL_GATEWAYS


FAKE_ONLY_GATEWAY_GPCLIENT = r"""
import os, sys, tty

tty.setraw(0)
if os.environ.get("FAKE_FOUND"):
    sys.stdout.write(
        "[INFO  gpapi::portal::config] Found %s gateways in portal config\r\n"
        % os.environ["FAKE_FOUND"]
    )
sys.stdout.write(
    "[INFO  gpclient::connect] Connecting to the %s gateway: gw-a (a.example.com)\r\n"
    % os.environ.get("FAKE_KIND", "only available")
)
sys.stdout.flush()
"""


class TestOnlyAvailableGatewayOverPty:
    """"Connecting to the only available gateway" means the portal offers just
    that one, so it replaces the stored list - unless gpclient found more."""

    STORED = ";".join(ALL_GATEWAYS[:3])

    def _run(self, service_module, tmp_path, monkeypatch, **env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        fake = tmp_path / "fake-gpclient-only.py"
        fake.write_text(FAKE_ONLY_GATEWAY_GPCLIENT)
        return asyncio.run(
            _run_against_fake(
                service_module, fake, "", stored_list=self.STORED, stored_count=3
            )
        )

    @staticmethod
    def _persisted(plugin):
        return TestPagedCollectionOverPty._persisted(plugin)

    def test_the_only_gateway_replaces_the_stored_list(
        self, service_module, tmp_path, monkeypatch
    ):
        plugin = self._run(service_module, tmp_path, monkeypatch, FAKE_FOUND="1")

        assert plugin._lap_entries == ["gw-a (a.example.com)"]
        assert self._persisted(plugin) == [
            ("gateway-list", "gw-a (a.example.com)"),
            ("gateway-list-count", "1"),
        ]

    @pytest.mark.parametrize(
        "env",
        [
            {"FAKE_FOUND": "5"},  # gpclient found more: not the whole list
            {"FAKE_KIND": "selected"},  # picked from a list, not the only one
            {"FAKE_KIND": "selected", "FAKE_FOUND": "1"},
            # No count: gpclient fell back to the portal address, which says
            # nothing about the portal's gateways
            {},
        ],
    )
    def test_otherwise_the_stored_list_is_only_added_to(
        self, service_module, tmp_path, monkeypatch, env
    ):
        plugin = self._run(service_module, tmp_path, monkeypatch, **env)

        assert plugin._lap_entries == []
        assert self._persisted(plugin) == [
            ("gateway-list", "gw-a (a.example.com);" + self.STORED)
        ]


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

    @pytest.mark.parametrize("key_name", ["KEY_PAGE_DOWN", "KEY_HOME"])
    def test_any_list_key_is_sent_and_the_redraw_is_awaited(
        self, service_module, key_name
    ):
        plugin = service_module.GpclientVPNPlugin()
        sent = []
        plugin._write_keys = lambda data, description: sent.append(data)
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "  gw-b (b.example.com)\r\n"
            "[to move, to select]\r\n"
        )
        previous = service_module.detect_select_prompt(plugin._screen.lines())
        key = getattr(service_module, key_name)

        async def scenario():
            async def redraw_later():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")
                plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n\r\n")

            asyncio.create_task(redraw_later())
            return await plugin._press_list_key(previous, key, "test")

        frame = asyncio.run(scenario())

        assert sent == [key]
        assert frame["cursor"] == 1

    def test_the_keys_are_what_crossterm_parses(self, service_module):
        assert service_module.KEY_PAGE_DOWN == b"\x1b[6~"
        assert service_module.KEY_HOME == b"\x1b[H"

    @pytest.mark.parametrize("key_name", ["KEY_PAGE_DOWN", "KEY_HOME"])
    def test_key_without_a_redraw_gives_up(
        self, service_module, monkeypatch, key_name
    ):
        # inquire does not redraw when the cursor stays where it is
        monkeypatch.setattr(service_module, "SELECT_REDRAW_TIMEOUT", 0.2)
        plugin = service_module.GpclientVPNPlugin()
        plugin._write_keys = lambda data, description: None
        plugin._screen.feed(
            "? Which gateway do you want to connect to?\r\n"
            "> gw-a (a.example.com)\r\n"
            "[to move, to select]\r\n"
        )
        previous = service_module.detect_select_prompt(plugin._screen.lines())

        key = getattr(service_module, key_name)
        assert asyncio.run(plugin._press_list_key(previous, key, "test")) is None

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

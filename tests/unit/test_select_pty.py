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


tty.setraw(0)
sys.stdout.write("[INFO  gpclient::cli] gpclient started: fake\r\n")
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


async def _run_against_fake(service_module, fake_path, preferred):
    plugin = service_module.GpclientVPNPlugin()
    plugin.preferred_gateway = preferred

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
    def _run(service_module, tmp_path, preferred):
        fake = tmp_path / "fake-gpclient-incremental.py"
        fake.write_text(FAKE_INCREMENTAL_GPCLIENT)
        return asyncio.run(_run_against_fake(service_module, fake, preferred))

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
        # Every gateway on the way is cached, not just the first page
        walked = [f"gw-{i:02d} (gw{i}.example.com)" for i in range(20)]
        assert plugin._gateway_list == walked[: walked.index(expected) + 1]

    @pytest.mark.parametrize("preferred", ["gw-tokyo", "gw13.example.org"])
    def test_unknown_preference_walks_the_list_and_takes_the_first(
        self, service_module, tmp_path, caplog, preferred
    ):
        plugin = self._run(service_module, tmp_path, preferred)

        # The walk wrapped around to where it started: the first proposal,
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
        assert plugin._gateway_list == ["gw-00 (gw0.example.com)"] + [
            f"gw-{i:02d} (gw{i}.example.com)" for i in range(1, 7)
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
            return await plugin._press_list_down()

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

        async def scenario():
            async def half_redraw():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")

            asyncio.create_task(half_redraw())
            return await plugin._press_list_down()

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

        async def scenario():
            async def redraw_in_two_steps():
                await asyncio.sleep(0.1)
                plugin._screen.feed("\x1b[3A\r  gw-a (a.example.com)\x1b[K\r\n")
                await asyncio.sleep(0.02)
                plugin._screen.feed("> gw-b (b.example.com)\x1b[K\r\n\r\n")

            asyncio.create_task(redraw_in_two_steps())
            return await plugin._press_list_down()

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

        assert asyncio.run(plugin._press_list_down()) is None


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

"""
Text, password and one-time prompts are read from the screen model (issue #27).

inquire 0.9.4 redraws a frame incrementally: only the rows that changed are
sent, with relative cursor moves. The line stream of such output holds stale
rows (a prompt row that is not sent again after an error row appears below it),
so the prompt is read from what a terminal would show: the row at the cursor.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import os
import pty
import sys

import pytest
from inquire_frames import InquireFrames

PASSWORD = "? Password: "
OTP = "? Enter the next tokencode: "
HELP = "[Enter to submit, Esc to cancel]"


# --- detect_screen_prompt ------------------------------------------------------


class TestDetectScreenPrompt:
    def test_prompt_at_the_cursor(self, service_module):
        found = service_module.detect_screen_prompt(["? Password:"], 0)
        assert found == (0, "Password")

    def test_rows_below_the_cursor_are_ignored(self, service_module):
        rows = ["? Password:", "# Invalid password", HELP]
        assert service_module.detect_screen_prompt(rows, 0) == (0, "Password")

    def test_error_row_above_the_prompt_is_ignored(self, service_module):
        rows = ["# Invalid password", "? Password:"]
        assert service_module.detect_screen_prompt(rows, 1) == (1, "Password")

    def test_terminated_prompt_is_found_above_the_empty_cursor_row(
        self, service_module
    ):
        rows = ["banner", "? Username:", ""]
        assert service_module.detect_screen_prompt(rows, 2) == (1, "Username")

    def test_prompt_without_a_colon_is_a_label(self, service_module):
        rows = ["? Enter the next tokencode"]
        found = service_module.detect_screen_prompt(rows, 0)
        assert found == (0, "Enter the next tokencode")

    @pytest.mark.parametrize(
        "rows, cursor",
        [
            (["# Invalid password"], 0),  # an error row alone
            (["# Invalid password", HELP], 1),
            (["? Password:", "# Invalid password"], 1),  # cursor left the prompt
            (["? Password: ****", ""], 1),  # answered and finalised
            (["? Username: jdoe"], 0),
            (["[INFO] started", ""], 1),
            (["Authentication failure"], 0),
            ([""], 0),
            ([], 0),
            (["? Password:"], 5),  # cursor outside the rows
            (["? Password:"], -1),
        ],
    )
    def test_no_prompt(self, service_module, rows, cursor):
        assert service_module.detect_screen_prompt(rows, cursor) is None

    def test_echo_of_our_answer_is_skipped(self, service_module):
        rows = ["? Enter the next tokencode 123456"]
        assert service_module.detect_screen_prompt(rows, 0, "123456") is None
        assert service_module.detect_screen_prompt(rows, 0) is not None


class TestScreenBufferCursor:
    def test_cursor_row_follows_relative_moves(self, service_module):
        screen = service_module.ScreenBuffer()
        screen.feed("a\r\nb\r\nc")
        assert screen.cursor_row == 2
        screen.feed("\x1b[2A")
        assert screen.cursor_row == 0

    def test_row_id_stays_when_the_top_is_trimmed(self, service_module):
        screen = service_module.ScreenBuffer()
        screen.feed("first\r\n")
        screen.feed("filler\r\n" * (screen.MAX_ROWS + 5))
        screen.feed("last")
        index = screen.cursor_row
        assert screen.dropped > 0
        assert screen.row_id(index) == screen.dropped + index
        assert screen.row_id(index) > screen.MAX_ROWS  # unique over the session

    def test_no_row_is_dropped_on_a_small_screen(self, service_module):
        screen = service_module.ScreenBuffer()
        screen.feed("a\r\nb\r\n")
        assert screen.dropped == 0
        assert screen.row_id(1) == 1


# --- the debounced check on a screen drawn by inquire ----------------------------


@pytest.fixture
def quick_debounce(service_module, monkeypatch):
    monkeypatch.setattr(service_module, "PROMPT_DEBOUNCE_SECONDS", 0.05)


class Watch:
    """A plugin that records the prompts it would answer"""

    def __init__(self, service_module):
        self.plugin = service_module.GpclientVPNPlugin()
        self.handled = []

        async def prompt(label):
            self.handled.append(label)

        self.plugin._handle_prompt = prompt

    async def feed(self, text, settle=0.2):
        """Output arrives as one chunk; wait until the debounce has run"""
        self.plugin._recent_lines.extend(self.plugin._consume_output(text))
        self.plugin._schedule_prompt_check()
        await asyncio.sleep(settle)


def run_watch(service_module, scenario):
    watch = Watch(service_module)
    asyncio.run(scenario(watch))
    return watch.handled


class TestPromptsFromTheScreen:
    def test_prompt_with_a_help_row_below_is_detected(
        self, service_module, quick_debounce
    ):
        """The prompt row is never the last line of such a frame"""

        async def scenario(watch):
            await watch.feed(InquireFrames().draw([PASSWORD, HELP], 0, len(PASSWORD)))

        assert run_watch(service_module, scenario) == ["Password"]

    @pytest.mark.parametrize(
        "error_above, expected",
        [
            # The error row is new, the prompt row is not sent again: the
            # answered prompt is not answered a second time
            (False, ["Password", "Password"]),
            # The error row pushes the prompt one row down: a new frame, and
            # the (rejected) prompt asks for an answer again
            (True, ["Password", "Password", "Password"]),
        ],
    )
    def test_prompt_rendered_again_after_an_error(
        self, service_module, quick_debounce, error_above, expected
    ):
        async def scenario(watch):
            frames = InquireFrames()
            await watch.feed(frames.draw([PASSWORD], 0, len(PASSWORD)))
            # inquire finalises the answer, then asks again
            await watch.feed(frames.finish(PASSWORD + "****"))
            await watch.feed(frames.draw([PASSWORD], 0, len(PASSWORD)))
            if error_above:
                rows, row = ["# Invalid password", PASSWORD], 1
            else:
                rows, row = [PASSWORD, "# Invalid password"], 0
            await watch.feed(frames.draw(rows, row, len(PASSWORD)))

        assert run_watch(service_module, scenario) == expected

    def test_second_prompt_drawn_over_the_first_is_detected(
        self, service_module, quick_debounce
    ):
        async def scenario(watch):
            frames = InquireFrames()
            await watch.feed(frames.draw([PASSWORD, HELP], 0, len(PASSWORD)))
            # No newline in between: the next prompt takes the same rows
            await watch.feed(frames.draw([OTP, HELP], 0, len(OTP)))

        assert run_watch(service_module, scenario) == [
            "Password",
            "Enter the next tokencode",
        ]

    def test_answered_prompt_is_not_answered_again_after_another_row_changes(
        self, service_module, quick_debounce
    ):
        async def scenario(watch):
            frames = InquireFrames()
            await watch.feed(frames.draw([PASSWORD, HELP], 0, len(PASSWORD)))
            # Only the second row is sent; the prompt row stays as it was
            await watch.feed(frames.draw([PASSWORD, "# Invalid"], 0, len(PASSWORD)))
            await watch.feed(frames.draw([PASSWORD, HELP], 0, len(PASSWORD)))
            await watch.feed("[INFO  gpclient::cli] still working\r\n")

        assert run_watch(service_module, scenario) == ["Password"]

    def test_error_row_alone_is_not_a_prompt(self, service_module, quick_debounce):
        async def scenario(watch):
            frames = InquireFrames()
            await watch.feed(frames.draw(["# Invalid password"], 0, 0))

        assert run_watch(service_module, scenario) == []

    @pytest.mark.parametrize(
        "after",
        [
            "[INFO  gpclient::connect] Authenticating\r\n",
            "Authentication failure\r\n",
            "# Invalid password\r\n",
        ],
    )
    def test_output_after_the_answer_is_not_a_prompt(
        self, service_module, quick_debounce, after
    ):
        async def scenario(watch):
            frames = InquireFrames()
            await watch.feed(frames.draw([PASSWORD], 0, len(PASSWORD)))
            await watch.feed(frames.finish(PASSWORD + "****"))
            await watch.feed(after)

        assert run_watch(service_module, scenario) == ["Password"]

    def test_prompt_is_not_answered_when_the_screen_moves_on(
        self, service_module, quick_debounce
    ):
        async def scenario(watch):
            watch.plugin._consume_output(PASSWORD)
            watch.plugin._schedule_prompt_check()
            watch.plugin._consume_output("x")  # the user (or gpclient) typed
            await asyncio.sleep(0.2)

        assert run_watch(service_module, scenario) == []

    def test_same_prompt_again_after_the_answer_was_finalised_is_new(
        self, service_module, quick_debounce
    ):
        async def scenario(watch):
            frames = InquireFrames()
            for _ in range(2):
                await watch.feed(frames.draw([OTP], 0, len(OTP)))
                await watch.feed(frames.finish(OTP + "123456"))

        assert run_watch(service_module, scenario) == [
            "Enter the next tokencode",
            "Enter the next tokencode",
        ]

    def test_no_prompt_once_the_tunnel_is_up(self, service_module, quick_debounce):
        async def scenario(watch):
            watch.plugin._tunnel_up = True
            await watch.feed(PASSWORD)

        assert run_watch(service_module, scenario) == []

    def test_answered_list_line_is_not_a_prompt(self, service_module, quick_debounce):
        async def scenario(watch):
            watch.plugin._answered_select = "Which gateway do you want to connect to?"
            await watch.feed(
                "? Which gateway do you want to connect to? gw-a (a.x)\r\n"
            )

        assert run_watch(service_module, scenario) == []

    def test_answered_marker_is_reset_per_attempt(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._answered_prompt = (3, "Password")
        plugin._reset_output_state()
        assert plugin._answered_prompt is None


# --- the whole pipeline against a stand-in for gpclient --------------------------

FAKE_BODY = r"""
import os, select, sys, tty

OVER = bool(os.environ.get("FAKE_OVER"))
HELP = "[Enter to submit, Esc to cancel]"
USER = "? Username: "
PASS = "? Password: "


def out(text):
    sys.stdout.write(text)
    sys.stdout.flush()


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
frames = InquireFrames()
out("[INFO  gpclient::cli] gpclient started: fake\r\n")
out("Enter login credentials (Portal: portal.example.com)\r\n")
out(frames.draw([USER, HELP], 0, len(USER)))
user = read_answer()
if not OVER:
    out(frames.finish(USER + user))
out(frames.draw([PASS, HELP], 0, len(PASS)))
password = read_answer()
# A rejected password: only the row below the prompt changes
out(frames.draw([PASS, "# Invalid password"], 0, len(PASS)))
extra = b""
if select.select([0], [], [], 1.0)[0]:
    extra = os.read(0, 64)
out("\r\nfake gpclient: user=%r password=%r extra=%r\r\n" % (user, password, extra))
sys.exit(0)
"""


async def _run_fake(service_module, fake_path, username="jdoe", password="s3cret"):
    plugin = service_module.GpclientVPNPlugin()
    plugin.vpn_username = username
    plugin.vpn_password = password
    plugin._reset_phase_state()

    master, slave = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fake_path),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, **getattr(_run_fake, "env", {})},
    )
    os.close(slave)
    plugin._pty_master = master
    plugin.gpclient_process = process

    monitor = asyncio.create_task(plugin._monitor_gpclient_output())
    try:
        await asyncio.wait_for(process.wait(), timeout=12)
        await asyncio.wait_for(monitor, timeout=10)
    finally:
        if not process.returncode:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if not monitor.done():
            monitor.cancel()
        if plugin._prompt_task and not plugin._prompt_task.done():
            plugin._prompt_task.cancel()
    return plugin


class TestTextPromptsOverPty:
    @staticmethod
    def _run(service_module, tmp_path, monkeypatch, over, **credentials):
        import inquire_frames

        fake = tmp_path / "fake-gpclient-frames.py"
        with open(inquire_frames.__file__, encoding="utf-8") as source:
            fake.write_text(source.read() + FAKE_BODY)
        monkeypatch.setattr(service_module, "PROMPT_DEBOUNCE_SECONDS", 0.1)
        _run_fake.env = {"FAKE_OVER": "1"} if over else {}
        try:
            return asyncio.run(_run_fake(service_module, fake, **credentials))
        finally:
            _run_fake.env = {}

    @staticmethod
    def _report(plugin):
        found = [l for l in plugin._recent_lines if l.startswith("fake gpclient:")]
        return found[-1] if found else ""

    @pytest.mark.parametrize("over", [False, True])
    def test_prompts_in_redrawn_frames_are_answered_exactly_once(
        self, service_module, tmp_path, monkeypatch, over
    ):
        plugin = self._run(service_module, tmp_path, monkeypatch, over)

        # Both prompts have a help row below the prompt row (never the last
        # line), and with over=True the second prompt is drawn over the first.
        # The error row that comes afterwards is not answered.
        assert self._report(plugin) == (
            "fake gpclient: user='jdoe' password='s3cret' extra=b''"
        )

    @pytest.mark.parametrize("over", [False, True])
    def test_without_stored_credentials_nothing_is_typed(
        self, service_module, tmp_path, monkeypatch, over, dbus_signals
    ):
        # Nobody can be asked: the login fails and nothing reaches gpclient
        plugin = self._run(service_module, tmp_path, monkeypatch, over, username="")

        assert plugin._login_failed is True
        assert self._report(plugin) == ""
        assert ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_LOGIN_FAILED) in (
            dbus_signals
        )

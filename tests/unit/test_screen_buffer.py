"""
Unit tests for ScreenBuffer, the terminal screen model behind the gateway list
detection (issue #25: inquire redraws incrementally with cursor moves).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import pytest


@pytest.fixture
def screen(service_module):
    return service_module.ScreenBuffer()


class TestText:
    def test_lines_are_written_at_the_cursor(self, screen):
        screen.feed("one\r\ntwo\r\n")
        assert screen.lines()[:2] == ["one", "two"]

    def test_carriage_return_overwrites_the_row(self, screen):
        screen.feed("hello\rHE")
        assert screen.lines() == ["HEllo"]

    def test_text_is_padded_after_a_column_move(self, screen):
        screen.feed("ab\x1b[5Gc")
        assert screen.lines() == ["ab  c"]

    def test_newline_keeps_the_column(self, screen):
        screen.feed("ab\ncd")
        assert screen.lines() == ["ab", "  cd"]

    def test_backspace_moves_left(self, screen):
        screen.feed("abc\bX")
        assert screen.lines() == ["abX"]

    def test_backspace_stops_at_the_left_edge(self, screen):
        screen.feed("\b\bX")
        assert screen.lines() == ["X"]

    def test_trailing_whitespace_is_stripped(self, screen):
        screen.feed("abc   ")
        assert screen.lines() == ["abc"]

    def test_control_characters_are_dropped(self, screen):
        screen.feed("a\x00\x07\x7fb\tc")
        assert screen.lines() == ["abc"]


class TestIgnoredEscapes:
    def test_colours_do_not_show_up(self, screen):
        screen.feed("\x1b[36m> \x1b[0mgw-a\x1b[39m")
        assert screen.lines() == ["> gw-a"]

    def test_cursor_visibility_and_osc_are_ignored(self, screen):
        screen.feed("\x1b[?25lab\x1b]0;title\x07c\x1b[?25h")
        assert screen.lines() == ["abc"]

    def test_unknown_csi_is_ignored(self, screen):
        screen.feed("ab\x1b[5Xc\x1b[1;2Hd\x1b[7~e")
        assert screen.lines() == ["abcde"]

    def test_other_fe_escape_is_ignored(self, screen):
        screen.feed("a\x1bMb")
        assert screen.lines() == ["ab"]


class TestCursorMoves:
    def test_cursor_up_and_rewrite_keeps_the_other_rows(self, screen):
        screen.feed("? Pick\r\n> one\r\n  two\r\n[help]\r\n")
        screen.feed("\x1b[3A\r  one\x1b[K\r\n> two\x1b[K\r\n")
        assert screen.lines()[:4] == ["? Pick", "  one", "> two", "[help]"]

    def test_rewrite_does_not_leave_the_old_text(self, screen):
        screen.feed("long old text\r\nnext\r\n")
        screen.feed("\x1b[2A\rnew\x1b[K")
        assert screen.lines()[:2] == ["new", "next"]

    def test_without_erase_a_shorter_rewrite_keeps_the_tail(self, screen):
        screen.feed("long old text\rnew")
        assert screen.lines() == ["newg old text"]

    def test_down_right_and_left(self, screen):
        screen.feed("a\x1b[2B\x1b[3Cb\x1b[2Dc")
        assert screen.lines() == ["a", "", "   cb"]

    def test_zero_count_moves_by_one(self, screen):
        screen.feed("a\r\nb\x1b[0A\rX")
        assert screen.lines() == ["X", "b"]

    def test_column_is_one_based(self, screen):
        screen.feed("abcdef\x1b[3GX")
        assert screen.lines() == ["abXdef"]

    def test_cursor_never_goes_above_the_first_row(self, screen):
        screen.feed("a\r\x1b[9AX")
        assert screen.lines() == ["X"]

    def test_cursor_never_goes_left_of_the_first_column(self, screen):
        screen.feed("ab\x1b[9DX\x1b[0GY")
        assert screen.lines() == ["Yb"]


class TestErase:
    def test_erase_to_end_of_line(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[K")
        assert screen.lines() == ["abc"]

    def test_erase_to_end_of_line_with_explicit_zero(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[0K")
        assert screen.lines() == ["abc"]

    def test_erase_to_start_of_line_includes_the_cursor(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[1K")
        assert screen.lines() == ["    ef"]

    def test_erase_whole_line_leaves_the_other_rows(self, screen):
        screen.feed("one\r\ntwo\r\nthree\r\x1b[2A\x1b[2K")
        assert screen.lines() == ["", "two", "three"]

    def test_erase_below_clears_the_rest_of_the_screen(self, screen):
        screen.feed("one\r\ntwo\r\nthree\x1b[2A\x1b[3D\x1b[J")
        assert screen.lines() == ["on"]

    def test_erase_below_keeps_the_text_before_the_cursor(self, screen):
        screen.feed("one\r\ntwo\x1b[1A\x1b[2D\x1b[0J")
        assert screen.lines() == ["o"]

    @pytest.mark.parametrize("sequence", ["\x1b[2J", "\x1b[3J"])
    def test_erase_all(self, screen, sequence):
        screen.feed("one\r\ntwo")
        screen.feed(sequence + "x")
        assert screen.lines() == ["x"]

    def test_erase_to_end_of_line_without_a_row_is_harmless(self, screen):
        screen.feed("\x1b[K\x1b[2K\x1b[J")
        assert screen.lines() == [""]


class TestVersionAndLimit:
    def test_version_changes_with_output(self, screen):
        before = screen.version
        screen.feed("a")
        assert screen.version != before

    def test_version_is_stable_without_output(self, screen):
        screen.feed("a")
        version = screen.version
        screen.feed("")
        assert screen.version == version

    def test_rows_are_capped_and_the_newest_are_kept(self, service_module, screen):
        cap = service_module.ScreenBuffer.MAX_ROWS
        screen.feed("".join(f"row {i}\r\n" for i in range(cap * 2)))
        lines = screen.lines()
        assert len(lines) <= cap
        assert lines[-2] == f"row {cap * 2 - 1}"
        assert "row 0" not in lines

    def test_cursor_row_follows_the_trimming(self, service_module, screen):
        cap = service_module.ScreenBuffer.MAX_ROWS
        screen.feed("".join(f"row {i}\r\n" for i in range(cap + 10)))
        screen.feed("\x1b[1A\rX")
        assert screen.lines()[-2] == f"Xow {cap + 9}"
        assert screen.lines()[-3] == f"row {cap + 8}"

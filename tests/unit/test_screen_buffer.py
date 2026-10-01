"""
Unit tests for ScreenBuffer, the terminal screen model behind the gateway list
detection (issue #25: inquire redraws incrementally with cursor moves).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import time

import pytest


@pytest.fixture
def screen(service_module):
    return service_module.ScreenBuffer()


class TestText:
    def test_lines_are_written_at_the_cursor(self, screen):
        screen.feed("one\r\ntwo\r\n")
        assert list(screen.lines())[:2] == ["one", "two"]

    def test_carriage_return_overwrites_the_row(self, screen):
        screen.feed("hello\rHE")
        assert list(screen.lines()) == ["HEllo"]

    def test_text_is_padded_after_a_column_move(self, screen):
        screen.feed("ab\x1b[5Gc")
        assert list(screen.lines()) == ["ab  c"]

    def test_newline_keeps_the_column(self, screen):
        screen.feed("ab\ncd")
        assert list(screen.lines()) == ["ab", "  cd"]

    def test_backspace_moves_left(self, screen):
        screen.feed("abc\bX")
        assert list(screen.lines()) == ["abX"]

    def test_backspace_stops_at_the_left_edge(self, screen):
        screen.feed("\b\bX")
        assert list(screen.lines()) == ["X"]

    def test_trailing_whitespace_is_stripped(self, screen):
        screen.feed("abc   ")
        assert list(screen.lines()) == ["abc"]

    def test_control_characters_are_dropped(self, screen):
        screen.feed("a\x00\x07\x7fb\x01c")
        assert list(screen.lines()) == ["abc"]

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("\tX", " " * 8 + "X"),
            ("abc\tX", "abc" + " " * 5 + "X"),
            ("12345678\tX", "12345678" + " " * 8 + "X"),
        ],
    )
    def test_tab_moves_to_the_next_multiple_of_8(self, screen, text, expected):
        screen.feed(text)
        assert list(screen.lines()) == [expected]

    def test_tab_stops_at_the_right_edge(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("\t" * width + "X")
        assert list(screen.lines()) == [" " * (width - 1) + "X"]

    @pytest.mark.parametrize("char", ["\x0b", "\x0c"])
    def test_vt_and_ff_are_line_feeds(self, screen, char):
        screen.feed(f"ab{char}cd")
        assert list(screen.lines()) == ["ab", "  cd"]


class TestIgnoredEscapes:
    def test_colours_do_not_show_up(self, screen):
        screen.feed("\x1b[36m> \x1b[0mgw-a\x1b[39m")
        assert list(screen.lines()) == ["> gw-a"]

    def test_cursor_visibility_and_osc_are_ignored(self, screen):
        screen.feed("\x1b[?25lab\x1b]0;title\x07c\x1b[?25h")
        assert list(screen.lines()) == ["abc"]

    def test_unknown_csi_is_ignored(self, screen):
        screen.feed("ab\x1b[5Xc\x1b[1;2Hd\x1b[7~e")
        assert list(screen.lines()) == ["abcde"]

    def test_other_fe_escape_is_ignored(self, screen):
        screen.feed("a\x1bMb")
        assert list(screen.lines()) == ["ab"]


class TestCursorMoves:
    def test_cursor_up_and_rewrite_keeps_the_other_rows(self, screen):
        screen.feed("? Pick\r\n> one\r\n  two\r\n[help]\r\n")
        screen.feed("\x1b[3A\r  one\x1b[K\r\n> two\x1b[K\r\n")
        assert list(screen.lines())[:4] == ["? Pick", "  one", "> two", "[help]"]

    def test_rewrite_does_not_leave_the_old_text(self, screen):
        screen.feed("long old text\r\nnext\r\n")
        screen.feed("\x1b[2A\rnew\x1b[K")
        assert list(screen.lines())[:2] == ["new", "next"]

    def test_without_erase_a_shorter_rewrite_keeps_the_tail(self, screen):
        screen.feed("long old text\rnew")
        assert list(screen.lines()) == ["newg old text"]

    def test_down_right_and_left(self, screen):
        screen.feed("a\x1b[2B\x1b[3Cb\x1b[2Dc")
        assert list(screen.lines()) == ["a", "", "   cb"]

    def test_zero_count_moves_by_one(self, screen):
        screen.feed("a\r\nb\x1b[0A\rX")
        assert list(screen.lines()) == ["X", "b"]

    def test_column_is_one_based(self, screen):
        screen.feed("abcdef\x1b[3GX")
        assert list(screen.lines()) == ["abXdef"]

    def test_cursor_never_goes_above_the_first_row(self, screen):
        screen.feed("a\r\x1b[9AX")
        assert list(screen.lines()) == ["X"]

    def test_cursor_never_goes_left_of_the_first_column(self, screen):
        screen.feed("ab\x1b[9DX\x1b[0GY")
        assert list(screen.lines()) == ["Yb"]


class TestErase:
    def test_erase_to_end_of_line(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[K")
        assert list(screen.lines()) == ["abc"]

    def test_erase_to_end_of_line_with_explicit_zero(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[0K")
        assert list(screen.lines()) == ["abc"]

    def test_erase_to_start_of_line_includes_the_cursor(self, screen):
        screen.feed("abcdef\x1b[3D\x1b[1K")
        assert list(screen.lines()) == ["    ef"]

    def test_erase_whole_line_leaves_the_other_rows(self, screen):
        screen.feed("one\r\ntwo\r\nthree\r\x1b[2A\x1b[2K")
        assert list(screen.lines()) == ["", "two", "three"]

    def test_erase_below_clears_the_rest_of_the_screen(self, screen):
        screen.feed("one\r\ntwo\r\nthree\x1b[2A\x1b[3D\x1b[J")
        assert list(screen.lines()) == ["on"]

    def test_erase_below_keeps_the_text_before_the_cursor(self, screen):
        screen.feed("one\r\ntwo\x1b[1A\x1b[2D\x1b[0J")
        assert list(screen.lines()) == ["o"]

    def test_erase_display_clears_content_but_keeps_the_cursor(self, screen):
        screen.feed("one\r\ntwo\x1b[2Jx")
        assert list(screen.lines()) == ["", "   x"]

    def test_relative_moves_still_line_up_after_erase_display(self, screen):
        screen.feed("one\r\ntwo\x1b[2J\x1b[1A\rY")
        assert list(screen.lines()) == ["Y", ""]

    def test_erase_scrollback_leaves_the_screen_alone(self, screen):
        screen.feed("one\r\ntwo\x1b[3Jx")
        assert list(screen.lines()) == ["one", "twox"]

    def test_erase_to_end_of_line_without_a_row_is_harmless(self, screen):
        screen.feed("\x1b[K\x1b[2K\x1b[J")
        assert list(screen.lines()) == [""]


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
        lines = list(screen.lines())
        assert len(lines) <= cap
        assert lines[-2] == f"row {cap * 2 - 1}"
        assert "row 0" not in lines

    def test_cursor_row_follows_the_trimming(self, service_module, screen):
        cap = service_module.ScreenBuffer.MAX_ROWS
        screen.feed("".join(f"row {i}\r\n" for i in range(cap + 10)))
        screen.feed("\x1b[1A\rX")
        assert list(screen.lines())[-2] == f"Xow {cap + 9}"
        assert list(screen.lines())[-3] == f"row {cap + 8}"


class TestBounds:
    """Output must never make the buffer grow without limit (root daemon)"""

    @pytest.mark.parametrize(
        "text",
        [
            "\x1b[30000000B",
            "\x1b[300000000C" + "x",
            "\x1b[300000000G" + "x",
            "\n" * 100000,
            "x" * 100000,
            "\x1b[" + "9" * 6000 + "Bx",
            "\x1b[" + "9" * 6000 + "Cx",
        ],
    )
    def test_huge_counts_stay_inside_the_grid(self, service_module, screen, text):
        started = time.monotonic()
        screen.feed(text)
        assert time.monotonic() - started < 2
        lines = list(screen.lines())
        assert len(lines) <= service_module.ScreenBuffer.MAX_ROWS
        assert all(len(line) <= service_module.PTY_COLUMNS for line in lines)
        assert len(screen._rows) <= service_module.ScreenBuffer.MAX_ROWS
        assert all(len(row) <= service_module.PTY_COLUMNS for row in screen._rows)

    def test_column_moves_clamp_to_the_last_column(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("\x1b[999Cx")
        assert list(screen.lines()) == [" " * (width - 1) + "x"]

    def test_text_wraps_at_the_right_edge(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("a" * width + "bc")
        assert list(screen.lines()) == ["a" * width, "bc"]

    def test_text_inside_the_width_does_not_wrap(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("a" * width)
        assert list(screen.lines()) == ["a" * width]

    def test_pty_size_matches_the_window_size_the_service_sets(self, service_module):
        source = open(service_module.__file__).read()
        assert 'struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0)' in source


class TestMalformedSequences:
    @pytest.mark.parametrize(
        "text",
        ["a\x1b[1?2hb", "a\x1b[?1?2hb", "a\x1b[1;2?3Hb", "a\x1b[2?Jb", "a\x1b[9?Bb"],
    )
    def test_params_with_a_misplaced_question_mark_are_ignored(self, screen, text):
        screen.feed(text)
        assert list(screen.lines()) == ["ab"]

    def test_valid_sequences_after_a_malformed_one_still_work(self, screen):
        screen.feed("a\x1b[1?2hb\x1b[2Dc")
        assert list(screen.lines()) == ["cb"]


class TestEscapeCoverage:
    MIXED = "a\x1b7b\x1b[>4;1mc\x1b(Bd\x1b8\x1b[38:5:1me\r\n"

    def test_save_cursor_private_csi_charset_and_colon_sgr_are_ignored(self, screen):
        screen.feed(self.MIXED)
        assert list(screen.lines())[0] == "abcde"

    def test_strip_ansi_removes_the_same_sequences(self, service_module):
        assert service_module.strip_ansi(self.MIXED) == "abcde\r\n"

    @pytest.mark.parametrize("params", ["<1", "=1", ">1", "?1"])
    def test_private_parameter_prefixes_are_ignored(self, screen, params):
        screen.feed(f"abc\x1b[{params}Dd")
        assert list(screen.lines()) == ["abcd"]

    def test_plain_text_that_looks_like_an_escape_tail_is_kept(
        self, screen, service_module
    ):
        text = "a7b(Bc8[38:5:1me"
        screen.feed(text)
        assert list(screen.lines()) == [text]
        assert service_module.strip_ansi(text) == text

    @pytest.mark.parametrize(
        "text",
        ["a\x1b[>4;1", "a\x1b[38:5", "a\x1b(", "a\x1b[1 "],
    )
    def test_cut_sequences_are_held_back(self, service_module, text):
        assert service_module.INCOMPLETE_ANSI_RE.search(text) is not None

    @pytest.mark.parametrize("text", ["a\x1b7", "a\x1b(B", "a\x1b[>4;1m", "a(", "a7"])
    def test_complete_sequences_and_plain_text_are_not_held_back(
        self, service_module, text
    ):
        assert service_module.INCOMPLETE_ANSI_RE.search(text) is None


class TestOverlongOsc:
    BODY = "x" * 300

    def test_body_split_across_two_feeds_is_not_shown(self, screen):
        screen.feed("a\x1b]0;" + self.BODY)
        screen.feed(self.BODY + "\x07b")
        assert list(screen.lines()) == ["ab"]

    def test_string_terminator_ends_it_too(self, screen):
        screen.feed("a\x1b]0;" + self.BODY)
        screen.feed(self.BODY + "\x1b\\b")
        assert list(screen.lines()) == ["ab"]

    def test_body_stays_hidden_over_several_feeds(self, screen):
        screen.feed("\x1b]0;" + self.BODY)
        screen.feed(self.BODY)
        screen.feed("end\x07ok")
        assert list(screen.lines()) == ["ok"]

    def test_text_before_and_after_are_kept(self, screen):
        screen.feed("before\x1b]0;" + self.BODY)
        screen.feed("\x07after")
        assert list(screen.lines()) == ["beforeafter"]

    def test_terminated_osc_does_not_swallow_the_next_feed(self, screen):
        screen.feed("a\x1b]0;title\x07b")
        screen.feed("c")
        assert list(screen.lines()) == ["abc"]

    def test_overlong_osc_does_not_leak_through_the_service(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._consume_output("a\x1b]0;" + self.BODY)
        plugin._consume_output("\x07b\r\n")
        assert list(plugin._screen.lines())[0] == "ab"


class TestLinesCache:
    def test_same_object_until_the_next_feed(self, screen):
        screen.feed("abc")
        first = screen.lines()
        assert screen.lines() is first

    def test_new_object_after_a_feed(self, screen):
        screen.feed("abc")
        first = screen.lines()
        screen.feed("d")
        second = screen.lines()
        assert second is not first
        assert second[0] == "abcd"
        assert first[0] == "abc"

    def test_result_cannot_be_mutated(self, screen):
        screen.feed("abc")
        with pytest.raises(TypeError):
            screen.lines()[0] = "x"

    def test_select_detection_accepts_the_cached_lines(self, service_module, screen):
        screen.feed(
            "? Which gateway?\r\n> gw-a (a.example.com)\r\n  gw-b (b.example.com)\r\n"
            "[to move, to select]\r\n"
        )
        frame = service_module.detect_select_prompt(screen.lines())
        assert frame["options"] == ["gw-a (a.example.com)", "gw-b (b.example.com)"]


class TestDisplayWidth:
    def test_wide_characters_take_two_columns(self, screen):
        screen.feed("日本\x1b[5Gx")
        assert list(screen.lines()) == ["日本x"]

    def test_ascii_takes_one_column(self, screen):
        screen.feed("ab\x1b[5Gx")
        assert list(screen.lines()) == ["ab  x"]

    def test_fullwidth_form_is_wide(self, screen):
        screen.feed("Ａ\x1b[3Gx")
        assert list(screen.lines()) == ["Ａx"]

    def test_overwriting_half_a_wide_character_blanks_the_rest(self, screen):
        screen.feed("日本\r")
        screen.feed("ab")
        assert list(screen.lines()) == ["ab本"]

    def test_overwriting_the_second_half_blanks_the_first(self, screen):
        screen.feed("日\x1b[2Gx")
        assert list(screen.lines()) == [" x"]

    def test_wide_character_over_two_narrow_ones(self, screen):
        screen.feed("abc\r日")
        assert list(screen.lines()) == ["日c"]

    def test_combining_mark_does_not_advance(self, screen):
        screen.feed("é\x1b[1Dy")
        assert list(screen.lines()) == ["y"]

    def test_combining_mark_joins_the_previous_character(self, screen):
        screen.feed("éx")
        assert list(screen.lines()) == ["éx"]

    def test_combining_mark_after_a_wide_character(self, screen):
        screen.feed("日́x")
        assert list(screen.lines()) == ["日́x"]

    def test_combining_mark_at_the_line_start_is_dropped(self, screen):
        screen.feed("́a")
        assert list(screen.lines()) == ["a"]

    def test_wide_character_wraps_instead_of_splitting(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("a" * (width - 1) + "日")
        assert list(screen.lines())[:2] == ["a" * (width - 1), "日"]

    def test_wide_character_fits_in_the_last_two_columns(self, service_module, screen):
        width = service_module.PTY_COLUMNS
        screen.feed("a" * (width - 2) + "日")
        assert list(screen.lines()) == ["a" * (width - 2) + "日"]

    def test_narrow_character_still_fits_in_the_last_column(
        self, service_module, screen
    ):
        width = service_module.PTY_COLUMNS
        screen.feed("a" * width)
        assert list(screen.lines()) == ["a" * width]

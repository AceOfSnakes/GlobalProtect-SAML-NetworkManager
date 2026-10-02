"""
Unit tests for gateway selection and browser resolution in the nm-gpclient
service (issue #7: portal addresses, gateway lists, browsers that never opened).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio

import pytest

# A single-page gateway list as inquire renders it: the question, one line per
# option (marker, space, value; '>' marks the cursor) and the help footer. Every
# line is terminated, so nothing is left in the output tail.
SINGLE_PAGE = [
    "[2026-07-20T12:44:26Z INFO  gpapi::portal::config] Retrieve the portal config",
    "? Which gateway do you want to connect to?",
    "> gw-warsaw (gw1.example.com)",
    "  gw-frankfurt (gw2.example.com)",
    "  gw-london (gw3.example.com)",
    "[↑↓ to move, enter to select, type to filter]",
]

# More entries than inquire's page size: the edge row carries a scroll marker
PAGED = [
    "? Which gateway do you want to connect to?",
    "> gw-01 (gw01.example.com)",
    "  gw-02 (gw02.example.com)",
    "  gw-03 (gw03.example.com)",
    "  gw-04 (gw04.example.com)",
    "  gw-05 (gw05.example.com)",
    "  gw-06 (gw06.example.com)",
    "v gw-07 (gw07.example.com)",
    "[↑↓ to move, enter to select, type to filter]",
]


def frame_with_cursor(options, cursor, more=False):
    """Build a detected-frame dict the way detect_select_prompt() would"""
    return {
        "message": "Which gateway do you want to connect to?",
        "options": list(options),
        "cursor": cursor,
        "more": more,
    }


def make_plugin(service_module, preferred="", sent=None):
    plugin = service_module.GpclientVPNPlugin()
    plugin.preferred_gateway = preferred
    if sent is not None:
        plugin._write_keys = lambda data, description: sent.append(data)
    return plugin


class TestDetectSelectPrompt:
    def test_single_page_frame(self, service_module):
        frame = service_module.detect_select_prompt(SINGLE_PAGE)
        assert frame["message"] == "Which gateway do you want to connect to?"
        assert frame["options"] == [
            "gw-warsaw (gw1.example.com)",
            "gw-frankfurt (gw2.example.com)",
            "gw-london (gw3.example.com)",
        ]
        assert frame["cursor"] == 0
        assert frame["more"] is False

    def test_scroll_marker_means_more_entries(self, service_module):
        frame = service_module.detect_select_prompt(PAGED)
        assert frame["more"] is True
        assert len(frame["options"]) == 7
        # The scroll-marked row is still an option
        assert frame["options"][-1] == "gw-07 (gw07.example.com)"

    def test_cursor_is_tracked(self, service_module):
        lines = [
            "? Which gateway do you want to connect to?",
            "  gw-warsaw (gw1.example.com)",
            "> gw-frankfurt (gw2.example.com)",
            "[↑↓ to move, enter to select, type to filter]",
        ]
        frame = service_module.detect_select_prompt(lines)
        assert frame["cursor"] == 1

    def test_option_name_starting_like_a_marker_is_kept(self, service_module):
        # 'v'/'^'/'>' as the first letter of a gateway name must not be eaten:
        # only a marker in column 0 followed by a space is a marker
        lines = [
            "? Which gateway do you want to connect to?",
            "> vpn-central (gwv.example.com)",
            "  ^caret-name (gwc.example.com)",
            "[↑↓ to move, enter to select, type to filter]",
        ]
        frame = service_module.detect_select_prompt(lines)
        assert frame["options"] == [
            "vpn-central (gwv.example.com)",
            "^caret-name (gwc.example.com)",
        ]
        assert frame["more"] is False

    def test_only_the_latest_frame_is_used(self, service_module):
        # A redraw appends a second frame; the newest one wins
        frame = service_module.detect_select_prompt(
            SINGLE_PAGE
            + [
                "? Which gateway do you want to connect to?",
                "  gw-warsaw (gw1.example.com)",
                "> gw-frankfurt (gw2.example.com)",
                "  gw-london (gw3.example.com)",
                "[↑↓ to move, enter to select, type to filter]",
            ]
        )
        assert frame["cursor"] == 1

    def test_regular_output_is_not_a_frame(self, service_module):
        assert service_module.detect_select_prompt(SINGLE_PAGE[:1]) is None
        assert service_module.detect_select_prompt([]) is None

    def test_frame_without_a_highlighted_entry_is_not_a_frame(self, service_module):
        # A Select always highlights one entry: this is a frame caught in the
        # middle of a redraw (issue #25)
        lines = [
            "? Which gateway do you want to connect to?",
            "  gw-a (a.example.com)",
            "  gw-b (b.example.com)",
            "[↑↓ to move, enter to select, type to filter]",
        ]
        assert service_module.detect_select_prompt(lines) is None

    def test_help_footer_without_question_is_not_a_frame(self, service_module):
        assert (
            service_module.detect_select_prompt(
                ["some output", "[↑↓ to move, enter to select]"]
            )
            is None
        )

    def test_text_prompt_is_not_a_list_frame(self, service_module):
        # The RSA/username flow must keep going through detect_prompt()
        assert (
            service_module.detect_select_prompt(
                ["Please enter RSA token (Portal: vpn.example.com)", "? Username: "]
            )
            is None
        )


class TestGatewayQuestionIsNotAUsername:
    """Regression for the misclassification the list prompt used to cause."""

    def test_question_never_reaches_the_text_prompt_path(self, service_module):
        # The frame ends with a terminated help line, so the tail is empty and
        # detect_prompt() sees nothing to answer
        scanner = service_module.OutputScanner()
        scanner.feed("\r\n".join(SINGLE_PAGE) + "\r\n")
        assert scanner.tail == ""
        assert service_module.detect_prompt(scanner.tail) is None


class TestPickGateway:
    OPTIONS = [
        "gw-warsaw (gw1.example.com)",
        "gw-frankfurt (gw2.example.com)",
        "gw-london (gw3.example.com)",
    ]

    def test_empty_preference_takes_the_first(self, service_module):
        assert service_module.pick_gateway(self.OPTIONS, "") == self.OPTIONS[0]

    def test_exact_entry(self, service_module):
        assert (
            service_module.pick_gateway(self.OPTIONS, "gw-london (gw3.example.com)")
            == self.OPTIONS[2]
        )

    def test_by_name(self, service_module):
        assert (
            service_module.pick_gateway(self.OPTIONS, "gw-frankfurt") == self.OPTIONS[1]
        )

    def test_by_host(self, service_module):
        assert (
            service_module.pick_gateway(self.OPTIONS, "gw3.example.com")
            == self.OPTIONS[2]
        )

    def test_case_insensitive(self, service_module):
        assert service_module.pick_gateway(self.OPTIONS, "GW-WARSAW") == self.OPTIONS[0]

    def test_exact_name_wins_over_substring(self, service_module):
        options = ["gw-1 (a.example.com)", "gw-10 (b.example.com)"]
        # "gw-10" appears in no other name, "gw-1" must not grab it
        assert service_module.pick_gateway(options, "gw-10") == options[1]

    def test_unknown_gateway(self, service_module):
        assert service_module.pick_gateway(self.OPTIONS, "gw-tokyo") is None

    @pytest.mark.parametrize("preferred", ["", "x", "  ", None])
    def test_no_options_means_no_gateway(self, service_module, preferred):
        assert service_module.pick_gateway([], preferred) is None

    def test_whitespace_preference_takes_the_first(self, service_module):
        assert service_module.pick_gateway(self.OPTIONS, "   ") == self.OPTIONS[0]

    def test_substring_fallback_is_kept(self, service_module):
        # Design decision: with no exact name/host match a substring wins
        options = ["gw-10 (b.example.com)", "gw-20 (c.example.com)"]
        assert service_module.pick_gateway(options, "gw-1") == options[0]

    def test_exact_match_after_a_substring_match_wins(self, service_module):
        options = ["gw-10 (b.example.com)", "gw-1 (a.example.com)"]
        assert service_module.pick_gateway(options, "gw-1") == options[1]


class TestPickGatewayTiers:
    """Three tiers: the whole entry, then name/host, then a substring"""

    def test_whole_entry_wins_over_a_host_match_of_an_earlier_option(
        self, service_module
    ):
        # The host of the first option equals the preference, but the second
        # option IS the preference
        options = ["x (a.example.com)", "a.example.com"]
        assert service_module.pick_gateway(options, "a.example.com") == options[1]

    def test_whole_entry_is_case_insensitive(self, service_module):
        options = ["x (a.example.com)", "A.Example.COM"]
        assert service_module.pick_gateway(options, "a.example.com") == options[1]

    def test_name_or_host_match_wins_over_a_substring(self, service_module):
        options = ["gw-10 (a.example.com)", "gw-1 (b.example.com)"]
        assert service_module.pick_gateway(options, "gw-1") == options[1]

    def test_without_a_whole_entry_the_first_name_or_host_match_is_taken(
        self, service_module
    ):
        # Counterpart: no whole entry is the preference, so the order of the
        # options decides between the name/host matches
        options = ["x (a.example.com)", "a.example.com (y)"]
        assert service_module.pick_gateway(options, "a.example.com") == options[0]


class TestPickGatewayUsesGatewayMatches:
    """There is one definition of "matches the preferred gateway":
    gateway_matches(), exact tiers (substring=False) before the substring one."""

    OPTIONS = ["a (a.example.com)", "b (b.example.com)", "c (c.example.com)"]

    def test_exact_tier_is_asked_first_and_wins(self, service_module, monkeypatch):
        calls = []

        def fake(preferred, option, substring=True):
            calls.append(substring)
            return option == self.OPTIONS[2] if not substring else True

        monkeypatch.setattr(service_module, "gateway_matches", fake)

        assert service_module.pick_gateway(self.OPTIONS, "x") == self.OPTIONS[2]
        # Every option was offered to the exact tier before any substring one
        assert calls == [False, False, False]

    def test_substring_tier_is_the_fallback(self, service_module, monkeypatch):
        def fake(preferred, option, substring=True):
            return substring and option == self.OPTIONS[1]

        monkeypatch.setattr(service_module, "gateway_matches", fake)

        assert service_module.pick_gateway(self.OPTIONS, "x") == self.OPTIONS[1]

    def test_no_tier_matching_means_no_gateway(self, service_module, monkeypatch):
        monkeypatch.setattr(
            service_module, "gateway_matches", lambda *args, **kwargs: False
        )

        assert service_module.pick_gateway(self.OPTIONS, "x") is None


class TestAnswerGatewayList:
    def _run(self, plugin, frame, stored_complete=True):
        # By default the profile already holds the whole list, so a paged list
        # is walked only to find the gateway (reading the pages has its own
        # tests, in TestCollectGatewayPages)
        if stored_complete:
            plugin._stored_gateway_list = ";".join(frame["options"])
            plugin._stored_gateway_count = len(frame["options"])
        asyncio.run(plugin._handle_select_prompt(frame))

    def test_no_preference_selects_the_first_proposal(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="", sent=sent)
        plugin._press_list_down = lambda previous: (_ for _ in ()).throw(
            AssertionError("must not walk the list")
        )

        self._run(plugin, service_module.detect_select_prompt(SINGLE_PAGE))

        assert sent == [service_module.KEY_ENTER]

    def test_preferred_gateway_is_reached_with_down_keys(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-london", sent=sent)

        options = service_module.detect_select_prompt(SINGLE_PAGE)["options"]
        moves = [frame_with_cursor(options, 1), frame_with_cursor(options, 2)]

        async def fake_down(previous):
            return moves.pop(0)

        plugin._press_list_down = fake_down
        self._run(plugin, service_module.detect_select_prompt(SINGLE_PAGE))

        # Two moves down to the third entry, then confirm
        assert sent == [service_module.KEY_ENTER]
        assert moves == []

    def test_unavailable_preference_falls_back_to_first(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-tokyo", sent=sent)
        plugin._press_list_down = lambda previous: (_ for _ in ()).throw(
            AssertionError("must not walk a fully visible list")
        )

        self._run(plugin, service_module.detect_select_prompt(SINGLE_PAGE))

        assert sent == [service_module.KEY_ENTER]
        # The user's setting must survive the fallback
        assert plugin.preferred_gateway == "gw-tokyo"

    def test_paged_list_is_walked_and_wraps_back(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-99", sent=sent)

        frame = service_module.detect_select_prompt(PAGED)
        options = frame["options"]
        # Walk through every entry and come back to the starting one
        moves = [
            frame_with_cursor(options, index, more=True)
            for index in list(range(1, len(options))) + [0]
        ]

        async def fake_down(previous):
            return moves.pop(0)

        plugin._press_list_down = fake_down
        self._run(plugin, frame)

        # One Enter after the wrap-around, i.e. the first proposal
        assert sent == [service_module.KEY_ENTER]
        assert moves == []

    @staticmethod
    def _walk(plugin, options, cursors, sent):
        """Drive the walk: `cursors` are the positions after each Down key.

        `sent` records (key, entry under the cursor when it was sent).
        """
        position = []
        moves = [frame_with_cursor(options, c, more=True) for c in cursors]

        async def fake_down(previous):
            frame = moves.pop(0)
            position.append(frame["options"][frame["cursor"]])
            return frame

        plugin._press_list_down = fake_down
        plugin._write_keys = lambda data, description: sent.append(
            (data, position[-1] if position else options[0])
        )
        return moves

    def test_paged_list_finds_the_preferred_gateway_after_walking(
        self, service_module
    ):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-05")
        frame = service_module.detect_select_prompt(PAGED)
        options = frame["options"]
        moves = self._walk(plugin, options, [1, 2, 3, 4], sent)

        self._run(plugin, frame)

        assert sent == [(service_module.KEY_ENTER, "gw-05 (gw05.example.com)")]
        assert moves == []

    def test_paged_list_exact_match_beats_an_earlier_substring_match(
        self, service_module
    ):
        # "gw-1" is a substring of "gw-10", which comes first - the entry that
        # is really called gw-1 must still win while walking the pages
        sent = []
        plugin = make_plugin(service_module, preferred="gw-1")
        options = [
            "gw-10 (a.example.com)",
            "gw-11 (b.example.com)",
            "gw-1 (c.example.com)",
            "gw-12 (d.example.com)",
        ]
        frame = frame_with_cursor(options, 0, more=True)
        moves = self._walk(plugin, options, [1, 2], sent)

        self._run(plugin, frame)

        assert sent == [(service_module.KEY_ENTER, "gw-1 (c.example.com)")]
        assert moves == []

    def test_paged_list_exact_host_match_beats_an_earlier_substring_match(
        self, service_module
    ):
        sent = []
        plugin = make_plugin(service_module, preferred="a.example.com")
        options = [
            "gw-x (aa.example.com)",
            "gw-y (a.example.com)",
        ]
        frame = frame_with_cursor(options, 0, more=True)
        moves = self._walk(plugin, options, [1], sent)

        self._run(plugin, frame)

        assert sent == [(service_module.KEY_ENTER, "gw-y (a.example.com)")]
        assert moves == []

    def test_paged_list_falls_back_to_the_substring_match_without_exact(
        self, service_module
    ):
        # No entry is called gw-1: the substring match is kept as the fallback
        # (as for a list that fits on one page), not the first proposal
        sent = []
        plugin = make_plugin(service_module, preferred="gw-1")
        options = [
            "gw-20 (a.example.com)",
            "gw-10 (b.example.com)",
            "gw-30 (c.example.com)",
        ]
        frame = frame_with_cursor(options, 0, more=True)
        # Down x3 wraps back to the start, then one more to the fallback
        moves = self._walk(plugin, options, [1, 2, 0, 1], sent)

        self._run(plugin, frame)

        assert sent == [(service_module.KEY_ENTER, "gw-10 (b.example.com)")]
        assert moves == []

    def test_paged_list_substring_match_at_the_start_is_selected_after_wrap(
        self, service_module
    ):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-1")
        options = ["gw-10 (a.example.com)", "gw-20 (b.example.com)"]
        frame = frame_with_cursor(options, 0, more=True)
        moves = self._walk(plugin, options, [1, 0], sent)

        self._run(plugin, frame)

        assert sent == [(service_module.KEY_ENTER, "gw-10 (a.example.com)")]
        assert moves == []

    @staticmethod
    def _long_list(plugin, options):
        """A paged list of `options` that wraps like inquire's; returns what
        the walk did: Down keys pressed and the entry under the cursor at Enter"""
        state = {"cursor": 0, "downs": 0, "selected": []}

        async def fake_down(previous):
            state["cursor"] = (state["cursor"] + 1) % len(options)
            state["downs"] += 1
            return frame_with_cursor(options, state["cursor"], more=True)

        plugin._press_list_down = fake_down
        plugin._write_keys = lambda data, description: state["selected"].append(
            options[state["cursor"]]
        )
        return state

    @staticmethod
    def _gateways(count):
        return [f"gw-{n:03d} (gw{n:03d}.example.com)" for n in range(count)]

    def test_long_list_substring_match_is_found_within_two_laps(self, service_module):
        # Only a substring hit, 60 entries in, on a list of 150: the first lap
        # (150 Downs) plus the way back to the hit (60) is 210 Downs - more
        # than SELECT_MAX_STEPS, which must bound the first lap only
        options = self._gateways(150)
        options[60] = "gw-frankfurt (fra.example.com)"
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-frankfurt (fra.example.com)"]
        assert state["downs"] == 150 + 60
        assert state["downs"] < 2 * 150

    def test_long_list_exact_match_after_a_substring_hit_wins(self, service_module):
        options = self._gateways(150)
        options[60] = "gw-frankfurt-old (fra-old.example.com)"
        options[100] = "frankfurt (fra.example.com)"
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["frankfurt (fra.example.com)"]
        assert state["downs"] == 100  # no second lap

    def test_long_list_without_any_match_selects_the_first_proposal(
        self, service_module
    ):
        options = self._gateways(150)
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == [options[0]]
        assert state["downs"] == 150  # one lap, back at the start

    def test_endless_list_gives_up_after_the_step_limit(self, service_module):
        # The first lap is longer than SELECT_MAX_STEPS
        options = self._gateways(500)
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["downs"] == service_module.SELECT_MAX_STEPS
        assert len(state["selected"]) == 1

    def test_long_list_substring_hit_beyond_the_step_limit_is_still_selected(
        self, service_module
    ):
        # 250 entries, only a substring hit at 5: the first lap is cut off at
        # SELECT_MAX_STEPS, but the hit is known by name, so the walk carries
        # on to it (down to the wrap-around and 5 more) instead of selecting
        # whatever is under the cursor at step 200
        options = self._gateways(250)
        options[5] = "gw-frankfurt (fra.example.com)"
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-frankfurt (fra.example.com)"]
        assert state["downs"] == 250 + 5

    def test_long_list_without_a_substring_hit_still_gives_up_at_the_limit(
        self, service_module
    ):
        # Counterpart: nothing to walk back to, so the limit applies as before
        options = self._gateways(250)
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["downs"] == service_module.SELECT_MAX_STEPS
        assert state["selected"] == [options[service_module.SELECT_MAX_STEPS]]

    def test_walk_to_a_substring_hit_has_its_own_step_limit(self, service_module):
        # The hit is 505 Downs away on a list of 500: beyond the extra budget
        options = self._gateways(500)
        options[5] = "gw-frankfurt (fra.example.com)"
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        limit = service_module.SELECT_MAX_STEPS
        assert state["downs"] == 2 * limit
        assert state["selected"] == [options[2 * limit]]  # gave up where it was

    def test_exact_match_seen_while_homing_wins(self, service_module):
        # 320 entries: the substring hit "gw-10" at 5, the entry really called
        # "gw-1" at 250. The first lap is cut off at SELECT_MAX_STEPS (200),
        # before the exact one, and the walk goes on to the substring hit -
        # through 250, where the exact match must be taken.
        options = self._gateways(320)
        options[5] = "gw-10 (a.example.com)"
        options[250] = "gw-1 (b.example.com)"
        plugin = make_plugin(service_module, preferred="gw-1")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-1 (b.example.com)"]
        assert state["downs"] == 250

    def test_homing_without_an_exact_match_still_ends_at_the_substring_hit(
        self, service_module
    ):
        # Counterpart: no entry is called gw-1, so the walk homes in on gw-10
        options = self._gateways(320)
        options[5] = "gw-10 (a.example.com)"
        options[250] = "gw-12 (b.example.com)"
        plugin = make_plugin(service_module, preferred="gw-1")
        state = self._long_list(plugin, options)

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-10 (a.example.com)"]
        assert state["downs"] == 320 + 5

    def test_exact_match_seen_while_homing_after_a_lap_wins(self, service_module):
        # The list changes under the walk: the exact entry appears only once
        # the walk is already on its way back to the substring hit
        options = self._gateways(150)
        options[5] = "gw-10 (a.example.com)"
        plugin = make_plugin(service_module, preferred="gw-1")
        state = self._long_list(plugin, options)
        real_down = plugin._press_list_down

        async def changing_down(previous):
            frame = await real_down(previous)
            if state["downs"] == 152:  # lapped at 150, now at entry 2
                options[3] = "gw-1 (b.example.com)"
                return frame_with_cursor(options, state["cursor"], more=True)
            return frame

        plugin._press_list_down = changing_down

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-1 (b.example.com)"]
        assert state["downs"] == 153

    def test_walk_back_goes_by_name_not_by_counting_downs(self, service_module):
        # A redraw that skips an entry (or a list that changes length) must not
        # throw a counted return trip off: the hit is found by its name
        options = self._gateways(150)
        options[60] = "gw-frankfurt (fra.example.com)"
        plugin = make_plugin(service_module, preferred="frankfurt")
        state = self._long_list(plugin, options)
        real_down = plugin._press_list_down
        skipped = []

        async def skipping_down(previous):
            frame = await real_down(previous)
            if not skipped and state["downs"] == 155:
                # one Down moved the cursor two entries
                skipped.append(True)
                state["cursor"] = (state["cursor"] + 1) % len(options)
                return frame_with_cursor(options, state["cursor"], more=True)
            return frame

        plugin._press_list_down = skipping_down

        self._run(plugin, frame_with_cursor(options[:7], 0, more=True))

        assert state["selected"] == ["gw-frankfurt (fra.example.com)"]

    def test_stalled_redraw_still_confirms(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="gw-london", sent=sent)

        async def no_redraw(previous):
            return None

        plugin._press_list_down = no_redraw
        self._run(plugin, service_module.detect_select_prompt(SINGLE_PAGE))

        assert sent == [service_module.KEY_ENTER]

    def test_answered_frame_is_not_answered_twice(self, service_module):
        sent = []
        plugin = make_plugin(service_module, preferred="", sent=sent)
        frame = service_module.detect_select_prompt(SINGLE_PAGE)

        self._run(plugin, frame)
        assert plugin._answered_select == frame["message"]

        plugin._screen.feed("\r\n".join(SINGLE_PAGE) + "\r\n")
        plugin._schedule_prompt_check()
        assert plugin._prompt_task is None


class TestGatewayListCache:
    def test_options_are_recorded_once(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._record_gateways(["gw-a (a.example.com)", "gw-a (a.example.com)"])
        assert plugin._gateway_list == ["gw-a (a.example.com)"]

    def test_separators_are_stripped_from_entries(self, service_module):
        # nmcli splits +vpn.data values on commas and ';' separates our entries
        plugin = service_module.GpclientVPNPlugin()
        plugin._record_gateways(["gw-a, extra (a.example.com)", "gw-b; x (b.example.com)"])
        assert plugin._gateway_list == [
            "gw-a extra (a.example.com)",
            "gw-b x (b.example.com)",
        ]

    def test_entries_are_appended_once_and_sanitized(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        entries = ["gw-a (a.example.com)"]
        plugin._append_entries(
            entries, ["gw-a (a.example.com)", "gw-b, x (b.example.com)", ",", ""]
        )
        plugin._append_entries(entries, ["gw-b x (b.example.com)"])
        assert entries == ["gw-a (a.example.com)", "gw-b x (b.example.com)"]
        assert plugin._gateway_list == []

    @pytest.mark.parametrize(
        "options", [[","], [";"], ["  "], [", ;"], [",", ";", "  ", ", ;"], [""]]
    )
    def test_record_gateways_skips_entries_empty_after_sanitizing(
        self, service_module, options
    ):
        plugin = service_module.GpclientVPNPlugin()
        plugin._record_gateways(options)
        assert plugin._gateway_list == []

    def test_chosen_gateway_line_is_harvested(self, service_module):
        line = (
            "[2026-05-20T11:58:37Z INFO  gpclient::connect] Connecting to the only "
            "available gateway: gp-gw-ext-b2b (vpn.example.com)"
        )
        match = service_module.GATEWAY_CHOSEN_RE.search(line)
        assert match.group("gateway") == "gp-gw-ext-b2b (vpn.example.com)"

    def test_selected_gateway_line_is_harvested(self, service_module):
        line = (
            "[2026-05-20T11:58:37Z INFO  gpclient::connect] Connecting to the "
            "selected gateway: gw-london (gw3.example.com)"
        )
        match = service_module.GATEWAY_CHOSEN_RE.search(line)
        assert match.group("gateway") == "gw-london (gw3.example.com)"

    @pytest.mark.parametrize(
        "line",
        [
            "Connecting to gateway: gw-a (a.example.com)",
            "Connecting to the selected gateway:",  # no name
            "Connecting to the only available gateway:",
            "Cannot find gateway specified",
            "Connecting to the first gateway: gw-a (a.example.com)",
            "",
        ],
    )
    def test_gateway_chosen_regex_rejects_other_lines(self, service_module, line):
        assert service_module.GATEWAY_CHOSEN_RE.search(line) is None

    def test_unchanged_list_does_not_touch_the_profile(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = "1234"
        plugin._gateway_list = ["gw-a (a.example.com)"]
        plugin._stored_gateway_list = "gw-a (a.example.com)"

        async def fail(*_args, **_kwargs):
            raise AssertionError("nmcli must not be called")

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fail
        try:
            asyncio.run(plugin._persist_gateway_list())
        finally:
            asyncio.create_subprocess_exec = original

    def test_nothing_is_written_without_a_uuid(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin._gateway_list = ["gw-a (a.example.com)"]

        async def fail(*_args, **_kwargs):
            raise AssertionError("nmcli must not be called")

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fail
        try:
            asyncio.run(plugin._persist_gateway_list())
        finally:
            asyncio.create_subprocess_exec = original


class TestGatewayCountLine:
    @pytest.mark.parametrize(
        "line, count",
        [
            ("Found 60 gateways in portal config", 60),
            (
                "[2026-07-20T12:44:26Z INFO  gpapi::portal::config] "
                "Found 8 gateways in portal config",
                8,
            ),
            ("gpclient output: Found 10000 gateways in portal config", 10000),
            ("Found 1 gateways in portal config", 1),
        ],
    )
    def test_count_line_is_recognised(self, service_module, line, count):
        assert service_module.parse_gateway_count(line) == count

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "Found gateways in portal config",
            "Found -3 gateways in portal config",
            "Found 6x gateways in portal config",
            "Found 6.5 gateways in portal config",
            "Found 0 gateways in portal config",
            "Found 10001 gateways in portal config",
            "Found 99999999999999999999 gateways in portal config",
            "Found 60 gateways in portal",
            "Found 60 gateways in the portal config",
            "Found 60 gateway profiles",
            "Found 60 gateways",
            "Found \u0666\u0660 gateways in portal config",  # non-ASCII digits
        ],
    )
    def test_other_lines_are_not(self, service_module, line):
        assert service_module.parse_gateway_count(line) is None

    @pytest.mark.parametrize("value, count", [("20", 20), (" 7 ", 7)])
    def test_stored_count_is_read(self, service_module, value, count):
        assert service_module.parse_stored_gateway_count(value) == count

    @pytest.mark.parametrize(
        "value", ["", "x", "-1", "0", "2.5", "10001", "1" * 30, "\u0662\u0660"]
    )
    def test_garbled_stored_count_is_ignored(self, service_module, value):
        assert service_module.parse_stored_gateway_count(value) is None


class TestStoredListComplete:
    OPTIONS = ["gw-a (a.example.com)", "gw-b (b.example.com)"]

    def _plugin(self, service_module, stored, count, found):
        plugin = service_module.GpclientVPNPlugin()
        plugin._stored_gateway_list = ";".join(stored)
        plugin._stored_gateway_count = count
        plugin._gateway_count = found
        return plugin

    @pytest.mark.parametrize(
        "stored, count, found",
        [
            (OPTIONS + ["gw-c (c.example.com)"], 3, 3),
            (OPTIONS, 2, 2),
        ],
    )
    def test_complete(self, service_module, stored, count, found):
        plugin = self._plugin(service_module, stored, count, found)
        assert plugin._stored_list_is_complete(self.OPTIONS)

    @pytest.mark.parametrize(
        "stored, count, found",
        [
            (OPTIONS + ["gw-c (c.example.com)"], 3, 4),  # count differs
            (OPTIONS + ["gw-c (c.example.com)"], 3, 2),
            (OPTIONS, None, 2),  # never walked
            (OPTIONS, None, None),
            # No Found line: the stored count is only trusted when it equals N
            (OPTIONS, 2, None),
            (OPTIONS + ["gw-c (c.example.com)"], 3, None),
            (OPTIONS[:1], 2, 2),  # a visible entry is missing
            ([], 2, 2),
        ],
    )
    def test_not_complete(self, service_module, stored, count, found):
        plugin = self._plugin(service_module, stored, count, found)
        assert not plugin._stored_list_is_complete(self.OPTIONS)

    def test_entries_are_compared_as_cached(self, service_module):
        # The cache holds them with the separators stripped
        plugin = self._plugin(service_module, ["gw-a x (a.example.com)"], 1, 1)
        assert plugin._stored_list_is_complete(["gw-a, x (a.example.com)"])


class TestCollectGatewayPages:
    """A paged list the profile does not hold completely is read with PageDown
    and the cursor goes back with Home (issue #25)."""

    GATEWAYS = [f"gw-{n:02d} (gw{n:02d}.example.com)" for n in range(20)]
    PAGE = 7

    def _plugin(
        self, service_module, preferred, gateways=None, found=-1,
        stall_page_after=None, no_home=False, page_step=None, **stored
    ):
        """A plugin on a fake inquire list: Down wraps, PageDown clamps and
        gives no redraw at the last entry, Home goes to the first entry.

        found: gpclient's "Found N" (-1: the list length, None: not logged)
        page_step: how far PageDown really moves (default: a page)
        """
        options = self.GATEWAYS if gateways is None else gateways
        plugin = make_plugin(service_module, preferred=preferred)
        plugin._stored_gateway_list = stored.get("list", "")
        plugin._stored_gateway_count = stored.get("count")
        plugin._gateway_count = len(options) if found == -1 else found
        state = {
            "cursor": 0, "start": 0, "selected": [],
            "down": 0, "pagedown": 0, "home": 0,
        }
        keys = service_module

        def move_to(new):
            if new == state["cursor"]:
                return None  # no redraw
            state["cursor"] = new
            if new == 0:
                state["start"] = 0
            elif new >= state["start"] + self.PAGE:
                state["start"] = new - self.PAGE + 1
            elif new < state["start"]:
                state["start"] = new
            return self._frame(options, state)

        async def press(previous, key, what):
            if key == keys.KEY_DOWN:
                state["down"] += 1
                return move_to((state["cursor"] + 1) % len(options))
            if key == keys.KEY_PAGE_DOWN:
                state["pagedown"] += 1
                if state["pagedown"] > (
                    1 << 30 if stall_page_after is None else stall_page_after
                ):
                    return None
                step = self.PAGE if page_step is None else page_step
                return move_to(min(state["cursor"] + step, len(options) - 1))
            assert key == keys.KEY_HOME
            state["home"] += 1
            return None if no_home else move_to(0)

        plugin._press_list_key = press
        plugin._write_keys = lambda data, description: state["selected"].append(
            options[state["cursor"]]
        )
        return plugin, state

    def _frame(self, options, state):
        first = state["start"]
        return frame_with_cursor(
            options[first : first + self.PAGE], state["cursor"] - first, more=True
        )

    def _run(self, plugin, gateways=None):
        first = (self.GATEWAYS if gateways is None else gateways)[: self.PAGE]
        asyncio.run(plugin._handle_select_prompt(frame_with_cursor(first, 0, more=True)))

    @pytest.mark.parametrize(
        "preferred, expected, downs",
        [
            ("gw-03", 3, 3),
            ("gw-00", 0, 0),  # the reported case: on the first page
            ("gw-14", 14, 14),  # beyond the first page
            ("gw-19", 19, 19),  # the last entry
            ("gw-0", 0, 0),  # substring only, at the start
            ("gw-1", 10, 10),  # substring only: the first one that contains it
            ("gw-tokyo", 0, 0),  # not offered: the first proposal, no lap
            ("", 0, 0),
        ],
    )
    def test_reading_the_pages_does_not_change_the_selection(
        self, service_module, preferred, expected, downs
    ):
        paged, paged_state = self._plugin(service_module, preferred)
        known, known_state = self._plugin(
            service_module,
            preferred,
            list=";".join(self.GATEWAYS),
            count=len(self.GATEWAYS),
        )

        self._run(paged)
        self._run(known)

        assert paged_state["selected"] == known_state["selected"]
        assert paged_state["selected"] == [self.GATEWAYS[expected]]
        # The whole list is known after reading: Down presses only as far as
        # the gateway (the walk without reading may need a lap to be sure)
        assert paged_state["down"] == downs
        assert (paged_state["pagedown"], paged_state["home"]) == (3, 1)
        assert (known_state["pagedown"], known_state["home"]) == (0, 0)
        assert paged._gateway_list == self.GATEWAYS
        assert paged._lap_entries == self.GATEWAYS
        assert known._lap_entries == []

    def test_nothing_is_selected_while_paging(self, service_module):
        plugin, state = self._plugin(service_module, "gw-00")
        cursors = []
        original = plugin._write_keys
        plugin._write_keys = lambda data, description: (
            cursors.append(state["cursor"]),
            original(data, description),
        )

        self._run(plugin)

        # Enter goes out once, with the cursor back on the first proposal
        assert cursors == [0]
        assert state["selected"] == [self.GATEWAYS[0]]

    @pytest.mark.parametrize(
        "stored",
        [
            {},
            {"list": ";".join(GATEWAYS) + ";gw-old (old.example.com)", "count": 21},
            {"list": ";".join(GATEWAYS[:3]), "count": None},
        ],
    )
    def test_unknown_count_reads_no_pages_and_only_merges(
        self, service_module, stored
    ):
        # Without "Found N" a PageDown that gets no redraw cannot be told from
        # the end of the list, so the list is not read at all
        plugin, state = self._plugin(service_module, "gw-03", found=None, **stored)
        plugin._connection_uuid = "1234"
        writes = []

        async def record(key, value):
            writes.append((key, value))
            return True

        plugin._write_vpn_data = record

        self._run(plugin)
        asyncio.run(plugin._persist_gateway_list())

        assert (state["pagedown"], state["home"]) == (0, 0)
        assert state["selected"] == [self.GATEWAYS[3]]
        assert plugin._lap_entries == []
        assert all(key != "gateway-list-count" for key, _ in writes)
        # Never shrinks: what is stored stays, whatever was seen comes first
        stored_entries = [e for e in stored.get("list", "").split(";") if e]
        written = dict(writes).get("gateway-list", stored.get("list", ""))
        assert set(stored_entries) <= set(written.split(";"))
        assert set(plugin._gateway_list) <= set(written.split(";"))

    @pytest.mark.parametrize("found", [25, 100])
    def test_count_above_the_list_is_not_complete(self, service_module, found):
        plugin, state = self._plugin(service_module, "gw-03", found=found)

        self._run(plugin)

        # The cursor stopped before the end the count says: a stalled gpclient
        assert plugin._lap_entries == []
        assert plugin._gateway_list == self.GATEWAYS
        assert state["selected"] == [self.GATEWAYS[3]]
        assert state["home"] == 1

    @pytest.mark.parametrize("found", [1, 5, 19])
    def test_count_below_the_list_is_not_complete(self, service_module, found):
        plugin, state = self._plugin(service_module, "gw-03", found=found)

        self._run(plugin)

        # More entries than gpclient counted: the count is wrong, or the list
        # is not what it said, so the list is not taken as the whole one
        assert plugin._lap_entries == []
        assert state["selected"] == [self.GATEWAYS[3]]

    @pytest.mark.parametrize("stall", [0, 1, 2])
    @pytest.mark.parametrize("preferred, expected", [("gw-03", 3), ("", 0)])
    def test_stalled_paging_is_not_complete_and_still_selects_right(
        self, service_module, stall, preferred, expected
    ):
        plugin, state = self._plugin(
            service_module, preferred, stall_page_after=stall
        )

        self._run(plugin)

        assert plugin._lap_entries == []
        assert state["selected"] == [self.GATEWAYS[expected]]
        assert state["pagedown"] == stall + 1
        # Home only when the cursor moved
        assert state["home"] == (1 if stall else 0)

    @pytest.mark.parametrize(
        "preferred, expected",
        [
            ("gw-03", 3),
            ("gw-19", 19),
            ("gw-12", 12),
            ("gw-1", 10),  # substring only: the same gateway as with Home
            ("", 0),
            ("gw-tokyo", 0),
        ],
    )
    def test_home_without_a_redraw_selects_from_where_the_cursor_is(
        self, service_module, caplog, preferred, expected
    ):
        plugin, state = self._plugin(service_module, preferred, no_home=True)

        self._run(plugin)

        # The cursor is on the last entry; the walk goes on by name, around
        assert state["selected"] == [self.GATEWAYS[expected]]
        assert state["home"] == 1
        assert "did not redraw the list after Home" in caplog.text
        assert plugin._lap_entries == self.GATEWAYS

    def test_home_with_a_redraw_logs_no_warning(self, service_module, caplog):
        plugin, _ = self._plugin(service_module, "gw-03")

        self._run(plugin)

        assert "did not redraw" not in caplog.text

    def test_page_budget_follows_the_count(self, service_module):
        # PageDown that moves less than a page never reaches the end in
        # ceil((20 - 1) / 7) = 3 presses: give up after two more
        plugin, state = self._plugin(service_module, "", page_step=1)

        self._run(plugin)

        assert state["pagedown"] == 5
        assert plugin._lap_entries == []
        # Back on the first proposal all the same
        assert state["selected"] == [self.GATEWAYS[0]]

    def test_long_list_within_the_budget_is_read(self, service_module):
        gateways = [f"gw-{n:03d} (gw{n:03d}.example.com)" for n in range(250)]
        plugin, state = self._plugin(service_module, "", gateways=gateways)

        self._run(plugin, gateways)

        assert plugin._lap_entries == gateways
        # The cursor goes 7, 14 ... 245 (35 keys), then to the end (249)
        assert state["pagedown"] == 36
        assert state["selected"] == [gateways[0]]

    @pytest.mark.parametrize("preferred, expected", [("gw-240", 240), ("gw-249", 249)])
    def test_preferred_beyond_the_step_limit_is_selected_after_reading(
        self, service_module, preferred, expected
    ):
        gateways = [f"gw-{n:03d} (gw{n:03d}.example.com)" for n in range(250)]
        plugin, state = self._plugin(service_module, preferred, gateways=gateways)

        self._run(plugin, gateways)

        assert state["selected"] == [gateways[expected]]
        assert state["down"] == expected
        assert state["down"] > service_module.SELECT_MAX_STEPS

    def test_preferred_beyond_the_step_limit_is_not_found_without_the_list(
        self, service_module
    ):
        # Counterpart: when the reading stalls the walk keeps its limit
        gateways = [f"gw-{n:03d} (gw{n:03d}.example.com)" for n in range(250)]
        plugin, state = self._plugin(
            service_module, "gw-240", gateways=gateways, stall_page_after=3
        )

        self._run(plugin, gateways)

        assert plugin._lap_entries == []
        assert state["down"] == service_module.SELECT_MAX_STEPS
        assert state["selected"] == [gateways[service_module.SELECT_MAX_STEPS]]

    @pytest.mark.parametrize("no_home", [False, True])
    @pytest.mark.parametrize(
        "preferred, downs_with_home, downs_without",
        [
            ("gw-tokyo", 0, 1),  # not offered: the first proposal, no lap
            ("", 0, 1),
            ("gw-19", 19, 0),  # the distance only
            ("gw-02", 2, 3),
        ],
    )
    def test_the_walk_after_reading_is_the_distance_only(
        self, service_module, no_home, preferred, downs_with_home, downs_without
    ):
        plugin, state = self._plugin(service_module, preferred, no_home=no_home)

        self._run(plugin)

        assert state["down"] == (downs_without if no_home else downs_with_home)
        assert state["down"] < len(self.GATEWAYS)

    def test_not_offered_preference_laps_the_list_when_it_is_unread(
        self, service_module, caplog
    ):
        # Counterpart: with the list not read (stalled) the walk laps the list
        plugin, state = self._plugin(
            service_module, "gw-tokyo", stall_page_after=1
        )

        self._run(plugin)

        assert state["down"] == len(self.GATEWAYS)
        assert state["selected"] == [self.GATEWAYS[0]]
        assert "Walked the whole list" in caplog.text

    def test_complete_stored_list_is_not_paged(self, service_module):
        plugin, state = self._plugin(
            service_module,
            "gw-03",
            list=";".join(self.GATEWAYS),
            count=len(self.GATEWAYS),
        )

        self._run(plugin)

        assert (state["pagedown"], state["home"]) == (0, 0)

    def test_stale_entries_of_an_earlier_attempt_stay_out(self, service_module):
        plugin, state = self._plugin(service_module, "gw-03")
        plugin._gateway_list = ["gw-stale (old.example.com)"]
        plugin._lap_entries = ["gw-stale (old.example.com)"]

        self._run(plugin)

        assert plugin._lap_entries == self.GATEWAYS

    def test_entry_shown_twice_is_one_entry_and_the_list_is_complete(
        self, service_module
    ):
        # Entry 5 again at index 10: only 19 different entries, but the cursor
        # reached the end after ceil((20 - 1) / 7) = 3 presses, so the list is
        # complete (and is not read again on every connect)
        gateways = list(self.GATEWAYS)
        gateways[10] = gateways[5]
        plugin, state = self._plugin(service_module, "gw-15", gateways=gateways)

        self._run(plugin, gateways)

        assert plugin._lap_entries == list(dict.fromkeys(gateways))
        assert len(plugin._lap_entries) == 19
        assert state["pagedown"] == 4  # the fourth gets no redraw
        assert state["selected"] == [gateways[15]]

    def test_entry_shown_twice_on_the_way_does_not_end_the_walk_early(
        self, service_module
    ):
        # The first proposal again at index 10: it is no lap of the list
        gateways = list(self.GATEWAYS)
        gateways[10] = gateways[0]
        plugin, state = self._plugin(service_module, "gw-15", gateways=gateways)

        self._run(plugin, gateways)

        assert state["selected"] == [gateways[15]]
        assert state["down"] == 15

    def test_count_one_above_the_list_is_complete_at_the_end(self, service_module):
        # Indistinguishable from one entry shown twice
        plugin, _ = self._plugin(service_module, "gw-03", found=21)

        self._run(plugin)

        assert plugin._lap_entries == self.GATEWAYS

    def test_entry_shown_twice_with_a_stall_half_way_is_not_complete(
        self, service_module
    ):
        # Counterpart: fewer different entries than N is no proof of the end
        gateways = list(self.GATEWAYS)
        gateways[10] = gateways[5]
        plugin, state = self._plugin(
            service_module, "gw-15", gateways=gateways, stall_page_after=2
        )

        self._run(plugin, gateways)

        assert plugin._lap_entries == []
        assert state["selected"] == [gateways[15]]

    @pytest.mark.parametrize("found", [3])
    def test_not_paged_list_is_the_whole_list(self, service_module, found):
        plugin = make_plugin(service_module, preferred="")
        plugin._write_keys = lambda data, description: None
        plugin._stored_gateway_list = "gw-old (old.example.com)"
        plugin._stored_gateway_count = 1
        plugin._gateway_count = found

        asyncio.run(
            plugin._handle_select_prompt(
                frame_with_cursor(self.GATEWAYS[:3], 0, more=False)
            )
        )

        assert plugin._lap_entries == self.GATEWAYS[:3]

    @pytest.mark.parametrize("found", [None, 4, 25, 2, 1])
    def test_not_paged_list_with_another_count_is_not_trusted(
        self, service_module, found
    ):
        plugin = make_plugin(service_module, preferred="")
        plugin._write_keys = lambda data, description: None
        plugin._gateway_count = found

        asyncio.run(
            plugin._handle_select_prompt(
                frame_with_cursor(self.GATEWAYS[:3], 0, more=False)
            )
        )

        assert plugin._lap_entries == []
        assert plugin._gateway_list == self.GATEWAYS[:3]

    def test_not_paged_list_with_an_entry_twice_is_the_whole_list(
        self, service_module
    ):
        options = [self.GATEWAYS[0], self.GATEWAYS[1], self.GATEWAYS[0]]
        plugin = make_plugin(service_module, preferred="")
        plugin._write_keys = lambda data, description: None
        plugin._gateway_count = 3

        asyncio.run(
            plugin._handle_select_prompt(frame_with_cursor(options, 0, more=False))
        )

        assert plugin._lap_entries == self.GATEWAYS[:2]

    def test_not_paged_list_is_not_paged_through(self, service_module):
        plugin, state = self._plugin(service_module, "gw-01")

        asyncio.run(
            plugin._handle_select_prompt(
                frame_with_cursor(self.GATEWAYS[:3], 0, more=False)
            )
        )

        assert (state["pagedown"], state["home"]) == (0, 0)


class TestPersistGatewayList:
    def _plugin(self, service_module, seen, stored=(), count=None, lap=(), found=None):
        plugin = service_module.GpclientVPNPlugin()
        plugin._connection_uuid = "1234"
        plugin._gateway_list = list(seen)
        plugin._stored_gateway_list = ";".join(stored)
        plugin._stored_gateway_count = count
        plugin._lap_entries = list(lap)
        plugin._gateway_count = found
        writes = []

        async def record(key, value):
            writes.append((key, value))
            return True

        plugin._write_vpn_data = record
        return plugin, writes

    def test_partial_list_does_not_replace_a_longer_one(self, service_module):
        stored = ["gw-a", "gw-b", "gw-c", "gw-d"]
        plugin, writes = self._plugin(service_module, ["gw-a", "gw-b"], stored, 4)

        asyncio.run(plugin._persist_gateway_list())

        assert writes == []

    def test_partial_list_adds_what_is_new_after_the_stored_entries(
        self, service_module
    ):
        plugin, writes = self._plugin(
            service_module, ["gw-b", "gw-x", "gw-a"], ["gw-a", "gw-b", "gw-c"], 3
        )

        asyncio.run(plugin._persist_gateway_list())

        # Seen entries first, then the stored ones not seen; no count: a
        # partial list does not make the stored one complete
        assert writes == [("gateway-list", "gw-b;gw-x;gw-a;gw-c")]

    def test_entries_of_an_earlier_attempt_stay_out_of_a_full_lap(self, service_module):
        # _gateway_list survives a retry; only what the lap saw is the list
        plugin, writes = self._plugin(
            service_module,
            ["gw-stale", "gw-a", "gw-b"],
            ["gw-a", "gw-b", "gw-stale"],
            count=3,
            lap=["gw-a", "gw-b"],
            found=2,
        )

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-a;gw-b"), ("gateway-list-count", "2")]

    def test_not_paged_list_replaces_the_stored_one(self, service_module):
        plugin, writes = self._plugin(
            service_module,
            ["gw-a", "gw-c"],
            ["gw-a", "gw-b", "gw-c"],
            count=3,
            lap=["gw-a", "gw-c"],
            found=2,
        )

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-a;gw-c"), ("gateway-list-count", "2")]

    def test_partial_list_keeps_the_seen_order_first(self, service_module):
        plugin, writes = self._plugin(
            service_module, ["gw-c", "gw-a"], ["gw-a", "gw-b", "gw-c", "gw-d"], 4
        )

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-c;gw-a;gw-b;gw-d")]

    def test_partial_list_into_an_empty_profile_stores_no_count(self, service_module):
        plugin, writes = self._plugin(service_module, ["gw-a", "gw-b"])

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-a;gw-b")]

    def test_full_lap_replaces_the_list_and_stores_the_count(self, service_module):
        plugin, writes = self._plugin(
            service_module,
            ["gw-a", "gw-c"],
            ["gw-a", "gw-b", "gw-c"],
            count=3,
            lap=["gw-a", "gw-c"],
            found=2,
        )

        asyncio.run(plugin._persist_gateway_list())

        # gw-b is gone from the portal, so it is gone from the profile
        assert writes == [("gateway-list", "gw-a;gw-c"), ("gateway-list-count", "2")]

    def test_count_is_what_gpclient_found_even_with_an_entry_twice(
        self, service_module
    ):
        plugin, writes = self._plugin(
            service_module, ["gw-a", "gw-b"], lap=["gw-a", "gw-b"], found=3
        )

        asyncio.run(plugin._persist_gateway_list())

        assert writes[-1] == ("gateway-list-count", "3")

    def test_no_count_is_written_without_a_found_line(self, service_module):
        plugin, writes = self._plugin(
            service_module, ["gw-a", "gw-b"], lap=["gw-a", "gw-b"]
        )

        asyncio.run(plugin._persist_gateway_list())

        assert all(key != "gateway-list-count" for key, _ in writes)

    def test_unchanged_after_a_full_lap_writes_nothing(self, service_module):
        plugin, writes = self._plugin(
            service_module,
            ["gw-a", "gw-b"],
            ["gw-a", "gw-b"],
            2,
            lap=["gw-a", "gw-b"],
            found=2,
        )

        asyncio.run(plugin._persist_gateway_list())

        assert writes == []

    def test_count_is_not_written_when_the_list_write_fails(self, service_module):
        plugin, writes = self._plugin(service_module, ["gw-a"], lap=["gw-a"], found=1)

        async def fail(key, value):
            writes.append((key, value))
            return False

        plugin._write_vpn_data = fail

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-a")]
        assert plugin._stored_gateway_count is None


    def test_failed_count_write_warns_that_the_list_is_read_again(
        self, service_module, caplog
    ):
        plugin, writes = self._plugin(service_module, ["gw-a"], lap=["gw-a"], found=1)

        async def fail_count(key, value):
            writes.append((key, value))
            return key != "gateway-list-count"

        plugin._write_vpn_data = fail_count

        asyncio.run(plugin._persist_gateway_list())

        assert writes == [("gateway-list", "gw-a"), ("gateway-list-count", "1")]
        assert plugin._stored_gateway_count is None
        assert "next connection reads the gateway list again" in caplog.text

    def test_written_count_does_not_warn(self, service_module, caplog):
        plugin, writes = self._plugin(service_module, ["gw-a"], lap=["gw-a"], found=1)

        asyncio.run(plugin._persist_gateway_list())

        assert plugin._stored_gateway_count == 1
        assert "reads the gateway list again" not in caplog.text


class TestResolveBrowser:
    def _with_wrapper(self, service_module, monkeypatch, present=True):
        monkeypatch.setattr(
            service_module.os.path,
            "exists",
            lambda path: present if path == service_module.BROWSER_WRAPPER else True,
        )

    def test_empty_defaults_to_wrapped_edge(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch)
        assert service_module.resolve_browser("") == (
            service_module.BROWSER_WRAPPER,
            "edge",
        )

    def test_friendly_names_are_wrapped(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch)
        for value, target in (
            ("firefox", "firefox"),
            ("Firefox", "firefox"),
            ("chrome", "chrome"),
            ("google-chrome", "chrome"),
            ("chromium", "chromium"),
            ("default", "default"),
            ("msedge", "edge"),
        ):
            assert service_module.resolve_browser(value) == (
                service_module.BROWSER_WRAPPER,
                target,
            )

    def test_known_binaries_are_wrapped(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch)
        for path in (
            "/usr/bin/firefox",
            "/usr/bin/microsoft-edge",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
        ):
            assert service_module.resolve_browser(path) == (
                service_module.BROWSER_WRAPPER,
                path,
            )

    def test_legacy_edge_wrapper_is_left_alone(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch)
        assert service_module.resolve_browser(service_module.LEGACY_EDGE_WRAPPER) == (
            service_module.LEGACY_EDGE_WRAPPER,
            None,
        )

    def test_custom_script_is_passed_through(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch)
        assert service_module.resolve_browser("/opt/me/my-wrapper") == (
            "/opt/me/my-wrapper",
            None,
        )

    @pytest.mark.parametrize("value", ["", "  ", "\t"])
    def test_blank_value_defaults_to_wrapped_edge(
        self, service_module, monkeypatch, value
    ):
        self._with_wrapper(service_module, monkeypatch)
        assert service_module.resolve_browser(value) == (
            service_module.BROWSER_WRAPPER,
            "edge",
        )

    @pytest.mark.parametrize("value", ["safari", "/usr/bin/safari", "epiphany"])
    def test_unknown_browser_is_passed_through_untouched(
        self, service_module, monkeypatch, value
    ):
        self._with_wrapper(service_module, monkeypatch)
        assert service_module.resolve_browser(value) == (value, None)

    def test_without_wrapper_and_binary_the_name_is_returned(
        self, service_module, monkeypatch
    ):
        monkeypatch.setattr(service_module.os.path, "exists", lambda path: False)
        monkeypatch.setattr(service_module.shutil, "which", lambda name: None)
        assert service_module.resolve_browser("firefox") == ("firefox", None)
        assert service_module.resolve_browser("edge") == ("edge", None)

    def test_without_wrapper_a_missing_absolute_path_is_kept(
        self, service_module, monkeypatch
    ):
        monkeypatch.setattr(service_module.os.path, "exists", lambda path: False)
        assert service_module.resolve_browser("/usr/bin/firefox") == (
            "/usr/bin/firefox",
            None,
        )

    def test_without_the_wrapper_the_binary_is_used(self, service_module, monkeypatch):
        self._with_wrapper(service_module, monkeypatch, present=False)
        monkeypatch.setattr(
            service_module.shutil, "which", lambda name: f"/usr/bin/{name}"
        )
        assert service_module.resolve_browser("firefox") == ("/usr/bin/firefox", None)
        assert service_module.resolve_browser("edge") == (
            "/usr/bin/microsoft-edge",
            None,
        )

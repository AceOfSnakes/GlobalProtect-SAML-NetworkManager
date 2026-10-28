"""
Property tests (hypothesis) for the parsers of gpclient output and of the vpnc
hook state file (issue #55): they must never raise and must keep the
invariants the code documents, whatever the input is.

hypothesis is a test dependency only; the service does not use it.
Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ipaddress
import os

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

settings.register_profile(
    "default-props",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
# CI: the same examples on every run, no example database
settings.register_profile(
    "ci",
    parent=settings.get_profile("default-props"),
    derandomize=True,
    database=None,
)
settings.load_profile("ci" if os.environ.get("CI") else "default-props")


# --- strategies --------------------------------------------------------------

# Text without ESC (so it never joins a neighbouring escape sequence)
plain_text = st.text(
    alphabet=st.characters(blacklist_characters="\x1b", blacklist_categories=("Cs",)),
    max_size=40,
)
csi_params = st.text(alphabet="0123456789;:?<=>", max_size=8)
csi_sequence = st.builds(
    lambda params, inter, final: "\x1b[" + params + inter + final,
    csi_params,
    st.text(alphabet=" !\"#$%&'()*+,-./", max_size=2),
    st.sampled_from("ABCDGHJKmhlsu@~"),
)
cursor_sequence = st.builds(
    lambda number, final: "\x1b[%s%s" % (number, final),
    st.one_of(st.just(""), st.integers(0, 400).map(str)),
    st.sampled_from("ABCDGKJ"),
)
osc_sequence = st.builds(
    lambda body, end: "\x1b]" + body + end,
    st.text(alphabet="0123456789;abc \\[", max_size=12),
    st.sampled_from(["\x07", "\x1b\\"]),
)
control_text = st.text(alphabet="ab \r\n\t\b\x0b\x0c\x00\x7f", max_size=10)
# Pieces that are complete on their own: feeding them one by one or joined
# must give the same result
piece = st.one_of(plain_text, control_text, csi_sequence, cursor_sequence, osc_sequence)
pieces = st.lists(piece, max_size=12)
# Any text at all, with plenty of ESC and brackets to reach the escape paths
any_text = st.text(
    alphabet=st.one_of(
        st.characters(blacklist_categories=("Cs",)),
        st.sampled_from("\x1b[]\\\x07;0129ABCDGJKm ?"),
    ),
    max_size=80,
)


def merged(tokens):
    """Tokens with adjacent text tokens joined (chunk borders split text)"""
    result = []
    for token in tokens:
        if token[0] == "text" and result and result[-1][0] == "text":
            result[-1] = ("text", result[-1][1] + token[1])
        else:
            result.append(tuple(token))
    return result


def tokenize_in_chunks(module, chunks):
    tokenizer = module.EscapeTokenizer()
    tokens = []
    for chunk in chunks:
        tokens += tokenizer.feed(chunk)
    return merged(tokens)


# --- EscapeTokenizer ---------------------------------------------------------


class TestEscapeTokenizer:
    @given(text=any_text)
    def test_never_raises_and_tokens_are_well_formed(self, service_module, text):
        tokens = service_module.EscapeTokenizer().feed(text)
        for token in tokens:
            if token[0] == "text":
                assert len(token) == 2 and token[1] and "\x1b" not in token[1]
            else:
                assert token[0] == "csi" and len(token) == 4

    @given(chunks=pieces)
    def test_complete_pieces_give_the_same_tokens_as_one_feed(
        self, service_module, chunks
    ):
        whole = tokenize_in_chunks(service_module, ["".join(chunks)])
        assert tokenize_in_chunks(service_module, chunks) == whole

    @given(text=any_text)
    def test_text_tokens_hold_no_escape_sequence(self, service_module, text):
        # Whatever is left as text is free of ESC, so no sequence leaks through
        assert "\x1b" not in service_module.strip_ansi(text)

    @given(before=plain_text, body=osc_sequence, after=plain_text, data=st.data())
    def test_osc_split_at_any_point_gives_the_same_tokens(
        self, service_module, before, body, after, data
    ):
        text = before + body + after
        # not inside the two-character opener: the input must not end there
        opener = len(before) + 1
        cut = data.draw(st.integers(0, len(text)).filter(lambda n: n != opener))
        whole = tokenize_in_chunks(service_module, [text])
        # A cut inside the text before or after the OSC is a chunk border of
        # plain text; a cut inside the OSC is the carried state
        assert tokenize_in_chunks(service_module, [text[:cut], text[cut:]]) == whole
        joined = before + after
        assert whole == ([("text", joined)] if joined else [])

    @given(before=plain_text, body=st.text(alphabet="0;ab", max_size=10))
    def test_unterminated_osc_is_carried_not_dropped_or_repeated(
        self, service_module, before, body
    ):
        tokenizer = service_module.EscapeTokenizer()
        first = tokenizer.feed(before + "\x1b]" + body)
        assert merged(first) == ([("text", before)] if before else [])
        assert merged(tokenizer.feed("tail\x07rest")) == [("text", "rest")]
        assert tokenizer.feed("again") == [("text", "again")]

    @given(text=plain_text, params=csi_params)
    def test_plain_text_is_kept_whole(self, service_module, text, params):
        tokens = service_module.EscapeTokenizer().feed(text)
        assert tokens == ([("text", text)] if text else [])
        tokens = service_module.EscapeTokenizer().feed("\x1b[" + params + "m" + text)
        assert tokens[0] == ("csi", params, "", "m")
        assert merged(tokens[1:]) == ([("text", text)] if text else [])


# --- ScreenBuffer ------------------------------------------------------------


def screen_after(module, chunks):
    screen = module.ScreenBuffer()
    for chunk in chunks:
        screen.feed(chunk)
    return screen


class TestScreenBuffer:
    @given(text=any_text)
    def test_never_raises_and_stays_inside_the_grid(self, service_module, text):
        lines = screen_after(service_module, [text]).lines()
        assert len(lines) <= service_module.ScreenBuffer.MAX_ROWS
        assert all(len(line) <= service_module.PTY_COLUMNS for line in lines)

    @given(chunks=pieces)
    def test_cursor_moves_and_erase_keep_the_grid_limits(self, service_module, chunks):
        screen = screen_after(service_module, chunks)
        assert len(screen.lines()) <= service_module.ScreenBuffer.MAX_ROWS
        assert all(len(row) <= service_module.PTY_COLUMNS for row in screen._rows)

    @given(chunks=pieces)
    def test_chunks_give_the_same_screen_as_one_feed(self, service_module, chunks):
        chunked = screen_after(service_module, chunks).lines()
        assert chunked == screen_after(service_module, ["".join(chunks)]).lines()

    @given(
        text=st.text(
            alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
            max_size=100,
        )
    )
    def test_plain_text_reads_back(self, service_module, text):
        assert screen_after(service_module, [text]).lines() == (text.rstrip(),)

    @given(
        lines=st.lists(
            st.text(alphabet="abc XYZ0129", min_size=1, max_size=20).filter(
                lambda s: s.strip() == s
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_crlf_lines_read_back(self, service_module, lines):
        text = "\r\n".join(lines)
        assert screen_after(service_module, [text]).lines() == tuple(lines)

    @given(text=any_text)
    def test_version_changes_when_tokens_are_applied(self, service_module, text):
        screen = service_module.ScreenBuffer()
        before = screen.version
        screen.feed(text)
        tokens = service_module.EscapeTokenizer().feed(text)
        assert (screen.version != before) == bool(tokens)


# --- detect_prompt / detect_select_prompt ------------------------------------


class TestDetectPrompt:
    @given(tail=any_text, answer=st.text(max_size=5))
    def test_never_raises_and_is_deterministic(self, service_module, tail, answer):
        first = service_module.detect_prompt(tail, answer)
        assert first is None or (isinstance(first, str) and first)
        assert service_module.detect_prompt(tail, answer) == first

    @given(tail=any_text)
    def test_without_a_question_mark_start_it_is_no_prompt(self, service_module, tail):
        if not tail.strip().startswith("?"):
            assert service_module.detect_prompt(tail) is None

    @given(
        label=st.text(alphabet="abcXYZ 019()-", min_size=1, max_size=30),
        pad_left=st.text(alphabet=" \t", max_size=3),
        pad_right=st.text(alphabet=" \t", max_size=3),
    )
    def test_label_is_found_and_whitespace_does_not_matter(
        self, service_module, label, pad_left, pad_right
    ):
        if not label.strip():
            return
        expected = label.strip()
        for tail in ("? " + label + ":", "? " + label):
            result = service_module.detect_prompt(pad_left + tail + pad_right)
            assert result == expected

    @given(
        label=st.text(alphabet="abcXYZ", min_size=1, max_size=10),
        echo=st.text(alphabet="abc123", min_size=1, max_size=8),
    )
    def test_text_after_the_colon_is_no_prompt(self, service_module, label, echo):
        assert service_module.detect_prompt("? %s: %s" % (label, echo)) is None

    @given(
        label=st.text(alphabet="abcXYZ", min_size=1, max_size=10),
        answer=st.text(alphabet="123", min_size=1, max_size=4),
    )
    def test_echo_of_the_last_answer_is_no_prompt(self, service_module, label, answer):
        tail = "? %s %s" % (label, answer)
        assert service_module.detect_prompt(tail, answer) is None
        assert service_module.detect_prompt(tail) == label + " " + answer

    @given(lines=st.lists(any_text, max_size=8))
    def test_select_prompt_never_raises(self, service_module, lines):
        result = service_module.detect_select_prompt(lines)
        assert result is None or isinstance(result, dict)
        assert service_module.detect_select_prompt(lines) == result


# --- vpnc environment ---------------------------------------------------------

ip4 = st.ip_addresses(v=4).map(str)
ip6 = st.ip_addresses(v=6).map(str)
domain = st.from_regex(
    r"[a-z][a-z0-9-]{0,10}(\.[a-z][a-z0-9-]{0,10}){0,2}", fullmatch=True
)


class TestVpncEnv:
    @given(
        env=st.dictionaries(st.text(max_size=24), st.text(max_size=40), max_size=10),
        iface=st.text(max_size=8),
    )
    def test_readers_never_raise(self, service_module, env, iface):
        result = service_module.learned_dns_from_state(env, iface)
        assert result is None or len(result) == 3
        service_module.vpn_gateway_from_state(env, iface)
        service_module.tunnel_ipv6_from_state(env, iface)
        service_module.tunnel_ipv4_from_env(env)
        service_module.tunnel_mtu_from_env(env)

    @given(
        env=st.dictionaries(
            st.sampled_from(
                [
                    "INTERNAL_IP4_ADDRESS",
                    "INTERNAL_IP4_NETMASK",
                    "INTERNAL_IP4_NETMASKLEN",
                    "INTERNAL_IP4_MTU",
                ]
            ),
            st.text(max_size=20),
        )
    )
    def test_ipv4_and_mtu_are_valid_or_none(self, service_module, env):
        found = service_module.tunnel_ipv4_from_env(env)
        if found is not None:
            address, prefix = found
            assert isinstance(address, ipaddress.IPv4Address)
            assert 0 <= prefix <= 32
        mtu = service_module.tunnel_mtu_from_env(env)
        assert mtu is None or 68 <= mtu <= 65535

    @given(address=ip4, prefix=st.integers(0, 32))
    def test_the_prefix_is_always_32(self, service_module, address, prefix):
        mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        found = service_module.tunnel_ipv4_from_env(
            {
                "INTERNAL_IP4_ADDRESS": address,
                "INTERNAL_IP4_NETMASKLEN": str(prefix),
                "INTERNAL_IP4_NETMASK": mask,
            }
        )
        assert found == (ipaddress.IPv4Address(address), 32)

    @given(
        servers=st.lists(ip4, max_size=4),
        servers6=st.lists(ip6, max_size=3),
        domains=st.lists(domain, max_size=4),
        split=st.lists(domain, max_size=4),
    )
    def test_generated_env_round_trips(
        self, service_module, servers, servers6, domains, split
    ):
        env = {
            "TUNDEV": "gpd0",
            "INTERNAL_IP4_DNS": " ".join(servers),
            "INTERNAL_IP6_DNS": " ".join(servers6),
            "CISCO_DEF_DOMAIN": " ".join(domains),
            "CISCO_SPLIT_DNS": ",".join(split),
        }
        found = service_module.learned_dns_from_state(env, "gpd0")
        dedupe = service_module._dedupe
        assert found == (
            dedupe(servers),
            service_module._merge_domains(domains, split),
            dedupe(servers6),
        )
        for address in found[0] + found[2]:
            ipaddress.ip_address(address)
        # the environment of another tunnel is not ours
        assert service_module.learned_dns_from_state(env, "tun9") is None

    @given(
        environ=st.dictionaries(st.text(max_size=12), st.text(max_size=20), max_size=12)
    )
    def test_notify_sends_only_the_allow_list(self, service_module, environ):
        reason, token, env = service_module.vpnc_notify_env(environ)
        assert set(env) <= set(service_module.VPNC_NOTIFY_ENV)
        assert all(env[key] == environ[key] for key in env)
        assert reason == environ.get("reason", "")
        assert token == environ.get(service_module.NOTIFY_TOKEN_ENV, "")


# --- parse_mtu ---------------------------------------------------------------


class TestParseMtu:
    @given(text=st.text(max_size=12))
    def test_result_is_none_zero_or_in_range(self, service_module, text):
        result = service_module.parse_mtu(text)
        assert result is None or result == 0 or 576 <= result <= 1500

    @given(number=st.integers(0, 1500), zeros=st.integers(0, 3))
    def test_every_valid_number_parses(self, service_module, number, zeros):
        expected = 0 if number == 0 else max(number, service_module.MTU_MIN)
        assert service_module.parse_mtu("0" * zeros + str(number)) == expected

    @given(number=st.integers(1501, 10**12))
    def test_too_large_is_invalid(self, service_module, number):
        assert service_module.parse_mtu(str(number)) is None

    @given(
        number=st.integers(0, 1500),
        sign=st.sampled_from(["-", "+", "1.", ".5", "1 2", "١٢"]),
    )
    def test_signs_dots_inner_space_and_non_ascii_digits_are_invalid(
        self, service_module, number, sign
    ):
        assert service_module.parse_mtu(sign + str(number)) is None
        assert service_module.parse_mtu(str(number) + sign) is None

    @given(number=st.integers(0, 1500), pad=st.text(alphabet=" \t\n\r\f\v", max_size=3))
    def test_surrounding_whitespace_is_ignored(self, service_module, number, pad):
        assert service_module.parse_mtu(pad + str(number) + pad) == (
            service_module.parse_mtu(str(number))
        )

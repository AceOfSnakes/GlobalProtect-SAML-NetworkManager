"""
Tests of plugins/gnome/nm-gpclient-validate.h, the field rules of the GTK3, GTK4
and Plasma editors (issue #53).

The header is plain C. It is compiled into a small program, once as C and once
as C++ (the Plasma editor includes it from C++). Each rule mirrors a function of
the service, so the editors accept every value the service accepts and reject
only what the service rejects or ignores. Without a compiler the tests are
skipped on a developer machine and FAIL in CI.

Run with: python3 -m pytest tests/unit/test_validate_header.py -v
"""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HEADER_DIR = os.path.join(ROOT, "plugins", "gnome")

# Arguments: a rule name and its texts (prints 1 = valid or 0 per text), or
# "form" and the six fields of nm_gpclient_validate_form (prints the message)
PROGRAM = r"""
#include <stdio.h>
#include "nm-gpclient-validate.h"

int main (int argc, char **argv)
{
    int i;

    if (argc == 8 && !strcmp (argv[1], "form")) {
        const char *m = nm_gpclient_validate_form (argv[2], argv[3], argv[4], argv[5], argv[6][0] == '1', argv[7]);
        printf ("%s\n", m ? m : "OK");
        return 0;
    }
    for (i = 2; i < argc; i++) {
        const char *rule = argv[1], *t = argv[i];
        int ok = 0;
        if (!strcmp (rule, "gateway")) ok = nm_gpclient_validate_gateway (t);
        else if (!strcmp (rule, "username")) ok = nm_gpclient_validate_username (t);
        else if (!strcmp (rule, "browser")) ok = nm_gpclient_validate_browser (t);
        else if (!strcmp (rule, "dns")) ok = nm_gpclient_validate_dns (t);
        else if (!strcmp (rule, "hip")) ok = nm_gpclient_validate_hip_script (t);
        printf ("%d\n", ok);
    }
    return 0;
}
"""

# (text, valid)
GATEWAY = [
    ("vpn.example.com", True),
    ("vpn.example.com:8443", True),
    ("https://vpn.example.com/ssl-vpn?x=1", True),
    ("192.0.2.1", True),
    ("[2001:db8::1]:443", True),
    ("  vpn.example.com \t", True),  # stripped, as the service does
    ("a-b", True),
    ("", False),
    ("   \t\n", False),
    ("-vpn.example.com", False),
    ("  -vpn.example.com", False),
    ("--help", False),
    ("vpn example.com", False),
    ("vpn\texample.com", False),
    ("vpn\x01example.com", False),
    ("vpn\x7fexample.com", False),
]
USERNAME = [
    ("alice", True),
    ("", True),
    ("DOMAIN\\alice", True),
    ("alice@example.com", True),
    ("alice smith", True),  # only control characters are rejected
    (" alice", True),
    ("a-lice", True),
    ("-alice", False),
    ("-", False),
    ("ali\tce", False),
    ("ali\nce", False),
    ("ali\x01ce", False),
    ("ali\x7fce", False),
]
BROWSER = [
    ("", True),
    ("   ", True),
    ("default", True),
    ("edge", True),
    ("firefox", True),
    ("brave-browser", True),
    ("Custom...", True),
    (" chrome ", True),
    ("/usr/bin/google-chrome-stable", True),
    ("/opt/wrapper/b.sh", True),
    ("/", True),
    ("-firefox", False),
    ("--browser", False),
    (".hidden", False),
    ("my browser", False),
    ("fire/fox", False),
    ("fire;fox", False),
    ("/opt/my wrapper.sh", False),
    ("/opt/bro\twser", False),
]
DNS = [
    ("", True),
    ("   ", True),
    ("10.0.0.1", True),
    ("10.0.0.1,10.0.0.2", True),
    ("10.0.0.1;10.0.0.2", True),
    ("10.0.0.1 10.0.0.2", True),
    (" 10.0.0.1 ,; 10.0.0.2\t", True),
    ("2001:db8::1", True),
    ("10.0.0.1, 2001:db8::53", True),
    ("::1", True),
    ("dns.example.com", False),
    ("10.0.0.1 dns.example.com", False),
    ("10.0.1", False),
    ("10.0.0.256", False),
    ("10.0.0.01", False),
    ("10.0.0.1/24", False),
    ("1", False),
    ("2001:db8::zz", False),
    ("a" * 200, False),
]
HIP_SCRIPT = [
    ("", True),
    ("  ", True),
    ("/usr/local/bin/hip.sh", True),
    ("  /usr/local/bin/hip.sh ", True),
    ("hip.sh", False),
    ("./hip.sh", False),
    ("~/hip.sh", False),
    ("bin/hip.sh", False),
]
RULES = {
    "gateway": GATEWAY,
    "username": USERNAME,
    "browser": BROWSER,
    "dns": DNS,
    "hip": HIP_SCRIPT,
}


def build(tmp_path_factory, names, source_name, extra):
    compiler = next((shutil.which(n) for n in names if shutil.which(n)), None)
    if compiler is None:
        message = f"no compiler ({' or '.join(names)}) for {source_name}"
        if os.environ.get("CI"):
            pytest.fail(message + " in CI")
        pytest.skip(message)
    tmp = tmp_path_factory.mktemp("validate_header")
    source = tmp / source_name
    source.write_text(PROGRAM)
    binary = tmp / "check"
    result = subprocess.run(
        [compiler, "-Wall", "-Wextra", "-Werror"] + extra + ["-I", HEADER_DIR, str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return str(binary)


@pytest.fixture(scope="module", params=["c", "c++"])
def program(request, tmp_path_factory):
    if request.param == "c":
        return build(tmp_path_factory, ["cc", "gcc", "clang"], "check.c", ["-std=c99", "-D_POSIX_C_SOURCE=200809L"])
    return build(tmp_path_factory, ["c++", "g++", "clang++"], "check.cpp", [])


def run_rule(binary, rule, texts):
    args = [binary, rule] + [os.fsencode(text) for text in texts]
    out = subprocess.run(args, capture_output=True, check=True).stdout.decode().splitlines()
    return [line == "1" for line in out]


def run_form(binary, *fields):
    args = [binary, "form"] + [os.fsencode(f) for f in fields]
    return subprocess.run(args, capture_output=True, check=True).stdout.decode().strip()


@pytest.mark.parametrize("rule", RULES)
def test_the_rule(program, rule):
    cases = RULES[rule]
    assert run_rule(program, rule, [t for t, _ in cases]) == [ok for _, ok in cases]


@pytest.mark.parametrize("rule", RULES)
def test_every_rule_has_positive_and_negative_cases(rule):
    cases = RULES[rule]
    assert any(ok for _, ok in cases)
    assert sum(1 for _, ok in cases if not ok) >= 3


def service_accepts(s, rule, text):
    """Whether the service takes the value (False: rejects or ignores it)"""
    if rule == "gateway":
        value = text.strip()
        return bool(value) and not value.startswith("-") and not s.UNSAFE_GATEWAY_RE.search(value)
    if rule == "username":
        return not s.is_unsafe_username(text)
    if rule == "browser":
        value = text.strip()
        return (
            not value
            or bool(s.CUSTOM_BROWSER_PATH_RE.match(value))
            or bool(s.BARE_BROWSER_NAME_RE.fullmatch(value))
        )
    if rule == "dns":
        return all(s.parse_ip_address(e) is not None for e in s.parse_dns_servers(text))
    return not text.strip() or os.path.isabs(text.strip())


@pytest.mark.parametrize("rule", RULES)
def test_the_header_gives_what_the_service_gives(program, service_module, rule):
    cases = RULES[rule]
    results = run_rule(program, rule, [t for t, _ in cases])
    for (text, _), result in zip(cases, results, strict=True):
        assert result == service_accepts(service_module, rule, text), (rule, text)


FORM_OK = ("vpn.example.com", "alice", "default", "10.0.0.1", "1", "/usr/bin/hip.sh")


def test_the_form_accepts_a_good_profile(program):
    assert run_form(program, *FORM_OK) == "OK"
    assert run_form(program, "vpn.example.com", "", "", "", "0", "") == "OK"


@pytest.mark.parametrize(
    "index, bad, word",
    [
        (0, "", "gateway is required"),
        (0, "-vpn", "gateway"),
        (1, "-alice", "username"),
        (2, "my browser", "browser"),
        (3, "dns.example.com", "dns"),
        (5, "hip.sh", "hip-script"),
    ],
)
def test_the_form_names_the_bad_field(program, index, bad, word):
    fields = list(FORM_OK)
    fields[index] = bad
    assert word in run_form(program, *fields)


def test_the_hip_script_is_ignored_while_hip_is_off(program):
    assert run_form(program, "vpn.example.com", "", "", "", "0", "hip.sh") == "OK"


EDITORS = [
    "plugins/gnome/nm-gpclient-editor.c",
    "plugins/gnome/nm-gpclient-editor-gtk3.c",
    "plugins/plasma/gpclientwidget.cpp",
]


@pytest.mark.parametrize("path", EDITORS)
def test_the_editor_includes_the_header_and_calls_the_form_check(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        text = handle.read()
    assert '#include "nm-gpclient-validate.h"' in text
    assert "nm_gpclient_validate_form" in text


@pytest.mark.parametrize("path", EDITORS)
def test_the_editor_has_no_own_copy_of_the_rules(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        text = handle.read()
    # the old check was an inline "is the gateway empty" in each editor
    assert "gateway is required" not in text
    assert "inet_pton" not in text

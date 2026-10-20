"""
Tests of plugins/gnome/nm-gpclient-mtu.h, the MTU rule of the GTK editors.

The header is compiled into a small program (gcc and the GLib headers are
needed; the test is skipped without them). The editor shows what the service's
parse_mtu() would use: the same rule, with 0 for automatic and for invalid.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import shutil
import subprocess

import pytest

HEADER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "gnome")

PROGRAM = r"""
#include <stdio.h>
#include "nm-gpclient-mtu.h"

int main (int argc, char **argv)
{
    int i;
    for (i = 1; i < argc; i++)
        printf ("%d %d\n", nm_gpclient_parse_mtu (argv[i]),
                nm_gpclient_mtu_is_automatic_text (argv[i]));
    return 0;
}
"""

# (text, shown by the editor, automatic text)
CASES = [
    ("1300", 1300, False),
    (" \t1300\n", 1300, False),
    ("01300", 1300, False),
    ("576", 576, False),
    ("1500", 1500, False),
    ("500", 576, False),
    ("1", 576, False),
    ("", 0, True),
    ("   ", 0, True),
    ("0", 0, True),
    ("00", 0, True),
    (" 0000 ", 0, True),
    ("1501", 0, False),
    ("1600", 0, False),
    ("99999999999", 0, False),
    ("abc", 0, False),
    ("+1300", 0, False),
    ("-1300", 0, False),
    ("13 00", 0, False),
    ("1300.0", 0, False),
    ("1300x", 0, False),
    ("١٣٠٠", 0, False),
    (" 1300", 0, False),
]


@pytest.fixture(scope="module")
def header_program(tmp_path_factory):
    gcc = shutil.which("gcc") or shutil.which("cc")
    pkg_config = shutil.which("pkg-config")
    if not gcc or not pkg_config:
        pytest.skip("no C compiler or pkg-config")
    flags = subprocess.run([pkg_config, "--cflags", "--libs", "glib-2.0"], capture_output=True, text=True)
    if flags.returncode != 0:
        pytest.skip("no GLib development files")
    tmp = tmp_path_factory.mktemp("mtu_header")
    source = tmp / "check.c"
    source.write_text(PROGRAM)
    binary = tmp / "check"
    build = subprocess.run(
        [gcc, "-Wall", "-Werror", "-I", HEADER_DIR, str(source), "-o", str(binary)] + flags.stdout.split(),
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    return str(binary)


def run_program(binary, texts):
    result = subprocess.run([binary] + texts, capture_output=True, text=True, check=True)
    return [tuple(int(v) for v in line.split()) for line in result.stdout.splitlines()]


def test_the_editor_rule(header_program):
    results = run_program(header_program, [text for text, _, _ in CASES])

    assert results == [(shown, int(automatic)) for _, shown, automatic in CASES]


def test_the_editor_shows_what_the_service_uses(header_program, service_module):
    results = run_program(header_program, [text for text, _, _ in CASES])

    for (text, _, _), (shown, _) in zip(CASES, results):
        # the service: 0 = automatic, None = invalid
        assert shown == (service_module.parse_mtu(text) or 0), repr(text)

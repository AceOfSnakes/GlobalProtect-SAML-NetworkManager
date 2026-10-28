"""
The openSUSE install test and build retry `zypper refresh`: download.opensuse.org
sends each file to a mirror, and one that has not synced the newest metadata yet
fails the refresh ("Repository 'openSUSE-Tumbleweed-Oss' is invalid").

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = ["rpm/install-test.sh", "rpm/container-build.sh"]
LOOP_RE = re.compile(
    r"for attempt in 1 2 3 4 5; do\n"
    r"\s+zypper --non-interactive refresh && break\n"
    r"\s+\[ \"\$attempt\" -lt 5 \] \|\| .+\n"
    r"\s+sleep 30\n"
    r"\s+done\n"
)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def has_retry(text):
    return bool(LOOP_RE.search(text))


@pytest.mark.parametrize("path", SCRIPTS)
def test_the_refresh_is_retried(path):
    assert has_retry(read(path))


@pytest.mark.parametrize("path", SCRIPTS)
@pytest.mark.parametrize("old, new", [
    ("zypper --non-interactive refresh && break", "zypper --non-interactive refresh"),
    ('[ "$attempt" -lt 5 ] ||', "true ||"),
    ("sleep 30\n", ""),
])
def test_a_refresh_without_retry_is_found(path, old, new):
    text = read(path)
    assert old in text

    assert not has_retry(text.replace(old, new, 1))


def run_loop(path, results, tmp_path):
    """The loop of `path` with a fake zypper that exits with `results` in turn
    and a sleep that does nothing; returns (exit code, zypper calls)"""
    loop = LOOP_RE.search(read(path)).group(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "results").write_text(" ".join(map(str, results)))
    for name, body in (
        ("zypper", 'set -- $(cat "$T/results"); echo x >> "$T/calls"\n'
                   'echo "${@:2}" > "$T/results"; exit "$1"\n'),
        ("sleep", "exit 0\n"),
    ):
        tool = bin_dir / name
        tool.write_text("#!/bin/bash\n" + body)
        tool.chmod(0o755)
    script = 'set -euo pipefail\nfail() { echo "$1" >&2; exit 1; }\n' + loop + "echo refreshed\n"
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "T": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )
    calls = (tmp_path / "calls").read_text().count("x")
    return proc.returncode, calls, proc.stdout


@pytest.mark.parametrize("path", SCRIPTS)
def test_a_later_refresh_that_works_goes_on(path, tmp_path):
    code, calls, out = run_loop(path, [4, 4, 0], tmp_path)
    assert (code, calls, out.strip()) == (0, 3, "refreshed")


@pytest.mark.parametrize("path", SCRIPTS)
def test_five_failed_refreshes_stop_the_script(path, tmp_path):
    code, calls, out = run_loop(path, [4, 4, 4, 4, 4, 0], tmp_path)
    assert (code, calls, out) == (1, 5, "")

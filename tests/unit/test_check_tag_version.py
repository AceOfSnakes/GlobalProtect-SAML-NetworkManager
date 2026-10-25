"""
Tests for .github/scripts/check_tag_version.py, which stops a release build when
the tag (v2.0.0) differs from the upstream version in the top line of
debian/changelog (2.0.0-1). Every positive test has negative counterparts.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import subprocess
import sys

import pytest
import workflow_steps

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "check_tag_version.py")


def run(tag, text, tmp_path):
    changelog = tmp_path / "changelog"
    changelog.write_text(text)
    return subprocess.run([sys.executable, SCRIPT, tag, str(changelog)], capture_output=True, text=True)


def entry(version):
    return f"network-manager-gpclient ({version}) unstable; urgency=medium\n\n  * x\n\n -- A <a@b.c>  Mon, 01 Jan 2024 00:00:00 +0000\n"


@pytest.mark.parametrize("tag, version", [
    ("v2.0.0", "2.0.0-1"),
    ("v2.0.0", "2.0.0-3"),
    ("v1.5.0", "1.5.0-1"),
    ("v2.0.0", "1:2.0.0-1"),
    ("v2.1", "2.1-1"),
])
def test_matching_tag_passes(tag, version, tmp_path):
    result = run(tag, entry(version), tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tag, version", [
    ("v2.0.1", "2.0.0-1"),
    ("v2.0", "2.0.0-1"),
    ("v2.0.0", "2.0.0.1-1"),
    ("v1.5.0", "2.0.0-1"),
    ("v2.0.0", "2.0.0~rc1-1"),
])
def test_mismatching_tag_fails(tag, version, tmp_path):
    result = run(tag, entry(version), tmp_path)
    assert result.returncode == 1
    assert "does not match" in result.stderr


@pytest.mark.parametrize("tag", ["2.0.0", "v", "v2.0.0-1", "v2.0.0\n", "vx", "v2..0", "refs/tags/v2.0.0", ""])
def test_malformed_tag_fails(tag, tmp_path):
    result = run(tag, entry("2.0.0-1"), tmp_path)
    assert result.returncode == 1
    assert "not a release tag" in result.stderr


@pytest.mark.parametrize("text", ["", "\n", "garbage\n", " package (1.0-1)\n", "package 1.0-1\n"])
def test_malformed_changelog_fails(text, tmp_path):
    result = run("v1.0", text, tmp_path)
    assert result.returncode == 1
    assert "changelog" in result.stderr


def test_missing_changelog_fails(tmp_path):
    result = subprocess.run([sys.executable, SCRIPT, "v1.0", str(tmp_path / "none")], capture_output=True, text=True)
    assert result.returncode == 1


def test_the_repository_changelog_gives_a_valid_tag():
    sys.path.insert(0, os.path.join(ROOT, ".github", "scripts"))
    import check_tag_version
    with open(os.path.join(ROOT, "debian", "changelog"), encoding="utf-8") as handle:
        text = handle.read()
    check_tag_version.check(f"v{check_tag_version.changelog_version(text)}", text)


def test_the_workflow_checks_the_tag_before_building():
    text = workflow_steps.read_workflow("build-release.yml")
    assert "python3 .github/scripts/check_tag_version.py" in text
    assert text.count("needs: check-tag") == 2

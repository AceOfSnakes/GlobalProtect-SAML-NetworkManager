"""
Tests for the username handling in browser-wrapper.

A username like first.last used to be rejected before the browser was started.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import shutil
import subprocess

import pytest

WRAPPER = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "browser-wrapper.sh"
)
URL = "https://portal.example.com/saml"
# The wrapper keeps its files in /tmp/edge-wrapper-<uid>; stay clear of real ones
FAKE_UID = 48213


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the wrapper runs sudo")
def test_accepts_username_with_a_dot(tmp_path):
    (tmp_path / "id").write_text(f"#!/bin/sh\necho {FAKE_UID}\n")
    (tmp_path / "browser").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/opened\n')
    (tmp_path / "id").chmod(0o755)
    (tmp_path / "browser").chmod(0o755)

    try:
        subprocess.run(
            ["bash", WRAPPER, URL],
            env={
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "USER": "first.last",
                "GP_BROWSER": str(tmp_path / "browser"),
            },
            timeout=30,
        )
    finally:
        shutil.rmtree(f"/tmp/edge-wrapper-{FAKE_UID}", ignore_errors=True)
        if os.path.exists(f"/tmp/edge-wrapper-{FAKE_UID}.log"):
            os.remove(f"/tmp/edge-wrapper-{FAKE_UID}.log")

    opened = tmp_path / "opened"
    assert opened.exists(), "the browser was not started"
    assert opened.read_text() == URL + "\n"

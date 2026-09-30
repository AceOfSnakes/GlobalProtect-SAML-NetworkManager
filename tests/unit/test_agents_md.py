"""
AGENTS.md is loaded into an AI agent's context in full, so it has to stay
small, and CLAUDE.md has to keep pointing at it (Claude Code reads CLAUDE.md,
other agents read AGENTS.md).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAX_AGENTS_MD_BYTES = 32 * 1024


def check_size(path, limit=MAX_AGENTS_MD_BYTES):
    """Problems with the size of `path` (empty list when it is fine)"""
    if not os.path.isfile(path):
        return [f"{path} does not exist"]
    size = os.path.getsize(path)
    if size > limit:
        return [f"{path} is {size} bytes, the limit is {limit}"]
    return []


def check_claude_md(path):
    """Problems with a CLAUDE.md: it must exist and point at AGENTS.md"""
    if not os.path.isfile(path):
        return [f"{path} does not exist"]
    with open(path, encoding="utf-8") as handle:
        if "AGENTS.md" not in handle.read():
            return [f"{path} does not reference AGENTS.md"]
    return []


class TestAgentsMd:
    def test_agents_md_exists_and_is_small_enough(self):
        assert check_size(os.path.join(ROOT, "AGENTS.md")) == []

    def test_a_file_one_byte_over_the_limit_is_rejected(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_bytes(b"x" * (MAX_AGENTS_MD_BYTES + 1))

        problems = check_size(str(path))

        assert len(problems) == 1
        assert "limit" in problems[0]

    def test_a_file_of_exactly_the_limit_is_accepted(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_bytes(b"x" * MAX_AGENTS_MD_BYTES)

        assert check_size(str(path)) == []

    def test_a_missing_file_is_rejected(self, tmp_path):
        assert check_size(str(tmp_path / "AGENTS.md")) != []


class TestClaudeMd:
    def test_claude_md_exists_and_references_agents_md(self):
        assert check_claude_md(os.path.join(ROOT, "CLAUDE.md")) == []

    @pytest.mark.parametrize(
        "content", ["", "# CLAUDE.md\n\nSome unrelated instructions.\n", "agents.md"]
    )
    def test_claude_md_without_the_reference_is_rejected(self, tmp_path, content):
        path = tmp_path / "CLAUDE.md"
        path.write_text(content)

        assert check_claude_md(str(path)) != []

    def test_a_missing_claude_md_is_rejected(self, tmp_path):
        assert check_claude_md(str(tmp_path / "CLAUDE.md")) != []

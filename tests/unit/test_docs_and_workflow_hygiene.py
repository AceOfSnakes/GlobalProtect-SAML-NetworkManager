"""
Checks for GitHub issue 54: the docs find the suite of the apt repository with the codename of
/etc/os-release (UBUNTU_CODENAME, which derivatives of Ubuntu set), not with `lsb_release -cs`;
publish-apt.yml does not cap the list of releases; every job of every workflow has timeout-minutes.

Every check has a negative counterpart: a synthetic text that breaks the rule is rejected.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import glob
import os
import re

import pytest

from workflow_steps import WORKFLOWS, read_workflow

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CODENAME = '$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")'
DOCS = ["README.md", "docs/APT_REPO.md"]


def codename_problems(text):
    return ["lsb_release -cs"] if "lsb_release -cs" in text else []


def limit_problems(text):
    return re.findall(r"gh release list[^\n]*--limit \d+", text)


def jobs_without_timeout(text):
    """Names of the jobs that run steps (not a call of a reusable workflow) and have no timeout-minutes"""
    lines = text.splitlines()
    start = lines.index("jobs:")
    jobs, current = {}, None
    for line in lines[start + 1:]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = jobs.setdefault(match.group(1), [])
        elif current is not None:
            current.append(line)
    return [name for name, body in jobs.items()
            if not any(x.startswith(("    timeout-minutes: ", "    uses: ")) for x in body)]


class TestCodename:
    @pytest.mark.parametrize("doc", DOCS)
    def test_the_docs_do_not_use_lsb_release(self, doc):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
            assert codename_problems(handle.read()) == []

    @pytest.mark.parametrize("doc", DOCS)
    def test_the_docs_use_the_os_release_codename(self, doc):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
            assert CODENAME in handle.read()

    def test_lsb_release_is_rejected(self):
        assert codename_problems("deb ... $(lsb_release -cs) main")

    def test_text_without_it_is_accepted(self):
        assert codename_problems("deb ... " + CODENAME + " main") == []


class TestReleaseList:
    def test_publish_apt_has_no_fixed_limit(self):
        assert limit_problems(read_workflow("publish-apt.yml")) == []

    def test_a_fixed_limit_is_rejected(self):
        assert limit_problems("gh release list --limit 100 --json tagName") != []

    def test_the_release_list_is_paginated_and_still_filtered(self):
        text = read_workflow("publish-apt.yml")
        assert "gh api --paginate" in text
        assert ".draft == false and .prerelease == false" in text


class TestTimeouts:
    @pytest.mark.parametrize("workflow", sorted(os.path.basename(p) for p in glob.glob(os.path.join(WORKFLOWS, "*.yml"))))
    def test_every_job_has_a_timeout(self, workflow):
        assert jobs_without_timeout(read_workflow(workflow)) == []

    def test_a_job_without_a_timeout_is_rejected(self):
        text = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
        assert jobs_without_timeout(text) == ["a"]

    def test_a_job_with_a_timeout_or_a_workflow_call_is_accepted(self):
        text = ("jobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: x\n"
                "  b:\n    uses: ./.github/workflows/x.yml\n")
        assert jobs_without_timeout(text) == []

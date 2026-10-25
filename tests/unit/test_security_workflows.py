"""
Tests for the security workflows (codeql.yml, zizmor.yml, scorecard.yml): the workflow
token is read-only, and the rights to write to the Security tab or to get an OIDC
token are given to the one job that needs them.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import re

import pytest
from workflow_steps import read_workflow

WORKFLOWS = ["codeql.yml", "zizmor.yml", "scorecard.yml"]


def top_permissions(text):
    match = re.search(r"^permissions:\n((?:  .*\n)+)", text, re.M)
    return match.group(1) if match else None


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_the_workflow_token_is_read_only(workflow):
    assert top_permissions(read_workflow(workflow)) == "  contents: read\n"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_write_rights_are_on_the_job_only(workflow):
    text = read_workflow(workflow)
    jobs = text[text.index("\njobs:\n"):]
    assert "write" not in text[:text.index("\njobs:\n")]
    assert all(line.startswith("      ") for line in re.findall(r"^ +\S+: write.*$", jobs, re.M))


def test_codeql_scans_python_and_the_workflows_on_pushes_pull_requests_and_weekly():
    text = read_workflow("codeql.yml")
    assert "language: [python, actions]" in text
    for trigger in ("  push:\n", "  pull_request:\n", "  schedule:\n"):
        assert trigger in text
    assert "security-events: write" in text


def test_zizmor_is_pinned_and_runs_for_pull_requests_and_main():
    text = read_workflow("zizmor.yml")
    assert re.search(r'ZIZMOR_VERSION: "\d+\.\d+\.\d+"', text)
    assert "  pull_request:\n" in text and "      - main\n" in text


def test_scorecard_publishes_weekly():
    text = read_workflow("scorecard.yml")
    assert "publish_results: true" in text
    assert "  schedule:\n" in text and "      - main\n" in text
    assert "id-token: write" in text

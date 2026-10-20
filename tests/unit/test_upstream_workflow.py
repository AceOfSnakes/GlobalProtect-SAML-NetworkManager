"""
Tests for .github/workflows/upstream.yml: the properties that make the gate
work and keep it safe. The file is read as text (CI installs only pytest, no
YAML parser).

Every positive test has a negative counterpart: the label events re-run the
check, and no step puts an expression into the script text.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import workflow_steps

WORKFLOW = "upstream.yml"


def text():
    with open(os.path.join(workflow_steps.WORKFLOWS, WORKFLOW), encoding="utf-8") as handle:
        return handle.read()


def test_pull_request_trigger_includes_the_label_events():
    assert "types: [opened, synchronize, reopened, labeled, unlabeled]" in text()


def test_only_the_safe_triggers_are_used():
    triggers = re.search(r"^on:\n(.*?)^permissions:", text(), re.M | re.S).group(1)
    assert "pull_request_target" not in triggers
    assert "workflow_run" not in triggers
    assert "push:" in triggers and "workflow_dispatch:" in triggers


def test_token_is_read_only_and_no_secrets_are_used():
    assert "permissions:\n  contents: read\n" in text()
    assert "secrets." not in text()


def test_checkout_gets_the_top_level_submodule_only():
    step = "\n".join(workflow_steps.step_lines(WORKFLOW, "Checkout code"))
    assert "submodules: true" in step
    assert "recursive" not in step


def test_labels_and_base_ref_reach_the_gate_through_the_environment():
    step = "\n".join(workflow_steps.step_lines(WORKFLOW, "Gate breaking upstream changes"))
    assert "LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}" in step
    assert 'BASE_REF: ${{ github.base_ref }}' in step
    assert '--labels "$LABELS"' in step
    assert "if: always() && github.event_name == 'pull_request'" in step


def test_no_expression_is_inside_a_script():
    for line in text().splitlines():
        stripped = line.strip()
        if "${{" in stripped:
            assert not stripped.startswith(("git ", "python", "TAG=", "BASE=", "echo", "if ")), line


def test_the_gate_reads_the_contract_of_the_base_branch():
    script = workflow_steps.step_script(WORKFLOW, "Gate breaking upstream changes", {
        "${{ github.base_ref }}": "main",
        "${{ toJSON(github.event.pull_request.labels.*.name) }}": "[]",
    })
    assert 'git show FETCH_HEAD:upstream/contract.json' in script
    assert '--base-contract "$BASE"' in script


def test_the_gate_step_is_not_run_outside_pull_requests():
    # A push to main has no base branch to compare with.
    assert "github.event_name == 'pull_request'" in "\n".join(
        workflow_steps.step_lines(WORKFLOW, "Gate breaking upstream changes"))
    assert "github.event_name == 'pull_request'" not in "\n".join(
        workflow_steps.step_lines(WORKFLOW, "Check the pinned upstream"))

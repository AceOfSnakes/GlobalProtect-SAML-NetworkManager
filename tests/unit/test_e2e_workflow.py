"""
Tests for .github/workflows/e2e.yml: it runs only by hand or when a pull
request gets the label "e2e", never on push or on every pull request, it needs
no secrets, and no step puts an expression into a script. The file is read as
text (CI installs only pytest, no YAML parser).

Every positive test has a negative counterpart (no push trigger, no secrets,
no unlabelled pull request runs).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import workflow_steps

WORKFLOW = "e2e.yml"


def text():
    with open(os.path.join(workflow_steps.WORKFLOWS, WORKFLOW), encoding="utf-8") as handle:
        return handle.read()


def triggers():
    return re.search(r"^on:\n(.*?)^permissions:", text(), re.M | re.S).group(1)


def test_runs_by_hand_and_for_the_label_only():
    assert "workflow_dispatch:" in triggers()
    assert "pull_request:\n    types: [labeled]" in triggers()


def test_no_push_and_no_unrestricted_pull_request_trigger():
    assert "push:" not in triggers()
    assert "pull_request_target" not in triggers()
    assert "schedule" not in triggers()
    assert triggers().count("pull_request:") == 1


def test_job_runs_only_for_dispatch_or_the_e2e_label():
    assert "if: github.event_name == 'workflow_dispatch' || github.event.label.name == 'e2e'" in text()


def test_token_is_read_only_and_there_are_no_secrets():
    assert "permissions:\n  contents: read\n" in text()
    assert "secrets." not in text()
    assert "contents: write" not in text()


def test_scenario_input_lists_every_scenario():
    options = re.search(r"options: \[(.*?)\]", text()).group(1).replace(" ", "").split(",")
    assert options == ["all", "gnome", "kde", "routing", "auth-cancel"]
    matrix = re.search(r"scenario: \[(gnome.*?)\]", text()).group(1).replace(" ", "").split(",")
    assert matrix == ["gnome", "kde", "routing", "auth-cancel"]


def test_image_is_built_with_the_actions_cache_and_artifacts_are_kept_14_days():
    assert "cache-from: type=gha" in text()
    assert "cache-to: type=gha" in text()
    assert "retention-days: 14" in text()
    assert "if: always() && steps.pick.outputs.run == 'true'" in text()


def test_no_run_script_contains_an_expression():
    for name in re.findall(r"^      - name: (.*)$", text(), re.M):
        lines = workflow_steps.step_lines(WORKFLOW, name)
        in_run = False
        for line in lines[1:]:
            if line.startswith("        run:"):
                in_run = True
                assert "${{" not in line, f"expression in the run line of step {name}"
            elif in_run and line.startswith("          "):
                assert "${{" not in line, f"expression in the script of step {name}"
            else:
                in_run = False


def concurrency_group():
    return re.search(r"^concurrency:\n  group: (.*)$", text(), re.M).group(1)


def test_concurrency_group_of_a_pull_request_includes_the_label():
    assert "github.event.label.name" in concurrency_group()


def test_another_label_does_not_share_the_group_of_the_e2e_run():
    # With only github.ref in the group, adding any other label started a
    # (skipped) run in the same group and cancelled the running e2e run
    assert "&& github.ref ||" not in concurrency_group()


def upload_steps(workflow_text):
    """The upload steps of the workflow, each as the text of its lines"""
    names = workflow_steps.step_names(WORKFLOW, workflow_text)
    steps = [workflow_steps.step_lines(WORKFLOW, name, workflow_text) for name in names]
    return ["\n".join(lines) for lines in steps if "actions/upload-artifact" in "\n".join(lines)]


def images_are_uploaded_only_when_made(workflow_text):
    """True when every upload step that has docs/images has the condition WEBP == 'true'"""
    for step in upload_steps(workflow_text):
        if "docs/images/" in step and "env.WEBP == 'true'" not in step:
            return False
    return True


def test_images_are_uploaded_only_when_they_are_made():
    assert any("docs/images/*.webp" in step for step in upload_steps(text()))
    assert images_are_uploaded_only_when_made(text())
    assert "gif" not in text().lower()


def test_a_workflow_that_always_uploads_the_images_is_rejected():
    always = text().replace(
        "if: always() && steps.pick.outputs.run == 'true' && env.WEBP == 'true'",
        "if: always() && steps.pick.outputs.run == 'true'",
    )
    assert always != text()
    assert not images_are_uploaded_only_when_made(always)
    # The images in the upload step of the results are rejected as well
    mixed = text().replace(
        "            tests/e2e/out/${{ matrix.scenario }}\n",
        "            tests/e2e/out/${{ matrix.scenario }}\n            docs/images/*.webp\n",
        1,
    )
    assert mixed != text()
    assert not images_are_uploaded_only_when_made(mixed)

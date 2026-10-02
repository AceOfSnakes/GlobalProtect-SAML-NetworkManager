"""
Tests for the "Upgrade test from the last release" step of
.github/workflows/build-release.yml and for .github/scripts/upgrade-test.sh,
which it starts in a fresh ubuntu container. The step is taken out of the
workflow file and run as it is, with a fake docker found through PATH: nothing
starts a container and nothing touches the network.

Every positive test has negative counterparts: when one scenario fails the
others still run and the step fails at the end naming the scenario; when all
succeed the step passes; a bad scenario or a missing debs directory is refused
by the script.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import subprocess
import sys

import pytest
import workflow_steps

WORKFLOW = "build-release.yml"
STEP = "Upgrade test from the last release"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "upgrade-test.sh")
SCENARIOS = ["gnome", "plasma", "plasma-new"]

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/docker_calls"
# the scenario is the argument after the script path
while [ $# -gt 0 ]; do
    if [ "$1" = "/scripts/upgrade-test.sh" ]; then scenario="$2"; fi
    shift
done
echo "fake docker output for $scenario"
case " $FAKE_FAIL " in *" $scenario "*) echo "fake docker: $scenario failed" >&2; exit 1 ;; esac
"""


def lines_of_step():
    return workflow_steps.step_lines(WORKFLOW, STEP)


def run_step(tmp_path, fail=""):
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.path.dirname(sys.executable)}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_DIR": str(fake),
        "FAKE_FAIL": fail,
        "UBUNTU": "24.04",
        "ARCH": "arm64",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", workflow_steps.step_script(WORKFLOW, STEP)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    calls = (fake / "docker_calls").read_text().splitlines() if (fake / "docker_calls").exists() else []
    return result, calls


class TestStepInTheWorkflow:
    def names(self):
        with open(os.path.join(workflow_steps.WORKFLOWS, WORKFLOW), encoding="utf-8") as handle:
            return [l.strip()[len("- name: "):] for l in handle if l.startswith("      - name:")]

    def test_runs_after_the_packages_are_verified_and_uploaded(self):
        # After the upload: a failed upgrade test must not take the packages
        # away from a manual test of the pull request
        names = self.names()

        assert names.index(STEP) == names.index("Upload artifacts") + 1
        assert names.index(STEP) > names.index("Verify .deb files exist")

    def test_matrix_values_come_through_env(self):
        text = "\n".join(lines_of_step())

        assert "          UBUNTU: ${{ matrix.ubuntu }}" in text
        assert "          ARCH: ${{ matrix.arch }}" in text

    def test_no_expression_inside_the_script(self):
        # step_script() asserts that no ${{ is left
        assert "${{" not in workflow_steps.step_script(WORKFLOW, STEP)

    def test_all_scenarios_are_listed(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert "for scenario in gnome plasma plasma-new; do" in script

    def test_mounts_and_image(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert '-v "$PWD/output/ubuntu${UBUNTU}-${ARCH}:/debs:ro"' in script
        assert '-v "$PWD/.github/scripts:/scripts:ro"' in script
        assert '"ubuntu:${UBUNTU}" bash /scripts/upgrade-test.sh "$scenario" /debs' in script

    def test_each_scenario_has_its_own_group(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert '::group::Upgrade test: $scenario' in script
        assert script.count("::endgroup::") == 1

    def test_the_build_job_gets_no_permissions_of_its_own(self):
        with open(os.path.join(workflow_steps.WORKFLOWS, WORKFLOW), encoding="utf-8") as handle:
            text = handle.read()

        build = text.split("\n  build:\n")[1].split("\n  release:\n")[0]
        assert "permissions:" not in build


class TestRunningTheStep:
    def test_all_scenarios_pass(self, tmp_path):
        result, calls = run_step(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert [c.split()[-2] for c in calls] == SCENARIOS
        assert "::error::" not in result.stdout

    def test_every_call_gets_the_matrix_values(self, tmp_path):
        _, calls = run_step(tmp_path)

        for call in calls:
            assert call.startswith("run --rm -v ")
            assert f"{tmp_path}/output/ubuntu24.04-arm64:/debs:ro" in call
            assert f"{tmp_path}/.github/scripts:/scripts:ro" in call
            assert " ubuntu:24.04 bash /scripts/upgrade-test.sh " in call
            assert call.endswith(" /debs")

    @pytest.mark.parametrize("failing", SCENARIOS)
    def test_one_failure_runs_the_others_and_fails_at_the_end(self, tmp_path, failing):
        result, calls = run_step(tmp_path, fail=failing)

        assert result.returncode == 1
        assert [c.split()[-2] for c in calls] == SCENARIOS
        assert f"::error::Upgrade test failed for: {failing}" in result.stdout

    def test_all_failures_are_listed(self, tmp_path):
        result, calls = run_step(tmp_path, fail="gnome plasma-new")

        assert result.returncode == 1
        assert len(calls) == 3
        assert "::error::Upgrade test failed for: gnome plasma-new" in result.stdout

    def test_output_of_each_scenario_is_in_its_own_group(self, tmp_path):
        result, _ = run_step(tmp_path, fail="plasma")

        out = result.stdout
        for scenario in SCENARIOS:
            start = out.index(f"::group::Upgrade test: {scenario}\n")
            end = out.index("::endgroup::", start)
            assert f"fake docker output for {scenario}" in out[start:end]


class TestScript:
    def test_syntax(self):
        result = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)

        assert result.returncode == 0, result.stderr

    def test_strict_mode(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            assert "set -euo pipefail" in handle.read()

    @pytest.mark.parametrize("scenario", ["", "kde", "GNOME", "plasma-6"])
    def test_unknown_scenario_is_refused(self, scenario, tmp_path):
        result = subprocess.run(["bash", SCRIPT, scenario, str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "unknown scenario" in result.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root users")
    def test_refuses_to_run_as_non_root(self, tmp_path):
        result = subprocess.run(["bash", SCRIPT, "gnome", str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "run as root" in result.stderr

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

import base64
import os
import re
import subprocess
import sys

import pytest
import workflow_steps

WORKFLOW = "build-release.yml"
STEP = "Upgrade test from the last release"
UPLOAD_STEP = "Upload GUI screenshots"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "upgrade-test.sh")
GUI_SCRIPT = os.path.join(ROOT, ".github", "scripts", "gui-smoke.sh")
SCENARIOS = ["gnome", "plasma"]

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

        assert "for scenario in gnome plasma; do" in script
        assert "plasma-new" not in script

    def test_mounts_and_image(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert '-v "$PWD/output/ubuntu${UBUNTU}-${ARCH}:/debs:ro"' in script
        assert '-v "$PWD/.github/scripts:/scripts:ro"' in script
        assert '"ubuntu:${UBUNTU}" bash /scripts/upgrade-test.sh "$scenario" /debs /out' in script

    def test_the_screenshot_directory_is_mounted_writable(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert '-v "$PWD/gui-smoke:/out" \\' in script
        assert '-v "$PWD/gui-smoke:/out:ro"' not in script
        # created by the runner, not by docker (as root)
        assert script.index('mkdir -p "$PWD/gui-smoke"') < script.index("docker run")

    def test_each_scenario_has_its_own_group(self):
        script = workflow_steps.step_script(WORKFLOW, STEP)

        assert '::group::Upgrade test: $scenario' in script
        assert script.count("::endgroup::") == 1

    def test_screenshots_are_uploaded_even_after_a_failure(self):
        names = self.names()
        step = "\n".join(workflow_steps.step_lines(WORKFLOW, UPLOAD_STEP))

        assert names.index(UPLOAD_STEP) == names.index(STEP) + 1
        assert "        if: always()" in step
        assert "uses: actions/upload-artifact@v4" in step
        assert "name: gui-smoke-ubuntu-${{ matrix.ubuntu }}-${{ matrix.arch }}" in step
        assert "path: gui-smoke/*.png" in step
        assert "if-no-files-found: ignore" in step

    def test_the_build_job_gets_no_permissions_of_its_own(self):
        with open(os.path.join(workflow_steps.WORKFLOWS, WORKFLOW), encoding="utf-8") as handle:
            text = handle.read()

        build = text.split("\n  build:\n")[1].split("\n  release:\n")[0]
        assert "permissions:" not in build


class TestRunningTheStep:
    def test_all_scenarios_pass(self, tmp_path):
        result, calls = run_step(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert [c.split()[-3] for c in calls] == SCENARIOS
        assert "::error::" not in result.stdout

    def test_every_call_gets_the_matrix_values(self, tmp_path):
        _, calls = run_step(tmp_path)

        for call in calls:
            assert call.startswith("run --rm -v ")
            assert f"{tmp_path}/output/ubuntu24.04-arm64:/debs:ro" in call
            assert f"{tmp_path}/.github/scripts:/scripts:ro" in call
            assert " ubuntu:24.04 bash /scripts/upgrade-test.sh " in call
            assert call.endswith(" /debs /out")
            assert f"{tmp_path}/gui-smoke:/out" in call

    @pytest.mark.parametrize("failing", SCENARIOS)
    def test_one_failure_runs_the_others_and_fails_at_the_end(self, tmp_path, failing):
        result, calls = run_step(tmp_path, fail=failing)

        assert result.returncode == 1
        assert [c.split()[-3] for c in calls] == SCENARIOS
        assert f"::error::Upgrade test failed for: {failing}" in result.stdout

    def test_all_failures_are_listed(self, tmp_path):
        result, calls = run_step(tmp_path, fail="gnome plasma")

        assert result.returncode == 1
        assert len(calls) == 2
        assert "::error::Upgrade test failed for: gnome plasma" in result.stdout

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

    def read(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            return handle.read()

    def test_gui_smoke_test_follows_the_pass_checks(self):
        text = self.read()
        call = 'bash "$HERE/gui-smoke.sh" "$SCENARIO" "$GUI_OUT"'

        assert text.count(call) == 1
        assert text.index('echo "SKIP: ') < text.index(call)
        assert text.index("check_upgrade.py") < text.index(call)
        assert text.index("check_plugin network-manager-gpclient-plasma") < text.index(call)
        assert text.index('echo "PASS: ') < text.index(call)
        # nothing but the end of the script follows the call
        assert text.rstrip().endswith("fi")
        assert text[text.index(call):].count("\n") <= 2

    def test_one_plasma_package_and_no_transitional_one(self):
        text = self.read()

        assert "OLD_PACKAGE=network-manager-gpclient-plasma ;;" in text
        assert "plasma-5" not in text and "plasma-6" not in text and "transitional" not in text

    def test_the_qt_directory_is_checked_for_plasma_only(self):
        text = self.read()

        assert text.count("--plasma-files") == 1
        assert 'if [ "$SCENARIO" = plasma ]; then\n    dpkg -L network-manager-gpclient-plasma' in text
        assert 'if [ "$SCENARIO" = plasma ]; then\n    check_plugin network-manager-gpclient-plasma' in text

    def test_gui_smoke_test_runs_only_with_an_output_directory(self):
        text = self.read()

        assert 'GUI_OUT="${3:-}"' in text
        assert 'if [ -n "$GUI_OUT" ]; then\n    bash "$HERE/gui-smoke.sh"' in text

    @pytest.mark.parametrize("scenario", ["", "kde", "GNOME", "plasma-6", "plasma-5", "plasma-new"])
    def test_unknown_scenario_is_refused(self, scenario, tmp_path):
        result = subprocess.run(["bash", SCRIPT, scenario, str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "unknown scenario" in result.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root users")
    def test_refuses_to_run_as_non_root(self, tmp_path):
        result = subprocess.run(["bash", SCRIPT, "gnome", str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "run as root" in result.stderr


class TestReleasedSuite:
    """check_released_suite: skip when the released repository has no suite for the release"""

    FAKE_CURL = r"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/curl_calls"
[ "$FAKE_EXIT" = 0 ] || exit "$FAKE_EXIT"
printf '%s' "$FAKE_CODE"
"""

    def run_function(self, tmp_path, code, curl_exit=0):
        with open(SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("check_released_suite() {")
        function = text[start:text.index("\n}\n", start) + 3]
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(self.FAKE_CURL)
        curl.chmod(0o755)
        script = ('fail() { echo "FAIL: $*" >&2; exit 1; }\nREPO_URL=https://example.org/repo\nCODENAME=oracular\n'
                  + function + 'check_released_suite\necho CONTINUE\n')
        return subprocess.run(
            ["bash", "-c", script],
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_DIR": str(tmp_path), "FAKE_CODE": code,
                 "FAKE_EXIT": str(curl_exit)},
            capture_output=True, text=True, timeout=30,
        )

    def test_a_suite_that_exists_continues(self, tmp_path):
        result = self.run_function(tmp_path, "200")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "CONTINUE\n"
        assert "https://example.org/repo/dists/oracular/Release" in (tmp_path / "curl_calls").read_text()

    def test_a_missing_suite_is_skipped_with_exit_zero(self, tmp_path):
        result = self.run_function(tmp_path, "404")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "SKIP: the released repository has no oracular suite\n"

    @pytest.mark.parametrize("code, curl_exit", [("500", 0), ("503", 0), ("403", 0), ("301", 0), ("000", 6), ("", 28), ("000", 0)])
    def test_any_other_answer_or_a_network_error_fails(self, tmp_path, code, curl_exit):
        result = self.run_function(tmp_path, code, curl_exit)

        assert result.returncode == 1
        assert "FAIL: cannot read" in result.stderr
        assert "SKIP" not in result.stdout and "CONTINUE" not in result.stdout

    def test_the_check_comes_before_the_released_repository_is_added(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            text = handle.read()

        call = text.index("\ncheck_released_suite\n")
        assert text.index("CODENAME=\"$(lsb_release") < call < text.index("gpclient.list")


class TestGuiSmokeScript:
    def test_syntax(self):
        result = subprocess.run(["bash", "-n", GUI_SCRIPT], capture_output=True, text=True)

        assert result.returncode == 0, result.stderr

    def test_strict_mode_and_installs_without_recommends(self):
        with open(GUI_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()

        assert "set -euo pipefail" in text
        assert "--no-install-recommends" in text
        for needed in ("xvfb", "xauth", "imagemagick", "gir1.2-gtk-3.0", "gir1.2-nm-1.0", "gir1.2-gtk-4.0", "python3-pyqt5", "python3-pyqt6"):
            assert needed in text

    @pytest.mark.parametrize("scenario", ["", "kde", "GNOME", "plasma-6", "plasma-5", "plasma-new"])
    def test_unknown_scenario_is_refused(self, scenario, tmp_path):
        result = subprocess.run(["bash", GUI_SCRIPT, scenario, str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "unknown scenario" in result.stderr

    def test_output_directory_is_required(self):
        result = subprocess.run(["bash", GUI_SCRIPT, "gnome"], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "usage: gui-smoke.sh" in result.stderr

    def test_the_plasma_probe_takes_the_one_plasma_package(self):
        with open(GUI_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()

        assert "PLASMA=network-manager-gpclient-plasma\n" in text
        assert "plasma-5" not in text and "plasma-6" not in text and "plasma-new" not in text

    @pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root users")
    def test_refuses_to_run_as_non_root(self, tmp_path):
        result = subprocess.run(["bash", GUI_SCRIPT, "gnome", str(tmp_path)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "run as root" in result.stderr


class TestEndOfLifeReleases:
    """Ubuntu releases past their end of life are served by old-releases.ubuntu.com"""

    CODENAMES = {"22.04": "jammy", "24.04": "noble", "24.10": "oracular", "26.04": "resolute"}

    def script_text(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            return handle.read()

    def eol_in_script(self):
        match = re.search(r'^EOL_CODENAMES="([^"]*)"$', self.script_text(), re.M)
        return set(match.group(1).split())

    def eol_in_dockerfiles(self):
        found = set()
        for version, codename in self.CODENAMES.items():
            with open(os.path.join(ROOT, f"Dockerfile.ubuntu{version}"), encoding="utf-8") as handle:
                text = handle.read()
            pattern = r'echo "([A-Za-z0-9+/=]+)" \| base64 -d > /etc/apt/sources\.list\.d/ubuntu\.sources'
            for blob in re.findall(pattern, text):
                if "old-releases.ubuntu.com" in base64.b64decode(blob).decode():
                    found.add(codename)
        return found

    def test_the_set_matches_the_dockerfiles(self):
        assert self.eol_in_script() == self.eol_in_dockerfiles()
        assert "oracular" in self.eol_in_script()

    def test_a_current_release_is_not_in_the_set(self):
        assert not self.eol_in_script() & {"jammy", "noble", "resolute"}

    def test_sources_are_replaced_before_the_first_apt_get_update(self):
        text = self.script_text()

        assert text.index("old-releases.ubuntu.com/ubuntu/") < text.index("apt-get update")

    def run_block(self, tmp_path, codename):
        """Run the EOL block with /etc/apt redirected into tmp_path"""
        text = self.script_text()
        block = text[text.index("EOL_CODENAMES="):text.index('echo "::group::Prepare')]
        block = block.replace('$(. /etc/os-release && echo "${VERSION_CODENAME:-}")', codename)
        block = block.replace("/etc/apt/", f"{tmp_path}/")
        (tmp_path / "sources.list.d").mkdir()
        (tmp_path / "sources.list").write_text("deb http://archive.ubuntu.com/ubuntu x main\n")
        subprocess.run(["bash", "-c", block], check=True, capture_output=True)

    def test_oracular_gets_the_old_releases_archive(self, tmp_path):
        self.run_block(tmp_path, "oracular")

        sources = (tmp_path / "sources.list.d" / "ubuntu.sources").read_text()
        assert "URIs: http://old-releases.ubuntu.com/ubuntu/\n" in sources
        assert "Suites: oracular oracular-updates oracular-backports oracular-security\n" in sources
        assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n" in sources
        assert (tmp_path / "sources.list").read_text() == ""

    @pytest.mark.parametrize("codename", ["jammy", "noble", "resolute", "oracular2", "x"])
    def test_other_releases_keep_their_sources(self, tmp_path, codename):
        self.run_block(tmp_path, codename)

        assert not (tmp_path / "sources.list.d" / "ubuntu.sources").exists()
        assert "archive.ubuntu.com" in (tmp_path / "sources.list").read_text()

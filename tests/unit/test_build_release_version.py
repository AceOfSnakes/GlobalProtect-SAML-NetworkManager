"""
Tests for the version of the packages that .github/workflows/build-release.yml
builds. A release build is versioned <version>~<codename>1 (1.4.2-1~noble1); a
pull request's build adds +pr<PR>.<run> (1.4.2-1~noble1+pr24.57), which sorts
above the release, so that installing it over the released version is an
upgrade for apt and not a no-op.

The steps are taken out of the workflow file and run as they are: docker,
dpkg-deb, dpkg-parsechangelog and dpkg-buildpackage are fakes found through PATH,
nothing is built. Every positive test has a negative counterpart: a release
build gets no suffix, and a bad number or a wrong version stops the build.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import subprocess
import sys

import pytest
import workflow_steps

CODENAME = "noble"
RELEASE_VERSION = "1.4.2-1"


def step_script(name, substitutions):
    return workflow_steps.step_script("build-release.yml", name, substitutions)


FAKE_DOCKER = r"""#!/bin/bash
# Records `docker run`: one argument per NUL-terminated entry
case "$1" in
    build) echo "$*" >> "$FAKE_DIR/docker_build" ;;
    run) printf '%s\0' "$@" > "$FAKE_DIR/docker_run" ;;
esac
"""

FAKE_PARSECHANGELOG = r"""#!/bin/bash
sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' debian/changelog
"""

FAKE_BUILDPACKAGE = r"""#!/bin/bash
echo "built" >> "$FAKE_DIR/buildpackage"
"""

# cp of the packages from /build has nothing to copy here
FAKE_CP = r"""#!/bin/bash
for arg in "$@"; do
    case "$arg" in /build/*|/output/*) exit 0 ;; esac
done
exec /bin/cp "$@"
"""

# dpkg-deb -f <file> <Field>: the value from <file>.fields ("Field: value" lines)
FAKE_DPKG_DEB = r"""#!/bin/bash
[ "$1" = "-f" ] || exit 2
sed -n "s/^$3: //p" "$2.fields"
"""


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.work = tmp_path / "work"
        for directory in (self.fake, self.bin, self.work):
            directory.mkdir()
        for name, body in (
            ("docker", FAKE_DOCKER), ("dpkg-parsechangelog", FAKE_PARSECHANGELOG),
            ("dpkg-buildpackage", FAKE_BUILDPACKAGE), ("cp", FAKE_CP), ("dpkg-deb", FAKE_DPKG_DEB),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.os_release = tmp_path / "os-release"
        self.os_release.write_text(f"ID=ubuntu\nVERSION_CODENAME={CODENAME}\n")
        (self.work / "debian").mkdir()
        (self.work / "debian" / "control.ubuntu24.04").write_text("Source: x\n")

    def env(self, event, pr, run, **extra):
        env = {
            "PATH": f"{self.bin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin",
            "HOME": str(self.root),
            "FAKE_DIR": str(self.fake),
            "EVENT_NAME": event,
            "PR_NUMBER": pr,
            "RUN_NUMBER": run,
        }
        env.update(extra)
        return env

    # --- the build step

    def build(self, event, pr="", run="57"):
        """Run the "Build Debian packages" step, then the script it hands to docker."""
        script = step_script("Build Debian packages", {
            "${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64",
            "${{ github.workspace }}": str(self.work),
        })
        step = subprocess.run(
            ["bash", "-c", script], cwd=self.work, env=self.env(event, pr, run),
            capture_output=True, text=True, timeout=60,
        )
        result = {"step": step, "version": None, "changelog": None}
        docker_run = self.fake / "docker_run"
        if step.returncode != 0 or not docker_run.exists():
            return result
        args = docker_run.read_bytes().decode().split("\0")[:-1]
        # docker run --rm -e PR_SUFFIX=<..> -v <..> <image> bash -c <script>
        result["docker_args"] = args
        suffix = [a for a in args if a.startswith("PR_SUFFIX=")]
        assert len(suffix) == 1, args
        inner = args[-1]
        inner = inner.replace("/etc/os-release", str(self.os_release))
        (self.work / "debian" / "changelog").write_text(
            f"network-manager-gpclient ({RELEASE_VERSION}) unstable; urgency=medium\n\n  * x\n"
        )
        run = subprocess.run(
            ["bash", "-c", inner], cwd=self.work, env=self.env(event, pr, run, PR_SUFFIX=suffix[0].split("=", 1)[1]),
            capture_output=True, text=True, timeout=60,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        result["version"] = re.search(
            r"Building version (\S+) for", run.stdout
        ).group(1)
        result["changelog"] = (self.work / "debian" / "changelog").read_text().splitlines()[0]
        return result

    # --- the verify step

    def verify(self, event, version, pr="", run="57", arch="amd64"):
        script = step_script("Verify .deb files exist", {"${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64"})
        out = self.work / "output" / "ubuntu24.04-amd64"
        out.mkdir(parents=True, exist_ok=True)
        deb = out / "network-manager-gpclient_x_amd64.deb"
        deb.write_text("deb")
        (out / (deb.name + ".fields")).write_text(f"Version: {version}\nArchitecture: {arch}\n")
        return subprocess.run(
            ["bash", "-c", script], cwd=self.work, env=self.env(event, pr, run), capture_output=True, text=True, timeout=60
        )


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


class TestBuildVersion:
    def test_a_pr_build_gets_the_pr_number_and_the_run_number_after_the_codename(self, box):
        result = box.build("pull_request", pr="24", run="57")

        assert result["version"] == f"{RELEASE_VERSION}~{CODENAME}1+pr24.57"
        assert result["changelog"].startswith(f"network-manager-gpclient ({RELEASE_VERSION}~{CODENAME}1+pr24.57)")
        assert "PR_SUFFIX=+pr24.57" in result["docker_args"]

    @pytest.mark.parametrize("pr, run", [("1", "1"), ("9999999", "4294967296"), ("24", "1234")])
    def test_the_numbers_are_taken_as_they_are(self, box, pr, run):
        result = box.build("pull_request", pr=pr, run=run)

        assert result["version"] == f"{RELEASE_VERSION}~{CODENAME}1+pr{pr}.{run}"

    @pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
    @pytest.mark.parametrize("pr", ["", "24"])
    def test_other_builds_keep_the_release_version(self, box, event, pr):
        result = box.build(event, pr=pr, run="57")

        assert result["version"] == f"{RELEASE_VERSION}~{CODENAME}1"
        assert "+pr" not in result["changelog"]
        assert "PR_SUFFIX=" in result["docker_args"]

    @pytest.mark.parametrize("pr", ["", "abc", "0", "007", "-1", "1;touch injected", "$(touch injected)", "`touch injected`",
                                    "12345678", "24\n", "2 4"])
    def test_a_pr_build_with_a_bad_pr_number_stops_before_docker_runs(self, box, pr):
        result = box.build("pull_request", pr=pr, run="57")

        assert result["step"].returncode != 0
        assert "unexpected pull request or run number" in result["step"].stdout
        assert not (box.fake / "docker_run").exists()
        assert not (box.work / "injected").exists()

    @pytest.mark.parametrize("run", ["", "abc", "0", "057", "5;touch injected", "$(touch injected)", "12345678901", "1\n"])
    def test_a_pr_build_with_a_bad_run_number_stops_before_docker_runs(self, box, run):
        result = box.build("pull_request", pr="24", run=run)

        assert result["step"].returncode != 0
        assert not (box.fake / "docker_run").exists()
        assert not (box.work / "injected").exists()

    @pytest.mark.parametrize("pr", ["abc", "$(touch injected)", ""])
    def test_a_release_build_ignores_the_pr_number(self, box, pr):
        result = box.build("push", pr=pr, run="57")

        assert result["step"].returncode == 0
        assert result["version"] == f"{RELEASE_VERSION}~{CODENAME}1"

    def test_the_suffix_reaches_the_container_only_as_an_environment_value(self, box):
        result = box.build("pull_request", pr="24", run="57")

        args = result["docker_args"]
        assert args[args.index("-e") + 1] == "PR_SUFFIX=+pr24.57"
        assert "24" not in args[-1].replace("24.04", "")


class TestVersionOrder:
    """apt has to see the PR build as an upgrade of the released one"""

    @staticmethod
    def newer(a, b):
        return subprocess.run(["dpkg", "--compare-versions", a, "gt", b]).returncode == 0

    @pytest.mark.parametrize("codename", ["jammy", "noble", "oracular", "resolute"])
    def test_a_pr_build_is_newer_than_the_release_of_the_same_version(self, codename):
        release = f"1.4.2-1~{codename}1"

        assert self.newer(f"{release}+pr24.57", release)

    def test_a_later_run_is_newer_than_an_earlier_one(self):
        assert self.newer("1.4.2-1~noble1+pr24.58", "1.4.2-1~noble1+pr24.57")
        assert self.newer("1.4.2-1~noble1+pr24.100", "1.4.2-1~noble1+pr24.99")

    def test_the_release_is_not_newer_than_its_pr_build(self):
        assert not self.newer("1.4.2-1~noble1", "1.4.2-1~noble1+pr24.57")

    def test_a_pr_build_is_older_than_the_next_upstream_release(self):
        # once the release that contains the PR is out, apt upgrades to it
        assert not self.newer("1.4.2-1~noble1+pr24.57", "1.4.3-1~noble1")
        assert not self.newer("1.4.2-1~noble1+pr24.57", "1.4.2-2~noble1")

    def test_the_changelog_version_of_the_pr_build_has_no_character_dpkg_refuses(self, box):
        result = box.build("pull_request", pr="24", run="57")

        assert subprocess.run(["dpkg", "--validate-version", result["version"]]).returncode == 0


class TestVerifyStep:
    @pytest.mark.parametrize(
        "event, version, pr, run",
        [
            ("pull_request", "1.4.2-1~noble1+pr24.57", "24", "57"),
            ("pull_request", "1.4.2-1~resolute1+pr1.1234567", "1", "1234567"),
            ("push", "1.4.2-1~noble1", "", "57"),
            ("workflow_dispatch", "1.4.2-1~oracular1", "", "57"),
        ],
    )
    def test_the_version_a_build_is_supposed_to_have_passes(self, box, event, version, pr, run):
        result = box.verify(event, version, pr=pr, run=run)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Built packages:" in result.stdout

    @pytest.mark.parametrize(
        "event, version, pr, run",
        [
            # a pull request's build without its suffix, or with another PR's or run's
            ("pull_request", "1.4.2-1~noble1", "24", "57"),
            ("pull_request", "1.4.2-1~noble1+pr25.57", "24", "57"),
            ("pull_request", "1.4.2-1~noble1+pr24.58", "24", "57"),
            ("pull_request", "1.4.2-1~noble1+pr24.57.1", "24", "57"),
            ("pull_request", "1.4.2-1~noble1+pr124.57", "24", "57"),
            ("pull_request", "1.4.2-1+pr24.57", "24", "57"),
            ("pull_request", "1.4.2-1~noble+pr24.57", "24", "57"),
            # a release or manual build must never carry the suffix
            ("push", "1.4.2-1~noble1+pr24.57", "", "57"),
            ("workflow_dispatch", "1.4.2-1~noble1+pr24.57", "24", "57"),
            # no codename at all (build-apt-repo.sh could not tell the suite)
            ("push", "1.4.2-1", "", "57"),
            ("pull_request", "1.4.2-1", "24", "57"),
        ],
    )
    def test_any_other_version_fails_the_build(self, box, event, version, pr, run):
        result = box.verify(event, version, pr=pr, run=run)

        assert result.returncode != 0
        assert f"has version '{version}'" in result.stdout

    def test_a_wrong_architecture_still_fails_the_build(self, box):
        result = box.verify("pull_request", "1.4.2-1~noble1+pr24.57", pr="24", arch="arm64")

        assert result.returncode != 0
        assert "expected 'amd64'" in result.stdout

    def test_no_package_at_all_still_fails_the_build(self, box):
        script = step_script("Verify .deb files exist", {"${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64"})

        result = subprocess.run(
            ["bash", "-c", script], cwd=box.work, env=box.env("push", "", "57"), capture_output=True, text=True, timeout=60
        )

        assert result.returncode != 0
        assert "No .deb files produced" in result.stdout

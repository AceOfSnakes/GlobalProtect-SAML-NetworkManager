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
# Records `docker build` (a line each) and `docker run` (docker_run.1, docker_run.2, ... and
# docker_run, the last one): one argument per NUL-terminated entry
case "$1" in
    build) echo "$*" >> "$FAKE_DIR/docker_build" ;;
    run)
        n=$(ls "$FAKE_DIR"/docker_run.* 2>/dev/null | wc -l)
        printf '%s\0' "$@" > "$FAKE_DIR/docker_run.$((n + 1))"
        cp "$FAKE_DIR/docker_run.$((n + 1))" "$FAKE_DIR/docker_run"
        ;;
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

    def add_variant(self, variant, ubuntu="24.04", dockerfile=True, control=True):
        """The files of a build variant of an Ubuntu release (Dockerfile.ubuntu<version>-<variant> and
        debian/control.ubuntu<version>-<variant>)"""
        if dockerfile:
            (self.work / f"Dockerfile.ubuntu{ubuntu}-{variant}").write_text("FROM scratch\n")
        if control:
            (self.work / "debian" / f"control.ubuntu{ubuntu}-{variant}").write_text(f"Source: {variant}\n")

    def container(self, args, event, pr, run):
        """Run the script that one `docker run` hands to its container, in a fresh copy of the sources:
        {docker_args, version, changelog, control}"""
        # docker run --rm -e PR_SUFFIX=<..> -e CONTROL=<..> -v <..> <image> bash -c <script>
        environment = {a.split("=", 1)[0]: a.split("=", 1)[1] for a, b in zip(args[1:], args) if b == "-e"}
        (self.work / "debian" / "changelog").write_text(
            f"network-manager-gpclient ({RELEASE_VERSION}) unstable; urgency=medium\n\n  * x\n"
        )
        (self.work / "debian" / "control").unlink(missing_ok=True)
        inner = args[-1].replace("/etc/os-release", str(self.os_release))
        completed = subprocess.run(
            ["bash", "-c", inner], cwd=self.work, env=self.env(event, pr, run, **environment),
            capture_output=True, text=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return {
            "docker_args": args,
            "version": re.search(r"Building version (\S+) for", completed.stdout).group(1),
            "changelog": (self.work / "debian" / "changelog").read_text().splitlines()[0],
            "control": (self.work / "debian" / "control").read_text(),
        }

    def build(self, event, pr="", run="57", arch="amd64"):
        """Run the "Build Debian packages" step, then the script each `docker run` of it hands to its
        container (result["runs"]; the first one is also in the keys of the result itself)."""
        script = step_script("Build Debian packages", {
            "${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": arch,
            "${{ github.workspace }}": str(self.work),
        })
        step = subprocess.run(
            ["bash", "-c", script], cwd=self.work, env=self.env(event, pr, run),
            capture_output=True, text=True, timeout=60,
        )
        result = {"step": step, "version": None, "changelog": None, "runs": []}
        docker_runs = sorted(self.fake.glob("docker_run.*"), key=lambda path: int(path.suffix[1:]))
        if step.returncode != 0 or not docker_runs:
            return result
        for path in docker_runs:
            result["runs"].append(self.container(path.read_bytes().decode().split("\0")[:-1], event, pr, run))
        result.update(result["runs"][0])
        return result

    def builds(self):
        """The `docker build` command lines of the step"""
        path = self.fake / "docker_build"
        return path.read_text().splitlines() if path.exists() else []

    # --- the verify step

    def verify(self, event, version, pr="", run="57", arch="amd64", extra=()):
        """Run the "Verify .deb files exist" step on a directory with the package
        network-manager-gpclient_x_amd64.deb of this version and architecture, and the `extra` ones
        (file name, version, architecture)"""
        script = step_script("Verify .deb files exist", {"${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64"})
        out = self.work / "output" / "ubuntu24.04-amd64"
        out.mkdir(parents=True, exist_ok=True)
        for name, deb_version, deb_arch in [("network-manager-gpclient_x_amd64.deb", version, arch), *extra]:
            deb = out / name
            deb.write_text("deb")
            (out / (deb.name + ".fields")).write_text(f"Version: {deb_version}\nArchitecture: {deb_arch}\n")
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


MAIN_CONTROL = "debian/control.ubuntu24.04"
NEON_CONTROL = "debian/control.ubuntu24.04-neon"
NEON_IMAGE = "gpclient-builder:ubuntu24.04-neon"


def mounts(args):
    """The -v arguments of a docker run"""
    return [args[i + 1] for i, a in enumerate(args) if a == "-v"]


def environment(args):
    """The -e arguments of a docker run as a dict"""
    return dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-e")


class TestBuildVariants:
    """Dockerfile.ubuntu<version>-<variant> with debian/control.ubuntu<version>-<variant> builds more
    packages of the release (KDE neon for Ubuntu 24.04) into the same directory"""

    def test_a_release_without_a_variant_is_built_once(self, box):
        result = box.build("push")

        assert len(result["runs"]) == 1
        assert environment(result["docker_args"])["CONTROL"] == MAIN_CONTROL
        assert box.builds() == ["build -t gpclient-builder:ubuntu24.04 -f Dockerfile.ubuntu24.04 ."]

    def test_a_variant_with_both_files_is_built_after_the_main_build(self, box):
        box.add_variant("neon")

        result = box.build("push")

        assert [environment(r["docker_args"])["CONTROL"] for r in result["runs"]] == [MAIN_CONTROL, NEON_CONTROL]
        assert box.builds() == [
            "build -t gpclient-builder:ubuntu24.04 -f Dockerfile.ubuntu24.04 .",
            f"build -t {NEON_IMAGE} -f Dockerfile.ubuntu24.04-neon .",
        ]
        assert result["runs"][1]["docker_args"][-4] == NEON_IMAGE
        # the control file of the variant is the one that is built with
        assert result["runs"][0]["control"] == "Source: x\n"
        assert result["runs"][1]["control"] == "Source: neon\n"

    @pytest.mark.parametrize("event, pr, suffix", [
        ("pull_request", "24", "+pr24.57"), ("push", "", ""), ("workflow_dispatch", "", ""),
    ])
    def test_a_variant_gets_the_version_of_the_main_build(self, box, event, pr, suffix):
        box.add_variant("neon")

        result = box.build(event, pr=pr, run="57")

        versions = [r["version"] for r in result["runs"]]
        assert versions == [f"{RELEASE_VERSION}~{CODENAME}1{suffix}"] * 2
        for run in result["runs"]:
            assert run["changelog"].startswith(f"network-manager-gpclient ({versions[0]})")
            assert environment(run["docker_args"])["PR_SUFFIX"] == suffix

    def test_a_variant_writes_to_the_output_directory_of_the_main_build(self, box):
        box.add_variant("neon")

        result = box.build("push")

        outputs = [[m for m in mounts(r["docker_args"]) if m.endswith(":/output")] for r in result["runs"]]
        assert outputs[0] == outputs[1] == [f"{box.work}/output/ubuntu24.04-amd64:/output"]

    @pytest.mark.parametrize("files", [{"dockerfile": False}, {"control": False}])
    def test_a_variant_without_one_of_its_files_is_not_built(self, box, files):
        box.add_variant("neon", **files)

        result = box.build("push")

        assert result["step"].returncode == 0
        assert len(result["runs"]) == 1
        assert box.builds() == ["build -t gpclient-builder:ubuntu24.04 -f Dockerfile.ubuntu24.04 ."]
        assert "::warning::variant neon of Ubuntu 24.04 needs both" in result["step"].stdout

    def test_a_variant_of_another_release_is_not_built(self, box):
        box.add_variant("neon", ubuntu="24.10")
        box.add_variant("neon", ubuntu="22.04")

        result = box.build("push")

        assert len(result["runs"]) == 1
        assert NEON_IMAGE not in " ".join(box.builds())

    def test_the_neon_variant_is_built_for_amd64_only(self, box):
        box.add_variant("neon")

        result = box.build("push", arch="arm64")

        assert result["step"].returncode == 0
        assert [environment(r["docker_args"])["CONTROL"] for r in result["runs"]] == [MAIN_CONTROL]
        assert box.builds() == ["build -t gpclient-builder:ubuntu24.04 -f Dockerfile.ubuntu24.04 ."]
        assert "Variant neon is built for amd64 only, not for arm64" in result["step"].stdout

    def test_the_main_build_still_runs_for_arm64_when_the_variant_has_no_arm64(self, box):
        box.add_variant("neon")

        result = box.build("pull_request", pr="24", arch="arm64")

        assert result["runs"][0]["version"] == f"{RELEASE_VERSION}~{CODENAME}1+pr24.57"

    @pytest.mark.parametrize("variant", ["other", "x1"])
    def test_a_variant_without_a_list_of_architectures_fails_the_step(self, box, variant):
        box.add_variant(variant)

        result = box.build("push")

        assert result["step"].returncode != 0
        assert f"variant {variant} has no list of architectures" in result["step"].stdout
        assert len(box.builds()) == 1

    @pytest.mark.parametrize("variant", ["Neon", "ne_on", "ne on", "$(touch injected)", "a;touch injected", "-x", "ne.on"])
    def test_a_variant_with_an_unexpected_name_fails_the_step(self, box, variant):
        box.add_variant(variant)

        result = box.build("push")

        assert result["step"].returncode != 0
        assert "unexpected name of a build variant" in result["step"].stdout
        assert not (box.work / "injected").exists()
        assert len(box.builds()) == 1

    def test_a_failing_variant_build_fails_the_step(self, box):
        box.add_variant("neon")
        failing = box.bin / "docker"
        failing.write_text(failing.read_text().replace("    build)", "    build) case \"$*\" in *neon*) exit 1 ;; esac ;;\n    build_)", 1))

        result = box.build("push")

        assert result["step"].returncode != 0
        assert len(list(box.fake.glob("docker_run.*"))) == 1

    def test_every_variant_of_the_repository_has_both_files_and_is_known_to_the_workflow(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = step_script("Build Debian packages", {
            "${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64", "${{ github.workspace }}": "/w"})
        found = 0
        for name in sorted(os.listdir(root)):
            match = re.fullmatch(r"Dockerfile\.ubuntu([0-9.]+)-([a-z0-9]+)", name)
            if not match:
                continue
            found += 1
            ubuntu, variant = match.groups()
            assert os.path.isfile(os.path.join(root, "debian", f"control.ubuntu{ubuntu}-{variant}")), name
            assert re.search(rf"^\s+{variant}\) echo [a-z0-9 ]+ ;;$", script, re.M), name
        assert found

    def test_the_neon_variant_does_not_list_arm64(self):
        script = step_script("Build Debian packages", {
            "${{ matrix.ubuntu }}": "24.04", "${{ matrix.arch }}": "amd64", "${{ github.workspace }}": "/w"})
        line = re.search(r"^\s+neon\) echo (.*) ;;$", script, re.M).group(1)
        assert line == "amd64"


class TestVerifyStepVariants:
    NEON = "network-manager-gpclient-plasma-6_1.5.0-1~noble1_amd64.deb"

    @pytest.mark.parametrize("event, version, pr", [
        ("push", "1.5.0-1~noble1", ""), ("pull_request", "1.5.0-1~noble1+pr31.57", "31"),
    ])
    def test_the_package_of_a_variant_passes_with_the_version_of_the_build(self, box, event, version, pr):
        result = box.verify(event, version, pr=pr, extra=[(self.NEON, version, "amd64")])

        assert result.returncode == 0, result.stdout + result.stderr
        assert "plasma-6" in result.stdout

    @pytest.mark.parametrize("version, arch, message", [
        ("1.5.0-1~noble1", "arm64", "expected 'amd64'"),
        ("1.5.0-1", "amd64", "has version '1.5.0-1'"),
        ("1.5.0-1~noble1+pr31.57", "amd64", "has version '1.5.0-1~noble1+pr31.57'"),
    ])
    def test_a_wrong_package_of_a_variant_fails_the_build(self, box, version, arch, message):
        result = box.verify("push", "1.5.0-1~noble1", extra=[(self.NEON, version, arch)])

        assert result.returncode != 0
        assert message in result.stdout

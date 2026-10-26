"""
Tests for the hardening of the release and build scripts: publish-apt.yml (a failing download
fails the job), build-release.yml (release jobs run for tag pushes only), build-all.sh, the
Makefile `install` target and the systemd enable symlink removal of debian/network-manager-gpclient.postinst.
(upgrade_possible is covered in test_upgrade_test_workflow.py.)

Every positive test has a negative counterpart. External tools are fakes on PATH.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import shutil
import subprocess

import pytest

from workflow_steps import read_workflow, step_script

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POSTINST = os.path.join(ROOT, "debian", "network-manager-gpclient.postinst")
SAFE_PATH = "/usr/bin:/bin"


def fake_bin(tmp_path, tools):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in tools.items():
        (bin_dir / name).write_text("#!/bin/bash\n" + body)
        (bin_dir / name).chmod(0o755)
    return bin_dir


# --- publish-apt.yml ---------------------------------------------------------

FAKE_GH = r"""echo "$*" >> "$FAKE_DIR/gh_calls"
case "$1 $2" in
    "api --paginate") printf '%s\n' v2 v1 ;;
    "release view") [ "$3" = "$FAKE_VIEW_FAIL" ] && exit 1; cat "$FAKE_DIR/assets_$3" ;;
    "release download")
        [ "$3" = "$FAKE_DL_FAIL" ] && exit 1
        echo "downloaded $3 $7" >> "$FAKE_DIR/downloads"
        # a file per pattern, in the directory given with -D
        [ "$7" = SHA256SUMS ] && : > "$5/SHA256SUMS" || : > "$5/$3-${7#\*.}.${7#\*.}" ;;
esac
"""


# The real script has tests of its own (test_verify_release_assets.py): here it records its arguments
FAKE_VERIFY = """echo "$1 $2" >> "$FAKE_DIR/verified"
[ "$1" = "$FAKE_VERIFY_FAIL" ] && exit 1
exit 0
"""


def run_download(tmp_path, assets, dl_fail="", view_fail="", verify_fail=""):
    for tag, names in assets.items():
        (tmp_path / f"assets_{tag}").write_text("".join(n + "\n" for n in names))
    env = {"PATH": f"{fake_bin(tmp_path, {'gh': FAKE_GH})}:{SAFE_PATH}", "FAKE_DIR": str(tmp_path),
           "FAKE_DL_FAIL": dl_fail, "FAKE_VIEW_FAIL": view_fail, "FAKE_VERIFY_FAIL": verify_fail,
           "GITHUB_REPOSITORY": "o/r"}
    script = step_script("publish-apt.yml", "Download packages from every published release")
    work = tmp_path / "work"
    (work / ".github" / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts" / "verify-release-assets.sh").write_text(FAKE_VERIFY)
    # GitHub Actions runs a step without `shell:` as `bash -e {0}`
    result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True,
                            text=True, timeout=30)
    downloads = (tmp_path / "downloads").read_text().splitlines() if (tmp_path / "downloads").exists() else []
    return result, downloads


class TestPublishAptDownload:
    ASSETS = {"v2": ["a_1_amd64.deb", "a.rpm", "SHA256SUMS"], "v1": ["b_1_amd64.deb"]}

    def test_debs_and_rpms_are_downloaded_only_when_the_release_has_them(self, tmp_path):
        result, downloads = run_download(tmp_path, self.ASSETS)

        assert result.returncode == 0, result.stderr
        assert downloads == ["downloaded v2 *.deb", "downloaded v2 *.rpm", "downloaded v2 SHA256SUMS",
                             "downloaded v1 *.deb"]
        assert "::warning::v1 has no .rpm assets" in result.stdout
        assert "no .deb assets" not in result.stdout

    def test_a_release_without_debs_and_rpms_only_warns(self, tmp_path):
        result, downloads = run_download(tmp_path, {"v2": ["notes.txt"], "v1": ["b.deb"]})

        assert result.returncode == 0, result.stderr
        assert downloads == ["downloaded v1 *.deb"]
        assert "::warning::v2 has no .deb assets" in result.stdout
        assert "::warning::v2 has no .rpm assets" in result.stdout

    @pytest.mark.parametrize("failing", ["v2", "v1"])
    def test_a_failing_download_fails_the_job(self, tmp_path, failing):
        result, _ = run_download(tmp_path, self.ASSETS, dl_fail=failing)

        assert result.returncode != 0
        assert "ls -la" not in result.stdout

    def test_a_failing_asset_listing_fails_the_job(self, tmp_path):
        result, downloads = run_download(tmp_path, self.ASSETS, view_fail="v2")

        assert result.returncode != 0
        assert downloads == []

    def test_every_release_is_verified_before_its_packages_are_used(self, tmp_path):
        result, _ = run_download(tmp_path, self.ASSETS)

        assert result.returncode == 0, result.stderr
        assert (tmp_path / "verified").read_text().splitlines() == ["v2 verify/v2", "v1 verify/v1"]
        assert sorted(os.listdir(tmp_path / "work" / "incoming")) == ["v1-deb.deb", "v2-deb.deb", "v2-rpm.rpm"]

    @pytest.mark.parametrize("failing", ["v2", "v1"])
    def test_a_failed_verification_fails_the_job_without_the_packages_of_the_release(self, tmp_path, failing):
        result, _ = run_download(tmp_path, self.ASSETS, verify_fail=failing)

        assert result.returncode != 0
        assert "ls -la" not in result.stdout
        assert not any(name.startswith(failing + "-") for name in os.listdir(tmp_path / "work" / "incoming"))

    def test_the_download_has_no_error_swallowing_fallback(self):
        script = step_script("publish-apt.yml", "Download packages from every published release")
        assert "|| echo" not in script


# --- build-release.yml -------------------------------------------------------

@pytest.mark.parametrize("job", ["sbom", "release", "release-notes", "publish-apt"])
def test_release_jobs_run_for_tag_pushes_only(job):
    text = read_workflow("build-release.yml")
    block = text[text.index(f"\n  {job}:\n"):]
    condition = re.search(r"^    if: (.*)$", block, re.M).group(1)

    assert condition == "startsWith(github.ref, 'refs/tags/') && github.event_name == 'push'"


def test_only_the_release_jobs_check_the_tag():
    # a workflow_dispatch run on a tag must not reach them: no other job needs the check
    text = read_workflow("build-release.yml")
    assert text.count("github.event_name == 'push'") >= 4
    assert text.count("startsWith(github.ref, 'refs/tags/')") == 4


# --- build-all.sh ------------------------------------------------------------

FAKE_DOCKER = r"""echo "$*" >> "$FAKE_DIR/docker_calls"
if [ "$1" = run ]; then eval "last=\${$#}"; printf '%s' "$last" > "$FAKE_DIR/run_command"; fi
exit 0
"""
FAKE_FAKEROOT = r"""[ -z "$FAKE_BUILD_FAIL" ] || exit 2
touch ../new.deb
[ -n "$FAKE_NO_DDEB" ] || touch ../new.ddeb
"""


def run_build_all(tmp_path, target="24.04", suffix=None, files=("24.04",), arch="amd64", cwd=None):
    """files: the targets that have a Dockerfile and a control file in the fake tree"""
    shutil.copy(os.path.join(ROOT, "build-all.sh"), tmp_path / "build-all.sh")
    (tmp_path / "debian").mkdir(exist_ok=True)
    for name in files:
        shutil.copy(os.path.join(ROOT, f"Dockerfile.ubuntu{name}"), tmp_path / f"Dockerfile.ubuntu{name}")
        shutil.copy(os.path.join(ROOT, "debian", f"control.ubuntu{name}"), tmp_path / "debian")
    out = tmp_path / "output" / "ubuntu24.04"
    out.mkdir(parents=True)
    (out / "stale.deb").write_text("old")
    (out / "stale.ddeb").write_text("old")
    tools = {"docker": FAKE_DOCKER}
    if arch == "uname-aarch64":  # no dpkg: fall back to uname -m
        tools["dpkg"] = "exit 127"
        tools["uname"] = "echo aarch64"
    else:
        tools["dpkg"] = f"echo {arch}"
    env = {"PATH": f"{fake_bin(tmp_path, tools)}:{SAFE_PATH}", "FAKE_DIR": str(tmp_path)}
    if suffix is not None:
        env["VERSION_SUFFIX"] = suffix
    result = subprocess.run(["bash", str(tmp_path / "build-all.sh"), *([target] if target else [])], env=env,
                            cwd=cwd, capture_output=True, text=True, timeout=30)
    return result, out


def run_container_command(tmp_path, out, **fake_env):
    """Run the command that build-all.sh gives the container, in a fake source tree"""
    source = tmp_path / "ctr" / "build" / "source"
    (source / "debian").mkdir(parents=True, exist_ok=True)
    (source / "debian" / "control.ubuntu24.04").write_text("x")
    command = (tmp_path / "run_command").read_text().replace("/output/", f"{out}/")
    env = {"PATH": f"{fake_bin(tmp_path, {'fakeroot': FAKE_FAKEROOT, 'dpkg-buildpackage': 'exit 0'})}:{SAFE_PATH}",
           "PR_SUFFIX": "", **fake_env}
    return subprocess.run(["bash", "-c", command], cwd=source, env=env, capture_output=True, text=True, timeout=30)


class TestBuildAll:
    def test_the_old_packages_are_removed_before_the_build(self, tmp_path):
        result, out = run_build_all(tmp_path)

        assert result.returncode == 0, result.stderr
        assert list(out.iterdir()) == []

    def test_the_target_cache_is_mounted_in_the_source_tree_of_the_container(self, tmp_path):
        run_build_all(tmp_path)
        run_line = next(c for c in (tmp_path / "docker_calls").read_text().splitlines() if c.startswith("run "))

        assert "gpclient-target-cache-24.04:/build/source/external/GlobalProtect-openconnect/target" in run_line
        assert ":/build/external/GlobalProtect-openconnect/target" not in run_line

    def test_every_dockerfile_builds_in_build_source(self):
        for name in os.listdir(ROOT):
            if name.startswith("Dockerfile.ubuntu"):
                with open(os.path.join(ROOT, name)) as handle:
                    assert re.search(r"^WORKDIR /build/source$", handle.read(), re.M), name

    def test_a_successful_build_copies_the_debs_and_the_ddebs(self, tmp_path):
        _, out = run_build_all(tmp_path)
        result = run_container_command(tmp_path, out, FAKE_DIR=str(tmp_path))
        # ../*.deb is /ctr/build/*.deb: the fake fakeroot wrote it there

        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in out.iterdir()) == ["new.ddeb", "new.deb"]

    def test_a_missing_ddeb_does_not_fail_the_build(self, tmp_path):
        _, out = run_build_all(tmp_path)
        result = run_container_command(tmp_path, out, FAKE_NO_DDEB="1")

        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in out.iterdir()) == ["new.deb"]

    def test_a_failing_build_fails_the_script_and_copies_nothing(self, tmp_path):
        _, out = run_build_all(tmp_path)
        result = run_container_command(tmp_path, out, FAKE_BUILD_FAIL="1")

        assert result.returncode != 0
        assert list(out.iterdir()) == []

    def test_a_build_without_any_deb_fails_the_script(self, tmp_path):
        _, out = run_build_all(tmp_path)
        source = tmp_path / "ctr" / "build" / "source"
        (source / "debian").mkdir(parents=True)
        (source / "debian" / "control.ubuntu24.04").write_text("x")
        command = (tmp_path / "run_command").read_text().replace("/output/", f"{out}/")
        # dpkg-buildpackage "succeeds" and writes no package
        env = {"PATH": f"{fake_bin(tmp_path, {'fakeroot': 'exit 0'})}:{SAFE_PATH}"}
        result = subprocess.run(["bash", "-c", command], cwd=source, env=env, capture_output=True, text=True)

        assert result.returncode != 0

    def test_the_dockerfile_path_does_not_depend_on_the_current_directory(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        result, _ = run_build_all(tmp_path, cwd=other)
        build = next(c for c in (tmp_path / "docker_calls").read_text().splitlines() if c.startswith("build "))
        dockerfile = build.split()[2]

        assert result.returncode == 0, result.stderr
        assert dockerfile == str(tmp_path / "Dockerfile.ubuntu24.04")

    def test_a_variant_builds_from_its_own_dockerfile_and_control_file(self, tmp_path):
        result, _ = run_build_all(tmp_path, "24.04-neon", files=("24.04", "24.04-neon"))
        calls = (tmp_path / "docker_calls").read_text()

        assert result.returncode == 0, result.stderr
        assert f"build -f {tmp_path}/Dockerfile.ubuntu24.04-neon -t gpclient-builder:ubuntu24.04-neon" in calls
        assert "cp debian/control.ubuntu24.04-neon debian/control" in (tmp_path / "run_command").read_text()
        assert "Dockerfile.ubuntu24.04 " not in calls

    def test_building_all_includes_the_variants(self, tmp_path):
        result, _ = run_build_all(tmp_path, None, files=("24.04", "24.04-neon"))
        calls = (tmp_path / "docker_calls").read_text()

        assert result.returncode == 0, result.stderr
        assert "-t gpclient-builder:ubuntu24.04 " in calls
        assert "-t gpclient-builder:ubuntu24.04-neon " in calls

    @pytest.mark.parametrize("target", ["24.04-nope", "20.04", "neon", "24.04-", "../24.04", "24.04 24.04-neon", "22.04 24.04", " 24.04", "24.04 "])
    def test_an_unknown_target_is_rejected_and_nothing_is_built(self, tmp_path, target):
        result, _ = run_build_all(tmp_path, target, files=("24.04", "24.04-neon"))

        assert result.returncode == 1
        assert f"Invalid Ubuntu version '{target}'" in result.stdout
        assert "24.04, 24.04-neon" in result.stdout
        assert not (tmp_path / "docker_calls").exists()

    def test_a_dockerfile_without_a_control_file_is_not_a_target(self, tmp_path):
        shutil.copy(os.path.join(ROOT, "Dockerfile.ubuntu22.04"), tmp_path / "Dockerfile.ubuntu22.04")
        result, _ = run_build_all(tmp_path, "22.04")

        assert result.returncode == 1
        assert not (tmp_path / "docker_calls").exists()

    def test_on_arm64_building_all_skips_the_amd64_only_variant(self, tmp_path):
        result, _ = run_build_all(tmp_path, None, files=("24.04", "24.04-neon"), arch="arm64")
        calls = (tmp_path / "docker_calls").read_text()

        assert result.returncode == 0, result.stderr
        assert "24.04-neon is built for amd64 only, not for arm64: skipped" in result.stdout.replace("Variant ", "")
        assert "-t gpclient-builder:ubuntu24.04 " in calls
        assert "ubuntu24.04-neon" not in calls

    def test_on_arm64_the_amd64_only_variant_chosen_explicitly_is_an_error(self, tmp_path):
        result, _ = run_build_all(tmp_path, "24.04-neon", files=("24.04", "24.04-neon"), arch="arm64")

        assert result.returncode == 1
        assert "built for amd64 only, not for arm64" in result.stdout
        assert not (tmp_path / "docker_calls").exists()

    def test_a_variant_without_an_architecture_field_is_an_error(self, tmp_path):
        result, _ = run_build_all(tmp_path, None, files=("24.04", "24.04-neon"))
        # the first run built; now remove the field and run again
        (tmp_path / "docker_calls").unlink()
        control = tmp_path / "debian" / "control.ubuntu24.04-neon"
        control.write_text(re.sub(r"^Architecture:.*\n", "", control.read_text(), flags=re.M))
        result = subprocess.run(["bash", str(tmp_path / "build-all.sh")], capture_output=True, text=True,
                                env={"PATH": f"{tmp_path / 'bin'}:{SAFE_PATH}", "FAKE_DIR": str(tmp_path)})

        assert result.returncode == 1
        assert "debian/control.ubuntu24.04-neon" in result.stdout
        assert not (tmp_path / "docker_calls").exists()

    def test_on_amd64_the_amd64_only_variant_is_built(self, tmp_path):
        result, _ = run_build_all(tmp_path, None, files=("24.04", "24.04-neon"), arch="amd64")

        assert result.returncode == 0, result.stderr
        assert "-t gpclient-builder:ubuntu24.04-neon " in (tmp_path / "docker_calls").read_text()

    def test_without_dpkg_the_architecture_comes_from_uname(self, tmp_path):
        result, _ = run_build_all(tmp_path, "24.04-neon", files=("24.04", "24.04-neon"), arch="uname-aarch64")

        assert result.returncode == 1
        assert "not for arm64" in result.stdout

    def test_the_suffix_is_added_to_the_version_in_the_container(self, tmp_path):
        _, out = run_build_all(tmp_path, suffix="+pr54.7")
        assert "PR_SUFFIX=+pr54.7" in (tmp_path / "docker_calls").read_text()
        source = tmp_path / "ctr" / "build" / "source"
        (source / "debian").mkdir(parents=True)
        (source / "debian" / "changelog").write_text("pkg (1.4.2-1) noble; urgency=medium\n")
        (tmp_path / "osr").write_text("VERSION_CODENAME=noble\n")
        command = (tmp_path / "run_command").read_text().replace("/etc/os-release", str(tmp_path / "osr"))
        (tmp_path / "run_command").write_text(command)
        result = run_container_command(tmp_path, out, PR_SUFFIX="+pr54.7")

        assert result.returncode == 0, result.stderr
        assert (source / "debian" / "changelog").read_text().startswith("pkg (1.4.2-1~noble1+pr54.7) noble")

    def test_without_a_suffix_the_changelog_is_not_touched(self, tmp_path):
        _, out = run_build_all(tmp_path)
        assert "PR_SUFFIX= " in (tmp_path / "docker_calls").read_text()
        source = tmp_path / "ctr" / "build" / "source"
        (source / "debian").mkdir(parents=True)
        (source / "debian" / "changelog").write_text("pkg (1.4.2-1) noble; urgency=medium\n")
        result = run_container_command(tmp_path, out)

        assert result.returncode == 0, result.stderr
        assert (source / "debian" / "changelog").read_text().startswith("pkg (1.4.2-1) noble")

    @pytest.mark.parametrize("suffix", ["pr54", "+pr54;id", "+a b", "+$(id)", "+pr/1", "+"])
    def test_a_bad_suffix_is_rejected_and_nothing_is_built(self, tmp_path, suffix):
        result, _ = run_build_all(tmp_path, suffix=suffix)

        assert result.returncode == 1
        assert "Invalid VERSION_SUFFIX" in result.stdout
        assert not (tmp_path / "docker_calls").exists()


# --- clean-cache.sh ----------------------------------------------------------

def shell_scripts_of_makefile():
    with open(os.path.join(ROOT, "Makefile")) as handle:
        block = re.search(r"^SHELL_SCRIPTS := \\\n((?:\t.*\n)+)", handle.read(), re.M).group(1)
    return [line.strip().rstrip("\\").strip() for line in block.splitlines()]


def test_clean_cache_script_is_gone_and_nothing_names_it():
    assert not os.path.exists(os.path.join(ROOT, "clean-cache.sh"))
    assert "clean-cache.sh" not in shell_scripts_of_makefile()
    result = subprocess.run(
        ["grep", "-rIl", "clean-cache", ".", "--exclude-dir=external", "--exclude-dir=.git",
         "--exclude-dir=__pycache__", "--exclude=test_script_hardening.py"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.stdout == ""


def test_every_script_of_shell_scripts_exists():
    missing = [s for s in shell_scripts_of_makefile() if not os.path.exists(os.path.join(ROOT, s))]
    assert missing == []


# --- Makefile install --------------------------------------------------------

CONFIG_INSTALLS = [
    ("config/nm-gpclient.conf", "/usr/share/dbus-1/system.d/nm-gpclient.conf"),
    ("config/org.freedesktop.NetworkManager.gpclient.service",
     "/usr/share/dbus-1/system-services/org.freedesktop.NetworkManager.gpclient.service"),
    ("config/nm-gpclient.service", "/lib/systemd/system/nm-gpclient.service"),
]


def install_recipe(text):
    return text[text.index("\ninstall: all\n"):text.index("\nclean:\n")]


def missing_installs(recipe):
    return [src for src, dst in CONFIG_INSTALLS if f"install -D -m 644 {src} {dst}\n" not in recipe]


class TestMakeInstall:
    def text(self):
        with open(os.path.join(ROOT, "Makefile")) as handle:
            return handle.read()

    def test_install_has_the_dbus_policy_the_activation_file_and_the_systemd_unit(self):
        assert missing_installs(install_recipe(self.text())) == []

    @pytest.mark.parametrize("src, dst", CONFIG_INSTALLS)
    def test_the_check_rejects_a_recipe_without_the_line(self, src, dst):
        recipe = install_recipe(self.text())
        broken = "".join(line for line in recipe.splitlines(True) if f" {src} " not in line)

        assert missing_installs(broken) == [src]

    @pytest.mark.parametrize("src, dst", CONFIG_INSTALLS)
    def test_the_sources_exist_and_install_dev_uses_the_same_files(self, src, dst):
        assert os.path.isfile(os.path.join(ROOT, src))
        assert src in self.text().split("\ninstall-dev:")[1]


# --- postinst: systemd enable symlink, nsswitch.conf -------------------------

def run_postinst(tmp_path, args):
    text = open(POSTINST).read().replace("remove_legacy_edge_wrapper /tmp\n", "")
    script = tmp_path / "postinst"
    script.write_text(text)
    tools = {"dbus-send": "exit 0\n", "pgrep": "exit 1\n", "pkill": "exit 1\n"}
    tools["deb-systemd-helper"] = 'echo "$*" >> "$FAKE_DIR/helper_calls"\n'
    # The real dpkg when there is one (CI); else a fake whose "lt" compares the
    # upstream versions only
    if not shutil.which("dpkg"):
        tools["dpkg"] = ('[ "$1" = --compare-versions ] || exit 2\n'
                         'v=${2%%-*}; [ "$(printf "%s\\n%s\\n" "$v" "${4%~}" | sort -V | head -n1)" = "$v" ] '
                         '&& [ "$v" != "${4%~}" ]\n')
    bin_dir = fake_bin(tmp_path, tools)
    result = subprocess.run(["sh", str(script), *args], capture_output=True, text=True, timeout=30,
                            env={"PATH": f"{bin_dir}:{SAFE_PATH}", "FAKE_DIR": str(tmp_path)})
    calls = tmp_path / "helper_calls"
    return result, calls.read_text() if calls.exists() else ""


class TestPostinstSystemd:
    @pytest.mark.parametrize("old", ["1.5.0-1", "1.4.1-1", "1.5.0-1~noble1", "1.5.0-1~jammy1~pr90"])
    def test_an_upgrade_from_before_2_0_0_disables_the_unit(self, tmp_path, old):
        result, calls = run_postinst(tmp_path, ["configure", old])

        assert result.returncode == 0, result.stderr
        assert calls == "purge nm-gpclient.service\nunmask nm-gpclient.service\n"

    @pytest.mark.parametrize("args", [["configure"], ["configure", "2.0.0-1"], ["configure", "2.1.0-1"],
                                      ["configure", "2.0.0-1~noble1"], ["configure", "2.0.0-1~jammy1~pr96"],
                                      ["reconfigure", "2.0.0-1"]])
    def test_a_first_install_and_a_newer_upgrade_leave_the_unit_alone(self, tmp_path, args):
        result, calls = run_postinst(tmp_path, args)

        assert result.returncode == 0, result.stderr
        assert calls == ""


class TestPostinstNsswitch:
    def test_the_postinst_does_not_mention_nsswitch(self):
        assert "nsswitch" not in open(POSTINST).read()


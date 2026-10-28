"""
Tests for the packaging checks of issue #54: build flags of the GNOME plugins,
the fields lintian reports as errors (Maintainer, python3 dependency of the
auth dialog, ldconfig trigger of the legacy symlink) and the lintian run of the
build workflow.

Every check is a function on the text of a file, so a negative test runs it on a
modified copy that must be rejected. Nothing is built and no tool is run.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import glob
import os
import re
import subprocess

import pytest
import workflow_steps

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONTROLS = sorted(glob.glob(os.path.join(ROOT, "debian", "control*")))
GNOME = "network-manager-gpclient-gnome"


def read(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as handle:
        return handle.read()


def makefile_problems(text):
    """What is wrong with the build flags of plugins/gnome/Makefile"""
    problems = []
    if re.search(r"^CC\s*:?=", text, re.M):
        problems.append("CC is set unconditionally (use CC ?=)")
    if not re.search(r"^CC\s*\?=", text, re.M):
        problems.append("CC is not overridable")
    compiles = [l for l in text.splitlines() if l.startswith("\t$(CC) ")]
    if len(compiles) != 3:
        problems.append("expected 3 compile/link commands, found %d" % len(compiles))
    for line in compiles:
        for flag in ("$(CPPFLAGS)", "$(CFLAGS)", "$(LDFLAGS)"):
            if flag not in line:
                problems.append("%s missing in: %s" % (flag, line.strip()))
    return problems


def maintainer_problems(text):
    """What is wrong with the Maintainer field of a control file"""
    match = re.search(r"^Maintainer:[ \t]*(.*)$", text, re.M)
    if not match:
        return ["no Maintainer field"]
    if not re.fullmatch(r"[^<>@]+ <[^<>\s@]+@[^<>\s@]+\.[A-Za-z]+>", match.group(1).strip()):
        return ["malformed Maintainer: %r" % match.group(1)]
    return []


def python3_problems(text):
    """The GNOME package ships a Python script, so it must depend on python3 itself"""
    stanza = re.search(r"^Package: %s\n.*?(?:\n\n|\Z)" % GNOME, text, re.M | re.S)
    if not stanza:
        return []
    depends = re.search(r"^Depends:((?:.*\n)(?:[ \t].*\n?)*)", stanza.group(0), re.M)
    names = {d.split("(")[0].split("|")[0].strip() for d in (depends.group(1) if depends else "").split(",")}
    # The auth dialog imports gi and Gtk 3.0 too
    return ["%s does not depend on %s" % (GNOME, name)
            for name in ("python3", "python3-gi", "gir1.2-gtk-3.0") if name not in names]


def lintian_problems(step):
    """The build step must fail on a lintian error, after the packages are built"""
    if not re.search(r"^\s*lintian\b[^\n]*--fail-on error\b[^\n]*\.deb", step, re.M):
        return ["no lintian --fail-on error run on the .deb files"]
    build = re.search(r"^\s*dpkg-buildpackage\b", step, re.M)
    if not build or build.start() > re.search(r"^\s*lintian\b", step, re.M).start():
        return ["lintian runs before the packages are built"]
    return []


class TestBuildFlags:
    def test_the_makefile_uses_the_build_flags_and_cc_is_overridable(self):
        assert makefile_problems(read("plugins", "gnome", "Makefile")) == []

    def test_a_hard_coded_cc_is_reported(self):
        text = read("plugins", "gnome", "Makefile")
        assert makefile_problems(text.replace("CC ?= gcc", "CC = gcc")) != []

    @pytest.mark.parametrize("flag", ["$(CPPFLAGS) ", "$(CFLAGS) ", "$(LDFLAGS) "])
    def test_a_makefile_that_ignores_a_flag_is_reported(self, flag):
        text = read("plugins", "gnome", "Makefile")
        assert flag in text
        assert makefile_problems(text.replace(flag, "")) != []

    def test_the_rules_pass_the_dpkg_flags_to_the_gnome_make_and_to_cmake(self):
        rules = read("debian", "rules")
        assert "hardening=+all" in rules
        assert "include /usr/share/dpkg/buildflags.mk" in rules
        make = [l for l in rules.splitlines() if "$(MAKE) gnome-plugins" in l]
        assert len(make) == 1 and all(f in make[0] for f in ("CFLAGS=", "CPPFLAGS=", "LDFLAGS="))
        cmake = [l for l in rules.splitlines() if " cmake .." in l]
        assert len(cmake) == 1 and all(f in cmake[0] for f in ("CXXFLAGS=", "LDFLAGS="))

    def test_the_flags_are_not_exported_to_the_rust_build(self):
        # cargo and the cc crate read CFLAGS and LDFLAGS from the environment
        assert not re.search(r"^export\s+\S*FLAGS", read("debian", "rules"), re.M)


class TestControlFields:
    @pytest.mark.parametrize("path", CONTROLS, ids=os.path.basename)
    def test_the_maintainer_is_a_name_and_an_address(self, path):
        with open(path, encoding="utf-8") as handle:
            assert maintainer_problems(handle.read()) == []

    @pytest.mark.parametrize("bad", ["WMP", "WMP <>", "WMP <wmp>", "<a@b.c>", "WMP a@b.c", "WMP <a@b.c"])
    def test_a_malformed_maintainer_is_reported(self, bad):
        text = read("debian", "control.ubuntu24.04")
        broken = re.sub(r"^Maintainer:.*$", "Maintainer: " + bad, text, flags=re.M)
        assert broken != text
        assert maintainer_problems(broken) != []

    def test_a_missing_maintainer_is_reported(self):
        text = read("debian", "control.ubuntu24.04")
        assert maintainer_problems(re.sub(r"^Maintainer:.*\n", "", text, flags=re.M)) != []

    @pytest.mark.parametrize("path", CONTROLS, ids=os.path.basename)
    def test_the_gnome_package_depends_on_python3(self, path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert python3_problems(text) == []
        # the packages that have the GNOME package: the neon variant has none
        assert (("Package: " + GNOME) in text) == ("neon" not in path)

    @pytest.mark.parametrize("name", ["control.ubuntu22.04", "control.ubuntu24.04", "control.ubuntu26.04"])
    def test_a_gnome_package_without_python3_is_reported(self, name):
        text = read("debian", name)
        broken = re.sub(r"^ +python3,?\n", "", text, flags=re.M)
        assert broken != text
        broken = broken.replace("network-manager-gnome\nDescription", "network-manager-gnome,\nDescription")
        assert python3_problems(broken) != []

    @pytest.mark.parametrize("dep", ["python3-gi", "gir1.2-gtk-3.0"])
    @pytest.mark.parametrize("name", ["control.ubuntu22.04", "control.ubuntu24.04", "control.ubuntu26.04"])
    def test_a_gnome_package_without_the_gtk_bindings_is_reported(self, name, dep):
        text = read("debian", name)
        broken = re.sub(r"(?<=[ ,])%s,?(?=\n)" % re.escape(dep), "", text)
        assert broken != text
        assert python3_problems(broken) != []

    def test_python3_in_another_package_does_not_count(self):
        # python3 only in the core package (python3-gi is not python3)
        text = read("debian", "control.ubuntu24.04")
        stanza_start = text.index("Package: " + GNOME)
        head, tail = text[:stanza_start], text[stanza_start:]
        assert python3_problems(head + tail.replace("         python3,\n", "")) != []


class TestLegacySymlink:
    def test_the_gnome_package_has_the_ldconfig_trigger(self):
        assert "activate-noawait ldconfig" in read("debian", GNOME + ".triggers").splitlines()


class TestLintianInTheBuild:
    def step(self):
        return "\n".join(workflow_steps.step_lines("build-release.yml", "Build Debian packages"))

    def test_the_build_runs_lintian_and_fails_on_errors(self):
        assert lintian_problems(self.step()) == []

    @pytest.mark.parametrize("old,new", [
        ("--fail-on error", ""),
        ("--fail-on error", "--fail-on warning --no-such"),
        ("--fail-on error", "--fail-on none"),
    ])
    def test_a_build_without_the_error_gate_is_reported(self, old, new):
        step = self.step()
        assert old in step
        assert lintian_problems(step.replace(old, new)) != []

    def test_a_build_without_lintian_is_reported(self):
        step = "\n".join(l for l in self.step().splitlines() if "lintian" not in l)
        assert lintian_problems(step) != []

    def test_lintian_before_the_build_is_reported(self):
        step = self.step()
        line = next(l for l in step.splitlines() if l.strip().startswith("lintian"))
        moved = step.replace(line + "\n", "").replace("dpkg-buildpackage -us -uc -b\n",
                                                      line + "\n dpkg-buildpackage -us -uc -b\n")
        assert lintian_problems(moved) != []

    @pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(ROOT, "Dockerfile.ubuntu*"))), ids=os.path.basename)
    def test_every_build_image_has_lintian(self, path):
        with open(path, encoding="utf-8") as handle:
            assert re.search(r"^\s+lintian\s*\\?$", handle.read(), re.M)


class TestLintianRunsInTheContainerScript:
    """The script of `docker run ... bash -c '...'` run with fake tools: a lintian error stops it"""

    def script(self):
        step = workflow_steps.step_lines("build-release.yml", "Build Debian packages")
        start = next(i for i, l in enumerate(step) if l.rstrip().endswith("bash -c '"))
        end = next(i for i in range(start + 1, len(step)) if step[i].strip() == "'")
        return "\n".join(step[start + 1:end])

    def run(self, tmp_path, lintian_status):
        bin_dir, work, out = tmp_path / "bin", tmp_path / "work", tmp_path / "out"
        for directory in (bin_dir, work / "debian", out):
            directory.mkdir(parents=True)
        (work / "debian" / "control.ubuntu24.04").write_text("Source: x\n")
        (work / "debian" / "changelog").write_text("x (1.0-1) unstable; urgency=medium\n")
        (tmp_path / "os-release").write_text("VERSION_CODENAME=noble\n")
        fakes = {
            "dpkg-buildpackage": 'echo built >> "$LOG"',
            "lintian": 'echo "lintian $*" >> "$LOG"; exit %d' % lintian_status,
            "dpkg-parsechangelog": "echo 1.0-1",
            "cp": 'echo "cp $*" >> "$LOG"; case "$*" in *debian/control*) /bin/cp "$@";; esac',
        }
        for name, body in fakes.items():
            (bin_dir / name).write_text("#!/bin/bash\n" + body + "\n")
            (bin_dir / name).chmod(0o755)
        script = self.script().replace("/etc/os-release", str(tmp_path / "os-release"))
        env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "LOG": str(tmp_path / "log"), "CONTROL": "debian/control.ubuntu24.04",
               "PR_SUFFIX": ""}
        result = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, timeout=30)
        log = (tmp_path / "log").read_text().splitlines()
        return result, log

    def test_a_clean_lintian_run_lets_the_packages_through(self, tmp_path):
        result, log = self.run(tmp_path, 0)
        assert result.returncode == 0, result.stderr
        assert any(l.startswith("lintian --fail-on error") and "/build/*.deb" in l for l in log)
        assert any(l.startswith("cp /build/") for l in log)

    def test_a_lintian_error_stops_the_build_before_the_packages_are_copied(self, tmp_path):
        result, log = self.run(tmp_path, 2)
        assert result.returncode != 0
        assert "built" in log
        assert not any(l.startswith("cp /build/") for l in log)


# --- merged /usr (lintian aliased-location, an error on Ubuntu 26.04) ---------

ALIASED_RE = re.compile(r"\$\(CURDIR\)/debian/[a-z0-9.+-]+/(lib|bin|sbin|lib64)/")


def aliased_locations(rules_text):
    """Install targets in debian/rules that use /lib, /bin or /sbin (aliases of /usr)"""
    return ALIASED_RE.findall(rules_text)


class TestNoAliasedLocations:
    def test_rules_install_nothing_into_an_aliased_directory(self):
        assert aliased_locations(read("debian", "rules")) == []

    def test_the_systemd_unit_is_in_usr_lib(self):
        assert "debian/network-manager-gpclient/usr/lib/systemd/system/nm-gpclient.service" in read(
            "debian", "rules")

    @pytest.mark.parametrize("target", [
        "$(CURDIR)/debian/network-manager-gpclient/lib/systemd/system/nm-gpclient.service",
        "$(CURDIR)/debian/network-manager-gpclient/bin/x",
        "$(CURDIR)/debian/network-manager-gpclient-gnome/sbin/x",
    ])
    def test_an_aliased_target_is_reported(self, target):
        assert aliased_locations("\tinstall -D -m 644 f \\\n\t\t%s\n" % target) != []


# --- the named volumes of build-all.sh belong to the build user (issue #139) ---

def _builds_rust(path):
    with open(path, encoding="utf-8") as handle:
        return "rustup" in handle.read()


# The images that build gpclient and gpauth. build-all.sh mounts the volumes in
# the neon image too, but it builds only the Plasma package and never runs cargo
# (HAS_CORE is empty in debian/rules).
DOCKERFILES = [p for p in sorted(glob.glob(os.path.join(ROOT, "Dockerfile.ubuntu*"))) if _builds_rust(p)]


def named_volumes():
    """Mount points of the named volumes of build-all.sh (a bind mount starts with $)"""
    with open(os.path.join(ROOT, "build-all.sh"), encoding="utf-8") as handle:
        mounts = re.findall(r'-v "([^":]+):([^"]+)"', handle.read())
    return [dest for source, dest in mounts if not source.startswith("$")]


def unowned_mount_points(dockerfile, mounts):
    """Mount points that the image does not create as a directory of builder.

    Docker gives a new named volume the owner of the directory in the image; a
    mount point missing from the image becomes a volume of root.
    """
    lines = dockerfile.splitlines()
    copy = next(i for i, l in enumerate(lines) if l.startswith("COPY ") and "/build/source/" in l)
    user, owned = "root", set()
    for i, line in enumerate(lines):
        if line.startswith("USER "):
            user = line.split()[1]
        made = re.match(r"RUN mkdir -p (\S+)$", line)
        if made and user == "builder" and i > copy:
            owned.add("/build/source/" + made.group(1))
    text = dockerfile.replace("\\\n", " ")
    for line in text.splitlines():
        if "mkdir -p" in line and "chown -R builder:builder" in line:
            owned.update(p for p in line.split() if p.startswith("/"))
    return [m for m in mounts if m not in owned]


def test_build_all_mounts_the_rust_target_cache():
    assert "/build/source/external/GlobalProtect-openconnect/target" in named_volumes()


def test_the_rust_images_are_the_ubuntu_versions():
    assert [os.path.basename(p) for p in DOCKERFILES] == [
        "Dockerfile.ubuntu22.04", "Dockerfile.ubuntu24.04", "Dockerfile.ubuntu26.04"]


@pytest.mark.parametrize("path", DOCKERFILES, ids=os.path.basename)
def test_every_named_volume_is_a_directory_of_builder(path):
    with open(path, encoding="utf-8") as handle:
        assert unowned_mount_points(handle.read(), named_volumes()) == []


@pytest.mark.parametrize("path", DOCKERFILES, ids=os.path.basename)
def test_a_missing_or_root_owned_target_directory_is_reported(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    line = "RUN mkdir -p external/GlobalProtect-openconnect/target\n"
    target = "/build/source/external/GlobalProtect-openconnect/target"
    assert unowned_mount_points(text.replace(line, ""), named_volumes()) == [target]
    as_root = text.replace(line, "USER root\n" + line + "USER builder\n")
    assert unowned_mount_points(as_root, named_volumes()) == [target]

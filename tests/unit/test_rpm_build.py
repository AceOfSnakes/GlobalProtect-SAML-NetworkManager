"""
Tests for rpm/targets, rpm/network-manager-gpclient.spec, rpm/build.sh and
rpm/install-test.sh (the parts that run without a container).

rpm/build.sh runs in a copy of the rpm/ directory with a fake docker and a fake curl in
PATH: the tests check which container it starts and with which options, and what it
accepts in the output. The real build is done by CI (and by hand) in the target images.
Every positive test has a negative counterpart: unknown target or architecture, a
half-set or non-numeric PR number, a download with another sha256, a build that makes
the wrong packages.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RPM_DIR = os.path.join(ROOT, "rpm")

EXPECTED_TARGETS = ["fedora43", "fedora44", "fedora45", "el10", "leap16.0", "tumbleweed"]


def read_targets():
    rows = []
    with open(os.path.join(RPM_DIR, "targets"), encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].split()
            if line:
                rows.append(line)
    return rows


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


# --- rpm/targets ---


def test_targets_are_the_documented_ones():
    assert [row[0] for row in read_targets()] == EXPECTED_TARGETS


def test_targets_columns():
    rows = read_targets()
    for row in rows:
        assert len(row) == 4, row
        target, image, dist, family = row
        assert re.fullmatch(r"[a-z0-9.]+", target)
        assert re.fullmatch(r"[a-z0-9./-]+:[A-Za-z0-9._-]+", image)
        assert re.fullmatch(r"\.[a-z]+[0-9]*", dist)
        assert family in ("fedora", "el", "suse")
    # A name or a dist tag twice would make two targets build the same file names
    assert len({row[0] for row in rows}) == len(rows)
    assert len({row[2] for row in rows}) == len(rows)


@pytest.mark.parametrize("target", EXPECTED_TARGETS)
def test_dist_matches_family(target):
    (row,) = [r for r in read_targets() if r[0] == target]
    prefix = {"fedora": ".fc", "el": ".el", "suse": ".lp" if target.startswith("leap") else ".tw"}
    assert row[2].startswith(prefix[row[3]])


# --- rpm/network-manager-gpclient.spec ---


def test_spec_takes_everything_from_the_build_script():
    spec = read("rpm/network-manager-gpclient.spec")
    assert re.search(r"^Version:\s+%\{project_version\}$", spec, re.M)
    assert re.search(r"^Release:\s+%\{debian_release\}%\{\?dist\}%\{\?pr_suffix\}$", spec, re.M)
    assert re.search(r"^Source1:\s+sdbus-%\{sdbus_version\}\.tar\.gz$", spec, re.M)
    # build.sh appends the generated entries: nothing may follow %changelog
    assert spec.rstrip().endswith("%changelog")
    assert len(re.findall(r"^%changelog", spec, re.M)) == 1


def test_spec_does_not_hard_code_what_the_script_derives():
    spec = read("rpm/network-manager-gpclient.spec")
    assert not re.search(r"^Version:\s+\d", spec, re.M)
    assert "x86_64-linux-gnu" not in spec
    # Fedora and EL manage /etc/nsswitch.conf with authselect
    assert not re.search(r"^[^#\n]*nsswitch", spec, re.M)


def test_spec_installs_what_debian_rules_installs():
    """Every source file debian/rules installs is installed by the spec as well."""
    rules = read("debian/rules")
    sources = re.findall(r"install -D -m \d+ (\S+) \\", rules)
    assert len(sources) >= 15
    spec = read("rpm/network-manager-gpclient.spec")
    # Built files are named by their build location in the spec
    renamed = {
        "libnm-vpn-plugin-gpclient.so": "plugins/gnome/libnm-vpn-plugin-gpclient.so",
        "libnm-vpn-plugin-gpclient-editor.so": "plugins/gnome/libnm-vpn-plugin-gpclient-editor.so",
        "libnm-gtk4-vpn-plugin-gpclient-editor.so": "plugins/gnome/libnm-gtk4-vpn-plugin-gpclient-editor.so",
        "plugins/plasma/build/plasmanetworkmanagement_gpclientui.so": "plugins/plasma/build/plasmanetworkmanagement_gpclientui.so",
    }
    missing = [s for s in sources if renamed.get(s, s) not in spec]
    # The Qt 5 kservices file has no Plasma 6 counterpart
    assert missing == ["plugins/plasma/plasmanetworkmanagement_gpclientui.desktop"]


def test_spec_files_has_the_url_handler_and_the_symlink_in_gnome():
    spec = read("rpm/network-manager-gpclient.spec")
    files = {}
    current = None
    for line in spec.splitlines():
        match = re.match(r"^%files\s*(\S*)", line)
        if match:
            current = match.group(1) or "core"
            files[current] = []
        elif current and line.strip() and not line.startswith("%changelog"):
            files[current].append(line)
    assert any("gpgui.desktop" in l for l in files["core"])
    assert any("libnm-gpclient-properties" in l for l in files["gnome"])
    assert not any("libnm-gpclient-properties" in l for l in files["core"])
    assert any("plasmanetworkmanagement_gpclientui.so" in l for l in files["plasma"])


# --- rpm/build.sh ---

FAKE_DOCKER = """\
#!/bin/bash
echo "$@" >> "$FAKE_LOG"
out=
for arg in "$@"; do
    case "$arg" in *:/out) out=${arg%:/out} ;; esac
done
if [ -n "$out" ]; then
    for name in $FAKE_RPMS; do : > "$out/$name"; done
fi
exit "${FAKE_DOCKER_RC:-0}"
"""

FAKE_CURL = """\
#!/bin/bash
# curl -fsSL --retry 3 -o FILE URL
while [ $# -gt 0 ]; do
    case "$1" in -o) shift; printf '%s' "$FAKE_SDBUS" > "$1" ;; esac
    shift
done
"""

SDBUS_CONTENT = "fake sdbus archive"


@pytest.fixture
def sandbox(tmp_path):
    """A copy of the rpm/ directory with a fake docker, a fake curl and a changelog."""
    root = tmp_path / "repo"
    shutil.copytree(RPM_DIR, root / "rpm", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "debian").mkdir()
    shutil.copy(os.path.join(ROOT, "debian", "changelog"), root / "debian" / "changelog")
    deps = root / "external" / "GlobalProtect-openconnect" / "crates" / "openconnect" / "deps"
    for name in ("openconnect", "libxml2"):
        (deps / name).mkdir(parents=True)
        (deps / name / "configure.ac").write_text("")
    # The pinned sha256 of the real archive is replaced by the one of the fake
    import hashlib

    digest = hashlib.sha256(SDBUS_CONTENT.encode()).hexdigest()
    sources = (root / "rpm" / "sources").read_text()
    (root / "rpm" / "sources").write_text(re.sub(r"SDBUS_SHA256=\w+", f"SDBUS_SHA256={digest}", sources))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "FAKE_SDBUS": SDBUS_CONTENT,
    }
    return root, env, log, tmp_path


def good_rpms(version, release, arch="x86_64"):
    return " ".join(
        f"network-manager-gpclient{suffix}-{version}-{release}.{arch}.rpm"
        for suffix in ("", "-gnome", "-plasma")
    )


def top_version():
    top = re.match(r"^\S+ \((\S+)\) ", read("debian/changelog")).group(1)
    return top.rsplit("-", 1)


def build(sandbox, *args, env_extra=None):
    root, env, _log, tmp = sandbox
    env = {**env, **(env_extra or {})}
    return subprocess.run(
        [str(root / "rpm" / "build.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
        check=False,
    )


def test_build_runs_the_target_image_and_collects_three_rpms(sandbox):
    version, revision = top_version()
    out = sandbox[3] / "out"
    result = build(
        sandbox,
        "fedora44",
        "amd64",
        str(out),
        env_extra={"FAKE_RPMS": good_rpms(version, f"{revision}.fc44")},
    )
    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(out)) == sorted(good_rpms(version, f"{revision}.fc44").split())
    call = sandbox[2].read_text().strip().splitlines()
    assert len(call) == 1
    assert "--platform linux/amd64" in call[0]
    assert "registry.fedoraproject.org/fedora:44 bash /container-build.sh fedora /in /out" in call[0]
    assert f"--define project_version {version}" in call[0]
    assert f"--define debian_release {revision}" in call[0]
    assert "--define dist .fc44" in call[0]
    assert "--define sdbus_version 0.14.3" in call[0]
    assert "pr_suffix" not in call[0]


@pytest.mark.parametrize(
    "target, arch, family, dist",
    [("el10", "arm64", "el", ".el10"), ("leap16.0", "arm64", "suse", ".lp160"), ("tumbleweed", "amd64", "suse", ".tw")],
)
def test_build_uses_family_dist_and_platform_of_the_target(sandbox, target, arch, family, dist):
    version, revision = top_version()
    rpm_arch = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    result = build(
        sandbox,
        target,
        arch,
        str(sandbox[3] / "out"),
        env_extra={"FAKE_RPMS": good_rpms(version, revision + dist, rpm_arch)},
    )
    assert result.returncode == 0, result.stderr
    call = sandbox[2].read_text()
    assert f"--platform linux/{arch}" in call
    assert f"/container-build.sh {family} /in /out" in call
    assert f"--define dist {dist}" in call


def test_build_pull_request_release(sandbox):
    version, revision = top_version()
    release = f"{revision}.fc44.pr36.7"
    result = build(
        sandbox,
        "fedora44",
        "amd64",
        str(sandbox[3] / "out"),
        env_extra={"PR_NUMBER": "36", "RUN_NUMBER": "7", "FAKE_RPMS": good_rpms(version, release)},
    )
    assert result.returncode == 0, result.stderr
    assert "--define pr_suffix .pr36.7" in sandbox[2].read_text()
    assert f"release {revision}.fc44.pr36.7" in result.stdout


@pytest.mark.parametrize(
    "pr, run",
    [("36", ""), ("", "7"), ("36x", "7"), ("36", "7 8"), ("$(id)", "7"), ("36", "-1")],
)
def test_build_rejects_a_bad_pr_or_run_number(sandbox, pr, run):
    result = build(
        sandbox, "fedora44", "amd64", str(sandbox[3] / "out"), env_extra={"PR_NUMBER": pr, "RUN_NUMBER": run}
    )
    assert result.returncode != 0
    assert "PR_NUMBER and RUN_NUMBER" in result.stderr
    assert not sandbox[2].exists(), "docker must not run"


@pytest.mark.parametrize(
    "args, message",
    [
        (["fedora99", "amd64"], "unknown target"),
        (["fedora44; id", "amd64"], "unknown target"),
        (["", "amd64"], "unknown target"),
        (["fedora44", "x86_64"], "unknown architecture"),
        (["fedora44", "i386"], "unknown architecture"),
        (["fedora44"], "usage"),
        ([], "usage"),
        (["a", "b", "c", "d"], "usage"),
    ],
)
def test_build_rejects_bad_arguments(sandbox, args, message):
    result = build(sandbox, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert not sandbox[2].exists(), "docker must not run"


def test_build_rejects_an_sdbus_archive_with_another_sha256(sandbox):
    result = build(
        sandbox,
        "fedora44",
        "amd64",
        str(sandbox[3] / "out"),
        env_extra={"FAKE_SDBUS": "tampered archive"},
    )
    assert result.returncode != 0
    assert "sha256" in result.stderr
    assert not sandbox[2].exists(), "docker must not run"


def test_build_stops_without_submodules(sandbox):
    root = sandbox[0]
    os.remove(
        root / "external" / "GlobalProtect-openconnect" / "crates" / "openconnect" / "deps" / "libxml2" / "configure.ac"
    )
    result = build(sandbox, "fedora44", "amd64", str(sandbox[3] / "out"))
    assert result.returncode != 0
    assert "submodules" in result.stderr


def test_build_stops_when_the_container_fails(sandbox):
    result = build(
        sandbox, "fedora44", "amd64", str(sandbox[3] / "out"), env_extra={"FAKE_DOCKER_RC": "3"}
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "names",
    [
        # Too few packages
        lambda v, r: good_rpms(v, r).rsplit(" ", 1)[0],
        # Wrong architecture
        lambda v, r: good_rpms(v, r, "aarch64"),
        lambda v, r: "",
    ],
)
def test_build_rejects_a_wrong_set_of_packages(sandbox, names):
    version, revision = top_version()
    result = build(
        sandbox,
        "fedora44",
        "amd64",
        str(sandbox[3] / "out"),
        env_extra={"FAKE_RPMS": names(version, f"{revision}.fc44")},
    )
    assert result.returncode != 0


@pytest.mark.parametrize("extra", ["{n}-{v}-{r}.src.rpm", "{n}-debuginfo-{v}-{r}.x86_64.rpm", "{n}-debugsource-{v}-{r}.x86_64.rpm"])
def test_build_leaves_source_and_debug_packages_out(sandbox, extra):
    version, revision = top_version()
    release = f"{revision}.fc44"
    out = sandbox[3] / "out"
    stray = extra.format(n="network-manager-gpclient", v=version, r=release)
    result = build(
        sandbox, "fedora44", "amd64", str(out), env_extra={"FAKE_RPMS": good_rpms(version, release) + " " + stray}
    )
    assert result.returncode == 0, result.stderr
    assert stray not in os.listdir(out)
    assert len(os.listdir(out)) == 3


def test_build_removes_old_rpms_from_the_output_dir(sandbox):
    version, revision = top_version()
    out = sandbox[3] / "out"
    out.mkdir()
    (out / "old-1.0-1.x86_64.rpm").write_text("")
    result = build(
        sandbox, "fedora44", "amd64", str(out), env_extra={"FAKE_RPMS": good_rpms(version, f"{revision}.fc44")}
    )
    assert result.returncode == 0, result.stderr
    assert "old-1.0-1.x86_64.rpm" not in os.listdir(out)


def test_install_test_mode_starts_a_fresh_container_with_the_rpms(sandbox):
    out = sandbox[3] / "rpms"
    out.mkdir()
    (out / "a.rpm").write_text("")
    result = build(sandbox, "--install-test", "tumbleweed", "arm64", str(out))
    assert result.returncode == 0, result.stderr
    call = sandbox[2].read_text()
    assert "--rm --platform linux/arm64" in call
    assert f"{out}:/rpms:ro" in call
    assert "registry.opensuse.org/opensuse/tumbleweed:latest bash /install-test.sh /rpms" in call


def test_install_test_mode_needs_rpms_and_a_known_target(sandbox):
    empty = sandbox[3] / "empty"
    empty.mkdir()
    result = build(sandbox, "--install-test", "tumbleweed", "arm64", str(empty))
    assert result.returncode != 0 and "no RPMs" in result.stderr
    result = build(sandbox, "--install-test", "nonsense", "arm64", str(empty))
    assert result.returncode != 0 and "unknown target" in result.stderr
    assert not sandbox[2].exists()


# --- rpm/install-test.sh ---


def run_install_test(*args):
    return subprocess.run(
        ["bash", os.path.join(RPM_DIR, "install-test.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_test_needs_a_directory(tmp_path):
    for args in ([], [""], [str(tmp_path / "missing")]):
        result = run_install_test(*args)
        assert result.returncode == 2
        assert "usage" in result.stderr


def test_install_test_needs_exactly_three_rpms(tmp_path):
    for count in (0, 2, 4):
        directory = tmp_path / f"d{count}"
        directory.mkdir()
        for i in range(count):
            (directory / f"p{i}.rpm").write_text("")
        result = run_install_test(str(directory))
        assert result.returncode == 1
        assert "expected 3 RPMs" in result.stderr


# --- the functions of rpm/install-test.sh (sourced; ldd is a fake) ---


def run_function(tmp_path, call, ldd_output=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if ldd_output is not None:
        (tmp_path / "ldd.out").write_text(ldd_output)
        (bin_dir / "ldd").write_text(f'#!/bin/sh\ncat "{tmp_path}/ldd.out"\n')
        (bin_dir / "ldd").chmod(0o755)
    return subprocess.run(
        ["bash", "-c", f'. "{RPM_DIR}/install-test.sh"; {call}'],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )


OPENSSL3 = "\tlibssl.so.3 => /lib64/libssl.so.3 (0x1)\n\tlibcrypto.so.3 => /lib64/libcrypto.so.3 (0x2)\n"


# OpenSSL 4 as on Fedora 45 (CI, arm64): libssl.so.4
OPENSSL4 = "\tlibssl.so.4 => /lib64/libssl.so.4 (0x1)\n\tlibcrypto.so.4 => /lib64/libcrypto.so.4 (0x2)\n"


@pytest.mark.parametrize("ldd_output", [OPENSSL3, OPENSSL4])
def test_check_openssl_accepts_openssl_3_and_later(tmp_path, ldd_output):
    result = run_function(tmp_path, "check_openssl /bin/a /bin/b", ldd_output)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "ldd_output",
    [
        "\tlibssl.so.60 => /lib64/libssl.so.60 (0x1)\n\tlibcrypto.so.58 => /lib64/libcrypto.so.58 (0x2)\n",
        "\tlibssl.so.59 (LIBRESSL_2.5) => /lib64/libssl.so.59 (0x1)\n",
        "\tlibssl.so.1.1 => /lib64/libssl.so.1.1 (0x1)\n",
        "\tlibssl.so.10 => /lib64/libssl.so.10 (0x1)\n",
        "\tlibssl.so.0.9.8 => /lib64/libssl.so.0.9.8 (0x1)\n",
        OPENSSL3 + "\tlibssl.so.60 => /lib64/libssl.so.60 (0x1)\n",
        "\tlibc.so.6 => /lib64/libc.so.6 (0x1)\n",
    ],
)
def test_check_openssl_rejects_other_libraries(tmp_path, ldd_output):
    result = run_function(tmp_path, "check_openssl /bin/a", ldd_output)
    assert result.returncode == 1
    assert "FAIL" in result.stderr


def make_vpnc_root(tmp_path, hooks="/etc/vpnc", hook=True, script_mode=0o755, where="/etc/vpnc/vpnc-script"):
    root = tmp_path / "root"
    script = root / where.lstrip("/")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\nHOOKS_DIR={hooks}\n")
    script.chmod(script_mode)
    if hook:
        hook_file = root / hooks.lstrip("/") / "connect.d" / "90-gpclient-routing"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("#!/bin/sh\n")
        hook_file.chmod(0o755)
    return root


@pytest.mark.parametrize("where", ["/etc/vpnc/vpnc-script", "/usr/share/vpnc-scripts/vpnc-script"])
def test_check_vpnc_script_accepts(tmp_path, where):
    root = make_vpnc_root(tmp_path, where=where)
    result = run_function(tmp_path, f'check_vpnc_script "{root}"')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hook": False},  # our hook is not in the directory the script runs
        {"hooks": "/usr/share/vpnc", "hook": False},
        {"script_mode": 0o644},  # not executable
        {"where": "/opt/vpnc-script"},  # a path gpclient does not search
    ],
)
def test_check_vpnc_script_rejects(tmp_path, kwargs):
    root = make_vpnc_root(tmp_path, **kwargs)
    if kwargs.get("hooks"):
        # The hook is in /etc/vpnc, the script runs those of another directory
        hook = root / "etc/vpnc/connect.d/90-gpclient-routing"
        hook.parent.mkdir(parents=True)
        hook.write_text("")
        hook.chmod(0o755)
    result = run_function(tmp_path, f'check_vpnc_script "{root}"')
    assert result.returncode == 1
    assert "FAIL" in result.stderr


def test_check_vpnc_script_rejects_a_missing_script(tmp_path):
    (tmp_path / "root").mkdir()
    result = run_function(tmp_path, f'check_vpnc_script "{tmp_path / "root"}"')
    assert result.returncode == 1 and "no executable vpnc-script" in result.stderr


# --- spec ---


def test_spec_openssl_and_vpnc_script_per_family():
    spec = read("rpm/network-manager-gpclient.spec")
    suse, _, other = spec.partition("%if 0%{?suse_version}\n")[2].partition("%else\n")
    assert "BuildRequires:  libopenssl-devel" in suse
    assert "BuildConflicts: libressl-devel" in suse
    assert "BuildRequires:  pkgconfig(openssl)" in other
    # Not for openSUSE, where it resolves to LibreSSL
    assert "BuildRequires:  pkgconfig(openssl)" not in suse
    assert "Requires:       vpnc\n%else\nRequires:       vpnc-script\n%endif" in spec


def test_spec_owns_its_directories_and_stops_the_service_by_command_line():
    spec = read("rpm/network-manager-gpclient.spec")
    for directory in ("%{_sysconfdir}/vpnc", "%{_sysconfdir}/vpnc/connect.d", "%{_prefix}/libexec/NetworkManager"):
        assert f"%dir {directory}\n" in spec
    # The process name is cut to 15 characters: pkill -x can never match
    assert "pkill -x" not in spec
    assert "pkill -f '^(/usr/bin/)?python3[.0-9]* /usr/lib/NetworkManager/nm-gpclient-service'" in spec


def test_spec_pkill_pattern_matches_the_service_only():
    spec = read("rpm/network-manager-gpclient.spec")
    pattern = re.search(r"pkill -f '(\^[^']*nm-gpclient-service)'", spec).group(1)
    regex = re.compile(pattern)
    for line in (
        "/usr/bin/python3 /usr/lib/NetworkManager/nm-gpclient-service",
        "python3 /usr/lib/NetworkManager/nm-gpclient-service",
        "/usr/bin/python3.13 /usr/lib/NetworkManager/nm-gpclient-service",
    ):
        assert regex.match(line), line
    for line in (
        "pkill -f nm-gpclient-service",
        "/bin/sh /var/tmp/rpm-tmp.x",
        "vim /usr/lib/NetworkManager/nm-gpclient-service",
        "/usr/bin/pkill -f /usr/lib/NetworkManager/nm-gpclient-service",
        "/usr/bin/perl /usr/lib/NetworkManager/nm-gpclient-service",
    ):
        assert not regex.match(line), line


def test_spec_strips_the_rust_binaries():
    # Fedora's rpmbuild exports RUSTFLAGS with -Cstrip=none, which overrides
    # upstream's release profile (strip = true)
    spec = read("rpm/network-manager-gpclient.spec")
    assert "env -u RUSTFLAGS cargo build --locked --release" in spec
    assert "CARGO_PROFILE_RELEASE_STRIP" not in spec


CARGO_BUILD_FILES = ("debian/rules", "Makefile", "rpm/network-manager-gpclient.spec")


def cargo_build_lines_without_locked(text):
    return [line.strip() for line in text.splitlines()
            if not line.lstrip().startswith("#") and re.search(r"\bcargo\b\S* build\b", line, re.I) and "--locked" not in line]


@pytest.mark.parametrize("path", CARGO_BUILD_FILES)
def test_every_cargo_build_is_locked(path):
    # the build must use the dependency versions of upstream's Cargo.lock, or fail
    text = read(path)
    assert re.search(r"\bcargo\b\S* build\b", text, re.I)
    assert cargo_build_lines_without_locked(text) == []


@pytest.mark.parametrize("path", CARGO_BUILD_FILES)
def test_a_cargo_build_without_locked_is_reported(path):
    text = read(path).replace(" --locked", "")
    assert cargo_build_lines_without_locked(text)


def test_spec_neither_builds_nor_installs_nor_packages_gpservice():
    # nothing in this project starts gpservice: the callback path is gpclient launch-gui -> gpauth
    assert "gpservice" not in read("rpm/network-manager-gpclient.spec")
    assert "gpservice" not in read("rpm/install-test.sh")


def provides_exclude_pattern():
    spec = read("rpm/network-manager-gpclient.spec")
    match = re.search(r"^%global __provides_exclude (\S+)$", spec, re.M)
    assert match, "no __provides_exclude"
    # The spec doubles the backslashes (rpm macro escaping); rpm passes single ones on
    return re.compile(match.group(1).replace("\\\\", "\\"))


@pytest.mark.parametrize("provide", ["python3dist(sdbus)", "python3.14dist(sdbus)", "python3.13dist(sdbus)"])
def test_spec_drops_the_generated_sdbus_provides(provide):
    assert provides_exclude_pattern().search(provide)


@pytest.mark.parametrize(
    "provide",
    ["bundled(python3dist(sdbus)) = 0.14.3", "network-manager-gpclient", "python3dist(sdbus-networkmanager)", "libssl.so.3()(64bit)"],
)
def test_spec_keeps_every_other_provide(provide):
    assert not provides_exclude_pattern().search(provide)


def make_elf(path, section_types):
    # A 64-bit little-endian ELF header and one section header per type: all
    # check_stripped reads (e_shoff at 0x28, e_shentsize and e_shnum at 0x3A)
    import struct

    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 0x28, 64)
    struct.pack_into("<HH", header, 0x3A, 64, len(section_types))
    sections = b"".join(struct.pack("<II", 0, t) + bytes(56) for t in section_types)
    path.write_bytes(bytes(header) + sections)
    return path


def test_check_stripped_accepts_binaries_without_a_symbol_table(tmp_path):
    # NULL, PROGBITS, STRTAB, DYNSYM (the dynamic symbols stay in a stripped binary)
    exe = make_elf(tmp_path / "gpclient", [0, 1, 3, 11])
    result = run_function(tmp_path, f'check_stripped "{exe}"')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("section_types", [[0, 1, 2, 3], [2]])
def test_check_stripped_rejects_a_symbol_table(tmp_path, section_types):
    exe = make_elf(tmp_path / "gpclient", section_types)
    result = run_function(tmp_path, f'check_stripped "{exe}"')
    assert result.returncode == 1
    assert "is not stripped" in result.stderr


def test_check_stripped_rejects_a_file_that_is_not_elf(tmp_path):
    exe = tmp_path / "gpclient"
    exe.write_text("#!/bin/sh\n")
    result = run_function(tmp_path, f'check_stripped "{exe}"')
    assert result.returncode == 1

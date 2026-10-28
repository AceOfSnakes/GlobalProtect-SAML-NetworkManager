"""
Tests for the vendored crates and the offline build of issue #160: debian/rules and the
RPM spec use vendor/config.toml and --offline only when the config exists, CI vendors
the crates before the package builds and runs the Debian build without network, and the
reproducibility workflow exists and is not run for pull requests.

Every check is a function on the text of a file, so a negative test runs it on a
modified copy that must be rejected. Nothing is built.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as handle:
        return handle.read()


def rules_problems(text):
    """What is wrong with the conditional offline flags of debian/rules"""
    problems = []
    flags = re.search(r"^CARGO_VENDOR_FLAGS\s*:=\s*\$\(if \$\(wildcard \$\(VENDOR_CONFIG\)\),(.*),\)\s*$", text, re.M)
    if not re.search(r"^VENDOR_CONFIG\s*:=\s*\$\(CURDIR\)/vendor/config\.toml$", text, re.M):
        problems.append("VENDOR_CONFIG is not $(CURDIR)/vendor/config.toml")
    if not flags:
        problems.append("CARGO_VENDOR_FLAGS is not conditional on the config file")
    elif "--offline" not in flags.group(1) or "--config $(VENDOR_CONFIG)" not in flags.group(1):
        problems.append("the conditional flags lack --offline or --config")
    build = [l for l in text.splitlines() if "cargo auditable build" in l]
    if len(build) != 1 or "--locked $(CARGO_VENDOR_FLAGS)" not in build[0]:
        problems.append("the cargo build does not use --locked and $(CARGO_VENDOR_FLAGS)")
    return problems


def spec_problems(text):
    """What is wrong with the conditional offline flags of the RPM spec"""
    problems = []
    if not re.search(r'if \[ -f "\$PWD/vendor/config\.toml" \]; then\n\s*vendor_flags="--offline --config \$PWD/vendor/config\.toml"\n\s*fi', text):
        problems.append("vendor_flags is not set only when vendor/config.toml exists")
    build = [l for l in text.splitlines() if "cargo auditable build" in l]
    if len(build) != 1 or "--locked $vendor_flags" not in build[0]:
        problems.append("the cargo build does not use --locked and $vendor_flags")
    return problems


def release_problems(text):
    """What is wrong with the vendoring in build-release.yml"""
    problems = []
    vendor = [m.start() for m in re.finditer(r"bash scripts/vendor-crates\.sh", text)]
    if len(vendor) != 2:
        problems.append("expected 2 vendor steps (Debian and RPM), found %d" % len(vendor))
    else:
        if not vendor[0] < text.find("docker build ") < vendor[1] < text.find("bash rpm/build.sh"):
            problems.append("the crates are not vendored before the Debian image and the RPM build")
    if not re.search(r"docker run --rm --network none -e PR_SUFFIX=", text):
        problems.append("the Debian build container does not run with --network none")
    return problems


def test_rules_use_vendored_crates_when_present():
    assert rules_problems(read("debian", "rules")) == []


def test_spec_uses_vendored_crates_when_present():
    assert spec_problems(read("rpm", "network-manager-gpclient.spec")) == []


def test_release_workflow_vendors_and_builds_without_network():
    assert release_problems(read(".github", "workflows", "build-release.yml")) == []


@pytest.mark.parametrize(
    "old, new",
    [
        ("--offline --config $(VENDOR_CONFIG)", "--config $(VENDOR_CONFIG)"),
        ("$(if $(wildcard $(VENDOR_CONFIG)),", "$(if 1,"),
        ("--locked $(CARGO_VENDOR_FLAGS)", "--locked"),
        ("$(CURDIR)/vendor/config.toml", "$(CURDIR)/config.toml"),
    ],
)
def test_rules_mutations_are_rejected(old, new):
    text = read("debian", "rules")
    assert old in text
    assert rules_problems(text.replace(old, new)) != []


@pytest.mark.parametrize(
    "old, new",
    [
        ('if [ -f "$PWD/vendor/config.toml" ]; then', "if true; then"),
        ("--offline --config", "--config"),
        ("--locked $vendor_flags", "--locked"),
    ],
)
def test_spec_mutations_are_rejected(old, new):
    text = read("rpm", "network-manager-gpclient.spec")
    assert old in text
    assert spec_problems(text.replace(old, new)) != []


@pytest.mark.parametrize(
    "old, new",
    [
        ("docker run --rm --network none -e", "docker run --rm -e"),
        ("bash rpm/build.sh", "bash rpm/build2.sh"),
    ],
)
def test_release_mutations_are_rejected(old, new):
    text = read(".github", "workflows", "build-release.yml")
    assert old in text
    assert release_problems(text.replace(old, new)) != []


def test_release_without_vendor_step_is_rejected():
    text = read(".github", "workflows", "build-release.yml")
    assert release_problems(text.replace("bash scripts/vendor-crates.sh", "true")) != []


def test_vendor_dir_is_ignored_by_git_but_not_by_docker():
    assert "vendor/" in read(".gitignore").splitlines()
    assert "vendor/" not in read(".dockerignore").splitlines()
    # The RPM source archive must keep vendor/
    assert "--exclude=./vendor" not in read("rpm", "build.sh")


def test_vendor_script_is_in_shell_scripts_and_strict():
    assert "\tscripts/vendor-crates.sh \\\n" in read("Makefile")
    assert "set -euo pipefail" in read("scripts", "vendor-crates.sh")


def test_vendor_script_needs_cargo(tmp_path):
    """Without cargo the script fails with a clear message (negative twin: the submodule check)"""
    import subprocess

    empty = tmp_path / "bin"
    empty.mkdir()
    result = subprocess.run(
        ["/bin/bash", os.path.join(ROOT, "scripts", "vendor-crates.sh")],
        env={"PATH": str(empty)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "cargo not found" in result.stderr


def reproducible_problems(text):
    """What is wrong with the trigger and the build of reproducible.yml"""
    on = text.split("\npermissions:")[0]
    problems = []
    if "workflow_dispatch:" not in on or "schedule:" not in on:
        problems.append("not run by hand and weekly")
    if "pull_request" in on or "push:" in on:
        problems.append("run on every pull request or push")
    for needle in ("persist-credentials: false", "submodules: recursive", "--network none", "diffoscope"):
        if needle not in text:
            problems.append("missing: " + needle)
    return problems


def test_reproducible_workflow_runs_by_hand_and_weekly_only():
    assert reproducible_problems(read(".github", "workflows", "reproducible.yml")) == []


@pytest.mark.parametrize(
    "old, new",
    [
        ("  schedule:", "  pull_request:\n  schedule:"),
        ("  workflow_dispatch:\n", ""),
        ("persist-credentials: false", ""),
        ("--network none", ""),
    ],
)
def test_reproducible_mutations_are_rejected(old, new):
    text = read(".github", "workflows", "reproducible.yml")
    assert old in text
    assert reproducible_problems(text.replace(old, new)) != []

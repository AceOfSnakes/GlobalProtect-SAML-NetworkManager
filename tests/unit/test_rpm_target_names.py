"""The RPM file name patterns of the CI scripts know every target of rpm/targets.

release_notes.py, test_packages_check.py and build-rpm-repo.sh each match the
RPM file names, with the dist tags of rpm/targets. A target with a new dist
family (.mga10, say) must be added to all of them, or its packages are left
out of the release notes, the PR test packages and the repository.
"""

import importlib.util
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = load("release_notes_for_names", ".github/scripts/release_notes.py")
packages_check = load("test_packages_check_for_names", ".github/scripts/test_packages_check.py")


def repo_name_re():
    text = open(os.path.join(ROOT, ".github/scripts/build-rpm-repo.sh")).read()
    match = re.search(r"^name_re='([^']+)'$", text, re.M)
    assert match, "no name_re in build-rpm-repo.sh"
    return re.compile(match.group(1))


def dists():
    result = []
    with open(os.path.join(ROOT, "rpm/targets")) as targets:
        for line in targets:
            fields = line.split()
            if fields and not fields[0].startswith("#"):
                result.append(fields[2])
    assert result, "rpm/targets lists no target"
    return result


PACKAGES = ["network-manager-gpclient", "network-manager-gpclient-gnome", "network-manager-gpclient-plasma"]


@pytest.mark.parametrize("dist", dists())
@pytest.mark.parametrize("package", PACKAGES)
@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_every_target_dist_matches_every_pattern(dist, package, arch):
    release = f"{package}-1.5.0-1{dist}.{arch}.rpm"
    assert release_notes.RPM_NAME_RE.fullmatch(release)
    assert packages_check.RPM_RE.fullmatch(release)
    assert repo_name_re().fullmatch(release)
    # Pull request builds: only the test packages take them
    assert packages_check.RPM_RE.fullmatch(f"{package}-1.5.0-1{dist}.pr42.7.{arch}.rpm")


@pytest.mark.parametrize("dist", [".mga10", ".fc", ".el", ".FC44", ""])
def test_a_dist_of_no_target_matches_no_release_pattern(dist):
    name = f"network-manager-gpclient-1.5.0-1{dist}.x86_64.rpm"
    assert dist not in dists()
    assert not release_notes.RPM_NAME_RE.fullmatch(name)
    assert not packages_check.RPM_RE.fullmatch(name)

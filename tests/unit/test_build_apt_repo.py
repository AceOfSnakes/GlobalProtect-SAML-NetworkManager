"""
Tests for .github/scripts/build-apt-repo.sh with the package for KDE neon,
network-manager-gpclient-plasma-6: it is built for Ubuntu 24.04 (version 1.5.0-1~noble1), so
it belongs to the suite noble and to amd64 only, like the codename of any other package says;
nothing is decided by the name of the package.

The .deb files are real (dpkg-deb --build of an empty package); apt-ftparchive, which only writes
the Release file, is a fake, and the repository is not signed. Every positive test has a negative
counterpart: a package that names no known release, or has an architecture the repository does
not serve, stops the script and builds no repository.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "build-apt-repo.sh")
CORE = "network-manager-gpclient"
PLASMA6 = CORE + "-plasma-6"

FAKE_FTPARCHIVE = """#!/bin/sh
# apt-ftparchive ... release <dir>: only the Release file's existence matters here
echo "Suite: fake"
"""


def make_deb(directory, package, version, arch="amd64"):
    """A real, empty .deb; returns its path"""
    root = directory / f"{package}_{version}_{arch}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: x <x@example.org>\n"
        "Description: x\n x\n"
    )
    deb = directory / f"{package}_{version}_{arch}.deb"
    subprocess.run(["dpkg-deb", "--build", str(root), str(deb)], check=True, capture_output=True)
    return deb


class Repo:
    def __init__(self, tmp_path):
        self.incoming = tmp_path / "incoming"
        self.incoming.mkdir()
        self.out = tmp_path / "repo"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "apt-ftparchive").write_text(FAKE_FTPARCHIVE)
        (self.bin / "apt-ftparchive").chmod(0o755)
        self.work = tmp_path / "work"
        self.work.mkdir()

    def add(self, package, version, arch="amd64"):
        return make_deb(self.work, package, version, arch).rename(self.incoming / f"{package}_{version}_{arch}.deb")

    def build(self):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(["bash", SCRIPT, str(self.incoming), str(self.out)], env=env, capture_output=True,
                              text=True, timeout=120)

    def index(self, suite, arch):
        path = self.out / "dists" / suite / "main" / f"binary-{arch}" / "Packages"
        return path.read_text() if path.exists() else ""

    def pool(self, suite):
        path = self.out / "pool" / suite / "main" / "n" / CORE
        return sorted(os.listdir(path)) if path.exists() else []


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


class TestPlasma6ForKdeNeon:
    def test_it_goes_to_the_suite_noble_and_the_amd64_index(self, repo):
        repo.add(CORE, "1.5.0-1~noble1")
        repo.add(PLASMA6, "1.5.0-1~noble1")

        result = repo.build()

        assert result.returncode == 0, result.stderr + result.stdout
        assert f"Package: {PLASMA6}\n" in repo.index("noble", "amd64")
        assert f"{PLASMA6}_1.5.0-1~noble1_amd64.deb" in repo.pool("noble")

    def test_it_is_in_no_other_suite_and_not_in_the_arm64_index(self, repo):
        repo.add(CORE, "1.5.0-1~noble1")
        repo.add(CORE, "1.5.0-1~noble1", "arm64")
        repo.add(CORE, "1.5.0-1~resolute1")
        repo.add(PLASMA6, "1.5.0-1~noble1")

        result = repo.build()

        assert result.returncode == 0, result.stderr + result.stdout
        assert PLASMA6 not in repo.index("noble", "arm64")
        for suite in ("jammy", "oracular", "resolute"):
            assert PLASMA6 not in repo.index(suite, "amd64"), suite
            assert PLASMA6 not in repo.index(suite, "arm64"), suite
            assert not [n for n in repo.pool(suite) if PLASMA6 in n], suite

    def test_the_suite_comes_from_the_version_and_not_from_the_name_of_the_package(self, repo):
        repo.add(PLASMA6, "1.5.0-1~resolute1")

        result = repo.build()

        assert result.returncode == 0, result.stderr + result.stdout
        assert f"Package: {PLASMA6}\n" in repo.index("resolute", "amd64")
        assert PLASMA6 not in repo.index("noble", "amd64")

    def test_a_pull_request_build_of_it_is_not_part_of_the_apt_repository(self, repo):
        # its version ends in +pr<PR>.<run>: the test packages of a pull request have their own release
        repo.add(PLASMA6, "1.5.0-1~noble1+pr31.57")

        result = repo.build()

        assert result.returncode != 0
        assert "cannot tell which Ubuntu release" in result.stderr
        assert not (repo.out / "dists" / "noble" / "Release").exists()

    @pytest.mark.parametrize("version", ["1.5.0-1", "1.5.0-1~focal1", "1.5.0-1+noble"])
    def test_a_version_without_a_known_codename_stops_the_script(self, repo, version):
        repo.add(PLASMA6, version)

        result = repo.build()

        assert result.returncode != 0
        assert "cannot tell which Ubuntu release" in result.stderr
        assert not (repo.out / "dists" / "noble" / "Release").exists()

    def test_an_architecture_the_repository_does_not_serve_stops_the_script(self, repo):
        repo.add(PLASMA6, "1.5.0-1~noble1", "i386")

        result = repo.build()

        assert result.returncode != 0
        assert "unsupported architecture 'i386'" in result.stderr

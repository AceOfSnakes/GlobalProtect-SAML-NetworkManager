"""
Tests for .github/scripts/build-rpm-repo.sh, which builds the signed RPM
repositories (one per target of rpm/targets and architecture) from the RPM
assets of the releases, and finishes the landing page.

createrepo_c, rpmsign and gpg are fakes found through PATH: they record how
they were called and make the files the script looks for. The RPMs are empty
files with real names. The secret key is not part of any call: the fakes show
that only the key id, a fingerprint, reaches the arguments and that the keyring
is found through GNUPGHOME.

Every positive test has negative counterparts: names that are not our
packages, source RPMs, dist tags that no target has, bad key ids, failing
tools and a missing list of targets stop the script or are skipped, and no
placeholder is left on the page whether there are RPMs or not.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "build-rpm-repo.sh")
TEMPLATE = os.path.join(ROOT, ".github", "pages", "index.html")
REAL_TARGETS = os.path.join(ROOT, "rpm", "targets")
BASE = "https://wmp.github.io/GlobalProtect-SAML-NetworkManager/rpm"
KEY = "0123456789ABCDEF0123456789ABCDEF01234567"
CORE = "network-manager-gpclient"

TARGETS = """# target image dist family
fedora44   registry.example/fedora:44      .fc44   fedora
el10       registry.example/alma:10        .el10   el
tumbleweed registry.example/tw:latest      .tw     suse
"""

FAKE_CREATEREPO = r"""#!/bin/bash
echo "createrepo_c $*" >> "$FAKE_DIR/calls"
[ -z "$FAKE_CREATEREPO_FAILS" ] || exit 1
dir="${@: -1}"
mkdir -p "$dir/repodata"
echo "<repomd/>" > "$dir/repodata/repomd.xml"
"""

FAKE_RPMSIGN = r"""#!/bin/bash
echo "rpmsign $*" >> "$FAKE_DIR/calls"
echo "GNUPGHOME=$GNUPGHOME" >> "$FAKE_DIR/rpmsign_env"
[ -z "$FAKE_RPMSIGN_FAILS" ] || exit 1
"""

FAKE_GPG = r"""#!/bin/bash
echo "gpg $*" >> "$FAKE_DIR/calls"
case " $* " in
    *" --detach-sign "*) file="${@: -1}"; echo "signature of $file" > "$file.asc" ;;
    *" --export "*) [ -n "$FAKE_NO_KEY" ] || echo "-----BEGIN PGP PUBLIC KEY BLOCK-----" ;;
esac
"""


def rpm_name(package, version="1.5.0", dist=".fc44", arch="x86_64", revision="1"):
    return f"{package}-{version}-{revision}{dist}.{arch}.rpm"


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.incoming = tmp_path / "incoming"
        self.out = tmp_path / "site" / "rpm"
        self.index = tmp_path / "site" / "index.html"
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.gnupg = tmp_path / "gnupg"
        for directory in (self.incoming, self.out.parent, self.fake, self.bin, self.gnupg):
            directory.mkdir()
        for name, body in (("createrepo_c", FAKE_CREATEREPO), ("rpmsign", FAKE_RPMSIGN), ("gpg", FAKE_GPG)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (tmp_path / "targets").write_text(TARGETS)
        self.write_index()

    def write_index(self):
        """The page as build-apt-repo.sh leaves it: only the RPM part is still to do"""
        with open(TEMPLATE, encoding="utf-8") as handle:
            text = handle.read()
        text = (text.replace("__VERSION_ROWS__", "<tr><td>noble</td><td>1.5.0-1~noble1</td></tr>")
                .replace("__FINGERPRINT__", KEY).replace("__UPDATED__", "now"))
        self.index.write_text(text)

    def add(self, *names):
        for name in names:
            (self.incoming / name).write_text(name)

    def add_set(self, dist=".fc44", arch="x86_64", version="1.5.0"):
        self.add(*[rpm_name(p, version, dist, arch) for p in (CORE, CORE + "-gnome", CORE + "-plasma")])

    def run(self, *args, key=KEY, targets=True, **extra_env):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_DIR": str(self.fake),
            "FAKE_CREATEREPO_FAILS": "",
            "FAKE_RPMSIGN_FAILS": "",
            "FAKE_NO_KEY": "",
            "GNUPGHOME": str(self.gnupg),
            "HOME": str(self.root),
        }
        if targets:
            env["RPM_TARGETS_FILE"] = str(self.root / "targets")
        env.update(extra_env)
        command = ["bash", SCRIPT, *args, str(self.incoming), str(self.out)]
        if key:
            command.append(key)
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        path = self.fake / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def page(self):
        return self.index.read_text()

    def tree(self):
        return sorted(
            os.path.relpath(os.path.join(d, f), self.out)
            for d, _dirs, files in os.walk(self.out) for f in files
        ) if self.out.exists() else []


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


class TestLayout:
    def test_a_repository_per_target_and_architecture(self, box):
        box.add_set(".fc44", "x86_64")
        box.add_set(".fc44", "aarch64")
        box.add_set(".el10", "x86_64")

        result = box.run("--index", str(box.index))

        assert result.returncode == 0, result.stderr + result.stdout
        tree = box.tree()
        for directory in ("fedora44/x86_64", "fedora44/aarch64", "el10/x86_64"):
            assert f"{directory}/repodata/repomd.xml" in tree
            assert f"{directory}/repodata/repomd.xml.asc" in tree
        assert rpm_name(CORE, dist=".fc44", arch="aarch64") not in os.listdir(box.out / "fedora44" / "x86_64")
        assert sorted(f for f in os.listdir(box.out / "fedora44" / "aarch64") if f.endswith(".rpm")) == sorted(
            rpm_name(p, arch="aarch64") for p in (CORE, CORE + "-gnome", CORE + "-plasma"))

    def test_a_target_without_packages_has_no_directory_and_no_repo_file(self, box):
        box.add_set(".fc44", "x86_64")

        result = box.run()

        assert result.returncode == 0, result.stderr
        assert sorted(os.listdir(box.out)) == ["RPM-GPG-KEY-gpclient", "fedora44"]
        assert not any("el10" in c or "tumbleweed" in c for c in box.calls())

    def test_an_architecture_without_packages_has_no_directory(self, box):
        box.add_set(".fc44", "aarch64")

        assert box.run().returncode == 0
        assert sorted(os.listdir(box.out / "fedora44")) == ["aarch64", "network-manager-gpclient.repo"]

    def test_the_dist_tag_alone_decides_the_target(self, box):
        box.add_set(".tw", "x86_64")

        assert box.run().returncode == 0
        assert os.path.isdir(box.out / "tumbleweed" / "x86_64")
        assert not os.path.exists(box.out / "fedora44")

    def test_the_output_is_built_from_scratch(self, box):
        (box.out).mkdir(parents=True)
        (box.out / "old").write_text("old")
        box.add_set()

        assert box.run().returncode == 0
        assert not (box.out / "old").exists()

    def test_the_real_list_of_targets_is_the_default(self, box):
        box.add_set(".fc44", "x86_64")
        assert os.path.exists(REAL_TARGETS), "rpm/targets is missing"

        result = box.run(targets=False)

        assert result.returncode == 0, result.stderr
        assert os.path.isdir(box.out / "fedora44" / "x86_64")

    @pytest.mark.parametrize("dist", [target.split()[2] for target in open(REAL_TARGETS).read().splitlines()
                                      if target.strip() and not target.startswith("#")]
                             if os.path.exists(REAL_TARGETS) else [])
    def test_every_target_of_rpm_targets_gets_its_repository(self, box, dist):
        box.add_set(dist, "x86_64")

        result = box.run(targets=False)

        assert result.returncode == 0, result.stderr
        assert len(os.listdir(box.out)) == 2  # the key and the target


class TestSigning:
    def test_every_rpm_is_signed_with_the_key_id_and_the_keyring_of_gnupghome(self, box):
        box.add_set(".fc44", "x86_64")
        box.add_set(".fc44", "aarch64")

        result = box.run()

        assert result.returncode == 0, result.stderr
        signs = [c for c in box.calls() if c.startswith("rpmsign ")]
        assert len(signs) == 2
        for call in signs:
            assert "--addsign" in call
            assert f"_gpg_name {KEY}" in call
            assert f"_gpg_path {box.gnupg}" in call
        signed = " ".join(signs)
        for name in os.listdir(box.incoming):
            assert name in signed or name.replace("x86_64", "x86_64") in signed
        assert (box.fake / "rpmsign_env").read_text().count(f"GNUPGHOME={box.gnupg}") == 2

    def test_the_packages_are_signed_before_the_metadata_is_made(self, box):
        box.add_set()

        assert box.run().returncode == 0
        kinds = [c.split()[0] for c in box.calls()]
        assert kinds.index("rpmsign") < kinds.index("createrepo_c") < kinds.index("gpg")

    def test_the_metadata_has_a_detached_armored_signature_and_the_key_is_published(self, box):
        box.add_set()

        assert box.run().returncode == 0
        gpg = [c for c in box.calls() if c.startswith("gpg ")]
        assert any("--detach-sign" in c and "--armor" in c and f"--local-user {KEY}" in c
                   and c.endswith("fedora44/x86_64/repodata/repomd.xml") for c in gpg)
        key = (box.out / "RPM-GPG-KEY-gpclient").read_text()
        assert key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
        assert any("--export" in c and "--armor" in c and c.endswith(KEY) for c in gpg)

    def test_nothing_secret_is_an_argument(self, box):
        box.add_set()

        assert box.run().returncode == 0
        for call in box.calls():
            for word in ("--export-secret", "--passphrase", "--import", "PRIVATE"):
                assert word not in call

    def test_the_repo_file_checks_packages_and_metadata(self, box):
        box.add_set()

        assert box.run().returncode == 0
        repo = (box.out / "fedora44" / "network-manager-gpclient.repo").read_text().splitlines()
        assert "[network-manager-gpclient]" in repo
        assert f"baseurl={BASE}/fedora44/$basearch" in repo
        assert "gpgcheck=1" in repo and "repo_gpgcheck=1" in repo
        assert f"gpgkey={BASE}/RPM-GPG-KEY-gpclient" in repo
        assert "enabled=1" in repo

    def test_the_base_url_can_be_set(self, box):
        box.add_set()

        assert box.run(BASE_URL="https://example.org/pkgs").returncode == 0
        repo = (box.out / "fedora44" / "network-manager-gpclient.repo").read_text()
        assert "baseurl=https://example.org/pkgs/fedora44/$basearch" in repo
        assert "gpgkey=https://example.org/pkgs/RPM-GPG-KEY-gpclient" in repo

    def test_without_a_key_nothing_is_signed_and_the_repo_file_says_so(self, box):
        box.add_set()

        result = box.run(key="")

        assert result.returncode == 0, result.stderr
        assert not [c for c in box.calls() if c.startswith(("rpmsign", "gpg"))]
        assert not any(name.endswith(".asc") or name == "RPM-GPG-KEY-gpclient" for name in box.tree())
        repo = (box.out / "fedora44" / "network-manager-gpclient.repo").read_text()
        assert "gpgcheck=0" in repo and "repo_gpgcheck=0" in repo and "gpgkey" not in repo
        assert "unsigned" in result.stdout

    @pytest.mark.parametrize("key", ["abc;rm -rf x", "$(id)", "XYZ12345", "0123", "key id", "A" * 65])
    def test_a_key_id_that_is_not_hex_is_refused_before_any_tool_runs(self, box, key):
        box.add_set()

        result = box.run(key=key)

        assert result.returncode != 0
        assert "key id" in result.stderr
        assert box.calls() == []
        assert not box.out.exists()

    def test_a_failing_rpmsign_stops_the_script(self, box):
        box.add_set()

        result = box.run(FAKE_RPMSIGN_FAILS="1")

        assert result.returncode != 0
        assert not [c for c in box.calls() if c.startswith("createrepo_c")]

    def test_a_failing_createrepo_stops_the_script(self, box):
        box.add_set()

        result = box.run(FAKE_CREATEREPO_FAILS="1")

        assert result.returncode != 0
        assert not (box.out / "RPM-GPG-KEY-gpclient").exists()

    def test_a_key_that_exports_nothing_is_an_error(self, box):
        box.add_set()

        result = box.run(FAKE_NO_KEY="1")

        assert result.returncode != 0
        assert "no public key" in result.stderr


class TestWhichFilesCount:
    def test_source_rpms_are_skipped(self, box):
        box.add_set()
        box.add(f"{CORE}-1.5.0-1.fc44.src.rpm")

        result = box.run()

        assert result.returncode == 0, result.stderr
        assert not any(name.endswith(".src.rpm") for name in box.tree())
        assert "skipping the source package" in result.stdout
        assert "src.rpm" not in " ".join(c for c in box.calls() if c.startswith("rpmsign"))

    @pytest.mark.parametrize("name", [
        f"{CORE}-debuginfo-1.5.0-1.fc44.x86_64.rpm",
        f"{CORE}-debugsource-1.5.0-1.fc44.x86_64.rpm",
        f"{CORE}-plasma-6-1.5.0-1.fc44.x86_64.rpm",
        f"{CORE}-1.5.0-1.fc44.i686.rpm",
        f"{CORE}-1.5.0-1.fc44.noarch.rpm",
        f"{CORE}-1.5.0-1.x86_64.rpm",
        f"{CORE}-1.5.0-1.fc44.pr24.57.x86_64.rpm",
        f"{CORE}-v1-1.fc44.x86_64.rpm",
        "other-1.5.0-1.fc44.x86_64.rpm",
        "notes.rpm",
    ])
    def test_a_name_that_is_not_one_of_our_packages_stops_the_script(self, box, name):
        box.add_set()
        box.add(name)

        result = box.run()

        assert result.returncode != 0
        assert "unexpected name of an RPM" in result.stderr
        assert box.calls() == []
        assert not box.out.exists()

    def test_other_files_are_not_looked_at(self, box):
        box.add_set()
        box.add("a.deb", "README.txt", f"{CORE}-1.5.0-1.fc44.x86_64.rpm.sig")

        result = box.run()

        assert result.returncode == 0, result.stderr
        assert not any(n.endswith((".deb", ".txt", ".sig")) for n in box.tree())

    def test_a_dist_tag_without_a_target_is_skipped_with_a_warning(self, box):
        box.add_set(".fc44")
        box.add_set(".fc30")

        result = box.run()

        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout and ".fc30" in result.stdout
        assert not any(".fc30" in name for name in box.tree())
        assert not any(".fc30" in c for c in box.calls())

    def test_only_unknown_dist_tags_is_an_error(self, box):
        box.add_set(".fc30")

        result = box.run()

        assert result.returncode != 0
        assert "no binary RPM files" in result.stderr
        assert not box.out.exists()

    def test_no_rpm_at_all_is_an_error(self, box):
        result = box.run()

        assert result.returncode != 0
        assert "no binary RPM files" in result.stderr
        assert not box.out.exists()

    def test_only_a_source_rpm_is_an_error(self, box):
        box.add(f"{CORE}-1.5.0-1.fc44.src.rpm")

        result = box.run()

        assert result.returncode != 0
        assert "no binary RPM files" in result.stderr

    def test_a_missing_incoming_directory_is_an_error(self, box):
        subprocess_result = subprocess.run(
            ["bash", SCRIPT, str(box.root / "nope"), str(box.out)], capture_output=True, text=True,
            env={"PATH": f"{box.bin}:/usr/bin:/bin", "RPM_TARGETS_FILE": str(box.root / "targets")})

        assert subprocess_result.returncode != 0
        assert "does not exist" in subprocess_result.stderr

    def test_a_missing_list_of_targets_is_an_error(self, box):
        box.add_set()

        result = box.run(RPM_TARGETS_FILE=str(box.root / "nope"))

        assert result.returncode != 0
        assert "list of targets does not exist" in result.stderr
        assert box.calls() == []

    @pytest.mark.parametrize("content", ["", "# only a comment\n", "fedora44 image\n", "bad;name image .fc44 fedora\n"])
    def test_a_broken_list_of_targets_is_an_error(self, box, content):
        box.add_set()
        (box.root / "targets").write_text(content)

        result = box.run()

        assert result.returncode != 0
        assert box.calls() == []

    def test_an_unknown_option_is_refused(self, box):
        result = box.run("--nope")

        assert result.returncode != 0
        assert "unknown option" in result.stderr
        assert box.calls() == []

    @pytest.mark.parametrize("args, message", [(["--index"], "needs a file"), ([], "usage"), (["one"], "usage"),
                                               (["a", "b", "c", "d"], "usage")])
    def test_the_wrong_number_of_arguments_is_refused(self, box, args, message):
        result = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                                env={"PATH": f"{box.bin}:/usr/bin:/bin", "RPM_TARGETS_FILE": str(box.root / "targets")})

        assert result.returncode != 0
        assert message in result.stderr

    def test_a_missing_index_file_is_an_error(self, box):
        box.add_set()

        result = box.run("--index", str(box.root / "nope.html"))

        assert result.returncode != 0
        assert "index file does not exist" in result.stderr
        assert box.calls() == []


class TestLandingPage:
    def placeholders(self, text):
        return re.findall(r"__[A-Z_]+__", text)

    def test_the_rows_are_the_newest_core_version_per_target(self, box):
        box.add_set(".fc44", version="1.9.0")
        box.add_set(".fc44", version="1.10.0")
        box.add_set(".el10", version="1.2.0")

        result = box.run("--index", str(box.index))

        assert result.returncode == 0, result.stderr
        page = box.page()
        assert "<tr><td>Fedora 44</td><td><code>fedora44</code></td><td><code>1.10.0</code></td></tr>" in page
        assert "<tr><td>EL 10</td><td><code>el10</code></td><td><code>1.2.0</code></td></tr>" in page
        assert "tumbleweed</code></td><td>" not in page
        assert "1.9.0" not in page

    def test_no_placeholder_and_no_marker_is_left(self, box):
        box.add_set()

        assert box.run("--index", str(box.index)).returncode == 0
        page = box.page()
        assert self.placeholders(page) == []
        assert "RPM-BEGIN" not in page and "RPM-END" not in page
        assert "dnf install" in page and "zypper" in page and "RPM-GPG-KEY-gpclient" in page

    def test_without_rpms_the_page_has_no_rpm_part_and_no_placeholder(self, box):
        result = box.run("--index", str(box.index), "--allow-empty")

        assert result.returncode == 0, result.stderr
        page = box.page()
        assert self.placeholders(page) == []
        assert "RPM-BEGIN" not in page and "RPM-END" not in page
        for text in ("dnf", "zypper", "RPM", "rpm", "Fedora"):
            assert text not in page, text
        assert "apt install" in page and "noble" in page
        assert not box.out.exists()
        assert box.calls() == []

    def test_without_rpms_and_without_allow_empty_the_page_is_not_touched(self, box):
        before = box.page()

        result = box.run("--index", str(box.index))

        assert result.returncode != 0
        assert box.page() == before

    def test_rpms_of_unknown_dist_tags_only_leave_no_rpm_part_with_allow_empty(self, box):
        box.add_set(".fc30")

        result = box.run("--index", str(box.index), "--allow-empty")

        assert result.returncode == 0, result.stderr
        assert "dnf" not in box.page() and self.placeholders(box.page()) == []

    def test_the_apt_part_of_the_page_is_not_changed_by_the_rpm_part(self, box):
        box.add_set()
        with_rpm = box.run("--index", str(box.index))
        assert with_rpm.returncode == 0
        page = box.page()

        assert "<tr><td>noble</td><td>1.5.0-1~noble1</td></tr>" in page
        assert KEY in page
        assert "gpclient-archive-keyring.gpg" in page

    def test_the_template_has_balanced_rpm_markers_and_one_placeholder(self):
        with open(TEMPLATE, encoding="utf-8") as handle:
            text = handle.read()

        assert text.count("<!-- RPM-BEGIN -->") == text.count("<!-- RPM-END -->") >= 1
        assert text.count("__RPM_VERSION_ROWS__") == 1
        # begin and end in turn, never nested or reversed
        marks = re.findall(r"<!-- RPM-(BEGIN|END) -->", text)
        assert marks == ["BEGIN", "END"] * (len(marks) // 2)
        assert text.index("__RPM_VERSION_ROWS__") > text.index("<!-- RPM-BEGIN -->")

    def test_without_an_index_option_the_page_is_not_looked_at(self, box):
        box.add_set()
        before = box.page()

        assert box.run().returncode == 0
        assert box.page() == before

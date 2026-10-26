"""
Checks for GitHub issue 54: the docs find the suite of the apt repository with the codename of
/etc/os-release (UBUNTU_CODENAME, which derivatives of Ubuntu set), not with `lsb_release -cs`;
publish-apt.yml does not cap the list of releases; every job of every workflow has timeout-minutes.

Every check has a negative counterpart: a synthetic text that breaks the rule is rejected.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import glob
import os
import re

import pytest

from workflow_steps import WORKFLOWS, read_workflow

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CODENAME = '$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")'
DOCS = ["README.md", "docs/APT_REPO.md"]


def codename_problems(text):
    return ["lsb_release -cs"] if "lsb_release -cs" in text else []


def limit_problems(text):
    return re.findall(r"gh release list[^\n]*--limit \d+", text)


def jobs_without_timeout(text):
    """Names of the jobs that run steps (not a call of a reusable workflow) and have no timeout-minutes"""
    lines = text.splitlines()
    start = lines.index("jobs:")
    jobs, current = {}, None
    for line in lines[start + 1:]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = jobs.setdefault(match.group(1), [])
        elif current is not None:
            current.append(line)
    return [name for name, body in jobs.items()
            if not any(x.startswith(("    timeout-minutes: ", "    uses: ")) for x in body)]


class TestCodename:
    @pytest.mark.parametrize("doc", DOCS)
    def test_the_docs_do_not_use_lsb_release(self, doc):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
            assert codename_problems(handle.read()) == []

    @pytest.mark.parametrize("doc", DOCS)
    def test_the_docs_use_the_os_release_codename(self, doc):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
            assert CODENAME in handle.read()

    def test_lsb_release_is_rejected(self):
        assert codename_problems("deb ... $(lsb_release -cs) main")

    def test_text_without_it_is_accepted(self):
        assert codename_problems("deb ... " + CODENAME + " main") == []


INSTALL_DOCS = DOCS + [".github/pages/index.html"]
OLD_STEPS = ["| sudo tee", "sources.list.d/gpclient.list\n", "/usr/share/keyrings/gpclient-archive-keyring.gpg]"]
OLD_FILES = "/etc/apt/sources.list.d/gpclient.list /usr/share/keyrings/gpclient-archive-keyring.gpg"
NEW_FILES = "/etc/apt/sources.list.d/gpclient.sources /etc/apt/keyrings/gpclient-archive-keyring.gpg"


def install_section(doc):
    """The apt install commands: from the key download to `sudo apt update`"""
    with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
        return install_commands(handle.read())


def removal_block(doc):
    """The commands from `sudo apt remove network-manager-gpclient` to `sudo apt update`"""
    with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("sudo apt remove network-manager-gpclient\n")
    return text[start:text.index("sudo apt update", start)]


def install_commands(text):
    start = text.rfind("\n", 0, text.index("curl -fsSL")) + 1
    return text[start:text.index("sudo apt update", start)]


class TestAptInstallSteps:
    @pytest.mark.parametrize("doc", INSTALL_DOCS)
    @pytest.mark.parametrize("needed", [
        "sudo curl -fsSLo /etc/apt/keyrings/gpclient-archive-keyring.gpg",
        "/etc/apt/sources.list.d/gpclient.sources",
        "Types: deb", "Suites:", "Components: main",
        "Signed-By: /etc/apt/keyrings/gpclient-archive-keyring.gpg",
    ])
    def test_the_install_steps_use_a_deb822_file(self, doc, needed):
        assert needed in install_section(doc)

    @pytest.mark.parametrize("doc", INSTALL_DOCS)
    @pytest.mark.parametrize("old", OLD_STEPS + ["lsb_release", "gpclient.list", "signed-by=", "deb ["])
    def test_the_install_steps_do_not_use_the_old_way(self, doc, old):
        assert old not in install_section(doc)

    def test_the_old_way_is_detected(self, tmp_path):
        old = tmp_path / "old.md"
        old.write_text("curl -fsSL URL \\\n  | sudo tee /usr/share/keyrings/k.gpg\n"
                       "echo deb | sudo tee /etc/apt/sources.list.d/gpclient.list\n\nsudo apt update\n")
        section = install_section(str(old))
        assert [step for step in OLD_STEPS if step in section] == ["| sudo tee", "sources.list.d/gpclient.list\n"]

    def test_the_deb822_snippet_has_the_expected_fields(self):
        text = install_section("README.md")
        body = text[text.index("Types:"):text.index("\nEOF")]
        fields = [line.split(":", 1)[0] for line in body.splitlines()]
        assert fields == ["Types", "URIs", "Suites", "Components", "Architectures", "Signed-By"]

    @pytest.mark.parametrize("doc", INSTALL_DOCS)
    def test_the_old_files_are_deleted_before_the_new_ones_are_written(self, doc):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
            text = handle.read()
        cleanup = "# old one-line setup (before 2.0.0), if present\nsudo rm -f " + OLD_FILES + "\n"
        assert cleanup in text
        assert text.index(cleanup) < text.index("sudo curl -fsSLo") < text.index("gpclient.sources")

    @pytest.mark.parametrize("doc", ["docs/APT_REPO.md", ".github/pages/index.html"])
    def test_the_removal_block_removes_the_new_files(self, doc):
        block = removal_block(doc)
        assert "sudo rm " + NEW_FILES + "\n" in block
        assert "gpclient.list" not in block
        assert "/usr/share/keyrings" not in block

    def test_a_removal_block_of_the_old_files_is_detected(self, tmp_path):
        old = tmp_path / "old.md"
        old.write_text("sudo apt remove network-manager-gpclient\nsudo rm " + OLD_FILES + "\nsudo apt update\n")
        block = removal_block(str(old))
        assert "gpclient.list" in block and "sudo rm " + NEW_FILES not in block


class TestReleaseList:
    def test_publish_apt_has_no_fixed_limit(self):
        assert limit_problems(read_workflow("publish-apt.yml")) == []

    def test_a_fixed_limit_is_rejected(self):
        assert limit_problems("gh release list --limit 100 --json tagName") != []

    def test_the_release_list_is_paginated_and_still_filtered(self):
        text = read_workflow("publish-apt.yml")
        assert "gh api --paginate" in text
        assert ".draft == false and .prerelease == false" in text


class TestTimeouts:
    @pytest.mark.parametrize("workflow", sorted(os.path.basename(p) for p in glob.glob(os.path.join(WORKFLOWS, "*.yml"))))
    def test_every_job_has_a_timeout(self, workflow):
        assert jobs_without_timeout(read_workflow(workflow)) == []

    def test_a_job_without_a_timeout_is_rejected(self):
        text = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
        assert jobs_without_timeout(text) == ["a"]

    def test_a_job_with_a_timeout_or_a_workflow_call_is_accepted(self):
        text = ("jobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: x\n"
                "  b:\n    uses: ./.github/workflows/x.yml\n")
        assert jobs_without_timeout(text) == []

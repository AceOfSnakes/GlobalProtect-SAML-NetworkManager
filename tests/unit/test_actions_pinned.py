"""
Every third-party action is pinned to a full commit SHA with the release tag in
a trailing comment, and Dependabot keeps the pins up to date (issue #54). No
YAML parser is needed (CI installs only pytest): the files are read as text.
"""

import glob
import os
import re

import pytest

import workflow_steps

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEPENDABOT = os.path.join(ROOT, ".github", "dependabot.yml")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+# v\d+(\.\d+)*\s*$")


def uses_problems(text):
    """The `uses:` lines of a workflow that are not local, docker:// or pinned with a version comment"""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.match(line)
        if not match:
            continue
        ref, rest = match.groups()
        ref = ref.strip("'\"")
        if ref.startswith(("./", "docker://")):
            continue
        if not PINNED.match(ref):
            problems.append(f"{number}: not pinned to a 40-hex SHA: {ref}")
        elif not VERSION_COMMENT.match(rest):
            problems.append(f"{number}: no '# vX.Y.Z' comment: {ref}")
    return problems


def dependabot_covers_actions(text):
    """True when dependabot.yml has a github-actions update for directory "/" (minimal text parse)"""
    for block in re.split(r"(?m)^  - ", text)[1:]:
        if re.search(r"(?m)^package-ecosystem:\s*github-actions\s*$", block) and re.search(
            r"""(?m)^\s+directory:\s*["']/["']\s*$""", block
        ):
            return True
    return False


WORKFLOW_FILES = sorted(os.path.basename(path) for path in glob.glob(os.path.join(workflow_steps.WORKFLOWS, "*.yml")))


@pytest.mark.parametrize("workflow", WORKFLOW_FILES)
def test_every_action_is_pinned_to_a_commit_sha(workflow):
    assert uses_problems(workflow_steps.read_workflow(workflow)) == []


def test_workflows_use_at_least_one_pinned_action():
    assert any("@" in line for line in workflow_steps.read_workflow("tests.yml").splitlines() if "uses:" in line)


SHA = "11d5960a326750d5838078e36cf38b85af677262"


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "        uses: actions/checkout@v4 # v4.4.0",
        f"        uses: actions/checkout@{SHA[:7]} # v4.4.0",
        f"        uses: actions/checkout@{SHA[:39]} # v4.4.0",
        f"        uses: actions/checkout@{SHA}",
        f"        uses: actions/checkout@{SHA} # latest",
        f"        uses: actions/checkout@{SHA.upper()} # v4.4.0",
        "        uses: softprops/action-gh-release@main # v2.6.2",
    ],
)
def test_unpinned_action_is_reported(line):
    assert len(uses_problems(f"steps:\n{line}\n")) == 1


@pytest.mark.parametrize(
    "line",
    [
        f"        uses: actions/checkout@{SHA} # v4.4.0",
        f"      - uses: actions/checkout@{SHA} # v4",
        "        uses: ./.github/actions/local",
        "        uses: docker://alpine:3.20",
    ],
)
def test_pinned_local_and_docker_actions_are_accepted(line):
    assert uses_problems(f"steps:\n{line}\n") == []


def test_dependabot_updates_github_actions_weekly_in_one_group():
    with open(DEPENDABOT, encoding="utf-8") as handle:
        text = handle.read()
    assert dependabot_covers_actions(text)
    assert re.search(r"(?m)^\s+interval:\s*weekly\s*$", text)
    assert re.search(r"(?m)^\s+groups:\s*$", text)
    assert re.search(r"""(?m)^\s+prefix:\s*["']?CI["']?\s*$""", text)


@pytest.mark.parametrize(
    "text",
    [
        "version: 2\nupdates: []\n",
        'version: 2\nupdates:\n  - package-ecosystem: pip\n    directory: "/"\n',
        'version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: "/docs"\n',
    ],
)
def test_dependabot_without_actions_at_root_is_reported(text):
    assert not dependabot_covers_actions(text)

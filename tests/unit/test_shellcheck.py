"""
Tests for the shellcheck job: .github/workflows/tests.yml runs `make shellcheck`, and the
SHELL_SCRIPTS lists of the Makefile name every shell script of the repository (a file with a
sh/bash shebang, outside external/), so a new script cannot skip the check.

`workflow_problems` and `missing_scripts` are the checkers; the repository as it is has no
problems, and every modified copy below is rejected.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import fnmatch
import shutil
import subprocess

import pytest

from workflow_steps import read_workflow, step_lines, step_names

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JOB = "  shellcheck:"
STEP = "Run shellcheck"
SHEBANG = re.compile(rb"^#!\s*(/usr)?/bin/(env\s+)?(ba|da)?sh\b")


def workflow_problems(text):
    """What is wrong with the shellcheck job of the workflow text"""
    lines = text.splitlines()
    if JOB not in lines:
        return ["no job shellcheck"]
    job = lines[lines.index(JOB) + 1:]
    for number, line in enumerate(job):
        if line and not line.startswith("   "):
            job = job[:number]
            break
    if "    runs-on: ubuntu-latest" not in job:
        return ["the shellcheck job does not run on ubuntu-latest"]
    if STEP not in step_names("tests.yml", text):
        return [f"no step {STEP!r}"]
    if "        run: make shellcheck" not in step_lines("tests.yml", STEP, text):
        return ["the step does not run make shellcheck"]
    return []


# Not tracked by git: what a walk of a copy without .git skips (see .gitignore)
UNTRACKED_DIRS = (".git", ".claude", "external", "vendor", "output", "tmp", "build", "__pycache__", "debian/.debhelper")
UNTRACKED_DIR_PATTERNS = ("debian/network-manager-gpclient*",)


def tracked_files(root):
    """The files git tracks under root, or, in a copy without .git (the non-root
    run of AGENTS.md), every file outside the untracked directories"""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if result.returncode == 0:
        return [name for name in result.stdout.decode().split("\0") if name]
    found = []
    for directory, dirs, files in os.walk(root):
        relative = os.path.relpath(directory, root)
        relative = "" if relative == "." else relative + "/"
        dirs[:] = [
            name for name in dirs
            if name not in UNTRACKED_DIRS
            and (relative + name) not in UNTRACKED_DIRS
            and not any(fnmatch.fnmatch(relative + name, pattern) for pattern in UNTRACKED_DIR_PATTERNS)
        ]
        found += [relative + name for name in files]
    return found


def repository_scripts(root=ROOT):
    """The tracked files with a sh/bash shebang, outside external/"""
    found = []
    for name in tracked_files(root):
        path = os.path.join(root, name)
        if name.startswith("external/") or not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            if SHEBANG.match(handle.readline()):
                found.append(name)
    return sorted(found)


def listed_scripts(makefile):
    """The files of the Makefile variables SHELL_SCRIPTS and SHELL_SCRIPTS_VENDORED"""
    found = []
    for variable in ("SHELL_SCRIPTS", "SHELL_SCRIPTS_VENDORED"):
        match = re.search(rf"^{variable} :=((?: *\\\n|[^\n])*)", makefile, re.M)
        if match:
            found += match.group(1).replace("\\\n", " ").split()
    return found


def missing_scripts(scripts, makefile):
    """The scripts that the Makefile does not list"""
    listed = set(listed_scripts(makefile))
    return [name for name in scripts if name not in listed]


@pytest.fixture(name="makefile")
def makefile_text():
    with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as handle:
        return handle.read()


def test_the_workflow_runs_make_shellcheck():
    assert workflow_problems(read_workflow("tests.yml")) == []


def test_a_workflow_without_the_job_is_rejected():
    text = read_workflow("tests.yml")
    assert workflow_problems(text[:text.index(JOB)]) == ["no job shellcheck"]


@pytest.mark.parametrize("old,new", [
    ("        run: make shellcheck", "        run: echo skipped"),
    ("      - name: Run shellcheck", "      - name: Lint"),
    ("    name: shellcheck\n    runs-on: ubuntu-latest", "    name: shellcheck\n    runs-on: macos-latest"),
])
def test_a_job_that_does_not_run_make_shellcheck_is_rejected(old, new):
    text = read_workflow("tests.yml")
    assert old in text
    assert workflow_problems(text.replace(old, new)) != []


def test_the_makefile_target_runs_shellcheck_at_warning_level(makefile):
    assert re.search(r"^shellcheck:\n\tshellcheck --severity=warning \$\(SHELL_SCRIPTS\)$", makefile, re.M)


def test_the_makefile_lists_every_shell_script(makefile):
    scripts = repository_scripts()
    assert len(scripts) > 20
    assert missing_scripts(scripts, makefile) == []


def test_every_listed_file_exists(makefile):
    assert [name for name in listed_scripts(makefile) if not os.path.isfile(os.path.join(ROOT, name))] == []


def test_a_new_script_missing_from_the_list_is_reported(makefile):
    assert missing_scripts(repository_scripts() + ["scripts/new-script.sh"], makefile) == ["scripts/new-script.sh"]


def test_dropping_a_script_from_the_list_is_reported(makefile):
    assert missing_scripts(repository_scripts(), makefile.replace("\tscripts/edge-wrapper.sh \\\n", "")) == [
        "scripts/edge-wrapper.sh"]


@pytest.mark.parametrize("line", [b"#!/usr/bin/python3\n", b"#!/usr/bin/env python3\n", b"# bash\n", b"echo\n"])
def test_other_files_are_not_shell_scripts(line):
    assert not SHEBANG.match(line)


@pytest.mark.parametrize("line", [b"#!/bin/sh\n", b"#!/bin/bash\n", b"#!/usr/bin/env bash\n"])
def test_shell_shebangs_are_found(line):
    assert SHEBANG.match(line)


def _copy_without_git(tmp_path):
    """A copy of the repository's tracked files without .git, plus build output"""
    copy = tmp_path / "copy"
    for name in tracked_files(ROOT):
        if name.startswith("external/") or not os.path.isfile(os.path.join(ROOT, name)):
            continue
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(ROOT, name), target)
    return copy


def test_a_copy_without_git_finds_the_same_scripts(tmp_path):
    copy = _copy_without_git(tmp_path)
    assert not (copy / ".git").exists()
    assert repository_scripts(str(copy)) == repository_scripts()


@pytest.mark.parametrize("directory", [
    "output/noble", "build", "plugins/plasma/build", "debian/network-manager-gpclient/usr/bin",
    "debian/.debhelper/x", ".claude/hooks", "tmp", "external/GlobalProtect-openconnect", "vendor/crates/x",
])
def test_a_copy_without_git_skips_build_and_untracked_dirs(tmp_path, directory):
    copy = _copy_without_git(tmp_path)
    script = copy / directory / "generated.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\necho\n")
    assert repository_scripts(str(copy)) == repository_scripts()


def test_a_copy_without_git_finds_a_new_script(tmp_path):
    copy = _copy_without_git(tmp_path)
    (copy / "scripts" / "new-script.sh").write_text("#!/bin/bash\necho\n")
    assert "scripts/new-script.sh" in repository_scripts(str(copy))

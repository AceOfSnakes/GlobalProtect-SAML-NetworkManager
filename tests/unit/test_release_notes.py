"""
Tests for .github/scripts/release_notes.py, which writes the table of download
links at the top of a release's notes: a row per Ubuntu release, a column per
architecture, the packages of the pair in the cell.

Every positive test has negative counterparts: bad input (repo, tag, file
names, a version without the codename, no packages) ends with "error: ..." on
stderr, exit status 1 and nothing on stdout, so the release never gets notes
with a table that silently lacks files.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import subprocess
import sys

import pytest
import workflow_steps

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".github", "scripts", "release_notes.py"))
REPO = "WMP/GlobalProtect-SAML-NetworkManager"
TAG = "v1.4.2"
VERSION = "1.4.2-1"
CODENAMES = {"jammy": "22.04", "noble": "24.04", "oracular": "24.10", "resolute": "26.04"}
PACKAGES = [
    "network-manager-gpclient",
    "network-manager-gpclient-gnome",
    "network-manager-gpclient-plasma-5",
    "network-manager-gpclient-plasma-6",
]
BASE = "https://github.com/%s/releases/download/%s" % (REPO, TAG)


def deb(package, codename, arch, version=VERSION):
    return "%s_%s~%s1_%s.deb" % (package, version, codename, arch)


def full_matrix():
    return [deb(p, c, a) for c in CODENAMES for a in ("amd64", "arm64") for p in PACKAGES]


def link(package, codename, arch):
    short = "gpclient" if package == PACKAGES[0] else package[len(PACKAGES[0]) + 1:]
    asset = deb(package, codename, arch).replace("~", ".")
    return "[%s](%s/%s)" % (short, BASE, asset)


def make_dir(tmp_path, names):
    directory = tmp_path / "release-files"
    directory.mkdir()
    for name in names:
        (directory / name).write_bytes(b"")
    return str(directory)


def run(directory, repo=REPO, tag=TAG):
    return subprocess.run(
        [sys.executable, SCRIPT, "--repo", repo, "--tag", tag, "--debs-dir", directory],
        capture_output=True, text=True, check=False,
    )


def table_rows(output):
    return [line for line in output.splitlines() if line.startswith("|")]


def test_full_matrix_rows_columns_and_links(tmp_path):
    result = run(make_dir(tmp_path, full_matrix()))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("## Downloads\n")
    assert result.stdout.endswith("\n")
    rows = table_rows(result.stdout)
    assert rows[0] == "| Ubuntu | amd64 | arm64 |"
    assert rows[1] == "|---|---|---|"
    expected = []
    for codename, number in CODENAMES.items():  # known releases in version order
        cells = ["<br>".join(link(p, codename, a) for p in PACKAGES) for a in ("amd64", "arm64")]
        expected.append("| Ubuntu %s (%s) | %s | %s |" % (number, codename, cells[0], cells[1]))
    assert rows[2:] == expected


def test_assets_are_linked_by_the_name_github_gives_them(tmp_path):
    result = run(make_dir(tmp_path, [deb(PACKAGES[0], "noble", "amd64")]))
    assert result.returncode == 0, result.stderr
    assert "%s/network-manager-gpclient_1.4.2-1.noble1_amd64.deb" % BASE in result.stdout
    assert "~" not in result.stdout


def test_text_of_the_notes_points_to_apt_and_the_install_command(tmp_path):
    out = run(make_dir(tmp_path, full_matrix())).stdout
    assert "https://github.com/%s/blob/%s/docs/APT_REPO.md" % (REPO, TAG) in out
    assert "sudo apt install ./network-manager-gpclient_*.deb ./network-manager-gpclient-gnome_*.deb" in out


def test_packages_in_a_cell_follow_the_fixed_order(tmp_path):
    names = [deb(p, "noble", "amd64") for p in
             ["zzz-extra", PACKAGES[3], PACKAGES[1], "aaa-extra", PACKAGES[0], PACKAGES[2]]]
    row = table_rows(run(make_dir(tmp_path, names)).stdout)[2]
    texts = re.findall(r"\[([^\]]+)\]", row)
    assert texts == ["gpclient", "gnome", "plasma-5", "plasma-6", "aaa-extra", "zzz-extra"]


def test_only_amd64_gives_a_single_column(tmp_path):
    names = [deb(p, "noble", "amd64") for p in PACKAGES]
    rows = table_rows(run(make_dir(tmp_path, names)).stdout)
    assert rows[0] == "| Ubuntu | amd64 |"
    assert rows[1] == "|---|---|"
    assert "arm64" not in "\n".join(rows)


def test_missing_cell_is_a_dash_and_other_architectures_sort_after_amd64(tmp_path):
    names = [deb(PACKAGES[0], "noble", "arm64"), deb(PACKAGES[0], "noble", "riscv64"),
             deb(PACKAGES[0], "jammy", "amd64")]
    rows = table_rows(run(make_dir(tmp_path, names)).stdout)
    assert rows[0] == "| Ubuntu | amd64 | arm64 | riscv64 |"
    assert rows[2].startswith("| Ubuntu 22.04 (jammy) | [gpclient]")
    assert rows[2].endswith("| — | — |")
    assert rows[3].startswith("| Ubuntu 24.04 (noble) | — | [gpclient]")


def test_unknown_codename_is_labelled_by_itself_and_sorted_last(tmp_path):
    names = [deb(PACKAGES[0], c, "amd64") for c in ("zesty", "noble", "aardvark", "jammy")]
    labels = [row.split("|")[1].strip() for row in table_rows(run(make_dir(tmp_path, names)).stdout)[2:]]
    assert labels == ["Ubuntu 22.04 (jammy)", "Ubuntu 24.04 (noble)", "aardvark", "zesty"]


@pytest.mark.parametrize("repo", ["", "owner", "owner/", "/repo", "a/b/c", "owner/re po", "o/r;rm", "o/r\n", "o/$(id)"])
def test_bad_repo_is_refused(tmp_path, repo):
    result = run(make_dir(tmp_path, full_matrix()), repo=repo)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


@pytest.mark.parametrize("tag", ["", "1.4.2", "main", "v", "va", "v1.4.2 ", "v1.4.2\n", "v1/../x", "v1;id", "v1$(id)", "v1.4.2/"])
def test_bad_tag_is_refused(tmp_path, tag):
    result = run(make_dir(tmp_path, full_matrix()), tag=tag)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


@pytest.mark.parametrize("name", [
    "README.md",                                   # not a package
    "network-manager-gpclient_1.4.2-1~noble1_amd64.deb.sig",
    "Network-Manager_1.4.2-1~noble1_amd64.deb",    # upper case
    "-gpclient_1.4.2-1~noble1_amd64.deb",
    "gpclient_1.4.2-1~noble1.deb",                 # no architecture
    "gpclient_1.4.2-1~noble1_amd64_x.deb",
    "gpclient_1.4.2-1~noble1_am d64.deb",
    "gpclient_1.4.2-1~noble1_amd64;id.deb",
    "gpclient_v1.4.2~noble1_amd64.deb",            # version does not start with a digit
])
def test_odd_file_names_are_refused(tmp_path, name):
    result = run(make_dir(tmp_path, full_matrix() + [name]))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


@pytest.mark.parametrize("version", [
    "1.4.2-1",                  # no suffix at all
    "1.4.2-1~noble2",           # not the release suffix
    "1.4.2-1~Noble1",
    "1.4.2-1~noble",
    "1.4.2-1~noble1+pr24.57",   # a pull request's build is not a release
    "1.4.2-1.noble1",           # already the name GitHub gives
])
def test_deb_without_a_codename_suffix_is_an_error(tmp_path, version):
    names = full_matrix() + ["network-manager-gpclient-gnome_%s_amd64.deb" % version]
    result = run(make_dir(tmp_path, names))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ") and "codename" in result.stderr


def test_empty_directory_is_an_error(tmp_path):
    result = run(make_dir(tmp_path, []))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


def test_missing_directory_is_an_error(tmp_path):
    result = run(str(tmp_path / "nonexistent"))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


def test_a_subdirectory_is_refused(tmp_path):
    directory = make_dir(tmp_path, [deb(PACKAGES[0], "noble", "amd64")])
    os.mkdir(os.path.join(directory, "sub"))
    result = run(directory)
    assert result.returncode == 1
    assert result.stdout == ""


# The workflow: the step runs this script and the release gets its output.

def release_job():
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    return text[text.index("\n  release:\n"):text.index("\n  publish-apt:\n")]


def test_release_job_checks_out_before_downloading():
    job = release_job()
    assert "uses: actions/checkout@v4" in job
    assert job.index("actions/checkout@v4") < job.index("- name: Download all artifacts")


def test_release_notes_step_takes_repo_and_tag_from_env():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", "Write release notes"))
    assert "REPO: ${{ github.repository }}" in step
    assert "TAG: ${{ github.ref_name }}" in step
    run = step[step.index("run: |"):]
    assert "${{" not in run


def test_release_notes_step_runs_after_collecting_and_before_creating():
    job = release_job()
    assert job.index("- name: Collect release files") < job.index("- name: Write release notes")
    assert job.index("- name: Write release notes") < job.index("- name: Create Release")


def test_create_release_uses_the_notes_and_keeps_the_generated_ones():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", "Create Release"))
    assert "body_path: release-notes.md" in step
    assert "generate_release_notes: true" in step


def test_release_notes_step_script_writes_the_table(tmp_path):
    script = workflow_steps.step_script("build-release.yml", "Write release notes")
    assert '--repo "$REPO" --tag "$TAG"' in script
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    os.symlink(os.path.join(repo_root, ".github", "scripts"), work / ".github" / "scripts")
    make_dir(work, full_matrix())
    result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=dict(os.environ, REPO=REPO, TAG=TAG),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (work / "release-notes.md").read_text(encoding="utf-8").startswith("## Downloads\n")


def test_release_notes_step_script_fails_without_packages(tmp_path):
    script = workflow_steps.step_script("build-release.yml", "Write release notes")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    os.symlink(os.path.join(repo_root, ".github", "scripts"), work / ".github" / "scripts")
    make_dir(work, [])
    result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=dict(os.environ, REPO=REPO, TAG=TAG),
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0

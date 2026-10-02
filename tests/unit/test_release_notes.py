"""
Tests for .github/scripts/release_notes.py, which writes the table of download
links at the top of a release's notes: a row per Ubuntu release, a column per
architecture, the packages of the pair in the cell.

Every positive test has negative counterparts: bad input (repo, tag, file
names, a version without the codename, no packages) ends with "error: ..." on
stderr, exit status 1 and nothing on stdout, so the release never gets notes
with a table that silently lacks files.

The table is wrapped in markers and merged into the body of a release that
exists already (--merge): a rerun replaces only the block, and malformed
markers are an error.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import json
import re
import stat
import subprocess
import sys

import pytest
import workflow_steps

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, ".github", "scripts"))
import release_notes  # noqa: E402

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
    assert result.stdout.startswith(release_notes.START + "\n## Downloads\n")
    assert result.stdout.endswith(release_notes.END + "\n")
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


def test_text_of_the_notes_tells_ubuntu_22_04_to_install_sdbus_with_pip(tmp_path):
    out = run(make_dir(tmp_path, full_matrix())).stdout
    assert "Ubuntu 22.04 `python3-sdbus` is not in apt: run `pip3 install sdbus` first" in out


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


@pytest.mark.parametrize("tag", ["v1.4.2", "v1.4.2+1", "v1.4.2-rc.1", "v1", "va", "v_1"])
def test_tags_of_the_v_trigger_are_accepted(tmp_path, tag):
    result = run(make_dir(tmp_path, full_matrix()), tag=tag)
    assert result.returncode == 0, result.stderr
    assert "/releases/download/%s/" % tag in result.stdout


@pytest.mark.parametrize("tag", ["", "1.4.2", "main", "v", "v1.4.2 ", "v1.4.2\n", "v1/../x", "v1;id", "v1$(id)", "v1.4.2/", "v1 2", "v1\"", "v1<b>", "v1`id`", "v1[x](y)", "v1\r"])
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


def test_a_directory_named_like_a_package_is_refused(tmp_path):
    directory = make_dir(tmp_path, [deb(PACKAGES[0], "noble", "amd64")])
    os.mkdir(os.path.join(directory, deb(PACKAGES[1], "noble", "amd64")))
    result = run(directory)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ") and "regular file" in result.stderr


def test_a_symlink_named_like_a_package_is_refused(tmp_path):
    directory = make_dir(tmp_path, [deb(PACKAGES[0], "noble", "amd64")])
    target = tmp_path / "target.deb"
    target.write_bytes(b"")
    os.symlink(str(target), os.path.join(directory, deb(PACKAGES[1], "noble", "amd64")))
    result = run(directory)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ") and "regular file" in result.stderr


def test_a_fifo_named_like_a_package_is_refused(tmp_path):
    directory = make_dir(tmp_path, [deb(PACKAGES[0], "noble", "amd64")])
    os.mkfifo(os.path.join(directory, deb(PACKAGES[1], "noble", "amd64")))
    result = run(directory)
    assert result.returncode == 1
    assert result.stdout == ""


def test_parse_gives_codename_arch_and_short_name():
    assert release_notes.parse("gpclient_1.4.2-1~noble1_amd64.deb") == ("noble", "amd64", "gpclient")


@pytest.mark.parametrize("name", [
    "gpclient_1.4.2-1~noble2_amd64.deb", "gpclient_1.4.2-1_amd64.deb", "gpclient_1.4.2~1_amd64.deb",
    "gpclient_~noble1_amd64.deb", "x", "",
])
def test_parse_raises_notes_error_and_nothing_else(name):
    with pytest.raises(release_notes.NotesError):
        release_notes.parse(name)


# Merging the table into the body of an existing release

TABLE = release_notes.render(REPO, TAG, full_matrix())
BLOCK = TABLE.rstrip("\n")
GENERATED = "## What's Changed\r\n* A fix by @someone in #1\n\n\n**Full Changelog**: https://x/compare/v1...v2  \n"


def merge_cli(tmp_path, old, table=TABLE):
    old_file, table_file = tmp_path / "old-body.md", tmp_path / "release-notes.md"
    old_file.write_bytes(old.encode("utf-8"))
    table_file.write_bytes(table.encode("utf-8"))
    return subprocess.run(
        [sys.executable, SCRIPT, "--merge", str(old_file), "--table", str(table_file)],
        capture_output=True, check=False,
    )


def test_merge_puts_the_block_on_top_of_a_body_without_one(tmp_path):
    result = merge_cli(tmp_path, GENERATED)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode() == BLOCK + "\n\n" + GENERATED  # the rest byte for byte


def test_merge_into_an_empty_body_gives_the_block(tmp_path):
    result = merge_cli(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().startswith(BLOCK)


def test_merge_replaces_exactly_the_block_and_keeps_the_rest(tmp_path):
    old = "intro\r\n\n" + release_notes.START + "\nold table\n" + release_notes.END + "\n\n" + GENERATED
    result = merge_cli(tmp_path, old)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode() == "intro\r\n\n" + BLOCK + "\n\n" + GENERATED
    assert "old table" not in result.stdout.decode()


def test_merge_twice_gives_the_same_body(tmp_path):
    once = merge_cli(tmp_path, GENERATED).stdout.decode()
    twice = merge_cli(tmp_path, once).stdout.decode()
    assert twice == once
    assert once.count(release_notes.START) == 1


def test_merge_replaces_a_table_of_an_earlier_run(tmp_path):
    other = release_notes.render(REPO, TAG, [deb(PACKAGES[0], "noble", "amd64")])
    first = merge_cli(tmp_path, GENERATED, table=other).stdout.decode()
    second = merge_cli(tmp_path, first).stdout.decode()
    assert second == BLOCK + "\n\n" + GENERATED


S, E = release_notes.START, release_notes.END


@pytest.mark.parametrize("old", [
    "text\n" + S + "\nno end\n",                  # start without end
    "text\n" + E + "\n",                          # end without start
    E + "\nx\n" + S + "\n",                      # end before start
    S + "\na\n" + E + "\n" + S + "\nb\n" + E,   # two blocks
    S + "\n" + S + "\n" + E + "\n",              # two starts
    S + "\n" + E + "\n" + E + "\n",              # two ends
])
def test_merge_with_malformed_markers_in_the_body_is_an_error(tmp_path, old):
    result = merge_cli(tmp_path, old)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"error: ")


@pytest.mark.parametrize("table", ["", "## Downloads\n", S + "\n", E + "\n" + S + "\n", TABLE + TABLE])
def test_merge_with_a_bad_table_is_an_error(tmp_path, table):
    result = merge_cli(tmp_path, GENERATED, table=table)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"error: ")


def test_merge_with_a_missing_file_is_an_error(tmp_path):
    table_file = tmp_path / "release-notes.md"
    table_file.write_text(TABLE)
    result = subprocess.run([sys.executable, SCRIPT, "--merge", str(tmp_path / "nope"), "--table", str(table_file)],
                            capture_output=True, check=False)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"error: ")


@pytest.mark.parametrize("args", [
    ["--merge", "x"],                                              # no table
    ["--table", "x"],                                              # no merge
    ["--merge", "x", "--table", "y", "--repo", REPO],              # both forms
    ["--repo", REPO, "--tag", TAG],                                # no directory
    [],
])
def test_wrong_combination_of_options_is_refused(args):
    result = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, check=False)
    assert result.returncode == 2
    assert result.stdout == b""


# The map of Ubuntu releases must follow the build matrix and the apt repository

def matrix_versions():
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    found = {tuple(json.loads(m)) for m in re.findall(r"fromJSON\('(\[[^']*\])'\)", text)}
    assert len(found) == 1, "the matrix is listed in more than one way: %s" % (found,)
    return set(found.pop())


def apt_suites():
    with open(os.path.join(REPO_ROOT, ".github", "scripts", "build-apt-repo.sh"), encoding="utf-8") as handle:
        match = re.search(r"^SUITES=\(([^)]*)\)", handle.read(), re.M)
    assert match, "build-apt-repo.sh does not list SUITES"
    return set(match.group(1).split())


def map_problems(ubuntu, versions, suites):
    problems = ["no codename for Ubuntu %s" % v for v in sorted(versions - set(ubuntu.values()))]
    problems += ["no entry for the suite %s" % c for c in sorted(suites - set(ubuntu))]
    problems += ["%s is in neither the matrix nor the suites" % c for c in sorted(set(ubuntu) - suites)]
    problems += ["%s (%s) is not in the build matrix" % (c, v) for c, v in sorted(ubuntu.items()) if v not in versions]
    return problems


def test_ubuntu_map_matches_the_build_matrix_and_the_apt_suites():
    assert map_problems(release_notes.UBUNTU, matrix_versions(), apt_suites()) == []


def test_map_check_reports_a_missing_entry():
    ubuntu = dict(release_notes.UBUNTU)
    del ubuntu["noble"]
    problems = map_problems(ubuntu, matrix_versions(), apt_suites())
    assert any("24.04" in p for p in problems) and any("noble" in p for p in problems)


def test_map_check_reports_an_entry_without_a_build():
    ubuntu = dict(release_notes.UBUNTU, zesty="99.04")
    assert map_problems(ubuntu, matrix_versions(), apt_suites())


# The workflow: the step runs this script and the release gets its output.

def release_job():
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    return text[text.index("\n  release:\n"):text.index("\n  publish-apt:\n")]


def test_release_job_checks_out_before_downloading():
    job = release_job()
    assert "uses: actions/checkout@v4" in job
    assert job.index("actions/checkout@v4") < job.index("- name: Download all artifacts")


def test_release_job_checkout_does_not_persist_credentials():
    job = release_job()
    start = job.index("- name: Checkout code")
    step = job[start:job.index("\n      - name:", start + 1)]
    assert "uses: actions/checkout@v4" in step
    assert "persist-credentials: false" in step


TABLE_STEP = "Write downloads table"
ADD_STEP = "Add downloads table to the release notes"


def test_steps_run_in_order_collect_write_create_add():
    job = release_job()
    order = [job.index("- name: " + n) for n in ("Collect release files", TABLE_STEP, "Create Release", ADD_STEP)]
    assert order == sorted(order)


def test_write_table_step_takes_repo_and_tag_from_env():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", TABLE_STEP))
    assert "REPO: ${{ github.repository }}" in step
    assert "TAG: ${{ github.ref_name }}" in step
    assert "${{" not in step[step.index("run: |"):]


def test_add_step_passes_token_repo_and_tag_by_env():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", ADD_STEP))
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "REPO: ${{ github.repository }}" in step
    assert "TAG: ${{ github.ref_name }}" in step
    assert "${{" not in step[step.index("run: |"):]


def test_create_release_keeps_the_generated_notes_and_does_not_set_a_body():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", "Create Release"))
    assert "body_path" not in step
    assert "\n          body:" not in step
    assert "generate_release_notes: true" in step
    assert "files: release-files/*.deb" in step
    assert "fail_on_unmatched_files: true" in step


def prepare_work(tmp_path, names):
    script = workflow_steps.step_script("build-release.yml", TABLE_STEP)
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    os.symlink(os.path.join(REPO_ROOT, ".github", "scripts"), work / ".github" / "scripts")
    make_dir(work, names)
    result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=dict(os.environ, REPO=REPO, TAG=TAG),
                            capture_output=True, text=True, check=False)
    return work, result


def test_write_table_step_script_writes_the_table(tmp_path):
    assert '--repo "$REPO" --tag "$TAG"' in workflow_steps.step_script("build-release.yml", TABLE_STEP)
    work, result = prepare_work(tmp_path, full_matrix())
    assert result.returncode == 0, result.stderr
    assert (work / "release-notes.md").read_text(encoding="utf-8").startswith(release_notes.START)


def test_write_table_step_script_fails_without_packages(tmp_path):
    _work, result = prepare_work(tmp_path, [])
    assert result.returncode != 0


FAKE_GH = """#!/bin/bash
echo "$@" >> "$GH_LOG"
if [ "$1 $2" = "release view" ]; then
    [ -n "$GH_VIEW_FAILS" ] && exit 1
    printf '%s' "$GH_BODY"
elif [ "$1 $2" = "release edit" ]; then
    while [ $# -gt 0 ]; do
        [ "$1" = "--notes-file" ] && cp "$2" "$GH_EDITED"
        shift
    done
fi
"""


def run_add_step(tmp_path, body, view_fails=False):
    script = workflow_steps.step_script("build-release.yml", ADD_STEP)
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    os.symlink(os.path.join(REPO_ROOT, ".github", "scripts"), work / ".github" / "scripts")
    (work / "release-notes.md").write_text(TABLE, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), REPO=REPO, TAG=TAG, GH_TOKEN="t",
               GH_LOG=str(tmp_path / "gh.log"), GH_EDITED=str(tmp_path / "edited.md"), GH_BODY=body)
    if view_fails:
        env["GH_VIEW_FAILS"] = "1"
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=work, env=env,
                            capture_output=True, text=True, check=False)
    log = (tmp_path / "gh.log").read_text() if (tmp_path / "gh.log").exists() else ""
    return result, log, tmp_path / "edited.md"


def test_add_step_edits_the_release_with_the_merged_body(tmp_path):
    result, log, edited = run_add_step(tmp_path, GENERATED)
    assert result.returncode == 0, result.stderr
    assert "release view %s --repo %s" % (TAG, REPO) in log
    assert "release edit %s --repo %s --notes-file" % (TAG, REPO) in log
    assert edited.read_bytes().decode() == BLOCK + "\n\n" + GENERATED


def test_add_step_on_a_rerun_gives_the_same_body(tmp_path):
    first = BLOCK + "\n\n" + GENERATED
    result, _log, edited = run_add_step(tmp_path, first)
    assert result.returncode == 0, result.stderr
    assert edited.read_bytes().decode() == first


def test_add_step_fails_and_does_not_edit_when_the_release_cannot_be_read(tmp_path):
    result, log, edited = run_add_step(tmp_path, GENERATED, view_fails=True)
    assert result.returncode != 0
    assert "release edit" not in log
    assert not edited.exists()


def test_add_step_does_not_edit_when_the_markers_are_malformed(tmp_path):
    result, log, edited = run_add_step(tmp_path, GENERATED + release_notes.START + "\n")
    assert result.returncode != 0
    assert "release edit" not in log
    assert not edited.exists()

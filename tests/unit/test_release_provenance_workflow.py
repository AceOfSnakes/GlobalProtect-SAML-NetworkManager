"""
Tests for the provenance and checksum parts of .github/workflows/build-release.yml:
the jobs that build the .deb and .rpm files attest them (only for tags and pull
requests of this repository, and only these two jobs may write attestations), and the
release job uploads a SHA256SUMS file for every package asset.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import hashlib
import re
import subprocess

import pytest
from workflow_steps import read_workflow, step_lines, step_names, step_script

WORKFLOW = "build-release.yml"
ATTEST_ACTION = re.compile(r"uses: actions/attest-build-provenance@[0-9a-f]{40} # v\d+\.\d+\.\d+$")


def job_block(job):
    text = read_workflow(WORKFLOW)
    start = text.index(f"\n  {job}:\n") + 1
    nxt = re.search(r"^  [a-z-]+:$", text[start + 1:], re.M)
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


@pytest.mark.parametrize("job, step, glob", [
    ("build", "Attest build provenance of the .deb files", "*.deb"),
    ("build-rpm", "Attest build provenance of the .rpm files", "*.rpm"),
])
def test_package_jobs_attest_their_files_before_the_upload(job, step, glob):
    lines = step_lines(WORKFLOW, step)
    assert any(ATTEST_ACTION.search(line) for line in lines)
    assert any(line.strip().startswith("subject-path:") and line.endswith(glob) for line in lines)
    names = step_names(WORKFLOW)
    assert names.index(step) < names.index("Upload artifacts" if job == "build" else "Upload RPM artifacts")


@pytest.mark.parametrize("step", ["Attest build provenance of the .deb files", "Attest build provenance of the .rpm files"])
def test_attestation_is_skipped_where_the_token_cannot_write(step):
    condition = [line for line in step_lines(WORKFLOW, step) if line.strip().startswith("if:")]
    assert len(condition) == 1
    condition = condition[0]
    # tags (push), and pull requests of this repository except Dependabot's: never forks
    assert "github.event_name == 'push'" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert "dependabot" in condition
    assert "workflow_dispatch" not in condition


def test_only_the_package_jobs_and_the_release_job_get_attestation_rights():
    text = read_workflow(WORKFLOW)
    assert text.count("attestations: write") == 3 and text.count("id-token: write") == 4
    assert "      contents: write\n      id-token: write\n      attestations: write\n" in job_block("release")
    assert "contents: read\n\n# A new push" in text or "permissions:\n  contents: read\n" in text
    for job in ("build", "build-rpm"):
        block = job_block(job)
        assert "      contents: read\n      id-token: write\n      attestations: write\n" in block
        assert "contents: write" not in block
    for job in ("check-tag", "release-notes"):
        assert "attestations: write" not in job_block(job)
    # the job that calls publish-apt only reads attestations (gh attestation verify)
    assert "attestations: read" in job_block("publish-apt")


def test_sha256sums_is_uploaded_with_the_packages():
    lines = step_lines(WORKFLOW, "Create Release")
    assert "            release-files/SHA256SUMS" in lines
    names = step_names(WORKFLOW)
    assert names.index("Write SHA256SUMS") < names.index("Create Release")
    assert "fail_on_unmatched_files: true" in "\n".join(lines)


def test_release_job_attests_the_sha256sums_it_uploads():
    step = "Attest build provenance of SHA256SUMS"
    lines = step_lines(WORKFLOW, step)
    assert any(ATTEST_ACTION.search(line) for line in lines)
    assert "          subject-path: release-files/SHA256SUMS" in lines
    assert not [line for line in lines if line.strip().startswith("if:")]
    names = step_names(WORKFLOW)
    # after the file is final, before it is uploaded
    assert names.index("Write SHA256SUMS") < names.index(step) < names.index("Create Release")
    assert step in job_block("release")


def test_the_attest_action_is_pinned_to_one_commit_everywhere():
    pins = set(re.findall(r"actions/attest-build-provenance@(\S+)", read_workflow(WORKFLOW)))
    assert len(pins) == 1 and re.fullmatch(r"[0-9a-f]{40}", pins.pop())


def run_sums(tmp_path, files):
    out = tmp_path / "release-files"
    out.mkdir()
    for name, data in files.items():
        (out / name).write_bytes(data)
    script = step_script(WORKFLOW, "Write SHA256SUMS")
    return out, subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True)


def test_sha256sums_covers_every_package_under_the_name_of_the_asset(tmp_path):
    files = {
        "network-manager-gpclient_2.0.0-1~noble1_amd64.deb": b"deb",
        "network-manager-gpclient-gnome_2.0.0-1~jammy1_arm64.deb": b"deb2",
        "network-manager-gpclient-2.0.0-1.fc44.x86_64.rpm": b"rpm",
    }
    out, result = run_sums(tmp_path, files)
    assert result.returncode == 0, result.stderr
    lines = (out / "SHA256SUMS").read_text().splitlines()
    got = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    # GitHub stores "~" as "."
    assert got == {name.replace("~", "."): hashlib.sha256(data).hexdigest() for name, data in files.items()}


def test_sha256sums_fails_without_packages(tmp_path):
    out, result = run_sums(tmp_path, {"notes.txt": b"x"})
    assert result.returncode != 0

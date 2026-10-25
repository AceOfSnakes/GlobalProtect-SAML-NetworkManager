"""
Tests for issue #54 (licensing): the scheduled Rust dependency audit
(.github/workflows/rust-audit.yml), the SBOM release asset of build-release.yml
and the DEP-5 file debian/copyright. No YAML parser is needed (CI installs only
pytest): the files are read as text.
"""

import os
import re

import pytest

from test_actions_pinned import uses_problems
from workflow_steps import read_workflow, step_lines

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AUDIT = "rust-audit.yml"
RELEASE = "build-release.yml"
SBOM = "sbom-rust.cdx.json"


def release_job_text():
    text = read_workflow(RELEASE)
    start = text.index("\n  release:\n") + 1
    return text[start:text.index("\n  release-notes:\n")]


# rust-audit.yml


def test_audit_runs_weekly_manually_and_for_upstream_changes():
    text = read_workflow(AUDIT)
    assert re.search(r'(?m)^  schedule:\n    - cron: "[^"]+"$', text)
    assert "\n  workflow_dispatch:\n" in text
    pr = text[text.index("\n  pull_request:\n"):text.index("\npermissions:")]
    assert "- external/GlobalProtect-openconnect\n" in pr and "- upstream/**\n" in pr


def test_audit_does_not_run_on_every_push_or_with_pull_request_target():
    text = read_workflow(AUDIT)
    assert "\n  push:" not in text
    assert "pull_request_target" not in text


def test_audit_permissions_are_minimal():
    text = read_workflow(AUDIT)
    assert "\npermissions:\n  contents: read\n\n" in text
    assert text.count("security-events: write") == 1  # the SARIF upload, in the job only
    for forbidden in ("contents: write", "id-token", "pull-requests: write", "actions: write"):
        assert forbidden not in text


def test_audit_scans_the_submodule_lock_file_with_both_tools_pinned():
    text = read_workflow(AUDIT)
    assert "submodules: true" in text and "persist-credentials: false" in text
    assert "LOCKFILE: external/GlobalProtect-openconnect/Cargo.lock" in text
    assert 'cargo install cargo-audit --locked --version "$CARGO_AUDIT_VERSION"' in text
    assert re.search(r'CARGO_AUDIT_VERSION: "\d+\.\d+\.\d+"', text)
    assert re.search(r'OSV_SCANNER_VERSION: "\d+\.\d+\.\d+"', text)
    assert re.search(r'OSV_SCANNER_SHA256: "[0-9a-f]{64}"', text)
    assert "sha256sum -c -" in text
    assert 'cargo audit --file "$LOCKFILE"' in text
    assert 'scan source --lockfile "$LOCKFILE"' in text


def test_only_the_schedule_run_fails_on_a_finding():
    assert "        if: github.event_name == 'schedule'" in step_lines(AUDIT, "Fail on findings (weekly schedule only)")
    # the scanning steps never fail the job themselves
    for name in ("Run cargo-audit", "Run osv-scanner"):
        assert any("|| status=$?" in line for line in step_lines(AUDIT, name))


def test_sarif_is_not_uploaded_for_pull_requests():
    lines = step_lines(AUDIT, "Upload OSV result to code scanning")
    assert any(line.strip().startswith("if: github.event_name != 'pull_request'") for line in lines)


def test_every_action_of_the_audit_is_pinned():
    text = read_workflow(AUDIT)
    assert text.count("uses:") >= 2
    assert uses_problems(text) == []


@pytest.mark.parametrize("workflow", [AUDIT, RELEASE])
def test_unpinned_action_in_a_copy_of_the_workflow_is_reported(workflow):
    text = read_workflow(workflow)
    bad = re.sub(r"(uses: [\w./-]+)@[0-9a-f]{40}", r"\1@v4", text, count=1)
    assert bad != text and len(uses_problems(bad)) == 1
    no_comment = re.sub(r"(uses: [\w./-]+@[0-9a-f]{40}) # v[\d.]+", r"\1", text, count=1)
    assert no_comment != text and len(uses_problems(no_comment)) == 1


# SBOM in build-release.yml


def sbom_job_text():
    text = read_workflow(RELEASE)
    start = text.index("\n  sbom:\n") + 1
    return text[start:text.index("\n  release:\n")]


def test_release_job_uploads_the_sbom_next_to_sha256sums():
    job = release_job_text()
    assert "\n    needs: [build, build-rpm, sbom]\n" in job
    assert f"            release-files/{SBOM}\n" in job
    assert "            release-files/SHA256SUMS\n" in job
    assert "fail_on_unmatched_files: true" in job
    assert f"-name '{SBOM}'" in "\n".join(step_lines(RELEASE, "Collect release files"))


def test_sbom_is_made_once_per_release_in_a_tag_only_job():
    text = read_workflow(RELEASE)
    assert text.count("anchore/sbom-action@") == 1  # not in a matrix job
    job = sbom_job_text()
    assert "anchore/sbom-action@" in job
    assert "    if: startsWith(github.ref, 'refs/tags/') && github.event_name == 'push'\n" in job
    assert "matrix" not in job and "contents: write" not in job


def test_sbom_step_uses_the_lock_file_and_its_file_is_uploaded():
    lines = "\n".join(step_lines(RELEASE, "Generate SBOM of the Rust dependencies"))
    assert "file: external/GlobalProtect-openconnect/Cargo.lock" in lines
    assert "format: cyclonedx-json" in lines
    assert f"output-file: {SBOM}" in lines
    assert re.search(r"syft-version: v\d+\.\d+\.\d+", lines)
    assert "upload-release-assets: false" in lines
    upload = "\n".join(step_lines(RELEASE, "Upload SBOM"))
    assert f"path: {SBOM}" in upload and "if-no-files-found: error" in upload
    assert "submodules: true" in sbom_job_text() and "persist-credentials: false" in sbom_job_text()


def test_sbom_is_not_in_sha256sums():
    # SHA256SUMS lists the packages only: publish-apt checks every package against it
    script = "\n".join(step_lines(RELEASE, "Write SHA256SUMS"))
    assert "sha256sum -- *.deb *.rpm > SHA256SUMS" in script
    assert "sbom" not in script


# debian/copyright (DEP-5)


def copyright_stanzas(text):
    """The paragraphs of a DEP-5 file as dicts of field -> value (comment lines dropped)"""
    stanzas = []
    for block in re.split(r"\n\s*\n", text.strip()):
        fields, key = {}, None
        for line in block.splitlines():
            if line.startswith("#"):
                continue
            if line[0] in " \t":
                fields[key] += "\n" + line.strip()
            else:
                key, colon, value = line.partition(":")
                assert colon, f"not a field: {line!r}"
                fields[key] = value.strip()
        stanzas.append(fields)
    return stanzas


def copyright_problems(text):
    """Problems with the structure of a DEP-5 file that this project relies on"""
    stanzas = copyright_stanzas(text)
    problems = []
    if "Format" not in stanzas[0] or "Upstream-Name" not in stanzas[0]:
        problems.append("no header")
    files = [s for s in stanzas[1:] if "Files" in s]
    for stanza in files:
        if "Copyright" not in stanza or "License" not in stanza:
            problems.append(f"Files stanza without Copyright or License: {stanza['Files']}")
    defined = {s["License"].split("\n")[0] for s in stanzas[1:] if "Files" not in s and "License" in s}
    for stanza in files:
        name = stanza.get("License", "").split("\n")[0].strip()
        if name and name not in defined:
            problems.append(f"license without a paragraph: {name}")
    return problems


def read_copyright():
    with open(os.path.join(ROOT, "debian", "copyright"), encoding="utf-8") as handle:
        return handle.read()


def stanza_for(text, pattern):
    (stanza,) = [s for s in copyright_stanzas(text) if pattern in s.get("Files", "").split("\n")]
    return stanza


def test_copyright_is_valid_dep5_and_keeps_the_header():
    text = read_copyright()
    assert copyright_problems(text) == []
    header = copyright_stanzas(text)[0]
    assert header["Format"] == "https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    assert header["Upstream-Name"] == "network-manager-gpclient"
    assert header["Source"] == "https://github.com/WMP/GlobalProtect-SAML-NetworkManager"


def test_copyright_asks_for_a_review_by_a_person():
    assert read_copyright().startswith("# Reviewed by: (needs a person)\n")


def test_copyright_files_star_is_lgpl_21_as_the_license_file():
    assert stanza_for(read_copyright(), "*")["License"].startswith("LGPL-2.1")
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as handle:
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in handle.read(200)
    assert "GPL-2+" not in read_copyright().replace("LGPL-2+", "")


def test_copyright_has_gpl3_for_upstream_and_lgpl_for_the_openconnect_fork():
    text = read_copyright()
    assert stanza_for(text, "external/GlobalProtect-openconnect/*")["License"].startswith("GPL-3")
    fork = "external/GlobalProtect-openconnect/crates/openconnect/deps/openconnect/*"
    assert stanza_for(text, fork)["License"].startswith("LGPL-2.1")
    assert stanza_for(text, fork.replace("/openconnect/*", "/libxml2/*"))["License"].startswith("MIT")


@pytest.mark.parametrize(
    "old, new",
    [
        ("Upstream-Name: network-manager-gpclient\n", ""),
        ("Copyright: 2024 WMP\n", ""),
        ("\nLicense: GPL-3\n This program", "\nLicense: GPL-4\n This program"),
        ("License: MIT\n Permission", "License: X11\n Permission"),
    ],
)
def test_broken_copyright_is_reported(old, new):
    text = read_copyright()
    bad = text.replace(old, new, 1)
    assert bad != text and copyright_problems(bad) != []


def test_copyright_checker_reports_an_unknown_license():
    text = "Format: f\nUpstream-Name: n\n\nFiles: *\nCopyright: me\nLicense: Foo\n"
    assert copyright_problems(text) == ["license without a paragraph: Foo"]

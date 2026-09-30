"""
Tests for .github/scripts/test_packages_check.py, which renders the "Test
packages" check run of a pull request: the install command as the title (shown
next to the check in the merge box) and what it does in the summary.

Every positive test has negative counterparts: a failed build or an
unpublished release yields a neutral check without an install command, and bad
input is refused instead of ending up in a check.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".github", "scripts", "test_packages_check.py")
)
spec = importlib.util.spec_from_file_location("test_packages_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

REPO = "WMP/GlobalProtect-SAML-NetworkManager"
SHA = "0123456789abcdef0123456789abcdef01234567"
PR = "24"
RUN_URL = f"https://github.com/{REPO}/actions/runs/123456"
DEBS = [
    "network-manager-gpclient_1.4.2-1~noble1_amd64.deb",
    "network-manager-gpclient-gnome_1.4.2-1~noble1_amd64.deb",
    "network-manager-gpclient-plasma-5_1.4.2-1~noble1_amd64.deb",
    "network-manager-gpclient_1.4.2-1~resolute1_arm64.deb",
    "network-manager-gpclient-plasma-6_1.4.2-1~resolute1_arm64.deb",
]
COMMAND = (
    f"bash <(curl -fsSL https://raw.githubusercontent.com/{REPO}/main/scripts/install-pr-build.sh) {PR}"
)


def published(**overrides):
    args = dict(state="published", pr=PR, sha=SHA, repo=REPO, debs=DEBS)
    args.update(overrides)
    return check.render(**args)


class TestPublished:
    def test_the_check_is_completed_and_successful_on_the_head_commit(self):
        result = published()

        assert result["name"] == "Test packages"
        assert result["head_sha"] == SHA
        assert result["status"] == "completed"
        assert result["conclusion"] == "success"
        assert result["details_url"] == f"https://github.com/{REPO}/releases/tag/pr-{PR}"

    def test_the_title_is_the_install_command_with_the_pr_number(self):
        assert published()["output"]["title"] == f"Install: {COMMAND}"

    def test_the_command_fetches_the_script_from_the_default_branch_not_the_pr(self):
        title = published(script_ref="trunk")["output"]["title"]

        assert f"/{REPO}/trunk/scripts/install-pr-build.sh) {PR}" in title
        assert SHA not in title

    def test_the_command_is_not_a_pipe_into_bash(self):
        summary = published()["output"]["summary"]

        assert "curl -fsSL" in summary
        assert "| bash" not in summary.replace("`curl ... | bash`", "")

    def test_the_summary_has_the_command_in_a_code_block(self):
        summary = published()["output"]["summary"]

        assert f"```bash\n{COMMAND}\n```" in summary

    def test_the_summary_says_what_the_script_detects(self):
        summary = published()["output"]["summary"]

        for text in ("/etc/os-release", "amd64 or arm64", "XDG_CURRENT_DESKTOP", "--desktop gnome", "pip3 install sdbus"):
            assert text in summary

    def test_the_summary_lists_the_published_files_sorted(self):
        summary = published(debs=list(reversed(DEBS)))["output"]["summary"]

        positions = [summary.index(f"- `{name}`") for name in sorted(DEBS)]
        assert positions == sorted(positions)
        assert f"Published packages ({len(DEBS)} files" in summary

    def test_the_summary_says_how_to_go_back(self):
        summary = published()["output"]["summary"]

        assert "sudo apt install --reinstall --allow-downgrades network-manager-gpclient" in summary
        assert "sudo apt remove network-manager-gpclient" in summary
        assert "docs/APT_REPO.md" in summary

    def test_the_summary_links_the_release_and_names_the_commit(self):
        summary = published()["output"]["summary"]

        assert f"[`pr-{PR}`](https://github.com/{REPO}/releases/tag/pr-{PR})" in summary
        assert f"commit `{SHA[:7]}`" in summary
        assert "Unreviewed test build" in summary


class TestNoPackages:
    def test_a_failed_build_is_neutral_and_has_no_install_command(self):
        result = check.render("failed", PR, SHA, REPO, run_url=RUN_URL)

        assert result["conclusion"] == "neutral"
        assert result["output"]["title"] == "No test packages: the build failed"
        assert RUN_URL in result["output"]["summary"]
        assert "install-pr-build.sh" not in json.dumps(result)
        assert "details_url" not in result

    def test_a_failed_build_without_a_run_link_still_works(self):
        result = check.render("failed", PR, SHA, REPO)

        assert result["conclusion"] == "neutral"
        assert "actions/runs" not in result["output"]["summary"]

    def test_a_failed_build_ignores_the_packages_it_is_given(self):
        result = check.render("failed", PR, SHA, REPO, debs=DEBS)

        assert ".deb" not in json.dumps(result)

    def test_not_published_says_why(self):
        reason = "pull requests from forks are not published"

        result = check.render("not-published", PR, SHA, REPO, reason=reason)

        assert result["conclusion"] == "neutral"
        assert result["output"]["title"] == f"No test packages: {reason}"
        assert "install-pr-build.sh" not in json.dumps(result)

    @pytest.mark.parametrize("run_url", ["http://github.com/a/b/actions/runs/1", "https://evil.example/actions/runs/1",
                                          f"{RUN_URL})[x](https://evil.example", "javascript:alert(1)"])
    def test_a_run_link_that_is_not_a_github_run_is_refused(self, run_url):
        with pytest.raises(check.CheckError):
            check.render("failed", PR, SHA, REPO, run_url=run_url)

    @pytest.mark.parametrize("reason", ["", "x" * 201, "line\nbreak", "`code`", "<b>html</b>", "[a](b)"])
    def test_a_reason_must_be_plain(self, reason):
        with pytest.raises(check.CheckError):
            check.render("not-published", PR, SHA, REPO, reason=reason)


class TestRefused:
    @pytest.mark.parametrize("pr", ["abc", "0", "007", "12345678", "", "-1", "1;rm", "24 "])
    def test_bad_pr_numbers(self, pr):
        for state in ("published", "failed", "not-published"):
            with pytest.raises(check.CheckError):
                check.render(state, pr, SHA, REPO, debs=DEBS, reason="because")

    @pytest.mark.parametrize("sha", ["", "abc", SHA[:-1], SHA.upper(), SHA + "0", "g" * 40])
    def test_bad_commit_hashes(self, sha):
        with pytest.raises(check.CheckError):
            published(sha=sha)

    @pytest.mark.parametrize("repo", ["", "no-slash", "a/b/c", "a b/c", "a/b;x"])
    def test_bad_repositories(self, repo):
        with pytest.raises(check.CheckError):
            published(repo=repo)

    @pytest.mark.parametrize("ref", ["", "a b", "main;x", "$(x)", "main)"])
    def test_bad_script_refs(self, ref):
        with pytest.raises(check.CheckError):
            published(script_ref=ref)

    def test_an_empty_release_is_not_announced(self):
        with pytest.raises(check.CheckError, match="no packages"):
            published(debs=[])

    @pytest.mark.parametrize(
        "name",
        ["../evil.deb", "a/b.deb", ".hidden.deb", "a b.deb", "x.deb;rm", "x.ddeb", "x.deb.sig", "`x`.deb", "x|y.deb", ""],
    )
    def test_package_names_that_are_not_plain_are_refused(self, name):
        with pytest.raises(check.CheckError, match="unexpected package file name"):
            published(debs=DEBS + [name])

    def test_an_unknown_state(self):
        with pytest.raises(check.CheckError, match="unknown state"):
            check.render("done", PR, SHA, REPO)


class TestCommandLine:
    def run(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)

    def test_published_prints_the_check_as_json(self, tmp_path):
        for name in DEBS + ["README.txt"]:
            (tmp_path / name).write_text("x")

        result = self.run("--state", "published", "--pr", PR, "--sha", SHA, "--repo", REPO, "--debs-dir", str(tmp_path))

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["conclusion"] == "success"
        assert data["output"]["title"] == f"Install: {COMMAND}"
        assert "README.txt" not in data["output"]["summary"]
        assert f"Published packages ({len(DEBS)} files" in data["output"]["summary"]

    def test_failed_prints_a_neutral_check(self):
        result = self.run("--state", "failed", "--pr", PR, "--sha", SHA, "--repo", REPO, "--run-url", RUN_URL)

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["conclusion"] == "neutral"
        assert data["output"]["title"] == "No test packages: the build failed"

    def test_an_empty_packages_directory_is_an_error_and_prints_nothing(self, tmp_path):
        result = self.run("--state", "published", "--pr", PR, "--sha", SHA, "--repo", REPO, "--debs-dir", str(tmp_path))

        assert result.returncode == 1
        assert result.stdout == ""
        assert "no packages" in result.stderr

    def test_a_directory_that_does_not_exist_is_an_error(self, tmp_path):
        result = self.run(
            "--state", "published", "--pr", PR, "--sha", SHA, "--repo", REPO, "--debs-dir", str(tmp_path / "nope")
        )

        assert result.returncode == 1 and result.stdout == ""
        assert "Traceback" not in result.stderr

    def test_a_package_with_an_odd_name_is_an_error(self, tmp_path):
        (tmp_path / "a b.deb").write_text("x")

        result = self.run("--state", "published", "--pr", PR, "--sha", SHA, "--repo", REPO, "--debs-dir", str(tmp_path))

        assert result.returncode == 1 and result.stdout == ""
        assert "unexpected package file name" in result.stderr

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["--state", "published"],
            ["--state", "bogus", "--pr", PR, "--sha", SHA, "--repo", REPO],
            ["--state", "failed", "--pr", "abc", "--sha", SHA, "--repo", REPO],
            ["--state", "failed", "--pr", PR, "--sha", "abc", "--repo", REPO],
        ],
    )
    def test_bad_arguments_are_rejected(self, args):
        result = self.run(*args)

        assert result.returncode != 0
        assert result.stdout == ""

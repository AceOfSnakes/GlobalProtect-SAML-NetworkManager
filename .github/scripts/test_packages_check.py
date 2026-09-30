#!/usr/bin/env python3
"""The "Test packages" check run of a pull request, as the JSON the GitHub API
takes (POST/PATCH /repos/{repo}/check-runs).

    test_packages_check.py --state published --pr 24 --sha <head sha> \\
        --repo OWNER/REPO --debs-dir upload/
    test_packages_check.py --state failed --pr 24 --sha ... --repo ... --run-url URL
    test_packages_check.py --state not-published --reason "..." --pr 24 ...

Used by .github/workflows/pr-test-packages.yml. The check's title is shown next
to it in the merge box of the pull request, so it carries the install command;
the summary says what the command does and how to go back. Standard library only.
"""

import argparse
import json
import os
import re
import sys

NAME = "Test packages"
SCRIPT_PATH = "scripts/install-pr-build.sh"
# File names that came out of a build artifact are data: only plain ones are
# listed (and uploaded, the workflow filters the same way)
DEB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*\.deb$")
RUN_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
REF_RE = re.compile(r"^[\w./-]+$")
PR_RE = re.compile(r"^[1-9][0-9]{0,6}$")
REASON_RE = re.compile(r"^[A-Za-z0-9 ,.;:()'#/_-]{1,200}$")
PACKAGE_RE = re.compile(r"^(network-manager-gpclient(?:-gnome|-plasma-[56])?)_")


class CheckError(Exception):
    """Input that must not end up in a check run."""


def install_command(repo, pr, script_ref):
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, script_ref, SCRIPT_PATH)
    # bash <(...) and not `curl | bash`: apt asks for confirmation on the
    # terminal, which a pipe would take away
    return "bash <(curl -fsSL %s) %s" % (url, pr)


def published_summary(repo, pr, sha, script_ref, debs):
    command = install_command(repo, pr, script_ref)
    release = "https://github.com/%s/releases/tag/pr-%s" % (repo, pr)
    shown = sorted({PACKAGE_RE.match(d).group(1) for d in debs if PACKAGE_RE.match(d)})
    lines = [
        "### Install this PR's test packages",
        "",
        "Unreviewed test build of #%s (commit `%s`), published as the prerelease "
        "[`pr-%s`](%s) and removed when the PR closes. "
        "It installs as root: read the diff first." % (pr, sha[:7], pr, release),
        "",
        "```bash",
        command,
        "```",
        "",
        "The script picks what fits the machine it runs on:",
        "",
        "- the Ubuntu release (22.04, 24.04, 24.10 or 26.04) from `/etc/os-release`,",
        "- the architecture (amd64 or arm64) from `dpkg`,",
        "- the desktop: Plasma when `XDG_CURRENT_DESKTOP` mentions KDE (Plasma 6 where "
        "the release has it, else Plasma 5), otherwise GNOME. Force one with "
        "`--desktop gnome` or `--desktop plasma`.",
        "",
        "It prints what it is going to install and lets apt ask for confirmation "
        "(`--yes` skips the question). On Ubuntu 22.04 `python3-sdbus` is not in apt: "
        "run `pip3 install sdbus` first.",
        "",
        "Run it as shown (`bash <(...)`), not as `curl ... | bash`: apt needs the terminal.",
        "",
        "#### Back to the released version",
        "",
        "With the apt repository set up as in `docs/APT_REPO.md`:",
        "",
        "```bash",
        "sudo apt update && sudo apt install --reinstall --allow-downgrades "
        "network-manager-gpclient network-manager-gpclient-gnome   # or -plasma-5 / -plasma-6, as installed",
        "```",
        "",
        "Without it: `sudo apt remove network-manager-gpclient network-manager-gpclient-gnome` "
        "(or the plasma package) and install the released `.deb` files again.",
        "",
        "<details><summary>Published packages (%d files, %s)</summary>" % (len(debs), ", ".join(shown) or "none"),
        "",
    ]
    lines += ["- `%s`" % name for name in debs]
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def render(state, pr, sha, repo, script_ref="main", debs=(), run_url="", reason=""):
    """The check run as a dict."""
    if not PR_RE.match(str(pr)):
        raise CheckError("not a pull request number: %r" % (pr,))
    if not SHA_RE.match(sha):
        raise CheckError("not a full commit sha: %r" % (sha,))
    if not REPO_RE.match(repo):
        raise CheckError("repo must look like OWNER/REPO: %r" % (repo,))
    if not REF_RE.match(script_ref):
        raise CheckError("not a branch name: %r" % (script_ref,))

    check = {"name": NAME, "head_sha": sha, "status": "completed"}
    if state == "published":
        debs = list(debs)
        if not debs:
            raise CheckError("no packages to publish: refusing to announce an empty release")
        for name in debs:
            if not DEB_RE.match(name):
                raise CheckError("unexpected package file name: %r" % (name,))
        check["conclusion"] = "success"
        check["details_url"] = "https://github.com/%s/releases/tag/pr-%s" % (repo, pr)
        check["output"] = {
            "title": "Install: " + install_command(repo, pr, script_ref),
            "summary": published_summary(repo, pr, sha, script_ref, sorted(debs)),
        }
    elif state == "failed":
        if run_url and not RUN_URL_RE.match(run_url):
            raise CheckError("not a workflow run url: %r" % (run_url,))
        summary = "The build of commit `%s` failed, so no test packages were published." % sha[:7]
        if run_url:
            summary += " See the [Build and Release run](%s)." % run_url
        check["conclusion"] = "neutral"
        check["output"] = {"title": "No test packages: the build failed", "summary": summary}
    elif state == "not-published":
        if not REASON_RE.match(reason):
            raise CheckError("a plain reason is required (letters, digits and punctuation): %r" % (reason,))
        check["conclusion"] = "neutral"
        check["output"] = {
            "title": "No test packages: " + reason,
            "summary": "No test packages were published for commit `%s`: %s." % (sha[:7], reason),
        }
    else:
        raise CheckError("unknown state: %r" % (state,))
    return check


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", required=True, choices=["published", "failed", "not-published"])
    parser.add_argument("--pr", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--script-ref", default="main", help="branch to fetch install-pr-build.sh from")
    parser.add_argument("--debs-dir", default="", help="directory with the published .deb files")
    parser.add_argument("--run-url", default="", help="the build run, shown when the build failed")
    parser.add_argument("--reason", default="", help="why nothing was published (state not-published)")
    args = parser.parse_args(argv)

    debs = []
    if args.debs_dir:
        try:
            debs = sorted(n for n in os.listdir(args.debs_dir) if n.endswith(".deb"))
        except OSError as exc:
            print("error: cannot read %s: %s" % (args.debs_dir, exc), file=sys.stderr)
            return 1
    try:
        check = render(args.state, args.pr, args.sha, args.repo, args.script_ref, debs, args.run_url, args.reason)
    except CheckError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    json.dump(check, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

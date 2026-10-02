#!/usr/bin/env python3
"""The release notes' table of download links, as markdown.

    release_notes.py --repo OWNER/REPO --tag v1.4.2 --debs-dir release-files/ > release-notes.md

Used by .github/workflows/build-release.yml. The release has one .deb per package,
Ubuntu release and architecture, so the notes start with a table: a row per
Ubuntu release, a column per architecture, the packages of that pair in the
cell. GitHub's generated notes are appended after it. Standard library only.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_packages_check import DEB_RE, REPO_RE, github_name  # noqa: E402  one definition of both

TAG_RE = re.compile(r"v[0-9][A-Za-z0-9._-]*")
# <package>_<version>_<arch>.deb; a release version ends in ~<codename>1 (1.4.2-1~noble1)
NAME_RE = re.compile(r"^(?P<package>[^_]+)_(?P<version>[^_]+)_(?P<arch>[^_]+)\.deb$")
CODENAME_RE = re.compile(r"~(?P<codename>[a-z]+)1$")
CORE = "network-manager-gpclient"
# Short names in the order of a cell; any other package follows alphabetically
ORDER = ["gpclient", "gnome", "plasma-5", "plasma-6"]
UBUNTU = {"jammy": "22.04", "noble": "24.04", "oracular": "24.10", "resolute": "26.04"}
EMPTY = "—"


class NotesError(Exception):
    """Input that must not end up in the release notes."""


def short_name(package):
    if package == CORE:
        return "gpclient"
    if package.startswith(CORE + "-"):
        return package[len(CORE) + 1:]
    return package


def parse(name):
    """(codename, arch, short package name) of a package file name"""
    if not DEB_RE.fullmatch(name):
        raise NotesError("unexpected file name: %r" % (name,))
    match = NAME_RE.match(name)
    codename = CODENAME_RE.search(match.group("version"))
    if not codename:
        raise NotesError("no ~<codename>1 suffix in the version of %r" % (name,))
    return codename.group("codename"), match.group("arch"), short_name(match.group("package"))


def release_key(codename):
    """Known Ubuntu releases by version, then the unknown ones by codename"""
    return (0, UBUNTU[codename], "") if codename in UBUNTU else (1, "", codename)


def row_label(codename):
    if codename in UBUNTU:
        return "Ubuntu %s (%s)" % (UBUNTU[codename], codename)
    return codename


def cell_key(item):
    short = item[0]
    return (ORDER.index(short), "") if short in ORDER else (len(ORDER), short)


def render(repo, tag, names):
    """The markdown for the package file names of the release"""
    if not REPO_RE.fullmatch(repo):
        raise NotesError("repo must look like OWNER/REPO: %r" % (repo,))
    if not TAG_RE.fullmatch(tag):
        raise NotesError("not a release tag: %r" % (tag,))
    names = sorted(names)
    if not names:
        raise NotesError("no packages: refusing to write a release without downloads")

    cells = {}
    for name in names:
        codename, arch, short = parse(name)
        cells.setdefault((codename, arch), []).append((short, name))
    arches = sorted({arch for _codename, arch in cells}, key=lambda a: (a != "amd64", a))
    codenames = sorted({codename for codename, _arch in cells}, key=release_key)

    base = "https://github.com/%s" % repo
    lines = [
        "## Downloads",
        "",
        "The recommended way to install is the apt repository: see "
        "[docs/APT_REPO.md](%s/blob/%s/docs/APT_REPO.md). With single files, take "
        "`network-manager-gpclient` and one desktop package (`-gnome`, `-plasma-5` or "
        "`-plasma-6`) from the same row and architecture and install them together, "
        "e.g. `sudo apt install ./network-manager-gpclient_*.deb "
        "./network-manager-gpclient-gnome_*.deb`." % (base, tag),
        "",
        "| Ubuntu | " + " | ".join(arches) + " |",
        "|---|" + "---|" * len(arches),
    ]
    for codename in codenames:
        row = []
        for arch in arches:
            links = [
                "[%s](%s/releases/download/%s/%s)" % (short, base, tag, github_name(name))
                for short, name in sorted(cells.get((codename, arch), []), key=cell_key)
            ]
            row.append("<br>".join(links) or EMPTY)
        lines.append("| %s | %s |" % (row_label(codename), " | ".join(row)))
    return "\n".join(lines) + "\n\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="OWNER/REPO of the release")
    parser.add_argument("--tag", required=True, help="the release's tag, e.g. v1.4.2")
    parser.add_argument("--debs-dir", required=True, help="directory with the release's .deb files")
    args = parser.parse_args(argv)
    try:
        names = os.listdir(args.debs_dir)
        text = render(args.repo, args.tag, names)
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.debs_dir, exc), file=sys.stderr)
        return 1
    except NotesError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

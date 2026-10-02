#!/usr/bin/env python3
"""The release notes' table of download links, as markdown.

    release_notes.py --repo OWNER/REPO --tag v1.4.2 --debs-dir release-files/ > release-notes.md
    release_notes.py --merge old-body.md --table release-notes.md > new-body.md

Used by .github/workflows/build-release.yml. The release has one .deb per package,
Ubuntu release and architecture, so the notes start with a table: a row per
Ubuntu release, a column per architecture, the packages of that pair in the
cell. The table is wrapped in markers; --merge puts it into the body of a
release that exists already (GitHub's generated notes): it replaces the block
between the markers, or, with no block yet, goes to the top. The rest of the
body is kept byte for byte, so a rerun changes nothing. Standard library only.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_packages_check import DEB_RE, REPO_RE, github_name  # noqa: E402  one definition of both

# The workflow runs for v*: anything after the "v" that is safe in a URL and in markdown
TAG_RE = re.compile(r"v[A-Za-z0-9._+-]+")
START = "<!-- downloads:start -->"
END = "<!-- downloads:end -->"
# <package>_<version>_<arch>.deb; a release version ends in ~<codename>1 (1.4.2-1~noble1)
NAME_RE = re.compile(
    r"(?P<package>[a-z0-9][a-z0-9+.-]*)_(?P<version>[0-9][A-Za-z0-9.+~-]*)"
    r"~(?P<codename>[a-z]+)1_(?P<arch>[a-z0-9]+)\.deb"
)
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
    match = NAME_RE.fullmatch(name)
    if not match:
        raise NotesError("no ~<codename>1 suffix in the version of %r" % (name,))
    return match.group("codename"), match.group("arch"), short_name(match.group("package"))


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
        START,
        "## Downloads",
        "",
        "The recommended way to install is the apt repository: see "
        "[docs/APT_REPO.md](%s/blob/%s/docs/APT_REPO.md). With single files, take "
        "`network-manager-gpclient` and one desktop package (`-gnome`, `-plasma-5` or "
        "`-plasma-6`) from the same row and architecture and install them together, "
        "e.g. `sudo apt install ./network-manager-gpclient_*.deb "
        "./network-manager-gpclient-gnome_*.deb`. On Ubuntu 22.04 `python3-sdbus` is "
        "not in apt: run `pip3 install sdbus` first." % (base, tag),
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
    lines.append(END)
    return "\n".join(lines) + "\n"


def find_block(text, what):
    """(start, end) offsets of the one marked block of `text`, or None if it has none"""
    starts = [m.start() for m in re.finditer(re.escape(START), text)]
    ends = [m.start() for m in re.finditer(re.escape(END), text)]
    if not starts and not ends:
        return None
    if len(starts) > 1 or len(ends) > 1:
        raise NotesError("%s has more than one downloads marker of a kind" % what)
    if not starts or not ends:
        raise NotesError("%s has a downloads marker without its pair" % what)
    if ends[0] < starts[0]:
        raise NotesError("%s has the downloads end marker before the start marker" % what)
    return starts[0], ends[0] + len(END)


def merge(old, table):
    """The body `old` with the marked block of `table` in it"""
    span = find_block(table, "the table")
    if span is None:
        raise NotesError("the table has no downloads markers")
    block = table[span[0]:span[1]]
    found = find_block(old, "the release body")
    if found is None:
        return block + "\n\n" + old
    return old[:found[0]] + block + old[found[1]:]


def list_debs(directory):
    """The file names in `directory`; only regular files are accepted"""
    names = sorted(os.listdir(directory))
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise NotesError("not a regular file: %r" % (name,))
    return names


def read_text(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="OWNER/REPO of the release")
    parser.add_argument("--tag", help="the release's tag, e.g. v1.4.2")
    parser.add_argument("--debs-dir", help="directory with the release's .deb files")
    parser.add_argument("--merge", metavar="OLD_BODY", help="file with the release's current body")
    parser.add_argument("--table", help="with --merge: file written by the first form")
    args = parser.parse_args(argv)
    given = [args.repo, args.tag, args.debs_dir]
    if args.merge is not None:
        if args.table is None or any(value is not None for value in given):
            parser.error("--merge goes with --table only")
    elif args.table is not None or any(value is None for value in given):
        parser.error("give --repo, --tag and --debs-dir, or --merge and --table")
    try:
        if args.merge is not None:
            text = merge(read_text(args.merge), read_text(args.table))
        else:
            text = render(args.repo, args.tag, list_debs(args.debs_dir))
    except (OSError, UnicodeDecodeError) as exc:
        print("error: cannot read the input: %s" % exc, file=sys.stderr)
        return 1
    except NotesError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The release notes' table of download links, as markdown.

    gh release view v1.4.2 --json assets,body > release.json
    release_notes.py --repo OWNER/REPO --tag v1.4.2 --release-json release.json > new-body.md

Used by .github/workflows/build-release.yml. The release has one .deb per package,
Ubuntu release and architecture, so the notes start with a table: a row per
Ubuntu release, a column per architecture, the packages of that pair in the
cell. The links are those of the assets the release has. The table is wrapped
in markers and merged into the body of the release (GitHub's generated notes):
it replaces the block between the markers, or, with no block yet, goes to the
top. The rest of the body is kept byte for byte, so a rerun changes nothing.
Standard library only.
"""

import argparse
import json
import re
import sys
import urllib.parse

REPO_RE = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
# Characters that are safe in a markdown link target
URL_RE = re.compile(r"[A-Za-z0-9._~%+/:-]+")
START = "<!-- downloads:start -->"
END = "<!-- downloads:end -->"
# <package>_<version>.<codename>1_<arch>.deb; GitHub turns the "~" of a release version
# (1.4.2-1~noble1) into "." in the name of an asset: accept either
NAME_RE = re.compile(
    r"(?P<package>[a-z0-9][a-z0-9+.-]*)_(?P<version>[0-9][A-Za-z0-9.+~-]*?)"
    r"[.~](?P<codename>[a-z]+)1_(?P<arch>[a-z0-9]+)\.deb"
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
    match = NAME_RE.fullmatch(name)
    if not match:
        raise NotesError("unexpected name of a .deb asset: %r" % (name,))
    return match.group("codename"), match.group("arch"), short_name(match.group("package"))


def check_url(repo, name, url):
    """`url` must be the download link of the asset `name`, and safe in markdown"""
    prefix = "https://github.com/%s/releases/download/" % repo
    if not isinstance(url, str) or not URL_RE.fullmatch(url):
        raise NotesError("unsafe url of the asset %r: %r" % (name, url))
    rest = url[len(prefix):] if url.startswith(prefix) else ""
    folder, _slash, last = rest.rpartition("/")
    if not folder or "/" in folder or last != name:
        raise NotesError("url of the asset %r is not its download link: %r" % (name, url))


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


def render(repo, tag, assets):
    """The markdown for the assets (dicts with name and url) of the release"""
    if not REPO_RE.fullmatch(repo):
        raise NotesError("repo must look like OWNER/REPO: %r" % (repo,))
    if not tag:
        raise NotesError("the tag is empty")
    debs = []
    for asset in assets:
        name = asset.get("name") if isinstance(asset, dict) else None
        if not isinstance(name, str):
            raise NotesError("an asset without a name: %r" % (asset,))
        if name.endswith(".deb"):
            debs.append((name, asset.get("url")))
    if not debs:
        raise NotesError("no .deb assets: refusing to write a release without downloads")

    cells = {}
    for name, url in sorted(debs, key=lambda deb: deb[0]):
        codename, arch, short = parse(name)
        check_url(repo, name, url)
        cells.setdefault((codename, arch), []).append((short, url))
    arches = sorted({arch for _codename, arch in cells}, key=lambda a: (a != "amd64", a))
    codenames = sorted({codename for codename, _arch in cells}, key=release_key)

    base = "https://github.com/%s" % repo
    docs = "%s/blob/%s/docs/APT_REPO.md" % (base, urllib.parse.quote(tag, safe=""))
    lines = [
        START,
        "## Downloads",
        "",
        "The recommended way to install is the apt repository: see "
        "[docs/APT_REPO.md](%s). With single files, take "
        "`network-manager-gpclient` and one desktop package (`-gnome`, `-plasma-5` or "
        "`-plasma-6`) from the same row and architecture and install them together, "
        "e.g. `sudo apt install ./network-manager-gpclient_*.deb "
        "./network-manager-gpclient-gnome_*.deb`. On Ubuntu 22.04 `python3-sdbus` is "
        "not in apt: run `pip3 install sdbus` first." % docs,
        "",
        "| Ubuntu | " + " | ".join(arches) + " |",
        "|---|" + "---|" * len(arches),
    ]
    for codename in codenames:
        row = []
        for arch in arches:
            links = [
                "[%s](%s)" % (short, url)
                for short, url in sorted(cells.get((codename, arch), []), key=cell_key)
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


def read_text(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="OWNER/REPO of the release")
    parser.add_argument("--tag", required=True, help="the release's tag, e.g. v1.4.2")
    parser.add_argument("--release-json", required=True, metavar="FILE",
                        help="output of: gh release view TAG --json assets,body")
    args = parser.parse_args(argv)
    try:
        release = json.loads(read_text(args.release_json))
        if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
            raise NotesError("the release JSON has no list of assets")
        body = release.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise NotesError("the release JSON has a body that is not text")
        text = merge(body, render(args.repo, args.tag, release["assets"]))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print("error: cannot read the input: %s" % exc, file=sys.stderr)
        return 1
    except NotesError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fail when a release tag does not match the upstream version in debian/changelog.

    check_tag_version.py v2.0.0 debian/changelog

The tag is v<version>. The version is the one in the top line of the changelog
without the Debian revision, if there is one (1.5.0-1 gives 1.5.0, a native 2.0.0 stays 2.0.0). Used by
.github/workflows/build-release.yml for tag pushes. Standard library only.
"""

import re
import sys

TAG_RE = re.compile(r"v([0-9]+(?:\.[0-9]+)*)")
TOP_RE = re.compile(r"[a-z0-9][a-z0-9+.-]* \(([^)\s]+)\)")


def split_changelog_version(text):
    """(upstream version, Debian revision or None if native) of the top changelog entry"""
    match = TOP_RE.match(text.splitlines()[0] if text.strip() else "")
    if not match:
        raise ValueError("the first line of the changelog is not 'package (version) ...'")
    version = match.group(1).split(":", 1)[-1]
    upstream, _, revision = version.rpartition("-")
    return (upstream, revision) if upstream else (version, None)


def changelog_version(text):
    """The upstream version of the top changelog entry (epoch and Debian revision removed)"""
    return split_changelog_version(text)[0]


def check(tag, text):
    """Raise ValueError when the tag is not v<upstream version of the changelog>"""
    match = TAG_RE.fullmatch(tag)
    if not match:
        raise ValueError(f"'{tag}' is not a release tag like v2.0.0")
    version, revision = split_changelog_version(text)
    if match.group(1) != version:
        raise ValueError(f"tag {tag} does not match the version {version} in debian/changelog")
    # Pure Python: dpkg may be missing where this runs. Only the numeric parts matter here.
    numbers = tuple(int(n) for n in re.match(r"[0-9]+(?:\.[0-9]+)*", version).group().split("."))
    if revision is not None and numbers >= (2, 0, 0):
        raise ValueError(
            f"from 2.0.0 on the version is native: write {version}, not {version}-{revision}"
        )


def main(argv):
    if len(argv) != 3:
        print("usage: check_tag_version.py <tag> <changelog>", file=sys.stderr)
        return 2
    try:
        with open(argv[2], encoding="utf-8") as handle:
            check(argv[1], handle.read())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

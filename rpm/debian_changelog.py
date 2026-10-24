#!/usr/bin/env python3
"""
Derive the RPM version, release and %changelog from debian/changelog, so that
debian/changelog stays the single source of truth for both package formats.

Usage:
    debian_changelog.py version   [--changelog FILE]   print the upstream version
    debian_changelog.py release   [--changelog FILE]   print the Debian revision
    debian_changelog.py changelog [--changelog FILE]   print the RPM %changelog entries
    debian_changelog.py pr-suffix PR RUN               print ".pr<PR>.<RUN>" (digits only)

Standard library only: it runs on the host of a build and in CI.
"""

import argparse
import datetime
import os
import re
import sys

DEFAULT_CHANGELOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "debian", "changelog"
)

HEADER_RE = re.compile(r"^(?P<source>\S+) \((?P<version>[^)\s]+)\) [^;]+;.*$")
TRAILER_RE = re.compile(
    r"^ -- (?P<who>.+?)  (?:[A-Za-z]{3}, )?(?P<day>\d{1,2}) (?P<month>[A-Za-z]{3}) "
    r"(?P<year>\d{4}) \d{2}:\d{2}:\d{2} [+-]\d{4}$"
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Characters RPM accepts in Version and Release (no "-", no ":")
RPM_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~^]*$")


class ChangelogError(ValueError):
    pass


def split_version(debian_version):
    """Return (upstream version, Debian revision) of "1.5.0-1" -> ("1.5.0", "1")."""
    if ":" in debian_version:
        raise ChangelogError(f"version {debian_version!r} has an epoch, which is not supported")
    upstream, sep, revision = debian_version.rpartition("-")
    if not sep:
        # Native package without a revision
        upstream, revision = debian_version, "1"
    # A hyphen is allowed in a Debian upstream version, not in an RPM Version
    upstream = upstream.replace("-", "_")
    if not RPM_VERSION_RE.match(upstream):
        raise ChangelogError(f"upstream version {upstream!r} is not valid for an RPM")
    if not RPM_VERSION_RE.match(revision):
        raise ChangelogError(f"Debian revision {revision!r} is not valid for an RPM")
    return upstream, revision


def pr_suffix(pr, run):
    """Return ".pr<PR>.<RUN>". Both are digits only, or both are empty (no suffix)."""
    if not pr and not run:
        return ""
    if not (pr and run):
        raise ChangelogError("PR_NUMBER and RUN_NUMBER must be set together")
    if not (pr.isascii() and pr.isdigit() and run.isascii() and run.isdigit()):
        raise ChangelogError("PR_NUMBER and RUN_NUMBER must contain digits only")
    return f".pr{pr}.{run}"


def parse(text):
    """Return a list of entries: dicts with version, who, date and items (newest first)."""
    entries = []
    current = None
    item = None
    for line in text.splitlines():
        header = HEADER_RE.match(line)
        if header and not line.startswith(" "):
            current = {"version": header.group("version"), "items": [], "who": None, "date": None}
            item = None
            continue
        if current is None:
            if line.strip():
                raise ChangelogError(f"text before the first changelog header: {line!r}")
            continue
        trailer = TRAILER_RE.match(line)
        if trailer:
            month = trailer.group("month").capitalize()
            if month not in MONTHS:
                raise ChangelogError(f"unknown month in {line!r}")
            try:
                date = datetime.date(
                    int(trailer.group("year")), MONTHS.index(month) + 1, int(trailer.group("day"))
                )
            except ValueError as exc:
                raise ChangelogError(f"invalid date in {line!r}: {exc}") from exc
            current["who"] = trailer.group("who")
            current["date"] = date
            entries.append(current)
            current = None
            item = None
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^  [*+-] ", line):
            item = [stripped[2:].strip()]
            current["items"].append(item)
        elif item is not None:
            item.append(stripped)
        else:
            raise ChangelogError(f"unexpected line in changelog entry {current['version']}: {line!r}")
    if current is not None:
        raise ChangelogError(f"entry {current['version']} has no trailer line")
    if not entries:
        raise ChangelogError("no changelog entries")
    return entries


def rpm_changelog(text):
    """Return the entries of an RPM %changelog (without the "%changelog" line)."""
    out = []
    for entry in parse(text):
        upstream, revision = split_version(entry["version"])
        date = entry["date"]
        out.append(
            f"* {WEEKDAYS[date.weekday()]} {MONTHS[date.month - 1]} {date.day:02d} {date.year} "
            f"{entry['who']} - {upstream}-{revision}"
        )
        for words in entry["items"]:
            # "%" starts a macro in a changelog too
            out.append("- " + " ".join(words).replace("%", "%%"))
        out.append("")
    return "\n".join(out)


def read_changelog(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("version", "release", "changelog"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--changelog", default=DEFAULT_CHANGELOG)
    cmd = sub.add_parser("pr-suffix")
    cmd.add_argument("pr")
    cmd.add_argument("run")
    args = parser.parse_args(argv)
    try:
        if args.command == "pr-suffix":
            print(pr_suffix(args.pr, args.run))
            return 0
        text = read_changelog(args.changelog)
        if args.command == "changelog":
            print(rpm_changelog(text))
        else:
            upstream, revision = split_version(parse(text)[0]["version"])
            print(upstream if args.command == "version" else revision)
    except (ChangelogError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

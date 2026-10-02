#!/usr/bin/env python3
"""Checks the result of a package upgrade (used by .github/scripts/upgrade-test.sh).

    check_upgrade.py --before before.txt --after after.txt \\
        --expected-version 1.5.0-1~noble1 --scenario plasma --codename noble

The two files are the output of
`dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\\n' 'network-manager-gpclient*'`
taken before and after the upgrade. Exit status: 0 = the upgrade is clean,
1 = problems (all of them are listed on stderr), 2 = bad arguments or input.
Standard library only.
"""

import argparse
import re
import sys

PREFIX = "network-manager-gpclient"
CORE = PREFIX
GNOME = PREFIX + "-gnome"
SCENARIOS = ("gnome", "plasma", "plasma-new")
# The Plasma package of each Ubuntu release (the old name is only transitional).
# Keep in sync with debian/control.ubuntu*: it is the first package that
# network-manager-gpclient-plasma depends on there (tests/unit checks it).
PLASMA_PACKAGE = {
    "jammy": PREFIX + "-plasma-5",  # 22.04
    "noble": PREFIX + "-plasma-5",  # 24.04
    "oracular": PREFIX + "-plasma-6",  # 24.10
    "resolute": PREFIX + "-plasma-6",  # 26.04
}
STATUS_RE = re.compile(r"^[a-zA-Z]{2,3}$")


def parse_status(text):
    """{package: (version, state)} from the dpkg-query output.

    `state` is the two letters of the status (want, status): "ii" is installed,
    "un" is only known to dpkg (no version), "rc" is removed with its
    configuration left, "iU" unpacked but not configured. Raises ValueError for
    an empty or malformed input."""
    packages = {}
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) == 3:
            name, version, status = fields
        elif len(fields) == 2 and STATUS_RE.match(fields[1]):
            # A package without a version, e.g. "network-manager-gpclient-foo  un "
            name, version, status = fields[0], "", fields[1]
        else:
            raise ValueError(f"line {number}: expected '<package> <version> <status>', got {line!r}")
        if not STATUS_RE.match(status):
            raise ValueError(f"line {number}: {status!r} is not a dpkg status abbreviation")
        if name in packages:
            raise ValueError(f"line {number}: package {name} is listed twice")
        packages[name] = (version, status[:2])
    if not packages:
        raise ValueError("no packages listed")
    return packages


def desktop_packages(scenario, codename):
    """The GUI packages the scenario must end up with"""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r} (known: {', '.join(SCENARIOS)})")
    if scenario == "gnome":
        return [GNOME]
    if codename not in PLASMA_PACKAGE:
        raise ValueError(f"unknown Ubuntu codename {codename!r} (known: {', '.join(PLASMA_PACKAGE)})")
    return [PLASMA_PACKAGE[codename]]


def check(before, after, expected_version, scenario, codename):
    """The list of problems of the upgrade (empty = clean). `before` and `after`
    come from parse_status()."""
    if not expected_version:
        raise ValueError("the expected version is empty")
    wanted = desktop_packages(scenario, codename)
    problems = []

    for name, (version, state) in sorted(before.items()):
        if state == "un":
            continue
        if name not in after or after[name][1] == "un":
            problems.append(f"{name} {version} was installed before and is gone after the upgrade")
        elif after[name][1] != "ii":
            problems.append(f"{name} was installed before and is in state '{after[name][1]}' after the upgrade")

    for name, (version, state) in sorted(after.items()):
        if state == "un":
            continue
        if state != "ii" and name not in before:
            problems.append(f"{name} is in state '{state}' after the upgrade (not fully installed)")
        elif state == "ii" and version != expected_version:
            problems.append(
                f"{name} is at version {version} after the upgrade, expected {expected_version} (kept back?)"
            )

    for name in wanted:
        if name not in after or after[name][1] != "ii":
            problems.append(f"{name} is not installed after the upgrade (the {scenario} scenario on {codename} needs it)")
    if scenario != "gnome":
        for other in sorted(set(PLASMA_PACKAGE.values()) - set(wanted)):
            if other in after and after[other][1] != "un":
                problems.append(f"{other} is installed after the upgrade, but {codename} uses {wanted[0]}")
    if CORE not in after or after[CORE][1] != "ii":
        problems.append(f"{CORE} is not installed after the upgrade")
    return problems


def read(path):
    with open(path, encoding="utf-8") as handle:
        return parse_status(handle.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--before", required=True, help="dpkg-query output before the upgrade")
    parser.add_argument("--after", required=True, help="dpkg-query output after the upgrade")
    parser.add_argument("--expected-version", required=True, help="the version of the new packages")
    parser.add_argument("--scenario", required=True, help="one of: " + ", ".join(SCENARIOS))
    parser.add_argument("--codename", required=True, help="Ubuntu codename, e.g. noble")
    args = parser.parse_args(argv)
    try:
        problems = check(
            read(args.before), read(args.after), args.expected_version, args.scenario, args.codename
        )
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if problems:
        print(f"UPGRADE CHECK FAILED ({args.scenario}, {args.codename}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"OK: upgrade clean ({args.scenario}, {args.codename}), all packages at {args.expected_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

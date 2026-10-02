#!/usr/bin/env python3
"""Checks the result of a package upgrade or fresh install (used by
.github/scripts/upgrade-test.sh).

    check_upgrade.py --before before.txt --after after.txt \\
        --expected-version 1.5.0-1~noble1 --scenario plasma --codename noble \\
        --plasma-files plasma-files.txt
    check_upgrade.py --fresh --after after.txt ...      # nothing was installed before
    check_upgrade.py --print-plugin plasma-files.txt    # the path of the editor plugin

The two files are the output of
`dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\\n' 'network-manager-gpclient*'`
taken before and after the upgrade. With --fresh there is no "before": every
package that is installed must be fully installed at the expected version.
The optional --plasma-files is the output of
`dpkg -L network-manager-gpclient-plasma` after the upgrade: the editor plugin
it lists must exist and be in the Qt directory of the release (it is owned by
the package: the list is the package's own). --print-plugin prints that plugin
for gui-smoke.sh. Exit status: 0 = clean, 1 = problems (all of them are listed on
stderr), 2 = bad arguments or input. Standard library only.
"""

import argparse
import os
import re
import sys

PREFIX = "network-manager-gpclient"
CORE = PREFIX
GNOME = PREFIX + "-gnome"
PLASMA = PREFIX + "-plasma"
SCENARIOS = ("gnome", "plasma")
# The Qt the Plasma package of each Ubuntu release is built with: the directory
# of its editor plugin, /usr/lib/<multiarch>/<qt>/plugins/. Keep in sync with
# debian/control.ubuntu*: qtbase5-dev or qt6-base-dev in Build-Depends
# (tests/unit checks it).
PLASMA_QT = {
    "jammy": "qt5",  # 22.04
    "noble": "qt5",  # 24.04
    "oracular": "qt6",  # 24.10
    "resolute": "qt6",  # 26.04
}
PLUGIN_NAME = "plasmanetworkmanagement_gpclientui.so"
PLUGIN_RE = re.compile(r"^/usr/lib/[^/]+/(qt[56])/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui\.so$")
# The packages of the former split: they are not built any more
FORMER_RE = re.compile("^" + re.escape(PLASMA) + "-[56]$")
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
    if codename not in PLASMA_QT:
        raise ValueError(f"unknown Ubuntu codename {codename!r} (known: {', '.join(PLASMA_QT)})")
    return [PLASMA]


def plugins_of(files):
    """The editor plugins in `files` (the output of dpkg -L for the Plasma package)"""
    return [line.strip() for line in files.splitlines() if line.strip().endswith(PLUGIN_NAME)]


def check_plugin(files, codename, exists=os.path.isfile):
    """The problems of the editor plugin in `files` (the output of dpkg -L for the Plasma package);
    `exists(path)` tells whether a listed file is on disk"""
    if codename not in PLASMA_QT:
        raise ValueError(f"unknown Ubuntu codename {codename!r} (known: {', '.join(PLASMA_QT)})")
    plugins = plugins_of(files)
    if not plugins:
        return [f"{PLASMA} lists no {PLUGIN_NAME}"]
    problems = []
    for plugin in plugins:
        if not exists(plugin):
            problems.append(f"{plugin} of {PLASMA} is missing on disk")
        match = PLUGIN_RE.match(plugin)
        if not match:
            problems.append(f"{plugin} is not in /usr/lib/<multiarch>/<qt>/plugins/plasma/network/vpn/")
        elif match.group(1) != PLASMA_QT[codename]:
            problems.append(f"{plugin} is a {match.group(1)} plugin, {codename} uses {PLASMA_QT[codename]}")
    return problems


def check(before, after, expected_version, scenario, codename):
    """The list of problems of the upgrade (empty = clean). `before` and `after`
    come from parse_status(). `before` is None for a fresh install: nothing was
    installed, so everything installed must be fully installed at the expected
    version."""
    if not expected_version:
        raise ValueError("the expected version is empty")
    wanted = desktop_packages(scenario, codename)
    problems = []
    when = "after the upgrade" if before is not None else "after the install"
    kept_back = " (kept back?)" if before is not None else ""
    before = before or {}

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
            problems.append(f"{name} is in state '{state}' {when} (not fully installed)")
        elif state == "ii" and version != expected_version:
            problems.append(
                f"{name} is at version {version} {when}, expected {expected_version}{kept_back}"
            )

    for name in wanted:
        if name not in after or after[name][1] != "ii":
            problems.append(f"{name} is not installed {when} (the {scenario} scenario on {codename} needs it)")
    for name in sorted(after):
        if FORMER_RE.match(name) and after[name][1] != "un":
            problems.append(f"{name} is installed {when}, but there is only {PLASMA}")
    if CORE not in after or after[CORE][1] != "ii":
        problems.append(f"{CORE} is not installed {when}")
    return problems


def read(path):
    with open(path, encoding="utf-8") as handle:
        return parse_status(handle.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--before", help="dpkg-query output before the upgrade")
    parser.add_argument("--fresh", action="store_true", help="a fresh install: there is no --before")
    parser.add_argument("--after", help="dpkg-query output after the upgrade")
    parser.add_argument("--expected-version", help="the version of the new packages")
    parser.add_argument("--scenario", help="one of: " + ", ".join(SCENARIOS))
    parser.add_argument("--codename", help="Ubuntu codename, e.g. noble")
    parser.add_argument("--plasma-files", help="dpkg -L output of the Plasma package after the upgrade")
    parser.add_argument("--root", default="", help="prefix of the paths in --plasma-files on disk (default: none)")
    parser.add_argument("--print-plugin", metavar="FILES", help="print the editor plugin listed in this dpkg -L output")
    args = parser.parse_args(argv)

    if args.print_plugin:
        try:
            with open(args.print_plugin, encoding="utf-8") as handle:
                plugins = plugins_of(handle.read())
        except OSError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        if not plugins:
            print(f"ERROR: {PLASMA} lists no {PLUGIN_NAME}", file=sys.stderr)
            return 1
        print(plugins[0])
        return 0

    for option in ("after", "expected_version", "scenario", "codename"):
        if not getattr(args, option):
            parser.error("--%s is required" % option.replace("_", "-"))
    if bool(args.before) == args.fresh:
        parser.error("give exactly one of --before and --fresh")
    try:
        problems = check(
            read(args.before) if args.before else None, read(args.after), args.expected_version, args.scenario,
            args.codename,
        )
        if args.plasma_files:
            if args.scenario != "plasma":
                raise ValueError("--plasma-files needs the plasma scenario")
            with open(args.plasma_files, encoding="utf-8") as handle:
                problems += check_plugin(
                    handle.read(), args.codename, lambda path: os.path.isfile(args.root + path)
                )
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    what = "install" if args.fresh else "upgrade"
    if problems:
        print(f"{what.upper()} CHECK FAILED ({args.scenario}, {args.codename}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"OK: {what} clean ({args.scenario}, {args.codename}), all packages at {args.expected_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

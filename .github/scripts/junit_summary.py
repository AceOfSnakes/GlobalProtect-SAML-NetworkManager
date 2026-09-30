#!/usr/bin/env python3
"""Markdown summary of a pytest junit file, for $GITHUB_STEP_SUMMARY.

    junit_summary.py [--title TITLE] junit.xml >> "$GITHUB_STEP_SUMMARY"

Shown when somebody opens the job from the checks of a pull request:
passed/failed/skipped/errors and the names of the first failing tests. A
missing or unreadable report is said so and never fails the job - the test step
has already reported the real result. Standard library only, Python 3.10+.
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET

MAX_BYTES = 10 * 1024 * 1024
MAX_LISTED = 10


def escape(text):
    """Keep a test name from becoming markdown or HTML."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return re.sub(r"([`|<>\[\]\\*_])", r"\\\1", text)[:200]


def _count(element, attribute):
    value = element.get(attribute, "0").strip()
    if not re.fullmatch(r"\d{1,9}", value):
        raise ValueError("bad %s=%r" % (attribute, value))
    return int(value)


def parse(path):
    """((passed, failed, skipped, errors), [failing test names]); ValueError
    when `path` is not a readable junit report."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            raise ValueError("file too large")
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ValueError(str(exc))
    # Entity expansion is the one thing in XML that can hurt a parser
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise ValueError("DOCTYPE/ENTITY not allowed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("not XML: %s" % exc)

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = [s for s in root if s.tag == "testsuite"]
    else:
        raise ValueError("unexpected root element <%s>" % root.tag)
    if not suites:
        raise ValueError("no testsuite")

    total = failed = skipped = errors = 0
    for suite in suites:
        total += _count(suite, "tests")
        failed += _count(suite, "failures")
        skipped += _count(suite, "skipped")
        errors += _count(suite, "errors")

    names = []
    for case in root.iter("testcase"):
        if any(child.tag in ("failure", "error") for child in case):
            names.append("%s::%s" % (case.get("classname", ""), case.get("name", "")))
    passed = max(total - failed - skipped - errors, 0)
    return (passed, failed, skipped, errors), names


def summary(title, path):
    heading = escape(title) if title else "Unit tests"
    try:
        (passed, failed, skipped, errors), names = parse(path)
    except ValueError as exc:
        if not os.path.exists(path):
            return "### ⚠️ %s\n\nno report: the tests did not get as far as writing `%s`.\n" % (
                heading, escape(os.path.basename(path)))
        return "### ⚠️ %s\n\nno report: cannot read it (%s).\n" % (heading, escape(exc))

    if failed or errors:
        emoji = "❌"
    elif passed == 0:
        emoji = "⚠️"
    else:
        emoji = "✅"
    lines = [
        "### %s %s" % (emoji, heading),
        "",
        "| Passed | Failed | Skipped | Errors |",
        "|--:|--:|--:|--:|",
        "| %d | %d | %d | %d |" % (passed, failed, skipped, errors),
        "",
    ]
    if passed == 0 and not (failed or errors):
        lines += ["No test passed: nothing was collected or everything was skipped.", ""]
    if names:
        lines.append("Failing:")
        lines += ["- `%s`" % name.replace("`", "'")[:200] for name in names[:MAX_LISTED]]
        if len(names) > MAX_LISTED:
            lines.append("- and %d more" % (len(names) - MAX_LISTED))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--title", default="")
    parser.add_argument("junit", help="the junit xml file")
    args = parser.parse_args(argv)
    sys.stdout.write(summary(args.title, args.junit))
    return 0


if __name__ == "__main__":
    sys.exit(main())

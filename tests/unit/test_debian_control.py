"""
Packaging transition of the Plasma editor package. v1.4.1 shipped
network-manager-gpclient-plasma; it was split into -plasma-5 and -plasma-6.
Without Replaces/Breaks dpkg refuses the new package (same files as the old
one), and the old package, which pins the core package to its own version,
keeps apt on 1.4.1. So every debian/control.ubuntu<version> must replace and
break the old package below 1.5.0~ and ship an empty transitional package of
the old name that pulls in the new one.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import glob
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEBIAN = os.path.join(ROOT, "debian")
CONTROLS = sorted(glob.glob(os.path.join(DEBIAN, "control*")))

OLD = "network-manager-gpclient-plasma"
RELATION = OLD + " (<< 1.5.0~)"
# What the transitional package has to pull in, per Ubuntu release
EXPECTED_DEPENDS = {
    "control.ubuntu22.04": "network-manager-gpclient-plasma-5",
    "control.ubuntu24.04": "network-manager-gpclient-plasma-5",
    "control.ubuntu24.10": "network-manager-gpclient-plasma-6 | network-manager-gpclient-plasma-5",
    "control.ubuntu26.04": "network-manager-gpclient-plasma-6",
}


def parse_control(text):
    """The binary stanzas of a control file as {package: {lowercase field: value}}"""
    packages = {}
    for stanza in text.split("\n\n"):
        fields = {}
        last = None
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t"):
                if last:
                    fields[last] += "\n" + line.strip()
            elif ":" in line:
                last, _, value = line.partition(":")
                last = last.strip().lower()
                fields[last] = value.strip()
        if "package" in fields:
            packages[fields["package"]] = fields
    return packages


def relations(value):
    """['a (<< 1)', 'b'] of 'a (<< 1),\\n b'"""
    return [" ".join(item.split()) for item in value.replace("\n", " ").split(",") if item.strip()]


def check_control(text):
    """Problems with the transition from network-manager-gpclient-plasma (empty list when fine)"""
    packages = parse_control(text)
    problems = []
    editors = sorted(name for name in packages if name.startswith(OLD + "-"))
    if not editors:
        problems.append("no network-manager-gpclient-plasma-<N> package")
    for name in editors:
        for field in ("replaces", "breaks"):
            if RELATION not in relations(packages[name].get(field, "")):
                problems.append("%s lacks %s: %s" % (name, field.capitalize(), RELATION))

    old = packages.get(OLD)
    if old is None:
        problems.append("no transitional package %s" % OLD)
        return problems
    for field, wanted in (("architecture", "any"), ("section", "oldlibs"), ("priority", "optional")):
        if old.get(field) != wanted:
            problems.append("%s has %s %r, not %r" % (OLD, field.capitalize(), old.get(field), wanted))
    description = old.get("description", "").lower()
    if "transitional package" not in description or "safely removed" not in description:
        problems.append("%s: the description does not say it is a transitional package that can be safely removed" % OLD)
    depends = [alternative.split("(")[0].strip()
               for item in relations(old.get("depends", ""))
               for alternative in item.split("|")]
    pulled = [name for name in depends if name in editors]
    if not pulled:
        problems.append("%s depends on none of the plasma packages of this file (%s)" % (OLD, ", ".join(editors)))
    return problems


GOOD = """\
Source: network-manager-gpclient
Section: net

Package: network-manager-gpclient
Architecture: any
Depends: ${shlibs:Depends}
Description: core

Package: network-manager-gpclient-plasma-5
Architecture: any
Depends: ${shlibs:Depends},
         network-manager-gpclient (= ${binary:Version}),
         plasma-nm
Replaces: network-manager-gpclient-plasma (<< 1.5.0~)
Breaks: network-manager-gpclient-plasma (<< 1.5.0~)
Description: Plasma GUI
 text

Package: network-manager-gpclient-plasma
Architecture: any
Section: oldlibs
Priority: optional
Depends: ${misc:Depends},
         network-manager-gpclient-plasma-5
Description: transitional package for network-manager-gpclient-plasma-5
 This is a transitional package. It can be safely removed.
"""


class TestCheckControl:
    def test_a_correct_control_file_has_no_problems(self):
        assert check_control(GOOD) == []

    @pytest.mark.parametrize("name, old, new", [
        ("no Replaces", "Replaces: network-manager-gpclient-plasma (<< 1.5.0~)\n", ""),
        ("no Breaks", "Breaks: network-manager-gpclient-plasma (<< 1.5.0~)\n", ""),
        ("Replaces on another version", "Replaces: network-manager-gpclient-plasma (<< 1.5.0~)",
         "Replaces: network-manager-gpclient-plasma (<< 1.4.0)"),
        ("Breaks on another package", "Breaks: network-manager-gpclient-plasma (<< 1.5.0~)",
         "Breaks: network-manager-gpclient (<< 1.5.0~)"),
        ("not oldlibs", "Section: oldlibs", "Section: net"),
        ("not optional", "Priority: optional\n", ""),
        ("Architecture all", "Package: network-manager-gpclient-plasma\nArchitecture: any",
         "Package: network-manager-gpclient-plasma\nArchitecture: all"),
        ("depends on a package that is not in the file",
         "         network-manager-gpclient-plasma-5\nDescription: transitional",
         "         network-manager-gpclient-plasma-6\nDescription: transitional"),
        ("does not say it can be removed", "It can be safely removed.", "Keep it."),
        ("no transitional package", GOOD[GOOD.index("Package: network-manager-gpclient-plasma\n"):], ""),
    ])
    def test_a_broken_transition_is_reported(self, name, old, new):
        assert old in GOOD, name
        assert check_control(GOOD.replace(old, new)) != [], name

    def test_a_file_without_a_plasma_package_is_reported(self):
        assert check_control(GOOD.split("Package: network-manager-gpclient-plasma-5")[0]) != []


class TestControlFiles:
    @pytest.mark.parametrize("path", CONTROLS, ids=os.path.basename)
    def test_the_transition_is_declared(self, path):
        with open(path, encoding="utf-8") as handle:
            assert check_control(handle.read()) == []

    @pytest.mark.parametrize("name, wanted", sorted(EXPECTED_DEPENDS.items()))
    def test_the_transitional_package_pulls_in_the_plasma_package_of_the_release(self, name, wanted):
        with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
            depends = relations(parse_control(handle.read())[OLD]["depends"])
        assert depends == ["${misc:Depends}", wanted]

    @pytest.mark.parametrize("name, wrong", [
        ("control.ubuntu22.04", "network-manager-gpclient-plasma-6"),
        ("control.ubuntu26.04", "network-manager-gpclient-plasma-5"),
        ("control.ubuntu24.10", "network-manager-gpclient-plasma-5"),
    ])
    def test_the_transitional_package_does_not_pull_in_the_wrong_plasma_package(self, name, wrong):
        with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
            depends = relations(parse_control(handle.read())[OLD]["depends"])
        assert depends != ["${misc:Depends}", wrong]

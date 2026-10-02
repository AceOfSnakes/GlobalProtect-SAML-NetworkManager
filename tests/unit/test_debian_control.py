"""
Packaging transition of the Plasma editor package. v1.4.1 shipped
network-manager-gpclient-plasma; it was split into -plasma-5 and -plasma-6.
Without Replaces/Breaks dpkg refuses the new package (same files as the old
one), and the old package, which pins the core package to its own version,
keeps apt on 1.4.1. So every debian/control.ubuntu<version> must replace and
break the old package below 1.5.0~ and ship an empty transitional package of
the old name that pulls in the new one, at the same version.

Ubuntu 26.04 has no Plasma 5. A system upgraded from 24.04 would keep the
-plasma-5 of 24.04, which pins the core package to the version of 24.04, so
control.ubuntu26.04 also ships a transitional -plasma-5 that pulls in -plasma-6.
The transitional packages are not downloads: .github/scripts/release_notes.py
lists them (TRANSITIONAL) and this file checks that list against the control files.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import glob
import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, ".github", "scripts"))
import release_notes  # noqa: E402

DEBIAN = os.path.join(ROOT, "debian")
CONTROLS = sorted(glob.glob(os.path.join(DEBIAN, "control*")))
UBUNTU_CONTROLS = [p for p in CONTROLS if re.fullmatch(r"control\.ubuntu\d\d\.\d\d", os.path.basename(p))]

CORE = "network-manager-gpclient"
OLD = "network-manager-gpclient-plasma"
PLASMA_5 = OLD + "-5"
PLASMA_6 = OLD + "-6"
RELATION = OLD + " (<< 1.5.0~)"
VERSIONED = " (= ${binary:Version})"
P5 = PLASMA_5 + VERSIONED
P6 = PLASMA_6 + VERSIONED
# What the transitional package has to pull in, per control file; debian/control
# is a copy of the one of Ubuntu 24.04
EXPECTED_DEPENDS = {
    "control": P5,
    "control.ubuntu22.04": P5,
    "control.ubuntu24.04": P5,
    "control.ubuntu24.10": P6 + " | " + P5,
    "control.ubuntu26.04": P6,
}
# Ubuntu 26.04 has no Plasma 5: its -plasma-5 is an empty package that pulls in -plasma-6
EXPECTED_PLASMA_5_TRANSITIONAL = {"control.ubuntu26.04": P6}
# What the core package recommends (24.10 ships Plasma 6: it comes first)
EXPECTED_RECOMMENDS = {
    "control": CORE + "-gnome | " + PLASMA_5,
    "control.ubuntu22.04": CORE + "-gnome | " + PLASMA_5,
    "control.ubuntu24.04": CORE + "-gnome | " + PLASMA_5,
    "control.ubuntu24.10": CORE + "-gnome | " + PLASMA_6 + " | " + PLASMA_5,
    "control.ubuntu26.04": CORE + "-gnome | " + PLASMA_6,
}


def parse_control(text):
    """The binary stanzas of a control file as {package: {lowercase field: value}}"""
    packages = {}
    # A blank line may hold spaces or tabs and end in CRLF
    for stanza in re.split(r"\n[ \t]*\r?\n", text):
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


def alternatives(item):
    """['a (= 1)', 'b (= 1)'] of 'a (= 1) | b (= 1)'"""
    return [" ".join(alternative.split()) for alternative in item.split("|")]


def check_control(text):
    """Problems with the transition from network-manager-gpclient-plasma (empty list when fine)"""
    packages = parse_control(text)
    problems = []
    transitional = sorted(name for name, fields in packages.items()
                          if name.startswith(OLD) and fields.get("section") == "oldlibs")
    editors = sorted(name for name in packages if name.startswith(OLD + "-") and name not in transitional)
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
    for name in transitional:
        fields = packages[name]
        for field, wanted in (("architecture", "any"), ("section", "oldlibs"), ("priority", "optional")):
            if fields.get(field) != wanted:
                problems.append("%s has %s %r, not %r" % (name, field.capitalize(), fields.get(field), wanted))
        description = fields.get("description", "").lower()
        if "transitional package" not in description or "safely removed" not in description:
            problems.append("%s: the description does not say it is a transitional package that can be safely removed"
                            % name)
        # Without the version a newer plasma package would not follow the core package
        pulled = [alternative
                  for item in relations(fields.get("depends", ""))
                  for alternative in alternatives(item)
                  if alternative.split("(")[0].strip() in editors]
        if not pulled:
            problems.append("%s depends on none of the plasma packages of this file (%s)" % (name, ", ".join(editors)))
        for alternative in pulled:
            if not alternative.endswith(VERSIONED):
                problems.append("%s: %r lacks %s" % (name, alternative, VERSIONED.strip()))
    if OLD not in transitional:
        problems.append("%s is not a transitional package (Section: oldlibs)" % OLD)
    return problems


def read(name):
    with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
        return handle.read()


def depends_problems(text, wanted):
    """What is wrong with the Depends of the transitional package OLD in a control file"""
    depends = relations(parse_control(text)[OLD]["depends"])
    return [] if depends == ["${misc:Depends}", wanted] else ["%s depends on %r, not on %r" % (OLD, depends, wanted)]


def mutate_old_depends(text, mutate):
    """The control file with `mutate` applied to the Depends field of the package OLD"""
    start = text.index("\nPackage: %s\n" % OLD)
    first = text.index("Depends:", start)
    last = text.index("\nDescription:", first)
    return text[:first] + mutate(text[first:last]) + text[last:]


def stanza_depends(path, package):
    with open(path, encoding="utf-8") as handle:
        return relations(parse_control(handle.read())[package]["depends"])


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
         network-manager-gpclient-plasma-5 (= ${binary:Version})
Description: transitional package for network-manager-gpclient-plasma-5
 This is a transitional package. It can be safely removed.
"""

# A release without Plasma 5: -plasma-5 is a transitional package as well
GOOD_NO_PLASMA_5 = """\
Source: network-manager-gpclient
Section: net

Package: network-manager-gpclient-plasma-6
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
         network-manager-gpclient-plasma-6 (= ${binary:Version})
Description: transitional package for network-manager-gpclient-plasma-6
 This is a transitional package. It can be safely removed.

Package: network-manager-gpclient-plasma-5
Architecture: any
Section: oldlibs
Priority: optional
Depends: ${misc:Depends},
         network-manager-gpclient-plasma-6 (= ${binary:Version})
Description: transitional package for network-manager-gpclient-plasma-6
 This is a transitional package. It can be safely removed.
"""


class TestParseControl:
    def test_stanzas_are_split_at_a_blank_line(self):
        assert sorted(parse_control(GOOD)) == [CORE, OLD, PLASMA_5]

    @pytest.mark.parametrize("blank", ["\n \n", "\n\t\n", "\n  \t \n", "\n\r\n", "\n \r\n", "\n\n"])
    def test_a_blank_line_with_spaces_or_a_carriage_return_still_ends_a_stanza(self, blank):
        text = "Package: a\nDepends: x\nDescription: A\n text" + blank + "Package: b\nDepends: y\nDescription: B\n"
        packages = parse_control(text)
        assert sorted(packages) == ["a", "b"]
        assert packages["a"]["depends"] == "x"
        assert packages["b"]["depends"] == "y"

    def test_a_crlf_file_is_parsed_like_an_lf_file(self):
        assert parse_control(GOOD.replace("\n", "\r\n")) == parse_control(GOOD)

    @pytest.mark.parametrize("line", [" .", " text", "\t."])
    def test_a_line_that_is_not_blank_does_not_end_a_stanza(self, line):
        packages = parse_control("Package: a\nDescription: A\n" + line + "\nDepends: x\n")
        assert sorted(packages) == ["a"]
        assert packages["a"]["depends"] == "x"

    def test_the_fields_of_one_stanza_do_not_leak_into_the_next(self):
        packages = parse_control(GOOD)
        assert "replaces" not in packages[OLD]
        assert "section" not in packages[PLASMA_5]

    def test_the_transitional_package_is_found_after_a_whitespace_only_line(self):
        assert check_control(GOOD.replace("\n\nPackage: " + OLD + "\n", "\n \nPackage: " + OLD + "\n")) == []

    def test_without_a_blank_line_two_stanzas_are_one(self):
        merged = GOOD.replace("\n\nPackage: " + OLD + "\n", "\nPackage: " + OLD + "\n")
        assert PLASMA_5 not in parse_control(merged)  # the later Package field wins
        assert check_control(merged) != []


class TestCheckControl:
    def test_a_correct_control_file_has_no_problems(self):
        assert check_control(GOOD) == []

    def test_a_file_whose_plasma_5_is_transitional_has_no_problems(self):
        assert check_control(GOOD_NO_PLASMA_5) == []

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
         "         network-manager-gpclient-plasma-5 (= ${binary:Version})\nDescription: transitional",
         "         network-manager-gpclient-plasma-6 (= ${binary:Version})\nDescription: transitional"),
        ("depends without the version", "network-manager-gpclient-plasma-5 (= ${binary:Version})\nDescription: transitional",
         "network-manager-gpclient-plasma-5\nDescription: transitional"),
        ("depends on another version relation", "plasma-5 (= ${binary:Version})\nDescription: transitional",
         "plasma-5 (>= ${binary:Version})\nDescription: transitional"),
        ("does not say it can be removed", "It can be safely removed.", "Keep it."),
        ("no transitional package", GOOD[GOOD.index("Package: network-manager-gpclient-plasma\n"):], ""),
    ])
    def test_a_broken_transition_is_reported(self, name, old, new):
        assert old in GOOD, name
        assert check_control(GOOD.replace(old, new)) != [], name

    @pytest.mark.parametrize("name, old, new", [
        ("-plasma-5 has no Section", "Package: network-manager-gpclient-plasma-5\nArchitecture: any\nSection: oldlibs\n",
         "Package: network-manager-gpclient-plasma-5\nArchitecture: any\n"),
        ("-plasma-5 has Architecture all", "Package: network-manager-gpclient-plasma-5\nArchitecture: any\n",
         "Package: network-manager-gpclient-plasma-5\nArchitecture: all\n"),
        ("-plasma-5 does not pull in -plasma-6",
         "Depends: ${misc:Depends},\n         network-manager-gpclient-plasma-6 (= ${binary:Version})\n"
         "Description: transitional package for network-manager-gpclient-plasma-6\n This is a transitional package. "
         "It can be safely removed.\n",
         "Depends: ${misc:Depends},\n         plasma-nm\n"
         "Description: transitional package for network-manager-gpclient-plasma-6\n This is a transitional package. "
         "It can be safely removed.\n"),
        ("-plasma-5 does not say it can be removed", "It can be safely removed.\n", "Keep it.\n"),
    ])
    def test_a_broken_transitional_plasma_5_is_reported(self, name, old, new):
        # the stanza of -plasma-5 is the last one of the fixture
        marker = "Package: network-manager-gpclient-plasma-5\nArchitecture: any\nSection: oldlibs\n"
        head, tail = GOOD_NO_PLASMA_5.split(marker)
        stanza = marker + tail
        assert old in stanza, name
        assert check_control(head + stanza.replace(old, new, 1)) != [], name

    def test_a_transitional_plasma_5_without_the_version_is_reported(self):
        head, tail = GOOD_NO_PLASMA_5.rsplit("network-manager-gpclient-plasma-6 (= ${binary:Version})", 1)
        assert check_control(head + "network-manager-gpclient-plasma-6" + tail) != []

    def test_a_plasma_5_that_replaces_nothing_is_reported_when_it_is_not_transitional(self):
        text = GOOD_NO_PLASMA_5.replace("Package: network-manager-gpclient-plasma-5\nArchitecture: any\nSection: oldlibs\n"
                                        "Priority: optional\n", "Package: network-manager-gpclient-plasma-5\n"
                                        "Architecture: any\n")
        assert check_control(text) != []

    def test_a_file_without_a_plasma_package_is_reported(self):
        assert check_control(GOOD.split("Package: network-manager-gpclient-plasma-5")[0]) != []


class TestControlFiles:
    def test_every_control_file_is_covered(self):
        assert sorted(os.path.basename(p) for p in CONTROLS) == sorted(EXPECTED_DEPENDS)
        assert sorted(os.path.basename(p) for p in CONTROLS) == sorted(EXPECTED_RECOMMENDS)

    @pytest.mark.parametrize("path", CONTROLS, ids=os.path.basename)
    def test_the_transition_is_declared(self, path):
        with open(path, encoding="utf-8") as handle:
            assert check_control(handle.read()) == []

    @pytest.mark.parametrize("name, wanted", sorted(EXPECTED_DEPENDS.items()))
    def test_the_transitional_package_pulls_in_the_plasma_package_of_the_release(self, name, wanted):
        assert stanza_depends(os.path.join(DEBIAN, name), OLD) == ["${misc:Depends}", wanted]

    @pytest.mark.parametrize("name", sorted(EXPECTED_DEPENDS))
    def test_the_expected_depends_are_accepted_by_the_check(self, name):
        assert depends_problems(read(name), EXPECTED_DEPENDS[name]) == []

    @pytest.mark.parametrize("name", sorted(EXPECTED_DEPENDS))
    @pytest.mark.parametrize("what, mutate", [
        ("the other plasma package", lambda d: d.replace("plasma-6", "@").replace("plasma-5", "plasma-6").replace("@", "plasma-5")),
        ("a package that does not exist", lambda d: d.replace("plasma-6", "plasma-7").replace("plasma-5", "plasma-7")),
        ("no version", lambda d: d.replace(VERSIONED, "")),
        ("another relation", lambda d: d.replace("(= ", "(>= ")),
        ("a fixed version", lambda d: d.replace("${binary:Version}", "1.5.0-1")),
        ("nothing", lambda d: "Depends: ${misc:Depends}"),
    ])
    def test_a_transitional_package_that_pulls_in_the_wrong_thing_is_not_accepted(self, name, what, mutate):
        text = read(name)
        mutated = mutate_old_depends(text, mutate)
        assert mutated != text, "the mutation %r changes nothing in %s" % (what, name)
        assert depends_problems(mutated, EXPECTED_DEPENDS[name]) != []

    @pytest.mark.parametrize("what", ["the first alternative only", "the second alternative only", "swapped"])
    def test_ubuntu_24_10_needs_both_alternatives_in_the_order_plasma_6_first(self, what):
        text = read("control.ubuntu24.10")
        both = P6 + "\n         | " + P5
        assert both in text
        replacement = {"the first alternative only": P6, "the second alternative only": P5,
                       "swapped": P5 + "\n         | " + P6}[what]
        assert depends_problems(text.replace(both, replacement), EXPECTED_DEPENDS["control.ubuntu24.10"]) != []

    @pytest.mark.parametrize("name, wanted", sorted(EXPECTED_RECOMMENDS.items()))
    def test_the_core_package_recommends_the_desktop_packages_of_the_release(self, name, wanted):
        with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
            assert parse_control(handle.read())[CORE]["recommends"] == wanted

    def test_ubuntu_24_10_recommends_plasma_6_before_plasma_5(self):
        with open(os.path.join(DEBIAN, "control.ubuntu24.10"), encoding="utf-8") as handle:
            recommends = parse_control(handle.read())[CORE]["recommends"]
        assert recommends.index(PLASMA_6) < recommends.index(PLASMA_5)

    @pytest.mark.parametrize("name, wanted", sorted(EXPECTED_PLASMA_5_TRANSITIONAL.items()))
    def test_a_release_without_plasma_5_has_a_transitional_plasma_5_for_plasma_6(self, name, wanted):
        with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
            package = parse_control(handle.read())[PLASMA_5]
        assert package["section"] == "oldlibs"
        assert package["architecture"] == "any"
        assert package["priority"] == "optional"
        assert relations(package["depends"]) == ["${misc:Depends}", wanted]
        assert "transitional package" in package["description"]

    @pytest.mark.parametrize("name", [n for n in sorted(EXPECTED_DEPENDS) if n not in EXPECTED_PLASMA_5_TRANSITIONAL])
    def test_a_release_with_plasma_5_has_the_real_package(self, name):
        with open(os.path.join(DEBIAN, name), encoding="utf-8") as handle:
            package = parse_control(handle.read())[PLASMA_5]
        assert package.get("section") != "oldlibs"
        assert "plasma-nm" in package["depends"]
        assert RELATION in relations(package["replaces"])

    @pytest.mark.parametrize("name", ["control.ubuntu22.04", "control.ubuntu24.04", "control.ubuntu24.10", "control.ubuntu26.04"])
    def test_the_transitional_package_does_not_pull_in_the_wrong_plasma_package(self, name):
        wrong = {"control.ubuntu22.04": P6, "control.ubuntu24.04": P6, "control.ubuntu24.10": P5,
                 "control.ubuntu26.04": P5}[name]
        assert stanza_depends(os.path.join(DEBIAN, name), OLD) != ["${misc:Depends}", wrong]


def transitional_drift(text, codename, table):
    """The differences between the packages `text` (a control file) builds as transitional ones for
    `codename` and what `table` (release_notes.TRANSITIONAL) says"""
    built = {name for name, fields in parse_control(text).items() if fields.get("section") == "oldlibs"}
    listed = {name for name, codenames in table.items() if codenames is None or codename in codenames}
    return sorted(built ^ listed)


class TestReleaseNotesKnowTheTransitionalPackages:
    CODENAME = {version: codename for codename, version in release_notes.UBUNTU.items()}

    @staticmethod
    def codename(path):
        return TestReleaseNotesKnowTheTransitionalPackages.CODENAME[os.path.basename(path)[len("control.ubuntu"):]]

    def test_every_ubuntu_release_has_a_control_file(self):
        assert sorted(self.codename(p) for p in UBUNTU_CONTROLS) == sorted(release_notes.UBUNTU)

    @pytest.mark.parametrize("path", UBUNTU_CONTROLS, ids=os.path.basename)
    def test_the_list_matches_the_control_file(self, path):
        with open(path, encoding="utf-8") as handle:
            assert transitional_drift(handle.read(), self.codename(path), release_notes.TRANSITIONAL) == []

    @pytest.mark.parametrize("path", UBUNTU_CONTROLS, ids=os.path.basename)
    def test_is_transitional_agrees_with_the_control_file(self, path):
        codename = self.codename(path)
        with open(path, encoding="utf-8") as handle:
            packages = parse_control(handle.read())
        for name, fields in packages.items():
            if name.startswith(CORE):
                assert release_notes.is_transitional(codename, name) == (fields.get("section") == "oldlibs"), name

    @pytest.mark.parametrize("path", UBUNTU_CONTROLS, ids=os.path.basename)
    def test_a_list_without_the_old_plasma_package_is_found(self, path):
        table = {k: v for k, v in release_notes.TRANSITIONAL.items() if k != OLD}
        assert transitional_drift(read(os.path.basename(path)), self.codename(path), table) == [OLD]

    def test_a_list_without_plasma_5_of_resolute_is_found(self):
        table = {OLD: None}
        assert transitional_drift(read("control.ubuntu26.04"), "resolute", table) == [PLASMA_5]

    def test_a_list_that_has_plasma_5_of_a_release_where_it_is_a_real_package_is_found(self):
        table = {OLD: None, PLASMA_5: {"noble", "resolute"}}
        assert transitional_drift(read("control.ubuntu24.04"), "noble", table) == [PLASMA_5]
        assert transitional_drift(read("control.ubuntu26.04"), "resolute", table) == []

    def test_an_empty_list_is_found_everywhere(self):
        assert transitional_drift(read("control.ubuntu26.04"), "resolute", {}) == [OLD, PLASMA_5]
        assert transitional_drift(read("control.ubuntu22.04"), "jammy", {}) == [OLD]

    def test_a_real_package_listed_for_its_release_is_found(self):
        table = {OLD: None, PLASMA_5: {"resolute"}, PLASMA_6: {"resolute"}}
        assert transitional_drift(read("control.ubuntu26.04"), "resolute", table) == [PLASMA_6]

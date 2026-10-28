"""
Issue #106: the License tags of the RPM spec name the same licenses as the
DEP-5 debian/copyright of the .deb packages.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPEC = os.path.join(ROOT, "rpm", "network-manager-gpclient.spec")
COPYRIGHT = os.path.join(ROOT, "debian", "copyright")
# DEP-5 short names (without "+": only that version) -> SPDX
SPDX = {"LGPL-2.1": "LGPL-2.1-only", "LGPL-2+": "LGPL-2.0-or-later", "GPL-3": "GPL-3.0-only", "MIT": "MIT"}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def spec_licenses(text):
    return {
        part.strip()
        for tag in re.findall(r"^License:\s*(.+)$", text, re.M)
        for part in tag.split(" AND ")
    }


def copyright_licenses(text):
    """Licenses of the Files: paragraphs (not the stand-alone license texts),
    as SPDX; libxml2 is not in the packages (system libxml2)"""
    found = set()
    for paragraph in text.split("\n\n"):
        files = re.search(r"^Files:(.*?)^\S", paragraph + "\nX", re.M | re.S)
        license_ = re.search(r"^License: (\S+)", paragraph, re.M)
        if files and license_ and "deps/libxml2" not in files.group(1):
            found.add(SPDX[license_.group(1)])
    return found


def test_the_spec_names_the_licenses_of_debian_copyright():
    assert spec_licenses(read(SPEC)) == copyright_licenses(read(COPYRIGHT))


def test_no_or_later_for_licenses_that_are_only_one_version():
    assert not {"GPL-3.0-or-later", "LGPL-2.1-or-later"} & spec_licenses(read(SPEC))


def test_a_missing_or_wrong_license_is_found():
    spec = read(SPEC)
    expected = copyright_licenses(read(COPYRIGHT))
    assert spec_licenses(spec.replace(" AND MIT", "")) != expected
    assert spec_licenses(spec.replace("GPL-3.0-only", "GPL-3.0-or-later")) != expected
    old = "License:        GPL-3.0-or-later AND LGPL-2.1-or-later\n"
    assert spec_licenses(old) != expected

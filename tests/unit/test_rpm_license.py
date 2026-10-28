"""
Issue #106: the License tags of the RPM spec name, per package, the licenses
of the files the package ships, as debian/copyright gives them for the .deb
packages, plus what only the RPM bundles (python-sdbus).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPEC = os.path.join(ROOT, "rpm", "network-manager-gpclient.spec")
COPYRIGHT = os.path.join(ROOT, "debian", "copyright")
# DEP-5 short names (without "+": only that version) -> SPDX
SPDX = {"LGPL-2.1": "LGPL-2.1-only", "LGPL-2+": "LGPL-2.0-or-later", "GPL-3": "GPL-3.0-only", "MIT": "MIT"}
# Only the RPM bundles python-sdbus (the .deb uses python3-sdbus): its SPDX headers
RPM_ONLY = {"LGPL-2.1-or-later"}
# What each package ships (its %files): the main package has the service,
# gpclient/gpauth (GPL-3.0-only, MIT crates, the LGPL-2.1-only openconnect fork)
# and libnm-vpn-plugin-gpclient.so (with nm-vpn-plugin-utils, LGPL-2.0-or-later)
EXPECTED = {
    "": {"GPL-3.0-only", "LGPL-2.1-only", "LGPL-2.0-or-later", "MIT"} | RPM_ONLY,
    "gnome": {"LGPL-2.1-only"},
    "plasma": {"LGPL-2.1-only"},
}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def package_licenses(text):
    """{package suffix ("" = main): set of SPDX IDs of its License tag}"""
    found, package = {}, ""
    for line in text.splitlines():
        sub = re.match(r"%package\s+(\S+)", line)
        if sub:
            package = sub.group(1)
        tag = re.match(r"License:\s*(.+)$", line)
        if tag:
            found[package] = {part.strip() for part in tag.group(1).split(" AND ")}
    return found


def copyright_licenses(text):
    """Licenses of the Files: paragraphs (not the stand-alone license texts),
    as SPDX; libxml2 is not in the packages (system libxml2)"""
    found = set()
    for paragraph in text.split("\n\n"):
        files = re.search(r"^Files:(.*?)^\S", paragraph + "\nX", re.M | re.S)
        license_ = re.search(r"^License: (\S+)", paragraph, re.M)
        if files and license_ and "deps/libxml2" not in files.group(1):
            name = license_.group(1)
            assert name in SPDX, f"add the SPDX ID of DEP-5 license {name!r} to SPDX"
            found.add(SPDX[name])
    return found


def test_each_package_names_the_licenses_of_its_files():
    assert package_licenses(read(SPEC)) == EXPECTED


def test_the_packages_name_every_license_of_debian_copyright():
    every = set().union(*EXPECTED.values()) - RPM_ONLY
    assert every == copyright_licenses(read(COPYRIGHT))


@pytest.mark.parametrize("old, new", [
    (" AND MIT", ""),  # a license left out
    ("License:        GPL-3.0-only", "License:        GPL-3.0-or-later"),  # an "or later" upstream does not give
    (" AND LGPL-2.1-or-later", ""),  # the bundled python-sdbus
    (" AND LGPL-2.0-or-later", ""),  # nm-vpn-plugin-utils in the main package
])
def test_a_missing_or_wrong_license_is_found(old, new):
    spec = read(SPEC)
    assert old in spec

    assert package_licenses(spec.replace(old, new, 1)) != EXPECTED


def test_a_license_on_the_wrong_package_is_found():
    # the #106 draft: nm-vpn-plugin-utils on -gnome, not on the main package
    spec = read(SPEC).replace(" AND LGPL-2.0-or-later", "", 1)
    spec = spec.replace("License:        LGPL-2.1-only\n",
                        "License:        LGPL-2.1-only AND LGPL-2.0-or-later\n", 1)

    assert package_licenses(spec) != EXPECTED


def test_the_old_tag_is_found():
    assert package_licenses("License:        GPL-3.0-or-later AND LGPL-2.1-or-later\n") != EXPECTED

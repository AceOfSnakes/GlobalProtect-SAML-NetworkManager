#!/usr/bin/env python3
"""Refresh the translation templates (.pot) and merge them into the .po files.

  python3 scripts/update_pot.py          # what `make pot` runs

Two catalogs (docs/TRANSLATIONS.md):
  po/NetworkManager-gpclient.pot
      GNOME editors and auth dialog (gettext): the files of po/POTFILES.in.
  plugins/plasma/po/plasmanetworkmanagement_gpclientui.pot
      Plasma editor (ki18n): plugins/plasma/*.cpp and the strings of the .ui file.
      KDE's own tool for this is plugins/plasma/Messages.sh (it needs extractrc,
      which is not packaged on most systems), so the strings of the .ui file are
      read here with the standard library and written as i18n() calls.

Needs xgettext and msgmerge (the gettext package).
"""

import glob
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GNOME_DOMAIN = "NetworkManager-gpclient"
PLASMA_DOMAIN = "plasmanetworkmanagement_gpclientui"
PLASMA_DIR = "plugins/plasma"
PLASMA_UI = "gpclientwidget.ui"
BUGS_ADDRESS = "https://github.com/WMP/GlobalProtect-SAML-NetworkManager/issues"


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True)


def c_string(text):
    """text as a C string literal"""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def ui_strings(ui_file):
    """(text, comment) of every translatable <string> of a Qt Designer file"""
    found = []
    # the .ui file is a file of this repository, not input from outside
    for element in ET.parse(ui_file).getroot().iter("string"):  # noqa: S314
        if element.get("notr") == "true" or not element.text:
            continue
        found.append((element.text, element.get("comment")))
    return found


def ui_as_cpp(ui_file):
    """The strings of a .ui file as i18n() calls, as KDE's extractrc writes them"""
    lines = []
    for text, comment in ui_strings(ui_file):
        if comment:
            lines.append(f"i18nc({c_string(comment)}, {c_string(text)});")
        else:
            lines.append(f"i18n({c_string(text)});")
    return "\n".join(lines) + "\n"


def gnome_pot(root, out):
    """Write the GNOME template of the files in po/POTFILES.in to out"""
    run(
        [
            "xgettext", "--from-code=UTF-8", "--add-comments=TRANSLATORS",
            "--add-location=file", "--sort-output", "--keyword=_", "--keyword=N_",
            f"--package-name={GNOME_DOMAIN}", f"--msgid-bugs-address={BUGS_ADDRESS}",
            "--directory=" + root, "--files-from=po/POTFILES.in", "--output=" + os.path.abspath(out),
        ],
        root,
    )  # fmt: skip


def plasma_pot(root, out):
    """Write the Plasma template (the .cpp files and the .ui file) to out"""
    plasma = os.path.join(root, PLASMA_DIR)
    sources = sorted(os.path.basename(p) for p in glob.glob(os.path.join(plasma, "*.cpp")))
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "rc.cpp"), "w", encoding="utf-8") as handle:
            handle.write(ui_as_cpp(os.path.join(plasma, PLASMA_UI)))
        run(
            [
                "xgettext", "--from-code=UTF-8", "--language=C++", "--kde",
                "--add-comments=i18n", "--add-location=file", "--sort-output",
                "--keyword=i18n:1", "--keyword=i18nc:1c,2",
                "--keyword=i18np:1,2", "--keyword=i18ncp:1c,2,3",
                f"--package-name={PLASMA_DOMAIN}", f"--msgid-bugs-address={BUGS_ADDRESS}",
                "--directory=" + tmp, "--directory=" + plasma,
                "--output=" + os.path.abspath(out), "rc.cpp", *sources,
            ],
            tmp,
        )  # fmt: skip


def linguas(root):
    with open(os.path.join(root, "po", "LINGUAS"), encoding="utf-8") as handle:
        lines = (line.split("#")[0].strip() for line in handle)
        return [line for line in lines if line]


def merge(pot, po):
    run(["msgmerge", "--update", "--backup=none", "--quiet", po, pot], ROOT)


def main():
    pots = {
        os.path.join(ROOT, "po", GNOME_DOMAIN + ".pot"): gnome_pot,
        os.path.join(ROOT, PLASMA_DIR, "po", PLASMA_DOMAIN + ".pot"): plasma_pot,
    }
    for pot, make in pots.items():
        make(ROOT, pot)
    for lang in linguas(ROOT):
        merge(os.path.join(ROOT, "po", GNOME_DOMAIN + ".pot"), os.path.join(ROOT, "po", lang + ".po"))
        merge(
            os.path.join(ROOT, PLASMA_DIR, "po", PLASMA_DOMAIN + ".pot"),
            os.path.join(ROOT, PLASMA_DIR, "po", lang, PLASMA_DOMAIN + ".po"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

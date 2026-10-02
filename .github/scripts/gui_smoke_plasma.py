#!/usr/bin/env python3
"""Load check of the installed Plasma connection editor plugin.

    gui_smoke_plasma.py --plugin <plasmanetworkmanagement_gpclientui.so> --name-file <...name>

Run by .github/scripts/gui-smoke.sh. The plugin must have no missing library
(ldd), carry the metadata of our service and load with QPluginLoader (PyQt5 for
a qt5 plugin, PyQt6 for qt6). Without PyQt only the JSON file next to the
plugin is checked and the log says so. There is no screenshot: showing the
editor needs a C++ harness against plasma-nm. Exit status: 0 = fine, 1 = a check
failed, 2 = bad arguments. The decisions are in gui_smoke_lib.py.
"""

import argparse
import importlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gui_smoke_lib as lib  # noqa: E402


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def load_with_qt(path, major):
    """(metadata dict, None) from QPluginLoader, or (None, reason) when PyQt is missing"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        core = importlib.import_module(f"PyQt{major}.QtCore")
    except ImportError as error:
        return None, str(error)
    loader = core.QPluginLoader(path)
    meta = loader.metaData()
    if not loader.load():
        fail(f"QPluginLoader cannot load {path}: {loader.errorString()}")
    return meta, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--name-file", required=True)
    args = parser.parse_args(argv)

    try:
        with open(args.name_file, encoding="utf-8") as handle:
            service = lib.parse_name_file(handle.read())["service"]
    except (OSError, ValueError) as error:
        fail(f"{args.name_file}: {error}")
    if not os.path.isfile(args.plugin):
        fail(f"{args.plugin} does not exist")

    ldd = subprocess.run(["ldd", args.plugin], capture_output=True, text=True)
    problems = lib.ldd_problems(ldd.returncode, ldd.stdout + ldd.stderr)
    if problems:
        fail("; ".join(problems))
    print(f"OK: ldd finds every library of {args.plugin}")

    major = lib.qt_major(args.plugin)
    if major is None:
        fail(f"cannot tell Qt 5 from Qt 6 by the path {args.plugin}")
    meta, reason = load_with_qt(args.plugin, major)
    if meta is None:
        print(f"WARN: PyQt{major} is not available ({reason}): checking the JSON file only, not loading the plugin")
        json_file = os.path.splitext(args.plugin)[0] + ".json"
        try:
            with open(json_file, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError) as error:
            fail(f"{json_file}: {error}")
    problems = lib.check_plasma_metadata(meta, service)
    if problems:
        fail("; ".join(problems))
    print(f"OK: Qt{major} plugin metadata matches {service}")


if __name__ == "__main__":
    main()

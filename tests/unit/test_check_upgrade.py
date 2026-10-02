"""
Tests for .github/scripts/check_upgrade.py, which judges the result of the
package upgrade test (.github/scripts/upgrade-test.sh): the packages installed
before and after, as `dpkg-query` prints them.

Every positive test has negative counterparts: a kept-back core package, a
removed GUI plugin, a package left in the rc/iU state, a package of the former
-plasma-5 / -plasma-6 split left behind, an editor plugin in the wrong Qt directory
for the release, an old version left behind, an unknown scenario and malformed
input are all reported.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import importlib.util
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "check_upgrade.py")
spec = importlib.util.spec_from_file_location("check_upgrade", SCRIPT)
check_upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_upgrade)

OLD = "1.4.1-1~noble1"
NEW = "1.5.0-1~noble1+pr31.7"
# Ubuntu version of debian/control.ubuntu<version> -> codename
CODENAMES = {"22.04": "jammy", "24.04": "noble", "24.10": "oracular", "26.04": "resolute"}


def lines(*entries):
    """dpkg-query output; entries are (package, version, status)"""
    return "".join(f"{name} {version} {status} \n" for name, version, status in entries)


def parsed(*entries):
    return check_upgrade.parse_status(lines(*entries))


def run_check(before, after, scenario="gnome", codename="noble", expected=NEW):
    return check_upgrade.check(parsed(*before), parsed(*after), expected, scenario, codename)


GNOME_BEFORE = [("network-manager-gpclient", OLD, "ii"), ("network-manager-gpclient-gnome", OLD, "ii")]
GNOME_AFTER = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-gnome", NEW, "ii")]
PLASMA_BEFORE = [("network-manager-gpclient", OLD, "ii"), ("network-manager-gpclient-plasma", OLD, "ii")]


PLASMA_AFTER = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-plasma", NEW, "ii")]


def plugin_files(qt="qt5", multiarch="x86_64-linux-gnu"):
    """dpkg -L of the Plasma package"""
    base = f"/usr/lib/{multiarch}/{qt}/plugins/plasma/network/vpn"
    return f"/usr\n/usr/lib\n{base}/plasmanetworkmanagement_gpclientui.json\n{base}/plasmanetworkmanagement_gpclientui.so\n"


class TestParse:
    def test_reads_package_version_and_state(self):
        result = parsed(("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-x", OLD, "rc"))

        assert result == {"network-manager-gpclient": (NEW, "ii"), "network-manager-gpclient-x": (OLD, "rc")}

    def test_a_package_only_known_to_dpkg_has_no_version(self):
        result = check_upgrade.parse_status("network-manager-gpclient-plasma-6  un \n")

        assert result == {"network-manager-gpclient-plasma-6": ("", "un")}

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "\n\n",
            "network-manager-gpclient\n",
            "network-manager-gpclient 1.0 ii extra\n",
            "network-manager-gpclient 1.0 I1\n",
            "network-manager-gpclient 1.0 installed\n",
            "network-manager-gpclient 1.0 ii \nnetwork-manager-gpclient 1.0 ii \n",
        ],
    )
    def test_malformed_input_is_refused(self, text):
        with pytest.raises(ValueError):
            check_upgrade.parse_status(text)


class TestCleanUpgrades:
    def test_gnome(self):
        assert run_check(GNOME_BEFORE, GNOME_AFTER) == []

    @pytest.mark.parametrize("codename", sorted(CODENAMES.values()))
    def test_plasma_keeps_its_name_on_every_release(self, codename):
        assert run_check(PLASMA_BEFORE, PLASMA_AFTER, "plasma", codename) == []

    def test_packages_unknown_to_dpkg_are_ignored(self):
        before = GNOME_BEFORE + [("network-manager-gpclient-plasma-5", "", "un")]
        after = GNOME_AFTER + [("network-manager-gpclient-plasma-5", "", "un")]

        assert run_check(before, after) == []


class TestBrokenUpgrades:
    def test_core_kept_back(self):
        after = [("network-manager-gpclient", OLD, "ii"), ("network-manager-gpclient-gnome", NEW, "ii")]

        problems = run_check(GNOME_BEFORE, after)

        assert any("network-manager-gpclient is at version " + OLD in p and "kept back" in p for p in problems)

    def test_old_version_left_on_the_gui_package(self):
        after = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-gnome", OLD, "ii")]

        problems = run_check(GNOME_BEFORE, after)

        assert any("network-manager-gpclient-gnome is at version " + OLD in p for p in problems)

    def test_gui_plugin_removed(self):
        problems = run_check(GNOME_BEFORE, [("network-manager-gpclient", NEW, "ii")])

        assert any("network-manager-gpclient-gnome" in p and "gone" in p for p in problems)
        assert any("network-manager-gpclient-gnome is not installed" in p for p in problems)

    def test_removed_package_with_configuration_left(self):
        after = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-gnome", OLD, "rc")]

        problems = run_check(GNOME_BEFORE, after)

        assert any("state 'rc'" in p for p in problems)

    @pytest.mark.parametrize("status", ["iU", "iF", "iH", "hi", "ri"])
    def test_package_not_fully_installed(self, status):
        after = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-gnome", NEW, status)]

        problems = run_check(GNOME_BEFORE, after)

        assert any("network-manager-gpclient-gnome" in p and f"'{status[:2]}'" in p for p in problems)

    def test_new_package_half_installed(self):
        after = GNOME_AFTER + [("network-manager-gpclient-plasma", NEW, "iF")]

        problems = run_check(GNOME_BEFORE, after)

        assert any("plasma" in p and "not fully installed" in p for p in problems)

    def test_everything_gone(self):
        problems = run_check(GNOME_BEFORE, [("network-manager-gpclient", "", "un")])

        assert len(problems) >= 3

    @pytest.mark.parametrize("codename", sorted(CODENAMES.values()))
    @pytest.mark.parametrize("former", ["network-manager-gpclient-plasma-5", "network-manager-gpclient-plasma-6"])
    def test_package_of_the_former_split_left_installed(self, codename, former):
        after = PLASMA_AFTER + [(former, NEW, "ii")]

        problems = run_check(PLASMA_BEFORE, after, "plasma", codename)

        assert any(f"{former} is installed" in p for p in problems)

    def test_package_of_the_former_split_that_dpkg_only_knows_is_fine(self):
        after = PLASMA_AFTER + [("network-manager-gpclient-plasma-6", "", "un")]

        assert run_check(PLASMA_BEFORE, after, "plasma", "resolute") == []

    @pytest.mark.parametrize("name", ["network-manager-gpclient-plasma-7", "network-manager-gpclient-plasma-x"])
    def test_only_the_former_split_packages_are_reported(self, name):
        assert run_check(PLASMA_BEFORE, PLASMA_AFTER + [(name, NEW, "ii")], "plasma") == []

    def test_plasma_scenario_without_the_plasma_package(self):
        after = [("network-manager-gpclient", NEW, "ii")]

        problems = run_check(PLASMA_BEFORE, after, "plasma")

        assert any("network-manager-gpclient-plasma is not installed" in p for p in problems)

    def test_gnome_scenario_without_gnome_package(self):
        before = [("network-manager-gpclient", OLD, "ii")]
        after = [("network-manager-gpclient", NEW, "ii")]

        problems = run_check(before, after, "gnome")

        assert any("network-manager-gpclient-gnome is not installed" in p for p in problems)

    def test_all_problems_are_listed(self):
        after = [("network-manager-gpclient", OLD, "ii"), ("network-manager-gpclient-gnome", OLD, "rc")]

        assert len(run_check(GNOME_BEFORE, after)) >= 3

    @pytest.mark.parametrize("scenario", ["", "gnom", "kde", "GNOME"])
    def test_unknown_scenario(self, scenario):
        with pytest.raises(ValueError, match="unknown scenario"):
            run_check(GNOME_BEFORE, GNOME_AFTER, scenario)

    @pytest.mark.parametrize("codename", ["", "focal", "Noble"])
    def test_unknown_codename_for_plasma(self, codename):
        with pytest.raises(ValueError, match="codename"):
            run_check(PLASMA_BEFORE, PLASMA_AFTER, "plasma", codename)

    def test_empty_expected_version(self):
        with pytest.raises(ValueError, match="expected version"):
            run_check(GNOME_BEFORE, GNOME_AFTER, expected="")


class TestPluginDirectory:
    @pytest.mark.parametrize("multiarch", ["x86_64-linux-gnu", "aarch64-linux-gnu"])
    @pytest.mark.parametrize("codename, qt", sorted(check_upgrade.PLASMA_QT.items()))
    def test_the_qt_directory_of_the_release_is_accepted(self, codename, qt, multiarch):
        assert check_upgrade.check_plugin(plugin_files(qt, multiarch), codename) == []

    @pytest.mark.parametrize("codename, wrong", [("jammy", "qt6"), ("noble", "qt6"), ("oracular", "qt5"), ("resolute", "qt5")])
    def test_the_other_qt_is_reported(self, codename, wrong):
        problems = check_upgrade.check_plugin(plugin_files(wrong), codename)

        assert len(problems) == 1 and f"is a {wrong} plugin" in problems[0]

    @pytest.mark.parametrize("files", ["", "/usr\n/usr/share/doc\n", "/usr/lib/x/qt5/plugins/plasma/network/vpn/other.so\n"])
    def test_a_package_without_the_plugin_is_reported(self, files):
        assert check_upgrade.check_plugin(files, "noble") == ["network-manager-gpclient-plasma lists no plasmanetworkmanagement_gpclientui.so"]

    @pytest.mark.parametrize("path", [
        "/opt/qt5/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.so",
        "/usr/lib/x86_64-linux-gnu/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.so",
        "/usr/lib/x86_64-linux-gnu/qt5/plugins/plasma/plasmanetworkmanagement_gpclientui.so",
        "/usr/lib/x86_64-linux-gnu/qt4/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.so",
    ])
    def test_a_plugin_outside_the_plugin_directory_is_reported(self, path):
        problems = check_upgrade.check_plugin(path + "\n", "noble")

        assert len(problems) == 1 and "is not in /usr/lib/<multiarch>/<qt>/plugins/plasma/network/vpn/" in problems[0]

    def test_one_plugin_in_each_qt_directory_reports_the_wrong_one(self):
        files = plugin_files("qt5") + plugin_files("qt6")

        assert len(check_upgrade.check_plugin(files, "noble")) == 1
        assert len(check_upgrade.check_plugin(files, "resolute")) == 1

    @pytest.mark.parametrize("codename", ["", "focal", "Noble"])
    def test_unknown_codename(self, codename):
        with pytest.raises(ValueError, match="codename"):
            check_upgrade.check_plugin(plugin_files(), codename)


class TestQtMapping:
    """PLASMA_QT must follow debian/control.ubuntu*: qtbase5-dev (Qt5) or
    qt6-base-dev (Qt6) in Build-Depends, never both"""

    @staticmethod
    def qt_of_control(version):
        path = os.path.join(ROOT, "debian", f"control.ubuntu{version}")
        with open(path, encoding="utf-8") as handle:
            build_depends = handle.read().split("\nStandards-Version:")[0]
        found = {qt for qt, package in (("qt5", "qtbase5-dev"), ("qt6", "qt6-base-dev"))
                 if re.search(rf"^\s+{package},", build_depends, re.M)}
        assert len(found) == 1, f"control.ubuntu{version} builds with {sorted(found) or 'no Qt'}"
        return found.pop()

    @pytest.mark.parametrize("version, codename", sorted(CODENAMES.items()))
    def test_matches_the_control_file(self, version, codename):
        assert check_upgrade.PLASMA_QT[codename] == self.qt_of_control(version)

    def test_covers_every_control_file(self):
        controls = {f[len("control.ubuntu"):] for f in os.listdir(os.path.join(ROOT, "debian")) if f.startswith("control.ubuntu")}

        assert controls == set(CODENAMES)
        assert set(check_upgrade.PLASMA_QT) == set(CODENAMES.values())

    def test_the_cross_check_notices_a_wrong_mapping(self, monkeypatch):
        monkeypatch.setitem(check_upgrade.PLASMA_QT, "noble", "qt6")

        assert check_upgrade.PLASMA_QT["noble"] != self.qt_of_control("24.04")


class TestCommandLine:
    def run(self, tmp_path, before, after, scenario="gnome", codename="noble", expected=NEW, files=None):
        (tmp_path / "before.txt").write_text(before)
        (tmp_path / "after.txt").write_text(after)
        extra = []
        if files is not None:
            (tmp_path / "files.txt").write_text(files)
            extra = ["--plasma-files", str(tmp_path / "files.txt")]
        return subprocess.run(
            [
                sys.executable, SCRIPT,
                "--before", str(tmp_path / "before.txt"), "--after", str(tmp_path / "after.txt"),
                "--expected-version", expected, "--scenario", scenario, "--codename", codename, *extra,
            ],
            capture_output=True, text=True, timeout=30,
        )

    @pytest.mark.parametrize("codename, qt", [("noble", "qt5"), ("resolute", "qt6")])
    def test_the_plugin_in_the_qt_directory_of_the_release_exits_zero(self, tmp_path, codename, qt):
        result = self.run(tmp_path, lines(*PLASMA_BEFORE), lines(*PLASMA_AFTER), "plasma", codename, files=plugin_files(qt))

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("codename, qt", [("noble", "qt6"), ("resolute", "qt5")])
    def test_the_plugin_in_the_wrong_qt_directory_exits_one(self, tmp_path, codename, qt):
        result = self.run(tmp_path, lines(*PLASMA_BEFORE), lines(*PLASMA_AFTER), "plasma", codename, files=plugin_files(qt))

        assert result.returncode == 1
        assert f"is a {qt} plugin" in result.stderr

    def test_plasma_files_for_the_gnome_scenario_exits_two(self, tmp_path):
        result = self.run(tmp_path, lines(*GNOME_BEFORE), lines(*GNOME_AFTER), "gnome", files=plugin_files())

        assert result.returncode == 2
        assert "--plasma-files needs the plasma scenario" in result.stderr

    def test_a_missing_plasma_files_exits_two(self, tmp_path):
        (tmp_path / "before.txt").write_text(lines(*PLASMA_BEFORE))
        (tmp_path / "after.txt").write_text(lines(*PLASMA_AFTER))
        result = subprocess.run(
            [sys.executable, SCRIPT, "--before", str(tmp_path / "before.txt"), "--after", str(tmp_path / "after.txt"),
             "--expected-version", NEW, "--scenario", "plasma", "--codename", "noble",
             "--plasma-files", str(tmp_path / "none.txt")],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 2

    def test_clean_upgrade_exits_zero(self, tmp_path):
        result = self.run(tmp_path, lines(*GNOME_BEFORE), lines(*GNOME_AFTER))

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("OK:")

    def test_broken_upgrade_exits_one_and_lists_the_problems(self, tmp_path):
        after = [("network-manager-gpclient", OLD, "ii"), ("network-manager-gpclient-gnome", NEW, "ii")]

        result = self.run(tmp_path, lines(*GNOME_BEFORE), lines(*after))

        assert result.returncode == 1
        assert "UPGRADE CHECK FAILED" in result.stderr
        assert "kept back" in result.stderr

    @pytest.mark.parametrize(
        "before, after, scenario",
        [
            ("", lines(*GNOME_AFTER), "gnome"),
            (lines(*GNOME_BEFORE), "garbage\n", "gnome"),
            (lines(*GNOME_BEFORE), lines(*GNOME_AFTER), "nope"),
        ],
    )
    def test_bad_input_exits_two(self, tmp_path, before, after, scenario):
        result = self.run(tmp_path, before, after, scenario)

        assert result.returncode == 2
        assert result.stderr.startswith("ERROR:")

    def test_missing_file_exits_two(self, tmp_path):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--before", str(tmp_path / "no"), "--after", str(tmp_path / "no"),
             "--expected-version", NEW, "--scenario", "gnome", "--codename", "noble"],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 2

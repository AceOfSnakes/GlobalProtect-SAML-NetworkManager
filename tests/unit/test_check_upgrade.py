"""
Tests for .github/scripts/check_upgrade.py, which judges the result of the
package upgrade test (.github/scripts/upgrade-test.sh): the packages installed
before and after, as `dpkg-query` prints them.

Every positive test has negative counterparts: a kept-back core package, a
removed GUI plugin, a package left in the rc/iU state, the wrong Plasma package
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


def plasma_after(package="network-manager-gpclient-plasma-5", transitional=True):
    after = [("network-manager-gpclient", NEW, "ii"), (package, NEW, "ii")]
    if transitional:
        after.append(("network-manager-gpclient-plasma", NEW, "ii"))
    return after


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

    @pytest.mark.parametrize(
        "codename, package",
        [
            ("jammy", "network-manager-gpclient-plasma-5"),
            ("noble", "network-manager-gpclient-plasma-5"),
            ("oracular", "network-manager-gpclient-plasma-6"),
            ("resolute", "network-manager-gpclient-plasma-6"),
        ],
    )
    @pytest.mark.parametrize("scenario", ["plasma", "plasma-new"])
    def test_plasma_gets_the_package_of_the_release(self, scenario, codename, package):
        before = PLASMA_BEFORE if scenario == "plasma" else [("network-manager-gpclient", OLD, "ii"), (package, OLD, "ii")]
        after = plasma_after(package, transitional=scenario == "plasma")

        assert run_check(before, after, scenario, codename) == []

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
        after = GNOME_AFTER + [("network-manager-gpclient-plasma-5", NEW, "iF")]

        problems = run_check(GNOME_BEFORE, after)

        assert any("plasma-5" in p and "not fully installed" in p for p in problems)

    def test_everything_gone(self):
        problems = run_check(GNOME_BEFORE, [("network-manager-gpclient", "", "un")])

        assert len(problems) >= 3

    @pytest.mark.parametrize(
        "codename, wrong",
        [
            ("noble", "network-manager-gpclient-plasma-6"),
            ("jammy", "network-manager-gpclient-plasma-6"),
            ("oracular", "network-manager-gpclient-plasma-5"),
            ("resolute", "network-manager-gpclient-plasma-5"),
        ],
    )
    def test_wrong_plasma_package_for_the_release(self, codename, wrong):
        problems = run_check(PLASMA_BEFORE, plasma_after(wrong), "plasma", codename)

        assert any("is not installed after" in p for p in problems)
        assert any(f"{wrong} is installed" in p for p in problems)

    def test_transitional_package_without_the_real_one(self):
        after = [("network-manager-gpclient", NEW, "ii"), ("network-manager-gpclient-plasma", NEW, "ii")]

        problems = run_check(PLASMA_BEFORE, after, "plasma")

        assert any("network-manager-gpclient-plasma-5 is not installed" in p for p in problems)

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
            run_check(PLASMA_BEFORE, plasma_after(), "plasma", codename)

    def test_empty_expected_version(self):
        with pytest.raises(ValueError, match="expected version"):
            run_check(GNOME_BEFORE, GNOME_AFTER, expected="")


class TestPlasmaPackageMapping:
    """PLASMA_PACKAGE must follow debian/control.ubuntu*: the first package that
    the transitional network-manager-gpclient-plasma depends on"""

    @staticmethod
    def transitional_target(version):
        path = os.path.join(ROOT, "debian", f"control.ubuntu{version}")
        with open(path, encoding="utf-8") as handle:
            paragraphs = handle.read().split("\n\n")
        paragraph = next(p for p in paragraphs if re.search(r"^Package: network-manager-gpclient-plasma$", p, re.M))
        depends = re.search(r"^Depends:(.*?)(?=^\S)", paragraph + "\nEnd:", re.M | re.S).group(1)
        names = re.findall(r"network-manager-gpclient-plasma-[56]", depends)
        return names[0]

    @pytest.mark.parametrize("version, codename", sorted(CODENAMES.items()))
    def test_matches_the_control_file(self, version, codename):
        assert check_upgrade.PLASMA_PACKAGE[codename] == self.transitional_target(version)

    def test_covers_every_control_file(self):
        controls = {f[len("control.ubuntu"):] for f in os.listdir(os.path.join(ROOT, "debian")) if f.startswith("control.ubuntu")}

        assert controls == set(CODENAMES)
        assert set(check_upgrade.PLASMA_PACKAGE) == set(CODENAMES.values())

    def test_the_cross_check_notices_a_wrong_mapping(self, monkeypatch):
        monkeypatch.setitem(check_upgrade.PLASMA_PACKAGE, "noble", "network-manager-gpclient-plasma-6")

        assert check_upgrade.PLASMA_PACKAGE["noble"] != self.transitional_target("24.04")


class TestCommandLine:
    def run(self, tmp_path, before, after, scenario="gnome", codename="noble", expected=NEW):
        (tmp_path / "before.txt").write_text(before)
        (tmp_path / "after.txt").write_text(after)
        return subprocess.run(
            [
                sys.executable, SCRIPT,
                "--before", str(tmp_path / "before.txt"), "--after", str(tmp_path / "after.txt"),
                "--expected-version", expected, "--scenario", scenario, "--codename", codename,
            ],
            capture_output=True, text=True, timeout=30,
        )

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

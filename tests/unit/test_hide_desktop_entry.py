"""
Tests for scripts/hide-desktop-entry.sh and its use in debian/rules and
rpm/network-manager-gpclient.spec.

The upstream gpgui.desktop shows a "GP Connect" menu entry, but gpgui is not shipped. The
file is still needed: it registers x-scheme-handler/globalprotectcallback. Both packages
install it with NoDisplay=true added to the [Desktop Entry] group.
Every positive test has a negative counterpart: the unmodified upstream file and a
NoDisplay key in another group are not accepted.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "scripts", "hide-desktop-entry.sh")

# external/GlobalProtect-openconnect/packaging/files/usr/share/applications/gpgui.desktop
UPSTREAM = """\
[Desktop Entry]

Type=Application
Name=GP Connect
Comment=A GUI client for GlobalProtect VPN
GenericName=GlobalProtect VPN Client
Categories=Network;Dialup;
Exec=/usr/bin/gpclient launch-gui %u
MimeType=x-scheme-handler/globalprotectcallback;
Icon=gpgui
Keywords=GlobalProtect;Openconnect;SAML;connection;VPN;
"""


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def hides_entry(text):
    """True when the [Desktop Entry] group has NoDisplay=true (the check of the Desktop Entry spec)"""
    group = None
    for line in text.splitlines():
        if line.startswith("["):
            group = line
        elif group == "[Desktop Entry]" and re.fullmatch(r"NoDisplay\s*=\s*true", line):
            return True
    return False


def hide(tmp_path, text):
    path = tmp_path / "gpgui.desktop"
    path.write_text(text, encoding="utf-8")
    result = subprocess.run(["sh", SCRIPT, str(path)], capture_output=True, text=True, timeout=30)
    return result, path.read_text(encoding="utf-8")


# --- the check itself ---


def test_the_upstream_file_is_not_hidden():
    assert not hides_entry(UPSTREAM)


def test_a_no_display_key_in_another_group_does_not_count():
    assert not hides_entry(UPSTREAM + "\n[Desktop Action Other]\nNoDisplay=true\n")


def test_no_display_false_does_not_count():
    assert not hides_entry(UPSTREAM.replace("[Desktop Entry]\n", "[Desktop Entry]\nNoDisplay=false\n"))


# --- the script ---


def test_the_script_hides_the_entry_and_keeps_the_rest(tmp_path):
    result, text = hide(tmp_path, UPSTREAM)

    assert result.returncode == 0, result.stderr
    assert hides_entry(text)
    assert text == UPSTREAM.replace("[Desktop Entry]\n", "[Desktop Entry]\nNoDisplay=true\n", 1)
    assert "MimeType=x-scheme-handler/globalprotectcallback;\n" in text
    assert "Exec=/usr/bin/gpclient launch-gui %u\n" in text


def test_the_second_run_changes_nothing(tmp_path):
    _, once = hide(tmp_path, UPSTREAM)
    result, twice = hide(tmp_path, once)

    assert result.returncode == 0, result.stderr
    assert twice == once
    assert twice.count("NoDisplay") == 1


def test_a_no_display_key_in_the_group_is_not_added_again(tmp_path):
    text = UPSTREAM.replace("[Desktop Entry]\n", "[Desktop Entry]\nNoDisplay=false\n")
    result, after = hide(tmp_path, text)

    assert result.returncode == 0, result.stderr
    assert after == text


def test_a_no_display_key_in_another_group_does_not_stop_the_script(tmp_path):
    text = UPSTREAM + "\n[Desktop Action Other]\nNoDisplay=true\n"
    result, after = hide(tmp_path, text)

    assert result.returncode == 0, result.stderr
    assert hides_entry(after)
    assert after.count("NoDisplay") == 2


@pytest.mark.parametrize("text", ["", "Type=Application\n", "[Other]\nType=Application\n"])
def test_a_file_without_the_group_is_an_error(tmp_path, text):
    result, after = hide(tmp_path, text)

    assert result.returncode != 0
    assert "no [Desktop Entry] group" in result.stderr
    assert not hides_entry(after)


@pytest.mark.parametrize("args", [[], ["a", "b"], ["/nonexistent/file"]])
def test_bad_arguments_are_an_error(args):
    result = subprocess.run(["sh", SCRIPT] + args, capture_output=True, text=True, timeout=30)

    assert result.returncode == 2
    assert "usage" in result.stderr


# --- the packages ---


def test_debian_rules_hides_the_entry_after_the_install():
    rules = read("debian/rules")
    install = rules.index("packaging/files/usr/share/applications/gpgui.desktop")
    call = rules.index("scripts/hide-desktop-entry.sh")

    assert install < call
    line = rules[call:].splitlines()[0]
    assert line.endswith("/debian/network-manager-gpclient/usr/share/applications/gpgui.desktop")


def test_spec_hides_the_entry_after_the_install():
    spec = read("rpm/network-manager-gpclient.spec")
    install = spec.index("packaging/files/usr/share/applications/gpgui.desktop")
    call = spec.index("scripts/hide-desktop-entry.sh")

    assert install < call
    line = spec[call:].splitlines()[0]
    assert line.endswith("%{buildroot}%{_datadir}/applications/gpgui.desktop")


@pytest.mark.parametrize("path", ["debian/rules", "rpm/network-manager-gpclient.spec"])
def test_the_packages_still_install_the_upstream_file_unchanged_first(path):
    text = read(path)

    assert text.count("packaging/files/usr/share/applications/gpgui.desktop") == 1
    assert text.count("scripts/hide-desktop-entry.sh") == 1


def test_install_test_checks_the_hidden_entry_and_the_handler():
    script = read("rpm/install-test.sh")

    assert "grep -qx 'NoDisplay=true' /usr/share/applications/gpgui.desktop" in script
    assert "MimeType=x-scheme-handler/globalprotectcallback;" in script


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)

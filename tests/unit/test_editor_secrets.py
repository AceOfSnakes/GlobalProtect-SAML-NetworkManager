"""Issue #53: the editors have a password field with secret flags.

The editors need libnma, GTK and Qt to build, so these are text checks of the
sources and the docs. The behaviour is tested in plugins/plasma/tests
(passwordStorage) and, for the GTK editors, by hand and in CI builds.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GTK = ["plugins/gnome/nm-gpclient-editor-gtk3.c", "plugins/gnome/nm-gpclient-editor.c"]
PLASMA = "plugins/plasma/gpclientwidget.cpp"


def read(rel):
    return (ROOT / rel).read_text()


def read_gtk(rel):
    # The flags rules live in the header the two GTK editors share
    return read(rel) + read("plugins/gnome/nm-gpclient-editor-common.h")


@pytest.mark.parametrize("rel", GTK)
def test_gtk_editor_uses_the_password_storage_menu(rel):
    src = read_gtk(rel)

    assert "nma_utils_setup_password_storage" in src
    assert "nma_utils_menu_to_secret_flags" in src
    assert "nm_setting_set_secret_flags" in src
    assert "NM_SETTING_SECRET_FLAG_AGENT_OWNED" in src  # default of a new connection
    assert '"otp-flags", "2"' in src


@pytest.mark.parametrize("rel", GTK)
def test_gtk_storage_menu_gets_no_setting(rel):
    # With a setting, libnma reads the flags from it and ignores the initial
    # ones: a new connection (no password-flags yet) would start at 0, stored
    # for all users in the profile, instead of 1
    calls = re.findall(r"nma_utils_setup_password_storage\s*\(([^;]*)\);", read(rel))

    assert calls
    for call in calls:
        args = [a.strip() for a in call.split(",")]
        assert args[2:4] == ["NULL", "NULL"], call


GTK_FLAGS_RULE = re.compile(
    r"if \(\(is_new && flags != NM_SETTING_SECRET_FLAG_NONE\) \|\| flags != menu_flags\)\s*"
    r"nm_setting_set_secret_flags"
)


def gtk_flags_written_only_when_new_or_changed(src):
    return bool(GTK_FLAGS_RULE.search(src)) and src.count("nm_setting_set_secret_flags") == 1


@pytest.mark.parametrize("rel", GTK)
def test_gtk_editor_writes_the_flags_only_when_new_or_changed(rel):
    # An existing profile keeps flags the menu cannot show (4, not required)
    assert gtk_flags_written_only_when_new_or_changed(read_gtk(rel))


@pytest.mark.parametrize(
    "bad",
    [
        '    nm_setting_set_secret_flags (NM_SETTING (s_vpn), "password", flags, NULL);',
        '    if (flags != NM_SETTING_SECRET_FLAG_NONE || nm_setting_vpn_get_data_item (s_vpn, "password-flags"))\n'
        '        nm_setting_set_secret_flags (NM_SETTING (s_vpn), "password", flags, NULL);',
    ],
)
def test_an_unconditional_or_old_flags_write_is_rejected(bad):
    assert not gtk_flags_written_only_when_new_or_changed(bad)


@pytest.mark.parametrize("rel", GTK + [PLASMA])
def test_every_editor_defaults_a_new_connection_only(rel):
    # The defaults are guarded by "new": an existing connection keeps its flags
    src = read_gtk(rel) if rel in GTK else read(rel)

    assert re.search(r"(\bis_new|m_isNew)\s*&&.*otp-flags|otp-flags.*(\bis_new|m_isNew)", src)


def test_plasma_editor_has_the_password_widgets():
    src = read(PLASMA)

    assert "password-flags" in src and "otp-flags" in src
    assert "setSecrets" in src and "loadSecrets" in src
    ui = read("plugins/plasma/gpclientwidget.ui")
    assert 'name="passwordLineEdit"' in ui and 'name="passwordFlagsComboBox"' in ui


@pytest.mark.parametrize("rel", ["README.md", "docs/INTERACTIVE_AUTH.md"])
def test_docs_do_not_put_the_password_on_the_command_line(rel):
    text = read(rel)

    assert "+vpn.secrets password=" not in text
    assert "password-flags" in text

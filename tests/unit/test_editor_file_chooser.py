"""Issues #141, #144: the client certificate, private key and HIP script fields of the editors have
a "choose a file" icon (typing the path keeps working).

The editors need GTK and Qt to build, so these are text checks of the sources
(as in test_editor_secrets.py); the CI builds compile them.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GTK = ["plugins/gnome/nm-gpclient-editor-gtk3.c", "plugins/gnome/nm-gpclient-editor.c"]
PLASMA = "plugins/plasma/gpclientwidget.cpp"
FIELDS = ("usercert", "userkey", "hip_script")
PLASMA_EDITS = {"usercert": "userCertLineEdit", "userkey": "userKeyLineEdit", "hip_script": "hipScriptLineEdit"}


def read(rel):
    return (ROOT / rel).read_text()


def gtk_fields_with_icon(src):
    return {f for f in FIELDS if re.search(rf"add_choose_file_icon \((?:self->)?{f}_entry,", src)}


def plasma_fields_with_action(src):
    return {
        f
        for f, edit in PLASMA_EDITS.items()
        if re.search(rf"addChooseFileAction\(m_ui->{edit},", src)
    }


@pytest.mark.parametrize("rel", GTK)
def test_gtk_editor_has_the_icon_on_all_fields(rel):
    src = read(rel)

    assert gtk_fields_with_icon(src) == set(FIELDS)
    assert '"document-open"' in src
    assert "GTK_ENTRY_ICON_SECONDARY" in src
    assert '"icon-press"' in src
    assert "gtk_file_chooser_native_new" in src
    assert "GTK_FILE_CHOOSER_ACTION_OPEN" in src


def guarded_set_text(src):
    """The entry is set only after Accept, while the editor exists, for a local file"""
    return bool(re.search(
        r"if \(entry && response == GTK_RESPONSE_ACCEPT\) \{\n"
        r"[^}]*g_file_get_path[^}]*\n\s*if \(path\)\n\s*gtk_(?:editable|entry)_set_text \(", src))


@pytest.mark.parametrize("rel", GTK)
def test_gtk_editor_sets_the_entry_only_for_a_local_file_and_an_accept(rel):
    assert guarded_set_text(read(rel))


@pytest.mark.parametrize("rel", GTK)
@pytest.mark.parametrize("old, new", [
    ("entry && response == GTK_RESPONSE_ACCEPT", "entry"),  # also on Cancel
    ("entry && response == GTK_RESPONSE_ACCEPT", "response == GTK_RESPONSE_ACCEPT"),  # editor gone
    ("        if (path)\n", ""),  # a non-local file
])
def test_gtk_editor_without_a_guard_is_found(rel, old, new):
    src = read(rel)
    assert old in src

    assert not guarded_set_text(src.replace(old, new))


def modal_chooser(src):
    """The native chooser is made modal right after it is created (as Plasma's)"""
    return bool(re.search(
        r"gtk_file_chooser_native_new \([^;]*;\n(?:\s*/\*[^\n]*\*/\n)?"
        r"\s*gtk_native_dialog_set_modal \(GTK_NATIVE_DIALOG \(dialog\), TRUE\);", src))


@pytest.mark.parametrize("rel", GTK)
def test_gtk_chooser_is_modal(rel):
    assert modal_chooser(read(rel))


@pytest.mark.parametrize("rel", GTK)
def test_gtk_chooser_that_is_not_modal_is_found(rel):
    src = re.sub(r"\s*gtk_native_dialog_set_modal \([^\n]*\n", "\n", read(rel))

    assert not modal_chooser(src)


@pytest.mark.parametrize("rel", GTK)
@pytest.mark.parametrize("field", FIELDS)
def test_gtk_a_field_without_the_icon_is_found(rel, field):
    src = re.sub(rf"    add_choose_file_icon \((?:self->)?{field}_entry,[^\n]*\n", "", read(rel))

    assert gtk_fields_with_icon(src) == set(FIELDS) - {field}


def test_gtk4_wraps_the_calls_deprecated_since_4_10():
    src = read("plugins/gnome/nm-gpclient-editor.c")

    assert src.index("G_GNUC_BEGIN_IGNORE_DEPRECATIONS") < src.index("gtk_file_chooser_native_new")
    assert src.index("gtk_file_chooser_native_new") < src.index("G_GNUC_END_IGNORE_DEPRECATIONS")


def test_plasma_editor_has_the_action_on_all_fields():
    src = read(PLASMA)

    assert plasma_fields_with_action(src) == set(FIELDS)
    assert 'QIcon::fromTheme(QStringLiteral("document-open"), ' in src
    assert "QLineEdit::TrailingPosition" in src
    assert "QFileDialog::getOpenFileName" in src


@pytest.mark.parametrize("field", FIELDS)
def test_plasma_a_field_without_the_action_is_found(field):
    edit = PLASMA_EDITS[field]
    src = re.sub(rf"    addChooseFileAction\(m_ui->{edit},[^\n]*\n", "", read(PLASMA))

    assert plasma_fields_with_action(src) == set(FIELDS) - {field}

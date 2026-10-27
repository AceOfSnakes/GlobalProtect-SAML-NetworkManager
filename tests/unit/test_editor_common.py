"""Issue #53, item 4.1: the GTK3 and GTK4 editors share one header.

The toolkit-independent logic (keys, defaults, what is saved and removed) is in
plugins/gnome/nm-gpclient-editor-common.h. The editors need GTK and libnma to
build, so these are text checks of the sources: both editors include the header,
call its helpers and do not define the moved code again.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HEADER = "plugins/gnome/nm-gpclient-editor-common.h"
EDITORS = ["plugins/gnome/nm-gpclient-editor.c", "plugins/gnome/nm-gpclient-editor-gtk3.c"]

HELPERS = [
    "nm_gpclient_editor_is_new",
    "nm_gpclient_editor_set_or_remove",
    "nm_gpclient_editor_data_is_true",
    "nm_gpclient_editor_hip_enabled",
    "nm_gpclient_editor_auth_index",
    "nm_gpclient_editor_fix_openssl_index",
    "nm_gpclient_editor_timeout_initial",
    "nm_gpclient_editor_parse_timeout",
    "nm_gpclient_editor_password_flags_initial",
    "nm_gpclient_editor_save_password",
    "nm_gpclient_editor_default_otp_flags",
    "nm_gpclient_editor_save_mtu",
    "nm_gpclient_editor_save_fix_openssl",
    "nm_gpclient_editor_saml_auth_index",
    "nm_gpclient_editor_save_saml_auth",
    "nm_gpclient_editor_save_browser",
]

# Text that is in the header and must not come back into an editor
MOVED = [
    r"#define\s+GATEWAY_AUTO_LABEL",
    r"#define\s+SAML_TIMEOUT_DEFAULT",
    r"browser_values\s*\[\s*\]\s*=",
    r"nm_setting_set_secret_flags",
    r"nm_setting_vpn_get_num_data_items",
    r"nm_setting_vpn_add_secret",
    r"g_ascii_strtod",
    r'"otp-flags"',
    r'"fix-openssl",\s*"true"',
    r"g_strdup_printf\s*\(\s*\"%d\"",
    r'nm_setting_vpn_add_data_item\s*\(\s*s_vpn,\s*"browser"',
]


def read(rel):
    return (ROOT / rel).read_text()


def header_problems(src):
    """What is wrong with the header: a helper or a moved definition is missing."""
    problems = [f"missing helper: {h}" for h in HELPERS
                if not re.search(rf"^{h}\s*\(", src, re.M)]
    problems += [f"missing definition: {m}" for m in MOVED[:3] if not re.search(m, src)]
    return problems


def editor_problems(src):
    """What is wrong with an editor: no include, a helper not used, moved code back."""
    problems = []
    if '#include "nm-gpclient-editor-common.h"' not in src:
        problems.append("no include")
    problems += [f"does not call: {h}" for h in HELPERS if h not in src]
    problems += [f"defines again: {m}" for m in MOVED if re.search(m, src)]
    return problems


def test_the_header_has_every_helper():
    assert header_problems(read(HEADER)) == []


def test_the_header_is_free_of_gtk():
    # Both toolkits include it, so it may use GLib and libnm only
    src = read(HEADER)

    assert "#include <gtk" not in src
    assert not re.search(r"\b(gtk_|Gtk|GTK_|nma_)", src)


@pytest.mark.parametrize("rel", EDITORS)
def test_the_editor_uses_the_header_and_has_no_own_copy(rel):
    assert editor_problems(read(rel)) == []


def test_the_helpers_in_the_editors_are_the_same_in_both():
    # GTK3 keeps its NULL checks, so compare only what the helpers are called with
    for helper in HELPERS:
        assert (helper in read(EDITORS[0])) == (helper in read(EDITORS[1])), helper


@pytest.mark.parametrize("helper", HELPERS)
def test_a_header_without_a_helper_is_rejected(helper):
    broken = re.sub(rf"^{helper}\s*\(", f"{helper}_gone (", read(HEADER), flags=re.M)

    assert f"missing helper: {helper}" in header_problems(broken)


@pytest.mark.parametrize("rel", EDITORS)
def test_an_editor_without_the_include_is_rejected(rel):
    broken = read(rel).replace('#include "nm-gpclient-editor-common.h"', "")

    assert "no include" in editor_problems(broken)


@pytest.mark.parametrize("rel", EDITORS)
@pytest.mark.parametrize("helper", HELPERS)
def test_an_editor_that_stops_calling_a_helper_is_rejected(rel, helper):
    broken = read(rel).replace(helper, "unrelated_call")

    assert f"does not call: {helper}" in editor_problems(broken)


@pytest.mark.parametrize("rel", EDITORS)
@pytest.mark.parametrize(
    "extra",
    [
        "#define SAML_TIMEOUT_DEFAULT 360",
        '#define GATEWAY_AUTO_LABEL _("First proposed by portal (automatic)")',
        'static const char *browser_values[] = { "edge", NULL };',
        'nm_setting_vpn_add_data_item (s_vpn, "otp-flags", "2");',
        'nm_setting_set_secret_flags (NM_SETTING (s_vpn), "password", flags, NULL);',
        'nm_setting_vpn_add_data_item (s_vpn, "browser", str);',
    ],
)
def test_an_editor_with_its_own_copy_is_rejected(rel, extra):
    broken = read(rel) + "\n" + extra + "\n"

    assert any(p.startswith("defines again") for p in editor_problems(broken))


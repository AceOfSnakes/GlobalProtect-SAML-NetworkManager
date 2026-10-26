"""
Tests of the import and export of a profile (issue #53): plugins/gnome/nm-gpclient-keyfile.h,
the libnm plugin that uses it and the Plasma plugin.

libnm is not available to the unit tests, so the C sources are checked as text:
each rule is a pattern that the function body must contain. Every rule has a
negative test: the real checker runs on a broken copy of the real source (the
line of the rule removed or replaced by a wrong one) and must find it.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def body(text, name):
    """The text of the function `name` (from its name at the start of a line to the closing brace)"""
    match = re.search(rf"^{re.escape(name)} \(.*?^}}", text, re.M | re.S)
    assert match, f"no function {name}"
    return match.group(0)


# function -> {rule: pattern that the body contains}
HEADER = "plugins/gnome/nm-gpclient-keyfile.h"
RULES = {
    "nm_gpclient_keyfile_import": {
        "reads the keyfile with libnm": r"nm_keyfile_read \(",
        "checks the service type": r"g_strcmp0 \(nm_setting_vpn_get_service_type \(s_vpn\), NM_GPCLIENT_KEYFILE_SERVICE_TYPE\) != 0",
        "refuses a file without connection or VPN settings": r"!s_con \|\| !s_vpn",
        "refuses a connection that is not a VPN": r"!nm_connection_is_type \(connection, NM_SETTING_VPN_SETTING_NAME\)",
        "makes the base directory absolute for libnm": r"base_dir = g_canonicalize_filename \(dir, NULL\);.*nm_keyfile_read \(keyfile, base_dir,",
        "never returns NULL without an error": r"if \(error && !\*error\)\s*g_set_error_literal",
        "checks the fields with the rules of the editors": r"nm_gpclient_validate_form \(",
        "translates the message of the rule": r"_\(problem\)",
        "gives a new UUID": r"nm_utils_uuid_generate \(\)[^}]*NM_SETTING_CONNECTION_UUID",
        "drops the secrets": r"nm_connection_clear_secrets \(connection\)",
        "drops the permissions and secondaries of the source machine": r"g_object_set \(s_con, NM_SETTING_CONNECTION_PERMISSIONS, NULL, NM_SETTING_CONNECTION_SECONDARIES, NULL, NULL\)",
        "normalizes the connection": r"nm_connection_normalize \(connection",
    },
    "nm_gpclient_keyfile_export": {
        "works on a copy": r"nm_simple_connection_new_clone \(connection\)",
        "drops the secrets of the copy": r"nm_connection_clear_secrets \(copy\)",
        "drops the permissions and secondaries of the copy": r"g_object_set \(s_con, NM_SETTING_CONNECTION_PERMISSIONS, NULL, NM_SETTING_CONNECTION_SECONDARIES, NULL, NULL\)",
        "writes the keyfile with libnm": r"nm_keyfile_write \(copy,",
        "writes the file with mode 0600": r"g_file_set_contents_full \(.*0600",
    },
    "nm_gpclient_keyfile_suggested_filename": {
        "replaces the slash": r"\*p == '/'",
        "replaces control characters": r"nm_gpclient_validate_is_control \(\*p\)",
        "adds the extension": r"NM_GPCLIENT_KEYFILE_EXTENSION",
    },
}
# (function, call that must come first, call that must come after it)
ORDER = [
    ("nm_gpclient_keyfile_export", "nm_connection_clear_secrets (copy)", "nm_keyfile_write (copy"),
    ("nm_gpclient_keyfile_export", "NM_SETTING_CONNECTION_PERMISSIONS", "nm_keyfile_write (copy"),
    ("nm_gpclient_keyfile_import", "nm_gpclient_validate_form (", "nm_utils_uuid_generate ()"),
    ("nm_gpclient_keyfile_import", "nm_connection_clear_secrets (connection)", "nm_connection_normalize (connection"),
]

PLUGIN = "plugins/gnome/nm-gpclient-plugin-simple.c"
PLUGIN_RULES = {
    "get_capabilities": {
        "can import": r"NM_VPN_EDITOR_PLUGIN_CAPABILITY_IMPORT",
        "can export": r"NM_VPN_EDITOR_PLUGIN_CAPABILITY_EXPORT",
    },
    "import_from_file": {"calls the helper": r"return nm_gpclient_keyfile_import \(path, error\)"},
    "export_to_file": {"calls the helper": r"return nm_gpclient_keyfile_export \(connection, path, error\)"},
    "get_suggested_filename": {
        "calls the helper with the id": r"nm_gpclient_keyfile_suggested_filename \(nm_connection_get_id \(connection\)\)"
    },
    "gpclient_plugin_interface_init": {
        "sets import_from_file": r"iface->import_from_file = import_from_file;",
        "sets export_to_file": r"iface->export_to_file = export_to_file;",
        "sets get_suggested_filename": r"iface->get_suggested_filename = get_suggested_filename;",
    },
}


def missing(text, rules):
    """The rules whose pattern the function text does not contain"""
    return [rule for rule, pattern in rules.items() if not re.search(pattern, text, re.S)]


def comes_before(text, first, second):
    return first in text and second in text and text.index(first) < text.index(second)


CASES = [(HEADER, name, rule) for name, rules in RULES.items() for rule in rules] + [
    (PLUGIN, name, rule) for name, rules in PLUGIN_RULES.items() for rule in rules
]
ALL_RULES = {**RULES, **PLUGIN_RULES}


@pytest.mark.parametrize("path, name, rule", CASES)
def test_the_source_follows_the_rule(path, name, rule):
    assert rule not in missing(body(read(path), name), {rule: ALL_RULES[name][rule]})


def without_lines_of(text, name, pattern):
    """The source text with the lines that the pattern matches in the function `name` removed"""
    start = re.search(rf"^{re.escape(name)} \(.*?^}}", text, re.M | re.S).start()
    match = re.search(pattern, body(text, name), re.S)
    first = text.rfind("\n", 0, start + match.start()) + 1
    last = text.find("\n", start + match.end())
    return text[:first] + text[last + 1 :]


@pytest.mark.parametrize("path, name, rule", CASES)
def test_a_source_that_breaks_the_rule_is_found(path, name, rule):
    broken = without_lines_of(read(path), name, ALL_RULES[name][rule])
    assert missing(body(broken, name), {rule: ALL_RULES[name][rule]}) == [rule]


@pytest.mark.parametrize("name, first, second", ORDER)
def test_the_calls_come_in_the_right_order(name, first, second):
    assert comes_before(body(read(HEADER), name), first, second)


@pytest.mark.parametrize("name, first, second", ORDER)
def test_calls_in_the_wrong_order_are_found(name, first, second):
    text = body(read(HEADER), name)
    swapped = text.replace(first, "@@").replace(second, first).replace("@@", second)
    assert not comes_before(swapped, first, second)
    assert not comes_before(text.replace(first, ""), first, second)


# Realistic broken sources (the code before the fix): the checker must find them
BROKEN_IMPORT = {
    "refuses a connection that is not a VPN": """
    if (!s_con || !s_vpn
        || g_strcmp0 (nm_setting_vpn_get_service_type (s_vpn), NM_GPCLIENT_KEYFILE_SERVICE_TYPE) != 0) {""",
    # nm_keyfile_read returns NULL without an error for a relative base_dir (nmcli then crashes)
    "makes the base directory absolute for libnm": """
    base_dir = g_path_get_dirname (path);
    connection = nm_keyfile_read (keyfile, base_dir, NM_KEYFILE_HANDLER_FLAGS_NONE, NULL, NULL, error);""",
    "never returns NULL without an error": """
    if (!connection)
        return NULL;""",
}
# The directory made absolute only after the read is as broken as no directory at all
CANONICALIZED_TOO_LATE = """
    connection = nm_keyfile_read (keyfile, base_dir, NM_KEYFILE_HANDLER_FLAGS_NONE, NULL, NULL, error);
    base_dir = g_canonicalize_filename (dir, NULL);"""


@pytest.mark.parametrize("rule", BROKEN_IMPORT)
def test_the_import_before_the_fix_is_found(rule):
    rules = {rule: RULES["nm_gpclient_keyfile_import"][rule]}
    assert missing(BROKEN_IMPORT[rule], rules) == [rule]


def test_a_directory_made_absolute_after_the_read_is_found():
    rule = "makes the base directory absolute for libnm"
    rules = {rule: RULES["nm_gpclient_keyfile_import"][rule]}
    assert missing(CANONICALIZED_TOO_LATE, rules) == [rule]


def bad_import_errors(text):
    """The error calls of the import that have no translation or no error code"""
    calls = re.findall(r"g_set_error_literal \(.*?\);", body(text, "nm_gpclient_keyfile_import"), re.S)
    return [c for c in calls if "NM_CONNECTION_ERROR" not in c or not re.search(r"_\(", c)]


def test_every_import_error_is_translated_and_has_a_code():
    text = read(HEADER)
    assert bad_import_errors(text) == []
    assert "The file is not a GlobalProtect VPN connection" in text


@pytest.mark.parametrize(
    "old, new",
    [
        ('_("The file is not a GlobalProtect VPN connection")', '"The file is not a GlobalProtect VPN connection"'),
        ("_(problem)", "problem"),
        ("g_set_error_literal (error, NM_CONNECTION_ERROR, NM_CONNECTION_ERROR_INVALID_PROPERTY,", "g_set_error_literal (error, 0, 0,"),
    ],
)
def test_an_untranslated_error_or_one_without_a_code_is_found(old, new):
    text = read(HEADER)
    assert old in text
    assert bad_import_errors(text.replace(old, new)) != []


def makefile_rule_misses(text):
    """The headers that the rule of the libnm plugin does not list"""
    rule = re.search(r"^libnm-vpn-plugin-gpclient\.so:.*$", text, re.M).group(0)
    return [h for h in ("nm-gpclient-keyfile.h", "nm-gpclient-validate.h") if h not in rule]


def test_the_plugin_includes_the_header_and_the_makefile_rebuilds_on_a_change():
    assert '#include "nm-gpclient-keyfile.h"' in read(PLUGIN)
    assert makefile_rule_misses(read("plugins/gnome/Makefile")) == []


def test_a_makefile_rule_without_the_header_is_found():
    broken = read("plugins/gnome/Makefile").replace(" nm-gpclient-keyfile.h", "", 1)
    assert makefile_rule_misses(broken) == ["nm-gpclient-keyfile.h"]


def test_the_header_has_no_secret_in_an_export_and_no_fixed_uuid():
    text = read(HEADER)
    assert "nm_keyfile_write" in text and "nm_utils_uuid_generate" in text
    # the exported file must not get the secrets back: no call that adds a secret
    assert "nm_setting_vpn_add_secret" not in text


# ---- Plasma ------------------------------------------------------------------------------

PLASMA = read("plugins/plasma/gpclientui.cpp")
PLASMA_RULES = {
    "uses the helper": r'#include "nm-gpclient-keyfile.h"',
    "imports with the helper": r"nm_gpclient_keyfile_import\(",
    "exports with the helper": r"nm_gpclient_keyfile_export\(",
    "suggests a name with the helper": r"nm_gpclient_keyfile_suggested_filename\(",
    "offers the extension": r'QStringLiteral\("\*\.nmconnection"\)',
    "gets the saved profile from NetworkManager": r"nm_client_new\(",
    "looks it up by UUID": r"nm_client_get_connection_by_uuid\(client, connection->uuid\(\)",
    "exports the remote connection": r"nm_gpclient_keyfile_export\(NM_CONNECTION\(remote\)",
    "releases the client": r"g_object_unref\(client\)",
    "passes the connection": r"ImportResult::pass\(connection\)",
    "translates the message of the helper": r"translateMessage\(error->message\)",
    "fails an import with a translated text": r"ImportResult::fail\(i18n\(",
    "adds the extension when it is missing": r'!path\.endsWith\(QStringLiteral\("\.nmconnection"\)\)',
    "exports to the path with the extension": r"nm_gpclient_keyfile_export\(NM_CONNECTION\(remote\), QFile::encodeName\(path\)",
    "fails an export with a translated text": r"ExportResult::fail\(i18n\(",
    "leaves the old plasma-nm API alone": r"#define GPCLIENT_PLASMANM_OLD_API 1",
}


@pytest.mark.parametrize("rule", PLASMA_RULES)
def test_the_plasma_plugin_follows_the_rule(rule):
    assert missing(PLASMA, {rule: PLASMA_RULES[rule]}) == []


@pytest.mark.parametrize("rule", PLASMA_RULES)
def test_a_plasma_plugin_that_breaks_the_rule_is_found(rule):
    broken = re.sub(rf"^.*(?:{PLASMA_RULES[rule]}).*\n", "", PLASMA, flags=re.M)
    assert missing(broken, {rule: PLASMA_RULES[rule]}) == [rule]


def version_header_comes_first(text):
    """The version header of NetworkManagerQt is included before the guard that tests its macro"""
    include = text.find("#include <networkmanagerqt_version.h>")
    guard = text.find("#define GPCLIENT_PLASMANM_OLD_API")
    return 0 <= include < guard


def test_the_plasma_plugin_includes_the_version_header_before_the_guard():
    assert version_header_comes_first(PLASMA)


@pytest.mark.parametrize(
    "broken",
    [
        # no include: the macro is never defined and the guard is always true on Qt 5
        "#include <QFile>\n#if QT_VERSION < 1 && !defined(NETWORKMANAGERQT_VERSION)\n#define GPCLIENT_PLASMANM_OLD_API 1\n#endif\n",
        # included after the guard
        "#if QT_VERSION < 1 && !defined(NETWORKMANAGERQT_VERSION)\n#define GPCLIENT_PLASMANM_OLD_API 1\n#endif\n#include <networkmanagerqt_version.h>\n",
    ],
)
def test_a_guard_without_the_version_header_before_it_is_found(broken):
    assert not version_header_comes_first(broken)


def nm_errors_block_comes_first(text):
    """nm-errors.h is included in an extern "C" block before any other include"""
    first = re.search(r"^#include", text, re.M)
    block = re.search(r'extern "C" \{\s*#include <nm-errors.h>\s*\}', text)
    # <glib.h> may come before the block: it has its own C linkage
    lead = text[: block.start()] if block else ""
    return bool(first and block) and re.findall(r"^#include.*$", lead, re.M) in ([], ["#include <glib.h>"])


def test_the_plasma_plugin_gives_nm_errors_c_linkage_before_other_includes():
    assert nm_errors_block_comes_first(PLASMA)


@pytest.mark.parametrize(
    "broken",
    [
        # plain include: C++ linkage, undefined reference to nm_connection_error_quark()
        '#include <glib.h>\n#include <nm-errors.h>\n#include "gpclientui.h"\n',
        # the block comes after another include that may pull in NetworkManager.h
        '#include "gpclientui.h"\nextern "C" {\n#include <nm-errors.h>\n}\n',
        '#include "gpclientui.h"\n',
    ],
)
def test_nm_errors_without_c_linkage_first_is_found(broken):
    assert not nm_errors_block_comes_first(broken)


def test_the_plasma_header_declares_the_methods_for_every_plasma_nm():
    header = read("plugins/plasma/gpclientui.h")
    for name in ("supportedFileExtensions", "importConnectionSettings", "exportConnectionSettings"):
        assert name in header
    assert "#if" not in header.replace("#ifndef PLASMA_NM_GPCLIENT_UI_H", "")


def hand_made_connections(text):
    """The calls that build a connection by hand or read secrets"""
    return [m for m in ("nm_simple_connection_new(", "nm_setting_vpn_add_data_item", "secrets()") if m in text]


def test_the_plasma_export_does_not_build_a_connection_by_hand():
    # a hand-made connection drops the settings that the user set (routes, IPv6, ...)
    assert hand_made_connections(PLASMA) == []


def test_a_plasma_export_with_a_hand_made_connection_is_found():
    line = "    NMClient *client = nm_client_new(nullptr, &error);"
    assert line in PLASMA
    broken = PLASMA.replace(line, "    NMConnection *nm = nm_simple_connection_new();")
    assert hand_made_connections(broken) == ["nm_simple_connection_new("]


# ---- Plasma catalog -------------------------------------------------------------------------

VALIDATOR = "plugins/gnome/nm-gpclient-validate.h"
POT = "plugins/plasma/po/plasmanetworkmanagement_gpclientui.pot"


def english_messages(*texts):
    """The texts of _("...") and N_("...") in the helper headers"""
    return {m for t in texts for m in re.findall(r'\b_N?\w*\("((?:[^"\\]|\\.)*)"\)', t) + re.findall(r'\bN_\("((?:[^"\\]|\\.)*)"\)', t)}


def catalog(text):
    """msgid -> msgstr of a .pot or .po text (multi-line strings joined)"""
    result = {}
    for msgid, msgstr in re.findall(r'^msgid ((?:".*"\n)+)msgstr ((?:".*"\n?)+)', text, re.M):
        join = lambda block: "".join(re.findall(r'"(.*)"', block)).replace('\\"', '"')
        result[join(msgid)] = join(msgstr)
    return result


def uncovered(headers, catalog_text, cpp, translated=False):
    """Messages of the headers that the Plasma catalog (or the .cpp list) does not have"""
    entries = catalog(catalog_text)
    return sorted(
        m
        for m in english_messages(*headers)
        if m not in entries or (translated and not entries[m]) or f'ki18n("{m}")' not in cpp
    )


def test_every_message_of_the_helper_headers_is_in_the_plasma_catalog():
    headers = [read(HEADER), read(VALIDATOR)]
    assert len(english_messages(*headers)) == 7
    assert uncovered(headers, read(POT), PLASMA) == []


def test_every_message_of_the_helper_headers_is_translated_in_every_plasma_language():
    headers = [read(HEADER), read(VALIDATOR)]
    po_dir = os.path.join(ROOT, "plugins/plasma/po")
    langs = [d for d in os.listdir(po_dir) if os.path.isdir(os.path.join(po_dir, d))]
    assert langs
    for lang in langs:
        po = read(f"plugins/plasma/po/{lang}/plasmanetworkmanagement_gpclientui.po")
        assert uncovered(headers, po, PLASMA, translated=True) == [], lang


def test_a_new_message_without_a_catalog_entry_is_found():
    headers = [read(HEADER), read(VALIDATOR).replace('N_("gateway is required")', 'N_("gateway is needed")')]
    assert uncovered(headers, read(POT), PLASMA) == ["gateway is needed"]


def test_a_message_missing_in_the_catalog_or_the_cpp_list_is_found():
    headers = [read(HEADER), read(VALIDATOR)]
    pot = read(POT)
    no_entry = pot.replace('msgid "hip-script must be an absolute path"', 'msgid "x"')
    assert uncovered(headers, no_entry, PLASMA) == ["hip-script must be an absolute path"]
    no_marker = PLASMA.replace('ki18n("hip-script must be an absolute path")', 'ki18n("x")')
    assert uncovered(headers, pot, no_marker) == ["hip-script must be an absolute path"]


def test_an_empty_translation_is_found():
    headers = [read(HEADER), read(VALIDATOR)]
    po = read("plugins/plasma/po/de/plasmanetworkmanagement_gpclientui.po")
    assert uncovered(headers, po, PLASMA, translated=True) == []
    broken = po.replace('msgstr "hip-script muss ein absoluter Pfad sein"', 'msgstr ""')
    assert broken != po
    assert uncovered(headers, broken, PLASMA, translated=True) == ["hip-script must be an absolute path"]

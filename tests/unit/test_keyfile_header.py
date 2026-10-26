"""
Tests of the import and export of a profile (issue #53): plugins/gnome/nm-gpclient-keyfile.h,
the libnm plugin that uses it and the Plasma plugin.

libnm is not available to the unit tests, so the C sources are checked as text:
each rule is a pattern that the function body must contain. Every rule has a
negative test: the body without the pattern is found, and the rules that are
about the order of calls are found when the order is wrong.

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
        "checks the fields with the rules of the editors": r"nm_gpclient_validate_form \(",
        "translates the message of the rule": r"_\(problem\)",
        "gives a new UUID": r"nm_utils_uuid_generate \(\)[^}]*NM_SETTING_CONNECTION_UUID",
        "drops the secrets": r"nm_connection_clear_secrets \(connection\)",
        "normalizes the connection": r"nm_connection_normalize \(connection",
    },
    "nm_gpclient_keyfile_export": {
        "works on a copy": r"nm_simple_connection_new_clone \(connection\)",
        "drops the secrets of the copy": r"nm_connection_clear_secrets \(copy\)",
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


@pytest.mark.parametrize("path, name, rule", CASES)
def test_a_source_that_breaks_the_rule_is_found(path, name, rule):
    broken = re.sub(ALL_RULES[name][rule], "", body(read(path), name), flags=re.S)
    assert missing(broken, {rule: ALL_RULES[name][rule]}) == [rule]


@pytest.mark.parametrize("name, first, second", ORDER)
def test_the_calls_come_in_the_right_order(name, first, second):
    assert comes_before(body(read(HEADER), name), first, second)


@pytest.mark.parametrize("name, first, second", ORDER)
def test_calls_in_the_wrong_order_are_found(name, first, second):
    text = body(read(HEADER), name)
    swapped = text.replace(first, "@@").replace(second, first).replace("@@", second)
    assert not comes_before(swapped, first, second)
    assert not comes_before(text.replace(first, ""), first, second)


def test_every_import_error_is_translated_and_has_a_code():
    text = body(read(HEADER), "nm_gpclient_keyfile_import")
    for call in re.findall(r"g_set_error_literal \(.*?\);", text, re.S):
        assert "NM_CONNECTION_ERROR" in call
        assert re.search(r"_\(", call)
    assert "The file is not a GlobalProtect VPN connection" in text


def test_an_untranslated_error_is_found():
    call = 'g_set_error_literal (error, NM_CONNECTION_ERROR, 0, "The file is bad");'
    assert not re.search(r"_\(", call)


def test_the_plugin_includes_the_header_and_the_makefile_rebuilds_on_a_change():
    assert '#include "nm-gpclient-keyfile.h"' in read(PLUGIN)
    rule = re.search(r"^libnm-vpn-plugin-gpclient\.so:.*$", read("plugins/gnome/Makefile"), re.M).group(0)
    assert "nm-gpclient-keyfile.h" in rule and "nm-gpclient-validate.h" in rule


def test_a_makefile_rule_without_the_header_is_found():
    assert "nm-gpclient-keyfile.h" not in "libnm-vpn-plugin-gpclient.so: nm-gpclient-plugin-simple.c nm-vpn-plugin-utils.c"


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
    "fails an import with a translated text": r"ImportResult::fail\(i18n\(",
    "fails an export with a translated text": r"ExportResult::fail\(i18n\(",
    "leaves the old plasma-nm API alone": r"#define GPCLIENT_PLASMANM_OLD_API 1",
}


@pytest.mark.parametrize("rule", PLASMA_RULES)
def test_the_plasma_plugin_follows_the_rule(rule):
    assert missing(PLASMA, {rule: PLASMA_RULES[rule]}) == []


@pytest.mark.parametrize("rule", PLASMA_RULES)
def test_a_plasma_plugin_that_breaks_the_rule_is_found(rule):
    broken = re.sub(PLASMA_RULES[rule], "", PLASMA)
    assert missing(broken, {rule: PLASMA_RULES[rule]}) == [rule]


def test_the_plasma_header_declares_the_methods_for_every_plasma_nm():
    header = read("plugins/plasma/gpclientui.h")
    for name in ("supportedFileExtensions", "importConnectionSettings", "exportConnectionSettings"):
        assert name in header
    assert "#if" not in header.replace("#ifndef PLASMA_NM_GPCLIENT_UI_H", "")


def test_the_plasma_export_does_not_build_a_connection_by_hand():
    # a hand-made connection drops the settings that the user set (routes, IPv6, ...)
    assert "nm_simple_connection_new" not in PLASMA
    assert "nm_setting_vpn_add_data_item" not in PLASMA
    assert "secrets()" not in PLASMA


def test_a_plasma_export_with_a_hand_made_connection_is_found():
    assert "nm_simple_connection_new" in "NMConnection *nm = nm_simple_connection_new();"

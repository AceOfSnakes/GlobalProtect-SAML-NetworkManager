"""
Behaviour tests of plugins/gnome/nm-gpclient-editor-common.h, the logic that the
GTK3 and GTK4 editors share (issue #53).

The header is compiled with gcc against GLib and a small stub of the libnm API it
calls (a fake NetworkManager.h that shadows the real one; NMSettingVpn is a set
of hash tables). Every helper runs with good and bad input. The expected values
come from the code the editors had before the logic moved
(nm-gpclient-editor.c and nm-gpclient-editor-gtk3.c on origin/main), not from the
header. Without a compiler or the GLib headers the tests are skipped on a
developer machine and FAIL in CI (the CI environment variable is set), like
test_mtu_header.py.
"""

import os
import shutil
import subprocess

import pytest

HEADER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "gnome")

STUB = r"""
#include <glib.h>
typedef struct { GHashTable *data, *secrets, *flags; } NMSettingVpn;
typedef NMSettingVpn NMSetting;
typedef int NMSettingSecretFlags;
#define NM_SETTING_SECRET_FLAG_NONE 0
#define NM_SETTING_SECRET_FLAG_AGENT_OWNED 1
#define NM_SETTING_SECRET_FLAG_NOT_SAVED 2
#define NM_SETTING(x) ((NMSetting *) (x))
static inline int nm_setting_vpn_get_num_data_items (NMSettingVpn *s) { return g_hash_table_size (s->data); }
static inline const char *nm_setting_vpn_get_data_item (NMSettingVpn *s, const char *k) { return g_hash_table_lookup (s->data, k); }
static inline void nm_setting_vpn_add_data_item (NMSettingVpn *s, const char *k, const char *v) { g_hash_table_replace (s->data, g_strdup (k), g_strdup (v)); }
static inline void nm_setting_vpn_remove_data_item (NMSettingVpn *s, const char *k) { g_hash_table_remove (s->data, k); }
static inline void nm_setting_vpn_add_secret (NMSettingVpn *s, const char *k, const char *v) { g_hash_table_replace (s->secrets, g_strdup (k), g_strdup (v)); }
static inline void nm_setting_vpn_remove_secret (NMSettingVpn *s, const char *k) { g_hash_table_remove (s->secrets, k); }
/* libnm: a secret without flags has the flags 0 */
static inline gboolean nm_setting_get_secret_flags (NMSetting *s, const char *k, NMSettingSecretFlags *f, void *e)
{ gpointer v; *f = g_hash_table_lookup_extended (s->flags, k, NULL, &v) ? GPOINTER_TO_INT (v) : 0; return TRUE; }
static inline gboolean nm_setting_set_secret_flags (NMSetting *s, const char *k, NMSettingSecretFlags f, void *e)
{ g_hash_table_replace (s->flags, g_strdup (k), GINT_TO_POINTER (f)); return TRUE; }
"""

# Command line: op, then items. "d:key=value" is a vpn.data item, "s:key=value" a
# secret, "f:key=N" secret flags, "nosetting" means no NMSettingVpn (NULL), and
# "a:text" is an argument ("a:@NULL" is a NULL pointer).
PROGRAM = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <glib/gi18n-lib.h>
#include "nm-gpclient-editor-common.h"

static void dump (const char *prefix, GHashTable *t, gboolean ints)
{
    GList *keys = g_list_sort (g_hash_table_get_keys (t), (GCompareFunc) strcmp), *l;
    for (l = keys; l; l = l->next)
        if (ints) printf ("%s%s=%d\n", prefix, (char *) l->data, GPOINTER_TO_INT (g_hash_table_lookup (t, l->data)));
        else printf ("%s%s=%s\n", prefix, (char *) l->data, (char *) g_hash_table_lookup (t, l->data));
    g_list_free (keys);
}

int main (int argc, char **argv)
{
    NMSettingVpn s = { g_hash_table_new (g_str_hash, g_str_equal), g_hash_table_new (g_str_hash, g_str_equal),
                       g_hash_table_new (g_str_hash, g_str_equal) };
    NMSettingVpn *v = &s;
    const char *a[8] = { 0 };
    int na = 0, i;
    const char *op = argv[1];
    long ret = 0;
    int have_ret = 1;

    for (i = 2; i < argc; i++) {
        char *eq = strchr (argv[i], '=');
        if (!strcmp (argv[i], "nosetting")) v = NULL;
        else if (!strncmp (argv[i], "d:", 2) && eq) { *eq = 0; nm_setting_vpn_add_data_item (&s, argv[i] + 2, eq + 1); }
        else if (!strncmp (argv[i], "s:", 2) && eq) { *eq = 0; nm_setting_vpn_add_secret (&s, argv[i] + 2, eq + 1); }
        else if (!strncmp (argv[i], "f:", 2) && eq) { *eq = 0; nm_setting_set_secret_flags (&s, argv[i] + 2, atoi (eq + 1), NULL); }
        else if (!strncmp (argv[i], "a:", 2) && na < 8) a[na++] = strcmp (argv[i] + 2, "@NULL") ? argv[i] + 2 : NULL;
    }

    if (!strcmp (op, "set_or_remove")) { nm_gpclient_editor_set_or_remove (v, a[0], a[1]); have_ret = 0; }
    else if (!strcmp (op, "is_new")) ret = nm_gpclient_editor_is_new (v);
    else if (!strcmp (op, "data_is_true")) ret = nm_gpclient_editor_data_is_true (v, a[0]);
    else if (!strcmp (op, "hip_enabled")) ret = nm_gpclient_editor_hip_enabled (v);
    else if (!strcmp (op, "auth_index")) ret = nm_gpclient_editor_auth_index (v);
    else if (!strcmp (op, "fix_openssl_index")) ret = nm_gpclient_editor_fix_openssl_index (v);
    else if (!strcmp (op, "timeout_initial")) ret = (long) nm_gpclient_editor_timeout_initial (atoi (a[0]), atol (a[1]), atoi (a[2]));
    else if (!strcmp (op, "parse_timeout")) ret = (long) nm_gpclient_editor_parse_timeout (a[0], 0, 4294967295.0);
    else if (!strcmp (op, "password_flags_initial")) ret = nm_gpclient_editor_password_flags_initial (v, atoi (a[0]));
    else if (!strcmp (op, "save_password")) { nm_gpclient_editor_save_password (v, atoi (a[0]), atoi (a[1]), atoi (a[2]), atoi (a[3]), a[4]); have_ret = 0; }
    else if (!strcmp (op, "default_otp")) { nm_gpclient_editor_default_otp_flags (v, atoi (a[0])); have_ret = 0; }
    else if (!strcmp (op, "save_mtu")) { nm_gpclient_editor_save_mtu (v, atoi (a[0]), a[1], atoi (a[2])); have_ret = 0; }
    else if (!strcmp (op, "save_fix_openssl")) { nm_gpclient_editor_save_fix_openssl (v, atoi (a[0])); have_ret = 0; }
    else if (!strcmp (op, "save_browser")) { nm_gpclient_editor_save_browser (v, a[0]); have_ret = 0; }
    else if (!strcmp (op, "constants")) { printf ("saml=%d\nauto=%s\nbrowsers=", SAML_TIMEOUT_DEFAULT, GATEWAY_AUTO_LABEL);
        for (i = 0; browser_values[i]; i++) { printf ("%s;", browser_values[i]); }
        printf ("\n"); have_ret = 0; }
    else return 2;

    if (have_ret) printf ("ret=%ld\n", ret);
    dump ("d:", s.data, 0);
    dump ("s:", s.secrets, 0);
    dump ("f:", s.flags, 1);
    return 0;
}
"""


@pytest.fixture(scope="module")
def program(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("gcc")
    glib = None
    if compiler and shutil.which("pkg-config"):
        glib = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0"], capture_output=True, text=True)
    if not compiler or glib is None or glib.returncode != 0:
        message = "no C compiler, pkg-config or GLib headers (libglib2.0-dev) for nm-gpclient-editor-common.h"
        if os.environ.get("CI"):
            pytest.fail(message + " in CI")
        pytest.skip(message)
    tmp = tmp_path_factory.mktemp("editor_common")
    (tmp / "NetworkManager.h").write_text(STUB)
    (tmp / "check.c").write_text(PROGRAM)
    result = subprocess.run(
        [compiler, "-Wall", "-Werror", '-DGETTEXT_PACKAGE="test"', "-I", str(tmp), "-I", HEADER_DIR,
         str(tmp / "check.c"), "-o", str(tmp / "check")] + glib.stdout.split(),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return str(tmp / "check")


def run(program, op, *items):
    """Run one helper. Returns (ret, data, secrets, flags) after the call."""
    out = subprocess.run([program, op, *items], capture_output=True, text=True, check=True).stdout
    ret, data, secrets, flags = None, {}, {}, {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "ret":
            ret = int(value)
        elif line.startswith("d:"):
            data[key[2:]] = value
        elif line.startswith("s:"):
            secrets[key[2:]] = value
        elif line.startswith("f:"):
            flags[key[2:]] = int(value)
    return ret, data, secrets, flags


def a(*args):
    return [f"a:{'@NULL' if x is None else x}" for x in args]


def test_the_constants(program):
    out = subprocess.run([program, "constants"], capture_output=True, text=True, check=True).stdout

    assert "saml=360\n" in out
    assert "auto=First proposed by portal (automatic)\n" in out
    assert "browsers=edge;firefox;chrome;chromium;default;\n" in out


# --- set_or_remove: add a non-empty value, remove for NULL or "" (old: str && *str)
@pytest.mark.parametrize("value, expected", [("x", "x"), (" ", " "), ("a;b", "a;b")])
def test_set_or_remove_saves_a_value(program, value, expected):
    assert run(program, "set_or_remove", *a("dns", value))[1] == {"dns": expected}
    assert run(program, "set_or_remove", *a("dns", value), "d:dns=old")[1] == {"dns": expected}


@pytest.mark.parametrize("value", [None, ""])
def test_set_or_remove_removes_on_empty(program, value):
    assert run(program, "set_or_remove", *a("dns", value), "d:dns=old", "d:other=1")[1] == {"other": "1"}
    assert run(program, "set_or_remove", *a("dns", value))[1] == {}


# --- is_new: no setting or no vpn.data items
@pytest.mark.parametrize("items, expected", [
    ([], 1), (["nosetting"], 1), (["d:gateway=g"], 0), (["d:hip=true", "d:dns=1"], 0), (["s:password=x"], 1),
])
def test_is_new(program, items, expected):
    assert run(program, "is_new", *items)[0] == expected


# --- data_is_true: g_ascii_strcasecmp (value ? value : "", "true") == 0
@pytest.mark.parametrize("items, expected", [
    (["d:as-gateway=true"], 1), (["d:as-gateway=TRUE"], 1), (["d:as-gateway=True"], 1),
    (["d:as-gateway=yes"], 0), (["d:as-gateway=1"], 0), (["d:as-gateway="], 0), (["d:as-gateway=truex"], 0),
    (["d:as-gateway=false"], 0), ([], 0), (["nosetting"], 0), (["d:other=true"], 0),
])
def test_data_is_true(program, items, expected):
    assert run(program, "data_is_true", *a("as-gateway"), *items)[0] == expected


# --- hip_enabled: default on; a non-empty value is on only when "true" (any case)
@pytest.mark.parametrize("items, expected", [
    ([], 1), (["nosetting"], 1), (["d:hip="], 1), (["d:hip=true"], 1), (["d:hip=TRUE"], 1),
    (["d:hip=false"], 0), (["d:hip=x"], 0), (["d:hip=yes"], 0), (["d:hip=0"], 0), (["d:hip=FALSE"], 0),
])
def test_hip_enabled(program, items, expected):
    assert run(program, "hip_enabled", *items)[0] == expected


# --- auth_index: 1 only for "credentials" (case-sensitive, g_strcmp0)
@pytest.mark.parametrize("items, expected", [
    (["d:auth-mode=credentials"], 1), (["d:auth-mode=Credentials"], 0), (["d:auth-mode=saml"], 0),
    (["d:auth-mode="], 0), ([], 0), (["nosetting"], 0), (["d:auth-mode=credentialsx"], 0),
])
def test_auth_index(program, items, expected):
    assert run(program, "auth_index", *items)[0] == expected


# --- fix_openssl_index: true -> 1, false -> 2 (any case), the rest -> 0
@pytest.mark.parametrize("items, expected", [
    (["d:fix-openssl=true"], 1), (["d:fix-openssl=TRUE"], 1), (["d:fix-openssl=false"], 2),
    (["d:fix-openssl=False"], 2), (["d:fix-openssl=auto"], 0), (["d:fix-openssl="], 0),
    (["d:fix-openssl=yes"], 0), (["d:fix-openssl=1"], 0), ([], 0), (["nosetting"], 0),
])
def test_fix_openssl_index(program, items, expected):
    assert run(program, "fix_openssl_index", *items)[0] == expected


# --- timeout_initial(is_new, stored, auth_index): a new profile with 0 starts at
# 360 for browser authentication; any other case shows the stored value
@pytest.mark.parametrize("args, expected", [
    ((1, 0, 0), 360), ((1, 0, 1), 0), ((0, 0, 0), 0), ((0, 0, 1), 0),
    ((1, 90, 0), 90), ((1, 90, 1), 90), ((0, 90, 0), 90), ((0, 4000000000, 1), 4000000000),
])
def test_timeout_initial(program, args, expected):
    assert run(program, "timeout_initial", *a(*args))[0] == expected


# --- parse_timeout: g_ascii_strtod, then clamp to 0..G_MAXUINT32
@pytest.mark.parametrize("text, expected", [
    ("90", 90), ("+90", 90), ("0", 0), ("360", 360), ("90.7", 90), ("1e2", 100),
    ("-5", 0), ("", 0), (None, 0), ("abc", 0), ("-", 0), ("x90", 0),
    ("99999999999999", 4294967295), ("4294967295", 4294967295), ("4294967296", 4294967295),
])
def test_parse_timeout(program, text, expected):
    assert run(program, "parse_timeout", *a(text))[0] == expected


# --- password_flags_initial: new -> agent-owned (1); existing keeps its flags (0 if none)
@pytest.mark.parametrize("args, items, expected", [
    ((1,), [], 1), ((1,), ["f:password=4"], 1),
    ((0,), [], 0), ((0,), ["d:gateway=g"], 0), ((0,), ["d:gateway=g", "f:password=4"], 4),
    ((0,), ["d:gateway=g", "f:password=1"], 1), ((0,), ["d:gateway=g", "f:password=2"], 2),
])
def test_password_flags_initial(program, args, items, expected):
    assert run(program, "password_flags_initial", *a(*args), *items)[0] == expected


# --- save_password(is_new, menu_flags, flags, credentials, text)
# Old rule: flags are written when (new and not 0) or changed; the secret is
# touched only in credentials mode, and saved only for a non-empty text without
# the not-saved flag.
@pytest.mark.parametrize("args, items, secrets, flags", [
    # a new profile: agent-owned flags are written, the secret is saved
    ((1, 1, 1, 1, "pw"), [], {"password": "pw"}, {"password": 1}),
    # a new profile with flags 0 writes nothing
    ((1, 0, 0, 1, "pw"), [], {"password": "pw"}, {}),
    # an existing profile, menu unchanged: flags are kept (4 is not shown by the menu)
    ((0, 4, 4, 1, "pw"), ["f:password=4"], {"password": "pw"}, {"password": 4}),
    ((0, 0, 0, 1, "pw"), [], {"password": "pw"}, {}),
    # an existing profile, menu changed: the new flags are written
    ((0, 0, 1, 1, "pw"), [], {"password": "pw"}, {"password": 1}),
    ((0, 1, 0, 1, "pw"), ["f:password=1"], {"password": "pw"}, {"password": 0}),
    # not saved: the secret is removed
    ((0, 0, 2, 1, "pw"), ["s:password=old"], {}, {"password": 2}),
    ((1, 1, 2, 1, "pw"), ["s:password=old"], {}, {"password": 2}),
    # empty or missing text in credentials mode removes the secret
    ((0, 0, 0, 1, ""), ["s:password=old"], {}, {}),
    ((0, 0, 0, 1, None), ["s:password=old"], {}, {}),
    # SAML mode: the secret is not touched
    ((0, 0, 0, 0, "pw"), ["s:password=old"], {"password": "old"}, {}),
    ((0, 0, 0, 0, ""), ["s:password=old"], {"password": "old"}, {}),
    ((0, 0, 0, 0, "pw"), [], {}, {}),
    # SAML mode still writes changed flags
    ((0, 0, 1, 0, "pw"), ["s:password=old"], {"password": "old"}, {"password": 1}),
])
def test_save_password(program, args, items, secrets, flags):
    _, _, got_secrets, got_flags = run(program, "save_password", *a(*args), *items)

    assert got_secrets == secrets
    assert got_flags == flags


# --- default_otp: only a new profile without the key gets "2"
@pytest.mark.parametrize("args, items, expected", [
    ((1,), [], {"otp-flags": "2"}),
    ((1,), ["d:otp-flags=0"], {"otp-flags": "0"}),
    ((0,), [], {}),
    ((0,), ["d:gateway=g"], {"gateway": "g"}),
    ((0,), ["d:otp-flags=0"], {"otp-flags": "0"}),
])
def test_default_otp_flags(program, args, items, expected):
    assert run(program, "default_otp", *a(*args), *items)[1] == expected


# --- save_mtu(mtu, stored, touched): 0 = no key; 1..575 -> 576; max 1500; an
# invalid stored text that the user did not touch is kept
@pytest.mark.parametrize("args, items, expected", [
    ((1300, None, 0), [], {"mtu": "1300"}),
    ((576, None, 0), [], {"mtu": "576"}),
    ((1500, None, 1), [], {"mtu": "1500"}),
    ((100, None, 1), [], {"mtu": "576"}),
    ((1, None, 1), [], {"mtu": "576"}),
    ((2000, None, 1), [], {"mtu": "1500"}),
    ((0, None, 0), ["d:mtu=1300"], {}),
    ((0, None, 1), ["d:mtu=1300"], {}),
    ((-5, None, 1), ["d:mtu=1300"], {}),
    ((0, "0", 0), ["d:mtu=0"], {}),
    ((1300, "1300", 0), [], {"mtu": "1300"}),
    # invalid stored text: kept when untouched, whatever the spin button shows
    ((0, "abc", 0), [], {"mtu": "abc"}),
    ((1400, "abc", 0), [], {"mtu": "abc"}),
    ((0, "1501", 0), ["d:mtu=1501"], {"mtu": "1501"}),
    # invalid stored text that the user touched: the spin button wins
    ((0, "abc", 1), ["d:mtu=abc"], {}),
    ((1400, "abc", 1), ["d:mtu=abc"], {"mtu": "1400"}),
])
def test_save_mtu(program, args, items, expected):
    assert run(program, "save_mtu", *a(*args), *items)[1] == expected


# --- save_fix_openssl: 1 -> true, 2 -> false, anything else removes the key
@pytest.mark.parametrize("index, expected", [(1, {"fix-openssl": "true"}), (2, {"fix-openssl": "false"})])
def test_save_fix_openssl_writes(program, index, expected):
    assert run(program, "save_fix_openssl", *a(index), "d:fix-openssl=old")[1] == expected


@pytest.mark.parametrize("index", [0, -1, 3, 99])
def test_save_fix_openssl_removes(program, index):
    assert run(program, "save_fix_openssl", *a(index), "d:fix-openssl=true", "d:dns=1")[1] == {"dns": "1"}


# --- save_browser: the "Custom..." prompt, "" and NULL store nothing
@pytest.mark.parametrize("text", ["edge", "firefox", "default", "/opt/my browser", "custom...", "Custom...x", " Custom..."])
def test_save_browser_saves(program, text):
    assert run(program, "save_browser", *a(text), "d:browser=old")[1] == {"browser": text}
    assert run(program, "save_browser", *a(text))[1] == {"browser": text}


@pytest.mark.parametrize("text", ["Custom...", "", None])
def test_save_browser_removes(program, text):
    assert run(program, "save_browser", *a(text), "d:browser=edge", "d:dns=1")[1] == {"dns": "1"}
    assert run(program, "save_browser", *a(text))[1] == {}

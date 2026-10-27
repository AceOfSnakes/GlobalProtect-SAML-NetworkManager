/* Toolkit-independent logic of the GTK3 and GTK4 connection editors.
 *
 * It uses GLib and libnm only, no GTK, so both editors include it and keep only
 * the widget code. Every function is a static inline helper, like in
 * nm-gpclient-validate.h, so the Makefile needs no new source. The keys and
 * defaults are in one place; tests/unit/test_editor_common.py checks that the
 * editors do not define them again. */

#ifndef NM_GPCLIENT_EDITOR_COMMON_H
#define NM_GPCLIENT_EDITOR_COMMON_H

#include <glib.h>
#include <glib/gi18n-lib.h>
#include <NetworkManager.h>
#include "nm-gpclient-mtu.h"

/* First entry of the preferred-gateway combo: let the portal decide. Stored as
 * an empty vpn.data value, so existing profiles keep behaving the same. */
#define GATEWAY_AUTO_LABEL _("First proposed by portal (automatic)")

/* NetworkManager stops a VPN activation after vpn.timeout seconds (0 = its
 * default, usually 60 s), which is too short for a SAML login with MFA. A new
 * connection with browser authentication starts with this value. While the
 * stored value is 0 and the user has not changed the field, choosing browser
 * authentication sets it (issue #46). It is longer than the limit of the
 * browser login itself (GP_AUTH_TIMEOUT, 300 s, scripts/browser-wrapper.sh)
 * plus the time to connect after the login, so that limit and its clearer
 * error come first. */
#define SAML_TIMEOUT_DEFAULT 360

/* Browser values understood by the service (see resolve_browser()) */
static const char *browser_values[] = {
    "edge", "firefox", "chrome", "chromium", "default", NULL
};

/* A profile that exists has at least the gateway in vpn.data */
static inline gboolean
nm_gpclient_editor_is_new (NMSettingVpn *s_vpn)
{
    return !s_vpn || nm_setting_vpn_get_num_data_items (s_vpn) == 0;
}

/* Add the vpn.data item, or remove it when the value is NULL or empty */
static inline void
nm_gpclient_editor_set_or_remove (NMSettingVpn *s_vpn, const char *key, const char *value)
{
    if (value && *value)
        nm_setting_vpn_add_data_item (s_vpn, key, value);
    else
        nm_setting_vpn_remove_data_item (s_vpn, key);
}

/* The browser combo: its "Custom..." entry is a prompt, not a browser */
static inline void
nm_gpclient_editor_save_browser (NMSettingVpn *s_vpn, const char *text)
{
    nm_gpclient_editor_set_or_remove (s_vpn, "browser",
        g_strcmp0 (text, _("Custom...")) != 0 ? text : NULL);
}

/* TRUE when the vpn.data item is "true" (any case); a missing item is FALSE */
static inline gboolean
nm_gpclient_editor_data_is_true (NMSettingVpn *s_vpn, const char *key)
{
    const char *value = s_vpn ? nm_setting_vpn_get_data_item (s_vpn, key) : NULL;

    return g_ascii_strcasecmp (value ? value : "", "true") == 0;
}

/* HIP is on unless the stored value is something other than "true" */
static inline gboolean
nm_gpclient_editor_hip_enabled (NMSettingVpn *s_vpn)
{
    const char *value = s_vpn ? nm_setting_vpn_get_data_item (s_vpn, "hip") : NULL;

    return (value && *value) ? g_ascii_strcasecmp (value, "true") == 0 : TRUE;
}

/* Index of the authentication combo: 1 = "Username and password", 0 = browser */
static inline int
nm_gpclient_editor_auth_index (NMSettingVpn *s_vpn)
{
    return s_vpn && g_strcmp0 (nm_setting_vpn_get_data_item (s_vpn, "auth-mode"), "credentials") == 0 ? 1 : 0;
}

/* Index of the legacy TLS combo: 0 = automatic (no key), 1 = on, 2 = off */
static inline int
nm_gpclient_editor_fix_openssl_index (NMSettingVpn *s_vpn)
{
    const char *value = s_vpn ? nm_setting_vpn_get_data_item (s_vpn, "fix-openssl") : NULL;

    if (g_ascii_strcasecmp (value ? value : "", "true") == 0)
        return 1;
    if (g_ascii_strcasecmp (value ? value : "", "false") == 0)
        return 2;
    return 0;
}

/* The value of the timeout spin button at load. A new profile starts with 360 s
 * for browser authentication; a stored 0 of an existing profile stays 0. */
static inline double
nm_gpclient_editor_timeout_initial (gboolean is_new, guint stored, int auth_index)
{
    if (is_new && stored == 0)
        return auth_index == 0 ? SAML_TIMEOUT_DEFAULT : 0;
    return (double) stored;
}

/* The text of a numeric spin button as GTK parses it on commit (a sign at
 * position 0 is accepted): g_ascii_strtod, then clamp to the range ("+90" is
 * 90, "-5" is 0, a very long number is the upper limit, empty text is 0). */
static inline guint
nm_gpclient_editor_parse_timeout (const char *text, double lower, double upper)
{
    double v = g_ascii_strtod (text ? text : "", NULL);

    v = CLAMP (v, lower, upper);
    return (guint) v;
}

/* The secret flags the storage menu of the secret `key` ("password" or
 * "cert-pass") starts with: a new connection asks the secret agent
 * (<key>-flags=1); an existing one keeps its flags (a missing key is 0,
 * system-owned). */
static inline NMSettingSecretFlags
nm_gpclient_editor_password_flags_initial (NMSettingVpn *s_vpn, const char *key, gboolean is_new)
{
    NMSettingSecretFlags flags = NM_SETTING_SECRET_FLAG_AGENT_OWNED;

    if (!is_new)
        nm_setting_get_secret_flags (NM_SETTING (s_vpn), key, &flags, NULL);
    return flags;
}

/* Save the flags and the value of the secret `key` ("password" or "cert-pass").
 * A new connection gets <key>-flags=1; an existing one without the key stays without it. The
 * secret is kept out of the connection when it is not to be saved. An
 * agent-owned secret goes to NetworkManager, which hands it to the agent and
 * keeps it out of the profile file. menu_flags is what the menu showed at load,
 * flags what it shows now. The secret is touched only when `credentials` is
 * true ("Username and password" for the password, a client certificate for
 * the passphrase). */
static inline void
nm_gpclient_editor_save_password (NMSettingVpn *s_vpn,
                                  const char *key,
                                  gboolean is_new,
                                  NMSettingSecretFlags menu_flags,
                                  NMSettingSecretFlags flags,
                                  gboolean credentials,
                                  const char *str)
{
    /* Only a new connection or a change in the menu writes the flags, so
     * bits the menu cannot show (4, not required) are kept */
    if ((is_new && flags != NM_SETTING_SECRET_FLAG_NONE) || flags != menu_flags)
        nm_setting_set_secret_flags (NM_SETTING (s_vpn), key, flags, NULL);
    if (credentials) {
        if (str && *str && !(flags & NM_SETTING_SECRET_FLAG_NOT_SAVED))
            nm_setting_vpn_add_secret (s_vpn, key, str);
        else
            nm_setting_vpn_remove_secret (s_vpn, key);
    }
}

/* Save the client certificate: the two paths (stripped) and the passphrase of
 * the key. Without a certificate the stored passphrase is removed (its flags
 * are kept), like the Plasma editor. */
static inline void
nm_gpclient_editor_save_cert (NMSettingVpn *s_vpn,
                              const char *usercert,
                              const char *userkey,
                              gboolean is_new,
                              NMSettingSecretFlags menu_flags,
                              NMSettingSecretFlags flags,
                              const char *pass)
{
    gchar *cert = g_strstrip (g_strdup (usercert ? usercert : ""));
    gchar *key = g_strstrip (g_strdup (userkey ? userkey : ""));

    nm_gpclient_editor_set_or_remove (s_vpn, "usercert", cert);
    nm_gpclient_editor_set_or_remove (s_vpn, "userkey", key);
    nm_gpclient_editor_save_password (s_vpn, "cert-pass", is_new, menu_flags, flags, *cert != '\0', pass);
    if (*cert == '\0')
        nm_setting_vpn_remove_secret (s_vpn, "cert-pass");
    g_free (cert);
    g_free (key);
}

/* A one-time code is never stored. The service sets this after the first
 * code too; a new connection starts with it. */
static inline void
nm_gpclient_editor_default_otp_flags (NMSettingVpn *s_vpn, gboolean is_new)
{
    if (is_new && !nm_setting_vpn_get_data_item (s_vpn, "otp-flags"))
        nm_setting_vpn_add_data_item (s_vpn, "otp-flags", "2");
}

/* Save the MTU: 0 = automatic (no key), below 576 is saved as 576. mtu_stored is
 * the raw text at load. When it is invalid, the spin button shows 0, and the
 * user did not touch it: keep the text instead of losing it. */
static inline void
nm_gpclient_editor_save_mtu (NMSettingVpn *s_vpn, int mtu, const char *mtu_stored, gboolean touched)
{
    if (!touched && nm_gpclient_mtu_parse (mtu_stored) < 0) {
        nm_setting_vpn_add_data_item (s_vpn, "mtu", mtu_stored);
    } else if (mtu <= 0) {
        nm_setting_vpn_remove_data_item (s_vpn, "mtu");
    } else {
        char *mtu_text = g_strdup_printf ("%d", CLAMP (mtu, MTU_MIN, MTU_MAX));

        nm_setting_vpn_add_data_item (s_vpn, "mtu", mtu_text);
        g_free (mtu_text);
    }
}

/* Save the legacy TLS workaround. Automatic stores nothing - the service reads
 * a missing key as "auto" and switches the profile to "true" once a portal
 * needs it. */
static inline void
nm_gpclient_editor_save_fix_openssl (NMSettingVpn *s_vpn, int index)
{
    switch (index) {
    case 1:
        nm_setting_vpn_add_data_item (s_vpn, "fix-openssl", "true");
        break;
    case 2:
        nm_setting_vpn_add_data_item (s_vpn, "fix-openssl", "false");
        break;
    default:
        nm_setting_vpn_remove_data_item (s_vpn, "fix-openssl");
        break;
    }
}

#endif /* NM_GPCLIENT_EDITOR_COMMON_H */

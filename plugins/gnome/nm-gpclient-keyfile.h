/* Import and export of a GlobalProtect profile as a NetworkManager keyfile
 * (.nmconnection), for the libnm plugin (nmcli, nm-connection-editor, the
 * GNOME settings) and the Plasma editor.
 *
 * Header only, plain C that also compiles as C++ (like nm-gpclient-validate.h).
 * Uses the keyfile functions of libnm (nm_keyfile_read, nm_keyfile_write: since
 * NetworkManager 1.30). tests/unit/test_keyfile_header.py checks the rules of
 * this file in the source (no compiler for libnm in the unit tests).
 *
 * A shared file must not carry a password: an export drops all secrets, and an
 * import drops the secrets of the file. An import gets a new UUID, so it never
 * replaces the profile that it came from. */

#ifndef NM_GPCLIENT_KEYFILE_H
#define NM_GPCLIENT_KEYFILE_H

#include <glib.h>
#include <NetworkManager.h>

#include "nm-gpclient-validate.h"

/* The Plasma editor has no gettext; the GNOME sources include glib/gi18n-lib.h first */
#ifndef _
#define _(text) text
#endif

#define NM_GPCLIENT_KEYFILE_SERVICE_TYPE "org.freedesktop.NetworkManager.gpclient"
#define NM_GPCLIENT_KEYFILE_EXTENSION ".nmconnection"

/* Read a profile from a keyfile. Returns a new connection (unref it), or NULL
 * and error. */
static inline NMConnection *
nm_gpclient_keyfile_import (const char *path, GError **error)
{
    GKeyFile *keyfile;
    char *base_dir;
    char *uuid;
    NMConnection *connection;
    NMSettingConnection *s_con;
    NMSettingVpn *s_vpn;
    const char *value;
    const char *problem;
    int hip_enabled = 1;

    keyfile = g_key_file_new ();
    if (!g_key_file_load_from_file (keyfile, path, G_KEY_FILE_NONE, error)) {
        g_key_file_unref (keyfile);
        return NULL;
    }

    base_dir = g_path_get_dirname (path);
    connection = nm_keyfile_read (keyfile, base_dir, NM_KEYFILE_HANDLER_FLAGS_NONE, NULL, NULL, error);
    g_free (base_dir);
    g_key_file_unref (keyfile);
    if (!connection)
        return NULL;

    s_con = nm_connection_get_setting_connection (connection);
    s_vpn = nm_connection_get_setting_vpn (connection);
    if (!s_con || !s_vpn
        || g_strcmp0 (nm_setting_vpn_get_service_type (s_vpn), NM_GPCLIENT_KEYFILE_SERVICE_TYPE) != 0) {
        g_set_error_literal (error, NM_CONNECTION_ERROR, NM_CONNECTION_ERROR_FAILED,
                             _("The file is not a GlobalProtect VPN connection"));
        g_object_unref (connection);
        return NULL;
    }

    /* The rules of the editors and the service */
    value = nm_setting_vpn_get_data_item (s_vpn, "hip");
    if (value && *value)
        hip_enabled = (g_ascii_strcasecmp (value, "true") == 0);
    problem = nm_gpclient_validate_form (nm_setting_vpn_get_data_item (s_vpn, "gateway"),
                                         nm_setting_vpn_get_data_item (s_vpn, "username"),
                                         nm_setting_vpn_get_data_item (s_vpn, "browser"),
                                         nm_setting_vpn_get_data_item (s_vpn, "dns"),
                                         hip_enabled,
                                         nm_setting_vpn_get_data_item (s_vpn, "hip-script"));
    if (problem) {
        g_set_error_literal (error, NM_CONNECTION_ERROR, NM_CONNECTION_ERROR_INVALID_PROPERTY,
                             _(problem));
        g_object_unref (connection);
        return NULL;
    }

    uuid = nm_utils_uuid_generate ();
    g_object_set (s_con, NM_SETTING_CONNECTION_UUID, uuid, NULL);
    g_free (uuid);

    nm_connection_clear_secrets (connection);

    if (!nm_connection_normalize (connection, NULL, NULL, error)) {
        g_object_unref (connection);
        return NULL;
    }
    return connection;
}

/* Write a profile as a keyfile (mode 0600) without its secrets. Returns TRUE
 * on success, FALSE and error otherwise. */
static inline gboolean
nm_gpclient_keyfile_export (NMConnection *connection, const char *path, GError **error)
{
    NMConnection *copy;
    GKeyFile *keyfile;
    char *data;
    gsize length = 0;
    gboolean ok;

    /* The copy loses the secrets, the connection of the caller keeps them */
    copy = nm_simple_connection_new_clone (connection);
    nm_connection_clear_secrets (copy);
    keyfile = nm_keyfile_write (copy, NM_KEYFILE_HANDLER_FLAGS_NONE, NULL, NULL, error);
    g_object_unref (copy);
    if (!keyfile)
        return FALSE;

    data = g_key_file_to_data (keyfile, &length, NULL);
    g_key_file_unref (keyfile);
    ok = g_file_set_contents_full (path, data, (gssize) length, G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
    g_free (data);
    return ok;
}

/* "<id>.nmconnection" with "/" and control characters of the id replaced by
 * "_". Free the result with g_free. */
static inline char *
nm_gpclient_keyfile_suggested_filename (const char *id)
{
    char *name = g_strdup (id && *id ? id : "gpclient");
    char *p;
    char *result;

    for (p = name; *p; p++)
        if (*p == '/' || nm_gpclient_validate_is_control (*p))
            *p = '_';
    result = g_strconcat (name, NM_GPCLIENT_KEYFILE_EXTENSION, NULL);
    g_free (name);
    return result;
}

#endif /* NM_GPCLIENT_KEYFILE_H */

/* NetworkManager gpclient VPN connection editor - GTK3 version */

#include <glib/gi18n-lib.h>
#include <gtk/gtk.h>
#include <NetworkManager.h>
#include <nm-vpn-editor-plugin.h>
#include <nma-ui-utils.h>

#include "nm-gpclient-editor.h"
#include "nm-gpclient-editor-common.h"
#include "nm-gpclient-mtu.h"
#include "nm-gpclient-validate.h"

/************** UI widget class **************/

struct _NMGpclientEditor {
    GObject parent;
    NMConnection *connection;
    GtkWidget *widget;
    GtkEntry *gateway_entry;
    GtkCheckButton *as_gateway_check;
    GtkComboBoxText *preferred_gateway_combo;
    GtkComboBoxText *auth_mode_combo;
    GtkEntry *username_entry;
    GtkWidget *password_label;
    GtkEntry *password_entry;
    gboolean is_new;  /* no vpn.data at load: first save adds the defaults */
    NMSettingSecretFlags password_menu_flags;  /* the storage menu at load */
    GtkComboBoxText *browser_combo;
    GtkSpinButton *timeout_spin;
    GtkSpinButton *mtu_spin;
    char *mtu_stored;  /* raw vpn.data mtu at load, NULL if none */
    gboolean mtu_touched;  /* the user changed the spin button after load */
    gboolean timeout_follows_auth_mode;
    GtkEntry *dns_entry;
    GtkCheckButton *hip_check;
    GtkWidget *hip_script_label;
    GtkEntry *hip_script_entry;
    GtkComboBoxText *fix_openssl_combo;
    GtkComboBoxText *saml_auth_combo;
    GtkEntry *usercert_entry;
    GtkEntry *userkey_entry;
    GtkEntry *cert_pass_entry;
    NMSettingSecretFlags cert_pass_menu_flags;  /* the storage menu at load */
};

static void nm_gpclient_editor_interface_init (NMVpnEditorInterface *iface_class);

G_DEFINE_TYPE_WITH_CODE (NMGpclientEditor, nm_gpclient_editor, G_TYPE_OBJECT,
                         G_IMPLEMENT_INTERFACE (NM_TYPE_VPN_EDITOR,
                                                nm_gpclient_editor_interface_init))

static void
stuff_changed_cb (GtkWidget *widget, gpointer user_data)
{
    g_signal_emit_by_name (G_OBJECT (user_data), "changed");
}

/* Client certificate and key fields: the "document-open" icon opens a file
 * chooser and the chosen local path goes into the entry (typing still works).
 * The title is passed as an N_() string. */
static void
choose_file_response_cb (GtkNativeDialog *dialog, gint response, gpointer user_data)
{
    if (response == GTK_RESPONSE_ACCEPT) {
        GFile *file = gtk_file_chooser_get_file (GTK_FILE_CHOOSER (dialog));
        char *path = file ? g_file_get_path (file) : NULL; /* NULL: not a local file */

        if (path)
            gtk_entry_set_text (GTK_ENTRY (user_data), path);
        g_free (path);
        g_clear_object (&file);
    }
    g_object_unref (dialog);
}

static void
choose_file_icon_press_cb (GtkEntry *entry, GtkEntryIconPosition pos, GdkEvent *event, gpointer title)
{
    GtkWidget *toplevel = gtk_widget_get_toplevel (GTK_WIDGET (entry));
    GtkFileChooserNative *dialog = gtk_file_chooser_native_new (
        _(title), GTK_IS_WINDOW (toplevel) ? GTK_WINDOW (toplevel) : NULL,
        GTK_FILE_CHOOSER_ACTION_OPEN, NULL, NULL);
    const char *current = gtk_entry_get_text (entry);

    if (g_path_is_absolute (current)) {
        char *dir = g_path_get_dirname (current);

        gtk_file_chooser_set_current_folder (GTK_FILE_CHOOSER (dialog), dir);
        g_free (dir);
    }
    g_signal_connect_object (dialog, "response", G_CALLBACK (choose_file_response_cb), entry, 0);
    gtk_native_dialog_show (GTK_NATIVE_DIALOG (dialog));
}

static void
add_choose_file_icon (GtkEntry *entry, const char *title)
{
    gtk_entry_set_icon_from_icon_name (entry, GTK_ENTRY_ICON_SECONDARY, "document-open");
    gtk_entry_set_icon_tooltip_text (entry, GTK_ENTRY_ICON_SECONDARY, _("Choose a file"));
    gtk_entry_set_icon_activatable (entry, GTK_ENTRY_ICON_SECONDARY, TRUE);
    g_signal_connect (entry, "icon-press", G_CALLBACK (choose_file_icon_press_cb), (gpointer) title);
}

static void
hip_toggled_cb (GtkWidget *widget, gpointer user_data)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (user_data);
    gboolean active = gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (widget));

    /* The HIP script only matters while HIP is on */
    gtk_widget_set_sensitive (self->hip_script_label, active);
    gtk_widget_set_sensitive (GTK_WIDGET (self->hip_script_entry), active);
    g_signal_emit_by_name (G_OBJECT (self), "changed");
}

static void
browser_combo_changed_cb (GtkComboBox *combo, gpointer user_data)
{
    const gchar *text = gtk_combo_box_text_get_active_text (GTK_COMBO_BOX_TEXT (combo));

    if (text && g_strcmp0(text, _("Custom...")) == 0) {
        GtkWidget *entry = gtk_bin_get_child (GTK_BIN (combo));
        gtk_entry_set_text (GTK_ENTRY (entry), "");
        gtk_widget_grab_focus (entry);
    }

    g_free ((gchar *)text);
    g_signal_emit_by_name (G_OBJECT (user_data), "changed");
}

static void
timeout_changed_cb (gpointer widget, gpointer user_data)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (user_data);

    /* The user set a value (+/-, typing): keep it whatever the authentication
     * mode. Connected only to the text "changed" of the spin button: every
     * user change (typing, +/-, wheel, keys) changes the text, so there is one
     * signal per change. GNOME Settings enables Apply only on the editor's
     * "changed". auth_mode_changed_cb blocks this handler for its own changes. */
    self->timeout_follows_auth_mode = FALSE;
    g_signal_emit_by_name (G_OBJECT (self), "changed");
}

/* The value the user sees. Typed text is committed to the adjustment only on
 * focus-out or Enter, so read the text (parsed in the common header). This
 * never changes the widget. */
static guint
timeout_from_spin (GtkSpinButton *spin)
{
    const char *text = gtk_entry_get_text (GTK_ENTRY (spin));
    GtkAdjustment *adj = gtk_spin_button_get_adjustment (spin);

    return nm_gpclient_editor_parse_timeout (text, gtk_adjustment_get_lower (adj),
                                             gtk_adjustment_get_upper (adj));
}

static void
mtu_changed_cb (GtkSpinButton *spin, gpointer user_data)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (user_data);

    /* Set after load: the value was set before this handler was connected */
    self->mtu_touched = TRUE;
    g_signal_emit_by_name (G_OBJECT (self), "changed");
}

static void
auth_mode_changed_cb (GtkComboBox *combo, gpointer user_data)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (user_data);
    gboolean credentials = gtk_combo_box_get_active (combo) == 1;

    /* The password is used only by "Username and password" */
    gtk_widget_set_visible (self->password_label, credentials);
    gtk_widget_set_visible (GTK_WIDGET (self->password_entry), credentials);

    if (self->timeout_follows_auth_mode && self->timeout_spin) {
        g_signal_handlers_block_by_func (self->timeout_spin, timeout_changed_cb, self);
        gtk_spin_button_set_value (self->timeout_spin,
                                   gtk_combo_box_get_active (combo) == 0 ? SAML_TIMEOUT_DEFAULT : 0);
        g_signal_handlers_unblock_by_func (self->timeout_spin, timeout_changed_cb, self);
    }
    g_signal_emit_by_name (G_OBJECT (self), "changed");
}

static GtkWidget *
build_ui (NMGpclientEditor *self)
{
    NMSettingVpn *s_vpn;
    GtkWidget *widget, *label, *timeout_box, *timeout_hint, *mtu_box, *mtu_hint;
    const char *value;
    gboolean is_new;
    guint stored;
    int auth_index;
    int shown;
    int row = 0;

    s_vpn = nm_connection_get_setting_vpn (self->connection);
    is_new = nm_gpclient_editor_is_new (s_vpn);
    self->is_new = is_new;
    stored = s_vpn ? nm_setting_vpn_get_timeout (s_vpn) : 0;

    widget = gtk_grid_new ();
    gtk_grid_set_column_spacing (GTK_GRID (widget), 12);
    gtk_grid_set_row_spacing (GTK_GRID (widget), 6);

    /* Portal / gateway address */
    label = gtk_label_new (_("Portal or gateway address:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->gateway_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->gateway_entry), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->gateway_entry),
        _("Address of the GlobalProtect portal (e.g. vpn.company.com).\n"
        "If your organisation gave you a gateway address instead, also tick "
        "\"Address is a gateway\" below."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "gateway");
        if (value)
            gtk_entry_set_text (self->gateway_entry, value);
    }
    g_signal_connect (G_OBJECT (self->gateway_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->gateway_entry), 1, row++, 1, 1);

    /* Address is a gateway, not a portal */
    self->as_gateway_check = GTK_CHECK_BUTTON (
        gtk_check_button_new_with_label (_("Address is a gateway (skip the portal)")));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->as_gateway_check),
        _("Enable when the address above is a gateway rather than a portal.\n"
        "Without this the portal workflow is tried first, which can make you "
        "authenticate twice."));
    if (s_vpn)
        gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (self->as_gateway_check),
                                      nm_gpclient_editor_data_is_true (s_vpn, "as-gateway"));
    g_signal_connect (G_OBJECT (self->as_gateway_check), "toggled", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->as_gateway_check), 1, row++, 1, 1);

    /* Preferred gateway */
    label = gtk_label_new (_("Preferred gateway:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->preferred_gateway_combo = GTK_COMBO_BOX_TEXT (gtk_combo_box_text_new_with_entry ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->preferred_gateway_combo), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->preferred_gateway_combo),
        _("Which gateway to connect to. The list is filled in after the first "
        "successful connection.\n"
        "If the chosen gateway is not offered by the portal any more, the first "
        "proposal is used instead."));
    gtk_combo_box_text_append_text (self->preferred_gateway_combo, GATEWAY_AUTO_LABEL);

    if (s_vpn) {
        const char *cached = nm_setting_vpn_get_data_item (s_vpn, "gateway-list");
        if (cached && *cached) {
            gchar **entries = g_strsplit (cached, ";", -1);
            for (int i = 0; entries[i] != NULL; i++) {
                gchar *entry = g_strstrip (entries[i]);
                if (*entry)
                    gtk_combo_box_text_append_text (self->preferred_gateway_combo, entry);
            }
            g_strfreev (entries);
        }

        value = nm_setting_vpn_get_data_item (s_vpn, "preferred-gateway");
        if (value && *value) {
            GtkWidget *entry = gtk_bin_get_child (GTK_BIN (self->preferred_gateway_combo));
            gtk_entry_set_text (GTK_ENTRY (entry), value);
        } else {
            gtk_combo_box_set_active (GTK_COMBO_BOX (self->preferred_gateway_combo), 0);
        }
    } else {
        gtk_combo_box_set_active (GTK_COMBO_BOX (self->preferred_gateway_combo), 0);
    }
    g_signal_connect (G_OBJECT (self->preferred_gateway_combo), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->preferred_gateway_combo), 1, row++, 1, 1);

    /* Authentication mode */
    label = gtk_label_new (_("Authentication:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->auth_mode_combo = GTK_COMBO_BOX_TEXT (gtk_combo_box_text_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->auth_mode_combo), TRUE);
    gtk_combo_box_text_append_text (self->auth_mode_combo, _("Browser (SAML, passkey, 2FA)"));
    gtk_combo_box_text_append_text (self->auth_mode_combo, _("Username and password (RSA token)"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->auth_mode_combo),
        _("Browser: the portal opens a browser window for single sign-on (default).\n"
        "Username and password: the portal asks on the terminal - the credentials "
        "and any token are requested in a dialog instead."));
    gtk_combo_box_set_active (GTK_COMBO_BOX (self->auth_mode_combo),
                              nm_gpclient_editor_auth_index (s_vpn));
    auth_index = gtk_combo_box_get_active (GTK_COMBO_BOX (self->auth_mode_combo));
    g_signal_connect (G_OBJECT (self->auth_mode_combo), "changed", G_CALLBACK (auth_mode_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->auth_mode_combo), 1, row++, 1, 1);

    /* Username */
    label = gtk_label_new (_("Username:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->username_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->username_entry), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->username_entry),
        _("Optional. Used by portals that ask for credentials on the terminal, so "
        "you are not asked for the username every time."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "username");
        if (value)
            gtk_entry_set_text (self->username_entry, value);
    }
    g_signal_connect (G_OBJECT (self->username_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->username_entry), 1, row++, 1, 1);

    /* Password, stored as a secret of the connection: only for "Username and
     * password". A new connection asks the secret agent (password-flags=1); an
     * existing one keeps its flags (a missing key is 0, system-owned). */
    self->password_label = gtk_label_new (_("Password:"));
    gtk_widget_set_halign (self->password_label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), self->password_label, 0, row, 1, 1);

    self->password_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_entry_set_visibility (self->password_entry, FALSE);
    gtk_widget_set_hexpand (GTK_WIDGET (self->password_entry), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->password_entry),
        _("Optional. Use the icon in the field to choose where the password is "
        "kept: for this user only, for all users, or not at all (asked every time)."));
    if (s_vpn) {
        value = nm_setting_vpn_get_secret (s_vpn, "password");
        if (value && *value)
            gtk_entry_set_text (self->password_entry, value);
    }
    {
        NMSettingSecretFlags flags = nm_gpclient_editor_password_flags_initial (s_vpn, "password", is_new);

        /* No setting: libnma would read the flags from it (none on a new
         * connection, so 0) and ignore these; update_connection() saves them */
        nma_utils_setup_password_storage (GTK_WIDGET (self->password_entry), flags, NULL, NULL, FALSE, FALSE);
        self->password_menu_flags = nma_utils_menu_to_secret_flags (GTK_WIDGET (self->password_entry));
    }
    g_signal_connect (G_OBJECT (self->password_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->password_entry), 1, row++, 1, 1);
    /* gtk_widget_show_all() below must not show them in browser mode */
    gtk_widget_set_no_show_all (self->password_label, TRUE);
    gtk_widget_set_no_show_all (GTK_WIDGET (self->password_entry), TRUE);
    gtk_widget_set_visible (self->password_label, auth_index == 1);
    gtk_widget_set_visible (GTK_WIDGET (self->password_entry), auth_index == 1);

    /* Browser */
    label = gtk_label_new (_("Browser:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->browser_combo = GTK_COMBO_BOX_TEXT (gtk_combo_box_text_new_with_entry ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->browser_combo), TRUE);

    for (int i = 0; browser_values[i] != NULL; i++)
        gtk_combo_box_text_append_text (self->browser_combo, browser_values[i]);

    gtk_combo_box_text_append_text (self->browser_combo, _("Custom..."));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->browser_combo),
        _("Browser used for SAML/2FA authentication. \"edge\" is the best tested "
        "option; \"default\" uses your desktop's default browser.\n"
        "Select 'Custom...' to give the full path to your own executable."));

    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "browser");
        if (value && *value) {
            GtkTreeModel *model = gtk_combo_box_get_model (GTK_COMBO_BOX (self->browser_combo));
            GtkTreeIter iter;
            gboolean found = FALSE;

            if (gtk_tree_model_get_iter_first (model, &iter)) {
                int index = 0;
                do {
                    gchar *item_text;
                    gtk_tree_model_get (model, &iter, 0, &item_text, -1);
                    if (g_strcmp0 (value, item_text) == 0) {
                        gtk_combo_box_set_active (GTK_COMBO_BOX (self->browser_combo), index);
                        found = TRUE;
                        g_free (item_text);
                        break;
                    }
                    g_free (item_text);
                    index++;
                } while (gtk_tree_model_iter_next (model, &iter));
            }

            if (!found) {
                /* An older profile with a full path - keep it as typed */
                GtkWidget *entry = gtk_bin_get_child (GTK_BIN (self->browser_combo));
                gtk_entry_set_text (GTK_ENTRY (entry), value);
            }
        } else {
            gtk_combo_box_set_active (GTK_COMBO_BOX (self->browser_combo), 0);
        }
    } else {
        gtk_combo_box_set_active (GTK_COMBO_BOX (self->browser_combo), 0);
    }
    g_signal_connect (G_OBJECT (self->browser_combo), "changed", G_CALLBACK (browser_combo_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->browser_combo), 1, row++, 1, 1);

    /* Where the SAML login runs (vpn.data saml-auth) */
    label = gtk_label_new (_("SAML login:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->saml_auth_combo = GTK_COMBO_BOX_TEXT (gtk_combo_box_text_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->saml_auth_combo), TRUE);
    gtk_combo_box_text_append_text (self->saml_auth_combo, _("In the service (default)"));
    gtk_combo_box_text_append_text (self->saml_auth_combo, _("In the login dialog"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->saml_auth_combo),
        _("Where the browser login runs: in the service, or in the login dialog (in your session)."));
    gtk_combo_box_set_active (GTK_COMBO_BOX (self->saml_auth_combo),
                              nm_gpclient_editor_saml_auth_index (s_vpn));
    g_signal_connect (G_OBJECT (self->saml_auth_combo), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->saml_auth_combo), 1, row++, 1, 1);

    /* Connect timeout (NetworkManager's vpn.timeout, not a vpn.data item) */
    label = gtk_label_new (_("Connect timeout:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    timeout_box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 6);
    self->timeout_spin = GTK_SPIN_BUTTON (gtk_spin_button_new_with_range (0, G_MAXUINT32, 10));
    gtk_spin_button_set_digits (self->timeout_spin, 0);
    gtk_spin_button_set_numeric (self->timeout_spin, TRUE);
    gtk_widget_set_hexpand (GTK_WIDGET (self->timeout_spin), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->timeout_spin),
        _("How long NetworkManager waits for the VPN to connect, including the "
        "login in the browser (vpn.timeout).\n"
        "0 uses the NetworkManager default, usually 60 s - often too short for a "
        "SAML login with MFA. A new connection with browser authentication starts "
        "with 360 s. While a connection has no timeout of its own (0) and you have "
        "not changed this field, choosing browser authentication sets 360 s."));
    /* A new profile starts with 360 s for browser authentication. A stored 0
     * (new or existing) follows the authentication mode until the user sets a
     * value; an existing profile still opens with its 0. */
    gtk_spin_button_set_value (self->timeout_spin,
                               nm_gpclient_editor_timeout_initial (is_new, stored, auth_index));
    self->timeout_follows_auth_mode = (stored == 0);
    /* Connect after the initial value is set */
    g_signal_connect (G_OBJECT (self->timeout_spin), "changed", G_CALLBACK (timeout_changed_cb), self);
    gtk_box_pack_start (GTK_BOX (timeout_box), GTK_WIDGET (self->timeout_spin), TRUE, TRUE, 0);
    timeout_hint = gtk_label_new (_("seconds (0 = NetworkManager default)"));
    gtk_box_pack_start (GTK_BOX (timeout_box), timeout_hint, FALSE, FALSE, 0);
    gtk_grid_attach (GTK_GRID (widget), timeout_box, 1, row++, 1, 1);

    /* Tunnel MTU (vpn.data "mtu") */
    label = gtk_label_new (_("MTU:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    mtu_box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 6);
    self->mtu_spin = GTK_SPIN_BUTTON (gtk_spin_button_new_with_range (0, MTU_MAX, 1));
    gtk_spin_button_set_digits (self->mtu_spin, 0);
    gtk_spin_button_set_numeric (self->mtu_spin, TRUE);
    gtk_widget_set_hexpand (GTK_WIDGET (self->mtu_spin), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->mtu_spin),
        _("Maximum size of a packet in the tunnel.\n"
        "0 = Automatic. Lowering it (for example to 1300-1400) may help when the "
        "tunnel falls back from ESP to HTTPS with \"Message too long\".\n"
        "It has an effect only when the gateway sends no MTU of its own. "
        "Values from 1 to 575 are saved as 576, the lowest value that is used."));
    gtk_widget_set_name (GTK_WIDGET (self->mtu_spin), "mtu_spin");
    g_free (self->mtu_stored);
    self->mtu_stored = g_strdup (s_vpn ? nm_setting_vpn_get_data_item (s_vpn, "mtu") : NULL);
    self->mtu_touched = FALSE;
    shown = nm_gpclient_mtu_parse (self->mtu_stored);
    gtk_spin_button_set_value (self->mtu_spin, shown > 0 ? shown : 0);
    g_signal_connect (G_OBJECT (self->mtu_spin), "value-changed", G_CALLBACK (mtu_changed_cb), self);
    gtk_box_pack_start (GTK_BOX (mtu_box), GTK_WIDGET (self->mtu_spin), TRUE, TRUE, 0);
    mtu_hint = gtk_label_new (_("bytes (0 = Automatic)"));
    gtk_box_pack_start (GTK_BOX (mtu_box), mtu_hint, FALSE, FALSE, 0);
    gtk_grid_attach (GTK_GRID (widget), mtu_box, 1, row++, 1, 1);

    /* Legacy TLS renegotiation workaround */
    label = gtk_label_new (_("Legacy TLS renegotiation:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->fix_openssl_combo = GTK_COMBO_BOX_TEXT (gtk_combo_box_text_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->fix_openssl_combo), TRUE);
    gtk_combo_box_text_append_text (self->fix_openssl_combo, _("Automatic (enable when the portal needs it)"));
    gtk_combo_box_text_append_text (self->fix_openssl_combo, _("Always on"));
    gtk_combo_box_text_append_text (self->fix_openssl_combo, _("Never"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->fix_openssl_combo),
        _("Portals with an old TLS stack need renegotiation that OpenSSL 3 refuses "
        "by default.\n"
        "Automatic: retry once with the workaround when the portal asks for it, "
        "and switch this setting to \"Always on\" afterwards, so the failed first "
        "attempt does not repeat."));
    gtk_combo_box_set_active (GTK_COMBO_BOX (self->fix_openssl_combo),
                              nm_gpclient_editor_fix_openssl_index (s_vpn));
    g_signal_connect (G_OBJECT (self->fix_openssl_combo), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->fix_openssl_combo), 1, row++, 1, 1);

    /* DNS */
    label = gtk_label_new (_("DNS Servers:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->dns_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->dns_entry), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->dns_entry),
        _("Recommended: Leave empty to use DNS servers from VPN with automatic split DNS configuration.\n"
        "Split DNS allows resolving VPN-specific domains through VPN DNS while using local DNS for other domains.\n"
        "Override: Enter semicolon-separated DNS servers (e.g., 8.8.8.8;8.8.4.4) to use custom DNS instead."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "dns");
        if (value)
            gtk_entry_set_text (self->dns_entry, value);
    }
    g_signal_connect (G_OBJECT (self->dns_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->dns_entry), 1, row++, 1, 1);

    /* HIP */
    label = gtk_label_new (_("HIP:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->hip_check = GTK_CHECK_BUTTON (gtk_check_button_new_with_label (_("Enable Host Integrity Protection (HIP)")));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->hip_check),
        _("Enable HIP (Host Integrity Protection) to send host information to the VPN gateway.\n"
        "This is required by some organizations for security compliance."));
    /* Default: enabled */
    gboolean hip_enabled = nm_gpclient_editor_hip_enabled (s_vpn);
    gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (self->hip_check), hip_enabled);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->hip_check), 1, row++, 1, 1);

    /* HIP script */
    self->hip_script_label = gtk_label_new (_("HIP script:"));
    gtk_widget_set_halign (self->hip_script_label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), self->hip_script_label, 0, row, 1, 1);

    self->hip_script_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->hip_script_entry), TRUE);
    gtk_entry_set_placeholder_text (self->hip_script_entry, _("Empty: built-in HIP report"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->hip_script_entry),
        _("Optional absolute path to a custom HIP executable, passed to gpclient as --hip=<path>.\n"
        "The service runs it as root: it must be a root-owned file that is not writable by group or others, "
        "in root-owned directories that are not writable by group or others. Otherwise the connection fails.\n"
        "Ignored when HIP is off."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "hip-script");
        if (value && *value)
            gtk_entry_set_text (self->hip_script_entry, value);
    }
    gtk_widget_set_sensitive (self->hip_script_label, hip_enabled);
    gtk_widget_set_sensitive (GTK_WIDGET (self->hip_script_entry), hip_enabled);
    g_signal_connect (G_OBJECT (self->hip_script_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    g_signal_connect (G_OBJECT (self->hip_check), "toggled", G_CALLBACK (hip_toggled_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->hip_script_entry), 1, row++, 1, 1);

    /* Client certificate (issue #134): paths as typed (gpclient reads the
     * files), and the passphrase of the key as a secret with the same storage
     * menu as the password */
    label = gtk_label_new (_("Client certificate:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->usercert_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->usercert_entry), TRUE);
    gtk_entry_set_placeholder_text (self->usercert_entry, _("Empty: no client certificate"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->usercert_entry),
        _("Optional absolute path to the client certificate file (PEM or PKCS#12), owned by your desktop user."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "usercert");
        if (value && *value)
            gtk_entry_set_text (self->usercert_entry, value);
    }
    add_choose_file_icon (self->usercert_entry, N_("Choose the client certificate"));
    g_signal_connect (G_OBJECT (self->usercert_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->usercert_entry), 1, row++, 1, 1);

    label = gtk_label_new (_("Private key:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->userkey_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->userkey_entry), TRUE);
    gtk_entry_set_placeholder_text (self->userkey_entry, _("Empty: the key is in the certificate file"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->userkey_entry),
        _("Optional absolute path to the private key file (PEM), owned by your desktop user. Ignored without a client certificate."));
    if (s_vpn) {
        value = nm_setting_vpn_get_data_item (s_vpn, "userkey");
        if (value && *value)
            gtk_entry_set_text (self->userkey_entry, value);
    }
    add_choose_file_icon (self->userkey_entry, N_("Choose the private key"));
    g_signal_connect (G_OBJECT (self->userkey_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->userkey_entry), 1, row++, 1, 1);

    label = gtk_label_new (_("Certificate passphrase:"));
    gtk_widget_set_halign (label, GTK_ALIGN_START);
    gtk_grid_attach (GTK_GRID (widget), label, 0, row, 1, 1);

    self->cert_pass_entry = GTK_ENTRY (gtk_entry_new ());
    gtk_entry_set_visibility (self->cert_pass_entry, FALSE);
    gtk_widget_set_hexpand (GTK_WIDGET (self->cert_pass_entry), TRUE);
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->cert_pass_entry),
        _("Optional. Use the icon in the field to choose where the password is kept: for this user only, for all users, or not at all (asked every time)."));
    if (s_vpn) {
        value = nm_setting_vpn_get_secret (s_vpn, "cert-pass");
        if (value && *value)
            gtk_entry_set_text (self->cert_pass_entry, value);
    }
    {
        NMSettingSecretFlags flags = nm_gpclient_editor_password_flags_initial (s_vpn, "cert-pass", is_new);

        nma_utils_setup_password_storage (GTK_WIDGET (self->cert_pass_entry), flags, NULL, NULL, FALSE, FALSE);
        self->cert_pass_menu_flags = nma_utils_menu_to_secret_flags (GTK_WIDGET (self->cert_pass_entry));
    }
    g_signal_connect (G_OBJECT (self->cert_pass_entry), "changed", G_CALLBACK (stuff_changed_cb), self);
    gtk_grid_attach (GTK_GRID (widget), GTK_WIDGET (self->cert_pass_entry), 1, row++, 1, 1);

    gtk_widget_show_all (widget);

    return widget;
}

static gboolean
update_connection (NMVpnEditor *iface,
                  NMConnection *connection,
                  GError **error)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (iface);
    NMSettingVpn *s_vpn;
    const char *str;
    gchar *gateway = NULL;

    /* Check the fields with the rules shared with the GTK4 and Plasma editors;
     * an error greys out Save */
    {
        char *browser = self->browser_combo ? gtk_combo_box_text_get_active_text (self->browser_combo) : NULL;
        const char *problem = nm_gpclient_validate_form (
            self->gateway_entry ? gtk_entry_get_text (self->gateway_entry) : NULL,
            self->username_entry ? gtk_entry_get_text (self->username_entry) : NULL,
            browser,
            self->dns_entry ? gtk_entry_get_text (self->dns_entry) : NULL,
            self->hip_check ? gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (self->hip_check)) : FALSE,
            self->hip_script_entry ? gtk_entry_get_text (self->hip_script_entry) : NULL,
            self->usercert_entry ? gtk_entry_get_text (self->usercert_entry) : NULL,
            self->userkey_entry ? gtk_entry_get_text (self->userkey_entry) : NULL);

        g_free (browser);
        if (problem) {
            if (error) {
                g_set_error_literal (error,
                                     NM_VPN_PLUGIN_ERROR,
                                     NM_VPN_PLUGIN_ERROR_BAD_ARGUMENTS,
                                     _(problem));
            }
            return FALSE;
        }
    }
    if (self->gateway_entry)
        gateway = g_strstrip (g_strdup (gtk_entry_get_text (self->gateway_entry)));

    s_vpn = nm_connection_get_setting_vpn (connection);
    if (!s_vpn) {
        s_vpn = (NMSettingVpn *) nm_setting_vpn_new ();
        nm_connection_add_setting (connection, NM_SETTING (s_vpn));
    }

    g_object_set (s_vpn, NM_SETTING_VPN_SERVICE_TYPE,
                  "org.freedesktop.NetworkManager.gpclient", NULL);

    if (gateway) {
        nm_setting_vpn_add_data_item (s_vpn, "gateway", gateway);
        g_free (gateway);
    }

    if (self->as_gateway_check)
        nm_gpclient_editor_set_or_remove (s_vpn, "as-gateway",
            gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (self->as_gateway_check)) ? "true" : NULL);

    if (self->preferred_gateway_combo) {
        str = gtk_combo_box_text_get_active_text (self->preferred_gateway_combo);
        /* The automatic entry means "let the portal decide" - store nothing */
        nm_gpclient_editor_set_or_remove (s_vpn, "preferred-gateway",
            g_strcmp0 (str, GATEWAY_AUTO_LABEL) != 0 ? str : NULL);
        g_free ((char *) str);
    }

    if (self->auth_mode_combo)
        nm_gpclient_editor_set_or_remove (s_vpn, "auth-mode",
            gtk_combo_box_get_active (GTK_COMBO_BOX (self->auth_mode_combo)) == 1 ? "credentials" : NULL);

    if (self->username_entry)
        nm_gpclient_editor_set_or_remove (s_vpn, "username",
                                          gtk_entry_get_text (self->username_entry));

    /* Save the password flags and the password (rules in the common header) */
    if (self->password_entry)
        nm_gpclient_editor_save_password (s_vpn, "password", self->is_new, self->password_menu_flags,
            nma_utils_menu_to_secret_flags (GTK_WIDGET (self->password_entry)),
            gtk_combo_box_get_active (GTK_COMBO_BOX (self->auth_mode_combo)) == 1,
            gtk_entry_get_text (self->password_entry));

    nm_gpclient_editor_default_otp_flags (s_vpn, self->is_new);

    if (self->browser_combo) {
        str = gtk_combo_box_text_get_active_text (self->browser_combo);
        nm_gpclient_editor_save_browser (s_vpn, str);
        g_free ((char *)str);
    }

    if (self->timeout_spin) {
        /* NetworkManager's own vpn.timeout; read what the user sees */
        g_object_set (s_vpn, NM_SETTING_VPN_TIMEOUT,
                      timeout_from_spin (self->timeout_spin),
                      NULL);
    }

    if (self->mtu_spin) {
        /* 0 = automatic (no key), below 576 is saved as 576; commit typed
         * text first */
        gtk_spin_button_update (self->mtu_spin);
        nm_gpclient_editor_save_mtu (s_vpn, gtk_spin_button_get_value_as_int (self->mtu_spin),
                                     self->mtu_stored, self->mtu_touched);
    }

    if (self->dns_entry)
        nm_gpclient_editor_set_or_remove (s_vpn, "dns", gtk_entry_get_text (self->dns_entry));

    if (self->fix_openssl_combo)
        nm_gpclient_editor_save_fix_openssl (s_vpn,
            gtk_combo_box_get_active (GTK_COMBO_BOX (self->fix_openssl_combo)));

    if (self->saml_auth_combo)
        nm_gpclient_editor_save_saml_auth (s_vpn,
            gtk_combo_box_get_active (GTK_COMBO_BOX (self->saml_auth_combo)));

    if (self->hip_check)
        nm_setting_vpn_add_data_item (s_vpn, "hip",
            gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (self->hip_check)) ? "true" : "false");

    if (self->hip_script_entry) {
        /* Saved even when HIP is off: the service ignores it then */
        gchar *script = g_strstrip (g_strdup (gtk_entry_get_text (self->hip_script_entry)));

        nm_gpclient_editor_set_or_remove (s_vpn, "hip-script", script);
        g_free (script);
    }

    if (self->usercert_entry && self->userkey_entry && self->cert_pass_entry)
        nm_gpclient_editor_save_cert (s_vpn,
            gtk_entry_get_text (self->usercert_entry),
            gtk_entry_get_text (self->userkey_entry),
            self->is_new, self->cert_pass_menu_flags,
            nma_utils_menu_to_secret_flags (GTK_WIDGET (self->cert_pass_entry)),
            gtk_entry_get_text (self->cert_pass_entry));

    return TRUE;
}

static GObject *
get_widget (NMVpnEditor *iface)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (iface);

    if (!self->widget) {
        self->widget = build_ui (self);
        g_object_ref_sink (self->widget);
    }

    return G_OBJECT (self->widget);
}

static void
dispose (GObject *object)
{
    NMGpclientEditor *self = NM_GPCLIENT_EDITOR (object);

    if (self->widget) {
        g_object_unref (self->widget);
        self->widget = NULL;
    }

    g_clear_pointer (&self->mtu_stored, g_free);

    if (self->connection) {
        g_object_unref (self->connection);
        self->connection = NULL;
    }

    G_OBJECT_CLASS (nm_gpclient_editor_parent_class)->dispose (object);
}

static void
nm_gpclient_editor_class_init (NMGpclientEditorClass *req_class)
{
    GObjectClass *object_class = G_OBJECT_CLASS (req_class);

    object_class->dispose = dispose;
}

static void
nm_gpclient_editor_interface_init (NMVpnEditorInterface *iface_class)
{
    iface_class->get_widget = get_widget;
    iface_class->update_connection = update_connection;
}

static void
nm_gpclient_editor_init (NMGpclientEditor *self)
{
    self->widget = NULL;
    self->connection = NULL;
    self->gateway_entry = NULL;
    self->as_gateway_check = NULL;
    self->preferred_gateway_combo = NULL;
    self->auth_mode_combo = NULL;
    self->username_entry = NULL;
    self->password_label = NULL;
    self->password_entry = NULL;
    self->browser_combo = NULL;
    self->timeout_spin = NULL;
    self->mtu_spin = NULL;
    self->timeout_follows_auth_mode = FALSE;
    self->dns_entry = NULL;
    self->hip_check = NULL;
    self->hip_script_label = NULL;
    self->hip_script_entry = NULL;
    self->fix_openssl_combo = NULL;
    self->saml_auth_combo = NULL;
    self->usercert_entry = NULL;
    self->userkey_entry = NULL;
    self->cert_pass_entry = NULL;
}

NMGpclientEditor *
nm_gpclient_editor_new (NMConnection *connection)
{
    NMGpclientEditor *self;

    g_return_val_if_fail (NM_IS_CONNECTION (connection), NULL);

    self = g_object_new (NM_TYPE_GPCLIENT_EDITOR, NULL);
    self->connection = g_object_ref (connection);

    return self;
}

/* Factory function called by nm_vpn_plugin_utils_load_editor() */
G_MODULE_EXPORT NMVpnEditor *
nm_vpn_editor_factory_gpclient (NMVpnEditorPlugin *editor_plugin,
                                NMConnection *connection,
                                GError **error)
{
    g_return_val_if_fail (!error || !*error, NULL);
    return NM_VPN_EDITOR (nm_gpclient_editor_new (connection));
}

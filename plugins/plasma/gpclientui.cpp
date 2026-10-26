/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#include "gpclientui.h"
#include "gpclientauthwidget.h"
#include "gpclientwidget.h"

#include <KLocalizedString>
#include <KPluginFactory>

#include <QFile>

// Last: it defines _() for its messages when no gettext header came first
#include "nm-gpclient-keyfile.h"

GpclientUiPlugin::GpclientUiPlugin(QObject *parent, const QVariantList &)
    : VpnUiPlugin(parent)
{
}

GpclientUiPlugin::~GpclientUiPlugin()
{
}

SettingWidget *GpclientUiPlugin::widget(const NetworkManager::VpnSetting::Ptr &setting, QWidget *parent)
{
    return new GpclientWidget(setting, parent);
}

SettingWidget *GpclientUiPlugin::askUser(const NetworkManager::VpnSetting::Ptr &setting, const QStringList &hints, QWidget *parent)
{
    return new GpclientAuthWidget(setting, hints, parent);
}

// Import and export of a profile as a NetworkManager keyfile (.nmconnection).
//
// Checked in the plasma-nm sources (tags v5.24.7, v5.27.11, v6.3.5): the
// network KCM (kcm/kcm.cpp, KCMNetworkmanagement::importVpn and exportVpn) asks
// the Plasma plugin itself (supportedFileExtensions, importConnectionSettings,
// suggestedFileName, exportConnectionSettings); it never uses the capabilities
// of the libnm plugin. So the Plasma plugin has to implement them, with the
// helper that the libnm plugin uses.
//
// plasma-nm before 5.27 (Ubuntu 22.04: 5.24) calls these four methods with
// other return types (QString, NMVariantMapMap, bool; libs/editor/vpnuiplugin.h).
// A plugin built with the 5.27 header cannot answer such calls, so there the
// plugin keeps an empty suggestedFileName: the KCM then reports "This VPN
// doesn't support export" and shows no import extension of ours.
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0) && \
    (!defined(NETWORKMANAGERQT_VERSION) || NETWORKMANAGERQT_VERSION < QT_VERSION_CHECK(5, 102, 0))
#define GPCLIENT_PLASMANM_OLD_API 1
#endif

#ifndef GPCLIENT_PLASMANM_OLD_API

QString GpclientUiPlugin::suggestedFileName(const NetworkManager::ConnectionSettings::Ptr &connection) const
{
    char *name = nm_gpclient_keyfile_suggested_filename(connection->id().toUtf8().constData());
    const QString result = QString::fromUtf8(name);
    g_free(name);
    return result;
}

QStringList GpclientUiPlugin::supportedFileExtensions() const
{
    return {QStringLiteral("*.nmconnection")};
}

VpnUiPlugin::ImportResult GpclientUiPlugin::importConnectionSettings(const QString &fileName)
{
    GError *error = nullptr;
    NMConnection *connection = nm_gpclient_keyfile_import(QFile::encodeName(fileName).constData(), &error);
    if (!connection) {
        const QString message = QString::fromUtf8(error->message);
        g_error_free(error);
        return VpnUiPlugin::ImportResult::fail(i18n("Cannot import the connection: %1", message));
    }
    return VpnUiPlugin::ImportResult::pass(connection);
}

VpnUiPlugin::ExportResult GpclientUiPlugin::exportConnectionSettings(const NetworkManager::ConnectionSettings::Ptr &connection, const QString &fileName)
{
    // The KCM exports a saved connection: take it from NetworkManager by its
    // UUID, so that all its settings (routes, IPv6, never-default, ...) go into
    // the file. NetworkManager hands out no secrets; the helper drops them anyway.
    GError *error = nullptr;
    QString message;
    NMClient *client = nm_client_new(nullptr, &error);
    if (client) {
        NMRemoteConnection *remote = nm_client_get_connection_by_uuid(client, connection->uuid().toUtf8().constData());
        if (!remote) {
            message = connection->id(); // not saved in NetworkManager: name the profile
        } else if (nm_gpclient_keyfile_export(NM_CONNECTION(remote), QFile::encodeName(fileName).constData(), &error)) {
            g_object_unref(client);
            return VpnUiPlugin::ExportResult::pass();
        }
        g_object_unref(client);
    }
    if (error) {
        message = QString::fromUtf8(error->message);
        g_error_free(error);
    }
    return VpnUiPlugin::ExportResult::fail(i18n("Cannot export the connection: %1", message));
}

#else // GPCLIENT_PLASMANM_OLD_API

QString GpclientUiPlugin::suggestedFileName(const NetworkManager::ConnectionSettings::Ptr &connection) const
{
    Q_UNUSED(connection);
    return QString();
}

QStringList GpclientUiPlugin::supportedFileExtensions() const
{
    return QStringList();
}

VpnUiPlugin::ImportResult GpclientUiPlugin::importConnectionSettings(const QString &fileName)
{
    Q_UNUSED(fileName);
    return VpnUiPlugin::ImportResult();
}

VpnUiPlugin::ExportResult GpclientUiPlugin::exportConnectionSettings(const NetworkManager::ConnectionSettings::Ptr &connection, const QString &fileName)
{
    Q_UNUSED(connection);
    Q_UNUSED(fileName);
    return VpnUiPlugin::ExportResult();
}

#endif // GPCLIENT_PLASMANM_OLD_API

K_PLUGIN_CLASS_WITH_JSON(GpclientUiPlugin, "plasmanetworkmanagement_gpclientui.json")

#include "gpclientui.moc"

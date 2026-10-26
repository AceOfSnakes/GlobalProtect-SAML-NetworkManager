/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#include "gpclientui.h"
#include "gpclientauthwidget.h"
#include "gpclientwidget.h"

#include "nm-gpclient-keyfile.h"

#include <KPluginFactory>
#include <NetworkManagerQt/Ipv4Setting>

#include <QFile>

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

namespace
{
// The profile as a libnm connection for the helper. Only what a profile of
// this plugin has: the connection and VPN settings and the IPv4 search domains.
// The secrets are not copied (the helper drops them anyway).
NMConnection *toNMConnection(const NetworkManager::ConnectionSettings::Ptr &connection)
{
    NMConnection *nm = nm_simple_connection_new();

    NMSetting *s_con = nm_setting_connection_new();
    g_object_set(s_con,
                 NM_SETTING_CONNECTION_ID, connection->id().toUtf8().constData(),
                 NM_SETTING_CONNECTION_UUID, connection->uuid().toUtf8().constData(),
                 NM_SETTING_CONNECTION_TYPE, NM_SETTING_VPN_SETTING_NAME,
                 nullptr);
    nm_connection_add_setting(nm, s_con);

    NMSetting *s_vpn = nm_setting_vpn_new();
    g_object_set(s_vpn, NM_SETTING_VPN_SERVICE_TYPE, NM_GPCLIENT_KEYFILE_SERVICE_TYPE, nullptr);
    const auto vpnSetting = connection->setting(NetworkManager::Setting::Vpn).dynamicCast<NetworkManager::VpnSetting>();
    if (vpnSetting) {
        const NMStringMap data = vpnSetting->data();
        for (auto it = data.constBegin(); it != data.constEnd(); ++it) {
            nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), it.key().toUtf8().constData(), it.value().toUtf8().constData());
        }
    }
    nm_connection_add_setting(nm, s_vpn);

    const auto ipv4Setting = connection->setting(NetworkManager::Setting::Ipv4).dynamicCast<NetworkManager::Ipv4Setting>();
    if (ipv4Setting && !ipv4Setting->dnsSearch().isEmpty()) {
        NMSetting *s_ip4 = nm_setting_ip4_config_new();
        g_object_set(s_ip4, NM_SETTING_IP_CONFIG_METHOD, NM_SETTING_IP4_CONFIG_METHOD_AUTO, nullptr);
        const QStringList search = ipv4Setting->dnsSearch();
        for (const QString &domain : search) {
            nm_setting_ip_config_add_dns_search(NM_SETTING_IP_CONFIG(s_ip4), domain.toUtf8().constData());
        }
        nm_connection_add_setting(nm, s_ip4);
    }
    return nm;
}
}

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
    GError *error = nullptr;
    NMConnection *nm = toNMConnection(connection);
    const bool ok = nm_gpclient_keyfile_export(nm, QFile::encodeName(fileName).constData(), &error);
    g_object_unref(nm);
    if (!ok) {
        const QString message = QString::fromUtf8(error->message);
        g_error_free(error);
        return VpnUiPlugin::ExportResult::fail(i18n("Cannot export the connection: %1", message));
    }
    return VpnUiPlugin::ExportResult::pass();
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

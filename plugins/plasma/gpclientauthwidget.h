/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#ifndef PLASMA_NM_GPCLIENT_AUTH_WIDGET_H
#define PLASMA_NM_GPCLIENT_AUTH_WIDGET_H

#include <NetworkManagerQt/VpnSetting>
#include <QHash>
#include <QStringList>
#include <QVariantMap>

#include <settingwidget.h>

class QLineEdit;
class QProcess;

class GpclientAuthWidget final : public SettingWidget
{
public:
    explicit GpclientAuthWidget(const NetworkManager::VpnSetting::Ptr &setting,
                                const QStringList &hints,
                                QWidget *parent = nullptr);

    ~GpclientAuthWidget() override;

    QVariantMap setting() const override;

private:
    /* saml-auth=dialog: plasma-nm shows this widget, not the GNOME auth dialog.
     * Run gpauth as the user (like auth-dialog/nm-gpclient-auth-dialog.py) and
     * close the password dialog with the result; nothing is asked. */
    void startSamlLogin(const QString &hint);
    void runGpauth(bool fixOpenssl);
    void finishSamlLogin(const QString &result);

    QProcess *m_gpauth = nullptr;
    QString m_samlServer;
    QString m_samlBrowser;
    QString m_samlBrowserTarget;
    QString m_samlFixOpenssl;
    bool m_samlGateway = false;
    bool m_samlRetried = false;
    QString m_samlResult;
    QStringList m_secretKeys;
    QHash<QString, QLineEdit *> m_fields;
};

#endif // PLASMA_NM_GPCLIENT_AUTH_WIDGET_H

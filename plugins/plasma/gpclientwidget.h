/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#ifndef PLASMA_NM_GPCLIENT_WIDGET_H
#define PLASMA_NM_GPCLIENT_WIDGET_H

#include <NetworkManagerQt/VpnSetting>
#include <settingwidget.h>

namespace Ui
{
class GpclientWidget;
}

class GpclientWidget : public SettingWidget
{
    Q_OBJECT
public:
    explicit GpclientWidget(const NetworkManager::VpnSetting::Ptr &setting, QWidget *parent = nullptr);
    ~GpclientWidget() override;

    void loadConfig(const NetworkManager::Setting::Ptr &setting) override;
    QVariantMap setting() const override;
    void loadSecrets(const NetworkManager::Setting::Ptr &setting) override;
    bool isValid() const override;

private:
    void setPasswordVisible(bool visible);
    Ui::GpclientWidget *m_ui;
    /* Everything the profile already had. setting() builds its map from this, so
     * keys this widget does not show (the cached gateway-list, dns-domains) are
     * not silently dropped on save. */
    NMStringMap m_originalData;
    /* vpn.persistent and vpn.user-name of the loaded setting; setting() builds a
     * new one and would reset them. Secrets are not copied. */
    bool m_persistent = false;
    QString m_userName;
    /* While the stored connect timeout is 0 (new or existing profile), the
     * timeout follows the authentication mode (360 s for the browser, 0
     * otherwise) until the user sets a value. */
    bool m_timeoutFollowsAuthMode = false;
    /* The user changed the MTU spin box after load: until then an invalid
     * stored value (the box shows 0) is kept on save. */
    bool m_mtuTouched = false;
    /* No vpn.data at load: the first save adds password-flags and otp-flags. */
    bool m_isNew = false;
    /* Index of the password storage combo at load: only a change writes
     * password-flags of an existing profile. */
    int m_passwordFlagsIndex = 1;
    /* The same for the passphrase of the client certificate (cert-pass-flags) */
    int m_certPassFlagsIndex = 1;
};

#endif // PLASMA_NM_GPCLIENT_WIDGET_H

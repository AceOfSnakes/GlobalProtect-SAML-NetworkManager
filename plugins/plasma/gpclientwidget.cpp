/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#include "gpclientwidget.h"
#include "ui_gpclientwidget.h"

// The MTU and field rules (plain C, shared with the GTK editors)
#include "nm-gpclient-mtu.h"
#include "nm-gpclient-validate.h"

#include <KLocalizedString>
#include <NetworkManagerQt/Utils>
#include <QCheckBox>
#include <QComboBox>
#include <QFileInfo>
#include <QLineEdit>
#include <QSignalBlocker>
#include <QDoubleSpinBox>
#include <QString>
#include <QStringList>

/* First entry of the preferred-gateway combo: let the portal decide. Stored as
 * no vpn.data value at all, so existing profiles keep behaving the same. */
static QString gatewayAutoLabel()
{
    return i18n("First proposed by portal (automatic)");
}

/* Last entry of the browser combo: the user types the path of a browser. */
static QString customBrowserLabel()
{
    return i18n("Custom...");
}

/* The browser to store: empty for the "Custom..." entry itself. In another
 * language that label is not a valid browser name ("Własna..."), so it must not
 * reach the validator either. */
static QString browserValue(const QString &text)
{
    const QString browser = text.trimmed();
    return browser == customBrowserLabel() ? QString() : browser;
}

/* NetworkManager stops a VPN activation after vpn.timeout seconds (0 = its
 * default, usually 60 s), which is too short for a SAML login with MFA. A new
 * connection with browser authentication starts with this value (issue #46).
 * It is longer than the limit of the browser login itself (GP_AUTH_TIMEOUT,
 * 300 s, scripts/browser-wrapper.sh) plus the time to connect after the login,
 * so that limit and its clearer error come first. */
static const int samlTimeoutDefault = 360;

/* The "Password storage" combo, as NMSettingSecretFlags: 1 = agent-owned
 * (this user only), 0 = system-owned (all users, in the profile), 2 = not saved
 * (asked every time). A new connection starts with the agent. */
static const uint passwordFlagsByIndex[] = {1, 0, 2};

/* Browser values understood by the service (see resolve_browser()) */
static const QStringList browserValues = {
    QStringLiteral("edge"),
    QStringLiteral("firefox"),
    QStringLiteral("chrome"),
    QStringLiteral("chromium"),
    QStringLiteral("default"),
};

GpclientWidget::GpclientWidget(const NetworkManager::VpnSetting::Ptr &setting, QWidget *parent)
    : SettingWidget(setting, parent)
    , m_ui(new Ui::GpclientWidget)
{
    m_ui->setupUi(this);

    m_ui->browserComboBox->addItems(browserValues);
    m_ui->browserComboBox->addItem(customBrowserLabel());

    m_ui->authModeComboBox->addItem(i18n("Browser (SAML, passkey, 2FA)"));
    m_ui->authModeComboBox->addItem(i18n("Username and password (RSA token)"));

    m_ui->preferredGatewayComboBox->addItem(gatewayAutoLabel());

    m_ui->passwordFlagsComboBox->addItem(i18n("Store for this user only (agent)"));
    m_ui->passwordFlagsComboBox->addItem(i18n("Store for all users (system)"));
    m_ui->passwordFlagsComboBox->addItem(i18n("Ask every time (not saved)"));

    m_ui->fixOpensslComboBox->addItem(i18n("Automatic (enable when the portal needs it)"));
    m_ui->fixOpensslComboBox->addItem(i18n("Always on"));
    m_ui->fixOpensslComboBox->addItem(i18n("Never"));

    // vpn.timeout is a guint32, so the field is a QDoubleSpinBox without decimals
    // (a QSpinBox stops at INT_MAX).

    // Load configuration if available
    loadConfig(setting);

    // Connect signals for change notification
    connect(m_ui->gatewayLineEdit, &QLineEdit::textChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->asGatewayCheckBox, &QCheckBox::toggled, this, &GpclientWidget::settingChanged);
    connect(m_ui->preferredGatewayComboBox, &QComboBox::currentTextChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->authModeComboBox, &QComboBox::currentTextChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->authModeComboBox, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int index) {
        // The password is used only by "Username and password"
        setPasswordVisible(index == 1);
        if (m_timeoutFollowsAuthMode) {
            // Keep following: only the user's own choice stops it
            const QSignalBlocker blocker(m_ui->timeoutSpinBox);
            m_ui->timeoutSpinBox->setValue(index == 0 ? static_cast<double>(samlTimeoutDefault) : 0.0);
        }
    });
    connect(m_ui->timeoutSpinBox, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this]() {
        // The user chose a value: keep it whatever the authentication mode.
        m_timeoutFollowsAuthMode = false;
        // plasma-nm 5 watches only QSpinBox in watchChangedSetting(), so a
        // QDoubleSpinBox needs our own emit there (checked by the timeout test).
        // plasma-nm 6 also watches QDoubleSpinBox, so there settingChanged comes
        // twice, which is harmless.
        Q_EMIT settingChanged();
    });
    connect(m_ui->mtuSpinBox, QOverload<int>::of(&QSpinBox::valueChanged), this, [this]() {
        // loadConfig() blocks this signal while it sets the value
        m_mtuTouched = true;
        Q_EMIT settingChanged();
    });
    connect(m_ui->usernameLineEdit, &QLineEdit::textChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->passwordLineEdit, &QLineEdit::textChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->passwordFlagsComboBox, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &GpclientWidget::settingChanged);
    // "Ask every time" stores nothing: no typing into the field (as in GNOME)
    connect(m_ui->passwordFlagsComboBox, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int index) {
        m_ui->passwordLineEdit->setEnabled(index != 2);
        if (index == 2) {
            m_ui->passwordLineEdit->clear();
        }
    });
    connect(m_ui->browserComboBox, &QComboBox::currentTextChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->dnsLineEdit, &QLineEdit::textChanged, this, &GpclientWidget::settingChanged);
    connect(m_ui->hipCheckBox, &QCheckBox::toggled, this, &GpclientWidget::settingChanged);
    connect(m_ui->hipScriptLineEdit, &QLineEdit::textChanged, this, &GpclientWidget::settingChanged);
    // The HIP script only matters while HIP is on
    connect(m_ui->hipCheckBox, &QCheckBox::toggled, m_ui->hipScriptLabel, &QWidget::setEnabled);
    connect(m_ui->hipCheckBox, &QCheckBox::toggled, m_ui->hipScriptLineEdit, &QWidget::setEnabled);
    connect(m_ui->fixOpensslComboBox, &QComboBox::currentTextChanged, this, &GpclientWidget::settingChanged);

    auto update_valid = [this]() {
        Q_EMIT validChanged(isValid());
    };
    connect(m_ui->gatewayLineEdit, &QLineEdit::textChanged, this, update_valid);
    connect(m_ui->usernameLineEdit, &QLineEdit::textChanged, this, update_valid);
    connect(m_ui->browserComboBox, &QComboBox::currentTextChanged, this, update_valid);
    connect(m_ui->dnsLineEdit, &QLineEdit::textChanged, this, update_valid);
    connect(m_ui->hipCheckBox, &QCheckBox::toggled, this, update_valid);
    connect(m_ui->hipScriptLineEdit, &QLineEdit::textChanged, this, update_valid);

    watchChangedSetting();
    update_valid();
}

GpclientWidget::~GpclientWidget()
{
    delete m_ui;
}

void GpclientWidget::setPasswordVisible(bool visible)
{
    m_ui->passwordLabel->setVisible(visible);
    m_ui->passwordLineEdit->setVisible(visible);
    m_ui->passwordFlagsLabel->setVisible(visible);
    m_ui->passwordFlagsComboBox->setVisible(visible);
}

void GpclientWidget::loadSecrets(const NetworkManager::Setting::Ptr &setting)
{
    const NetworkManager::VpnSetting::Ptr vpnSetting = setting.staticCast<NetworkManager::VpnSetting>();

    if (vpnSetting) {
        m_ui->passwordLineEdit->setText(vpnSetting->secrets().value(QLatin1String("password")));
    }
}

void GpclientWidget::loadConfig(const NetworkManager::Setting::Ptr &setting)
{
    NetworkManager::VpnSetting::Ptr vpnSetting = setting.staticCast<NetworkManager::VpnSetting>();

    if (vpnSetting) {
        const NMStringMap data = vpnSetting->data();
        m_originalData = data;
        // toMap() always writes these, so a fresh setting() would reset them on save
        m_persistent = vpnSetting->persistent();
        m_userName = vpnSetting->username();

        // Load the portal / gateway address
        if (data.contains(QLatin1String("gateway"))) {
            m_ui->gatewayLineEdit->setText(data.value(QLatin1String("gateway")));
        }

        // The address is a gateway, not a portal
        m_ui->asGatewayCheckBox->setChecked(
            data.value(QLatin1String("as-gateway")).toLower() == QLatin1String("true"));

        // Offer the gateways discovered during the last successful connection
        const QString cached = data.value(QLatin1String("gateway-list"));
        if (!cached.isEmpty()) {
            const QStringList gateways = cached.split(QLatin1Char(';'), Qt::SkipEmptyParts);
            for (const QString &gateway : gateways) {
                const QString entry = gateway.trimmed();
                if (!entry.isEmpty()) {
                    m_ui->preferredGatewayComboBox->addItem(entry);
                }
            }
        }

        const QString preferred = data.value(QLatin1String("preferred-gateway"));
        if (preferred.isEmpty()) {
            m_ui->preferredGatewayComboBox->setCurrentIndex(0);
        } else {
            const int index = m_ui->preferredGatewayComboBox->findText(preferred);
            if (index >= 0) {
                m_ui->preferredGatewayComboBox->setCurrentIndex(index);
            } else {
                // A gateway that is not in the cache yet - keep it as typed
                m_ui->preferredGatewayComboBox->setEditText(preferred);
            }
        }

        // Authentication mode (SAML is the default)
        m_ui->authModeComboBox->setCurrentIndex(
            data.value(QLatin1String("auth-mode")) == QLatin1String("credentials") ? 1 : 0);

        // Password storage: a new connection asks the agent; an existing one
        // keeps its flags (a missing key is 0, system-owned)
        m_isNew = data.isEmpty();
        const uint passwordFlags = data.value(QLatin1String("password-flags")).toUInt();
        m_passwordFlagsIndex = m_isNew || (passwordFlags & 1u) ? 0 : 1;
        if (!m_isNew && (passwordFlags & 2u)) {
            m_passwordFlagsIndex = 2;
        }
        m_ui->passwordFlagsComboBox->setCurrentIndex(m_passwordFlagsIndex);
        // Loaded before the combo's signal is connected: "Ask every time" here too
        m_ui->passwordLineEdit->setEnabled(m_passwordFlagsIndex != 2);
        loadSecrets(setting);
        setPasswordVisible(m_ui->authModeComboBox->currentIndex() == 1);

        // Connect timeout: NetworkManager's own vpn.timeout, not a vpn.data item.
        // A profile that exists has at least the gateway in vpn.data; only a new
        // one gets the default for the authentication mode as its first value.
        // While the value is 0 (new or existing profile) it follows the
        // authentication mode until the user sets a value.
        const bool isNew = data.isEmpty();
        const uint stored = vpnSetting->timeout();
        uint timeoutValue = stored;
        m_timeoutFollowsAuthMode = (stored == 0);
        if (isNew && stored == 0) {
            timeoutValue = m_ui->authModeComboBox->currentIndex() == 0 ? samlTimeoutDefault : 0;
        }
        {
            const QSignalBlocker blocker(m_ui->timeoutSpinBox);
            m_ui->timeoutSpinBox->setValue(static_cast<double>(timeoutValue));
        }

        // Username for portals that ask for credentials on the terminal
        m_ui->usernameLineEdit->setText(data.value(QLatin1String("username")));

        // Load Browser
        const QString browserValue = data.value(QLatin1String("browser"), browserValues.first());
        const int browserIndex = m_ui->browserComboBox->findText(browserValue);
        if (browserIndex >= 0) {
            m_ui->browserComboBox->setCurrentIndex(browserIndex);
        } else {
            // An older profile with a full path - keep it as typed
            m_ui->browserComboBox->setEditText(browserValue);
        }

        // Load DNS
        if (data.contains(QLatin1String("dns"))) {
            m_ui->dnsLineEdit->setText(data.value(QLatin1String("dns")));
        }

        // Legacy TLS renegotiation workaround; the service switches this to
        // "Always on" itself after a connection that needed it
        const QString fixOpenssl = data.value(QLatin1String("fix-openssl")).toLower();
        if (fixOpenssl == QLatin1String("true")) {
            m_ui->fixOpensslComboBox->setCurrentIndex(1);
        } else if (fixOpenssl == QLatin1String("false")) {
            m_ui->fixOpensslComboBox->setCurrentIndex(2);
        } else {
            m_ui->fixOpensslComboBox->setCurrentIndex(0);
        }

        // Tunnel MTU: a missing or invalid value shows as automatic (0)
        const int mtuShown = nm_gpclient_mtu_parse(data.value(QLatin1String("mtu")).toUtf8().constData());
        m_mtuTouched = false;
        {
            const QSignalBlocker blocker(m_ui->mtuSpinBox);
            m_ui->mtuSpinBox->setValue(mtuShown > 0 ? mtuShown : 0);
        }

        // Load HIP (default: enabled)
        const QString hipValue = data.value(QLatin1String("hip"), QLatin1String("true"));
        m_ui->hipCheckBox->setChecked(hipValue.toLower() == QLatin1String("true"));

        // Load HIP script
        if (data.contains(QLatin1String("hip-script")) && !data.value(QLatin1String("hip-script")).isEmpty()) {
            m_ui->hipScriptLineEdit->setText(data.value(QLatin1String("hip-script")));
        }
        m_ui->hipScriptLabel->setEnabled(m_ui->hipCheckBox->isChecked());
        m_ui->hipScriptLineEdit->setEnabled(m_ui->hipCheckBox->isChecked());
    }
}

QVariantMap GpclientWidget::setting() const
{
    NetworkManager::VpnSetting setting;
    setting.setServiceType(QLatin1String("org.freedesktop.NetworkManager.gpclient"));

    // Start from what the profile already had, so keys this widget does not show
    // (gateway-list written by the service, dns-domains) survive a save
    NMStringMap data = m_originalData;

    // Save the portal / gateway address
    const QString gateway = m_ui->gatewayLineEdit->text().trimmed();
    if (!gateway.isEmpty()) {
        data.insert(QLatin1String("gateway"), gateway);
    } else {
        data.remove(QLatin1String("gateway"));
    }

    // Save "address is a gateway"
    if (m_ui->asGatewayCheckBox->isChecked()) {
        data.insert(QLatin1String("as-gateway"), QLatin1String("true"));
    } else {
        data.remove(QLatin1String("as-gateway"));
    }

    // Save the preferred gateway (the automatic entry stores nothing)
    const QString preferred = m_ui->preferredGatewayComboBox->currentText().trimmed();
    if (!preferred.isEmpty() && preferred != gatewayAutoLabel()) {
        data.insert(QLatin1String("preferred-gateway"), preferred);
    } else {
        data.remove(QLatin1String("preferred-gateway"));
    }

    // Save the authentication mode (SAML is the default and stores nothing)
    if (m_ui->authModeComboBox->currentIndex() == 1) {
        data.insert(QLatin1String("auth-mode"), QLatin1String("credentials"));
    } else {
        data.remove(QLatin1String("auth-mode"));
    }

    // Save the username
    if (!m_ui->usernameLineEdit->text().isEmpty()) {
        data.insert(QLatin1String("username"), m_ui->usernameLineEdit->text());
    } else {
        data.remove(QLatin1String("username"));
    }

    // Save the password flags: a new connection gets the default, an existing
    // one keeps its own (an untouched combo writes nothing)
    const int flagsIndex = m_ui->passwordFlagsComboBox->currentIndex();
    const uint passwordFlags = passwordFlagsByIndex[qBound(0, flagsIndex, 2)];
    if (m_isNew || flagsIndex != m_passwordFlagsIndex) {
        data.insert(QLatin1String("password-flags"), QString::number(passwordFlags));
    }

    // A one-time code is never stored. The service sets this after the first
    // code too; a new connection starts with it.
    if (m_isNew && !data.contains(QLatin1String("otp-flags"))) {
        data.insert(QLatin1String("otp-flags"), QStringLiteral("2"));
    }

    // Save Browser
    const QString browserText = browserValue(m_ui->browserComboBox->currentText());
    if (!browserText.isEmpty()) {
        data.insert(QLatin1String("browser"), browserText);
    } else {
        data.remove(QLatin1String("browser"));
    }

    // Save DNS
    if (!m_ui->dnsLineEdit->text().isEmpty()) {
        data.insert(QLatin1String("dns"), m_ui->dnsLineEdit->text());
    } else {
        data.remove(QLatin1String("dns"));
    }

    // Save the legacy TLS workaround. Automatic stores nothing - the service
    // reads a missing key as "auto" and switches the profile to "true" once a
    // portal needs it.
    switch (m_ui->fixOpensslComboBox->currentIndex()) {
    case 1:
        data.insert(QLatin1String("fix-openssl"), QLatin1String("true"));
        break;
    case 2:
        data.insert(QLatin1String("fix-openssl"), QLatin1String("false"));
        break;
    default:
        data.remove(QLatin1String("fix-openssl"));
        break;
    }

    // Save the MTU: 0 = automatic (no key), below 576 is saved as 576. An
    // invalid stored value shows as 0; while the user did not touch the spin box
    // the key keeps its text (data starts from m_originalData).
    const int mtu = m_ui->mtuSpinBox->value();
    const bool keepStored = !m_mtuTouched
        && nm_gpclient_mtu_parse(m_originalData.value(QLatin1String("mtu")).toUtf8().constData()) < 0;
    if (keepStored) {
        // nothing to do
    } else if (mtu <= 0) {
        data.remove(QLatin1String("mtu"));
    } else {
        data.insert(QLatin1String("mtu"), QString::number(qBound(MTU_MIN, mtu, MTU_MAX)));
    }

    // Save HIP
    data.insert(QLatin1String("hip"),
                m_ui->hipCheckBox->isChecked() ? QLatin1String("true") : QLatin1String("false"));

    // Save HIP script (also when HIP is off: the service ignores it then)
    const QString hipScript = m_ui->hipScriptLineEdit->text().trimmed();
    if (!hipScript.isEmpty()) {
        data.insert(QLatin1String("hip-script"), hipScript);
    } else {
        data.remove(QLatin1String("hip-script"));
    }

    setting.setData(data);

    // The password stays out of the connection when it is not to be saved. An
    // agent-owned secret goes to NetworkManager, which hands it to the agent and
    // keeps it out of the profile file.
    const QString password = m_ui->passwordLineEdit->text();
    if (!password.isEmpty() && !(passwordFlags & 2u)) {
        setting.setSecrets(NMStringMap{{QStringLiteral("password"), password}});
    }
    setting.setPersistent(m_persistent);
    setting.setUsername(m_userName);

    // toMap() always writes the timeout, so leaving it out here would reset a
    // value set with nmcli (vpn.timeout) to 0 on every save
    setting.setTimeout(static_cast<uint>(m_ui->timeoutSpinBox->value()));

    return setting.toMap();
}

bool GpclientWidget::isValid() const
{
    // The same rules as the GNOME editors (nm-gpclient-validate.h): the
    // gateway must be set, the other fields must be what the service accepts
    return nm_gpclient_validate_form(m_ui->gatewayLineEdit->text().toUtf8().constData(),
                                     m_ui->usernameLineEdit->text().toUtf8().constData(),
                                     browserValue(m_ui->browserComboBox->currentText()).toUtf8().constData(),
                                     m_ui->dnsLineEdit->text().toUtf8().constData(),
                                     m_ui->hipCheckBox->isChecked(),
                                     m_ui->hipScriptLineEdit->text().toUtf8().constData())
        == nullptr;
}

/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#include "gpclientauthwidget.h"

#include <NetworkManagerQt/Utils>
#include <KLocalizedString>
#include <QFormLayout>
#include <QLabel>
#include <QDialog>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QTimer>

namespace
{
// The same names and rules as auth-dialog/nm-gpclient-auth-dialog.py
const QString samlHintPrefix = QStringLiteral("x-gp-saml:");
const QString samlResultKey = QStringLiteral("gpauth-result");
const QString gpauthBinary = QStringLiteral("/usr/bin/gpauth");

// Nothing from the hint may become an option, a command or a line of its own
bool validServer(const QString &server)
{
    static const QRegularExpression unsafe(QStringLiteral("[\\s\\x{0}-\\x{1f}\\x{7f}]"));
    return !server.isEmpty() && !server.startsWith(QLatin1Char('-')) && !unsafe.match(server).hasMatch();
}

bool validBrowser(const QString &browser)
{
    static const QRegularExpression re(
        QStringLiteral("\\A(?:/[^\\s\\x{0}-\\x{1f}\\x{7f}]*|[A-Za-z0-9][A-Za-z0-9._+-]*)\\z"));
    return re.match(browser).hasMatch();
}

QString labelForSecret(const QString &key)
{
    if (key == QLatin1String("username")) {
        return i18n("Username");
    }
    if (key == QLatin1String("password")) {
        return i18n("Password");
    }
    if (key == QLatin1String("otp")) {
        return i18n("Token / one-time code");
    }

    QString label = key;
    label.replace(QLatin1Char('-'), QLatin1Char(' '));
    if (!label.isEmpty()) {
        label[0] = label.at(0).toUpper();
    }
    return label;
}
}

GpclientAuthWidget::GpclientAuthWidget(const NetworkManager::VpnSetting::Ptr &setting,
                                       const QStringList &hints,
                                       QWidget *parent)
    : SettingWidget(setting, hints, parent)
{
    auto *layout = new QFormLayout(this);
    layout->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);

    const QString messageHintPrefix = QStringLiteral("x-vpn-message:");
    QString message;
    QString samlHint;
    bool samlRequested = false;
    for (const QString &hint : hints) {
        if (hint.startsWith(messageHintPrefix)) {
            message = hint.mid(messageHintPrefix.size());
        } else if (hint.startsWith(samlHintPrefix)) {
            samlRequested = true;
            samlHint = hint.mid(samlHintPrefix.size());
        } else if (!hint.isEmpty() && !m_secretKeys.contains(hint)) {
            m_secretKeys.append(hint);
        }
    }

    // NetworkManager requests the initial password with an empty hints list.
    // Interactive username, password, and OTP prompts include their secret key.
    if (m_secretKeys.isEmpty() && setting
        && setting->data().value(QLatin1String("auth-mode")) == QLatin1String("credentials")
        && (setting->data().value(QLatin1String("password-flags")).toUInt() & 4) == 0) {
        m_secretKeys.append(QLatin1String("password"));
    }

    if (!message.isEmpty()) {
        auto *messageLabel = new QLabel(message, this);
        messageLabel->setTextFormat(Qt::PlainText);
        messageLabel->setWordWrap(true);
        layout->addRow(messageLabel);
    }

    if (samlRequested) {
        // No field: the user only waits for the browser login
        m_secretKeys.clear();
        auto *signingIn = new QLabel(i18n("Signing in…"), this);
        signingIn->setTextFormat(Qt::PlainText);
        layout->addRow(signingIn);
        startSamlLogin(samlHint);
        return;
    }

    for (const QString &key : m_secretKeys) {
        auto *field = new QLineEdit(this);
        if (key != QLatin1String("username") && key != QLatin1String("otp")) {
            field->setEchoMode(QLineEdit::Password);
        }

        if (m_fields.isEmpty()) {
            setFocusProxy(field);
        }
        m_fields.insert(key, field);

        auto *label = new QLabel(labelForSecret(key), this);
        label->setTextFormat(Qt::PlainText);
        label->setBuddy(field);
        layout->addRow(label, field);
    }
}

GpclientAuthWidget::~GpclientAuthWidget()
{
    if (m_gpauth && m_gpauth->state() != QProcess::NotRunning) {
        // The dialog is gone: nobody waits for the login
        m_gpauth->disconnect(this);
        m_gpauth->terminate();
        if (!m_gpauth->waitForFinished(3000)) {
            m_gpauth->kill();
            m_gpauth->waitForFinished(1000);
        }
    }
}

void GpclientAuthWidget::startSamlLogin(const QString &hint)
{
    // A hint that is not valid cancels the request, as in the GNOME auth dialog
    const QJsonObject saml = QJsonDocument::fromJson(hint.toUtf8()).object();
    const QString server = saml.value(QLatin1String("server")).toString();
    const QString fixOpenssl = saml.value(QLatin1String("fix_openssl")).toString();
    const QString browser = saml.value(QLatin1String("browser")).toString();
    const QJsonValue target = saml.value(QLatin1String("browser_target"));
    const bool valid = validServer(server)
        && saml.value(QLatin1String("gateway")).isBool()
        && (fixOpenssl == QLatin1String("auto") || fixOpenssl == QLatin1String("true") || fixOpenssl == QLatin1String("false"))
        && saml.value(QLatin1String("browser")).isString() && validBrowser(browser)
        && (target.isUndefined() || target.isNull() || (target.isString() && validBrowser(target.toString())));
    if (!valid) {
        qWarning("gpclient: invalid SAML request from the VPN service");
        QTimer::singleShot(0, this, [this]() { finishSamlLogin(QString()); });
        return;
    }
    m_samlServer = server;
    m_samlGateway = saml.value(QLatin1String("gateway")).toBool();
    m_samlFixOpenssl = fixOpenssl;
    m_samlBrowser = browser;
    m_samlBrowserTarget = target.toString();
    m_samlRetried = (fixOpenssl == QLatin1String("true")); // already passed from the start

    m_gpauth = new QProcess(this);
    connect(m_gpauth, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            qWarning("gpclient: cannot run %s", qPrintable(gpauthBinary));
            finishSamlLogin(QString());
        }
    });
    connect(m_gpauth, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this](int status, QProcess::ExitStatus exitStatus) {
        const QString out = QString::fromUtf8(m_gpauth->readAllStandardOutput());
        const QString err = QString::fromUtf8(m_gpauth->readAllStandardError());
        const QStringList lines = out.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
        QString last;
        for (const QString &line : lines) {
            if (!line.trimmed().isEmpty()) {
                last = line;
            }
        }
        if (exitStatus == QProcess::NormalExit && status == 0 && !last.isEmpty()) {
            // A success line only: {"cancelled":null} and others end the request
            if (QJsonDocument::fromJson(last.toUtf8()).object().contains(QLatin1String("success"))) {
                finishSamlLogin(last);
            } else {
                finishSamlLogin(QString());
            }
        } else if (exitStatus == QProcess::NormalExit && status == 1 && err.contains(QLatin1String("non-SAML prelogin"))) {
            finishSamlLogin(QStringLiteral("standard")); // the server has no SAML login
        } else if (m_samlFixOpenssl == QLatin1String("auto") && !m_samlRetried
                   && (err.contains(QLatin1String("unsafe legacy renegotiation disabled"))
                       || err.contains(QLatin1String("requires unsafe legacy TLS renegotiation"))
                       || err.contains(QLatin1String("--fix-openssl")))) {
            m_samlRetried = true;
            runGpauth(true);
        } else {
            qWarning("gpclient: gpauth failed (status %d)", status);
            finishSamlLogin(QString());
        }
    });
    runGpauth(m_samlRetried);
}

void GpclientAuthWidget::runGpauth(bool fixOpenssl)
{
    QStringList args{QStringLiteral("--log-format"), QStringLiteral("json")};
    if (m_samlGateway) {
        args << QStringLiteral("--gateway");
    }
    if (fixOpenssl) {
        args << QStringLiteral("--fix-openssl");
    }
    args << QStringLiteral("--browser=") + m_samlBrowser << QStringLiteral("--") << m_samlServer;

    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.remove(QStringLiteral("GP_BROWSER"));
    if (!m_samlBrowserTarget.isEmpty()) {
        env.insert(QStringLiteral("GP_BROWSER"), m_samlBrowserTarget);
    }
    m_gpauth->setProcessEnvironment(env);
    m_gpauth->start(gpauthBinary, args);
}

void GpclientAuthWidget::finishSamlLogin(const QString &result)
{
    m_samlResult = result;
    // plasma-nm has no non-interactive answer: close its password dialog
    // ourselves. accepted -> it reads setting(); rejected -> the login is
    // cancelled and NetworkManager fails the activation.
    if (auto *dialog = qobject_cast<QDialog *>(window())) {
        if (result.isEmpty()) {
            dialog->reject();
        } else {
            dialog->accept();
        }
    }
}

QVariantMap GpclientAuthWidget::setting() const
{
    NMStringMap secrets;
    if (!m_samlResult.isEmpty()) {
        secrets.insert(samlResultKey, m_samlResult);
    }
    for (const QString &key : m_secretKeys) {
        const QString value = m_fields.value(key)->text();
        if (!value.isEmpty()) {
            secrets.insert(key, value);
        }
    }

    QVariantMap result;
    result.insert(QStringLiteral("secrets"), QVariant::fromValue<NMStringMap>(secrets));
    return result;
}

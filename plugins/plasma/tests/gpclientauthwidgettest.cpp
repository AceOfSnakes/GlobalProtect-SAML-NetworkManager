/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

// The SAML login of GpclientAuthWidget (saml-auth=dialog): the hint check, the
// gpauth run and the way the dialog is closed. gpauth is a shell script of the
// test (GPCLIENT_TEST_GPAUTH, defined only in the test build).

#include "gpclientauthwidget.h"

#include <NetworkManagerQt/VpnSetting>
#include <QDialog>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QtTest>

namespace
{
const QString successLine = QStringLiteral(R"({"success":{"username":"a","preloginCookie":"123456"}})");
}

class GpclientAuthWidgetTest : public QObject
{
    Q_OBJECT

private:
    QTemporaryDir m_dir;
    QString m_argsFile;

    // Writes the fake gpauth; `body` is shell code. Every run appends its arguments to m_argsFile
    void fakeGpauth(const QString &body)
    {
        const QString path = m_dir.filePath(QStringLiteral("gpauth"));
        QFile::remove(m_argsFile);
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate));
        file.write(("#!/bin/sh\necho \"$*\" >> '" + m_argsFile + "'\n" + body + "\n").toUtf8());
        file.close();
        file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
        qputenv("GPCLIENT_TEST_GPAUTH", path.toUtf8());
    }

    QStringList runs() const
    {
        QFile file(m_argsFile);
        if (!file.open(QIODevice::ReadOnly)) {
            return {};
        }
        return QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    }

    static QJsonObject goodHint()
    {
        return QJsonObject{{QStringLiteral("server"), QStringLiteral("vpn.example.com")},
                           {QStringLiteral("gateway"), false},
                           {QStringLiteral("fix_openssl"), QStringLiteral("false")},
                           {QStringLiteral("browser"), QStringLiteral("firefox")},
                           {QStringLiteral("browser_target"), QJsonValue::Null}};
    }

    static QString hint(const QJsonObject &saml)
    {
        return QStringLiteral("x-gp-saml:") + QString::fromUtf8(QJsonDocument(saml).toJson(QJsonDocument::Compact));
    }

    // Shows the widget in a dialog and waits until the widget closes it
    bool login(const QString &samlHint, QString *result, QDialog::DialogCode *code)
    {
        NetworkManager::VpnSetting::Ptr setting(new NetworkManager::VpnSetting);
        QDialog dialog;
        QSignalSpy spy(&dialog, &QDialog::finished); // before the widget: it may close the dialog at once
        auto *widget = new GpclientAuthWidget(setting, {samlHint}, &dialog);
        const bool closed = spy.count() > 0 || spy.wait(10000);
        *code = static_cast<QDialog::DialogCode>(dialog.result());
        NetworkManager::VpnSetting saved;
        saved.fromMap(widget->setting());
        *result = saved.secrets().value(QStringLiteral("gpauth-result"));
        return closed;
    }

private Q_SLOTS:
    void initTestCase()
    {
        QVERIFY(m_dir.isValid());
        m_argsFile = m_dir.filePath(QStringLiteral("args"));
    }

    void successLineIsTheResult()
    {
        fakeGpauth(QStringLiteral("echo '") + successLine + QStringLiteral("'"));
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(goodHint()), &result, &code));
        QCOMPARE(code, QDialog::Accepted);
        QCOMPARE(result, successLine);
        const QStringList all = runs();
        QCOMPARE(all.size(), 1);
        QVERIFY(all.first().endsWith(QStringLiteral("--browser=firefox -- vpn.example.com")));
        QVERIFY(!all.first().contains(QStringLiteral("--gateway")));
        QVERIFY(!all.first().contains(QStringLiteral("--fix-openssl")));
    }

    void gatewayAndFixOpensslAreArguments()
    {
        fakeGpauth(QStringLiteral("echo '") + successLine + QStringLiteral("'"));
        QJsonObject saml = goodHint();
        saml.insert(QStringLiteral("gateway"), true);
        saml.insert(QStringLiteral("fix_openssl"), QStringLiteral("true"));
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(saml), &result, &code));
        QCOMPARE(code, QDialog::Accepted);
        QCOMPARE(runs().size(), 1);
        QVERIFY(runs().first().contains(QStringLiteral("--gateway")));
        QVERIFY(runs().first().contains(QStringLiteral("--fix-openssl")));
    }

    void cancelledIsRejected_data()
    {
        QTest::addColumn<QString>("script");
        QTest::newRow("cancelled") << QStringLiteral("echo '{\"cancelled\":null}'");
        QTest::newRow("no output") << QStringLiteral("exit 0");
        QTest::newRow("failure") << QStringLiteral("echo '{\"failure\":\"x\"}'");
        QTest::newRow("exit 1") << QStringLiteral("echo '") + successLine + QStringLiteral("'; exit 1");
        QTest::newRow("exit 1, other error") << QStringLiteral("echo boom >&2; exit 1");
    }

    void cancelledIsRejected()
    {
        QFETCH(QString, script);
        fakeGpauth(script);
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(goodHint()), &result, &code));
        QCOMPARE(code, QDialog::Rejected);
        QVERIFY(result.isEmpty());
    }

    void nonSamlServerIsStandard()
    {
        fakeGpauth(QStringLiteral("echo 'non-SAML prelogin' >&2; exit 1"));
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(goodHint()), &result, &code));
        QCOMPARE(code, QDialog::Accepted);
        QCOMPARE(result, QStringLiteral("standard"));
    }

    void legacyTlsErrorRetriesWithFixOpenssl_data()
    {
        QTest::addColumn<QString>("mode");
        QTest::addColumn<int>("expectedRuns");
        QTest::addColumn<QDialog::DialogCode>("expectedCode");
        QTest::newRow("auto retries once") << QStringLiteral("auto") << 2 << QDialog::Accepted;
        // the negative twins: no retry
        QTest::newRow("false does not retry") << QStringLiteral("false") << 1 << QDialog::Rejected;
        QTest::newRow("true already has the option") << QStringLiteral("true") << 1 << QDialog::Rejected;
    }

    void legacyTlsErrorRetriesWithFixOpenssl()
    {
        QFETCH(QString, mode);
        QFETCH(int, expectedRuns);
        QFETCH(QDialog::DialogCode, expectedCode);
        // With --fix-openssl it works, without it prints the legacy TLS error.
        // Mode "true" passes the option at once, so the fake fails there too.
        const QString ok = QStringLiteral("echo '") + successLine + QStringLiteral("'");
        const QString fail = QStringLiteral("echo 'unsafe legacy renegotiation disabled' >&2; exit 1");
        fakeGpauth(mode == QLatin1String("true")
                       ? fail
                       : QStringLiteral("case \"$*\" in *--fix-openssl*) ") + ok + QStringLiteral(";; *) ") + fail + QStringLiteral(";; esac"));
        QJsonObject saml = goodHint();
        saml.insert(QStringLiteral("fix_openssl"), mode);
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(saml), &result, &code));
        QCOMPARE(code, expectedCode);
        const QStringList all = runs();
        QCOMPARE(all.size(), expectedRuns);
        if (mode == QLatin1String("auto")) {
            QVERIFY(!all.at(0).contains(QStringLiteral("--fix-openssl")));
            QVERIFY(all.at(1).contains(QStringLiteral("--fix-openssl")));
            QCOMPARE(result, successLine);
        }
    }

    void invalidHintIsRejectedWithoutGpauth_data()
    {
        QTest::addColumn<QString>("hint");
        auto with = [](const QString &key, const QJsonValue &value) {
            QJsonObject saml = goodHint();
            saml.insert(key, value);
            return hint(saml);
        };
        QTest::newRow("server is an option") << with(QStringLiteral("server"), QStringLiteral("-evil"));
        QTest::newRow("server has a space") << with(QStringLiteral("server"), QStringLiteral("a b"));
        QTest::newRow("server has a newline") << with(QStringLiteral("server"), QStringLiteral("a\nb"));
        QTest::newRow("server has a control char") << with(QStringLiteral("server"), QStringLiteral("a\x01" "b"));
        QTest::newRow("server is empty") << with(QStringLiteral("server"), QString());
        QTest::newRow("browser has a space") << with(QStringLiteral("browser"), QStringLiteral("my browser"));
        QTest::newRow("browser is a number") << with(QStringLiteral("browser"), 1);
        QTest::newRow("target has a space") << with(QStringLiteral("browser_target"), QStringLiteral("a b"));
        QTest::newRow("gateway is a string") << with(QStringLiteral("gateway"), QStringLiteral("true"));
        QTest::newRow("fix_openssl other value") << with(QStringLiteral("fix_openssl"), QStringLiteral("yes"));
        QTest::newRow("fix_openssl is a bool") << with(QStringLiteral("fix_openssl"), true);
        QTest::newRow("bad JSON") << QStringLiteral("x-gp-saml:{not json");
        QTest::newRow("empty") << QStringLiteral("x-gp-saml:");
    }

    void invalidHintIsRejectedWithoutGpauth()
    {
        QFETCH(QString, hint);
        fakeGpauth(QStringLiteral("echo '") + successLine + QStringLiteral("'"));
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint, &result, &code));
        QCOMPARE(code, QDialog::Rejected);
        QVERIFY(result.isEmpty());
        QVERIFY(runs().isEmpty());
    }

    void missingGpauthIsRejected()
    {
        qputenv("GPCLIENT_TEST_GPAUTH", m_dir.filePath(QStringLiteral("nothing-here")).toUtf8());
        QString result;
        QDialog::DialogCode code;
        QVERIFY(login(hint(goodHint()), &result, &code));
        QCOMPARE(code, QDialog::Rejected);
        QVERIFY(result.isEmpty());
    }
};

QTEST_MAIN(GpclientAuthWidgetTest)
#include "gpclientauthwidgettest.moc"

/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

// Runs the real GpclientWidget: the connect timeout (vpn.timeout) and the way
// it follows the authentication mode (issue #46).

#include "gpclientwidget.h"

#include <NetworkManagerQt/VpnSetting>
#include <QComboBox>
#include <QLineEdit>
#include <QSignalSpy>
#include <QSpinBox>
#include <QtTest>

namespace
{
const QString serviceType = QStringLiteral("org.freedesktop.NetworkManager.gpclient");
}

class GpclientWidgetTest : public QObject
{
    Q_OBJECT

private:
    static NetworkManager::VpnSetting::Ptr makeSetting(const NMStringMap &data, uint timeout)
    {
        NetworkManager::VpnSetting::Ptr setting(new NetworkManager::VpnSetting);
        setting->setServiceType(serviceType);
        setting->setData(data);
        setting->setTimeout(timeout);
        return setting;
    }

private Q_SLOTS:
    void timeout_data();
    void timeout();
    void keepsUnknownDataKeys();
};

void GpclientWidgetTest::timeout_data()
{
    QTest::addColumn<NMStringMap>("data");
    QTest::addColumn<uint>("stored");
    // Steps: "auth:<index>", "spin:<value>", "type:<digits>"
    QTest::addColumn<QStringList>("steps");
    QTest::addColumn<int>("expectedValue");
    QTest::addColumn<uint>("expectedTimeout");

    const NMStringMap none;
    const NMStringMap gateway{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}};
    const NMStringMap credentials{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")},
                                  {QStringLiteral("auth-mode"), QStringLiteral("credentials")}};

    QTest::newRow("new, browser") << none << 0u << QStringList{} << 360 << 360u;
    QTest::newRow("new, credentials") << none << 0u << QStringList{"auth:1"} << 0 << 0u;
    QTest::newRow("new, credentials, browser again") << none << 0u << QStringList{"auth:1", "auth:0"} << 360 << 360u;
    QTest::newRow("new, spin, credentials") << none << 0u << QStringList{"spin:300", "auth:1"} << 300 << 300u;
    QTest::newRow("new, typed, credentials") << none << 0u << QStringList{"type:500", "auth:1"} << 500 << 500u;
    QTest::newRow("new, stored 90, credentials") << none << 90u << QStringList{"auth:1"} << 90 << 90u;
    QTest::newRow("existing 0 stays 0") << gateway << 0u << QStringList{} << 0 << 0u;
    QTest::newRow("existing credentials 0, browser") << credentials << 0u << QStringList{"auth:0"} << 360 << 360u;
    QTest::newRow("existing credentials 240, browser") << credentials << 240u << QStringList{"auth:0"} << 240 << 240u;
    QTest::newRow("existing credentials 240 kept") << credentials << 240u << QStringList{} << 240 << 240u;
    QTest::newRow("existing credentials 240, typed") << credentials << 240u << QStringList{"type:300"} << 300 << 300u;
}

void GpclientWidgetTest::timeout()
{
    QFETCH(NMStringMap, data);
    QFETCH(uint, stored);
    QFETCH(QStringList, steps);
    QFETCH(int, expectedValue);
    QFETCH(uint, expectedTimeout);

    GpclientWidget widget(makeSetting(data, stored));
    auto *spin = widget.findChild<QSpinBox *>(QStringLiteral("timeoutSpinBox"));
    auto *authMode = widget.findChild<QComboBox *>(QStringLiteral("authModeComboBox"));
    QVERIFY(spin);
    QVERIFY(authMode);
    auto *edit = spin->findChild<QLineEdit *>();
    QVERIFY(edit);

    QSignalSpy changed(&widget, &SettingWidget::settingChanged);
    for (const QString &step : steps) {
        // Every step must emit settingChanged itself (plasma-nm enables OK on it)
        const int before = changed.count();
        const QString kind = step.section(QLatin1Char(':'), 0, 0);
        const QString arg = step.section(QLatin1Char(':'), 1);
        if (kind == QLatin1String("auth")) {
            authMode->setCurrentIndex(arg.toInt());
        } else if (kind == QLatin1String("spin")) {
            spin->setValue(arg.toInt());
        } else if (kind == QLatin1String("type")) {
            spin->setFocus();
            edit->selectAll();
            QTest::keyClicks(edit, arg);
        } else {
            QFAIL("unknown step");
        }
        QVERIFY2(changed.count() > before, qPrintable(QStringLiteral("no settingChanged after ") + step));
    }

    QCOMPARE(spin->value(), expectedValue);
    const QVariantMap map = widget.setting();
    QVERIFY(map.contains(QStringLiteral("timeout")));
    QCOMPARE(map.value(QStringLiteral("timeout")).toUInt(), expectedTimeout);
}

void GpclientWidgetTest::keepsUnknownDataKeys()
{
    NMStringMap data{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")},
                     {QStringLiteral("gateway-list"), QStringLiteral("gw1;gw2")}};
    GpclientWidget widget(makeSetting(data, 0));

    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QCOMPARE(saved.data().value(QStringLiteral("gateway-list")), QStringLiteral("gw1;gw2"));
    QCOMPARE(saved.data().value(QStringLiteral("gateway")), QStringLiteral("vpn.example.com"));
    QCOMPARE(saved.serviceType(), serviceType);
}

QTEST_MAIN(GpclientWidgetTest)
#include "gpclientwidgettest.moc"

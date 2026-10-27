/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

// Runs the real GpclientWidget: the connect timeout (vpn.timeout) and the way
// it follows the authentication mode (issue #46).

#include "gpclientwidget.h"

#include <NetworkManagerQt/VpnSetting>
#include <QCheckBox>
#include <QComboBox>
#include <QLineEdit>
#include <QSignalSpy>
#include <QSpinBox>
#include <QDoubleSpinBox>
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
    void removesClearedKeys_data();
    void removesClearedKeys();
    void keepsPersistentAndUserName();
    void doesNotInventPersistentAndUserName();
    void trimsGateway_data();
    void trimsGateway();
    void fieldValidity_data();
    void fieldValidity();
    void readsBooleansIgnoringCase_data();
    void readsBooleansIgnoringCase();
    void mtuTyping_data();
    void mtuTyping();
    void mtuDeleteOnAutomaticKeepsIt_data();
    void mtuDeleteOnAutomaticKeepsIt();
    void mtuEmptyIsAutomatic_data();
    void mtuEmptyIsAutomatic();
    void mtuArrowsAndWheelStillStep();
    void passwordStorage_data();
    void passwordStorage();
    void certPassStorage_data();
    void certPassStorage();
};

namespace
{
// Types into the MTU spin box like a user: focus, optional select all, keys
struct MtuBox {
    QSpinBox *spin = nullptr;
    QLineEdit *edit = nullptr;
};

MtuBox findMtu(GpclientWidget &widget)
{
    MtuBox box;
    box.spin = widget.findChild<QSpinBox *>(QStringLiteral("mtuSpinBox"));
    if (box.spin) {
        box.edit = box.spin->findChild<QLineEdit *>();
    }
    return box;
}

QString savedMtu(const GpclientWidget &widget, bool *has = nullptr)
{
    NetworkManager::VpnSetting result;
    result.fromMap(widget.setting());
    if (has) {
        *has = result.data().contains(QStringLiteral("mtu"));
    }
    return result.data().value(QStringLiteral("mtu"));
}
}

void GpclientWidgetTest::mtuTyping_data()
{
    QTest::addColumn<QString>("stored");
    QTest::addColumn<QString>("place"); // where the cursor is before typing
    QTest::addColumn<QString>("typed");
    QTest::addColumn<int>("expectedValue");
    QTest::addColumn<QString>("expectedSaved"); // empty: no mtu key

    // Positive: digits replace "Automatic", wherever the cursor is
    QTest::newRow("automatic, cursor at end, type 1300") << "" << "end" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, Key_End, type 1300") << "" << "keyEnd" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, Key_Home, type 1300") << "" << "keyHome" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, mouse click, type 1300") << "" << "click" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, focus in (mouse), type 1300") << "" << "focusIn" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, focus in (tab), type 1300") << "" << "tab" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, select all, type 1300") << "" << "selectAll" << "1300" << 1300 << "1300";
    QTest::newRow("automatic, cursor at end, type 1500") << "" << "end" << "1500" << 1500 << "1500";
    QTest::newRow("automatic, cursor at end, type 1") << "" << "end" << "1" << 1 << "576";
    QTest::newRow("automatic, cursor at end, type 0") << "" << "end" << "0" << 0 << "";
    QTest::newRow("1400, select all, type 0") << "1400" << "selectAll" << "0" << 0 << "";
    QTest::newRow("1400, select all, type 1200") << "1400" << "selectAll" << "1200" << 1200 << "1200";
    // Negative: not a number, or above the maximum, is not accepted
    QTest::newRow("automatic, end, abc") << "" << "end" << "abc" << 0 << "";
    QTest::newRow("automatic, click, abc") << "" << "click" << "abc" << 0 << "";
    QTest::newRow("automatic, select all, abc") << "" << "selectAll" << "abc" << 0 << "";
    QTest::newRow("1400, select all, abc") << "1400" << "selectAll" << "abc" << 1400 << "1400";
    QTest::newRow("automatic, 13a00") << "" << "click" << "13a00" << 1300 << "1300";
    QTest::newRow("automatic, minus") << "" << "click" << "-5" << 5 << "576";
    QTest::newRow("automatic, 1501") << "" << "click" << "1501" << 150 << "576";
    QTest::newRow("automatic, 99999") << "" << "click" << "99999" << 999 << "999";
}

void GpclientWidgetTest::mtuTyping()
{
    QFETCH(QString, stored);
    QFETCH(QString, place);
    QFETCH(QString, typed);
    QFETCH(int, expectedValue);
    QFETCH(QString, expectedSaved);

    NMStringMap data{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}};
    if (!stored.isEmpty()) {
        data.insert(QStringLiteral("mtu"), stored);
    }
    GpclientWidget widget(makeSetting(data, 0));
    widget.show();
    const MtuBox box = findMtu(widget);
    QVERIFY(box.spin);
    QVERIFY(box.edit);
    QCOMPARE(box.spin->value(), stored.isEmpty() ? 0 : stored.toInt());
    if (stored.isEmpty()) {
        QCOMPARE(box.edit->text(), QStringLiteral("Automatic"));
    }

    box.spin->setFocus();
    if (place == QLatin1String("selectAll")) {
        box.edit->selectAll();
    } else if (place == QLatin1String("end")) {
        box.edit->setCursorPosition(box.edit->text().size());
    } else if (place == QLatin1String("keyEnd")) {
        QTest::keyClick(box.edit, Qt::Key_End);
    } else if (place == QLatin1String("keyHome")) {
        QTest::keyClick(box.edit, Qt::Key_Home);
    } else if (place == QLatin1String("click")) {
        QTest::mouseClick(box.edit, Qt::LeftButton, Qt::NoModifier, QPoint(box.edit->width() - 3, box.edit->height() / 2));
    } else if (place == QLatin1String("focusIn") || place == QLatin1String("tab")) {
        QFocusEvent in(QEvent::FocusIn, place == QLatin1String("tab") ? Qt::TabFocusReason : Qt::MouseFocusReason);
        QCoreApplication::sendEvent(box.edit, &in);
    } else {
        QFAIL("unknown place");
    }
    QTest::keyClicks(box.edit, typed);

    QCOMPARE(box.spin->value(), expectedValue);
    bool has = false;
    const QString saved = savedMtu(widget, &has);
    QCOMPARE(has, !expectedSaved.isEmpty());
    QCOMPARE(saved, expectedSaved);
}

void GpclientWidgetTest::mtuDeleteOnAutomaticKeepsIt_data()
{
    QTest::addColumn<int>("key");
    QTest::addColumn<bool>("cursorAtEnd");

    QTest::newRow("Backspace at end") << int(Qt::Key_Backspace) << true;
    QTest::newRow("Delete at start") << int(Qt::Key_Delete) << false;
    QTest::newRow("Backspace at start") << int(Qt::Key_Backspace) << false;
    QTest::newRow("Delete at end") << int(Qt::Key_Delete) << true;
}

void GpclientWidgetTest::mtuDeleteOnAutomaticKeepsIt()
{
    QFETCH(int, key);
    QFETCH(bool, cursorAtEnd);

    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}}, 0));
    widget.show();
    const MtuBox box = findMtu(widget);
    QVERIFY(box.spin);
    box.spin->setFocus();
    QTest::keyClick(box.edit, cursorAtEnd ? Qt::Key_End : Qt::Key_Home);
    QTest::keyClick(box.edit, Qt::Key(key));
    QTest::keyClick(box.edit, Qt::Key_Return);

    QCOMPARE(box.spin->value(), 0);
    QCOMPARE(box.edit->text(), QStringLiteral("Automatic"));
    bool has = true;
    savedMtu(widget, &has);
    QVERIFY(!has);
    // and digits still replace it afterwards
    QTest::keyClick(box.edit, Qt::Key_End);
    QTest::keyClicks(box.edit, QStringLiteral("1300"));
    QCOMPARE(box.spin->value(), 1300);
}

void GpclientWidgetTest::mtuEmptyIsAutomatic_data()
{
    QTest::addColumn<QString>("stored");
    QTest::addColumn<bool>("useBackspace");
    QTest::addColumn<bool>("finishWithReturn");

    QTest::newRow("1400, Delete, Enter") << "1400" << false << true;
    QTest::newRow("1400, Delete, focus out") << "1400" << false << false;
    QTest::newRow("1400, Backspace, Enter") << "1400" << true << true;
    QTest::newRow("1400, Backspace, focus out") << "1400" << true << false;
    QTest::newRow("700, Delete, focus out") << "700" << false << false;
}

void GpclientWidgetTest::mtuEmptyIsAutomatic()
{
    QFETCH(QString, stored);
    QFETCH(bool, useBackspace);
    QFETCH(bool, finishWithReturn);

    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}, {QStringLiteral("mtu"), stored}}, 0));
    widget.show();
    const MtuBox box = findMtu(widget);
    QVERIFY(box.spin);
    QCOMPARE(box.spin->value(), stored.toInt());
    // Positive: the stored value is saved while it is shown
    QCOMPARE(savedMtu(widget), stored);

    box.spin->setFocus();
    box.edit->selectAll();
    QTest::keyClick(box.edit, useBackspace ? Qt::Key_Backspace : Qt::Key_Delete);
    if (finishWithReturn) {
        QTest::keyClick(box.edit, Qt::Key_Return);
    } else {
        // The editor loses focus (the window is not active offscreen, so send the event)
        QFocusEvent out(QEvent::FocusOut, Qt::TabFocusReason);
        QCoreApplication::sendEvent(box.edit, &out);
    }

    // Negative: the old value is not restored, no mtu key is saved
    QCOMPARE(box.spin->value(), 0);
    QCOMPARE(box.edit->text(), QStringLiteral("Automatic"));
    bool has = true;
    savedMtu(widget, &has);
    QVERIFY(!has);
}

void GpclientWidgetTest::mtuArrowsAndWheelStillStep()
{
    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}}, 0));
    widget.show();
    const MtuBox box = findMtu(widget);
    QVERIFY(box.spin);
    QCOMPARE(box.spin->value(), 0);

    box.spin->setFocus();
    QTest::keyClick(box.spin, Qt::Key_Up);
    QCOMPARE(box.spin->value(), 1);
    QCOMPARE(box.edit->text(), QStringLiteral("1 bytes"));
    QTest::keyClick(box.spin, Qt::Key_Up);
    QCOMPARE(box.spin->value(), 2);
    QTest::keyClick(box.spin, Qt::Key_Down);
    QTest::keyClick(box.spin, Qt::Key_Down);
    QCOMPARE(box.spin->value(), 0);
    QCOMPARE(box.edit->text(), QStringLiteral("Automatic"));
    // Negative: it does not go below the minimum
    QTest::keyClick(box.spin, Qt::Key_Down);
    QCOMPARE(box.spin->value(), 0);

    // Mouse wheel
    QWheelEvent up(QPointF(5, 5), QPointF(box.spin->mapToGlobal(QPoint(5, 5))), QPoint(), QPoint(0, 120), Qt::NoButton, Qt::NoModifier, Qt::NoScrollPhase, false);
    QCoreApplication::sendEvent(box.spin, &up);
    QCOMPARE(box.spin->value(), 1);

    // Saved after stepping
    QCOMPARE(savedMtu(widget), QStringLiteral("576"));
}

void GpclientWidgetTest::trimsGateway_data()
{
    QTest::addColumn<QString>("typed");
    QTest::addColumn<bool>("valid");
    QTest::addColumn<QString>("saved");

    QTest::newRow("padded") << "  vpn.example.com \t" << true << "vpn.example.com";
    QTest::newRow("plain") << "vpn.example.com" << true << "vpn.example.com";
    QTest::newRow("empty") << "" << false << "";
    QTest::newRow("spaces only") << "   \t " << false << "";
}

void GpclientWidgetTest::trimsGateway()
{
    QFETCH(QString, typed);
    QFETCH(bool, valid);
    QFETCH(QString, saved);

    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("old.example.com")}}, 0));
    auto *edit = widget.findChild<QLineEdit *>(QStringLiteral("gatewayLineEdit"));
    QVERIFY(edit);
    edit->setText(typed);

    QCOMPARE(widget.isValid(), valid);
    NetworkManager::VpnSetting result;
    result.fromMap(widget.setting());
    QCOMPARE(result.data().value(QStringLiteral("gateway")), saved);
}

void GpclientWidgetTest::fieldValidity_data()
{
    QTest::addColumn<QString>("field"); // object name of the line edit or combo box
    QTest::addColumn<QString>("typed");
    QTest::addColumn<bool>("hip");
    QTest::addColumn<bool>("valid");

    // The rules are nm-gpclient-validate.h, the same as in the GNOME editors
    QTest::newRow("gateway host:port") << "gatewayLineEdit" << "vpn.example.com:8443" << true << true;
    QTest::newRow("gateway url") << "gatewayLineEdit" << "https://vpn.example.com/ssl-vpn" << true << true;
    QTest::newRow("gateway option") << "gatewayLineEdit" << "-vpn" << true << false;
    QTest::newRow("gateway inner space") << "gatewayLineEdit" << "vpn example" << true << false;
    QTest::newRow("dns list") << "dnsLineEdit" << "10.0.0.1, 2001:db8::1;10.0.0.2" << true << true;
    QTest::newRow("dns empty") << "dnsLineEdit" << "" << true << true;
    QTest::newRow("dns name") << "dnsLineEdit" << "10.0.0.1 dns.example.com" << true << false;
    QTest::newRow("dns short ipv4") << "dnsLineEdit" << "10.0.1" << true << false;
    QTest::newRow("hip script absolute") << "hipScriptLineEdit" << "/usr/local/bin/hip.sh" << true << true;
    QTest::newRow("hip script relative") << "hipScriptLineEdit" << "hip.sh" << true << false;
    QTest::newRow("hip script relative, hip off") << "hipScriptLineEdit" << "hip.sh" << false << true;
    QTest::newRow("usercert absolute") << "userCertLineEdit" << "/etc/ssl/me.p12" << true << true;
    QTest::newRow("usercert empty") << "userCertLineEdit" << "" << true << true;
    QTest::newRow("usercert relative") << "userCertLineEdit" << "me.p12" << true << false;
    QTest::newRow("usercert control character") << "userCertLineEdit" << "/etc/ssl/me\t.p12" << true << false;
    QTest::newRow("userkey absolute") << "userKeyLineEdit" << "/etc/ssl/me.key" << true << true;
    QTest::newRow("userkey relative") << "userKeyLineEdit" << "me.key" << true << false;
    QTest::newRow("username plain") << "usernameLineEdit" << "alice" << true << true;
    QTest::newRow("username option") << "usernameLineEdit" << "-alice" << true << false;
    QTest::newRow("browser bare name") << "browserComboBox" << "brave-browser" << true << true;
    QTest::newRow("browser absolute path") << "browserComboBox" << "/opt/bin/my-browser" << true << true;
    QTest::newRow("browser with space") << "browserComboBox" << "my browser" << true << false;
    QTest::newRow("browser relative path") << "browserComboBox" << "bin/browser" << true << false;
}

void GpclientWidgetTest::fieldValidity()
{
    QFETCH(QString, field);
    QFETCH(QString, typed);
    QFETCH(bool, hip);
    QFETCH(bool, valid);

    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}}, 0));
    QVERIFY(widget.isValid());
    auto *check = widget.findChild<QCheckBox *>(QStringLiteral("hipCheckBox"));
    auto *edit = widget.findChild<QLineEdit *>(field);
    auto *combo = widget.findChild<QComboBox *>(field);
    QVERIFY(check);
    QVERIFY(edit || combo);
    const auto type = [&](const QString &text) {
        if (combo) {
            combo->setEditText(text);
        } else {
            edit->setText(text);
        }
    };
    QSignalSpy spy(&widget, &GpclientWidget::validChanged);
    check->setChecked(true);
    // A bad value first, so that a valid row must also report the way back
    type(field == QLatin1String("hipScriptLineEdit") ? QStringLiteral("relative") : QStringLiteral("-bad value"));
    QVERIFY(!widget.isValid());
    QVERIFY(!spy.isEmpty());
    QCOMPARE(spy.last().at(0).toBool(), false);
    check->setChecked(hip);
    type(typed);

    QCOMPARE(widget.isValid(), valid);
    QCOMPARE(spy.last().at(0).toBool(), valid);
}

void GpclientWidgetTest::readsBooleansIgnoringCase_data()
{
    QTest::addColumn<QString>("key");
    QTest::addColumn<QString>("value");
    QTest::addColumn<QString>("expectedSaved");

    QTest::newRow("hip TRUE") << "hip" << "TRUE" << "true";
    QTest::newRow("hip True") << "hip" << "True" << "true";
    QTest::newRow("hip FALSE") << "hip" << "FALSE" << "false";
    QTest::newRow("hip other is off") << "hip" << "yes" << "false";
    QTest::newRow("as-gateway TRUE") << "as-gateway" << "TRUE" << "true";
    QTest::newRow("as-gateway other is off") << "as-gateway" << "yes" << "";
    QTest::newRow("fix-openssl TRUE") << "fix-openssl" << "TRUE" << "true";
    QTest::newRow("fix-openssl False") << "fix-openssl" << "False" << "false";
    QTest::newRow("fix-openssl other is auto") << "fix-openssl" << "yes" << "";
    QTest::newRow("saml-auth dialog") << "saml-auth" << "dialog" << "dialog";
    QTest::newRow("saml-auth Dialog is the service") << "saml-auth" << "Dialog" << "";
    QTest::newRow("saml-auth service") << "saml-auth" << "service" << "";
    QTest::newRow("saml-auth other is the service") << "saml-auth" << "yes" << "";
}

void GpclientWidgetTest::readsBooleansIgnoringCase()
{
    QFETCH(QString, key);
    QFETCH(QString, value);
    QFETCH(QString, expectedSaved);

    GpclientWidget widget(makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}, {key, value}}, 0));
    NetworkManager::VpnSetting result;
    result.fromMap(widget.setting());
    QCOMPARE(result.data().value(key), expectedSaved);
}

void GpclientWidgetTest::timeout_data()
{
    QTest::addColumn<NMStringMap>("data");
    QTest::addColumn<uint>("stored");
    // Steps: "auth:<index>", "spin:<value>", "type:<digits>"
    QTest::addColumn<QStringList>("steps");
    QTest::addColumn<uint>("expectedValue");
    QTest::addColumn<uint>("expectedTimeout");

    const NMStringMap none;
    const NMStringMap gateway{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}};
    const NMStringMap credentials{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")},
                                  {QStringLiteral("auth-mode"), QStringLiteral("credentials")}};

    QTest::newRow("new, browser") << none << 0u << QStringList{} << 360u << 360u;
    QTest::newRow("new, credentials") << none << 0u << QStringList{"auth:1"} << 0u << 0u;
    QTest::newRow("new, credentials, browser again") << none << 0u << QStringList{"auth:1", "auth:0"} << 360u << 360u;
    QTest::newRow("new, spin, credentials") << none << 0u << QStringList{"spin:300", "auth:1"} << 300u << 300u;
    QTest::newRow("new, typed, credentials") << none << 0u << QStringList{"type:500", "auth:1"} << 500u << 500u;
    QTest::newRow("new, stored 90, credentials") << none << 90u << QStringList{"auth:1"} << 90u << 90u;
    QTest::newRow("existing 0 stays 0") << gateway << 0u << QStringList{} << 0u << 0u;
    QTest::newRow("existing credentials 0, browser") << credentials << 0u << QStringList{"auth:0"} << 360u << 360u;
    QTest::newRow("existing credentials 240, browser") << credentials << 240u << QStringList{"auth:0"} << 240u << 240u;
    QTest::newRow("existing credentials 240 kept") << credentials << 240u << QStringList{} << 240u << 240u;
    QTest::newRow("existing credentials 240, typed") << credentials << 240u << QStringList{"type:300"} << 300u << 300u;
    // Above INT_MAX: shown and saved unchanged (guint32)
    QTest::newRow("existing 3000000000") << gateway << 3000000000u << QStringList{} << 3000000000u << 3000000000u;
    QTest::newRow("existing 4294967295") << gateway << 4294967295u << QStringList{} << 4294967295u << 4294967295u;
}

void GpclientWidgetTest::timeout()
{
    QFETCH(NMStringMap, data);
    QFETCH(uint, stored);
    QFETCH(QStringList, steps);
    QFETCH(uint, expectedValue);
    QFETCH(uint, expectedTimeout);

    GpclientWidget widget(makeSetting(data, stored));
    auto *spin = widget.findChild<QDoubleSpinBox *>(QStringLiteral("timeoutSpinBox"));
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
            spin->setValue(arg.toDouble());
        } else if (kind == QLatin1String("type")) {
            spin->setFocus();
            edit->selectAll();
            QTest::keyClicks(edit, arg);
        } else {
            QFAIL("unknown step");
        }
        // At least one signal. plasma-nm 5 watches only QSpinBox, so our own emit is
        // needed there. plasma-nm 6 also watches QDoubleSpinBox, so there
        // settingChanged comes twice, which is harmless.
        QVERIFY2(changed.count() > before, qPrintable(QStringLiteral("no settingChanged after ") + step));
    }

    QCOMPARE(static_cast<uint>(spin->value()), expectedValue);
    // No group separators or decimals in the text
    if (expectedValue > 1000u) {
        QCOMPARE(edit->text(), QString::number(expectedValue) + QStringLiteral(" s"));
    }
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

void GpclientWidgetTest::removesClearedKeys_data()
{
    QTest::addColumn<QString>("key");
    QTest::addColumn<QString>("editName");

    QTest::newRow("username") << "username" << "usernameLineEdit";
    QTest::newRow("dns") << "dns" << "dnsLineEdit";
    QTest::newRow("usercert") << "usercert" << "userCertLineEdit";
    QTest::newRow("userkey") << "userkey" << "userKeyLineEdit";
}

void GpclientWidgetTest::removesClearedKeys()
{
    QFETCH(QString, key);
    QFETCH(QString, editName);

    NMStringMap data{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}, {key, QStringLiteral("bob")}};
    GpclientWidget widget(makeSetting(data, 0));
    auto *edit = widget.findChild<QLineEdit *>(editName);
    QVERIFY(edit);
    QCOMPARE(edit->text(), QStringLiteral("bob"));

    // Positive: untouched, the key is saved
    NetworkManager::VpnSetting kept;
    kept.fromMap(widget.setting());
    QCOMPARE(kept.data().value(key), QStringLiteral("bob"));

    // Negative: cleared by the user, the key is gone
    edit->clear();
    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QVERIFY(!saved.data().contains(key));
    QCOMPARE(saved.data().value(QStringLiteral("gateway")), QStringLiteral("vpn.example.com"));
}

void GpclientWidgetTest::keepsPersistentAndUserName()
{
    auto setting = makeSetting({{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}}, 0);
    setting->setPersistent(true);
    setting->setUsername(QStringLiteral("alice"));
    GpclientWidget widget(setting);

    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QVERIFY(saved.persistent());
    QCOMPARE(saved.username(), QStringLiteral("alice"));
}

void GpclientWidgetTest::doesNotInventPersistentAndUserName()
{
    NetworkManager::VpnSetting::Ptr setting(new NetworkManager::VpnSetting);
    setting->setServiceType(serviceType);
    GpclientWidget widget(setting);

    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QVERIFY(!saved.persistent());
    QVERIFY(saved.username().isEmpty());
}

void GpclientWidgetTest::passwordStorage_data()
{
    QTest::addColumn<NMStringMap>("data");
    QTest::addColumn<int>("flagsIndex"); // -1: leave the combo alone
    QTest::addColumn<QString>("password");
    QTest::addColumn<QString>("expectedFlags"); // empty: no password-flags key
    QTest::addColumn<QString>("expectedOtpFlags"); // empty: no otp-flags key
    QTest::addColumn<QString>("expectedSecret"); // empty: no secret

    const NMStringMap none;
    const NMStringMap gateway{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}};
    NMStringMap notSaved = gateway;
    notSaved.insert(QStringLiteral("password-flags"), QStringLiteral("2"));

    // The combo items are: agent (1), system (0), not saved (2)
    QTest::newRow("new, default is the agent") << none << -1 << QString() << "1" << "2" << QString();
    QTest::newRow("new, agent keeps the password for NetworkManager") << none << 0 << "pw" << "1" << "2" << "pw";
    QTest::newRow("new, system") << none << 1 << "pw" << "0" << "2" << "pw";
    QTest::newRow("new, not saved drops the password") << none << 2 << "pw" << "2" << "2" << QString();
    // Negative: an existing profile is left as it is
    QTest::newRow("existing without flags stays without") << gateway << -1 << QString() << QString() << QString() << QString();
    QTest::newRow("existing without flags, changed") << gateway << 0 << QString() << "1" << QString() << QString();
    QTest::newRow("existing not saved stays") << notSaved << -1 << "pw" << "2" << QString() << QString();
    QTest::newRow("existing not saved, to system") << notSaved << 1 << "pw" << "0" << QString() << "pw";
}

void GpclientWidgetTest::passwordStorage()
{
    QFETCH(NMStringMap, data);
    QFETCH(int, flagsIndex);
    QFETCH(QString, password);
    QFETCH(QString, expectedFlags);
    QFETCH(QString, expectedOtpFlags);
    QFETCH(QString, expectedSecret);

    GpclientWidget widget(makeSetting(data, 0));
    auto *combo = widget.findChild<QComboBox *>(QStringLiteral("passwordFlagsComboBox"));
    auto *edit = widget.findChild<QLineEdit *>(QStringLiteral("passwordLineEdit"));
    QVERIFY(combo);
    QVERIFY(edit);
    if (flagsIndex >= 0) {
        combo->setCurrentIndex(flagsIndex);
    }
    // "Ask every time" takes no typing, also for a profile loaded with it
    QCOMPARE(edit->isEnabled(), combo->currentIndex() != 2);
    edit->setText(password);

    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QCOMPARE(saved.data().value(QStringLiteral("password-flags")), expectedFlags);
    QCOMPARE(saved.data().value(QStringLiteral("otp-flags")), expectedOtpFlags);
    // gpauth-result-flags follows the same rule as otp-flags
    QCOMPARE(saved.data().value(QStringLiteral("gpauth-result-flags")), expectedOtpFlags);
    QCOMPARE(saved.secrets().value(QStringLiteral("password")), expectedSecret);
}

void GpclientWidgetTest::certPassStorage_data()
{
    QTest::addColumn<NMStringMap>("data");
    QTest::addColumn<QString>("userCert");
    QTest::addColumn<int>("flagsIndex"); // -1: leave the combo alone
    QTest::addColumn<QString>("expectedFlags"); // empty: no cert-pass-flags key
    QTest::addColumn<QString>("expectedSecret"); // empty: no secret

    const NMStringMap none;
    const NMStringMap gateway{{QStringLiteral("gateway"), QStringLiteral("vpn.example.com")}};

    // The combo items are: agent (1), system (0), not saved (2)
    QTest::newRow("new, certificate, the agent keeps it") << none << "/etc/ssl/me.p12" << -1 << "1" << "pp";
    QTest::newRow("new, certificate, system") << none << "/etc/ssl/me.p12" << 1 << "0" << "pp";
    QTest::newRow("new, certificate, not saved drops it") << none << "/etc/ssl/me.p12" << 2 << "2" << QString();
    QTest::newRow("existing, certificate, kept without flags") << gateway << "/etc/ssl/me.p12" << -1 << QString() << "pp";
    // Negative: no certificate, no passphrase
    QTest::newRow("new, no certificate") << none << QString() << -1 << "1" << QString();
    QTest::newRow("existing, no certificate") << gateway << QString() << -1 << QString() << QString();
}

void GpclientWidgetTest::certPassStorage()
{
    QFETCH(NMStringMap, data);
    QFETCH(QString, userCert);
    QFETCH(int, flagsIndex);
    QFETCH(QString, expectedFlags);
    QFETCH(QString, expectedSecret);

    GpclientWidget widget(makeSetting(data, 0));
    auto *combo = widget.findChild<QComboBox *>(QStringLiteral("certPassFlagsComboBox"));
    auto *edit = widget.findChild<QLineEdit *>(QStringLiteral("certPassLineEdit"));
    auto *cert = widget.findChild<QLineEdit *>(QStringLiteral("userCertLineEdit"));
    QVERIFY(combo);
    QVERIFY(edit);
    QVERIFY(cert);
    if (flagsIndex >= 0) {
        combo->setCurrentIndex(flagsIndex);
    }
    // "Ask every time" takes no typing
    QCOMPARE(edit->isEnabled(), combo->currentIndex() != 2);
    cert->setText(userCert);
    edit->setText(QStringLiteral("pp"));

    NetworkManager::VpnSetting saved;
    saved.fromMap(widget.setting());
    QCOMPARE(saved.data().value(QStringLiteral("cert-pass-flags")), expectedFlags);
    QCOMPARE(saved.secrets().value(QStringLiteral("cert-pass")), expectedSecret);
    QCOMPARE(saved.data().value(QStringLiteral("usercert")), userCert);
    // The VPN password is a different secret
    QVERIFY(!saved.secrets().contains(QStringLiteral("password")));
}

QTEST_MAIN(GpclientWidgetTest)
#include "gpclientwidgettest.moc"

/*
    SPDX-License-Identifier: LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL
    SPDX-FileCopyrightText: 2025 GlobalProtect VPN Plugin
*/

#ifndef PLASMA_NM_GPCLIENT_MTU_SPIN_BOX_H
#define PLASMA_NM_GPCLIENT_MTU_SPIN_BOX_H

#include <QLineEdit>
#include <QSpinBox>

/* The MTU spin box. 0 is "Automatic" (special value text). A plain QSpinBox
 * does not let the user type over the special value text, and it restores the
 * old value when the text is deleted. Here digits replace "Automatic", and an
 * empty field means 0 (Automatic). Anything but digits is rejected. It has no
 * Q_OBJECT: it adds no signal or slot, so the header needs no moc run. */
class MtuSpinBox : public QSpinBox
{
public:
    explicit MtuSpinBox(QWidget *parent = nullptr)
        : QSpinBox(parent)
    {
        // The base class restores the old value when editing ends on an empty
        // field. Remember that the user emptied it, and make it 0 (Automatic).
        connect(lineEdit(), &QLineEdit::textEdited, this, [this](const QString &text) {
            m_emptied = text != specialValueText() && digitsOf(text).isEmpty() && strippedOf(text).isEmpty();
        });
        connect(this, &QAbstractSpinBox::editingFinished, this, [this]() {
            if (m_emptied) {
                m_emptied = false;
                setValue(0);
                lineEdit()->setText(specialValueText());
            }
        });
    }

    QValidator::State validate(QString &input, int &pos) const override
    {
        const QString special = specialValueText();
        // The text may come with the suffix, also when it is the special text
        const QString core = withoutAffixes(input).trimmed();
        if (!special.isEmpty() && core == special) {
            input = special;
            pos = qMin(pos, special.size());
            return QValidator::Acceptable;
        }
        // Backspace or Delete on "Automatic" while it is shown: it stays "Automatic"
        if (!special.isEmpty() && value() == minimum() && !core.isEmpty() && core.size() < special.size() && special.contains(core)) {
            input = special;
            pos = qMin(pos, special.size());
            return QValidator::Acceptable;
        }
        const QString digits = digitsOf(input);
        // Not a number (or too long for the range): reject the typed text
        if (digits.size() != strippedOf(input).size() || digits.size() > QString::number(maximum()).size()
            || (!digits.isEmpty() && digits.toInt() > maximum())) {
            return QValidator::Invalid;
        }
        if (digits.isEmpty()) {
            return QValidator::Intermediate;
        }
        // Digits typed over "Automatic" or an old text: keep only the digits
        const QString clean = prefix() + digits + suffix();
        if (input != clean) {
            input = clean;
            pos = prefix().size() + digits.size();
        }
        return QValidator::Acceptable;
    }

    int valueFromText(const QString &text) const override
    {
        const QString digits = digitsOf(text);
        return digits.isEmpty() ? 0 : qBound(minimum(), digits.toInt(), maximum());
    }

    void fixup(QString &input) const override
    {
        const QString digits = digitsOf(input);
        input = digits.isEmpty() ? specialValueText() : prefix() + digits + suffix();
    }

private:
    bool m_emptied = false;

    QString withoutAffixes(const QString &text) const
    {
        QString s = text;
        if (!suffix().isEmpty() && s.endsWith(suffix())) {
            s.chop(suffix().size());
        }
        if (!prefix().isEmpty() && s.startsWith(prefix())) {
            s.remove(0, prefix().size());
        }
        return s;
    }

    // The text without the prefix, the suffix and the special value text
    QString strippedOf(const QString &text) const
    {
        QString s = withoutAffixes(text);
        const QString special = specialValueText();
        if (!special.isEmpty() && s.startsWith(special)) {
            s.remove(0, special.size());
        } else if (!special.isEmpty() && s.endsWith(special)) {
            s.chop(special.size());
        }
        return s.trimmed();
    }

    // The digits of the text, or an empty string when there is any other character
    QString digitsOf(const QString &text) const
    {
        const QString s = strippedOf(text);
        for (const QChar c : s) {
            if (c < QLatin1Char('0') || c > QLatin1Char('9')) {
                return QString();
            }
        }
        return s;
    }
};

#endif // PLASMA_NM_GPCLIENT_MTU_SPIN_BOX_H

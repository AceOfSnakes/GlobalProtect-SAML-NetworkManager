/* Tunnel MTU (vpn.data "mtu"), shared by the GTK3 and GTK4 editors.
 *
 * 0 = automatic: no key is stored. gpclient --mtu reaches openconnect as the
 * requested MTU, which it uses ONLY when the gateway sends no MTU of its own.
 * openconnect raises values below 576 to 576, so the editors do the same.
 *
 * The same rule is in the service (parse_mtu) and in the Plasma editor. */

#ifndef NM_GPCLIENT_MTU_H
#define NM_GPCLIENT_MTU_H

#include <glib.h>

#define MTU_MIN 576
#define MTU_MAX 1500

/* The stored mtu as a number for the spin button: only ASCII whitespace is
 * stripped, the rest must be ASCII digits (leading zeros are fine).
 * Zero, missing or invalid text (sign, dot, inner space, non-ASCII digits,
 * more than MTU_MAX) gives 0 = automatic; 1..575 gives MTU_MIN. */
static inline int
nm_gpclient_parse_mtu (const char *text)
{
    const char *p;
    int mtu = 0;

    if (!text)
        return 0;
    while (g_ascii_isspace (*text))
        text++;
    for (p = text; g_ascii_isdigit (*p); p++) {
        mtu = mtu * 10 + (*p - '0');
        if (mtu > MTU_MAX)
            return 0;
    }
    if (p == text)
        return 0;
    while (g_ascii_isspace (*p))
        p++;
    if (*p != '\0')
        return 0;
    if (mtu == 0)
        return 0;
    return mtu < MTU_MIN ? MTU_MIN : mtu;
}

#endif /* NM_GPCLIENT_MTU_H */

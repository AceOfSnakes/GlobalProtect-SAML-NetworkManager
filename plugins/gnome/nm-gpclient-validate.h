/* Field checks for the connection editors (GTK3, GTK4 and Plasma).
 *
 * Plain C with no GLib, NetworkManager or Qt dependency (POSIX inet_pton
 * only): it compiles as C and as C++, like nm-gpclient-mtu.h. Every rule
 * mirrors what the service (service/nm-gpclient-service.py) accepts, so a
 * profile made with nmcli still opens and saves. A rule rejects only a value
 * that the service rejects or silently ignores. A field without a rule in the
 * service has none here (preferred-gateway, dns-domains, ...). The rules
 * check text only; they do not look at the file system.
 * tests/unit/test_validate_header.py compiles this header and tests it.
 *
 * shortcut: only ASCII white space is handled (Python \s and strip() also
 * take Unicode spaces such as U+00A0). Inside a gateway the editor then lets
 * a value through that the service rejects; at the edges of browser,
 * hip-script and dns entries, and between dns entries, it rejects a value the
 * service accepts. Handle UTF-8 spaces when a user reports one. */

#ifndef NM_GPCLIENT_VALIDATE_H
#define NM_GPCLIENT_VALIDATE_H

#include <arpa/inet.h>
#include <string.h>
#include <sys/socket.h>

/* Marks a message for xgettext (po/POTFILES.in). The GTK editors include
 * glib/gi18n-lib.h first and translate the message where they show it; here
 * (and in the Plasma editor, which shows no message) it stays as it is. */
#ifndef N_
#define N_(text) text
#endif

/* Python str.strip() / re \s for ASCII: space, \t, \n, \v, \f, \r */
static inline int
nm_gpclient_validate_is_space (char c)
{
    return c == ' ' || (c >= '\t' && c <= '\r');
}

/* A control character as in the service: \x00-\x1f and \x7f */
static inline int
nm_gpclient_validate_is_control (char c)
{
    return (unsigned char) c < 0x20 || c == 0x7f;
}

/* Skip leading white space and set *len to the length without trailing white
 * space (str.strip()). NULL gives the empty text. */
static inline const char *
nm_gpclient_validate_strip (const char *text, size_t *len)
{
    size_t n;

    if (!text)
        text = "";
    while (nm_gpclient_validate_is_space (*text))
        text++;
    n = strlen (text);
    while (n > 0 && nm_gpclient_validate_is_space (text[n - 1]))
        n--;
    *len = n;
    return text;
}

/* Text without white space and control characters (UNSAFE_GATEWAY_RE) */
static inline int
nm_gpclient_validate_is_plain (const char *text, size_t len)
{
    size_t i;

    for (i = 0; i < len; i++)
        if (nm_gpclient_validate_is_space (text[i]) || nm_gpclient_validate_is_control (text[i]))
            return 0;
    return 1;
}

/* gateway is required: mirrors _do_connect (strip; empty is "No gateway
 * specified"; a leading "-" or white space or control characters inside is
 * "Invalid gateway specified"). A host, host:port or URL all pass. */
static inline int
nm_gpclient_validate_gateway (const char *text)
{
    size_t len;

    text = nm_gpclient_validate_strip (text, &len);
    return len > 0 && text[0] != '-' && nm_gpclient_validate_is_plain (text, len);
}

/* username is optional: mirrors is_unsafe_username (not stripped; no leading
 * "-", no control characters) */
static inline int
nm_gpclient_validate_username (const char *text)
{
    size_t i;

    if (!text)
        return 1;
    if (text[0] == '-')
        return 0;
    for (i = 0; text[i]; i++)
        if (nm_gpclient_validate_is_control (text[i]))
            return 0;
    return 1;
}

/* browser is optional: mirrors resolve_browser. A friendly name, a command
 * name (BARE_BROWSER_NAME_RE) and an absolute path without white space
 * (CUSTOM_BROWSER_PATH_RE) are accepted. Any other value is silently replaced
 * by "default" there. */
static inline int
nm_gpclient_validate_browser (const char *text)
{
    size_t len, i;

    text = nm_gpclient_validate_strip (text, &len);
    if (len == 0)
        return 1;
    if (text[0] == '/')
        return nm_gpclient_validate_is_plain (text, len);
    if (!((text[0] >= '0' && text[0] <= '9') || (text[0] >= 'A' && text[0] <= 'Z')
          || (text[0] >= 'a' && text[0] <= 'z')))
        return 0;
    for (i = 1; i < len; i++) {
        char c = text[i];

        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
              || c == '.' || c == '_' || c == '+' || c == '-'))
            return 0;
    }
    return 1;
}

/* dns is optional: mirrors parse_dns_servers (entries separated by commas,
 * semicolons or white space). The service ignores an entry that is no IP
 * address, so each entry must be an IPv4 or IPv6 address. */
static inline int
nm_gpclient_validate_dns (const char *text)
{
    char entry[64];
    size_t n = 0;
    unsigned char addr[sizeof (struct in6_addr)];

    if (!text)
        return 1;
    for (;; text++) {
        int end = *text == '\0';

        if (end || *text == ',' || *text == ';' || nm_gpclient_validate_is_space (*text)) {
            if (n > 0) {
                entry[n] = '\0';
                char *zone = strchr (entry, '%');

                /* Python's ipaddress takes an IPv6 zone (fe80::1%eth0) */
                if (zone && zone[1] && !strchr (zone + 1, '%'))
                    *zone = '\0';
                else
                    zone = NULL;
                if ((zone || inet_pton (AF_INET, entry, addr) != 1) && inet_pton (AF_INET6, entry, addr) != 1)
                    return 0;
                n = 0;
            }
            if (end)
                return 1;
        } else {
            if (n >= sizeof (entry) - 1)
                return 0;
            entry[n++] = *text;
        }
    }
}

/* hip-script is optional: mirrors validate_hip_script, which wants an
 * absolute path (the owner and mode checks need the file system and stay in
 * the service). The service ignores the value while HIP is off, so the caller
 * passes only an enabled field. */
static inline int
nm_gpclient_validate_hip_script (const char *text)
{
    size_t len;

    text = nm_gpclient_validate_strip (text, &len);
    return len == 0 || text[0] == '/';
}

/* The first problem of the whole form as a message that names the field, or
 * NULL when all fields are fine. The editors show it and so disable Save. */
static inline const char *
nm_gpclient_validate_form (const char *gateway,
                           const char *username,
                           const char *browser,
                           const char *dns,
                           int hip_enabled,
                           const char *hip_script)
{
    size_t len;

    nm_gpclient_validate_strip (gateway, &len);
    if (len == 0)
        return N_("gateway is required");
    if (!nm_gpclient_validate_gateway (gateway))
        return N_("gateway must not start with '-' or contain spaces or control characters");
    if (!nm_gpclient_validate_username (username))
        return N_("username must not start with '-' or contain control characters");
    if (!nm_gpclient_validate_browser (browser))
        return N_("browser must be a name, a command name or an absolute path without spaces");
    if (!nm_gpclient_validate_dns (dns))
        return N_("dns must be IPv4 or IPv6 addresses separated by commas, semicolons or spaces");
    if (hip_enabled && !nm_gpclient_validate_hip_script (hip_script))
        return N_("hip-script must be an absolute path");
    return NULL;
}

#endif /* NM_GPCLIENT_VALIDATE_H */

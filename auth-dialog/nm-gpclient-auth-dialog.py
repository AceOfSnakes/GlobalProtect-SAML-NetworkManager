#!/usr/bin/env python3
"""
NetworkManager auth dialog for the GlobalProtect (gpclient) VPN plugin.

Speaks the standard NetworkManager VPN auth-dialog protocol used by
nm-applet / GNOME Shell:

  - invoked with:  -u UUID -n NAME -s SERVICE [-i] [-r] [-t HINT]...
  - reads connection data on stdin as DATA_KEY=/DATA_VAL= and
    SECRET_KEY=/SECRET_VAL= line pairs, terminated by a line "DONE"
  - writes requested secrets to stdout as "key\nvalue\n" pairs followed
    by an empty line, then waits for "QUIT" on stdin before exiting

Two modes:
  - no hints: upfront password request (auth-mode=credentials connections)
  - with hints: dynamic challenge from the VPN service (SecretsRequired),
    e.g. "otp" for an RSA token / one-time code, "password", "username".
    A hint prefixed with "x-vpn-message:" carries the human-readable
    prompt message from the VPN service.

SAML mode (saml-auth=dialog, hint "x-gp-saml:<JSON>"): the dialog runs gpauth in
the user's session and returns its result line as the secret "gpauth-result"
(no window of its own: gpauth opens the browser). The same line goes to
gpclient's stdin in the service. A cancelled or failed login exits with 1. The
secret "standard" means that the server has no SAML login.

External UI mode (--external-ui-mode, enabled by supports-external-ui-mode in
nm-gpclient-service.name): no window and no "key\nvalue" pairs. The dialog
prints a GKeyFile and exits at once, without waiting for QUIT:

  [VPN Plugin UI]            Version=2, Title, Description
  [<secret name>]            Value, Label, IsSecret, ShouldAsk, ForceEcho

GNOME Shell then shows its own native prompt, and "nmcli --ask" prompts on the
terminal. nmcli only asks for groups with IsSecret=true and ShouldAsk=true, so
every group has IsSecret=true. ForceEcho=true (username, one-time code) makes
only nmcli show the typed text; GNOME Shell and nm-applet mask every field.
No secret group at all means "nothing to ask".
The secrets, labels and flag rules are the same as in the GTK path, except that
a NOT_SAVED (2) password is always asked, even when a stored value comes in.
"""

import argparse
import gettext
import json
import os
import re
import selectors
import signal
import subprocess
import sys

# The catalog po/NetworkManager-gpclient.pot, installed by the package; the
# English text is used when there is no catalog for the language
_ = gettext.translation(
    "NetworkManager-gpclient", localedir="/usr/share/locale", fallback=True
).gettext


def N_(text):
    """Mark a text for xgettext; it is translated where it is shown"""
    return text


SECRET_LABELS = {
    "username": N_("Username"),
    "password": N_("Password"),
    "otp": N_("Token / one-time code"),
}

# Hints that should be shown as plain-text entries instead of masked ones
PLAIN_TEXT_HINTS = ("username", "otp")

# One-time secrets: a stored value is worse than none, because the gateway
# rejects a reused passcode and the whole login fails. Never answer these from
# what the connection happens to have saved, and never pre-fill them.
ONE_TIME_HINTS = ("otp", "gpauth-result")

# SAML in the dialog: the gpauth to run (debian/rules, Makefile and the RPM
# spec install it here) and the names the service uses
GPAUTH_BINARY = "/usr/bin/gpauth"
effective_uid = os.geteuid  # replaced in the tests
SAML_RESULT_KEY = "gpauth-result"
SAML_HINT_PREFIX = "x-gp-saml:"
SAML_STANDARD = "standard"
# Same rules as the service: nothing from the hint may become an option, a
# command or a line of its own
UNSAFE_SERVER_RE = re.compile(r"[\s\x00-\x1f\x7f]")
BROWSER_RE = re.compile(r"^(?:/[^\s\x00-\x1f\x7f]*|[A-Za-z0-9][A-Za-z0-9._+-]*)$")
# gpauth fails with the same errors as gpclient (see the service)
OPENSSL_LEGACY_RE = re.compile(
    r"unsafe legacy renegotiation disabled"
    r"|requires unsafe legacy TLS renegotiation"
    r"|--fix-openssl"
)
NON_SAML_RE = re.compile(r"non-SAML prelogin")


def read_stdin_data():
    """Read DATA_KEY/DATA_VAL and SECRET_KEY/SECRET_VAL pairs until DONE"""
    data = {}
    secrets = {}
    key = None
    is_secret = False

    for line in sys.stdin:
        line = line.rstrip("\n")
        if line == "DONE":
            break
        if line.startswith("DATA_KEY="):
            key = line[len("DATA_KEY=") :]
            is_secret = False
        elif line.startswith("DATA_VAL=") and key is not None and not is_secret:
            data[key] = line[len("DATA_VAL=") :]
            key = None
        elif line.startswith("SECRET_KEY="):
            key = line[len("SECRET_KEY=") :]
            is_secret = True
        elif line.startswith("SECRET_VAL=") and key is not None and is_secret:
            secrets[key] = line[len("SECRET_VAL=") :]
            key = None

    return data, secrets


def wait_for_quit():
    """Block until the agent sends QUIT (or closes stdin)"""
    for line in sys.stdin:
        if line.strip() == "QUIT":
            break


def output_secrets(secrets):
    """Write secrets to stdout in the auth-dialog protocol format"""
    for key, value in secrets.items():
        sys.stdout.write(f"{key}\n{value}\n")
    sys.stdout.write("\n\n")
    sys.stdout.flush()


def keyfile_escape(value):
    """Escape a value like GKeyFile does (backslash, whitespace, newlines)"""
    value = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return "\\s" + value[1:] if value.startswith(" ") else value


def output_external_ui(title, description, entries):
    """Write the external UI keyfile; entries are (key, value, label, ask, echo)"""
    lines = [
        "[VPN Plugin UI]",
        "Version=2",
        f"Description={keyfile_escape(description)}",
        f"Title={keyfile_escape(title)}",
    ]
    for key, value, label, ask, echo in entries:
        lines += [
            "",
            f"[{key}]",
            f"Value={keyfile_escape(value)}",
            f"Label={keyfile_escape(label)}",
            "IsSecret=true",
            f"ShouldAsk={str(ask).lower()}",
            f"ForceEcho={str(echo).lower()}",
        ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def secret_label(key):
    if key in SECRET_LABELS:
        return _(SECRET_LABELS[key])
    return key.replace("-", " ").capitalize()


def header_text(vpn_name, message, reprompt):
    if message:
        return message
    if reprompt:
        return (
            _("Authentication failed. Enter the credentials for “%s” again.") % vpn_name
        )
    return _("Credentials are required to connect to “%s”.") % vpn_name


def parse_saml_hint(text):
    """The checked dict of an x-gp-saml hint; ValueError for anything else"""
    try:
        saml = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("the SAML hint is not JSON") from None
    if not isinstance(saml, dict):
        raise ValueError("the SAML hint is not an object")
    server = saml.get("server")
    if (
        not isinstance(server, str)
        or not server
        or server.startswith("-")
        or UNSAFE_SERVER_RE.search(server)
    ):
        raise ValueError("the SAML hint has no valid server")
    if not isinstance(saml.get("gateway"), bool):
        raise ValueError("the SAML hint has no valid gateway flag")
    if saml.get("fix_openssl") not in ("auto", "true", "false"):
        raise ValueError("the SAML hint has no valid fix_openssl")
    browser = saml.get("browser")
    if not isinstance(browser, str) or not BROWSER_RE.match(browser):
        raise ValueError("the SAML hint has no valid browser")
    target = saml.get("browser_target")
    if target is not None and (not isinstance(target, str) or not BROWSER_RE.match(target)):
        raise ValueError("the SAML hint has no valid browser target")
    return saml


def parse_hints(raw_hints):
    """Split hints into requested secret keys, an optional message and the
    SAML request (None without one; ValueError when it is not valid)"""
    keys = []
    message = None
    saml = None
    for hint in raw_hints:
        if hint.startswith("x-vpn-message:"):
            message = hint[len("x-vpn-message:") :]
        elif hint.startswith(SAML_HINT_PREFIX):
            saml = parse_saml_hint(hint[len(SAML_HINT_PREFIX) :])
        else:
            keys.append(hint)
    return keys, message, saml


def run_gpauth_once(saml, fix_openssl):
    """Run gpauth; (exit status, stdout, stderr). SIGTERM, or the end of stdin
    (the agent is gone), stops it."""
    cmd = [GPAUTH_BINARY, "--log-format", "json"]
    if saml["gateway"]:
        cmd.append("--gateway")
    if fix_openssl:
        cmd.append("--fix-openssl")
    cmd += [f"--browser={saml['browser']}", "--", saml["server"]]
    env = dict(os.environ)
    env.pop("GP_BROWSER", None)
    if saml["browser_target"]:
        env["GP_BROWSER"] = saml["browser_target"]

    proc = subprocess.Popen(  # noqa: S603  # fixed binary, checked arguments
        cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out = {proc.stdout: [], proc.stderr: []}
    selector = selectors.DefaultSelector()
    for pipe in out:
        selector.register(pipe, selectors.EVENT_READ)
    try:
        selector.register(0, selectors.EVENT_READ)
    except (ValueError, OSError):  # no usable stdin
        pass
    try:
        open_pipes = set(out)
        while open_pipes:
            for key, _ in selector.select():
                if key.fileobj == 0:
                    if not os.read(0, 4096):  # EOF: nobody waits for the answer
                        raise SystemExit(1)
                    continue
                chunk = os.read(key.fd, 65536)
                if chunk:
                    out[key.fileobj].append(chunk)
                else:
                    selector.unregister(key.fileobj)
                    open_pipes.discard(key.fileobj)
        status = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        selector.close()
    text = [b"".join(parts).decode("utf-8", "replace") for parts in out.values()]
    return status, text[0], text[1]


def run_gpauth(saml):
    """The answer for the service: gpauth's success line, "standard" for a
    server without SAML, None when the login did not succeed"""
    retried = saml["fix_openssl"] == "true"  # already passed from the start
    fix = retried
    while True:
        try:
            status, stdout, stderr = run_gpauth_once(saml, fix)
        except OSError as e:
            print(f"Cannot run {GPAUTH_BINARY}: {e}", file=sys.stderr)
            return None
        lines = [line for line in stdout.splitlines() if line.strip()]
        if status == 0 and lines:
            try:
                result = json.loads(lines[-1])
            except (ValueError, RecursionError):
                result = None
            if isinstance(result, dict) and "success" in result:
                return lines[-1]
            print("The SAML login did not succeed", file=sys.stderr)
            return None
        if status == 1 and NON_SAML_RE.search(stderr):
            return SAML_STANDARD
        if (
            saml["fix_openssl"] == "auto"
            and not retried
            and OPENSSL_LEGACY_RE.search(stderr)
        ):
            retried = fix = True
            continue
        print(f"gpauth failed (status {status})", file=sys.stderr)
        return None


def answer_saml(args, saml, message):
    """Run gpauth and return its result as the secret; 1 when there is none"""
    if not args.allow_interaction:
        print("The SAML login needs the user and interaction is not allowed", file=sys.stderr)
        return 1
    if effective_uid() == 0:
        # an agent that runs as root: gpauth and the browser must not run as root
        print("The SAML login does not run as root; start the VPN as the desktop user",
              file=sys.stderr)
        return 1
    result = run_gpauth(saml)
    if result is None:
        return 1
    if args.external_ui_mode:
        output_external_ui(
            _("Authenticate VPN: %s") % args.name,
            header_text(args.name, message, False),
            [(SAML_RESULT_KEY, result, secret_label(SAML_RESULT_KEY), False, False)],
        )
        return 0
    output_secrets({SAML_RESULT_KEY: result})
    wait_for_quit()
    return 0


def ask_user(vpn_name, requested, message, existing_secrets, data, reprompt):
    """Show a GTK dialog asking for the requested secrets.

    Returns a dict {key: value} or None when the user cancelled.
    """
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    dialog = Gtk.Dialog(title=_("Authenticate VPN: %s") % vpn_name)
    dialog.set_modal(True)
    dialog.set_keep_above(True)
    dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
    ok_button = dialog.add_button(_("_OK"), Gtk.ResponseType.OK)
    ok_button.get_style_context().add_class("suggested-action")
    dialog.set_default_response(Gtk.ResponseType.OK)

    content = dialog.get_content_area()
    content.set_border_width(12)
    content.set_spacing(8)

    header = Gtk.Label()
    header.set_text(header_text(vpn_name, message, reprompt))
    header.set_line_wrap(True)
    header.set_max_width_chars(60)
    header.set_xalign(0)
    content.pack_start(header, False, False, 0)

    if data.get("username"):
        user_label = Gtk.Label(label=_("User: %s") % data["username"])
        user_label.set_xalign(0)
        content.pack_start(user_label, False, False, 0)

    grid = Gtk.Grid()
    grid.set_row_spacing(6)
    grid.set_column_spacing(8)
    content.pack_start(grid, False, False, 0)

    entries = {}
    for row, key in enumerate(requested):
        label = Gtk.Label(label=_("%s:") % secret_label(key))
        label.set_xalign(1)
        grid.attach(label, 0, row, 1, 1)

        entry = Gtk.Entry()
        entry.set_width_chars(24)
        entry.set_activates_default(True)
        if key not in PLAIN_TEXT_HINTS:
            entry.set_visibility(False)
        if not reprompt and key not in ONE_TIME_HINTS and existing_secrets.get(key):
            entry.set_text(existing_secrets[key])
        grid.attach(entry, 1, row, 1, 1)
        entries[key] = entry

    dialog.show_all()
    if entries:
        first_empty = next(
            (e for e in entries.values() if not e.get_text()),
            next(iter(entries.values())),
        )
        first_empty.grab_focus()

    response = dialog.run()
    result = None
    if response == Gtk.ResponseType.OK:
        result = {key: entry.get_text() for key, entry in entries.items()}
    dialog.destroy()

    # Drain pending GTK events so the dialog disappears immediately
    while Gtk.events_pending():
        Gtk.main_iteration()

    return result


def external_ui(args, data, existing_secrets, requested, message):
    """External UI mode: describe the secrets to ask, never open a window"""
    upfront = not requested  # no hints: NetworkManager wants the stored secrets
    if upfront and data.get("auth-mode", "saml") == "credentials":
        requested = ["password"]
    # A hint that cannot be a keyfile group name is not a secret name
    requested = [
        k for k in requested if k.isprintable() and not set(k) & set("[]") and k.strip()
    ]
    try:
        flags = int(data.get("password-flags", 0))
    except (TypeError, ValueError):
        flags = 0
    if upfront and flags & 4:
        requested = []  # password NOT_REQUIRED

    entries = []
    for key in requested:
        # One-time codes are never pre-filled; NOT_SAVED (2) is always asked
        fresh = args.reprompt or key in ONE_TIME_HINTS
        stored = "" if fresh else existing_secrets.get(key, "")
        ask = args.allow_interaction and (
            not stored or (key == "password" and bool(flags & 2))
        )
        entries.append((key, stored, secret_label(key), ask, key in PLAIN_TEXT_HINTS))
    description = header_text(args.name, message, args.reprompt)
    if data.get("username"):
        # The GTK window shows the account too: say whose password is asked
        description += "\n" + _("User: %s") % data["username"]
    output_external_ui(_("Authenticate VPN: %s") % args.name, description, entries)
    return 0


def main():
    parser = argparse.ArgumentParser(description="GlobalProtect VPN auth dialog")
    parser.add_argument("-u", "--uuid", required=True)
    parser.add_argument("-n", "--name", required=True)
    parser.add_argument("-s", "--service", required=True)
    parser.add_argument("-i", "--allow-interaction", action="store_true")
    parser.add_argument("-r", "--reprompt", action="store_true")
    parser.add_argument("-t", "--hint", action="append", default=[])
    parser.add_argument(
        "--external-ui-mode",
        action="store_true",
        help="Print a keyfile for the agent (GNOME Shell, nmcli) instead of a window",
    )
    args = parser.parse_args()

    if args.service != "org.freedesktop.NetworkManager.gpclient":
        print(f"Unsupported VPN service: {args.service}", file=sys.stderr)
        return 1

    # SIGTERM: leave through SystemExit, which stops gpauth (run_gpauth_once)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

    data, existing_secrets = read_stdin_data()
    try:
        requested, message, saml = parse_hints(args.hint)
    except ValueError as e:
        print(f"Invalid request from the VPN service: {e}", file=sys.stderr)
        return 1
    if saml is not None:
        return answer_saml(args, saml, message)

    if args.external_ui_mode:
        return external_ui(args, data, existing_secrets, requested, message)

    if not requested:
        # No hints: NetworkManager wants the connection's stored secrets, not
        # an answer to a challenge from the VPN service.
        #
        # SAML connections (the default) have no stored secrets at all - the
        # browser does the authentication. Exiting non-zero here is reported
        # to NetworkManager as "user canceled the secrets request", which is
        # what kept the connection editor spinning until the 25 s secrets
        # timeout expired (issue #8). Answer with an empty set instead.
        if data.get("auth-mode", "saml") != "credentials":
            output_secrets({})
            wait_for_quit()
            return 0

        # Standard login portal, but the password is explicitly not required
        # (password-flags: 4 = NOT_REQUIRED).
        try:
            not_required = int(data.get("password-flags", 0)) & 4
        except (TypeError, ValueError):
            not_required = 0
        if not_required:
            output_secrets({})
            wait_for_quit()
            return 0

        # Standard login portal: only the password is stored as a secret.
        requested = ["password"]

    # If we already have every requested secret and don't need to re-ask,
    # return them without any UI. One-time codes are always asked for.
    reusable = not args.reprompt and not any(key in ONE_TIME_HINTS for key in requested)
    if reusable and all(existing_secrets.get(key) for key in requested):
        output_secrets({key: existing_secrets[key] for key in requested})
        wait_for_quit()
        return 0

    if not args.allow_interaction:
        print("No secrets available and interaction is not allowed", file=sys.stderr)
        return 1

    try:
        result = ask_user(
            args.name, requested, message, existing_secrets, data, args.reprompt
        )
    except Exception as e:
        print(f"Failed to show auth dialog: {e}", file=sys.stderr)
        return 1

    if result is None:
        # User cancelled
        return 1

    output_secrets(result)
    wait_for_quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

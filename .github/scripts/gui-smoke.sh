#!/bin/bash
# GUI smoke test of the connection editor plugin that the upgrade test has just
# installed. Called by upgrade-test.sh in the same throw-away container, after
# the upgrade was checked:
#
#   gui-smoke.sh <scenario> <out-dir>
#
#   gnome                 opens the GTK3 editor (and the GTK4 one, where the
#                         release ships it) under Xvfb with a test connection,
#                         checks what it shows and saves a screenshot as
#                         <out-dir>/gnome-gtk<3|4>-<codename>-<arch>.png
#   plasma                load check only (ldd, plugin metadata, QPluginLoader);
#                         no screenshot, that would need a C++ harness
set -euo pipefail

SCENARIO="${1:-}"
OUT="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

case "$SCENARIO" in
    gnome | plasma) ;;
    *) fail "unknown scenario '$SCENARIO' (use: gnome, plasma)" ;;
esac
[ -n "$OUT" ] || fail "usage: gui-smoke.sh <scenario> <out-dir>"
[ "$(id -u)" = 0 ] || fail "run as root, inside a throw-away container"

# shellcheck disable=SC1091
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
ARCH="$(dpkg --print-architecture)"
mkdir -p "$OUT"

# The files of an installed package that match a pattern (first one)
file_of() {
    dpkg -L "$1" | grep -E "$2" | head -n 1 || true
}

NAME_FILE="$(file_of network-manager-gpclient '\.name$')"
[ -f "$NAME_FILE" ] || fail "network-manager-gpclient installs no .name file"

install_packages() {
    echo "::group::GUI smoke test: install $*"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "$@" > /dev/null
    echo "::endgroup::"
}

# A runtime directory for the toolkits, so they do not warn about its absence
XDG_RUNTIME_DIR="$(mktemp -d)"
export XDG_RUNTIME_DIR
trap 'rm -rf "$XDG_RUNTIME_DIR"' EXIT

run_gtk() {
    local gtk="$1" png="$OUT/gnome-gtk$1-$CODENAME-$ARCH.png"
    echo "::group::GUI smoke test: GTK$gtk editor"
    rm -f "$png"
    # No window manager and no D-Bus session: keep GTK from waiting for them
    xvfb-run -a -s "-screen 0 800x500x24" \
        env GDK_BACKEND=x11 GSK_RENDERER=cairo NO_AT_BRIDGE=1 GSETTINGS_BACKEND=memory \
        python3 "$HERE/gui_smoke_gtk.py" --gtk "$gtk" --name-file "$NAME_FILE" --screenshot "$png" \
        || fail "GTK$gtk editor smoke test"
    [ -s "$png" ] || fail "no screenshot $png"
    echo "::endgroup::"
    echo "Screenshot: $png"
}

if [ "$SCENARIO" = gnome ]; then
    # fonts and icons: without them the screenshot shows empty boxes
    install_packages xvfb xauth imagemagick fonts-dejavu-core adwaita-icon-theme \
        python3-gi gir1.2-gtk-3.0 gir1.2-nm-1.0
    run_gtk 3
    # The GTK4 editor is built only where libnma-gtk4 exists (not on 22.04)
    if [ -n "$(file_of network-manager-gpclient-gnome 'libnm-gtk4-vpn-plugin-gpclient-editor\.so$')" ]; then
        install_packages gir1.2-gtk-4.0
        run_gtk 4
    else
        echo "No GTK4 editor in network-manager-gpclient-gnome on $CODENAME: GTK4 not tested"
    fi
else
    PLASMA=network-manager-gpclient-plasma
    dpkg-query -W -f='${db:Status-Abbrev}' "$PLASMA" | grep -q '^ii' || fail "$PLASMA is not installed"
    # The same lookup as the upgrade test's: check_upgrade.py has already
    # checked that the plugin exists and where it is
    dpkg -L "$PLASMA" > "$XDG_RUNTIME_DIR/plasma-files.txt"
    PLUGIN="$(python3 "$HERE/check_upgrade.py" --print-plugin "$XDG_RUNTIME_DIR/plasma-files.txt")" \
        || fail "$PLASMA installs no plasmanetworkmanagement_gpclientui.so"
    case "$PLUGIN" in
        */qt6/*) PYQT=python3-pyqt6 ;;
        *) PYQT=python3-pyqt5 ;;
    esac
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "$PYQT" > /dev/null \
        || fail "$PYQT cannot be installed on $CODENAME/$ARCH"
    echo "::group::GUI smoke test: load $PLUGIN"
    QT_QPA_PLATFORM=offscreen python3 "$HERE/gui_smoke_plasma.py" --plugin "$PLUGIN" --name-file "$NAME_FILE" \
        || fail "Plasma plugin load check"
    echo "::endgroup::"
fi

chmod -R a+rX "$OUT"
echo "PASS: GUI smoke test $SCENARIO on $CODENAME/$ARCH"

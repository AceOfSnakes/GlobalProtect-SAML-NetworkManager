#!/bin/bash
# Installs the RPMs of a build in a fresh container of the target image and checks them.
# Exits non-zero with a message on the first failed check.
#
# Run it from the repository root, with the RPMs in <dir> and the image of the target
# (column 2 of rpm/targets), for example fedora44 on amd64:
#
#   docker run --rm --platform linux/amd64 \
#     -v "$PWD/output/fedora44-amd64:/rpms:ro" -v "$PWD/rpm/install-test.sh:/install-test.sh:ro" \
#     registry.fedoraproject.org/fedora:44 bash /install-test.sh /rpms
#
# or: rpm/build.sh --install-test fedora44 amd64 [<dir>]
#
# Usage inside the container: install-test.sh <dir with the RPMs>
set -euo pipefail

rpms=${1:-}
[ -n "$rpms" ] && [ -d "$rpms" ] || { echo "usage: install-test.sh <dir with the RPMs>" >&2; exit 2; }

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

shopt -s nullglob
files=("$rpms"/*.rpm)
[ "${#files[@]}" -eq 3 ] || fail "expected 3 RPMs in $rpms, found ${#files[@]}"

. /etc/os-release
case "${ID:-}" in
    opensuse-* | sles)
        # The RPMs are not signed here (only the repository they are published in is)
        install_cmd=(zypper --non-interactive install --no-recommends --allow-unsigned-rpm)
        ;;
    *)
        install_cmd=(dnf -y --setopt=install_weak_deps=False install)
        case "${ID:-}" in
            fedora) ;;
            *)
                # Plasma and libnma-gtk4 dependencies of EL are in EPEL and CRB
                dnf -y install dnf-plugins-core epel-release
                dnf config-manager --set-enabled crb
                ;;
        esac
        # The Cisco openh264 repository is http only and not needed
        [ "${ID:-}" != fedora ] || install_cmd+=(--disablerepo=fedora-cisco-openh264)
        ;;
esac

echo "== Installing ${files[*]##*/}"
"${install_cmd[@]}" "${files[@]}"

echo "== Checks"
for pkg in network-manager-gpclient network-manager-gpclient-gnome network-manager-gpclient-plasma; do
    rpm -q "$pkg" >/dev/null || fail "$pkg is not installed"
    # Files changed after the install: none expected (config files are not verified)
    if ! out=$(rpm -V "$pkg" 2>&1); then
        fail "rpm -V $pkg: $out"
    fi
done

for path in \
    /usr/bin/gpclient /usr/bin/gpauth /usr/bin/gpservice \
    /usr/lib/NetworkManager/nm-gpclient-service \
    /usr/lib/NetworkManager/VPN/nm-gpclient-service.name \
    /usr/libexec/gpclient/browser-wrapper /usr/libexec/gpclient/edge-wrapper \
    /usr/libexec/nm-gpclient-auth-dialog \
    /usr/share/applications/gpgui.desktop \
    /usr/lib/systemd/system/nm-gpclient.service \
    /usr/share/dbus-1/system-services/org.freedesktop.NetworkManager.gpclient.service \
    /etc/dbus-1/system.d/nm-gpclient.conf \
    /etc/vpnc/connect.d/90-gpclient-routing \
    "$(rpm --eval '%{_libdir}')/NetworkManager/libnm-vpn-plugin-gpclient.so" \
    "$(rpm --eval '%{_libdir}')/NetworkManager/libnm-vpn-plugin-gpclient-editor.so" \
    "$(rpm --eval '%{_libdir}')/NetworkManager/libnm-gtk4-vpn-plugin-gpclient-editor.so" \
    "$(rpm --eval '%{_libdir}')/qt6/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.so" \
    "$(rpm --eval '%{_libdir}')/qt6/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.json"; do
    [ -e "$path" ] || fail "missing file $path"
done

[ -L /usr/lib/libnm-gpclient-properties ] \
    || fail "/usr/lib/libnm-gpclient-properties is not a symlink"
[ -e /usr/lib/libnm-gpclient-properties ] \
    || fail "/usr/lib/libnm-gpclient-properties is a dangling symlink"
rpm -qf --qf '%{NAME}\n' /usr/lib/libnm-gpclient-properties | grep -qx network-manager-gpclient-gnome \
    || fail "libnm-gpclient-properties is not in network-manager-gpclient-gnome"

# Every program and plugin finds its shared libraries
for exe in /usr/bin/gpclient /usr/bin/gpauth /usr/bin/gpservice \
    "$(rpm --eval '%{_libdir}')"/NetworkManager/libnm-*gpclient*.so \
    "$(rpm --eval '%{_libdir}')"/qt6/plugins/plasma/network/vpn/plasmanetworkmanagement_gpclientui.so; do
    if missing=$(ldd "$exe" 2>&1 | grep 'not found'); then
        fail "$exe: missing libraries: $missing"
    fi
done

/usr/bin/gpclient --version >/dev/null || fail "gpclient --version"
/usr/bin/gpauth --version >/dev/null || fail "gpauth --version"
python3 -c 'import sdbus; print("sdbus", sdbus.__file__)' || fail "python3 cannot import sdbus"

# The service needs a D-Bus to run: byte-compile it and import it with the real sdbus
python3 - <<'PY' || fail "the service does not import"
import importlib.machinery
import importlib.util

path = "/usr/lib/NetworkManager/nm-gpclient-service"
loader = importlib.machinery.SourceFileLoader("nm_gpclient_service", path)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
PY

# The auth dialog is a GTK program: its Python module imports must resolve
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' || fail "python3 gi / GTK 3 typelib missing"

echo "OK: all checks passed"

#!/bin/bash
# Runs INSIDE the build container (started by rpm/build.sh): installs the build
# dependencies of the spec and builds the binary RPMs.
#
# Usage: container-build.sh <family> <input dir> <output dir> <rpmbuild --define NAME VALUE ...>
#   family      fedora, el or suse (column 4 of rpm/targets)
#   input dir   has SOURCES/ and SPECS/network-manager-gpclient.spec
#   output dir  receives the binary RPMs
set -euo pipefail

family=$1
indir=$2
outdir=$3
shift 3
defines=("$@")

case "$family" in
    fedora)
        # The Cisco openh264 repository (http only) is not needed: Fedora has noopenh264
        install_cmd=(dnf -y --setopt=install_weak_deps=False --disablerepo=fedora-cisco-openh264 install)
        dnf -y install rpm-build tar gzip
        ;;
    el)
        install_cmd=(dnf -y --setopt=install_weak_deps=False install)
        # Plasma 6, libnma-gtk4 and part of the build dependencies are in EPEL and CRB
        dnf -y install dnf-plugins-core epel-release
        dnf config-manager --set-enabled crb
        dnf -y install rpm-build tar gzip
        ;;
    suse)
        install_cmd=(zypper --non-interactive install --no-recommends)
        # download.opensuse.org sends each file to a mirror: one that has not synced the
        # newest metadata yet fails the refresh, the next try usually gets another one
        for attempt in 1 2 3 4 5; do
            zypper --non-interactive refresh && break
            [ "$attempt" -lt 5 ] || { echo "zypper refresh failed $attempt times" >&2; exit 1; }
            sleep 30
        done
        zypper --non-interactive install --no-recommends rpm-build tar gzip
        ;;
    *)
        echo "unknown family: $family" >&2
        exit 1
        ;;
esac

topdir=/root/rpmbuild
mkdir -p "$topdir"
cp -r "$indir/SOURCES" "$indir/SPECS" "$topdir/"
spec=$topdir/SPECS/network-manager-gpclient.spec

mapfile -t requires < <(rpmspec -q --buildrequires "${defines[@]}" "$spec")
if [ "${#requires[@]}" -eq 0 ]; then
    echo "no build dependencies found in $spec" >&2
    exit 1
fi
"${install_cmd[@]}" "${requires[@]}"

# cargo-auditable embeds the dependency list in gpclient and gpauth (the spec runs
# `cargo auditable build`). Fedora 43 and EL10 have no package for it, and Leap 16.0
# has another version: every target gets the same one (0.6.6, the one Leap 16.0
# ships: an older release, for the older rustc of some targets, e.g. 1.90 on Fedora 43).
cargo install cargo-auditable --locked --version 0.6.6 --root /usr/local

rpmbuild -bb --define "_topdir $topdir" "${defines[@]}" "$spec"

find "$topdir/RPMS" -name '*.rpm' -exec cp -v {} "$outdir/" \;

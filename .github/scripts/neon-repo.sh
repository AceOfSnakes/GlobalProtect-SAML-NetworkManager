#!/bin/bash
# Add the KDE neon "user" repository to an Ubuntu 24.04 (noble) system.
#
#   neon-repo.sh
#
# KDE neon User Edition is Ubuntu 24.04 with Plasma 6 / KF6 from
# https://archive.neon.kde.org/user (suite noble, component main). The package
# network-manager-gpclient-plasma-6 is built against it (Dockerfile.ubuntu24.04-neon)
# and tested on top of it (upgrade-test.sh, scenario neon); both call this
# script, so the repository is set up in one place.
#
# The signing key is fetched from the neon archive and must be exactly the one
# with the fingerprint below (a key with another fingerprint, or more than one
# key, stops the script before anything is written). Then the key is stored in
# a keyring that only this repository is bound to (Signed-By) and the
# repository is described in a deb822 .sources file.
#
# Needs: curl, gpg (gnupg), ca-certificates. Run as root. Does not run
# apt-get update. Test hooks: NEON_KEY_URL, NEON_KEYRING, NEON_SOURCES.
set -euo pipefail

NEON_URL="https://archive.neon.kde.org"
KEY_URL="${NEON_KEY_URL:-$NEON_URL/public.key}"
SUITE="noble"
# "Neon CI", RSA 4096, created 2015-12-18; it signs InRelease of the archive
FINGERPRINT="444DABCF3667D0283F894EDDE6D4736255751E5D"
KEYRING="${NEON_KEYRING:-/usr/share/keyrings/neon-archive-keyring.gpg}"
SOURCES="${NEON_SOURCES:-/etc/apt/sources.list.d/neon.sources}"

fail() {
    echo "neon-repo: $*" >&2
    exit 1
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

curl -fsSL --retry 3 --connect-timeout 20 --max-time 120 -o "$WORK/key.asc" "$KEY_URL" \
    || fail "cannot download the key from $KEY_URL"

# One primary key (pub), and it is the pinned one. Subkeys do not matter.
LISTING="$(gpg --batch --show-keys --with-colons "$WORK/key.asc" 2> /dev/null)" \
    || fail "$KEY_URL is not an OpenPGP key"
KEYS="$(printf '%s\n' "$LISTING" | grep -c '^pub:' || true)"
FOUND="$(printf '%s\n' "$LISTING" | awk -F: '/^fpr:/ {print $10; exit}')"
[ "$KEYS" = 1 ] || fail "$KEY_URL has $KEYS keys, expected exactly one"
[ "$FOUND" = "$FINGERPRINT" ] \
    || fail "the key from $KEY_URL has the fingerprint ${FOUND:-none}, expected $FINGERPRINT"

gpg --batch --yes --dearmor -o "$WORK/keyring.gpg" "$WORK/key.asc" || fail "cannot convert the key"
mkdir -p "$(dirname "$KEYRING")" "$(dirname "$SOURCES")"
install -m 644 "$WORK/keyring.gpg" "$KEYRING"
printf '%s\n' \
    "Types: deb" \
    "URIs: $NEON_URL/user" \
    "Suites: $SUITE" \
    "Components: main" \
    "Signed-By: $KEYRING" \
    > "$SOURCES"
chmod 644 "$SOURCES"
echo "KDE neon repository added: $SOURCES (key $FINGERPRINT)"

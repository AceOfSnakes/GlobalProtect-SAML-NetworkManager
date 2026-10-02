#!/bin/bash
# Upgrade test: install the newest RELEASED packages from the public apt
# repository, then upgrade to the .deb files built from this commit and check
# that nothing is kept back, removed or left half-configured.
#
# Run as root inside a fresh ubuntu:<version> container (see
# .github/workflows/build-release.yml; never on a developer's machine):
#
#   upgrade-test.sh <scenario> [debs-dir [gui-out-dir]]
#
# Scenarios:
#   gnome       install network-manager-gpclient-gnome from the release
#   plasma      install network-manager-gpclient-plasma from the release; the
#               editor plugin must be in the Qt directory of the Ubuntu release
#               (qt5 on 22.04 and 24.04, qt6 on 24.10 and 26.04) afterwards
#
# With a gui-out-dir, a GUI smoke test of the installed editor plugin follows
# the successful upgrade (gui-smoke.sh; screenshots go to that directory).
#
# When the released repository has no such package for this Ubuntu release and
# architecture (e.g. 24.10 and arm64 had no 1.4.1 builds), there is nothing to
# upgrade from: the package is installed fresh from this build and checked the
# same way (check_upgrade.py --fresh).
set -euo pipefail

SCENARIO="${1:-}"
DEBS_DIR="${2:-/debs}"
GUI_OUT="${3:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://wmp.github.io/GlobalProtect-SAML-NetworkManager"
KEYRING="/usr/share/keyrings/gpclient-archive-keyring.gpg"
LOCAL_REPO="/repo"
LOCAL_LIST="/etc/apt/sources.list.d/gpclient-local.list"
RELEASED_LIST="/etc/apt/sources.list.d/gpclient.list"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export DEBIAN_FRONTEND=noninteractive

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

case "$SCENARIO" in
    gnome | plasma) ;;
    *) fail "unknown scenario '$SCENARIO' (use: gnome, plasma)" ;;
esac
[ "$(id -u)" = 0 ] || fail "run as root, inside a throw-away container"
[ -d "$DEBS_DIR" ] || fail "no such directory: $DEBS_DIR"

# Containers have no init system: do not let postinst scripts start services
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d

# Ubuntu releases that are end of life: their archive moved to
# old-releases.ubuntu.com. Keep in sync with the Dockerfile.ubuntu<version> that
# write the same ubuntu.sources (tests/unit checks it).
EOL_CODENAMES="oracular"
OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
case " $EOL_CODENAMES " in
    *" $OS_CODENAME "*)
        echo "$OS_CODENAME is end of life: using old-releases.ubuntu.com"
        : > /etc/apt/sources.list
        printf '%s\n' \
            "Types: deb" \
            "URIs: http://old-releases.ubuntu.com/ubuntu/" \
            "Suites: $OS_CODENAME $OS_CODENAME-updates $OS_CODENAME-backports $OS_CODENAME-security" \
            "Components: main universe restricted multiverse" \
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" \
            > /etc/apt/sources.list.d/ubuntu.sources
        ;;
esac

echo "::group::Prepare ($SCENARIO)"
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl lsb-release dpkg-dev python3 > /dev/null
CODENAME="$(lsb_release -cs)"
ARCH="$(dpkg --print-architecture)"
echo "::endgroup::"

# The released repository may have no suite for this Ubuntu release at all (1.4.1
# had no 24.10 builds). Only a 404 means that (returns 1); any other answer or a
# network error fails the test, so a broken network is never taken for "nothing
# to upgrade from".
check_released_suite() {
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 20 --max-time 120 \
        "$REPO_URL/dists/$CODENAME/Release")" || code="000"
    case "$code" in
        200) return 0 ;;
        404) return 1 ;;
        *) fail "cannot read $REPO_URL/dists/$CODENAME/Release (HTTP $code)" ;;
    esac
}

# candidate <package>: the version apt would install, empty if there is none
candidate() {
    local version
    version="$(apt-cache policy "$1" | awk '/Candidate:/ {print $2}')"
    [ "$version" = "(none)" ] && version=""
    echo "$version"
}

case "$SCENARIO" in
    gnome) PACKAGE=network-manager-gpclient-gnome ;;
    plasma) PACKAGE=network-manager-gpclient-plasma ;;
esac

# upgrade_possible: adds the released repository, exactly as README.md
# "Installation" does, and succeeds when it has $PACKAGE for this release and
# architecture. Fails (returns 1) when it has no suite or no such package.
upgrade_possible() {
    check_released_suite || return 1
    curl -fsSL "$REPO_URL/gpclient-archive-keyring.gpg" > "$KEYRING"
    echo "deb [arch=$ARCH signed-by=$KEYRING] $REPO_URL $CODENAME main" > "$RELEASED_LIST"
    apt-get update -qq
    [ -n "$(candidate "$PACKAGE")" ]
}

# The packages of this build for this release and architecture as a local
# apt repository; sets EXPECTED to their version
prepare_local_repo() {
    echo "::group::Local repository from $DEBS_DIR"
    mkdir -p "$LOCAL_REPO"
    shopt -s nullglob
    for deb in "$DEBS_DIR"/*~"${CODENAME}"1*_"${ARCH}".deb "$DEBS_DIR"/*."${CODENAME}"1*_"${ARCH}".deb; do
        cp "$deb" "$LOCAL_REPO/"
    done
    shopt -u nullglob
    chmod -R a+rX "$LOCAL_REPO"
    ls "$LOCAL_REPO"/*.deb > /dev/null 2>&1 || fail "no .deb for $CODENAME/$ARCH in $DEBS_DIR"
    EXPECTED="$(for deb in "$LOCAL_REPO"/*.deb; do dpkg-deb -f "$deb" Version; done | sort -u)"
    [ "$(echo "$EXPECTED" | wc -l)" = 1 ] || fail "the packages have more than one version: $(echo "$EXPECTED" | tr '\n' ' ')"
    echo "New version: $EXPECTED"
    (cd "$LOCAL_REPO" && dpkg-scanpackages . /dev/null > Packages)
    echo "deb [trusted=yes] file:$LOCAL_REPO ./" > "$LOCAL_LIST"
    apt-get update -qq
    echo "::endgroup::"
}

# check_newer <built> <released>: apt installs the build over the release only
# when its version is greater
check_newer() {
    dpkg --compare-versions "$1" gt "$2" \
        || fail "the build $1 is not newer than the released $2: the upgrade would not install it"
}

status() {
    dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\n' 'network-manager-gpclient*'
}

if upgrade_possible; then
    echo "::group::Install the released $PACKAGE"
    apt-get install -y --no-install-recommends "$PACKAGE"
    echo "::endgroup::"
    status > "$WORK/before.txt"
    echo "Installed before the upgrade:"
    cat "$WORK/before.txt"

    prepare_local_repo
    RELEASED="$(dpkg-query -W -f='${Version}' network-manager-gpclient)"
    check_newer "$EXPECTED" "$RELEASED"
    # Informational: apt-get upgrade keeps back what needs new packages
    echo "apt-get -s upgrade:"
    apt-get -s upgrade | grep -E "gpclient|^[0-9]+ upgraded" || true

    echo "::group::apt upgrade"
    apt upgrade -y --no-install-recommends
    echo "::endgroup::"
    MODE=upgrade
    MODE_ARGS=(--before "$WORK/before.txt")
else
    echo "No released $PACKAGE for $CODENAME/$ARCH: fresh install of this build instead"
    # only this build: not the released packages of other names
    rm -f "$RELEASED_LIST"
    prepare_local_repo
    echo "::group::Install $PACKAGE from this build"
    apt-get install -y --no-install-recommends "$PACKAGE"
    echo "::endgroup::"
    MODE=fresh
    MODE_ARGS=(--fresh)
fi

status > "$WORK/after.txt"
echo "Installed after the $MODE:"
cat "$WORK/after.txt"

# check_upgrade.py checks the packages and, for Plasma, the editor plugin: it is
# listed by dpkg -L, exists and is in the Qt directory of the release
PLASMA_FILES=()
if [ "$SCENARIO" = plasma ]; then
    dpkg -L network-manager-gpclient-plasma > "$WORK/plasma-files.txt" \
        || fail "network-manager-gpclient-plasma is not installed after the $MODE"
    PLASMA_FILES=(--plasma-files "$WORK/plasma-files.txt")
fi
python3 "$HERE/check_upgrade.py" "${MODE_ARGS[@]}" --after "$WORK/after.txt" \
    --expected-version "$EXPECTED" --scenario "$SCENARIO" --codename "$CODENAME" "${PLASMA_FILES[@]}"

echo "PASS: $SCENARIO $MODE on $CODENAME/$ARCH to $EXPECTED"
if [ -n "$GUI_OUT" ]; then
    bash "$HERE/gui-smoke.sh" "$SCENARIO" "$GUI_OUT"
fi

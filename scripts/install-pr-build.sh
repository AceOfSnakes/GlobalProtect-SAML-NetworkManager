#!/bin/bash
# Install the test packages of a pull request on this machine.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/WMP/GlobalProtect-SAML-NetworkManager/main/scripts/install-pr-build.sh) <PR number>
#   install-pr-build.sh <PR number> [--desktop gnome|plasma] [--yes]
#
# CI publishes every open pull request as the prerelease "pr-<N>" (see
# .github/workflows/pr-report.yml) and removes it when the PR closes. This picks
# the .deb files for your Ubuntu release, architecture and desktop from that
# release and hands them to apt. apt asks for confirmation unless --yes is given.
#
# Run it as `bash <(curl ...)`, not `curl ... | bash`: apt asks its question on
# the terminal, and a pipe would take stdin away.
#
# Test hooks: OS_RELEASE_FILE (default /etc/os-release) and GP_RELEASE_API
# (default https://api.github.com/repos/WMP/GlobalProtect-SAML-NetworkManager).

set -euo pipefail

REPO="WMP/GlobalProtect-SAML-NetworkManager"
API_BASE="${GP_RELEASE_API:-https://api.github.com/repos/$REPO}"
OS_RELEASE="${OS_RELEASE_FILE:-/etc/os-release}"

die() { echo "install-pr-build: $*" >&2; exit 1; }

usage() {
    cat >&2 <<'EOF'
usage: install-pr-build.sh <PR number> [--desktop gnome|plasma] [--yes]

  <PR number>          the pull request whose test packages to install
  --desktop gnome|plasma
                       which editor plugin to install (default: plasma when
                       XDG_CURRENT_DESKTOP mentions KDE, otherwise gnome)
  --yes                do not ask apt for confirmation (apt install -y)
EOF
}

# --- Arguments ---------------------------------------------------------------

pr=""
desktop=""
assume_yes=0
positional=0

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --yes) assume_yes=1 ;;
        --desktop)
            [ $# -ge 2 ] || { usage; die "--desktop needs a value: gnome or plasma"; }
            desktop="$2"; shift ;;
        --desktop=*) desktop="${1#--desktop=}" ;;
        --*) usage; die "unknown option: $1" ;;
        *)
            positional=$((positional + 1))
            pr="$1" ;;
    esac
    shift
done

[ "$positional" -ge 1 ] || { usage; die "missing the pull request number"; }
[ "$positional" -eq 1 ] || { usage; die "expected one pull request number, got $positional arguments"; }

# Digits only, no leading zero: the number ends up in a URL and a release tag
case "$pr" in
    [1-9]|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9]|\
    [1-9][0-9][0-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) die "'$pr' is not a pull request number (1 to 7 digits, no leading zero)" ;;
esac

case "$desktop" in
    ""|gnome|plasma) ;;
    *) die "--desktop must be gnome or plasma, not '$desktop'" ;;
esac

# --- This machine ------------------------------------------------------------

# Read the keys one by one: os-release is not sourced, so nothing in it runs
os_value() {
    local value
    value="$(sed -n "s/^$1=//p" "$OS_RELEASE" | tail -n 1)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "$value"
}

[ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE: this script supports Ubuntu only"
[ "$(os_value ID)" = "ubuntu" ] \
    || die "this is '$(os_value ID)', not Ubuntu: the test packages are built for Ubuntu 22.04, 24.04, 24.10 and 26.04 only"

version_id="$(os_value VERSION_ID)"
codename="$(os_value VERSION_CODENAME)"
if [ -z "$codename" ]; then
    case "$version_id" in
        22.04) codename=jammy ;;
        24.04) codename=noble ;;
        24.10) codename=oracular ;;
        26.04) codename=resolute ;;
    esac
fi
# Same suites as .github/scripts/build-apt-repo.sh
case "$codename" in
    jammy|noble|oracular|resolute) ;;
    *) die "Ubuntu ${version_id:-?} (${codename:-unknown}) is not supported: the test packages are built for jammy (22.04), noble (24.04), oracular (24.10) and resolute (26.04)" ;;
esac

arch="$(dpkg --print-architecture)"
case "$arch" in
    amd64|arm64) ;;
    *) die "architecture '$arch' is not supported: the test packages are built for amd64 and arm64" ;;
esac

if [ -z "$desktop" ]; then
    case "${XDG_CURRENT_DESKTOP:-}" in
        *[Kk][Dd][Ee]*) desktop=plasma ;;
        *) desktop=gnome ;;
    esac
fi

# --- The release -------------------------------------------------------------

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# apt downloads as the unprivileged _apt user and complains about a private dir
chmod 755 "$tmp"

tag="pr-$pr"
url="$API_BASE/releases/tags/$tag"
echo "Looking for the test packages of PR #$pr ($url)"
status="$(curl -sSL -H 'Accept: application/vnd.github+json' \
    --connect-timeout 20 --max-time 120 \
    -o "$tmp/release.json" -w '%{http_code}' "$url")" \
    || die "cannot reach GitHub ($url)"

case "$status" in
    200) ;;
    404) die "there is no release '$tag': the build of PR #$pr may still be running or have failed, or the PR is closed (its packages are removed then). The PR's CI report comment shows the state." ;;
    *) die "GitHub answered HTTP $status for $url (rate limit? try again later)" ;;
esac

# Package names: <package>_<version>~<codename>1_<arch>.deb. GitHub stores the
# "~" of a release asset as "." (1.4.2-1.noble1), so both are accepted. The
# names come from the release listing and are matched strictly before they are
# used anywhere.
selection="$(python3 - "$tmp/release.json" "$codename" "$arch" "$desktop" <<'PY'
import json
import re
import sys

path, codename, arch, desktop = sys.argv[1:5]
try:
    with open(path, encoding="utf-8") as handle:
        assets = json.load(handle).get("assets", [])
except (OSError, ValueError, AttributeError):
    sys.exit("the release answer is not valid JSON")
if not isinstance(assets, list):
    sys.exit("the release answer has no asset list")


def find(package):
    pattern = re.compile(
        re.escape(package) + r"_[0-9][A-Za-z0-9.+-]*[~.]" + re.escape(codename)
        + r"[0-9]+_" + re.escape(arch) + r"\.deb"
    )
    found = []
    for asset in assets:
        name = asset.get("name") if isinstance(asset, dict) else None
        url = asset.get("browser_download_url") if isinstance(asset, dict) else None
        if isinstance(name, str) and isinstance(url, str) and pattern.fullmatch(name):
            found.append((name, url))
    if len(found) > 1:
        sys.exit("more than one %s package for %s/%s in the release" % (package, codename, arch))
    return found[0] if found else None


wanted = ["network-manager-gpclient"]
if desktop == "gnome":
    wanted.append("network-manager-gpclient-gnome")
else:
    # Plasma 6 where the release has it (24.10, 26.04), Plasma 5 otherwise
    wanted.append(
        "network-manager-gpclient-plasma-6" if find("network-manager-gpclient-plasma-6")
        else "network-manager-gpclient-plasma-5"
    )

for package in wanted:
    asset = find(package)
    if asset is None:
        sys.exit("the release has no %s package for %s/%s (the build of this PR may have failed or is not finished)"
                 % (package, codename, arch))
    print("%s\t%s\t%s" % (package, asset[0], asset[1]))
PY
)" || die "cannot pick the packages for Ubuntu $codename/$arch from release $tag (see the message above)"

[ -n "$selection" ] || die "no packages selected from release $tag"

echo
echo "Ubuntu ${version_id:-?} ($codename), $arch, desktop: $desktop"
echo "Unreviewed test build of PR #$pr, installed as root. Packages:"
debs=()
packages=()
while IFS=$'\t' read -r package name asset_url; do
    # With GP_RELEASE_API set (tests, mirrors) any URL is fine; by default only
    # GitHub's own download address is trusted
    if [ -z "${GP_RELEASE_API:-}" ]; then
        case "$asset_url" in
            "https://github.com/$REPO/releases/download/$tag/$name") ;;
            *) die "unexpected download address for $name: $asset_url" ;;
        esac
    fi
    echo "  $name"
    curl -fsSL --connect-timeout 20 --max-time 600 -o "$tmp/$name" "$asset_url" \
        || die "cannot download $name"
    chmod 644 "$tmp/$name"
    debs+=("$tmp/$name")
    packages+=("$package")
done <<< "$selection"

# --- Install -----------------------------------------------------------------

if [ "$codename" = "jammy" ]; then
    cat <<'EOF'

Ubuntu 22.04 only: python3-sdbus is not in apt, install it with pip first
(if you have not yet):
    pip3 install sdbus
EOF
fi

apt_args=(install)
[ "$assume_yes" -eq 1 ] && apt_args+=(-y)

runner=()
[ "$(id -u)" -eq 0 ] || runner=(sudo)

echo
echo "Running: ${runner[*]:+${runner[*]} }apt ${apt_args[*]} <the packages above>"
"${runner[@]}" apt "${apt_args[@]}" "${debs[@]}" || die "apt failed, nothing more was done"

cat <<EOF

Installed.

Back to the released version, with the apt repository set up as described in
docs/APT_REPO.md:
    sudo apt update && sudo apt install --reinstall --allow-downgrades ${packages[*]}
Without the apt repository: sudo apt remove ${packages[*]}
and install the released packages again (https://github.com/$REPO/releases).
EOF

#!/bin/bash
# Build a (signed) RPM repository from a directory of binary .rpm files.
#
#   build-rpm-repo.sh [--index FILE] [--allow-empty] <incoming-dir> <output-dir> [gpg-key-id]
#
# The repository is always rebuilt from scratch - GitHub Releases are the source
# of truth, this tree is a derived artifact. Without a key id the repository is
# left unsigned, which is only useful for local testing. The secret key is never
# an argument: gpg and rpmsign find it in the keyring of the GNUPGHOME of the
# environment (the workflow imports it there). Source RPMs are release assets
# but do not belong to a binary repository and are skipped.
#
# Which target (rpm/targets) an RPM belongs to follows from the dist tag in its
# file name: network-manager-gpclient-gnome-1.5.0-1.fc44.x86_64.rpm is for
# fedora44 (.fc44) and x86_64. A well-formed name with a dist tag no target has
# (a target that was dropped) is skipped with a warning, and so is any other
# name that is not one of our three packages.
#
# Layout produced (every RPM signed with rpmsign when there is a key):
#   <target>/<x86_64|aarch64>/*.rpm + repodata/{repomd.xml,repomd.xml.asc}
#   <target>/network-manager-gpclient.repo      (gpgcheck=1, repo_gpgcheck=1)
#   RPM-GPG-KEY-gpclient                        (armored public key)
#
# --index FILE: the landing page the apt repository script wrote. The RPM
#   version rows go into it, and the RPM sections are removed from it when there
#   is no RPM at all, so no placeholder is left on the page.
# --allow-empty: no RPM files is not an error: nothing is built and only the
#   index is cleaned up (the first releases had no RPMs).
#
# The repository does not grow forever (GitHub Pages is limited to 1 GB): per
# target, architecture and package only the newest RPM_KEEP_VERSIONS versions
# are published (default 2: the current one and the previous one, to go back
# to), ordered like rpm orders versions (version, then release). The older
# files are logged and left out.
#
# The .repo file of the Fedora targets and of Leap names its directory with
# $releasever, so a release upgrade (dnf system-upgrade --releasever=45) follows
# to the packages built for the new release; EL 10 and Tumbleweed have one
# directory. skip_if_unavailable=1 keeps dnf working when a target is dropped
# from rpm/targets later and its directory is gone.
#
# Environment: BASE_URL (the address of the output directory, default: this
# repository's GitHub Pages /rpm), RPM_TARGETS_FILE (default: rpm/targets),
# RPM_KEEP_VERSIONS (default 2).
#
# Requires: createrepo_c, rpmsign (package rpm) for a signed repository, gnupg,
# python3 (the ordering of versions).

set -euo pipefail

USAGE="usage: build-rpm-repo.sh [--index FILE] [--allow-empty] <incoming-dir> <output-dir> [gpg-key-id]"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGETS_FILE="${RPM_TARGETS_FILE:-$SCRIPT_DIR/../../rpm/targets}"
BASE_URL="${BASE_URL:-https://wmp.github.io/GlobalProtect-SAML-NetworkManager/rpm}"
KEEP_VERSIONS="${RPM_KEEP_VERSIONS-2}"
KEY_FILE="RPM-GPG-KEY-gpclient"
REPO_FILE="network-manager-gpclient.repo"
BEGIN_MARK="<!-- RPM-BEGIN -->"
END_MARK="<!-- RPM-END -->"

log() { echo "[build-rpm-repo] $*"; }
die() { echo "[build-rpm-repo] ERROR: $*" >&2; exit 1; }

INDEX=""
ALLOW_EMPTY=0
positional=()
while [ $# -gt 0 ]; do
    case "$1" in
        --index) [ $# -ge 2 ] || die "--index needs a file. $USAGE"; INDEX="$2"; shift ;;
        --allow-empty) ALLOW_EMPTY=1 ;;
        --*) die "unknown option: $1. $USAGE" ;;
        *) positional+=("$1") ;;
    esac
    shift
done
[ "${#positional[@]}" -ge 2 ] && [ "${#positional[@]}" -le 3 ] || die "$USAGE"
INCOMING="${positional[0]}"
OUTDIR="${positional[1]}"
GPG_KEY="${positional[2]:-}"

[[ "$KEEP_VERSIONS" =~ ^[1-9][0-9]{0,3}$ ]] \
    || die "RPM_KEEP_VERSIONS must be a number from 1 to 9999, not '$KEEP_VERSIONS'"

# A key id is a fingerprint or key id: hex digits (it ends up in rpmsign and gpg arguments)
if [ -n "$GPG_KEY" ] && ! [[ "$GPG_KEY" =~ ^[0-9A-Fa-f]{8,64}$ ]]; then
    die "the key id must be a fingerprint or key id (hex digits), not '$GPG_KEY'"
fi

[ -d "$INCOMING" ] || die "incoming directory does not exist: $INCOMING"
[ -f "$TARGETS_FILE" ] || die "the list of targets does not exist: $TARGETS_FILE"
if [ -n "$INDEX" ] && [ ! -f "$INDEX" ]; then
    die "the index file does not exist: $INDEX"
fi

# --- Targets: dist tag -> target, in the order of the file -------------------

declare -A target_of_dist=()
declare -A family_of=()
targets=()
while read -r target _image dist family; do
    case "$target" in ""|"#"*) continue ;; esac
    [ -n "${dist:-}" ] || die "$TARGETS_FILE: no dist tag for target $target"
    [[ "$target" =~ ^[a-z0-9][a-z0-9.]*$ && "$dist" =~ ^\.[a-z]+[0-9]*$ ]] \
        || die "$TARGETS_FILE: unexpected target or dist tag: '$target' '$dist'"
    target_of_dist["$dist"]="$target"
    family_of["$target"]="${family:-}"
    targets+=("$target")
done < "$TARGETS_FILE"
[ "${#targets[@]}" -gt 0 ] || die "$TARGETS_FILE has no targets"

# --- Which RPM belongs where ---------------------------------------------------

shopt -s nullglob
rpms=("$INCOMING"/*.rpm)
shopt -u nullglob

name_re='^(network-manager-gpclient(-gnome|-plasma)?)-([0-9][A-Za-z0-9.+~_]*)-([0-9]+)(\.[a-z]+[0-9]*)\.(x86_64|aarch64)\.rpm$'
cand_file=(); cand_target=(); cand_arch=(); cand_package=(); cand_version=(); cand_revision=()
for rpm in "${rpms[@]}"; do
    file="$(basename "$rpm")"
    case "$file" in *.src.rpm) log "skipping the source package $file"; continue ;; esac
    # A name that is not one of ours is skipped, not fatal: one odd asset in any
    # release must not stop the publication (the apt repository is built in the
    # same job). Nothing of it is copied or passed to a tool.
    if ! [[ "$file" =~ $name_re ]]; then
        log "WARNING: unexpected name of an RPM, not published: ${file//[^A-Za-z0-9._+~-]/?}"
        continue
    fi
    dist="${BASH_REMATCH[5]}"
    target="${target_of_dist[$dist]:-}"
    if [ -z "$target" ]; then
        log "WARNING: $file has the dist tag $dist, which no target in rpm/targets has: not published"
        continue
    fi
    cand_file+=("$rpm"); cand_target+=("$target"); cand_arch+=("${BASH_REMATCH[6]}")
    cand_package+=("${BASH_REMATCH[1]}"); cand_version+=("${BASH_REMATCH[3]}"); cand_revision+=("${BASH_REMATCH[4]}")
done

# Which candidates are too old to keep, and the newest core version per target.
# The names were matched above: nothing but package names, versions and
# numbers goes to python.
prune_and_newest() {
    local i
    for i in "${!cand_file[@]}"; do
        echo "$i ${cand_target[$i]} ${cand_arch[$i]} ${cand_package[$i]} ${cand_version[$i]} ${cand_revision[$i]}"
    done | python3 -c '
import sys
from functools import cmp_to_key

def rpmvercmp(a, b):
    """rpm\x27s ordering of two version (or release) strings"""
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and not a[i].isalnum() and a[i] not in "~^":
            i += 1
        while j < len(b) and not b[j].isalnum() and b[j] not in "~^":
            j += 1
        ca = a[i] if i < len(a) else ""
        cb = b[j] if j < len(b) else ""
        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            j += 1
            continue
        if ca == "^" or cb == "^":
            if not ca:
                return -1
            if not cb:
                return 1
            if ca != "^":
                return 1
            if cb != "^":
                return -1
            i += 1
            j += 1
            continue
        if not ca or not cb:
            break
        number = ca.isdigit()
        test = str.isdigit if number else str.isalpha
        si = i
        while i < len(a) and test(a[i]):
            i += 1
        sj = j
        while j < len(b) and test(b[j]):
            j += 1
        left, right = a[si:i], b[sj:j]
        if not right:
            return 1 if number else -1
        if number:
            left, right = left.lstrip("0"), right.lstrip("0")
            if len(left) != len(right):
                return 1 if len(left) > len(right) else -1
        if left != right:
            return 1 if left > right else -1
    if i >= len(a) and j >= len(b):
        return 0
    return -1 if i >= len(a) else 1

def compare(x, y):
    return rpmvercmp(x[0], y[0]) or rpmvercmp(x[1], y[1])

keep = int(sys.argv[1])
groups, newest = {}, {}
for line in sys.stdin:
    index, target, arch, package, version, revision = line.split()
    groups.setdefault((target, arch, package), []).append((int(index), (version, revision)))
for (target, arch, package), items in groups.items():
    items.sort(key=cmp_to_key(lambda x, y: compare(y[1], x[1])))  # newest first
    for index, _ in items[keep:]:
        print("prune", index)
    if package == "network-manager-gpclient":
        best = items[0][1]
        if target not in newest or compare(best, newest[target]) > 0:
            newest[target] = best
for target, (version, _revision) in newest.items():
    print("newest", target, version)
' "$KEEP_VERSIONS"
}

selected=()     # files to publish
dir_of=()       # <target>/<rpm arch> of each, same index
declare -A newest=()    # target -> newest version of the core package
declare -A pruned=()
if [ "${#cand_file[@]}" -gt 0 ]; then
    while read -r kind first second; do
        case "$kind" in
            prune) pruned["$first"]=1 ;;
            newest) newest["$first"]="$second" ;;
        esac
    done < <(prune_and_newest)
fi
for i in "${!cand_file[@]}"; do
    if [ -n "${pruned[$i]:-}" ]; then
        log "pruned $(basename "${cand_file[$i]}"): not among the newest $KEEP_VERSIONS versions of ${cand_package[$i]} for ${cand_target[$i]}/${cand_arch[$i]}"
        continue
    fi
    selected+=("${cand_file[$i]}")
    dir_of+=("${cand_target[$i]}/${cand_arch[$i]}")
done

# --- The landing page ----------------------------------------------------------

# Replace the placeholder of the RPM sections, or take the sections out
update_index() {
    [ -n "$INDEX" ] || return 0
    local rows="" target label
    for target in "${targets[@]}"; do
        [ -n "${newest[$target]:-}" ] || continue
        case "$target" in
            fedora*) label="Fedora ${target#fedora}" ;;
            el*) label="EL ${target#el}" ;;
            leap*) label="openSUSE Leap ${target#leap}" ;;
            tumbleweed) label="openSUSE Tumbleweed" ;;
            *) label="$target" ;;
        esac
        rows="$rows<tr><td>$label</td><td><code>$target</code></td><td><code>${newest[$target]}</code></td></tr>"
    done
    if [ "${#selected[@]}" -gt 0 ]; then
        sed -e "/$BEGIN_MARK/d" -e "/$END_MARK/d" -e "s|__RPM_VERSION_ROWS__|$rows|g" "$INDEX" > "$INDEX.tmp"
    else
        sed -e "/$BEGIN_MARK/,/$END_MARK/d" "$INDEX" > "$INDEX.tmp"
    fi
    mv "$INDEX.tmp" "$INDEX"
}

if [ "${#selected[@]}" -eq 0 ]; then
    if [ "$ALLOW_EMPTY" -eq 1 ]; then
        update_index
        log "no RPM packages: no RPM repository built"
        exit 0
    fi
    die "no binary RPM files for a target of rpm/targets found in $INCOMING"
fi

command -v createrepo_c >/dev/null || die "createrepo_c is required"
if [ -n "$GPG_KEY" ]; then
    command -v gpg >/dev/null || die "gpg is required for a signed repository"
    command -v rpmsign >/dev/null || die "rpmsign (package rpm) is required for a signed repository"
fi

# --- Copy, sign, index -----------------------------------------------------------

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
for i in "${!selected[@]}"; do
    mkdir -p "$OUTDIR/${dir_of[$i]}"
    cp "${selected[$i]}" "$OUTDIR/${dir_of[$i]}/"
done

sign_args=()
if [ -n "$GPG_KEY" ]; then
    sign_args=(--define "_gpg_name $GPG_KEY")
    # Where the key is: the keyring of the environment's GNUPGHOME
    [ -z "${GNUPGHOME:-}" ] || sign_args+=(--define "_gpg_path $GNUPGHOME")
fi

mapfile -t repo_dirs < <(printf '%s\n' "${dir_of[@]}" | sort -u)
for dir in "${repo_dirs[@]}"; do
    path="$OUTDIR/$dir"
    count=$(find "$path" -maxdepth 1 -name '*.rpm' | wc -l)
    if [ -n "$GPG_KEY" ]; then
        # Every package, not only the metadata: gpgcheck=1 checks each RPM
        rpmsign --addsign "${sign_args[@]}" "$path"/*.rpm >/dev/null
    fi
    createrepo_c --quiet "$path"
    [ -f "$path/repodata/repomd.xml" ] || die "createrepo_c did not create $dir/repodata/repomd.xml"
    if [ -n "$GPG_KEY" ]; then
        gpg --batch --yes --local-user "$GPG_KEY" --detach-sign --armor "$path/repodata/repomd.xml"
        [ -f "$path/repodata/repomd.xml.asc" ] || die "no signature of $dir/repodata/repomd.xml"
    fi
    log "$dir: $count package(s)"
done

if [ -n "$GPG_KEY" ]; then
    gpg --batch --yes --export --armor "$GPG_KEY" > "$OUTDIR/$KEY_FILE"
    [ -s "$OUTDIR/$KEY_FILE" ] || die "gpg exported no public key for $GPG_KEY"
fi

# One .repo file per target that has packages
for target in "${targets[@]}"; do
    [ -d "$OUTDIR/$target" ] || continue
    {
        echo "[network-manager-gpclient]"
        echo "name=GlobalProtect NetworkManager plugin ($target)"
        # Fedora and Leap: the directory follows the release of the system
        # (the shell must not expand the dollar signs: they are for dnf/zypper)
        case "${family_of[$target]}:$target" in
            fedora:fedora*) echo "baseurl=$BASE_URL/fedora"'$releasever/$basearch' ;;
            suse:leap*) echo "baseurl=$BASE_URL/leap"'$releasever/$basearch' ;;
            *) echo "baseurl=$BASE_URL/$target/"'$basearch' ;;
        esac
        echo "enabled=1"
        echo "skip_if_unavailable=1"
        if [ -n "$GPG_KEY" ]; then
            echo "gpgcheck=1"
            echo "repo_gpgcheck=1"
            echo "gpgkey=$BASE_URL/$KEY_FILE"
        else
            echo "gpgcheck=0"
            echo "repo_gpgcheck=0"
        fi
    } > "$OUTDIR/$target/$REPO_FILE"
done

update_index

log "repository built in $OUTDIR"
if [ -z "$GPG_KEY" ]; then
    log "WARNING: unsigned repository - the .repo files switch the signature checks off"
fi

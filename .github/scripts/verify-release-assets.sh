#!/bin/bash
# Verify the packages of one release before they are signed into the apt and
# RPM repositories.
#
#   verify-release-assets.sh [--checksums-only] <tag> <directory> [<legacy tags file>]
#
# <directory> holds the .deb and .rpm assets of the release <tag> and, if the
# release has one, its SHA256SUMS asset. For every package:
#   1. its SHA256 is the one in SHA256SUMS, and
#   2. `gh attestation verify <file> --repo $GITHUB_REPOSITORY` succeeds, with
#      the signer workflow build-release.yml and the source ref refs/tags/<tag>
#      (the build provenance written for that tag).
# With --checksums-only (first argument) step 2 is left out: no GitHub API
# call is made. publish-apt.yml uses it for the releases that were fully
# verified when they were published, to stay under the API rate limit.
# A tag listed in the legacy file (default
# .github/legacy-unattested-releases.txt) is not verified: those releases are
# older than SHA256SUMS and the attestations. Every other release without
# SHA256SUMS fails. Any failure exits non-zero.
#
# GitHub stores the "~" of a release asset name as ".", so both are the same
# name here.

set -euo pipefail

usage="usage: verify-release-assets.sh [--checksums-only] <tag> <directory> [<legacy tags file>]"
checksums_only=0
if [ "${1:-}" = "--checksums-only" ]; then checksums_only=1; shift; fi
tag="${1:?$usage}"
dir="${2:?$usage}"
legacy="${3:-$(dirname "${BASH_SOURCE[0]}")/../legacy-unattested-releases.txt}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"

die() { echo "::error::verify-release-assets: $tag: $*" >&2; exit 1; }

[ -d "$dir" ] || die "$dir is not a directory"
[ -r "$legacy" ] || die "cannot read the list of legacy releases $legacy"

if awk -v tag="$tag" '{ sub(/[ \t]*#.*/, ""); sub(/[ \t]+$/, "") } $0 == tag { found = 1 } END { exit !found }' "$legacy"; then
    echo "$tag is a legacy release without SHA256SUMS and attestations: not verified"
    exit 0
fi

sums="$dir/SHA256SUMS"
[ -f "$sums" ] || die "the release has no SHA256SUMS asset and is not in $legacy"

shopt -s nullglob
files=("$dir"/*.deb "$dir"/*.rpm)
[ "${#files[@]}" -gt 0 ] || die "no .deb or .rpm files in $dir"

for file in "${files[@]}"; do
    name="$(basename "$file")"
    # Lines are "<hash>  <name>" (or "<hash> *<name>"); the name is compared whole
    expected="$(awk -v name="${name//\~/.}" '
        { n = $2; sub(/^\*/, "", n); gsub(/~/, ".", n) }
        n == name { print $1 }' "$sums")"
    [ -n "$expected" ] || die "$name is not in SHA256SUMS"
    [ "$(printf '%s\n' "$expected" | wc -l)" -eq 1 ] || die "$name is more than once in SHA256SUMS"
    actual="$(sha256sum "$file")"; actual="${actual%% *}"
    [ "$actual" = "$expected" ] || die "SHA256 of $name is $actual, SHA256SUMS says $expected"
    if [ "$checksums_only" -eq 1 ]; then
        echo "checksum verified $name"
        continue
    fi
    gh attestation verify "$file" --repo "$repo" \
        --signer-workflow "$repo/.github/workflows/build-release.yml" \
        --source-ref "refs/tags/$tag" >/dev/null \
        || die "no valid build provenance for $name (gh attestation verify failed)"
    echo "verified $name"
done

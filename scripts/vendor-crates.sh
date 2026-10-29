#!/bin/bash
# Vendors the crates of external/GlobalProtect-openconnect (Cargo.lock) into vendor/crates
# and writes the source replacement for cargo to vendor/config.toml. The package builds
# (debian/rules, rpm spec) use it when it exists and then run cargo --offline.
# Usage: scripts/vendor-crates.sh   (from any directory)
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORKSPACE=external/GlobalProtect-openconnect

command -v cargo >/dev/null 2>&1 || {
    echo "error: cargo not found (install Rust: https://rustup.rs)" >&2
    exit 1
}
[ -f "$ROOT/$WORKSPACE/Cargo.lock" ] || {
    echo "error: $WORKSPACE is empty: git submodule update --init --recursive" >&2
    exit 1
}

cd "$ROOT"
rm -rf vendor
mkdir -p vendor
# cargo reads "directory" relative to the parent of the directory of the config file
# (here the repository root), so the path is right in any build directory. The config
# goes to a temporary file first: no half-written config can enable the offline build.
cargo vendor --locked --manifest-path "$WORKSPACE/Cargo.toml" vendor/crates >vendor/config.tmp
sed -i 's#^directory = .*#directory = "vendor/crates"#' vendor/config.tmp
grep -q '^directory = "vendor/crates"$' vendor/config.tmp || {
    echo "error: unexpected output of cargo vendor" >&2
    exit 1
}
# Some crates ship files without read access for others (fnv: 0640); the build user of
# the container is not the user that vendored, and reads vendor/ through a mount
chmod -R a+rX vendor
mv vendor/config.tmp vendor/config.toml
echo "Vendored $(find vendor/crates -mindepth 1 -maxdepth 1 -type d | wc -l) crates into vendor/crates"

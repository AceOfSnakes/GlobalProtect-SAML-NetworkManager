#!/bin/bash
# Build script for creating .deb packages for multiple Ubuntu versions
# Usage: ./build-all.sh [VERSION]
#   VERSION: Optional target: an Ubuntu version (22.04, 24.04, 26.04) or a release
#            variant (24.04-neon). A target is valid when debian/control.ubuntu<VERSION>
#            and its Dockerfile exist (Dockerfile.ubuntu for a version,
#            Dockerfile.ubuntu<VERSION> for a variant). If not specified, builds all.
# Environment:
#   VERSION_SUFFIX  Optional, like the pull request suffix of CI (+pr<PR>.<run>):
#                   added to the package version, after the release codename tag.
#                   The codename tag (~noble1) is added only with a suffix: a plain local
#                   build stays e.g. 1.5.0-1, which sorts above the released 1.5.0-1~noble1.
#                   A variant is built only on a host architecture its control file lists
#                   (or "any"), as in CI: 24.04-neon is amd64 only.
# Examples:
#   ./build-all.sh                               # Build for all versions and variants
#   ./build-all.sh 24.04                         # Build only for Ubuntu 24.04
#   ./build-all.sh 24.04-neon                    # Build only the KDE neon variant
#   VERSION_SUFFIX=+pr54.1 ./build-all.sh 24.04  # Version gets the suffix +pr54.1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# dockerfile_of <target>: one Dockerfile.ubuntu (build argument UBUNTU) builds every
# version; a variant (name with -) has its own Dockerfile.ubuntu<target>.
dockerfile_of() {
    case "$1" in *-*) echo "Dockerfile.ubuntu$1" ;; *) echo "Dockerfile.ubuntu" ;; esac
}

# A target has a control file and a Dockerfile (the same rule as CI).
ALL_TARGETS=()
for file in "$SCRIPT_DIR"/debian/control.ubuntu*; do
    name="${file##*/control.ubuntu}"
    # A release (22.04) or a variant (24.04-neon); not a backup like 24.04~
    case "$name" in [0-9][0-9].[0-9][0-9] | [0-9][0-9].[0-9][0-9]-*[!~]) ;; *) continue ;; esac
    if [ -f "$SCRIPT_DIR/$(dockerfile_of "$name")" ]; then
        ALL_TARGETS+=("$name")
    fi
done
SUPPORTED="${ALL_TARGETS[*]}"

HOST_ARCH="$(dpkg --print-architecture 2>/dev/null || true)"
if [ -z "$HOST_ARCH" ]; then
    case "$(uname -m)" in
        x86_64) HOST_ARCH=amd64 ;;
        aarch64) HOST_ARCH=arm64 ;;
        *) HOST_ARCH="$(uname -m)" ;;
    esac
fi

# can_build <target>: a variant (name with -) is built only for the architectures
# of its binary packages (same awk as variant_arches of build-release.yml).
can_build() {
    case "$1" in *-*) ;; *) return 0 ;; esac
    local arches
    arches=$(awk '
        /^[ \t]*$/ { binary = 0 }
        tolower($1) == "package:" { binary = 1 }
        binary && tolower($1) == "architecture:" { for (i = 2; i <= NF; i++) print $i }
    ' "$SCRIPT_DIR/debian/control.ubuntu$1" | sort -u | tr '\n' ' ')
    if [ -z "$arches" ]; then
        echo "ERROR: variant $1 has no Architecture field in its binary packages in debian/control.ubuntu$1"
        exit 1
    fi
    case " $arches " in
        *" any "* | *" $HOST_ARCH "*) return 0 ;;
    esac
    BUILD_ARCHES="${arches% }"
    return 1
}

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    echo "Usage: $0 [VERSION]"
    echo ""
    echo "Build .deb packages for Ubuntu versions and release variants."
    echo ""
    echo "Arguments:"
    echo "  VERSION    Optional target (${SUPPORTED// /, })"
    echo "             If not specified, builds all of them"
    echo ""
    echo "Environment:"
    echo "  VERSION_SUFFIX    Optional version suffix, for example +pr54.1 (as CI does for pull requests)"
    echo ""
    echo "Examples:"
    echo "  $0                    # Build all targets"
    echo "  $0 24.04              # Build only for Ubuntu 24.04"
    echo "  $0 24.04-neon         # Build only the KDE neon variant"
    exit 0
fi

# The suffix goes into a sed command in the container: allow a plus sign, letters, digits and dots only.
VERSION_SUFFIX="${VERSION_SUFFIX:-}"
if ! [[ "$VERSION_SUFFIX" =~ ^(\+[A-Za-z0-9.]+)?$ ]]; then
    echo "ERROR: Invalid VERSION_SUFFIX '$VERSION_SUFFIX'. Use a plus sign, then letters, digits or dots (for example +pr54.1)"
    exit 1
fi

if [ -n "$1" ]; then
    # Validate version
    FOUND=""
    for name in "${ALL_TARGETS[@]}"; do
        [ "$name" = "$1" ] && FOUND=1
    done
    if [ -z "$FOUND" ]; then
        echo "ERROR: Invalid Ubuntu version '$1'. Supported versions: ${SUPPORTED// /, }"
        echo "Run '$0 --help' for usage information"
        exit 1
    fi
    if ! can_build "$1"; then
        echo "ERROR: $1 is built for ${BUILD_ARCHES:-no architecture} only, not for $HOST_ARCH"
        exit 1
    fi
    UBUNTU_VERSIONS=("$1")
    echo "=== Building package for Ubuntu $1 only ==="
else
    # Build for all versions and variants
    UBUNTU_VERSIONS=()
    for name in "${ALL_TARGETS[@]}"; do
        if can_build "$name"; then
            UBUNTU_VERSIONS+=("$name")
        else
            echo "Variant $name is built for ${BUILD_ARCHES:-no architecture} only, not for $HOST_ARCH: skipped"
        fi
    done
    echo "=== Building packages for Ubuntu ${UBUNTU_VERSIONS[*]} ==="
fi

# Enable Docker BuildKit for better caching
export DOCKER_BUILDKIT=1

# Vendored crates (scripts/vendor-crates.sh): with them the build needs no network, so
# the build container runs with --network none. Without them (and without cargo to make
# them) the build downloads the crates, as before.
NETWORK_ARGS=()
if [ ! -f "$SCRIPT_DIR/vendor/config.toml" ] && command -v cargo >/dev/null 2>&1; then
    bash "$SCRIPT_DIR/scripts/vendor-crates.sh"
fi
if [ -f "$SCRIPT_DIR/vendor/config.toml" ]; then
    NETWORK_ARGS=(--network none)
else
    echo "NOTE: no vendor/config.toml and no cargo: the build downloads the crates"
fi

for VERSION in "${UBUNTU_VERSIONS[@]}"; do
    echo ""
    echo "======================================"
    echo "=== Building for Ubuntu $VERSION ==="
    echo "======================================"

    IMAGE_NAME="gpclient-builder:ubuntu${VERSION}"
    DOCKERFILE="$(dockerfile_of "$VERSION")"
    BUILD_ARGS=()
    case "$VERSION" in *-*) ;; *) BUILD_ARGS=(--build-arg "UBUNTU=$VERSION") ;; esac
    OUTPUT_DIR="$SCRIPT_DIR/output/ubuntu${VERSION}"

    echo "=== Building Docker image from $DOCKERFILE ==="
    docker build "${BUILD_ARGS[@]}" -f "$SCRIPT_DIR/$DOCKERFILE" -t "$IMAGE_NAME" "$SCRIPT_DIR"

    echo "=== Building .deb package for Ubuntu $VERSION ==="
    mkdir -p "$OUTPUT_DIR"
    rm -f "$OUTPUT_DIR"/*.deb "$OUTPUT_DIR"/*.ddeb
    chmod 777 "$OUTPUT_DIR"

    # Run with cache mounts for Cargo registry and Rust target directory
    # Copy version-specific control file before building. With a suffix, tag the
    # version as CI does (1.4.0-1~noble1+pr54.1): the same sed as build-release.yml,
    # but only with a suffix (CI always tags the version).
    docker run --rm \
        "${NETWORK_ARGS[@]}" \
        --name "gpclient-build-ubuntu${VERSION}-$$" \
        -e PR_SUFFIX="$VERSION_SUFFIX" \
        -v "$OUTPUT_DIR:/output" \
        -v "gpclient-cargo-cache-${VERSION}:/cargo-cache" \
        -v "gpclient-target-cache-${VERSION}:/build/source/external/GlobalProtect-openconnect/target" \
        "$IMAGE_NAME" \
        bash -c "cp debian/control.ubuntu${VERSION} debian/control && { [ -z \"\$PR_SUFFIX\" ] || { . /etc/os-release && sed -i \"1s/(\\([^)]*\\))/(\\1~\${VERSION_CODENAME}1\${PR_SUFFIX})/\" debian/changelog; }; } && fakeroot dpkg-buildpackage -us -uc -b && cp -v ../*.deb /output/ && { cp -v ../*.ddeb /output/ || true; }"

    echo "=== Build complete for Ubuntu $VERSION ==="
    echo "Packages available in: $OUTPUT_DIR/"
    ls -lh "$OUTPUT_DIR/"*.deb 2>/dev/null || echo "No .deb files found"
done

echo ""
echo "======================================"
echo "=== All builds complete ==="
echo "======================================"
echo ""
echo "Packages summary:"
for VERSION in "${UBUNTU_VERSIONS[@]}"; do
    OUTPUT_DIR="$SCRIPT_DIR/output/ubuntu${VERSION}"
    echo ""
    echo "Ubuntu $VERSION:"
    ls -lh "$OUTPUT_DIR/"*.deb 2>/dev/null || echo "  No packages found"
done

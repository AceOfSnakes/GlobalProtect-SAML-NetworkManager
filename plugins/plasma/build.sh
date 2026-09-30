#!/bin/bash
# Build script for GlobalProtect Plasma VPN Plugin

set -e

echo "Building GlobalProtect Plasma VPN Plugin..."

# Multiarch triplet (x86_64-linux-gnu, aarch64-linux-gnu, ...)
MULTIARCH="${MULTIARCH:-$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch 2>/dev/null)}" || true
# An empty value would configure KDE_INSTALL_LIBDIR=lib/ and install into the wrong place
if [ -z "$MULTIARCH" ]; then
    echo "ERROR: cannot determine the multiarch triplet: run with MULTIARCH=<triplet> (e.g. x86_64-linux-gnu)" >&2
    exit 1
fi

# Create build directory
mkdir -p build
cd build

# Run CMake
cmake .. \
    -DCMAKE_INSTALL_PREFIX=/usr \
    -DKDE_INSTALL_LIBDIR="lib/$MULTIARCH" \
    -DCMAKE_BUILD_TYPE=Release

# Build
make -j$(nproc)

echo ""
echo "Build complete!"
echo ""
echo "To install, run:"
echo "  cd plasma/build && sudo make install"
echo ""
echo "Then restart plasma-nm:"
echo "  killall plasmashell && plasmashell &"

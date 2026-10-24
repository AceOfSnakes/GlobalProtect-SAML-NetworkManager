#!/bin/sh
# Add NoDisplay=true to the [Desktop Entry] group of a .desktop file, so that
# the entry is not shown in menus. The MIME association still works.
# The file is not changed when that group already has a NoDisplay key.
# Usage: hide-desktop-entry.sh FILE
set -eu

[ $# -eq 1 ] && [ -f "$1" ] || { echo "usage: $0 FILE" >&2; exit 2; }

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

awk '
    FNR == NR {
        if ($0 ~ /^\[/) group = $0
        if (group == "[Desktop Entry]" && $0 ~ /^NoDisplay[ \t]*=/) found = 1
        next
    }
    { print }
    !found && !done && $0 == "[Desktop Entry]" { print "NoDisplay=true"; done = 1 }
' "$1" "$1" > "$tmp"

cat "$tmp" > "$1"
grep -q '^\[Desktop Entry\]$' "$1" || { echo "$0: $1 has no [Desktop Entry] group" >&2; exit 1; }

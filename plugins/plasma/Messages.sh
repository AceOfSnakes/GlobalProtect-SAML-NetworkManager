#!/bin/sh
# KDE message extraction (run by KDE's tools, which set EXTRACTRC, XGETTEXT and
# podir). `make pot` (scripts/update_pot.py) does the same without extractrc.
# shellcheck disable=SC2154
$EXTRACTRC ./*.ui >> rc.cpp
$XGETTEXT ./*.cpp -o "$podir/plasmanetworkmanagement_gpclientui.pot"
rm -f rc.cpp

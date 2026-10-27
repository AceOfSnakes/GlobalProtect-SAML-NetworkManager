#!/bin/bash
# End-to-end scenario (GNOME): saml-auth=dialog. The secret agent (the GNOME
# auth dialog) runs gpauth as the user; the service gives the result to
# gpclient --cookie-on-stdin. gpauth must not be a child of gpclient (root).
# Not recorded for an animation. Runs as user demo; see lib.sh.
# shellcheck disable=SC2034 # the EDGE_* settings are read by lib.sh
source /opt/gp-demo/lib.sh
EDGE_LEFT=66 # the dock
EDGE_TOP=40  # the top bar

nmcli connection add type vpn vpn-type gpclient con-name "Contoso VPN" \
    vpn.data "gateway=vpn.contoso.example,browser=edge,saml-auth=dialog,otp-flags=2,gpauth-result-flags=2" > /dev/null || fail "cannot add the VPN profile"
pause 3
snapshot start

# Not "nmcli connection up": nmcli registers its own secret agent and takes the
# gpauth-result request. Activate over D-Bus like the GNOME menu, so the
# GNOME Shell agent (the auth dialog) answers.
uuid=$(nmcli -g connection.uuid connection show "Contoso VPN") || fail "no VPN profile"
path=$(busctl --system call org.freedesktop.NetworkManager /org/freedesktop/NetworkManager/Settings \
    org.freedesktop.NetworkManager.Settings GetConnectionByUuid s "$uuid" | awk '{print $2}' | tr -d '"')
busctl --system call org.freedesktop.NetworkManager /org/freedesktop/NetworkManager \
    org.freedesktop.NetworkManager ActivateConnection ooo "$path" / / > "$DEMO_OUT/nmcli-up.log" 2>&1 \
    || fail "cannot activate the VPN profile"
edge_signin
wait_vpn "Contoso VPN" activated 60 || fail "the VPN did not become active"
pause 1.5
snapshot connected

disconnect_vpn "Contoso VPN" || fail "the VPN did not disconnect"
pause 1.5
snapshot disconnected

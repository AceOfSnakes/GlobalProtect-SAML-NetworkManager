#!/bin/bash
# End-to-end scenario (GNOME desktop): saml-auth=dialog. The secret agent of
# nmcli --ask runs the auth dialog, which runs gpauth as the user; the service gives the result to
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

# "nmcli --ask": nmcli's own secret agent runs the auth dialog
# (--external-ui-mode), which runs gpauth as this user; the result is not asked
# (ShouldAsk=false), so nothing is read from stdin. Without --ask nmcli does not
# run the dialog ("No valid secrets"). GNOME Shell's agent is not used here: in
# this container NetworkManager did not hand it the request.
nmcli --ask connection up "Contoso VPN" < /dev/null > "$DEMO_OUT/nmcli-up.log" 2>&1 &
edge_signin
wait_vpn "Contoso VPN" activated 60 || fail "the VPN did not become active"
pause 1.5
snapshot connected

disconnect_vpn "Contoso VPN" || fail "the VPN did not disconnect"
pause 1.5
snapshot disconnected

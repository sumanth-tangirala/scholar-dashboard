#!/bin/bash
# Lets your iPad (or another of your own devices) chat through Claude Code on this Mac: publishes the bridge
# (127.0.0.1:7823) on your private Tailscale network over HTTPS. Only devices signed in to your Tailscale
# account can reach it, and each still has to be paired (you click Allow in a dialog on this Mac).
#
# Before running: install Tailscale on this Mac and on the iPad and sign in to the same account on both;
# in the Tailscale admin console (DNS page) turn on MagicDNS and HTTPS certificates.
# Undo with:  tailscale serve --https=443 off
set -e
TS="$(command -v tailscale || true)"
[ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [ -z "$TS" ]; then
  echo "Tailscale isn't installed. Get it from https://tailscale.com/download (Mac) and the App Store (iPad),"
  echo "sign in to the same account on both, then run this again."
  exit 1
fi
"$TS" serve --bg --https=443 http://127.0.0.1:7823
URL="https://$("$TS" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo
echo "The bridge is on your tailnet at:  $URL"
echo "On the iPad: open a paper, Chat tab, enter that address, then click Allow in the dialog on this Mac."
echo "The Mac has to be awake for chat to work from the iPad."

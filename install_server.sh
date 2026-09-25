#!/bin/bash
# Install the Scholar Dashboard as a background service accessible at http://scholar.localhost
# (and the older http://scholar.local). Use scholar.localhost: browsers treat *.localhost as a
# secure address, which the encrypted notes need; scholar.local is not.
#
# What this does:
#   1. Installs a LaunchDaemon that runs a Python HTTP server on port 80 (bound to localhost only)
#   2. Adds "scholar.localhost" and "scholar.local" to /etc/hosts
#
# Requires sudo (port 80 + /etc/hosts).
# Usage: ./install_server.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="scholar.local"
PLIST_NAME="com.scholar-dashboard.plist"
PLIST_SRC="$DIR/$PLIST_NAME"
DAEMON_DST="/Library/LaunchDaemons/$PLIST_NAME"
AGENT_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "Installing Scholar Dashboard..."
echo ""

# --- Clean up any old user-agent version ---
launchctl unload "$AGENT_DST" 2>/dev/null || true
rm -f "$AGENT_DST"

# --- 1. LaunchDaemon (runs as root to bind port 80) ---
echo "Requires sudo for port 80 and /etc/hosts..."
sudo launchctl unload "$DAEMON_DST" 2>/dev/null || true
sudo cp "$PLIST_SRC" "$DAEMON_DST"
sudo chown root:wheel "$DAEMON_DST"
sudo chmod 644 "$DAEMON_DST"
sudo launchctl load "$DAEMON_DST"
echo "[1/2] HTTP server installed (port 80, localhost only)"

# --- 2. /etc/hosts entries (Chrome resolves *.localhost itself; Safari needs the line) ---
for D in "scholar.localhost" "$DOMAIN"; do
  if grep -qE "[[:space:]]$D([[:space:]]|$)" /etc/hosts 2>/dev/null; then
    echo "[2/2] /etc/hosts already has $D"
  else
    echo "127.0.0.1  $D" | sudo tee -a /etc/hosts > /dev/null
    echo "[2/2] Added $D to /etc/hosts"
  fi
done

# --- Clean up any old pfctl rules from previous install ---
sudo rm -f /etc/pf.anchors/com.scholar-dashboard 2>/dev/null || true
sudo launchctl unload /Library/LaunchDaemons/com.scholar-dashboard.portfwd.plist 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/com.scholar-dashboard.portfwd.plist 2>/dev/null || true
if grep -q "com.scholar-dashboard" /etc/pf.conf 2>/dev/null; then
  sudo sed -i '' '/com.scholar-dashboard/d' /etc/pf.conf
  sudo sed -i '' '/# Scholar Dashboard port forward/d' /etc/pf.conf
fi

echo ""
echo "Done! Your dashboard is live at:"
echo ""
echo "  http://scholar.localhost   (use this one: encrypted notes need it)"
echo ""
echo "Bookmark that URL. It will work after every reboot."
echo "To uninstall: ./uninstall_server.sh"

#!/bin/bash
# Starts the Scholar Dashboard <-> Claude Code bridge now and at every login (a per-user LaunchAgent; no sudo).
# Uninstall: launchctl bootout gui/$(id -u)/com.scholar-dashboard.bridge && rm ~/Library/LaunchAgents/com.scholar-dashboard.bridge.plist
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.scholar-dashboard.bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"
CLAUDE_DIR="$(dirname "$(command -v claude || echo "$HOME/.local/bin/claude")")"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/scholar_bridge.py</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$CLAUDE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/scholar-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/scholar-bridge.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
curl -s -H 'Origin: https://www.sumanthtangirala.com' http://127.0.0.1:7823/health && echo && echo "Bridge running. Open a paper in the dashboard and choose Chat."

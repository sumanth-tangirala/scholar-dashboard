#!/bin/bash
# Lets Claude Code and Claude Desktop on this Mac add papers to your library (and search it): registers
# bridge/scholar_mcp.py as a local MCP server called "scholar-dashboard". Local only: each app starts it itself and
# talks to it over stdio, and it reaches the bridge on 127.0.0.1. Nothing is opened to the network.
#
#   bash bridge/mcp_setup.sh            both apps
#   bash bridge/mcp_setup.sh code       Claude Code only
#   bash bridge/mcp_setup.sh desktop    Claude Desktop only (then quit it and open it again)
#
# Undo: claude mcp remove --scope user scholar-dashboard; and in Claude Desktop, Settings > Developer > Edit Config,
# remove "scholar-dashboard" from "mcpServers".
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
SERVER="$DIR/scholar_mcp.py"
WHICH="${1:-both}"

if [ "$WHICH" != desktop ]; then
  CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
  if [ -x "$CLAUDE" ]; then
    "$CLAUDE" mcp remove --scope user scholar-dashboard >/dev/null 2>&1 || true
    "$CLAUDE" mcp add --scope user scholar-dashboard -- "$PY" "$SERVER" >/dev/null
    echo "Claude Code: added (every project). New sessions have add_paper, get_add_status and search_library."
  else
    echo "Claude Code: not installed, skipped."
  fi
fi

if [ "$WHICH" != code ]; then
  CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  if [ ! -d "$(dirname "$CFG")" ]; then
    echo "Claude Desktop: not installed, skipped."
  else
    [ -f "$CFG" ] && cp "$CFG" "$CFG.before-scholar"
    "$PY" - "$CFG" "$PY" "$SERVER" <<'PY'
import json, os, sys
path, py, server = sys.argv[1:4]
cfg = {}
if os.path.exists(path):
    with open(path) as f: cfg = json.load(f)
cfg.setdefault('mcpServers', {})['scholar-dashboard'] = {'command': py, 'args': [server]}
with open(path + '.tmp', 'w') as f: json.dump(cfg, f, indent=2)
os.chmod(path + '.tmp', 0o600)
os.replace(path + '.tmp', path)
PY
    echo "Claude Desktop: added (the old settings are in claude_desktop_config.json.before-scholar)."
    pgrep -xq Claude && echo "    Quit Claude Desktop (Cmd+Q) and open it again: it reads its MCP servers when it starts."
  fi
fi

if curl -s -m 3 -H 'Origin: http://localhost' http://127.0.0.1:7823/health | grep -q '"version": [6-9]'; then
  echo "Bridge: running."
else
  echo "Bridge: not running, or an older version. Start it with: bash bridge/install_bridge.sh"
fi

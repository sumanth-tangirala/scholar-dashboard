#!/bin/bash
# Lets Chat run on the Claude API (billed to your API account) instead of Claude Code. Stores the key for the bridge
# (~/.scholar-bridge/config.json, readable by you alone; the page never sees it), installs the Anthropic SDK and
# pypdf for the bridge (~/.scholar-bridge/pylib, not into your Python), checks the key, switches Chat to the API
# and restarts the bridge. Afterwards the chat panel's Code / API menu switches between them.
#
#   bash bridge/api_setup.sh [workspace-id]      # asks for the key (hidden, so it stays out of your shell history)
#   bash bridge/api_setup.sh sk-ant-... [wrkspc_...]
#   bash bridge/api_setup.sh --off               # forgets the key; Chat goes back to Claude Code
# A Claude Console key already belongs to its workspace, so the workspace ID is optional (kept only as a note).
set -e
HOME_DIR="${SCHOLAR_BRIDGE_HOME:-$HOME/.scholar-bridge}"
LABEL=com.scholar-dashboard.bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# the Python the bridge runs with, so the SDK is installed for that one
PY="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$PLIST" 2>/dev/null || command -v python3)"
mkdir -p "$HOME_DIR"

KEY="" WS="" OFF=""
for a in "$@"; do
  case "$a" in
    --off) OFF=1 ;;
    sk-ant-*) KEY="$a" ;;
    wrkspc_*) WS="$a" ;;
    *) echo "Not a key (sk-ant-…) or a workspace ID (wrkspc_…): $a"; exit 1 ;;
  esac
done

restart() {
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null && sleep 1 && echo "Bridge restarted." \
    || echo "Bridge not installed as a login item yet: run  bash bridge/install_bridge.sh"
}

if [ -n "$OFF" ]; then
  HOME_DIR="$HOME_DIR" "$PY" - <<'PY'
import json, os
p = os.path.join(os.environ['HOME_DIR'], 'config.json')
try: c = json.load(open(p))
except Exception: c = {}
c.pop('api_key', None); c.pop('workspace_id', None); c['engine'] = 'code'
fd = os.open(p + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as f: json.dump(c, f)
os.replace(p + '.tmp', p)
PY
  echo "API key removed; Chat uses Claude Code."
  restart; exit 0
fi

if [ -z "$KEY" ]; then
  read -rsp "Claude API key (sk-ant-…, input hidden): " KEY; echo
fi
case "$KEY" in sk-ant-*) ;; *) echo "That doesn't look like a Claude Console API key (they start with sk-ant-)."; exit 1 ;; esac

echo "Installing the Anthropic SDK for the bridge…"
if ! OUT="$("$PY" -m pip install -q --disable-pip-version-check --upgrade --target "$HOME_DIR/pylib" anthropic pypdf 2>&1)"; then
  echo "$OUT"; echo "Installing the SDK failed."; exit 1
fi

# check the key (listing a model costs nothing), then store it
API_KEY="$KEY" WS="$WS" HOME_DIR="$HOME_DIR" "$PY" - <<'PY'
import json, os, sys
home = os.environ['HOME_DIR']
sys.path.insert(0, os.path.join(home, 'pylib'))
import anthropic
key, ws = os.environ['API_KEY'], os.environ.get('WS', '')
try:
    m = anthropic.Anthropic(api_key=key).models.retrieve('claude-opus-5-5')
except anthropic.AuthenticationError:
    sys.exit('The API refused this key. Check it in the Claude Console (platform.claude.com → API keys).')
except anthropic.APIStatusError as e:
    sys.exit(f'The API answered {e.status_code}: {e.message}')
except anthropic.APIConnectionError:
    sys.exit("Couldn't reach the Claude API. Is this Mac online?")
p = os.path.join(home, 'config.json')
try: c = json.load(open(p))
except Exception: c = {}
c.update(api_key=key, engine='api')
if ws: c['workspace_id'] = ws
else: c.pop('workspace_id', None)
fd = os.open(p + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as f: json.dump(c, f)
os.replace(p + '.tmp', p)
print(f'Key works ({m.display_name} available). Stored in {p} (readable by you alone).')
if ws: print(f'Workspace {ws} noted (a Console key already belongs to its workspace).')
PY

restart
echo "Chat now runs on the Claude API. Switch back any time with the Code / API menu in the chat panel."

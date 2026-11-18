# Scholar Dashboard browser extension

Adds papers to your library from Google Scholar, Scholar alert emails in Gmail, and any paper's page. Papers go
through the bridge on your Mac (`bridge/scholar_bridge.py`), like Library → + Add paper: Claude Code finds the paper
and `bridge/add_paper.py` publishes it.

**Install** (Chrome, Arc, Brave or Edge): open `chrome://extensions` (Arc: `arc://extensions`), turn on Developer
mode, click **Load unpacked** and choose this folder. Pin it to the toolbar. The first paper you add opens a small
window: click **Allow** in the dialog on your Mac, once.

**Update** after pulling changes: click the reload arrow on the extension's card in `chrome://extensions`.

| File | What it does |
|---|---|
| `background.js` | talks to the bridge, follows the papers being added, keeps an index of your library (the public CSV) |
| `page.js`, `page.css` | the Add to library buttons on Google Scholar and in Gmail |
| `popup.*` | the toolbar button: the paper on this page, and what's being added |
| `connect.*` | connecting to your Mac (pairing, or the Mac's Tailscale address on another computer) |
| `ui.css`, `fonts/`, `icons/` | the dashboard's look |

The `key` in `manifest.json` fixes the extension's id (`mhfedfofciaejamdeikbedlfapmpfcpm`), which the bridge allows
(`EXTENSION` in `bridge/scholar_bridge.py`). It is a public key: there is no secret in this folder. The pairing
token is kept in the extension's own storage.

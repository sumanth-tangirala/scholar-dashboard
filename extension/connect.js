/* Connecting this browser to the bridge on your Mac: once (the token is kept), with Allow in a dialog on the Mac.
   A window of its own, because the toolbar popup closes as soon as the Mac's dialog takes the focus. Opened with the
   state it found (#unpaired, #offline, #old); papers asked for meanwhile are added once it's connected. */
const DEFAULT_BRIDGE = 'http://127.0.0.1:7823';
const app = document.getElementById('app');
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const show = (html, focus) => { app.innerHTML = html; const f = focus && document.getElementById(focus); if (f) f.focus(); };
const bridge = async () => (await chrome.storage.local.get('bridge')).bridge || DEFAULT_BRIDGE;

async function check() {
  show('<div class="status" style="margin:0"><span class="spin"></span>Looking for your Mac…</div>');
  const s = await chrome.runtime.sendMessage({ type: 'status' });
  route(s.state);
}
function route(state) {
  if (state === 'ready') return connected();
  if (state === 'unpaired') return pair();
  if (state === 'old') return old();
  return offline();
}

async function pair() {
  const code = String(Math.floor(100000 + Math.random() * 900000));
  show(`<div class="kicker">Connect to your Mac</div><h1 class="title">Click Allow on your Mac</h1>
    <p class="lead">A dialog on your Mac asks whether this browser may add papers through Claude Code there. It shows this code:</p>
    <p class="code">${code.slice(0, 3)} ${code.slice(3)}</p>
    <div class="status"><span class="spin"></span>Waiting for you to allow it…</div>`);
  try {
    const r = await fetch(await bridge() + '/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code, who: 'extension' }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.token) { await chrome.storage.local.set({ token: j.token }); return connected(); }
    show(`<div class="kicker warn">Not connected</div><h1 class="title">The dialog was declined</h1>
      <p class="lead">Or it closed by itself after a minute. Nothing is added from this browser until you allow it.</p>
      <div class="row"><button class="go" id="again">Try again</button><button class="plain" id="close">Close</button></div>`, 'again');
    document.getElementById('again').onclick = pair;
    document.getElementById('close').onclick = () => window.close();
  } catch (e) { offline(); }
}

async function connected() {
  const { pending = [] } = await chrome.storage.local.get('pending');
  show(`<div class="kicker in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>Connected</div>
    <h1 class="title">This browser adds papers through your Mac</h1>
    <p class="lead">${pending.length ? `Adding ${pending.length === 1 ? 'the paper' : `the ${pending.length} papers`} you asked for now.` : 'Use the toolbar button on a paper’s page, or Add to library on Google Scholar.'}</p>`);
  chrome.runtime.sendMessage({ type: 'connected' });
  setTimeout(() => window.close(), 1800);
}

async function offline() {
  const b = await bridge();
  show(`<div class="kicker warn">Not reachable</div><h1 class="title">Your Mac isn’t reachable</h1>
    <p class="lead">Adding papers runs through Claude Code on your Mac, so it needs to be awake with the bridge running. On another computer, use your Mac’s Tailscale address (<code>bash bridge/tailscale_setup.sh</code> prints it).</p>
    <form class="row" id="f"><input class="input" id="addr" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://your-mac.your-tailnet.ts.net" value="${esc(b === DEFAULT_BRIDGE ? '' : b)}"><button class="go">Try again</button></form>
    <p class="note">Leave it empty for this Mac.</p>`, 'addr');
  document.getElementById('f').onsubmit = async e => {
    e.preventDefault();
    let v = document.getElementById('addr').value.trim().replace(/\/+$/, '');
    if (v && !/^https?:\/\//.test(v)) v = 'https://' + v;
    let origin = '';
    try { origin = v ? new URL(v).origin : ''; } catch (err) { return; }
    if (!origin || /^http:\/\/(127\.0\.0\.1|localhost)(:7823)?$/.test(origin)) await chrome.storage.local.remove('bridge');
    else {
      // only a Tailscale address can be reached (the extension asks for permission to talk to it)
      if (!/^https:\/\/[^/]+\.ts\.net$/.test(origin)) { document.querySelector('.note').textContent = 'Give the Mac’s Tailscale address: it ends in .ts.net.'; return; }
      const ok = await chrome.permissions.request({ origins: [origin + '/*'] });
      if (!ok) { document.querySelector('.note').textContent = 'The browser needs your permission to reach that address.'; return; }
      await chrome.storage.local.set({ bridge: origin, token: '' });
    }
    check();
  };
}

function old() {
  show(`<div class="kicker warn">Restart needed</div><h1 class="title">The bridge on your Mac is an older version</h1>
    <p class="lead">Restart it so it knows this extension: in the Scholar Dashboard folder, run <code>bash bridge/install_bridge.sh</code>.</p>
    <div class="row"><button class="go" id="again">Try again</button></div>`, 'again');
  document.getElementById('again').onclick = check;
}

const first = location.hash.slice(1);
if (first === 'unpaired') pair(); else check();

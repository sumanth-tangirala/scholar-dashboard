/* Scholar Dashboard extension: the background.
   - Adding a paper goes to the bridge on your Mac (bridge/scholar_bridge.py), the same as the dashboard's Add paper box:
     Claude Code finds and describes the paper, bridge/add_paper.py publishes it. Papers wait their turn in the bridge's queue.
   - Pages ask which of their papers are already in your library: an index of the public papers_database.csv.
   - It follows the papers being added (the popup, the pages' buttons and the toolbar badge show where each one is) and
     says when each is in.
   Stored (chrome.storage.local): bridge (address), token (from pairing: Allow on the Mac), lib (the index), jobs (the
   papers asked for lately, from anywhere), mine (which of them were asked for here), pending (asked for before connecting). */
const DEFAULT_BRIDGE = 'http://127.0.0.1:7823';
const DASHBOARD = 'https://www.sumanthtangirala.com/scholar-dashboard/';
const CSV_URL = 'https://raw.githubusercontent.com/sumanth-tangirala/scholar-dashboard/main/papers_database.csv';
const LIB_MAX_AGE = 10 * 60e3;   // the index is checked against GitHub's copy at most this often (an unchanged file isn't downloaded)

const paperUrl = id => DASHBOARD + '#paper/' + encodeURIComponent(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function conf() {
  const s = await chrome.storage.local.get(['bridge', 'token']);
  return { bridge: s.bridge || DEFAULT_BRIDGE, token: s.token || '' };
}

async function bridgeFetch(path, { body, timeout = 8000 } = {}) {
  const { bridge, token } = await conf();
  const ctrl = new AbortController(), t = setTimeout(() => ctrl.abort(), timeout);
  try {
    return await fetch(bridge + path, { method: body ? 'POST' : 'GET', signal: ctrl.signal, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: body && JSON.stringify(body) });
  } finally { clearTimeout(t); }
}

/* ready | unpaired | offline | old (a bridge from before the add queue, or one that doesn't know this extension) */
async function status() {
  try {
    const r = await bridgeFetch('/health', { timeout: 4000 });
    if (r.status === 403) return { state: 'old' };
    const j = await r.json();
    if ((j.version || 0) < 6) return { state: 'old' };
    return { state: j.paired ? 'ready' : 'unpaired' };
  } catch (e) { return { state: 'offline' }; }
}

/* ---------- the library index ---------- */
const norm = t => (t || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9]/g, '');
function idsIn(s) {
  let d = s || '';
  try { d = decodeURIComponent(d); } catch (e) {}
  const out = new Set();
  for (const m of d.matchAll(/(?:arxiv\.org\/(?:abs|pdf|html)\/|arxiv[:\s]+)?(\d{4}\.\d{4,5})(?:v\d+)?/gi)) out.add('ax:' + m[1]);
  for (const m of d.matchAll(/10\.\d{4,9}\/[^\s"'<>|,?#&]+/gi)) out.add('doi:' + m[0].toLowerCase().replace(/[.)]+$/, '').replace(/\.pdf$/, ''));
  return [...out];
}

/* A small CSV reader (quoted fields, doubled quotes, line breaks inside quotes): the rows as arrays */
function parseCsv(text) {
  const rows = []; let row = [], f = '', q = false;
  for (let i = 0, n = text.length; i < n; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; }
      else f += c;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(f); f = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(f); f = ''; rows.push(row); row = [];
    } else f += c;
  }
  if (f || row.length) { row.push(f); rows.push(row); }
  return rows;
}

let _libLoading = null;
async function libIndex(force) {
  const { lib } = await chrome.storage.local.get('lib');
  if (!force && lib && Date.now() - lib.at < LIB_MAX_AGE) return lib;
  if (_libLoading) return _libLoading;
  _libLoading = (async () => {
    try {
      const r = await fetch(CSV_URL, { cache: 'no-cache' });   // asks GitHub whether it changed (an unchanged file comes from the cache)
      if (!r.ok) throw new Error(r.status);
      const etag = r.headers.get('etag') || '';
      if (lib && etag && lib.etag === etag) { const same = { ...lib, at: Date.now() }; await chrome.storage.local.set({ lib: same }); return same; }
      const rows = parseCsv(await r.text()), head = rows.shift() || [];
      const col = n => head.indexOf(n);
      const [ci, ct, cu, cp, ch] = ['id', 'title', 'url', 'pdf_url', 'is_hidden'].map(col);
      const items = rows.filter(x => x[ci] && x[ct]).map(x => [x[ci], norm(x[ct]), idsIn((x[cu] || '') + ' ' + (x[cp] || '')).join(' '), (x[ch] || '') === 'true' ? 1 : 0]);
      const keep = (lib && lib.extra || []).filter(e => !items.some(i => i[0] === e[0]));   // papers added here that GitHub's copy hasn't caught up with
      const out = { at: Date.now(), etag, items: items.concat(keep), extra: keep };
      await chrome.storage.local.set({ lib: out });
      return out;
    } catch (e) {
      return lib || { at: 0, items: [], extra: [] };
    } finally { _libLoading = null; }
  })();
  return _libLoading;
}

async function rememberAdded(rows) {   // a paper just added counts as in the library at once
  const { lib = { at: 0, items: [], extra: [] } } = await chrome.storage.local.get('lib');
  const fresh = rows.filter(r => r.id && !lib.items.some(i => i[0] === r.id)).map(r => [r.id, norm(r.title), idsIn((r.url || '') + ' ' + (r.pdf_url || '')).join(' '), 0]);
  if (!fresh.length) return;
  await chrome.storage.local.set({ lib: { ...lib, items: lib.items.concat(fresh), extra: (lib.extra || []).concat(fresh) } });
}

/* Which of these are in the library: {k: id} for the ones that are */
async function lookup(items) {
  const lib = await libIndex();
  const byTitle = new Map(), byId = new Map();
  for (const [id, t, keys] of lib.items) {
    if (t.length > 12 && !byTitle.has(t)) byTitle.set(t, id);
    for (const k of keys ? keys.split(' ') : []) if (!byId.has(k)) byId.set(k, id);
  }
  const out = {};
  for (const it of items) {
    let id = null;
    for (const k of idsIn((it.urls || []).join(' '))) if (byId.has(k)) { id = byId.get(k); break; }
    const t = norm((it.title || '').replace(/(…|\.\.\.)\s*$/, ''));
    if (!id && t.length > 12) id = byTitle.get(t) || null;
    if (!id && t.length >= 30 && /(…|\.\.\.)\s*$/.test(it.title || '')) {   // a title the page cut short
      const hit = lib.items.find(i => i[1].startsWith(t)); id = hit ? hit[0] : null;
    }
    if (id) out[it.k] = id;
  }
  return out;
}

/* ---------- adding, and following what's being added ---------- */
const slim = j => ({ id: j.id, state: j.state, stage: j.stage, title: j.title || '', input: (j.input || '').slice(0, 300), from: j.from, ahead: j.ahead || 0,
  message: j.message || '', queued: j.queued, ended: j.ended || 0,
  added: (j.added || []).map(r => ({ id: r.id, title: r.title })), existing: (j.existing || []).map(id => ({ id, title: (j.titles || {})[id] || '' })) });
const pendingState = s => s === 'queued' || s === 'running';

async function add({ input, title, star, k }) {
  const st = await status();
  if (st.state !== 'ready') {   // connect first (in place), then this paper is added
    const { pending = [] } = await chrome.storage.local.get('pending');
    if (!pending.some(p => p.input === input)) pending.push({ input, title, star, k });
    await chrome.storage.local.set({ pending });
    await openConnect(st.state);
    return { waiting: 'connect', state: st.state };
  }
  const r = await bridgeFetch('/paper/add', { body: { input, title, star: !!star, from: 'extension' } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.job) return { error: j.error || 'The bridge refused it (' + r.status + ').' };
  const { mine = {} } = await chrome.storage.local.get('mine');
  mine[j.job.id] = { k: k || '', at: Date.now() };
  for (const [id, m] of Object.entries(mine)) if (Date.now() - m.at > 7 * 864e5) delete mine[id];
  await chrome.storage.local.set({ mine });
  await follow([slim(j.job)]);
  return { job: slim(j.job) };
}

async function addPending() {
  const { pending = [] } = await chrome.storage.local.get('pending');
  if (!pending.length) return;
  await chrome.storage.local.set({ pending: [] });
  for (const p of pending) await add(p);
}

let _following = false;
/* Keep the jobs current while any is waiting or being added: every couple of seconds (each storage write also keeps this
   worker alive); an alarm picks it up again if the browser stopped the worker meanwhile. */
async function follow(seed) {
  if (seed) await mergeJobs(seed);
  if (_following) return;
  _following = true;
  chrome.alarms.create('follow', { periodInMinutes: 0.5 });
  try {
    for (let quiet = 0; quiet < 3;) {
      await sleep(2000);
      let jobs;
      try { const r = await bridgeFetch('/paper/adds'); if (!r.ok) throw 0; jobs = (await r.json()).jobs.map(slim); }
      catch (e) { quiet++; continue; }   // the Mac asleep or the bridge restarting: try again shortly
      const any = await mergeJobs(jobs, true);
      quiet = any ? 0 : quiet + 1;
    }
  } finally {
    _following = false;
    const { jobs = [] } = await chrome.storage.local.get('jobs');
    if (!jobs.some(j => pendingState(j.state))) chrome.alarms.clear('follow');
  }
}

/* Store the bridge's view of the jobs; tell about the ones of mine that just finished. Returns whether any is still going. */
async function mergeJobs(list, whole) {
  const { jobs: old = [], mine = {} } = await chrome.storage.local.get(['jobs', 'mine']);
  const was = new Map(old.map(j => [j.id, j]));
  const jobs = whole ? list.slice(0, 30) : list.concat(old.filter(o => !list.some(j => j.id === o.id))).slice(0, 30);
  for (const j of jobs) {
    const before = was.get(j.id);
    if (mine[j.id] && before && pendingState(before.state) && !pendingState(j.state)) finished(j);
    if (j.state === 'done' && j.added.length && !(before && before.state === 'done')) rememberAdded(j.added);
  }
  await chrome.storage.local.set({ jobs });
  const n = jobs.filter(j => mine[j.id] && pendingState(j.state)).length;
  chrome.action.setBadgeText({ text: n ? String(n) : '' });
  return jobs.some(j => pendingState(j.state));
}

function finished(j) {
  const first = j.added[0] || j.existing[0];
  const title = j.state === 'done' ? (j.added.length ? 'Added to your library' : 'Already in your library') : 'Not added';
  const msg = j.state === 'done' ? (first && first.title) || j.title || j.input : (j.title ? j.title + ': ' : '') + (j.message || 'something went wrong');
  chrome.notifications.create('job:' + j.id + ':' + (first ? first.id : ''), { type: 'basic', iconUrl: 'icons/128.png', title, message: msg.slice(0, 300), priority: 0 }, () => void chrome.runtime.lastError);
}
chrome.notifications.onClicked.addListener(nid => {
  const id = nid.split(':')[2];
  if (id) chrome.tabs.create({ url: paperUrl(id) });
  chrome.notifications.clear(nid);
});
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'follow') follow(); });

/* ---------- connecting to the Mac: a small window of its own (the popup would close as the Mac's dialog takes the focus) ---------- */
let _connectWin = null;
async function openConnect(state) {
  if (_connectWin) { try { await chrome.windows.update(_connectWin, { focused: true }); return; } catch (e) { _connectWin = null; } }
  const w = await chrome.windows.create({ url: 'connect.html' + (state ? '#' + state : ''), type: 'popup', width: 420, height: 360, focused: true });
  _connectWin = w.id;
}
chrome.windows.onRemoved.addListener(async id => {
  if (id !== _connectWin) return;
  _connectWin = null;
  if ((await status()).state !== 'ready') chrome.storage.local.set({ pending: [] });   // closed without connecting: those papers aren't waiting any more
});

/* ---------- the right-click menu ---------- */
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'add-link', title: 'Add link to your library', contexts: ['link'] });
    chrome.contextMenus.create({ id: 'add-page', title: 'Add this paper to your library', contexts: ['page'] });
  });
  libIndex(true);
});
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'add-link') return add({ input: info.linkUrl, title: '', star: false, k: '' });
  const meta = await pageMeta(tab);
  add({ input: meta.input, title: meta.title, star: false, k: '' });
});

/* What paper a page is about, from the tags publishers and arXiv put in it (citation_*, Dublin Core, Open Graph) */
function readMeta() {
  const all = n => [...document.querySelectorAll(`meta[name="${n}" i], meta[property="${n}" i]`)].map(e => (e.content || '').trim()).filter(Boolean);
  const one = (...ns) => { for (const n of ns) { const v = all(n)[0]; if (v) return v; } return ''; };
  const canon = document.querySelector('link[rel="canonical"]');
  return { citationTitle: one('citation_title', 'dc.title', 'eprints.title'), ogTitle: one('og:title', 'twitter:title'), docTitle: document.title,
    authors: all('citation_author').length ? all('citation_author') : all('dc.creator'),
    date: one('citation_publication_date', 'citation_date', 'citation_online_date', 'dc.date'),
    venue: one('citation_conference_title', 'citation_journal_title', 'citation_inbook_title'),
    doi: one('citation_doi', 'prism.doi', 'bepress_citation_doi', 'dc.identifier'), arxiv: one('citation_arxiv_id'),
    pdf: one('citation_pdf_url'), canonical: canon ? canon.href : '', url: location.href };
}
async function pageMeta(tab) {
  let m = {};
  try { [{ result: m }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readMeta }); } catch (e) {}
  return describePage({ ...m, url: m.url || tab.url, docTitle: m.docTitle || tab.title });
}
/* The best thing to ask for: the arXiv page, else the DOI, else the page itself; and how sure we are that it's a paper */
function describePage(m) {
  const url = m.url || '';
  const ax = (m.arxiv || '').match(/\d{4}\.\d{4,5}/) || url.match(/arxiv\.org\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5})/) || [];
  const axId = ax[1] || ax[0] || '';
  const doiRaw = ((m.doi || '').match(/10\.\d{4,9}\/\S+/) || url.match(/10\.\d{4,9}\/[^\s?#&]+/) || [''])[0].replace(/[.)]+$/, '');
  const link = axId ? 'https://arxiv.org/abs/' + axId : doiRaw ? 'https://doi.org/' + doiRaw : (/^https?:/.test(m.canonical) ? m.canonical : url);
  const title = (m.citationTitle || '').trim();
  const isPaper = !!(title || axId || doiRaw);
  const fallback = (m.ogTitle || m.docTitle || '').trim();
  const lines = [link];
  if (title) lines.push('Title: ' + title);
  if (m.authors && m.authors.length) lines.push('Authors: ' + m.authors.slice(0, 12).join(', '));
  return { isPaper, input: lines.join('\n'), link, title: title || (isPaper ? fallback : ''), pageTitle: fallback,
    authors: m.authors || [], date: m.date || '', venue: m.venue || '', web: /^https?:/.test(url), urls: [url, m.pdf || '', link] };
}

/* ---------- messages from the popup, the connect window and pages ---------- */
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const go = async () => {
    switch (msg.type) {
      case 'status': return status();
      case 'lookup': return lookup(msg.items || []);
      case 'add': return add(msg);
      case 'page': return pageMeta(msg.tab);
      case 'jobs': { const s = await chrome.storage.local.get(['jobs', 'mine']); return s; }
      case 'refresh': { const r = await bridgeFetch('/paper/adds').catch(() => null); if (r && r.ok) await mergeJobs((await r.json()).jobs.map(slim), true); follow(); return true; }
      case 'stop': { await bridgeFetch('/paper/add/stop', { body: { id: msg.id } }).catch(() => {}); follow(); return true; }
      case 'connect': return openConnect(msg.state);
      case 'connected': addPending(); follow(); return true;   // from the connect window
      case 'open': chrome.tabs.create({ url: paperUrl(msg.id) }); return true;
    }
  };
  go().then(reply, e => reply({ error: String(e && e.message || e) }));
  return true;
});

follow();   // the worker woke: pick up anything still being added

/* The toolbar popup: the paper on this page (in your library already? add it, star it), what's being added, and the
   connection to your Mac. Everything it does goes through the background (background.js). */
const send = m => chrome.runtime.sendMessage(m);
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const pending = s => s === 'queued' || s === 'running';

let page = null, conn = 'unknown', inLib = null, jobs = [], star = false, typed = '', myJob = null, cardSig = '';

const firstLine = s => (s || '').split('\n')[0].trim();
const jobTitle = j => j.title || (j.added[0] || j.existing[0] || {}).title || firstLine(j.input);
const personName = n => n.includes(',') ? n.split(',').slice(1).join(' ').trim() + ' ' + n.split(',')[0].trim() : n;   // "Kim, Jaeik" → "Jaeik Kim"
function shortAuthors(a) { a = a.map(personName); return !a.length ? '' : a.length > 2 ? a[0] + ' et al.' : a.join(' and '); }
function metaLine() {
  const y = (page.date.match(/\d{4}/) || [''])[0];
  return [shortAuthors(page.authors), page.venue || hostOf(page.link), y].filter(Boolean).join(' · ');
}
function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } }
function waitText(j) { return j.state === 'queued' ? (j.ahead ? `Waiting · ${j.ahead} ahead of it` : 'Waiting · next') : (j.stage || 'Adding…'); }

/* the job this card is about: the one just asked for here, else the latest asked for this page's paper */
function cardJob() {
  return (myJob && jobs.find(j => j.id === myJob)) || (page && page.isPaper && jobs.find(j => firstLine(j.input) === page.link)) || null;
}

function renderConn() {
  const c = $('conn');
  const say = { unpaired: ['This browser isn’t connected to your Mac yet.', 'Connect'],
    offline: ['Your Mac isn’t reachable: it needs to be awake, with the bridge running.', 'Connect…'],
    old: ['The bridge on your Mac needs restarting to work with this.', 'How'] }[conn];
  c.innerHTML = say ? `<div class="conn"><span class="grow">${say[0]}</span><button class="plain small" id="connBtn">${say[1]}</button></div>` : '';
  if (say) $('connBtn').onclick = () => { send({ type: 'connect', state: conn }); window.close(); };
}

function renderCard() {
  const j = cardJob();
  const done = j && j.state === 'done' && (j.added[0] || j.existing[0]);
  const libId = inLib || (done && done.id);
  const sig = JSON.stringify([page.isPaper, libId, j && [j.id, j.state, j.stage, j.ahead, j.message]]);
  if (sig === cardSig) return;
  cardSig = sig;
  const head = (kicker, cls) => `<div class="kicker ${cls || ''}">${cls === 'in' ? ICON_CHECK : ''}${kicker}</div>`;
  const title = t => `<h1 class="title">${esc(t)}</h1>`;
  const name = page.isPaper ? page.title || page.pageTitle : (j ? jobTitle(j) : '');
  let html;
  if (j && pending(j.state)) {
    html = head('Adding to your library') + title(j.title || name || firstLine(j.input))
      + `<div class="status"><span class="spin"></span><span class="grow">${esc(waitText(j))}</span><button class="plain small" id="stopBtn">Stop</button></div>
         <p class="note">You can close this: the paper keeps going on your Mac, and you’ll be told when it’s in.</p>`;
  } else if (libId) {
    const t = (done && done.title) || name;
    html = head(j && j.state === 'done' && j.added.length ? 'Added to your library' : 'In your library', 'in') + title(t)
      + (page.isPaper ? `<div class="meta">${esc(metaLine())}</div>` : '')
      + `<div class="row"><button class="go" id="openBtn">Open in your library</button></div>`;
  } else if (page.isPaper) {
    const failed = j && !pending(j.state) && j.state !== 'done';
    html = head(failed ? 'Not added' : 'On this page', failed ? 'warn' : '') + title(name) + `<div class="meta">${esc(metaLine())}</div>`
      + (failed ? `<div class="msg">${esc(j.message || 'Something went wrong.')}</div>` : '')
      + `<div class="row"><button class="go" id="addBtn">${failed ? 'Try again' : 'Add to library'}</button><label class="star"><input type="checkbox" id="star" ${star ? 'checked' : ''}> Star it</label></div>`
      + (failed ? '' : `<p class="note">Claude Code on your Mac finds the paper, reads its abstract, matches it to your interests and adds it (about a minute).</p>`);
  } else {
    const failed = j && !pending(j.state) && j.state !== 'done';
    html = head(failed ? 'Not added' : 'Add a paper', failed ? 'warn' : '')
      + (failed ? `<div class="msg">${esc(j.message || 'Something went wrong.')}</div>` : '')
      + `<form class="row" id="form"><input class="input" id="input" type="text" autocomplete="off" spellcheck="false" placeholder="arXiv or DOI link, or the paper’s title" value="${esc(typed)}"><button class="go" id="addBtn" ${typed.trim() ? '' : 'disabled'}>Add</button></form>
         <label class="star" style="margin:10px 0 0"><input type="checkbox" id="star" ${star ? 'checked' : ''}> Star it</label>
         <p class="note">${page.web ? 'This page doesn’t name a paper. Add its link anyway, or ' : ''}${page.web ? 'paste' : 'Paste'} a link, a DOI or a title: Claude Code on your Mac finds the paper and adds it. A paper with no public version isn’t added.</p>`;
  }
  const card = $('card');
  const hadFocus = document.activeElement && document.activeElement.id;
  card.innerHTML = html;
  const on = (id, ev, f) => { const el = $(id); if (el) el[ev] = f; };
  on('star', 'onchange', e => { star = e.target.checked; });
  on('openBtn', 'onclick', () => { send({ type: 'open', id: libId }); window.close(); });
  on('stopBtn', 'onclick', () => send({ type: 'stop', id: j.id }));
  on('input', 'oninput', e => { typed = e.target.value; $('addBtn').disabled = !typed.trim(); });
  on('form', 'onsubmit', e => { e.preventDefault(); addTyped(); });
  if (page.isPaper) on('addBtn', 'onclick', addPage);
  const focus = $(hadFocus) || $('input') || $('addBtn') || $('openBtn');
  if (focus) {
    if (!hadFocus && !keyed) focus.dataset.auto = '';   // ready for Enter, without a keyboard ring the click that opened this didn't ask for
    focus.focus();
    if (focus.id === 'input') focus.setSelectionRange(focus.value.length, focus.value.length);
  }
}

function renderRecent() {
  const cj = cardJob();
  const list = jobs.filter(j => !cj || j.id !== cj.id).slice(0, 5);
  const r = $('recent');
  r.hidden = !list.length;
  if (!list.length) { r.innerHTML = ''; return; }
  r.className = 'recent';
  r.innerHTML = '<h2>Recently asked for</h2>' + list.map(j => {
    const hit = j.state === 'done' && (j.added[0] || j.existing[0]);
    const s = pending(j.state) ? `<div class="s"><span class="spin"></span>${esc(waitText(j))}</div>`
      : j.state === 'done' ? `<div class="s ok">${j.added.length ? 'Added' : hit ? 'Already in your library' : 'Nothing added'}</div>`
      : `<div class="s bad">Not added: ${esc(j.message || 'something went wrong')}</div>`;
    return `<div class="job"><div class="t"><div class="h" title="${esc(jobTitle(j))}">${esc(jobTitle(j))}</div>${s}</div>${hit ? `<button class="plain small" data-open="${esc(hit.id)}">Open</button>` : ''}</div>`;
  }).join('');
  r.querySelectorAll('[data-open]').forEach(b => b.onclick = () => { send({ type: 'open', id: b.dataset.open }); window.close(); });
}

function render() { renderConn(); renderCard(); renderRecent(); }
let keyed = false;   // once a key moves around, focus shows as usual
addEventListener('keydown', e => { if (e.key !== 'Enter') { keyed = true; document.querySelectorAll('[data-auto]').forEach(el => delete el.dataset.auto); } }, true);

async function add(input, title) {
  if (!input.trim()) return;
  const r = await send({ type: 'add', input, title, star, k: page.isPaper ? 'page:' + page.link : '' });
  if (r && r.waiting) { window.close(); return; }   // the connect window takes over, then adds it
  if (r && r.job) { myJob = r.job.id; jobs = [r.job].concat(jobs.filter(j => j.id !== r.job.id)); typed = ''; }
  else if (r && r.error) { conn = (await send({ type: 'status' })).state; }
  render();
}
const addPage = () => add(page.input, page.title);
const addTyped = () => add(typed, '');

chrome.storage.onChanged.addListener(c => {
  if (c.jobs) { jobs = c.jobs.newValue || []; renderCard(); renderRecent(); }
  if (c.lib && page && page.isPaper && !inLib) send({ type: 'lookup', items: [{ k: 'page', title: page.title, urls: page.urls }] }).then(r => { inLib = r.page || null; renderCard(); });
});

(async () => {
  $('card').innerHTML = '<div class="skel" style="width:90px"></div><div class="skel t"></div><div class="skel t2"></div><div class="skel b"></div>';
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const [p, st, s] = await Promise.all([send({ type: 'page', tab: { id: tab.id, url: tab.url || '', title: tab.title || '' } }), send({ type: 'status' }), send({ type: 'jobs' })]);
  page = p; conn = st.state; jobs = s.jobs || [];
  typed = page.isPaper ? '' : page.web ? page.link : '';
  if (page.isPaper) inLib = (await send({ type: 'lookup', items: [{ k: 'page', title: page.title, urls: page.urls }] })).page || null;
  render();
  send({ type: 'refresh' });
})();

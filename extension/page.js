/* Scholar Dashboard on Google Scholar and in Scholar alert emails (Gmail): an "Add to library" button on every paper,
   which shows instead when a paper is already in your library, waiting, or being added.
     Scholar: search results (and Cited by, My library), author profiles' paper lists, a paper's citation page.
     Gmail: each paper's title in a Scholar alert email (its link goes through scholar.google.com/scholar_url).
   The DOM is built with createElement and textContent only: Gmail allows no HTML strings (Trusted Types). */
(() => {
  if (window.__scholarDashboard) return;
  window.__scholarDashboard = true;
  const onScholar = /^scholar\.google\./.test(location.hostname), onGmail = location.hostname === 'mail.google.com';
  if (!onScholar && !onGmail) return;
  const DASHBOARD = 'https://www.sumanthtangirala.com/scholar-dashboard/';
  const items = new Map();   // k -> { k, title, urls, input, els: [{ btn, lbl, ico, why }] }
  const inLib = new Map();   // k -> paper id
  let jobs = [], mine = {}, pending = [];
  const norm = t => (t || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9]/g, '');
  const alive = () => { try { return !!chrome.runtime.id; } catch (e) { return false; } };   // false once the extension is reloaded

  /* ---------- icons ---------- */
  const NS = 'http://www.w3.org/2000/svg';
  const PATHS = {
    add: 'M12 3L1 9l4 2.18v6L12 21l7-3.82v-6l2-1.09V17h2V9L12 3zm6.82 6L12 12.72 5.18 9 12 5.28 18.82 9zM17 15.99l-5 2.73-5-2.73v-3.72L12 15l5-2.73v3.72z',
    in: 'M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2z',
    fail: 'M11 15h2v2h-2zm0-8h2v6h-2zm1-5C6.47 2 2 6.5 2 12a10 10 0 1 0 10-10zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16z',
  };
  function icon() {
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true'); s.setAttribute('class', 'sd-ico');
    s.appendChild(document.createElementNS(NS, 'path'));
    return s;
  }
  function setIcon(svg, kind) {
    svg.classList.toggle('sd-spin', kind === 'busy');
    if (kind === 'busy') {   // a ring with a gap, turning
      svg.firstChild.setAttribute('d', 'M12 3a9 9 0 1 0 9 9h-2.2A6.8 6.8 0 1 1 12 5.2z');
    } else svg.firstChild.setAttribute('d', PATHS[kind] || PATHS.add);
  }

  /* ---------- finding the papers on the page ---------- */
  const text = el => (el ? el.textContent : '').replace(/\s+/g, ' ').trim();
  function scholarUrl(href) {   // the real link inside a Scholar (or Gmail-wrapped Scholar) redirect
    try {
      let u = new URL(href);
      if (/google\.[^/]+$/.test(u.hostname) && u.pathname === '/url' && u.searchParams.get('q')) u = new URL(u.searchParams.get('q'));
      return u.searchParams.get('url') || '';
    } catch (e) { return ''; }
  }
  function scan() {
    if (!alive()) return;
    const found = [];
    const take = (k, data, mount) => {
      if (!k) return;
      let it = items.get(k);
      if (!it) { it = { k, ...data, els: [] }; items.set(k, it); found.push(it); }
      it.els.push(mount(it));
      paint(it);
    };
    if (onScholar) {
      // results: search, Cited by, My library
      document.querySelectorAll('.gs_r.gs_or').forEach(r => {
        if (r.dataset.sd) return;
        const h3 = r.querySelector('h3.gs_rt'), foot = r.querySelector('.gs_ri > .gs_fl');
        if (!h3 || !foot) return;
        r.dataset.sd = '1';
        const a = h3.querySelector('a[href]');
        const clone = h3.cloneNode(true);
        clone.querySelectorAll('.gs_ctg2, .gs_ct1, .gs_ct2, .gs_ctc, .gs_ctu').forEach(x => x.remove());   // [PDF], [CITATION] and such
        const title = text(a || clone).replace(/^(\[[A-Z]+\]\s*)+/, '');
        const link = a ? a.href : '', pdf = (r.querySelector('.gs_or_ggsm a[href]') || {}).href || '';
        const by = text(r.querySelector('.gs_a'));
        take(norm(title) || link, { title, urls: [link, pdf], input: [link, 'Title: ' + title, by && 'On Google Scholar: ' + by].filter(Boolean).join('\n') },
          it => mountScholar(it, foot));
      });
      // an author's profile: one row per paper
      document.querySelectorAll('tr.gsc_a_tr').forEach(r => {
        if (r.dataset.sd) return;
        const a = r.querySelector('a.gsc_a_at'), cell = r.querySelector('td.gsc_a_t');
        if (!a || !cell) return;
        r.dataset.sd = '1';
        const gray = [...cell.querySelectorAll('.gs_gray')].map(text);
        const title = text(a), year = text(r.querySelector('.gsc_a_y'));
        take(norm(title), { title, urls: [], input: ['Title: ' + title, gray[0] && 'Authors: ' + gray[0], (gray[1] || year) && 'Published: ' + [gray[1], year].filter(Boolean).join(', ')].filter(Boolean).join('\n') },
          it => mountMini(it, cell));
      });
      // one paper's citation page
      const oci = document.getElementById('gsc_oci_title');
      if (oci && !oci.dataset.sd) {
        oci.dataset.sd = '1';
        const a = oci.querySelector('a[href]'), title = text(oci), pdf = document.querySelector('#gsc_oci_title_gg a[href]');
        const fields = {};
        document.querySelectorAll('#gsc_oci_table .gs_scl').forEach(row => { fields[text(row.querySelector('.gsc_oci_field')).toLowerCase()] = text(row.querySelector('.gsc_oci_value')); });
        const link = a ? a.href : '';
        take(norm(title) || link, { title, urls: [link, pdf ? pdf.href : ''], input: [link, 'Title: ' + title, fields.authors && 'Authors: ' + fields.authors,
          fields['publication date'] && 'Published: ' + fields['publication date']].filter(Boolean).join('\n') }, it => mountBlock(it, oci));
      }
    }
    if (onGmail) {
      document.querySelectorAll('a[href*="scholar_url"]').forEach(a => {
        if (a.dataset.sd) return;
        const url = scholarUrl(a.href), title = text(a);
        if (!url || title.length < 8) return;   // the title links only (not Save, share or "see all")
        a.dataset.sd = '1';
        const by = text(a.closest('h3') && a.closest('h3').nextElementSibling);
        take(norm(title) || url, { title, urls: [url], input: [url, 'Title: ' + title, by && 'From a Google Scholar alert: ' + by].filter(Boolean).join('\n') },
          it => mountPill(it, a));
      });
    }
    if (found.length) {
      chrome.runtime.sendMessage({ type: 'lookup', items: found.map(({ k, title, urls }) => ({ k, title, urls })) }, res => {
        if (chrome.runtime.lastError || !res) return;
        for (const [k, id] of Object.entries(res)) inLib.set(k, id);
        found.forEach(paint);
      });
    }
  }

  /* ---------- the buttons ---------- */
  function button(it, cls) {
    const btn = document.createElement('a');
    btn.href = '#'; btn.setAttribute('role', 'button'); btn.className = 'sd-add ' + cls;
    const ico = icon(), lbl = document.createElement('span');
    lbl.className = 'sd-lbl';
    btn.append(ico, lbl);
    btn.addEventListener('click', e => click(e, it));
    btn.addEventListener('keydown', e => { if (e.key === ' ') click(e, it); });
    return { btn, ico, lbl };
  }
  function whyLine(cls) { const w = document.createElement('div'); w.className = 'sd-why ' + cls; w.hidden = true; return w; }
  function mountScholar(it, foot) {   // in the row of links under the result, after Save and Cite (styled as Scholar's own)
    const el = button(it, 'gs_or_btn sd-scholar');
    const after = foot.querySelector('.gs_or_cit') || foot.querySelector('.gs_or_sav');
    if (after) after.after(el.btn); else foot.prepend(el.btn);
    el.why = whyLine('sd-why-scholar'); foot.after(el.why);
    return el;
  }
  function mountMini(it, cell) {   // under the paper's authors and venue in a profile's list
    const el = button(it, 'sd-mini');
    const wrap = document.createElement('div'); wrap.className = 'sd-mini-row';
    wrap.appendChild(el.btn); cell.appendChild(wrap);
    el.why = whyLine('sd-why-mini'); cell.appendChild(el.why);
    return el;
  }
  function mountBlock(it, oci) {
    const el = button(it, 'sd-block');
    const wrap = document.createElement('div'); wrap.className = 'sd-block-row';
    wrap.appendChild(el.btn); oci.appendChild(wrap);   // under the title, inside its box (the PDF links sit beside it)
    el.why = whyLine('sd-why-block'); wrap.after(el.why);
    return el;
  }
  function mountPill(it, a) {   // beside the title in the email, after a plain space (which disappears if the chip wraps to the next line)
    const el = button(it, 'sd-pill');
    a.after(document.createTextNode(' '), el.btn);
    el.why = whyLine('sd-why-pill');
    (a.closest('h3') || a.parentElement).after(el.why);
    return el;
  }

  function jobFor(it) {
    return jobs.find(j => mine[j.id] && mine[j.id].k === it.k) || jobs.find(j => (j.input || '').split('\n')[0] === it.input.split('\n')[0] && it.urls[0]) || null;
  }
  function paint(it) {
    const j = jobFor(it);
    const hit = j && j.state === 'done' && (j.added[0] || j.existing[0]);
    const id = inLib.get(it.k) || (hit && hit.id);
    let state = 'add', label = 'Add to library', tip = 'Add this paper to your Scholar Dashboard library', why = '';
    if (j && j.state === 'queued') { state = 'busy'; label = 'Waiting…'; tip = j.ahead ? `${j.ahead} ${j.ahead === 1 ? 'paper' : 'papers'} ahead of it` : 'Next to be added'; }
    else if (j && j.state === 'running') { state = 'busy'; label = 'Adding…'; tip = j.stage || 'Adding it to your library'; }
    else if (pending.some(p => p.k === it.k)) { state = 'busy'; label = 'Connecting…'; tip = 'Waiting for your Mac'; }
    else if (id) { state = 'in'; label = 'In your library'; tip = 'Open it in your library'; }
    else if (j && (j.state === 'stopped' || j.state === 'error')) { state = 'fail'; label = 'Try again'; tip = 'Not added'; why = 'Not added: ' + (j.message || 'something went wrong'); }
    for (const el of it.els) {
      el.btn.dataset.state = state;
      el.lbl.textContent = label;
      el.btn.title = tip;
      setIcon(el.ico, state);
      if (state === 'in') { el.btn.href = DASHBOARD + '#paper/' + encodeURIComponent(id); el.btn.target = '_blank'; el.btn.rel = 'noopener'; el.btn.removeAttribute('role'); }
      else { el.btn.href = '#'; el.btn.removeAttribute('target'); el.btn.setAttribute('role', 'button'); }
      el.btn.setAttribute('aria-disabled', String(state === 'busy'));
      if (el.why) { el.why.textContent = why; el.why.hidden = !why; }
    }
  }
  function click(e, it) {
    const state = e.currentTarget.dataset.state;
    if (state === 'in') return;   // a real link to the paper
    e.preventDefault(); e.stopPropagation();
    if (state === 'busy' || !alive()) return;
    it.els.forEach(el => { el.btn.dataset.state = 'busy'; el.lbl.textContent = 'Adding…'; setIcon(el.ico, 'busy'); });
    chrome.runtime.sendMessage({ type: 'add', input: it.input, title: it.title, k: it.k, star: false }, res => {
      if (chrome.runtime.lastError || !res || res.error) {
        it.els.forEach(el => { if (el.why) { el.why.textContent = 'Not added: ' + ((res && res.error) || 'the extension didn’t answer'); el.why.hidden = false; } });
      }
      paint(it);
    });
  }

  /* ---------- keeping up ---------- */
  chrome.storage.local.get(['jobs', 'mine', 'pending'], s => {
    jobs = s.jobs || []; mine = s.mine || {}; pending = s.pending || [];
    scan();
  });
  chrome.storage.onChanged.addListener(c => {
    if (!alive()) return;
    if (c.jobs) jobs = c.jobs.newValue || [];
    if (c.mine) mine = c.mine.newValue || {};
    if (c.pending) pending = c.pending.newValue || [];
    if (c.jobs || c.mine || c.pending) items.forEach(paint);
  });
  let t = 0;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(scan, 300); }).observe(document.body, { childList: true, subtree: true });
})();

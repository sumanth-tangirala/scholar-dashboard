#!/usr/bin/env python3
"""Check that papers in the database are real, and described by their own source.

An earlier run stored a placeholder arXiv id and a made-up abstract for a paper (BURNS, fixed 2026-10-20), and the
audit that followed found links that open a different paper and abstracts that aren't the paper's. So both digest
modes run this on the papers they add, before syncing, and fix what it reports (CLAUDE.md, step 9.5).

  python3 verify_papers.py --since 2026-10-20      papers added on or after a date (a run's own)
  python3 verify_papers.py --ids a,b,c              these papers
  python3 verify_papers.py --all --csv audit.csv    every paper (an audit: a few minutes, gentle with arXiv)

Read only: it never changes the database. For each paper:
  arXiv link   the id must exist, arXiv's title must be the paper's title, and the stored abstract must be arXiv's own
               words (copied, not reworded or cut short; a venue's longer version that holds arXiv's text is fine).
               The real title, abstract and date are printed to copy in.
  other link   it must open, and be the paper's own page (not a search, a homepage or a "not found").
               Publishers that turn scripts away (403) can't be checked here: open those with the browser tools.
  both         url and pdf_url must name the same arXiv paper, and no id may look like a placeholder (2604.00000).
For a link that's wrong or dead, arXiv is searched for the paper's exact title and any match is suggested.
Exit status: 0 all verified, 1 something to fix, 2 nothing to fix but some papers couldn't be checked.
A paper that still can't be verified is kept, with its notes starting "Unverified: <why>." (the user's choice).
"""
import argparse, csv, difflib, html, json, os, re, ssl, sys, time, urllib.error, urllib.parse, urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
UA_SCRIPT = 'scholar-dashboard verify_papers.py (a personal research dashboard)'
UA_BROWSER = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
ARXIV_API = 'https://export.arxiv.org/api/query'
ARXIV_ID = re.compile(r'arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})', re.I)


def words(s):
    s = re.sub(r'\$[^$]*\$', ' ', s or '')   # LaTeX in arXiv titles
    return re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split()


def contained(a, b):
    """Share of a's words that appear, in order, in b (1.0: a is b, or a stretch of it)."""
    a, b = words(a), words(b)
    if not a:
        return 0.0
    return sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()) / len(a)


def same_title(a, b):
    return ' '.join(words(a)) == ' '.join(words(b))


def fetch(url, ua=UA_SCRIPT, limit=600000, timeout=40):
    req = urllib.request.Request(url, headers={'User-Agent': ua, 'Accept': 'text/html,application/xml,application/pdf,*/*'})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        ctype = r.headers.get('Content-Type') or ''
        return r.status, r.geturl(), ctype, r.read(limit)


def arxiv_entries(query):
    """arXiv's records for an API query: {id: {title, abstract, published}}. Raises when arXiv can't be reached."""
    for attempt in range(3):
        try:
            body = fetch(ARXIV_API + '?' + query)[3].decode('utf-8', 'ignore')
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(8)
    out = {}
    for e in body.split('<entry>')[1:]:
        m = re.search(r'<id>https?://arxiv\.org/abs/([^<]+?)(v\d+)?</id>', e)
        if not m:
            continue
        get = lambda tag: html.unescape(re.sub(r'\s+', ' ', (re.search(rf'<{tag}>(.*?)</{tag}>', e, re.S) or [None, ''])[1])).strip()
        out[m.group(1)] = {'title': get('title'), 'abstract': get('summary'), 'published': get('published')[:10]}
    time.sleep(3.2)   # arXiv asks for a pause between API calls
    return out


def arxiv_lookup(ids):
    got = {}
    ids = sorted(set(ids))
    for k in range(0, len(ids), 100):
        got.update(arxiv_entries('max_results=200&id_list=' + ','.join(ids[k:k + 100])))
    return got


STOP = {'a', 'an', 'the', 'of', 'for', 'and', 'with', 'via', 'in', 'on', 'to', 'from', 'by', 'using', 'is', 'are', 'at', 'as'}


def arxiv_search(title):
    """The arXiv paper with this exact title, if there is one: (id, record) or None. A phrase search first (arXiv's
    word search skips short words like "the", so asking for every word finds nothing), then the title's first
    words, then its longer words."""
    w = words(title)
    tries = ['ti:"%s"' % ' '.join(w), 'ti:"%s"' % ' '.join(w[:6]), ' AND '.join('ti:' + x for x in w if x not in STOP and len(x) > 2)]
    for q in dict.fromkeys(t for t in tries if len(t) > 6):
        best = None
        for aid, rec in arxiv_entries('max_results=10&search_query=' + urllib.parse.quote(q[:900])).items():
            score = difflib.SequenceMatcher(None, w, words(rec['title'])).ratio()
            if score >= 0.85 and (not best or score > best[0]):
                best = (score, aid, rec)
        if best:
            return best[1], best[2]
    return None


def month_year(iso):
    try:
        return time.strftime('%b %Y', time.strptime(iso, '%Y-%m-%d'))
    except ValueError:
        return ''


def check_other_link(url, title):
    """(kind, problem) for a link that isn't arXiv: kind is ok, fail or check."""
    p = urllib.parse.urlparse(url)
    if re.search(r'scholar\.google\.|google\.com/search|arxiv\.org/(a/|search|list)|semanticscholar\.org/search', url):
        return 'fail', 'the link is a search page, not the paper'
    if not p.path.strip('/') and not p.query:
        return 'fail', 'the link is a site\'s homepage, not the paper'
    if p.netloc.endswith('openreview.net'):   # (its pages and its API both ask scripts to pass a challenge)
        return 'check', 'OpenReview turns scripts away: open it with the browser tools'
    try:
        status, final, ctype, body = fetch(url, UA_BROWSER)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 400):
            return 'fail', f'the link doesn’t open ({e.code})'
        return 'check', f'the site turns scripts away ({e.code}): open it with the browser tools'
    except Exception as e:
        return 'check', f'couldn’t reach it ({type(e).__name__})'
    if status == 202:
        return 'check', 'the site answered "accepted" without the page (likely blocks scripts): open it with the browser tools'
    if 'html' in ctype:
        text = body.decode('utf-8', 'ignore')
        if contained(title, text) < 0.6 and len(set(words(title)) & set(words(text))) < 0.6 * len(set(words(title))):
            return 'check', 'the page doesn’t show the paper’s title (wrong page, or one built by scripts): check it'
    return 'ok', ''


def verify(rows, suggest=True, progress=None):
    """One result per row: {id, kind, problems, real_*}"""
    results = {r['id']: {'id': r['id'], 'kind': 'ok', 'problems': [], 'real_title': '', 'real_abstract': '', 'real_date': '', 'suggested_link': ''} for r in rows}
    def flag(rid, kind, problem):
        res = results[rid]
        res['problems'].append(problem)
        if kind == 'fail' or res['kind'] == 'ok':
            res['kind'] = kind
    ax = {}
    for r in rows:
        if not (r.get('url') or '').strip():
            flag(r['id'], 'fail', 'no url')
        ids = {col: (ARXIV_ID.search(r.get(col) or '') or [None, None])[1] for col in ('url', 'pdf_url')}
        if ids['url'] and ids['pdf_url'] and ids['url'] != ids['pdf_url']:
            flag(r['id'], 'fail', f"url and pdf_url name different arXiv papers ({ids['url']} / {ids['pdf_url']})")
        aid = ids['url'] or ids['pdf_url']
        if aid:
            ax[r['id']] = aid
            if re.fullmatch(r'\d{4}\.(0+|0*1|(\d)\2{3,4})', aid):
                flag(r['id'], 'fail', f'arXiv id {aid} looks like a placeholder')
    try:
        real = arxiv_lookup(ax.values()) if ax else {}
    except Exception as e:
        real = None
        for rid in ax:
            flag(rid, 'check', f'couldn’t reach arXiv ({type(e).__name__}): check the link by hand')
    lost = []
    for n, r in enumerate(rows):
        rid, res = r['id'], results[r['id']]
        if progress:
            progress(n, len(rows))
        if rid in ax and real is not None:
            rec = real.get(ax[rid])
            if not rec:
                flag(rid, 'fail', f'arXiv id {ax[rid]} doesn’t exist'); lost.append(r); continue
            t_in = contained(r['title'], rec['title']) if r['title'] else 0
            a_in = contained(r.get('abstract', ''), rec['abstract'])
            if not same_title(r['title'], rec['title']) and t_in < 0.6 and a_in < 0.4:
                flag(rid, 'fail', f'the arXiv link ({ax[rid]}) opens a different paper: “{rec["title"]}”'); lost.append(r); continue
            res.update(real_title=rec['title'], real_abstract=rec['abstract'], real_date=month_year(rec['published']))
            if not same_title(r['title'], rec['title']):
                flag(rid, 'fail', 'the title isn’t arXiv’s exact title')
            if not (r.get('abstract') or '').strip():
                flag(rid, 'fail', 'no abstract')
            elif a_in < 0.9 and contained(rec['abstract'], r['abstract']) < 0.9:   # (a venue's longer version holds arXiv's)
                flag(rid, 'fail', f'the abstract isn’t arXiv’s text (only {round(a_in * 100)}% of it is): copy the real one')
            elif len(words(r['abstract'])) < 0.8 * len(words(rec['abstract'])):
                flag(rid, 'fail', 'the abstract is cut short: copy all of it')
        elif rid not in ax:
            kind, problem = check_other_link(r['url'], r['title']) if (r.get('url') or '').strip() else ('ok', '')
            if kind != 'ok':
                flag(rid, kind, problem)
                if kind == 'fail':
                    lost.append(r)
    if suggest:
        for r in lost:
            try:
                hit = arxiv_search(r['title'])
            except Exception:
                hit = None
            if hit:
                results[r['id']].update(suggested_link=f'https://arxiv.org/abs/{hit[0]}', real_title=hit[1]['title'], real_abstract=hit[1]['abstract'], real_date=month_year(hit[1]['published']))
                results[r['id']]['problems'].append(f'arXiv has a paper with this title: https://arxiv.org/abs/{hit[0]} (check the authors)')
            else:
                results[r['id']]['problems'].append('no arXiv paper with this title: look for it at its venue (or keep it, marked Unverified)')
    return [results[r['id']] for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--since', help='papers whose date_found is on or after this date (YYYY-MM-DD)')
    g.add_argument('--ids', help='comma-separated paper ids')
    g.add_argument('--all', action='store_true', help='every paper')
    ap.add_argument('--csv', help='also write the results to this CSV file')
    ap.add_argument('--db', default=os.path.join(DIR, 'papers_database.csv'))
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.db, newline='', encoding='utf-8')))
    if a.since:
        rows = [r for r in rows if (r.get('date_found') or '') >= a.since]
    elif a.ids:
        want = [x.strip() for x in a.ids.split(',') if x.strip()]
        missing = [x for x in want if x not in {r['id'] for r in rows}]
        if missing:
            sys.exit('not in the database: ' + ', '.join(missing))
        rows = [r for r in rows if r['id'] in want]
    if not rows:
        print('No papers to check.'); return 0
    print(f'Checking {len(rows)} paper{"s" if len(rows) != 1 else ""}…', file=sys.stderr)
    out = verify(rows, suggest=True)
    byid = {r['id']: r for r in rows}
    if a.csv:
        with open(a.csv, 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['id', 'result', 'problems', 'title', 'real_title', 'url', 'suggested_link', 'real_date', 'stored_abstract', 'real_abstract', 'starred', 'read', 'in_lists', 'date_found', 'source_mode'])
            for x in out:
                r = byid[x['id']]
                w.writerow([x['id'], x['kind'], ' | '.join(x['problems']), r['title'], x['real_title'], r['url'], x['suggested_link'], x['real_date'], r.get('abstract', ''), x['real_abstract'],
                            r.get('is_starred', ''), r.get('is_read', ''), r.get('user_lists', ''), r.get('date_found', ''), r.get('source_mode', '')])
    bad = [x for x in out if x['kind'] != 'ok']
    for x in bad:
        print(f"\n{'FAIL' if x['kind'] == 'fail' else 'CHECK'} {x['id']}")
        for p in x['problems']:
            print('   - ' + p)
        if not a.all and x['real_title']:
            print(f"   real title:    {x['real_title']}\n   published:     {x['real_date']}\n   real abstract: {x['real_abstract']}")
    fails, checks = sum(x['kind'] == 'fail' for x in bad), sum(x['kind'] == 'check' for x in bad)
    print(f"\n{len(out) - len(bad)} verified, {fails} to fix, {checks} to check by hand" + (f'. Details in {a.csv}' if a.csv else ''))
    return 1 if fails else 2 if checks else 0


if __name__ == '__main__':
    sys.exit(main())

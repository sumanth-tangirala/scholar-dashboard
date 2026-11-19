#!/usr/bin/env python3
"""Scholar Dashboard as an MCP server, so Claude Code and Claude Desktop on this Mac can add papers to your library.

    add_paper        a link, DOI, arXiv id or title -> the bridge's add queue (the same as the dashboard's Add paper box:
                     Claude Code researches it read-only, bridge/add_paper.py checks and publishes it); can put it in lists
    get_add_status   how a paper asked for is getting on, or the recent ones
    search_library   your published papers (the public papers_database.csv), to see what's already there
    list_lists       your lists and their papers
    add_to_list,     papers already in the library into a list (a new name makes it) or out of one
    remove_from_list
    star_papers      star papers in the library, or unstar them

Local only, over stdio: the app starts it as `python3 bridge/scholar_mcp.py` (bridge/mcp_setup.sh registers it).
The app keeps it running for as long as the app is open, so when this file changes the server starts itself again
(between calls) and tells the app its tools may have changed: a fix reaches Claude without restarting the app.
Nothing listens on the network. It talks to the bridge on 127.0.0.1 with the bridge's pairing token
(~/.scholar-bridge/token); with no token yet, it asks for one through the usual Allow dialog on this Mac.
Never reads or sends your notes or the papers you uploaded for review: only the public database.
"""
import csv, io, json, os, re, sys, threading, time, urllib.error, urllib.request

PORT = int(os.environ.get('SCHOLAR_BRIDGE_PORT', '7823'))
BRIDGE = f'http://127.0.0.1:{PORT}'
HOME = os.path.expanduser(os.environ.get('SCHOLAR_BRIDGE_HOME', '~/.scholar-bridge'))
REPO_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = 'https://www.sumanthtangirala.com/scholar-dashboard/'
CSV_URL = 'https://raw.githubusercontent.com/sumanth-tangirala/scholar-dashboard/main/papers_database.csv'
WAIT = float(os.environ.get('SCHOLAR_MCP_WAIT', '40'))   # how long a call waits for its paper before answering "still working"
VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05']
STOP = set('a an the for of with via using in on to and or from by is are be can will has have not but as at it its this that '
           'paper papers about my me any some'.split())

_out = threading.Lock()
_calls = {'n': 0, 'lock': threading.Lock()}   # tool calls being answered (each on its own thread)
SELF = os.path.abspath(__file__)
STARTED = os.path.getmtime(SELF)


def log(*a): print('[scholar-mcp]', *a, file=sys.stderr, flush=True)


# ---------- the bridge ----------
class BridgeError(Exception): pass


def bridge_token():
    try:
        with open(os.path.join(HOME, 'token')) as f: return f.read().strip()
    except OSError: return ''


def bridge(path, body=None, token=None, timeout=15):
    req = urllib.request.Request(BRIDGE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Origin': 'http://localhost', 'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + (token if token is not None else bridge_token())})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b'{}')
        except Exception: return e.code, {}
    except (urllib.error.URLError, OSError):
        raise BridgeError('The bridge on this Mac isn\'t running, so papers can\'t be added. Start it with: bash bridge/install_bridge.sh '
                          '(in the Scholar Dashboard folder).')


def bridge_call(path, body=None, timeout=15):
    """A call that needs the pairing token; with none yet, the Allow dialog on this Mac gives one first."""
    status, out = bridge(path, body, timeout=timeout)
    if status != 401: return status, out
    log('not paired: asking for permission on this Mac')
    status, pair = bridge('/pair', {'code': '', 'who': 'mcp'}, token='', timeout=80)
    if status != 200 or not pair.get('token'):
        raise BridgeError('This needs your permission on the Mac, which wasn\'t given (the Allow dialog was declined or timed out). Ask again to show the dialog again.')
    return bridge(path, body, timeout=timeout)


# ---------- the library: the public database, plus papers just added (GitHub's copy can lag a few minutes) ----------
_lib = {'at': 0, 'rows': []}


def library():
    if time.time() - _lib['at'] < 180 and _lib['rows']: return _lib['rows']
    text = None
    try:
        req = urllib.request.Request(CSV_URL + '?t=' + str(int(time.time())), headers={'User-Agent': 'ScholarDashboard-MCP/1'})
        with urllib.request.urlopen(req, timeout=15) as r: text = r.read().decode('utf-8')
    except Exception as e:
        log('library from GitHub failed:', e)
        for path in (os.path.join(HOME, 'repo', 'papers_database.csv'), os.path.join(REPO_SRC, 'papers_database.csv')):
            try:
                with open(path, encoding='utf-8') as f: text = f.read(); break
            except OSError: pass
    rows = [r for r in csv.DictReader(io.StringIO(text or '')) if (r.get('title') or '').strip()] if text else []
    if rows: _lib.update(at=time.time(), rows=rows)
    return rows or _lib['rows']


def recent_lists():
    """Lists and stars changed through the bridge lately, oldest first: [{rows: [{id, field, before, after}]}]."""
    try:
        status, out = bridge('/papers/changes', timeout=5)
        if status == 404: status, out = bridge('/lists/changes', timeout=5)   # a bridge from before stars
        return list(reversed(out.get('changes') or [])) if status == 200 else []
    except BridgeError: return []


def library_now():
    """The library as it is now: GitHub's copy, plus papers and list changes the bridge made that it may not show yet."""
    rows = [dict(r) for r in library()]
    known = {r['id'] for r in rows}
    rows += [dict(r) for r in recent_added() if r.get('id') not in known]
    by_id = {r['id']: r for r in rows}
    for c in recent_lists():
        for x in c.get('rows') or []:
            r, f = by_id.get(x.get('id')), x.get('field', 'user_lists')
            if r is not None and f in ('user_lists', 'is_starred') and (r.get(f) or ('false' if f == 'is_starred' else '')) == x.get('before', ''): r[f] = x.get('after', '')
    return rows


def lists_of(r): return [n for n in (r.get('user_lists') or '').split('|') if n]


def recent_added():
    try:
        status, out = bridge('/paper/adds', timeout=5)
        if status != 200: return []
        return [r for j in out.get('jobs') or [] if j.get('state') == 'done' for r in j.get('added') or []]
    except BridgeError: return []


def link(pid): return DASHBOARD + '#paper/' + urllib.request.quote(pid)


def short_authors(a):
    names = [x.strip() for x in (a or '').split(',') if x.strip()]
    return names[0] + (' et al.' if len(names) > 2 else (' and ' + names[1] if len(names) == 2 else '')) if names else ''


def describe(r, i=None):
    flags = [f for f, k in (('starred', 'is_starred'), ('read', 'is_read')) if r.get(k) == 'true']
    where = ', '.join(x for x in ((r.get('venue') or '').strip(), (r.get('publishing_date') or '').strip()) if x)
    head = f'{i}. ' if i is not None else ''
    out = [f"{head}{r.get('title', '').strip()} ({short_authors(r.get('authors'))}{'; ' + where if where else ''})" + (f" [{', '.join(flags)}]" if flags else '')]
    if r.get('headline'): out.append('   Takeaway: ' + r['headline'].strip())
    topics = ' | '.join(x for x in (r.get('theme_groups') or '').split('|') if x)
    if topics: out.append('   Topics: ' + topics)
    if lists_of(r): out.append('   Lists: ' + ', '.join(lists_of(r)))
    out.append(f"   Id: {r['id']}  ·  In the library: {link(r['id'])}" + (f"  ·  Paper: {r['url']}" if r.get('url') else ''))
    return '\n'.join(out)


def search(query, limit=8):
    rows = [r for r in library_now() if (r.get('is_hidden') or '').lower() != 'true']
    q = query.lower()
    ids = set(re.findall(r'\d{4}\.\d{4,5}', q)) | set(re.findall(r'10\.\d{4,9}/\S+', q))
    words = re.findall(r'"([^"]+)"', q) + [w for w in re.findall(r'[\w-]+', re.sub(r'"[^"]*"', ' ', q)) if len(w) > 1 and w not in STOP]
    fields = (('title', 4), ('authors', 3), ('headline', 2), ('matched_interests', 2), ('theme_groups', 2), ('venue', 1),
              ('publishing_date', 1), ('summary', 1), ('abstract', 1))
    scored = []
    for r in rows:
        if ids and any(x in (r.get('url', '') + ' ' + r.get('pdf_url', '')).lower() for x in ids):
            scored.append((99, 99, r.get('date_found', ''), r)); continue
        hit, score = 0, 0
        for w in words:
            best = max((wt for f, wt in fields if w in (r.get(f) or '').lower()), default=0)
            if best: hit += 1; score += best
        if hit: scored.append((hit, score, r.get('date_found', ''), r))
    scored.sort(key=lambda x: x[2], reverse=True)   # ties: the most recently added first
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [x[3] for x in scored[:limit]], len(rows)


# ---------- the tools ----------
TOOLS = [
    {'name': 'add_paper', 'title': 'Add a paper to my library',
     'description': ('Add a paper to the user\'s Scholar Dashboard library (their published paper database). Give a link (arXiv, DOI, '
                     'OpenReview, a conference or publisher page, a PDF), an arXiv id, a DOI, or the title (with an author or the year '
                     'if the title alone is ambiguous). Claude Code on the user\'s Mac finds the paper, reads its abstract, matches it '
                     'to their research interests and publishes it: about a minute each, one paper at a time. A paper already in the '
                     'library is reported, not added twice. A paper with no public version (a submission under review) is not added. '
                     'Waits up to about 40 seconds; if the paper isn\'t in by then, it returns a job id for get_add_status. '
                     'Call it once per paper. star and lists apply to a paper already in the library too.'),
     'inputSchema': {'type': 'object', 'properties': {
         'paper': {'type': 'string', 'description': 'A link, DOI, arXiv id, or the paper\'s title (add an author or year if ambiguous)'},
         'star': {'type': 'boolean', 'description': 'Also star the paper (default false; it never unstars one)'},
         'lists': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Also put the paper in these lists (a new name makes a new list); if it is already in the library, it still goes into them'}},
         'required': ['paper'], 'additionalProperties': False},
     'annotations': {'title': 'Add a paper', 'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'get_add_status', 'title': 'Check papers being added',
     'description': ('How a paper asked for with add_paper is getting on: give its job_id to wait (up to about 40 seconds) for its '
                     'result. Without a job_id, lists the papers asked for lately (from anywhere: the dashboard, the browser '
                     'extension, here) and where each one stands.'),
     'inputSchema': {'type': 'object', 'properties': {'job_id': {'type': 'string', 'description': 'The job id add_paper returned'}},
                     'additionalProperties': False},
     'annotations': {'title': 'Check papers being added', 'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'search_library', 'title': 'Search my library',
     'description': ('Search the papers in the user\'s Scholar Dashboard library by keywords (title words, an author\'s surname, a '
                     'topic, a venue or year), an arXiv id or a DOI. Use it to see whether a paper is already there, or to find '
                     'papers on a subject. Returns the best matches with their takeaway, topics and links.'),
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': 'Keywords, e.g. "conformal prediction STL" or "ames backup cbf 2026"; quote a phrase to keep it together'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'description': 'How many papers to return (default 8)'}},
         'required': ['query'], 'additionalProperties': False},
     'annotations': {'title': 'Search my library', 'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'list_lists', 'title': 'My lists',
     'description': ('The user\'s lists in their Scholar Dashboard library (reading lists and the like) with how many papers each '
                     'holds, or the papers in one list. Use it to find a list\'s exact name before adding to it.'),
     'inputSchema': {'type': 'object', 'properties': {'list': {'type': 'string', 'description': 'A list\'s name, to see its papers (optional)'}},
                     'additionalProperties': False},
     'annotations': {'title': 'My lists', 'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'add_to_list', 'title': 'Add papers to a list',
     'description': ('Put papers that are already in the user\'s library into one of their lists; a name that isn\'t a list yet '
                     'makes a new list (names match whatever the capitals). Name each paper by its library id (from search_library '
                     'or add_paper), its link, arXiv id, DOI or exact title. For a paper not in the library yet, use add_paper with '
                     'lists instead: it adds the paper and puts it in the list. Takes a few seconds; the dashboard shows it at once.'),
     'inputSchema': {'type': 'object', 'properties': {
         'papers': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 50, 'description': 'Library ids (best), links, arXiv ids, DOIs or exact titles'},
         'list': {'type': 'string', 'description': 'The list\'s name; a new name makes a new list'}},
         'required': ['papers', 'list'], 'additionalProperties': False},
     'annotations': {'title': 'Add papers to a list', 'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'remove_from_list', 'title': 'Take papers out of a list',
     'description': ('Take papers out of one of the user\'s lists (they stay in the library). Name each paper by its library id, '
                     'link, arXiv id, DOI or exact title.'),
     'inputSchema': {'type': 'object', 'properties': {
         'papers': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 50, 'description': 'Library ids (best), links, arXiv ids, DOIs or exact titles'},
         'list': {'type': 'string', 'description': 'The list\'s name'}},
         'required': ['papers', 'list'], 'additionalProperties': False},
     'annotations': {'title': 'Take papers out of a list', 'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'star_papers', 'title': 'Star papers',
     'description': ('Star papers that are already in the user\'s library (starred papers are their shortlist in the dashboard), '
                     'or unstar them with unstar: true. Name each paper by its library id (from search_library or add_paper), '
                     'link, arXiv id, DOI or exact title. For a paper not in the library yet, use add_paper with star: true. '
                     'list_lists with list "Starred" shows the starred papers.'),
     'inputSchema': {'type': 'object', 'properties': {
         'papers': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 50, 'description': 'Library ids (best), links, arXiv ids, DOIs or exact titles'},
         'unstar': {'type': 'boolean', 'description': 'Unstar them instead (default false)'}},
         'required': ['papers'], 'additionalProperties': False},
     'annotations': {'title': 'Star papers', 'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
]


def quoted(n): return f'\u201c{n}\u201d'


def stars_text(out, star):
    """What star_papers did, in words."""
    done = [c for c in out.get('changed') or [] if c.get('field') == 'is_starred']
    lines = []
    if done: lines.append(f"{'Starred' if star else 'Unstarred'} {len(done)} paper{'s' if len(done) != 1 else ''}:\n" + '\n'.join(f"  - {c['title']}" for c in done))
    if out.get('unchanged'): lines.append(('Already starred: ' if star else 'Not starred anyway: ') + '; '.join(r['title'] for r in out['unchanged']))
    if out.get('missing'): lines.append('Not in the library: ' + '; '.join(out['missing']) + ('. Add it with add_paper and star: true.' if star else '.'))
    lines.append(f'Starred papers in the dashboard: {DASHBOARD}#library')
    return '\n'.join(lines)


def lists_text(out, verb):
    """What add_to_list / remove_from_list did, in words."""
    names, counts = out.get('lists') or [], out.get('counts') or {}
    name = names[0] if names else ''
    n = counts.get(name, 0)
    lines = []
    out = {**out, 'changed': [c for c in out.get('changed') or [] if c.get('field', 'user_lists') == 'user_lists']}
    if out.get('changed'):
        k = len(out['changed'])
        new = ' (a new list)' if name in (out.get('created') or []) else ''
        lines.append(f"{'Put' if verb == 'add' else 'Took'} {k} paper{'s' if k != 1 else ''} {'in' if verb == 'add' else 'out of'} {quoted(name)}{new}; "
                     f"it now has {n} paper{'s' if n != 1 else ''}{'' if n else ' (an empty list stops showing once the dashboard reloads, unless you made it there)'}:")
        lines += [f"  - {r['title']}" for r in out['changed']]
    if out.get('unchanged'):
        lines.append(('Already in it: ' if verb == 'add' else 'Not in it anyway: ') + '; '.join(r['title'] for r in out['unchanged']))
    if out.get('missing'):
        lines.append('Not in the library: ' + '; '.join(out['missing']) + ('. Add it with add_paper, which can put it in the list too.' if verb == 'add' else '.'))
    lines.append(f'The list in the dashboard: {DASHBOARD}#library')
    return '\n'.join(lines)


def job_text(j):
    st = j.get('state')
    what = j.get('title') or j.get('input', '')[:200]
    if st == 'done':
        lines = []
        for r in j.get('added') or []:
            lines.append(f"Added to the library: {r.get('title', '').strip()} ({short_authors(r.get('authors'))}"
                         f"{'; ' + r['venue'] if r.get('venue') else ''}{', ' + r['publishing_date'] if r.get('publishing_date') else ''})"
                         f"{' and starred' if r.get('is_starred') == 'true' else ''}.\n  Takeaway: {r.get('headline', '').strip()}\n  Open: {link(r['id'])}")
        titles = j.get('titles') or {}
        ch = (j.get('listed') or {}).get('changed') or []
        moved = {r['id'] for r in ch if r.get('field', 'user_lists') == 'user_lists'}
        starred = {r['id'] for r in ch if r.get('field') == 'is_starred'}
        for pid in j.get('existing') or []:
            lines.append(f"Already in the library: {titles.get(pid) or pid}" + (f", now in {', '.join(quoted(n) for n in (j.get('listed') or {}).get('lists') or j.get('lists') or [])}" if pid in moved else '')
                         + (', now starred' if pid in starred else '') + f".\n  Open: {link(pid)}")
        names = (j.get('listed') or {}).get('lists') or j.get('lists') or []   # as the file spells them
        if names and j.get('added'): lines.append('Put in ' + ', '.join(quoted(n) for n in names) + '.')
        return '\n'.join(lines) or 'Nothing was added.'
    if st in ('stopped', 'error'):
        return f"Not added ({what}): {j.get('message') or 'something went wrong'}"
    if st == 'queued':
        n = j.get('ahead', 0)
        return (f"Waiting its turn: {n} paper{'s' if n != 1 else ''} ahead of it ({what}). Each takes about a minute.\n"
                f"Job id: {j['id']} (call get_add_status with it for the result).")
    return (f"Still being added ({j.get('stage') or 'working'}): {what}. It usually takes about a minute.\n"
            f"Job id: {j['id']} (call get_add_status with it for the result).")


def wait_for(jid, seconds):
    end = time.time() + seconds
    while True:
        status, out = bridge_call('/paper/add?id=' + urllib.request.quote(jid))
        if status == 404: raise BridgeError(f'No paper is being added with job id {jid} (the bridge forgets finished ones after a while, or it restarted).')
        j = out.get('job') or {}
        if j.get('state') not in ('queued', 'running') or time.time() >= end: return j
        time.sleep(2)


def call_tool(name, args):
    if name == 'add_paper':
        paper = str(args.get('paper') or '').strip()
        if not paper: return 'Give a link, DOI, arXiv id or title.', True
        lists = [str(x) for x in args.get('lists') or [] if str(x).strip()] if isinstance(args.get('lists'), list) else []
        status, out = bridge_call('/paper/add', {'input': paper[:2000], 'star': bool(args.get('star')), 'lists': lists, 'from': 'mcp'})
        if status == 409: raise BridgeError('The bridge on this Mac is an older version that adds one paper at a time: restart it with bash bridge/install_bridge.sh.')
        if status != 200 or not out.get('job'): return out.get('error') or f'The bridge refused it ({status}).', True
        if lists and 'lists' not in out['job']: raise BridgeError('The bridge on this Mac is an older version that can\'t put papers in lists: restart it with bash bridge/install_bridge.sh.')
        _lib['at'] = 0
        j = wait_for(out['job']['id'], WAIT)
        return job_text(j), j.get('state') == 'error'
    if name == 'get_add_status':
        jid = str(args.get('job_id') or '').strip()
        if jid: j = wait_for(jid, WAIT); return job_text(j), False
        status, out = bridge_call('/paper/adds')
        jobs = (out.get('jobs') or [])[:10]
        if not jobs: return 'No papers have been asked for since the bridge started.', False
        return '\n\n'.join(job_text(j) for j in jobs), False
    if name == 'search_library':
        q = str(args.get('query') or '').strip()
        if not q: return 'Give some keywords.', True
        try: limit = max(1, min(20, int(args.get('limit') or 8)))
        except (TypeError, ValueError): limit = 8
        hits, n = search(q, limit)
        if not n: return 'The library couldn\'t be loaded (no connection to GitHub?).', True
        if not hits: return f'No paper in the library ({n} papers) matches "{q}". Try other keywords.', False
        return f'{len(hits)} best match{"es" if len(hits) != 1 else ""} in the library ({n} papers):\n\n' + '\n\n'.join(describe(r, i + 1) for i, r in enumerate(hits)), False
    if name == 'list_lists':
        rows = [r for r in library_now() if (r.get('is_hidden') or '').lower() != 'true']
        if not rows: return 'The library couldn\'t be loaded (no connection to GitHub?).', True
        counts = {}
        for r in rows:
            for n in lists_of(r): counts[n] = counts.get(n, 0) + 1
        starred = [r for r in rows if r.get('is_starred') == 'true']
        want = str(args.get('list') or '').strip()
        if want.lower() in ('starred', 'stars', 'starred papers') and not any(n.lower() == want.lower() for n in counts):
            if not starred: return 'No paper is starred.', False
            return f'{len(starred)} starred paper{"s" if len(starred) != 1 else ""}:\n\n' + '\n\n'.join(describe(r, i + 1) for i, r in enumerate(starred)), False
        if want:
            name = next((n for n in counts if n.lower() == want.lower()), None)
            if not name: return f'No list is called {quoted(want)}. The lists: ' + (', '.join(f'{quoted(n)} ({c})' for n, c in sorted(counts.items())) or 'none yet') + '.', False
            papers = [r for r in rows if name in lists_of(r)]
            return f'{quoted(name)} has {len(papers)} paper{"s" if len(papers) != 1 else ""}:\n\n' + '\n\n'.join(describe(r, i + 1) for i, r in enumerate(papers)), False
        star_line = f'\nStarred (not a list, the shortlist): {len(starred)} paper{"s" if len(starred) != 1 else ""}; list "Starred" shows them.'
        if not counts: return 'There are no lists yet. add_to_list makes one.' + star_line, False
        return 'The lists (papers in each):\n' + '\n'.join(f'- {quoted(n)}: {c}' for n, c in sorted(counts.items(), key=lambda x: x[0].lower())) + star_line, False
    if name in ('add_to_list', 'remove_from_list'):
        papers = [str(x) for x in args.get('papers') or [] if str(x).strip()] if isinstance(args.get('papers'), list) else []
        lst = str(args.get('list') or '').strip()
        if not papers or not lst: return 'Name the papers and the list.', True
        verb = 'add' if name == 'add_to_list' else 'remove'
        status, out = bridge_call('/lists', {'papers': papers[:50], verb: [lst], 'from': 'mcp'}, timeout=120)
        if status == 404: raise BridgeError('The bridge on this Mac is an older version without lists: restart it with bash bridge/install_bridge.sh.')
        if status != 200: return out.get('error') or f'The bridge refused it ({status}).', True
        _lib['at'] = 0
        return lists_text(out, verb), False
    if name == 'star_papers':
        papers = [str(x) for x in args.get('papers') or [] if str(x).strip()] if isinstance(args.get('papers'), list) else []
        if not papers: return 'Name the papers.', True
        star = not args.get('unstar')
        status, out = bridge_call('/papers/edit', {'papers': papers[:50], 'star': star, 'from': 'mcp'}, timeout=120)
        if status == 404: raise BridgeError('The bridge on this Mac is an older version without stars: restart it with bash bridge/install_bridge.sh.')
        if status != 200: return out.get('error') or f'The bridge refused it ({status}).', True
        _lib['at'] = 0
        return stars_text(out, star), False
    raise KeyError(name)


# ---------- JSON-RPC over stdio ----------
def send(msg):
    with _out:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n'); sys.stdout.flush()


def handle(m):
    mid, method, params = m.get('id'), m.get('method'), m.get('params') or {}
    if mid is None: return   # a notification (initialized, cancelled, ...): nothing to answer
    try:
        if method == 'initialize':
            v = params.get('protocolVersion')
            result = {'protocolVersion': v if v in VERSIONS else VERSIONS[0], 'capabilities': {'tools': {'listChanged': True}},
                      'serverInfo': {'name': 'scholar-dashboard', 'title': 'Scholar Dashboard', 'version': '1.0.0'},
                      'instructions': ('Adds papers to the user\'s Scholar Dashboard library, keeps their lists and stars, and searches it. When '
                                       'the user asks to add, save or keep a paper ("add this to my library"), call add_paper once '
                                       'per paper, with lists if they name a list. To put papers already in the library into a list '
                                       '(a new name makes the list), use add_to_list with their ids from search_library. Adding '
                                       'takes about a minute per paper; tell the user when it\'s in, with its link.')}
        elif method == 'ping': result = {}
        elif method == 'tools/list': result = {'tools': TOOLS}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            if name not in {t['name'] for t in TOOLS}:
                return send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': f'Unknown tool: {name}'}})
            log('call', name)
            try: text, err = call_tool(name, args)
            except BridgeError as e: text, err = str(e), True
            except Exception as e: text, err = f'Failed: {e}', True
            result = {'content': [{'type': 'text', 'text': text}], 'isError': bool(err)}
        elif method in ('resources/list', 'resources/templates/list', 'prompts/list'):
            result = {method.split('/')[0] if method != 'resources/templates/list' else 'resourceTemplates': []}
        else:
            return send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'Method not found: {method}'}})
        send({'jsonrpc': '2.0', 'id': mid, 'result': result})
    except Exception as e:
        send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32603, 'message': str(e) or 'internal error'}})


def call_thread(m):
    try: handle(m)
    finally:
        with _calls['lock']: _calls['n'] -= 1


def changed():
    try: return os.path.getmtime(SELF) != STARTED
    except OSError: return False


def reload(line):
    """This file changed: start it again in this same process (the app's pipes stay as they are), handing the new one
    the message just read. Only between calls, so no answer is lost."""
    try:
        with open(SELF, encoding='utf-8') as f: compile(f.read(), SELF, 'exec')
    except Exception as e:   # caught mid-write, or broken: keep running as is, and try again with the next message
        log('this file changed but doesn\'t compile yet:', e); return
    log('this file changed: starting again')
    env = dict(os.environ, SCHOLAR_MCP_PENDING=line.decode('utf-8', 'replace'), SCHOLAR_MCP_RELOADED='1')
    sys.stdout.flush(); sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, SELF, *sys.argv[1:]], env)


def lines():
    """Messages, one per line. Read a byte at a time (no read-ahead), so nothing waiting in a buffer is lost when the
    server starts itself again."""
    pending = os.environ.pop('SCHOLAR_MCP_PENDING', None)
    if pending is not None: yield pending.encode()
    raw = sys.stdin.buffer.raw
    while True:
        line = raw.readline()
        if not line: return
        yield line


def main():
    log('started; bridge', BRIDGE)
    if os.environ.pop('SCHOLAR_MCP_RELOADED', None):   # the tools may have changed: the app asks for them again
        send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
    for raw_line in lines():
        if changed() and not _calls['n']: reload(raw_line)
        line = raw_line.decode('utf-8', 'replace').strip()
        if not line: continue
        try: msg = json.loads(line)
        except Exception:
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}); continue
        for m in (msg if isinstance(msg, list) else [msg]):
            if isinstance(m, dict) and m.get('method'):
                # tool calls wait for papers: each runs on its own, so pings and other calls are answered meanwhile
                if m.get('method') == 'tools/call':
                    with _calls['lock']: _calls['n'] += 1   # counted before it starts, so a restart never cuts it off
                    threading.Thread(target=call_thread, args=(m,), daemon=True).start()
                else: handle(m)


if __name__ == '__main__':
    main()

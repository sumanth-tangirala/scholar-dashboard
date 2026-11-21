#!/usr/bin/env python3
"""Scholar Dashboard <-> Claude Code bridge.

A small local server that lets the dashboard (the GitHub Pages site, or scholar.localhost) chat with Claude
through Claude Code on this laptop: one Claude Code session per paper, streamed back to the page.
Or, once bridge/api_setup.sh has stored an API key, through the Claude API (the chat panel's Code / API switch):
the bridge then keeps each conversation itself and gives Claude the same abilities as tools (web search and
fetch, looking at the PDF's pages, searching your library). A conversation can change engine midway: the
other one is shown the turns it missed.

Safety:
  * listens on 127.0.0.1 only, and answers only the dashboard's own origins;
  * every request except /health and /pair needs a pairing token, issued only after you click Allow in a
    macOS dialog on this laptop;
  * Claude runs with no shell, no connectors or plugins, and may read only files in this bridge's work folder
    (the paper PDFs and figures the dashboard sends); it may search and fetch from the web.
  * Tagging existing papers with a topic you approved (/topic/tag): the same, with add_topic.py.
  * Adding a paper (the dashboard's Add paper box, the browser extension, the MCP server: /paper/add): Claude only
    researches it, read-only, in the bridge's own copy of the repository (~/.scholar-bridge/repo); add_paper.py checks
    what it returns and commits and pushes just that row (and its link-preview page) from that copy. Your working copy
    is never touched. Papers asked for while one is being added wait their turn, one at a time.
  * The API key stays in ~/.scholar-bridge/config.json (readable by you alone); the page never sees it.
  * A copy of each uploaded paper's PDF and video (To review) is kept in ~/.scholar-bridge/uploads (/upload): a browser
    can lose its own (it wipes a site's storage when the disk runs low), and another device fetches it from here.

Everything lives in ~/.scholar-bridge:  token, config.json, chats/<paper>.json (what the dashboard shows, the paper's
text, and an API conversation), work/ (Claude Code's working folder: papers/<paper>.pdf, attachments/), pylib/ (the
Anthropic SDK). The Claude Code sessions themselves are ordinary ones: `cd ~/.scholar-bridge/work && claude --resume`.
"""
import base64, json, os, re, secrets, shutil, socket, subprocess, sys, threading, time, urllib.request, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_paper, add_topic
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('SCHOLAR_BRIDGE_PORT', '7823'))
ORIGINS = {'https://www.sumanthtangirala.com', 'https://sumanthtangirala.com', 'http://scholar.localhost', 'http://localhost'}
EXTENSION = 'chrome-extension://mhfedfofciaejamdeikbedlfapmpfcpm'   # the browser extension (extension/: its manifest's key fixes this id)
ORIGINS |= {EXTENSION}
ORIGINS |= set(filter(None, os.environ.get('SCHOLAR_BRIDGE_ORIGINS', '').split(',')))   # a test copy of the site
HOME = os.path.expanduser(os.environ.get('SCHOLAR_BRIDGE_HOME', '~/.scholar-bridge'))   # override: a separate test copy
WORK = os.path.join(HOME, 'work')
CHATS = os.path.join(HOME, 'chats')
TOKEN_FILE = os.path.join(HOME, 'token')
UPLOADS = os.path.join(HOME, 'uploads')   # uploaded papers' PDFs and videos: <id>.pdf, <id>.video (+ .json: name, type)
CONFIG_FILE = os.path.join(HOME, 'config.json')   # the API key (bridge/api_setup.sh) and which engine Chat uses
PYLIB = os.path.join(HOME, 'pylib')   # the Anthropic SDK and pypdf, installed here by api_setup.sh (not into your Python)
SESSIONS_FILE = os.path.join(HOME, 'sessions.json')
NO_MCP = os.path.join(HOME, 'no-mcp.json')
MODELS = {'opus': 'opus', 'sonnet': 'sonnet', 'haiku': 'haiku'}
API_MODELS = {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5', 'haiku': 'claude-haiku-5-5'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}   # anything else: the default of Claude Code or of the model
VERSION = 9   # 2: /paper/add; 3: /topic/tag; 4: the Claude API engine (/engine); 5: copies of uploaded papers (/upload);
              # 6: papers to add wait in a queue (/paper/add?id=, /paper/adds), for the extension and the MCP server;
              # 7: lists (/lists: papers into or out of a list; /lists/changes; /paper/add takes lists);
              # 8: stars too (/papers/edit, the same as /lists with star; /papers/changes has both)
              # 9: side chats (/chat/side: a quick question about words in an answer, to Haiku, in its own conversation)
UPLOAD_KINDS = {'pdf': 'application/pdf', 'video': 'video/mp4'}
UPLOAD_MAX = 4 << 30   # 4 GB

for d in (HOME, WORK, CHATS, os.path.join(WORK, 'papers'), os.path.join(WORK, 'attachments')):
    os.makedirs(d, exist_ok=True)
os.makedirs(UPLOADS, mode=0o700, exist_ok=True)
if not os.path.exists(NO_MCP):
    with open(NO_MCP, 'w') as f: json.dump({'mcpServers': {}}, f)

_lock = threading.Lock()
_running = {}   # paper id -> subprocess.Popen (side chats: 'side:' + paper id)
_side_tid = {}   # paper id -> the side chat being answered (one at a time per paper)
_adds = {'jobs': [], 'proc': None, 'wake': threading.Event()}   # papers to add, in the order asked (the finished ones too,
# the last ADDS_KEPT); one is added at a time. The asker follows its job: GET /paper/add?id=<job>, or all of them: /paper/adds
ADDS_KEPT, ADDS_WAITING = 40, 25
_list_log = []   # lists and stars changed here lately, newest last: the dashboard applies the ones its copy of the database lacks


def log_lists(listed, source, added=()):
    """Remember a change to lists and stars ({changed: [{id, title, field, before, after}], created} from add_paper;
    papers just added into lists count as going from no list to theirs)."""
    rows = list((listed or {}).get('changed') or []) + [{'id': r['id'], 'title': r.get('title', ''), 'field': 'user_lists', 'before': '', 'after': r['user_lists']} for r in added if r.get('user_lists')]
    if not rows: return
    sets = lambda v: {n for n in v.split('|') if n}
    _list_log.append({'id': uuid.uuid4().hex[:12], 'at': time.time(), 'from': source, 'rows': rows,
                      'lists': sorted({n for r in rows if r.get('field', 'user_lists') == 'user_lists' for n in sets(r['before']) ^ sets(r['after'])}),   # the lists that changed
                      'created': (listed or {}).get('created') or []})
    del _list_log[:-40]


def changes_view(fields=None):
    """The changes, newest first; only those fields' rows (a dashboard from before stars reads every row as lists)."""
    out = []
    for c in reversed(_list_log):
        rows = [r for r in c['rows'] if not fields or r.get('field', 'user_lists') in fields]
        if rows: out.append({**c, 'rows': rows})
    return out
_topic = {'job': None, 'proc': None}   # the topic whose existing papers are being tagged; GET /topic/tag


def start_topic(name):
    job = {'id': uuid.uuid4().hex[:12], 'state': 'running', 'stage': 'Starting…', 'topic': name[:200], 'started': time.time()}
    _topic['job'] = job

    def stage(s): job['stage'] = s
    def run():
        try:
            out = add_topic.tag(name, cb=stage, on_proc=lambda p: _topic.__setitem__('proc', p))
            job.update(state='done', tagged=out['tagged'], group=out['group'])
        except add_paper.Stop as e:
            job.update(state='stopped', message=str(e))
        except Exception as e:
            job.update(state='error', message=(str(e) or 'failed')[-400:])
        finally:
            _topic['proc'] = None; job['ended'] = time.time()
    threading.Thread(target=run, daemon=True).start()
    return job


def queue_add(text, star, title='', source='', lists=()):
    """A paper to add, after the ones already waiting (call with _lock held). The same input asked for again while it
    waits or runs (a second click) is the same job. Returns the job, or None when too many are waiting."""
    key = re.sub(r'\s+', ' ', text).strip().lower()
    for j in _adds['jobs']:
        if j['state'] in ('queued', 'running') and j['key'] == key:
            if star: j['star'] = True
            if lists and j['state'] == 'queued': j['lists'] = add_paper.clean_list_names(list(j.get('lists') or []) + list(lists))
            return j
    if sum(j['state'] == 'queued' for j in _adds['jobs']) >= ADDS_WAITING: return None
    job = {'id': uuid.uuid4().hex[:12], 'state': 'queued', 'stage': 'Waiting…', 'input': text, 'title': title[:300],
           'star': bool(star), 'lists': add_paper.clean_list_names(lists), 'from': source[:20], 'key': key, 'queued': time.time()}
    _adds['jobs'].append(job)
    done = [j for j in _adds['jobs'] if j['state'] not in ('queued', 'running')]
    for j in done[:max(0, len(done) - ADDS_KEPT)]: _adds['jobs'].remove(j)
    _adds['wake'].set()
    return job


def add_view(j):
    """A job as the asker sees it, with how many papers are ahead of it while it waits."""
    v = {k: x for k, x in dict(j).items() if k != 'key'}
    if j['state'] == 'queued':
        v['ahead'] = sum(1 for o in _adds['jobs'] if o['state'] in ('queued', 'running') and o['queued'] < j['queued'])
    return v


def add_job(jid): return next((j for j in _adds['jobs'] if j['id'] == jid), None)


def add_worker():
    """Adds the waiting papers one at a time, oldest first (add_paper resets the shared copy of the repository)."""
    while True:
        with _lock:
            job = next((j for j in _adds['jobs'] if j['state'] == 'queued'), None)
            if job: job.update(state='running', stage='Starting…', started=time.time())
        if not job:
            _adds['wake'].wait(); _adds['wake'].clear(); continue
        def stage(s, job=job): job['stage'] = s
        try:
            out = add_paper.add(job['input'], star=job['star'], lists=job.get('lists'), cb=stage, on_proc=lambda p: _adds.__setitem__('proc', p))
            job.update(state='done', added=out['added'], existing=out['existing'], titles=out.get('titles', {}), listed=out.get('listed'))
            log_lists(out.get('listed'), job['from'], out['added'])
        except add_paper.Stop as e:
            job.update(state='stopped', message=str(e))
        except Exception as e:
            if job.get('stopping'): job.update(state='stopped', message='Stopped.')
            else: job.update(state='error', message=(str(e) or 'failed')[-400:])
        finally:
            _adds['proc'] = None; job['ended'] = time.time()


def claude_bin():
    for p in (shutil.which('claude'), os.path.expanduser('~/.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude'):
        if p and os.path.exists(p): return p
    return 'claude'


def read_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default


def write_json(path, obj, private=False):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    if private: os.fchmod(fd, 0o600)   # it holds the API key: readable by you alone from the first byte
    with os.fdopen(fd, 'w') as f: json.dump(obj, f)
    os.replace(tmp, path)


def config(): return read_json(CONFIG_FILE, {})


def engine():
    """What Chat runs on: the Claude API once a key is set up and chosen, else Claude Code."""
    c = config()
    return 'api' if c.get('api_key') and c.get('engine') == 'api' else 'code'


def token():
    try:
        with open(TOKEN_FILE) as f: return f.read().strip()
    except Exception: return ''


def safe_id(pid):
    return re.sub(r'[^A-Za-z0-9._-]', '_', pid or '')[:120]


def chat_file(pid): return os.path.join(CHATS, safe_id(pid) + '.json')


def load_chat(pid):
    return read_json(chat_file(pid), {'paper': pid, 'session': None, 'model': 'opus', 'messages': []})


# Side chats: every conversation's, in one file per paper, each tied to the answer it's about by that answer's
# timestamp (unique), so they come and go with their conversation (New chat, History) with nothing to move
def side_file(pid): return os.path.join(CHATS, safe_id(pid) + '.side.json')
def load_side(pid): return read_json(side_file(pid), {'threads': {}})


def side_view(pid, msgs):
    """This conversation's side chats, for the page (without the engines' own records)"""
    stamps, now, side = {m.get('ts') for m in msgs if m.get('role') == 'assistant'}, time.time(), load_side(pid)
    gone = [k for k, t in side['threads'].items() if t.get('deleted') and now - t['deleted'] > 86400]   # Undo is long past
    if gone:
        for k in gone: del side['threads'][k]
        write_json(side_file(pid), side)
    return sorted(({'id': k, 'anchor': t['anchor'], 'messages': t['messages'], 'created': t.get('created', 0)}
                   for k, t in side['threads'].items() if t.get('messages') and not t.get('deleted') and t['anchor'].get('ts') in stamps),
                  key=lambda t: t['created'])


def side_intro(c, t):
    """What a side chat is given first: the paper, the main conversation up to the answer asked about, the words"""
    msgs = c.get('messages') or []
    at = next((i for i, m in enumerate(msgs) if m.get('ts') == t['anchor']['ts']), len(msgs) - 1)
    lines = []
    for m in msgs[:at + 1]:
        if m.get('role') == 'user':
            if m.get('auto'): continue
            about = ''.join(f'[about p.{q["page"]} "{q.get("text", "")}"] ' if q.get('page') else f'[about its words "{q.get("text", "")}"] ' for q in m.get('quotes') or [])
            lines.append('User: ' + about + (m.get('text') or ('(a figure)' if m.get('images') else '')))
        else:
            lines.append('Main chat: ' + (m.get('text') or ''))
    convo = '\n\n'.join(lines)
    if len(convo) > 60000: convo = '[earlier turns left out]\n\n' + convo[-60000:]   # its end holds the answer asked about
    parts = ['<paper>\n' + c['paper_text'] + '\n</paper>'] if c.get('paper_text') else []
    return parts + ['<main-chat>\n' + convo + '\n</main-chat>', '<selected>\n' + t['anchor']['text'] + '\n</selected>']


def code_stream(proc, emit):
    """A Claude Code reply as it's written (stream-json): the text so far goes to the page; returns (text, result)"""
    text, result = '', None
    for line in proc.stdout:
        try: e = json.loads(line)
        except Exception: continue
        t = e.get('type')
        if t == 'stream_event':
            ev = e.get('event') or {}
            if ev.get('type') == 'content_block_delta' and (ev.get('delta') or {}).get('type') == 'text_delta':
                chunk = ev['delta'].get('text', ''); text += chunk; emit({'t': 'text', 'v': chunk})
            elif ev.get('type') == 'content_block_start' and (ev.get('content_block') or {}).get('type') in ('tool_use', 'server_tool_use'):
                emit({'t': 'tool', 'v': ev['content_block'].get('name')})
            elif ev.get('type') == 'message_start' and text and not text.endswith('\n\n'):
                text += '\n\n'; emit({'t': 'text', 'v': '\n\n'})   # a new assistant message after a tool call
        elif t == 'result':
            result = e
    proc.wait()
    return text, result


def archive_current(pid):
    """Keep the current conversation as an earlier one (a unique name, so nothing is ever overwritten)."""
    ts = int(time.time())
    while os.path.exists(chat_file(pid)[:-5] + f'.{ts}.json'): ts += 1
    os.replace(chat_file(pid), chat_file(pid)[:-5] + f'.{ts}.json')


def chat_list(pid):
    """This paper's conversations: the current one and earlier ones (kept when you start a new chat)."""
    base, out = safe_id(pid), []
    for f in os.listdir(CHATS):
        if f == base + '.json': key = 'current'
        elif f.startswith(base + '.') and f.endswith('.json') and f[len(base) + 1:-5].isdigit(): key = f[len(base) + 1:-5]
        else: continue
        msgs = read_json(os.path.join(CHATS, f), {}).get('messages') or []
        if not msgs: continue
        first = next((m.get('text') for m in msgs if m.get('role') == 'user' and m.get('text')), '') or 'Figure or passage'
        out.append({'id': key, 'title': first[:140], 'count': len(msgs), 'updated': msgs[-1].get('ts') or 0})
    return sorted(out, key=lambda c: -c['updated'])


def upload_path(pid, kind, meta=False):
    """Where the copy of an uploaded paper's file is kept (None for anything but an uploaded paper's id)."""
    if not re.fullmatch(r'up-[0-9a-f]{12}', pid or '') or kind not in UPLOAD_KINDS: return None
    return os.path.join(UPLOADS, f'{pid}.{kind}' + ('.json' if meta else ''))


def upload_list():
    """What the Mac keeps: {id: {kind: {name, size}}}"""
    out = {}
    for f in os.listdir(UPLOADS):
        m = re.fullmatch(r'(up-[0-9a-f]{12})\.(pdf|video)', f)
        if m:
            meta = read_json(os.path.join(UPLOADS, f + '.json'), {})
            out.setdefault(m.group(1), {})[m.group(2)] = {'name': meta.get('name', ''), 'size': os.path.getsize(os.path.join(UPLOADS, f))}
    return out


def forget_paper(pid):
    """Everything kept for a paper: its chats, the PDF and figure copies, the copies of its uploaded files, and Claude
    Code's own transcripts of them."""
    base = safe_id(pid)
    if not base: return 0
    sessions, gone = set(), 0
    for kind in UPLOAD_KINDS:
        for path in (upload_path(pid, kind), upload_path(pid, kind, meta=True)):
            if path and os.path.exists(path): os.remove(path); gone += 1
    for t in load_side(pid)['threads'].values():
        if re.fullmatch(r'[0-9a-f-]{36}', t.get('session') or ''): sessions.add(t['session'])
    if os.path.exists(side_file(pid)): os.remove(side_file(pid)); gone += 1
    for f in os.listdir(CHATS):
        if f == base + '.json' or (f.startswith(base + '.') and f.endswith('.json') and f[len(base) + 1:-5].isdigit()):
            s = read_json(os.path.join(CHATS, f), {}).get('session')
            if s and re.fullmatch(r'[0-9a-f-]{36}', s): sessions.add(s)
            os.remove(os.path.join(CHATS, f)); gone += 1
    for path in [os.path.join(WORK, 'papers', base + '.pdf')] + [os.path.join(WORK, 'attachments', f) for f in os.listdir(os.path.join(WORK, 'attachments')) if f.startswith(base + '-')]:
        if os.path.exists(path): os.remove(path); gone += 1
    projects = os.path.expanduser('~/.claude/projects')
    for d in (os.listdir(projects) if os.path.isdir(projects) else []):
        for s in sessions:
            for path in (os.path.join(projects, d, s + '.jsonl'), os.path.join(projects, d, s)):
                if os.path.isfile(path): os.remove(path); gone += 1
                elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True); gone += 1
    return gone


WHO = {'extension': 'The Scholar Dashboard extension in your browser wants to add papers through Claude Code on this Mac.',
       'mcp': 'Scholar Dashboard\'s MCP server (for Claude Code and Claude Desktop) wants to add papers through Claude Code on this Mac.'}


def ask_permission(code, who=''):
    """A native dialog on this laptop: the only way to get a token."""
    what = WHO.get(who, 'Scholar Dashboard wants to chat with Claude through Claude Code on this Mac.')
    shown = {'extension': 'Code shown in your browser', 'mcp': ''}.get(who, 'Code shown in the page')
    script = (f'display dialog "{what}' + (f'\\n\\n{shown}: {code}' if shown and code else '') + '" with title "Scholar Dashboard" buttons {"Don\'t Allow", "Allow"} '
              'default button "Allow" cancel button "Don\'t Allow" with icon note giving up after 60')
    try:
        r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=70)
        return r.returncode == 0 and 'Allow' in r.stdout and 'gave up:true' not in r.stdout
    except Exception:
        return False


# What Claude can add to the user's notes on the paper (the app turns these blocks into notes, highlights and comments)
NOTES = """Notes, highlights and comments: you can add them to the user's notes on this paper. Do it when they ask, in any words ("highlight the assumptions", "comment on where the proof is weak", "put this in my notes", "flag what I should check in section 4"), or when what they asked you to do is to mark up the paper. While answering ordinary questions, don't add anything; you may offer to. The app adds them as soon as your answer ends, marked as yours, and the user can undo them. Write each as its own fenced block, exactly like these:
```note
A note, in Markdown (you may cite with [[p.N "..."]]).
```
```highlight p.N color "exact words"```
```comment p.N "exact words"
The comment, in Markdown: a sentence or a few.
```
A comment goes on the user's highlight of those words if they have one, else it underlines them; put a color before its quote (```comment p.N color "exact words"```) to highlight them too. color is one of orange, yellow, green, blue, red, purple; <context> says what each means to this user, so choose by meaning. The quoted words follow the citation rules: verbatim, contiguous, a phrase to a sentence, from that page. Highlight the few passages that matter, not whole paragraphs. Never put a block inside a list or table; outside the blocks, say in a sentence or two what you added. Don't repeat what <context> shows is already there (items marked "added by you" are yours). You can't edit or delete existing notes, highlights or comments; if asked, say so and suggest the change."""
NOTES_V = 1   # bump when NOTES changes: API conversations begun with an older prompt are told once

# A side chat: the user selected words in one of the main chat's answers and asks a cheaper model what they mean
SIDE_MODEL = 'haiku'
SIDE_SYSTEM = """You are a quick helper inside the user's paper-reading app. The user is reading a paper (its text is in <paper>, page-marked) and talking about it with another assistant, the main chat (in <main-chat>). They selected words from one of its answers (in <selected>) and opened this side chat to understand what it meant.

Explain what they ask about those words, in the context of that answer and the paper: plainly and briefly (a few sentences or a short list), defining terms and notation, with a small concrete example when it helps. Focus on what they asked; don't redo the whole answer. If the main chat's answer looks wrong or unclear on this point, say so. You may cite the paper as [[p.N "exact words from the paper"]] (verbatim, contiguous, a phrase to a sentence; the app shows it as a small clickable pill, so each sentence must read fine without it). Use Markdown and $math$ / $$math$$. The panel is narrow: no tables wider than three short columns.{profile}"""

SYSTEM = """You are a research assistant inside the user's paper-reading app. The user is reading one paper; its full text is given in <paper> in the conversation (page-marked){pdf_how}

Each message may start with <context>: the user's current highlights, comments and notes on this paper (they change as they read), their research interests, and sometimes other papers they @-mentioned. Use them; don't repeat them back.

Citing the paper: whenever you point to something in the paper, cite it with its exact words in this form: [[p.N "exact words from the paper"]] (N = page). The quote must be contiguous and verbatim (no ellipses, no paraphrase), a phrase to a sentence long, with symbols as they appear in the text. The app shows each citation only as a small clickable pill (page number and a few words), so the reader cannot read its words: it is a footnote marker, never part of your prose. Every sentence, list item and table cell must say everything on its own, as if the citation were deleted; put the citation right after the claim it supports. Wrong: "Predicates live on [[p.2 "the low-dimensional state space"]]" (reads as "Predicates live on p.2 the low-dime…"). Right: "Predicates are defined over a low-dimensional state space [[p.2 "the low-dimensional state space"]]." A citation never replaces words, and a table cell never consists of only a citation. Tables are where this slips most: wrong "| $\\phi$ | The sensor. It [[p.4 "is the observation function"]]. |", right "| $\\phi$ | The sensor: maps the true state to what the sensors observe [[p.4 "is the observation function"]]. |". Before sending, reread each sentence with its citations removed; if any is incomplete, rewrite it. Use [[p.N "..."]] only for the paper being read (its pills jump within it); cite other papers, including ones the user @-mentions, in plain text by title or author, with a page if useful.

Other papers: {library_how} If more than one could fit, ask them with a short numbered list of titles. Once you know which paper(s) they mean, reply with only one line per paper, ```paper <id>```, and nothing else: the app then sends you that paper's text and the user's own notes on it, and you continue answering their question. Papers they @-mention arrive the same way, in <other-paper>.

{notes}

Be direct and concise; use Markdown and $math$ / $$math$$. The chat is a narrow side panel (often under 400px): keep tables to about three columns of short cells, and put citations in the text after the table rather than inside its cells. Say when something is your inference rather than in the paper.{web}{profile}"""

# How each engine gives Claude the PDF, the user's images and library, and the web
SYSTEM_CODE = {
    'pdf_how': ', and the PDF is at ./papers/{pdf} if you need to look at a figure, table or equation layout (use Read with the pages you need). Images the user attaches are saved under ./attachments/ and named in their message.',
    'library_how': 'the user\'s library is in ./library.tsv (tab-separated, one paper per line: id, title, authors, date, venue, topics, one-line summary, and flags such as starred, read, notes). When they refer to another paper by description ("the Ames backup-CBF paper", "the conformal STL one I read last month"), search that file with Grep (try a few keywords) to identify it.',
    'web': ''}
SYSTEM_API = {
    'pdf_how': '; to look at a figure, table or equation layout, call view_pages with the pages you need. Images the user attaches come with their message.',
    'library_how': 'search_library searches the user\'s library (one paper per line: id, title, authors, date, venue, topics, one-line summary, and flags such as starred, read, notes). When they refer to another paper by description ("the Ames backup-CBF paper", "the conformal STL one I read last month"), search it (try a few keywords) to identify it.',
    'web': ' When you use the web, link the pages you relied on inline as Markdown links.'}

LIBRARY_TOOL = {
    'name': 'search_library',
    'description': 'Search the user\'s paper library: every paper in their dashboard, one per line, tab-separated: id, title, authors, date, venue, topics, one-line summary, and flags (starred, read, lists, has my notes, opened <date>, added <date>, uploaded for review). Use it when the user refers to another paper by description, to find its id. It returns the header and up to 15 lines, those matching the most query words first (case-insensitive substring match on the whole line). Try other keywords (an author surname, title words, a topic, a venue or year) if the first search misses.',
    'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Keywords, e.g. "ames backup cbf" or "conformal stl 2026"'}}, 'required': ['query'], 'additionalProperties': False},
    'strict': True}
PAGES_TOOL = {
    'name': 'view_pages',
    'description': 'Look at pages of the paper being read, as the PDF itself: figures, plots, tables and equation layout that the extracted text in <paper> loses. Pages are numbered as in <paper> ([p.N], from 1). Ask only for the pages you need, at most 5 per call; the pages stay in the conversation, so don\'t ask for the same page twice.',
    'input_schema': {'type': 'object', 'properties': {'pages': {'type': 'string', 'description': 'Pages, e.g. "3", "4-6" or "2, 7"'}}, 'required': ['pages'], 'additionalProperties': False},
    'strict': True}


def api_tools(model):
    basic = model == 'haiku'   # Haiku takes the web tools without dynamic filtering
    return [{'type': 'web_search_20250305' if basic else 'web_search_20260209', 'name': 'web_search', 'max_uses': 5},
            {'type': 'web_fetch_20250910' if basic else 'web_fetch_20260209', 'name': 'web_fetch', 'max_uses': 5, 'max_content_tokens': 40000},
            LIBRARY_TOOL, PAGES_TOOL]


def earlier_turns(msgs):
    """Turns of this conversation that the other engine answered (the user switched Code / API midway)."""
    out = []
    for i, m in enumerate(msgs):
        if m.get('role') == 'user':
            if i + 1 >= len(msgs) or msgs[i + 1].get('role') != 'assistant': continue   # got no answer (failed or declined)
            about = ''.join(f'[about p.{q["page"]} "{q.get("text", "")}"] ' if q.get('page') else f'[about your words "{q.get("text", "")}"] ' for q in m.get('quotes') or [])
            said = '(sent the papers you asked for)' if m.get('auto') else (m.get('text') or ('(a figure)' if m.get('images') else ''))
            out.append('User: ' + about + said)
        else:
            out.append('You: ' + (m.get('text') or ''))
    if not out: return ''
    return ('<earlier>\nEarlier turns of this conversation that ran in another session (the user switched between Claude Code and the Claude API), so you haven\'t seen them; they come before this message:\n\n'
            + '\n\n'.join(out) + '\n</earlier>')


_sdk = {}
def anthropic_sdk():
    """The Anthropic SDK (from ~/.scholar-bridge/pylib) and a client with the stored key, made again if the key changes."""
    key = config().get('api_key', '')
    if _sdk.get('key') != key:
        if PYLIB not in sys.path: sys.path.insert(0, PYLIB)
        import anthropic
        _sdk.update(key=key, mod=anthropic, client=anthropic.Anthropic(api_key=key))
    return _sdk['mod'], _sdk['client']


def api_error(anthropic, e):
    if isinstance(e, anthropic.AuthenticationError): return 'the API key was refused. Set it up again: bash bridge/api_setup.sh'
    if isinstance(e, anthropic.RateLimitError): return 'rate-limited; try again in a minute.'
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500: return f'the API is busy ({e.status_code}); try again shortly.'
    if isinstance(e, anthropic.APIStatusError): return e.message
    if isinstance(e, anthropic.APIConnectionError): return 'couldn\'t reach the API (is this Mac online?).'
    return str(e) or type(e).__name__


def hydrate(msgs):
    """The stored conversation as sent: images and PDF pages are kept as files under work/ and stored as '@<path>'."""
    def fix(b):
        if not isinstance(b, dict): return b
        s = b.get('source')
        if isinstance(s, dict) and str(s.get('data', '')).startswith('@'):
            path = os.path.join(WORK, 'attachments', os.path.basename(s['data'][1:]))
            try:
                with open(path, 'rb') as f: return {**b, 'source': {**s, 'data': base64.b64encode(f.read()).decode()}}
            except OSError: return {'type': 'text', 'text': '(an attachment that is no longer kept)'}
        if b.get('type') == 'tool_result' and isinstance(b.get('content'), list):
            return {**b, 'content': [fix(x) for x in b['content']]}
        return b
    return [{**m, 'content': [fix(b) for b in m['content']]} if isinstance(m['content'], list) else m for m in msgs]


def echo(blocks):
    """A reply as it goes back into the conversation. If the model declined midway and the API's fallback model took
    over (a 'fallback' block), the first model's own blocks before that point are left out, as the API requires."""
    out = []
    for b in blocks:
        d = b.to_dict(mode='json')
        d.pop('parsed_output', None)   # the SDK's own field, not the API's
        out.append(d)
    last = max((i for i, b in enumerate(out) if b.get('type') == 'fallback'), default=-1)
    if last < 0: return out
    answered = {b.get('tool_use_id') for b in out[:last] if b.get('type', '').endswith('_tool_result')}
    keep = lambda b: b['type'] == 'text' or b['type'].endswith('_tool_result') or (b['type'] == 'server_tool_use' and b.get('id') in answered)
    return [b for b in out[:last] if keep(b)] + out[last + 1:]


def search_library(query):
    try:
        with open(os.path.join(WORK, 'library.tsv'), encoding='utf-8') as f: lines = f.read().splitlines()
    except OSError:
        return 'The library isn\'t available yet (the page sends it when the chat opens).'
    head, rows = (lines[0], lines[1:]) if lines else ('', [])
    words = [w for w in re.findall(r'\w+', query.lower()) if len(w) > 1]
    scored = sorted(((sum(w in r.lower() for w in words), i) for i, r in enumerate(rows)), key=lambda x: (-x[0], x[1]))
    hits = [rows[i] for n, i in scored[:15] if n]
    return head + '\n' + '\n'.join(hits) if hits else f'No paper matches "{query}". Try other keywords.'


def view_pages(pid, spec):
    path = os.path.join(WORK, 'papers', safe_id(pid) + '.pdf')
    if not os.path.exists(path): raise ValueError('No PDF of this paper is available here; use the text in <paper>.')
    if PYLIB not in sys.path: sys.path.insert(0, PYLIB)
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(path)
    n, pages = len(reader.pages), set()
    for a, z in re.findall(r'(\d+)(?:\s*-\s*(\d+))?', spec):
        a, z = int(a), int(z or a)
        pages.update(range(min(a, z), max(a, z) + 1))
    pages = sorted(p for p in pages if 1 <= p <= n)[:5]
    if not pages: raise ValueError(f'The paper has pages 1-{n}.')
    w = PdfWriter()
    for p in pages: w.add_page(reader.pages[p - 1])
    name = f'{safe_id(pid)}-{int(time.time() * 1000)}-pages.pdf'   # kept, so the conversation can be sent again as it was
    with open(os.path.join(WORK, 'attachments', name), 'wb') as f: w.write(f)
    return [{'type': 'text', 'text': f'Page{"s" if len(pages) > 1 else ""} {", ".join(map(str, pages))} of {n}:'},
            {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': '@' + name}}]


def run_tool(b, pid):
    """One of the bridge's own tools that Claude called; a failure goes back to Claude as an error it can act on."""
    inp = b.input if isinstance(b.input, dict) else {}
    try:
        if b.name == 'search_library' and isinstance(inp.get('query'), str) and inp['query'].strip():
            content = search_library(inp['query'])
        elif b.name == 'view_pages' and isinstance(inp.get('pages'), str):
            content = view_pages(pid, inp['pages'])
        else:
            raise ValueError(f'Unknown tool or input: {b.name} {json.dumps(inp)[:200]}')
        return {'type': 'tool_result', 'tool_use_id': b.id, 'content': content}
    except Exception as e:
        return {'type': 'tool_result', 'tool_use_id': b.id, 'content': str(e)[:500] or type(e).__name__, 'is_error': True}


class ApiTurn:
    """A reply being written through the API: stoppable like a Claude Code process (Stop, or the paper forgotten)."""
    def __init__(self): self.stopped, self.stream = False, None

    def terminate(self):
        self.stopped = True
        s = self.stream
        if s:   # wake the thread waiting for the next bytes (closing the response from here doesn't); it then closes it
            try: socket.socket.shutdown(s.response.extensions['network_stream'].get_extra_info('socket'), socket.SHUT_RDWR)
            except Exception: pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---- plumbing ----
    def origin_ok(self):
        # The dashboard's own pages; or a request with no Origin that no web page made: the browser extension's GETs
        # (Chrome sends no Origin there, and Sec-Fetch-Site: none). A web page's requests always say where they're from:
        # Origin on its fetches, Sec-Fetch-Site: cross-site on its images and links (which can't carry the token anyway).
        o = self.headers.get('Origin')
        return o in ORIGINS if o is not None else self.headers.get('Sec-Fetch-Site', 'none') == 'none'

    def cors(self):
        o = self.headers.get('Origin', '')
        if o in ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Access-Control-Allow-Headers', 'content-type, authorization, x-file-type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Vary', 'Origin')

    def reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status); self.cors()
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def authed(self):
        t = token()
        return bool(t) and secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + t)

    def do_OPTIONS(self):
        self.send_response(204 if self.origin_ok() else 403); self.cors()
        self.send_header('Content-Length', '0'); self.end_headers()

    def do_GET(self):
        if not self.origin_ok(): return self.reply(403, {'error': 'origin'})
        path, _, q = self.path.partition('?')
        params = dict(p.split('=', 1) for p in q.split('&') if '=' in p)
        if path == '/health':
            if not self.authed(): return self.reply(200, {'ok': True, 'version': VERSION, 'paired': False})
            return self.reply(200, {'ok': True, 'version': VERSION, 'paired': True, 'engine': engine(), 'api': bool(config().get('api_key'))})
        if not self.authed(): return self.reply(401, {'error': 'not paired'})
        if path == '/chats':
            return self.reply(200, {'chats': chat_list(urllib.request.unquote(params.get('paper', '')))})
        if path == '/paper/add':   # one paper you asked for (?id=); without an id, the one being added, else the last asked for
            with _lock:
                j = add_job(params.get('id', '')) if params.get('id') else (next((x for x in _adds['jobs'] if x['state'] == 'running'), None) or (_adds['jobs'][-1] if _adds['jobs'] else None))
                return self.reply(200 if j or not params.get('id') else 404, {'job': j and add_view(j)})
        if path == '/paper/adds':   # every paper asked for lately, newest first (waiting, being added, and finished)
            with _lock: return self.reply(200, {'jobs': [add_view(j) for j in reversed(_adds['jobs'])]})
        if path == '/uploads':   # which uploaded papers' files the Mac keeps
            return self.reply(200, {'uploads': upload_list()})
        if path == '/upload':   # one of them, back to a browser that lost its own (or never had it)
            pid, kind = urllib.request.unquote(params.get('paper', '')), params.get('kind', '')
            f = upload_path(pid, kind)
            if not f or not os.path.exists(f): return self.reply(404, {'error': 'not kept'})
            meta = read_json(upload_path(pid, kind, meta=True), {})
            self.send_response(200); self.cors()
            self.send_header('Content-Type', meta.get('type') or UPLOAD_KINDS[kind]); self.send_header('Content-Length', str(os.path.getsize(f)))
            self.send_header('X-File-Name', urllib.request.quote(meta.get('name', ''))); self.send_header('Access-Control-Expose-Headers', 'X-File-Name')
            self.send_header('Cache-Control', 'no-store'); self.end_headers()
            with open(f, 'rb') as fh:
                try: shutil.copyfileobj(fh, self.wfile, 1 << 20)
                except (BrokenPipeError, ConnectionResetError): pass
            return
        if path == '/topic/tag':
            return self.reply(200, {'job': _topic['job']})
        if path == '/lists/changes':   # lists changed here lately, newest first
            return self.reply(200, {'changes': changes_view({'user_lists'})})
        if path == '/papers/changes':   # lists and stars
            return self.reply(200, {'changes': changes_view()})
        if path == '/chat':
            pid = urllib.request.unquote(params.get('paper', ''))
            c = load_chat(pid)
            # fresh: the bridge doesn't have this conversation's paper yet (the page then sends its text and PDF)
            return self.reply(200, {'messages': c['messages'], 'model': c.get('model', 'opus'), 'running': pid in _running, 'fresh': not c.get('paper_text'),
                                    'engine': engine(), 'api': bool(config().get('api_key')),
                                    'side': side_view(pid, c['messages']), 'side_running': _side_tid.get(pid) if ('side:' + pid) in _running else None})
        return self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.origin_ok(): return self.reply(403, {'error': 'origin'})
        path = self.path.partition('?')[0]
        if path == '/pair':
            d = self.body()
            code = re.sub(r'\D', '', str(d.get('code', '')))[:6]   # digits only: it goes into the dialog's script
            if not ask_permission(code, str(d.get('who', ''))): return self.reply(403, {'error': 'declined'})
            t = token() or secrets.token_urlsafe(32)
            with open(TOKEN_FILE, 'w') as f: f.write(t)
            os.chmod(TOKEN_FILE, 0o600)
            return self.reply(200, {'token': t})
        if not self.authed(): return self.reply(401, {'error': 'not paired'})
        if path == '/upload':   # keep a copy of an uploaded paper's PDF or video (the body is the file itself)
            return self.receive_upload()
        data = self.body()
        if path == '/upload/delete':   # the video was removed from the paper
            f = upload_path(data.get('paper', ''), data.get('kind', ''))
            if not f: return self.reply(400, {'error': 'paper'})
            for x in (f, f + '.json'):
                if os.path.exists(x): os.remove(x)
            return self.reply(200, {'ok': True})
        if path == '/chat/new':
            pid = data.get('paper', '')
            c = load_chat(pid)
            if c['messages']: archive_current(pid)   # keep the old conversation, start a fresh one
            write_json(chat_file(pid), {'paper': pid, 'session': None, 'model': c.get('model', 'opus'), 'messages': []})
            return self.reply(200, {'ok': True})
        if path == '/chat/switch':   # go back to an earlier conversation; the current one is kept
            pid, key = data.get('paper', ''), str(data.get('id', ''))
            if pid in _running: return self.reply(409, {'error': 'busy'})
            target = chat_file(pid)[:-5] + f'.{key}.json'
            if not key.isdigit() or not os.path.exists(target): return self.reply(404, {'error': 'no such chat'})
            cur = chat_file(pid)
            if load_chat(pid)['messages']: archive_current(pid)
            os.replace(target, cur)
            return self.reply(200, {'ok': True})
        if path == '/chat/stop':
            p = _running.get(('side:' if data.get('side') else '') + data.get('paper', ''))
            if p: p.terminate()
            return self.reply(200, {'ok': True})
        if path == '/chat/send':
            return self.send_chat(data)
        if path == '/chat/side':   # a side chat's question (bridge v9)
            return self.side_chat(data)
        if path == '/chat/side/delete':   # deleted from the page (kept a day, for Undo: undo=true)
            pid, tid = data.get('paper', ''), str(data.get('thread', ''))
            with _lock:
                side = load_side(pid)
                t = side['threads'].get(tid)
                if not t: return self.reply(404, {'error': 'no such side chat'})
                if data.get('undo'): t.pop('deleted', None)
                else: t['deleted'] = time.time()
                write_json(side_file(pid), side)
            return self.reply(200, {'ok': True})
        if path == '/engine':   # the chat panel's Code / API switch: one setting for every chat and device
            e = data.get('engine')
            cfg = config()
            if e not in ('code', 'api') or (e == 'api' and not cfg.get('api_key')): return self.reply(400, {'error': 'engine'})
            cfg['engine'] = e
            write_json(CONFIG_FILE, cfg, private=True)
            return self.reply(200, {'engine': engine()})
        if path == '/paper/forget':   # an uploaded paper was removed: keep nothing of it
            pid = data.get('paper', '')
            if not re.fullmatch(r'up-[0-9a-f]{12}', pid): return self.reply(400, {'error': 'only uploaded papers'})
            if pid in _running: _running[pid] and _running[pid].terminate()
            return self.reply(200, {'ok': True, 'removed': forget_paper(pid)})
        if path == '/paper/add':   # add a paper you chose: research (Claude, read-only), then publish (add_paper.py); it waits its turn
            text = str(data.get('input', '')).strip()[:2000]
            if not text: return self.reply(400, {'error': 'input'})
            with _lock:
                lists = data.get('lists') if isinstance(data.get('lists'), list) else []
                j = queue_add(text, bool(data.get('star')), str(data.get('title') or ''), str(data.get('from') or 'dashboard'), lists)
                if not j: return self.reply(429, {'error': 'Too many papers are waiting to be added; try again once some are in.'})
                return self.reply(200, {'job': add_view(j)})
        if path == '/topic/tag':   # a topic was approved: put it on the existing papers that belong in it
            name = str(data.get('topic', '')).strip()[:200]
            if not name: return self.reply(400, {'error': 'topic'})
            with _lock:
                j = _topic['job']
                if j and j['state'] == 'running': return self.reply(409, {'error': 'busy', 'job': j})
                return self.reply(200, {'job': start_topic(name)})
        if path in ('/lists', '/papers/edit'):   # papers into a list (made if it's new) or out of one, starred or not:
            # {papers: [ids, links or titles], add: [names], remove: [names], star: true | false (left alone if absent)}
            papers = [str(x)[:300] for x in (data.get('papers') or []) if str(x).strip()][:50] if isinstance(data.get('papers'), list) else []
            add = data.get('add') if isinstance(data.get('add'), list) else []
            remove = data.get('remove') if isinstance(data.get('remove'), list) else []
            star = data.get('star') if isinstance(data.get('star'), bool) else None
            if not papers: return self.reply(400, {'error': 'Name the papers.'})
            try:
                out = add_paper.edit_papers(papers, add, remove, star)
            except add_paper.Stop as e:
                return self.reply(400, {'error': str(e)})
            except Exception as e:
                return self.reply(500, {'error': (str(e) or 'failed')[-300:]})
            log_lists(out, str(data.get('from') or 'dashboard')[:20])
            return self.reply(200, out)
        if path == '/paper/add/stop':   # one paper (id), or the one being added: a waiting one is just taken off the queue
            with _lock:
                j = add_job(str(data.get('id', ''))) if data.get('id') else next((x for x in _adds['jobs'] if x['state'] == 'running'), None)
                if j and j['state'] == 'queued': j.update(state='stopped', message='Stopped before it started.', ended=time.time())
                elif j and j['state'] == 'running':
                    j['stopping'] = True
                    if _adds['proc']: _adds['proc'].terminate()
            return self.reply(200, {'ok': True, 'job': j and add_view(j)})
        if path == '/library':   # the user's paper list, for finding papers they describe (no notes in it)
            with open(os.path.join(WORK, 'library.tsv'), 'w') as f: f.write(str(data.get('tsv', ''))[:3_000_000])
            return self.reply(200, {'ok': True})
        return self.reply(404, {'error': 'not found'})

    # ---- one turn: Claude Code (this paper's session) or the API (the conversation kept here), streamed back ----
    def send_chat(self, d):
        pid = d.get('paper', '')
        if not pid: return self.reply(400, {'error': 'paper'})
        with _lock:
            if pid in _running: return self.reply(409, {'error': 'busy'})
            _running[pid] = None
        try:
            c = load_chat(pid)
            model = d.get('model') if d.get('model') in MODELS else c.get('model', 'opus')
            eng = engine()
            pdf_path = os.path.join(WORK, 'papers', safe_id(pid) + '.pdf')
            if not c.get('paper_text') and (d.get('pdfBase64') or not os.path.exists(pdf_path)):   # a file you gave may have changed since
                self.fetch_pdf(d.get('pdfUrl'), d.get('pdfBase64'), pdf_path)
            if d.get('paperText'): c['paper_text'] = d['paperText']   # kept, so either engine can be given it
            names = []
            for i, img in enumerate(d.get('images') or []):   # figure crops: saved where Claude can see them
                m = re.match(r'data:image/(png|jpeg);base64,(.+)', img or '')
                if not m: continue
                name = f'{safe_id(pid)}-{int(time.time())}-{i}.{m.group(1)}'
                with open(os.path.join(WORK, 'attachments', name), 'wb') as f: f.write(base64.b64decode(m.group(2)))
                names.append(name)
            # the turns this engine hasn't seen (answered by the other one)
            if c.get('session') and 'code_seen' not in c: c['code_seen'] = len(c['messages'])   # a chat from before the API engine
            seen_key = eng + '_seen'
            missed = c['messages'][c.get(seen_key, 0):]

            now, at = time.time(), len(c['messages'])
            c['messages'].append({'role': 'user', 'text': d.get('display', d.get('message', '')), 'quotes': d.get('quotes') or [], 'images': len(names), 'mentions': d.get('mentions') or [], 'auto': bool(d.get('auto')), 'ts': now})   # what you typed, not the wrapped prompt
            c['model'] = model
            write_json(chat_file(pid), c)

            self.send_response(200); self.cors()
            self.send_header('Content-Type', 'application/x-ndjson'); self.send_header('Cache-Control', 'no-store')
            self.send_header('Transfer-Encoding', 'chunked'); self.end_headers()

            def emit(obj):
                b = (json.dumps(obj) + '\n').encode()
                try:
                    self.wfile.write(f'{len(b):x}\r\n'.encode() + b + b'\r\n'); self.wfile.flush()
                except Exception:
                    pass

            profile = ('\n\nAbout the user: ' + d['profile']) if d.get('profile') else ''
            try:
                run = self.api_turn if eng == 'api' else self.code_turn
                text, err, state = run(c, d, pid, model, names, missed, profile, emit)
            except Exception as e:
                text, err, state = '', str(e) or type(e).__name__, {}
            c, ats = load_chat(pid), time.time()
            if len(c['messages']) > at and c['messages'][at].get('ts') == now:   # still this conversation (not a New chat meanwhile)
                if text: c['messages'].append({'role': 'assistant', 'text': text, 'ts': ats, 'model': model, 'engine': eng})
                c.update(state)
                if text: c[seen_key] = len(c['messages'])   # (a question that got no answer is never passed on: see earlier_turns)
                write_json(chat_file(pid), c)
            emit({'t': 'done', 'engine': eng, 'error': err if not text else '', 'ts': ats if text else None, 'uts': now})   # (ts: the answer's, which side chats are tied to)
            try: self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            except Exception: pass
        finally:
            _running.pop(pid, None)

    def side_chat(self, d):
        """A side chat: a quick question about words in one of the main chat's answers, to a cheaper model, in its own
        conversation (one per selection), given the paper and the main conversation so far. The main chat never sees it."""
        pid, tid, anchor = d.get('paper', ''), str(d.get('thread', '')), d.get('anchor') or {}
        if not pid or not re.fullmatch(r'[A-Za-z0-9_-]{4,40}', tid): return self.reply(400, {'error': 'thread'})
        key = 'side:' + pid
        with _lock:
            if key in _running: return self.reply(409, {'error': 'busy'})
            _running[key] = None
            _side_tid[pid] = tid
        try:
            c, now = load_chat(pid), time.time()
            with _lock:
                side = load_side(pid)
                t = side['threads'].get(tid)
                if not t:
                    if not any(m.get('ts') == anchor.get('ts') and m.get('role') == 'assistant' for m in c['messages']): return self.reply(404, {'error': 'no such answer'})
                    t = side['threads'][tid] = {'anchor': {'ts': anchor['ts'], 'text': str(anchor.get('text', ''))[:4000], 'key': str(anchor.get('key', ''))[:4000]}, 'messages': [], 'created': now}
                t.pop('deleted', None)
                t['messages'].append({'role': 'user', 'text': str(d.get('message') or 'What does this mean?')[:8000], 'ts': now})
                write_json(side_file(pid), side)

            self.send_response(200); self.cors()
            self.send_header('Content-Type', 'application/x-ndjson'); self.send_header('Cache-Control', 'no-store')
            self.send_header('Transfer-Encoding', 'chunked'); self.end_headers()
            def emit(obj):
                b = (json.dumps(obj) + '\n').encode()
                try: self.wfile.write(f'{len(b):x}\r\n'.encode() + b + b'\r\n'); self.wfile.flush()
                except Exception: pass

            eng, profile = engine(), ('\n\nAbout the user: ' + d['profile']) if d.get('profile') else ''
            try:
                text, err, state = (self.side_api_turn if eng == 'api' else self.side_code_turn)(c, t, pid, profile, emit)
            except Exception as e:
                text, err, state = '', str(e) or type(e).__name__, {}
            ats = time.time()
            with _lock:
                side = load_side(pid)
                t = side['threads'].get(tid)
                if t and t['messages'] and t['messages'][-1].get('ts') == now:
                    if text: t['messages'].append({'role': 'assistant', 'text': text, 'ts': ats, 'model': SIDE_MODEL, 'engine': eng})
                    else: t['messages'].pop()   # no answer: the question isn't kept (the page offers it again)
                    t.update(state)
                    if not t['messages'] and not t.get('session') and not t.get('api'): del side['threads'][tid]
                    write_json(side_file(pid), side)
            emit({'t': 'done', 'engine': eng, 'error': err if not text else '', 'ts': ats if text else None, 'uts': now})
            try: self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            except Exception: pass
        finally:
            _running.pop(key, None)
            _side_tid.pop(pid, None)

    def side_code_turn(self, c, t, pid, profile, emit):
        fresh = not t.get('session')
        session = t.get('session') or str(uuid.uuid4())
        parts = (side_intro(c, t) if fresh else []) + [t['messages'][-1]['text']]
        args = [claude_bin(), '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                '--model', MODELS[SIDE_MODEL], '--system-prompt', SIDE_SYSTEM.format(profile=profile),
                '--tools', '',   # no tools: it has what it needs, and answers fast
                '--strict-mcp-config', '--mcp-config', NO_MCP, '--setting-sources', '',
                ('--session-id' if fresh else '--resume'), session]
        env = dict(os.environ); env['PATH'] = os.path.dirname(claude_bin()) + ':' + env.get('PATH', '/usr/bin:/bin')
        proc = subprocess.Popen(args, cwd=WORK, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _running['side:' + pid] = proc
        proc.stdin.write('\n\n'.join(parts).encode()); proc.stdin.close()
        text, result = code_stream(proc, emit)
        err = '' if result else (proc.stderr.read().decode(errors='replace')[-600:] or 'stopped')
        if result and result.get('result') and not text: text = result['result']
        return text, err, ({'session': session} if text else {})   # a session that never started: the next question starts one

    def side_api_turn(self, c, t, pid, profile, emit):
        turn = ApiTurn()
        _running['side:' + pid] = turn
        try: anthropic, client = anthropic_sdk()
        except ImportError: return '', 'the Anthropic SDK isn\'t installed for the bridge: run bash bridge/api_setup.sh', {}
        hist = t.get('api') or []
        new = {'role': 'user', 'content': [{'type': 'text', 'text': '\n\n'.join(([] if hist else side_intro(c, t)) + [t['messages'][-1]['text']])}]}
        text, err = '', ''
        try:
            with client.beta.messages.stream(model=API_MODELS[SIDE_MODEL], max_tokens=8000, system=SIDE_SYSTEM.format(profile=profile), messages=hist + [new],
                                             cache_control={'type': 'ephemeral', 'ttl': '1h'}) as s:   # the paper, between follow-ups
                turn.stream = s
                for ev in s:
                    if turn.stopped: break
                    if ev.type == 'content_block_delta' and ev.delta.type == 'text_delta':
                        text += ev.delta.text; emit({'t': 'text', 'v': ev.delta.text})
                if not turn.stopped and s.get_final_message().stop_reason == 'max_tokens': err = 'the answer reached its length limit.'
        except Exception as e:
            err = 'stopped' if turn.stopped else api_error(anthropic, e)
        finally:
            turn.stream = None
        if turn.stopped and not err: err = 'stopped'
        if text.strip(): return text, err, {'api': hist + [new, {'role': 'assistant', 'content': [{'type': 'text', 'text': text.strip()}]}]}
        return text, err, {}

    def code_turn(self, c, d, pid, model, names, missed, profile, emit):
        fresh = not c.get('session')
        session = c.get('session') or str(uuid.uuid4())
        pdf_name = safe_id(pid) + '.pdf'
        parts = []
        if fresh and c.get('paper_text'): parts.append('<paper>\n' + c['paper_text'] + '\n</paper>')
        if earlier_turns(missed): parts.append(earlier_turns(missed))
        if d.get('context'): parts.append('<context>\n' + d['context'] + '\n</context>')
        if names: parts.append('Attached images (Read them): ' + ', '.join('./attachments/' + n for n in names))
        parts.append(d.get('message', ''))
        how = {**SYSTEM_CODE, 'pdf_how': SYSTEM_CODE['pdf_how'].format(pdf=pdf_name if os.path.exists(os.path.join(WORK, 'papers', pdf_name)) else '(none)')}
        args = [claude_bin(), '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                '--model', MODELS[model], '--system-prompt', SYSTEM.format(**how, notes=NOTES, profile=profile),
                *(['--effort', d['effort']] if d.get('effort') in EFFORTS else []),
                '--tools', 'Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch',
                '--allowedTools', 'Read(./**)', 'Grep(./**)', 'Glob(./**)', 'WebSearch', 'WebFetch',   # files: this folder only
                '--strict-mcp-config', '--mcp-config', NO_MCP, '--setting-sources', '',
                ('--session-id' if fresh else '--resume'), session]
        env = dict(os.environ); env['PATH'] = os.path.dirname(claude_bin()) + ':' + env.get('PATH', '/usr/bin:/bin')
        proc = subprocess.Popen(args, cwd=WORK, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _running[pid] = proc
        proc.stdin.write('\n\n'.join(parts).encode()); proc.stdin.close()
        c['session'] = session
        write_json(chat_file(pid), c)

        text, result = code_stream(proc, emit)
        err = '' if result else (proc.stderr.read().decode(errors='replace')[-600:] or 'stopped')
        if result and result.get('result') and not text: text = result['result']
        return text, err, ({'session': None} if fresh and not text else {})   # a session that never started: the next message starts it again

    def api_turn(self, c, d, pid, model, names, missed, profile, emit):
        turn = ApiTurn()
        _running[pid] = turn
        try: anthropic, client = anthropic_sdk()
        except ImportError: return '', 'the Anthropic SDK isn\'t installed for the bridge: run bash bridge/api_setup.sh', {}
        hist = c.get('api') or []
        # the system prompt stays as the conversation began: changing it would make the API drop Claude's earlier thinking
        system = (hist and c.get('api_system')) or SYSTEM.format(**SYSTEM_API, notes=NOTES, profile=profile)
        # a conversation begun under an older prompt keeps it (see above), so it hears once what it can add now
        told = not hist or NOTES in system or c.get('api_notes') == NOTES_V
        head = []
        if not hist and c.get('paper_text'): head.append('<paper>\n' + c['paper_text'] + '\n</paper>')
        if not told: head.append('<app-update>The app has changed what you can add to my notes; this replaces any earlier instruction about notes and highlights.\n' + NOTES + '</app-update>')
        if earlier_turns(missed): head.append(earlier_turns(missed))
        if d.get('context'): head.append('<context>\n' + d['context'] + '\n</context>')
        content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/' + n.rsplit('.', 1)[1], 'data': '@' + n}} for n in names]
        content.append({'type': 'text', 'text': '\n\n'.join(head + [d.get('message', '') or 'Explain this.'])})
        new = [{'role': 'user', 'content': content}]   # this turn's messages, kept once it's complete
        params = dict(model=API_MODELS[model], max_tokens=64000, system=system, tools=api_tools(model),
                      cache_control={'type': 'ephemeral', 'ttl': '1h'},   # the paper and the conversation so far, between questions
                      betas=['server-side-fallback-2026-07-01'], fallbacks='default')   # a model that declines hands over to another
        if d.get('effort') in EFFORTS: params['output_config'] = {'effort': d['effort']}

        text, err, kind, done = '', '', None, False
        try:
            for _ in range(16):   # rounds of tool calls
                if turn.stopped: break
                with client.beta.messages.stream(messages=hydrate(hist + new), **params) as s:
                    turn.stream = s
                    for ev in s:
                        if turn.stopped: break
                        if ev.type == 'content_block_start':
                            t = ev.content_block.type
                            if t in ('tool_use', 'server_tool_use'): emit({'t': 'tool', 'v': ev.content_block.name})
                            elif t == 'text' and text and kind != 'text' and not text.endswith('\n\n'):
                                text += '\n\n'; emit({'t': 'text', 'v': '\n\n'})   # new text after a tool call
                            kind = t
                        elif ev.type == 'content_block_delta' and ev.delta.type == 'text_delta':
                            text += ev.delta.text; emit({'t': 'text', 'v': ev.delta.text})
                    if turn.stopped: break
                    msg = s.get_final_message()
                new.append({'role': 'assistant', 'content': echo(msg.content)})
                if msg.stop_reason == 'tool_use':
                    new.append({'role': 'user', 'content': [run_tool(b, pid) for b in msg.content if b.type == 'tool_use']})
                elif msg.stop_reason != 'pause_turn':   # pause_turn: a long web search, carried on by sending it back
                    if msg.stop_reason == 'refusal': err = 'declined to answer this.'
                    elif msg.stop_reason == 'max_tokens': err = 'the answer reached its length limit.'
                    elif msg.stop_reason in ('end_turn', 'stop_sequence') and text: done = True
                    else: err = 'the answer came back empty or cut off.'
                    break
            else:
                err = 'stopped after too many tool calls.'
        except Exception as e:
            err = 'stopped' if turn.stopped else api_error(anthropic, e)
        finally:
            turn.stream = None
        if turn.stopped and not err: err = 'stopped'
        if done and text: return text, err, {'api': hist + new, 'api_system': system, 'api_notes': NOTES_V}
        if text.strip():   # stopped or failed partway: what was shown is kept, as a plain answer
            return text, err, {'api': hist + [new[0], {'role': 'assistant', 'content': [{'type': 'text', 'text': text.strip()}]}], 'api_system': system, 'api_notes': NOTES_V}
        return text, err, {}   # nothing to keep: the next message is sent as if this one never was

    def receive_upload(self):
        params = dict(p.split('=', 1) for p in self.path.partition('?')[2].split('&') if '=' in p)
        pid, kind = urllib.request.unquote(params.get('paper', '')), params.get('kind', '')
        f, n = upload_path(pid, kind), int(self.headers.get('Content-Length') or 0)
        if not f or not n: return self.reply(400, {'error': 'paper'})
        if n > UPLOAD_MAX: return self.reply(413, {'error': 'too large'})
        tmp = f + '.part'
        try:
            with open(tmp, 'wb') as out:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk: raise ConnectionError('the upload stopped')
                    out.write(chunk); left -= len(chunk)
            os.replace(tmp, f)
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return self.reply(500, {'error': str(e)[:200]})
        name = urllib.request.unquote(params.get('name', ''))[:300]
        write_json(upload_path(pid, kind, meta=True), {'name': name, 'type': (self.headers.get('X-File-Type') or '')[:100], 'size': n, 'at': time.time()})
        return self.reply(200, {'ok': True, 'size': n})

    def fetch_pdf(self, url, b64, path):
        try:
            if b64:
                with open(path, 'wb') as f: f.write(base64.b64decode(b64))
            elif url and url.startswith('https://'):
                req = urllib.request.Request(url, headers={'User-Agent': 'ScholarDashboard/1'})
                with urllib.request.urlopen(req, timeout=30) as r, open(path, 'wb') as f: shutil.copyfileobj(r, f)
        except Exception:
            try: os.remove(path)
            except Exception: pass


if __name__ == '__main__':
    print(f'Scholar bridge on http://127.0.0.1:{PORT} (claude: {claude_bin()})', flush=True)
    threading.Thread(target=add_worker, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()

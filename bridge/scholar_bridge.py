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
  * Adding a paper (the dashboard's Add paper box, /paper/add): Claude only researches it, read-only, in the bridge's
    own copy of the repository (~/.scholar-bridge/repo); add_paper.py checks what it returns and commits and pushes
    just that row (and its link-preview page) from that copy. Your working copy is never touched.
  * The API key stays in ~/.scholar-bridge/config.json (readable by you alone); the page never sees it.

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
ORIGINS |= set(filter(None, os.environ.get('SCHOLAR_BRIDGE_ORIGINS', '').split(',')))   # a test copy of the site
HOME = os.path.expanduser(os.environ.get('SCHOLAR_BRIDGE_HOME', '~/.scholar-bridge'))   # override: a separate test copy
WORK = os.path.join(HOME, 'work')
CHATS = os.path.join(HOME, 'chats')
TOKEN_FILE = os.path.join(HOME, 'token')
CONFIG_FILE = os.path.join(HOME, 'config.json')   # the API key (bridge/api_setup.sh) and which engine Chat uses
PYLIB = os.path.join(HOME, 'pylib')   # the Anthropic SDK and pypdf, installed here by api_setup.sh (not into your Python)
SESSIONS_FILE = os.path.join(HOME, 'sessions.json')
NO_MCP = os.path.join(HOME, 'no-mcp.json')
MODELS = {'opus': 'opus', 'sonnet': 'sonnet', 'haiku': 'haiku'}
API_MODELS = {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5', 'haiku': 'claude-haiku-5-5'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}   # anything else: the default of Claude Code or of the model
VERSION = 4   # 2: /paper/add; 3: /topic/tag; 4: the Claude API engine (/engine)

for d in (HOME, WORK, CHATS, os.path.join(WORK, 'papers'), os.path.join(WORK, 'attachments')):
    os.makedirs(d, exist_ok=True)
if not os.path.exists(NO_MCP):
    with open(NO_MCP, 'w') as f: json.dump({'mcpServers': {}}, f)

_lock = threading.Lock()
_running = {}   # paper id -> subprocess.Popen
_add = {'job': None, 'proc': None}   # the paper being added (one at a time); the page polls GET /paper/add
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


def start_add(text, star):
    job = {'id': uuid.uuid4().hex[:12], 'state': 'running', 'stage': 'Starting…', 'input': text[:300], 'started': time.time()}
    _add['job'] = job

    def stage(s): job['stage'] = s
    def run():
        try:
            out = add_paper.add(text, star=star, cb=stage, on_proc=lambda p: _add.__setitem__('proc', p))
            job.update(state='done', added=out['added'], existing=out['existing'])
        except add_paper.Stop as e:
            job.update(state='stopped', message=str(e))
        except Exception as e:
            job.update(state='error', message=(str(e) or 'failed')[-400:])
        finally:
            _add['proc'] = None; job['ended'] = time.time()
    threading.Thread(target=run, daemon=True).start()
    return job


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


def forget_paper(pid):
    """Everything kept for a paper: its chats, the PDF and figure copies, and Claude Code's own transcripts of them."""
    base = safe_id(pid)
    if not base: return 0
    sessions, gone = set(), 0
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


def ask_permission(code):
    """A native dialog on this laptop: the only way to get a token."""
    script = ('display dialog "Scholar Dashboard wants to chat with Claude through Claude Code on this Mac.\\n\\n'
              f'Code shown in the page: {code}" with title "Scholar Dashboard" buttons {{"Don\'t Allow", "Allow"}} '
              'default button "Allow" cancel button "Don\'t Allow" with icon note giving up after 60')
    try:
        r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=70)
        return r.returncode == 0 and 'Allow' in r.stdout and 'gave up:true' not in r.stdout
    except Exception:
        return False


SYSTEM = """You are a research assistant inside the user's paper-reading app. The user is reading one paper; its full text is given in <paper> in the conversation (page-marked){pdf_how}

Each message may start with <context>: the user's current highlights, comments and notes on this paper (they change as they read), their research interests, and sometimes other papers they @-mentioned. Use them; don't repeat them back.

Citing the paper: whenever you point to something in the paper, cite it with its exact words in this form: [[p.N "exact words from the paper"]] (N = page). The quote must be contiguous and verbatim (no ellipses, no paraphrase), a phrase to a sentence long, with symbols as they appear in the text. The app shows each citation only as a small clickable pill (page number and a few words), so the reader cannot read its words: it is a footnote marker, never part of your prose. Every sentence, list item and table cell must say everything on its own, as if the citation were deleted; put the citation right after the claim it supports. Wrong: "Predicates live on [[p.2 "the low-dimensional state space"]]" (reads as "Predicates live on p.2 the low-dime…"). Right: "Predicates are defined over a low-dimensional state space [[p.2 "the low-dimensional state space"]]." A citation never replaces words, and a table cell never consists of only a citation. Tables are where this slips most: wrong "| $\\phi$ | The sensor. It [[p.4 "is the observation function"]]. |", right "| $\\phi$ | The sensor: maps the true state to what the sensors observe [[p.4 "is the observation function"]]. |". Before sending, reread each sentence with its citations removed; if any is incomplete, rewrite it. Use [[p.N "..."]] only for the paper being read (its pills jump within it); cite other papers, including ones the user @-mentions, in plain text by title or author, with a page if useful.

Other papers: {library_how} If more than one could fit, ask them with a short numbered list of titles. Once you know which paper(s) they mean, reply with only one line per paper, ```paper <id>```, and nothing else: the app then sends you that paper's text and the user's own notes on it, and you continue answering their question. Papers they @-mention arrive the same way, in <other-paper>.

Notes and highlights: never add to the user's notes unless they explicitly ask you to. When they do, put each note in a fenced block ```note ... ``` (Markdown; you may cite with [[p.N "..."]] inside), and each highlight as ```highlight p.N color "exact words"``` (color is one of orange, green, blue, red, purple). The app saves them for the user.

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
        return self.headers.get('Origin', '') in ORIGINS

    def cors(self):
        o = self.headers.get('Origin', '')
        if o in ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Access-Control-Allow-Headers', 'content-type, authorization')
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
        if path == '/paper/add':   # the paper being (or last) added
            return self.reply(200, {'job': _add['job']})
        if path == '/topic/tag':
            return self.reply(200, {'job': _topic['job']})
        if path == '/chat':
            pid = urllib.request.unquote(params.get('paper', ''))
            c = load_chat(pid)
            # fresh: the bridge doesn't have this conversation's paper yet (the page then sends its text and PDF)
            return self.reply(200, {'messages': c['messages'], 'model': c.get('model', 'opus'), 'running': pid in _running, 'fresh': not c.get('paper_text'),
                                    'engine': engine(), 'api': bool(config().get('api_key'))})
        return self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.origin_ok(): return self.reply(403, {'error': 'origin'})
        path = self.path.partition('?')[0]
        if path == '/pair':
            code = re.sub(r'\D', '', str(self.body().get('code', '')))[:6]   # digits only: it goes into the dialog's script
            if not ask_permission(code): return self.reply(403, {'error': 'declined'})
            t = token() or secrets.token_urlsafe(32)
            with open(TOKEN_FILE, 'w') as f: f.write(t)
            os.chmod(TOKEN_FILE, 0o600)
            return self.reply(200, {'token': t})
        if not self.authed(): return self.reply(401, {'error': 'not paired'})
        data = self.body()
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
            p = _running.get(data.get('paper', ''))
            if p: p.terminate()
            return self.reply(200, {'ok': True})
        if path == '/chat/send':
            return self.send_chat(data)
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
        if path == '/paper/add':   # add a paper you chose: research (Claude, read-only), then publish (add_paper.py)
            text = str(data.get('input', '')).strip()[:2000]
            if not text: return self.reply(400, {'error': 'input'})
            with _lock:
                j = _add['job']
                if j and j['state'] == 'running': return self.reply(409, {'error': 'busy', 'job': j})
                return self.reply(200, {'job': start_add(text, bool(data.get('star')))})
        if path == '/topic/tag':   # a topic was approved: put it on the existing papers that belong in it
            name = str(data.get('topic', '')).strip()[:200]
            if not name: return self.reply(400, {'error': 'topic'})
            with _lock:
                j = _topic['job']
                if j and j['state'] == 'running': return self.reply(409, {'error': 'busy', 'job': j})
                return self.reply(200, {'job': start_topic(name)})
        if path == '/paper/add/stop':
            p = _add['proc']
            if p: p.terminate()
            return self.reply(200, {'ok': True})
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
            c = load_chat(pid)
            if len(c['messages']) > at and c['messages'][at].get('ts') == now:   # still this conversation (not a New chat meanwhile)
                if text: c['messages'].append({'role': 'assistant', 'text': text, 'ts': time.time(), 'model': model, 'engine': eng})
                c.update(state)
                if text: c[seen_key] = len(c['messages'])   # (a question that got no answer is never passed on: see earlier_turns)
                write_json(chat_file(pid), c)
            emit({'t': 'done', 'engine': eng, 'error': err if not text else ''})
            try: self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            except Exception: pass
        finally:
            _running.pop(pid, None)

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
                '--model', MODELS[model], '--system-prompt', SYSTEM.format(**how, profile=profile),
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
        system = (hist and c.get('api_system')) or SYSTEM.format(**SYSTEM_API, profile=profile)
        head = []
        if not hist and c.get('paper_text'): head.append('<paper>\n' + c['paper_text'] + '\n</paper>')
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
        if done and text: return text, err, {'api': hist + new, 'api_system': system}
        if text.strip():   # stopped or failed partway: what was shown is kept, as a plain answer
            return text, err, {'api': hist + [new[0], {'role': 'assistant', 'content': [{'type': 'text', 'text': text.strip()}]}], 'api_system': system}
        return text, err, {}   # nothing to keep: the next message is sent as if this one never was

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
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()

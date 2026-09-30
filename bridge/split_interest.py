#!/usr/bin/env python3
"""Split a broad interest into narrower ones, re-tagging the papers.

Claude reads every paper's title and abstract and says which of the new interests apply (and how strongly: about /
partly / touches); papers that had the old interest are also checked against some existing interests that take
over part of it. This script checks the answers and publishes from the bridge's own copy of the repository: the new
interests (confirmed, Related), the old one retired (status rejected, with a note naming what replaced it, so digests
never suggest it again), and the papers' matched_interests / match_strengths updated. Strengths already on a paper
(your own edits) are never overwritten.

    python3 bridge/split_interest.py --rate      # ask Claude (cached in ~/.scholar-bridge/split_cache.json)
    python3 bridge/split_interest.py --report    # what it would change, and the effect on labels
    python3 bridge/split_interest.py --publish   # write and push (SCHOLAR_ADD_DRY_RUN=1: don't push)
"""
import concurrent.futures as cf, csv, datetime, json, os, re, subprocess, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_paper as ap
import rate_matches as rm
from add_paper import CLONE, PAPERS, INTERESTS

OLD = 'Safe & robust robot control'
NEW = [
    {'interest_name': 'Safety filters & shielding', 'category': 'method',
     'notes': 'Runtime filters or shields that minimally correct a controller\'s unsafe actions: MPC/predictive safety filters, shields, action projection, Lyapunov/backup-based filters (CBF-based filters also keep the CBF interest).',
     'related_to': 'Control Barrier Functions (CBFs), Hamilton-Jacobi reachability, Runtime verification of learned policies'},
    {'interest_name': 'Constraint-satisfying generative policies', 'category': 'method',
     'notes': 'Diffusion or flow-matching policies and planners that guarantee or enforce constraint satisfaction (projection, constrained sampling, guidance with guarantees).',
     'related_to': 'Diffusion models, Flow matching, Safety filters & shielding'},
    {'interest_name': 'Robust control under uncertainty', 'category': 'method',
     'notes': 'Controllers robust to disturbances, model error and uncertainty: robust, tube and stochastic MPC, robust learning-based control, disturbance rejection.',
     'related_to': 'Risk-aware control (CVaR/risk measures), Sampling-based motion planning'},
    {'interest_name': 'Safety of foundation-model policies', 'category': 'application',
     'notes': 'Safety of VLAs and generalist robot policies: safe behaviour, safety alignment, guardrails and failure analysis specific to foundation-model policies.',
     'related_to': 'Vision-Language-Action models (VLAs), Generalist policies, Uncertainty quantification & failure detection for learned policies'},
]
TAKEOVER = ['Policy evaluation methodology & benchmark validity', 'Safety benchmarking for learned policies']   # for the old interest's papers
CACHE = os.path.join(ap.HOME, 'split_cache.json')
BATCH, WORKERS = 40, 4
_lock = threading.Lock()

PROMPT = """You tag research papers with research interests for a robotics researcher's paper dashboard.
For each paper below, decide which of the candidate interests listed under it genuinely apply, and how strongly:
- "about": the interest is what the paper is about (its main method, object of study, or what it evaluates or verifies).
- "partly": a substantial part of the paper.
- "touches": it appears, but not substantially.
Leave out candidates that don't apply at all. Most papers match none or one of them. Judge from the title and abstract.

Candidate interests:
{glossary}

Reply with ONLY one JSON object mapping each paper id to an object of interest name -> "about" | "partly" | "touches"
(an empty object when none apply), using the exact ids and names given. No other text.

Papers:
{papers}"""


def rate():
    ap.fresh_clone()
    _, papers = ap.read_papers()
    with open(os.path.join(CLONE, INTERESTS), newline='', encoding='utf-8') as f:
        existing = {i['interest_name']: i for i in csv.DictReader(f)}
    defs = {n['interest_name']: n['notes'] for n in NEW}
    defs.update({t: (existing.get(t) or {}).get('notes', '') for t in TAKEOVER})
    glossary = '\n'.join(f'- {k}: {v}' for k, v in defs.items())
    todo = []
    for p in papers:
        if p.get('is_hidden') == 'true': continue
        cands = [n['interest_name'] for n in NEW] + (TAKEOVER if OLD in (p.get('matched_interests') or '').split('|') else [])
        p['_c'] = cands; todo.append(p)
    try: cache = json.load(open(CACHE))
    except Exception: cache = {}
    todo = [p for p in todo if p['id'] not in cache]
    print(f'{len(todo)} papers to check ({len(cache)} cached)', flush=True)

    def one(batch):
        def fmt(p):
            abstract = re.sub(r'\s+', ' ', p.get('abstract') or '')[:1200]
            return f"id: {p['id']}\ntitle: {p['title']}\nabstract: {abstract}\ncandidates: {' | '.join(p['_c'])}"
        prompt = PROMPT.format(glossary=glossary, papers='\n\n'.join(fmt(p) for p in batch))
        env = dict(os.environ); env['PATH'] = os.path.dirname(ap.claude_bin()) + ':' + env.get('PATH', '/usr/bin:/bin')
        with tempfile.TemporaryDirectory() as empty:
            r = subprocess.run([ap.claude_bin(), '-p', '--output-format', 'json', '--model', 'opus', '--tools', 'Read', '--allowedTools', 'Read(./**)',
                                '--strict-mcp-config', '--mcp-config', os.path.join(ap.HOME, 'no-mcp.json'), '--setting-sources', ''],
                               input=prompt, capture_output=True, text=True, cwd=empty, env=env, timeout=900)
        result = json.loads(r.stdout).get('result') or ''
        m = re.search(r'\{[\s\S]*\}', result)
        ans = json.loads(m.group(0)) if m else {}
        out = {}
        for p in batch:
            if p['id'] not in ans: continue
            got = ans[p['id']] or {}
            out[p['id']] = {k: v for k, v in got.items() if k in p['_c'] and v in rm.STRENGTHS}
        return out

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    done = 0
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for fut in cf.as_completed([ex.submit(one, b) for b in batches]):
            try:
                got = fut.result()
                with _lock:
                    cache.update(got)
                    with open(CACHE + '.tmp', 'w') as f: json.dump(cache, f)
                    os.replace(CACHE + '.tmp', CACHE)
                done += len(got); print(f'checked {done}/{len(todo)}', flush=True)
            except Exception as e:
                print('a batch failed (retried next run): ' + str(e)[:200], flush=True)


def apply(papers, irows):
    """The change, in memory: returns what changed."""
    cache = json.load(open(CACHE))
    today = datetime.date.today().isoformat()
    names = {i['interest_name'] for i in irows}
    for n in NEW:
        if n['interest_name'] not in names:
            irows.append({**n, 'relevance_mapping': 'probably', 'status': 'confirmed', 'discovered_from': f'Split from "{OLD}" (manual)', 'date_added': today, 'date_updated': today})
    for i in irows:
        if i['interest_name'] == OLD:
            i['status'] = 'rejected'; i['date_updated'] = today
            i['notes'] = f'Split on {today} into: ' + ', '.join(n['interest_name'] for n in NEW) + ' (and the existing evaluation interests).'
    added, removed = {}, 0
    for p in papers:
        mi = [x for x in (p.get('matched_interests') or '').split('|') if x]
        st = rm.parse_strengths(p.get('match_strengths'))
        if OLD in mi: mi.remove(OLD); st.pop(OLD, None); removed += 1
        for k, v in (cache.get(p['id']) or {}).items():
            if v == 'touches': continue   # a passing appearance isn't a match: the narrow interests stay meaningful
            if k not in mi: mi.append(k); added[k] = added.get(k, 0) + 1
            st.setdefault(k, v)   # a strength already there stays
        p['matched_interests'] = '|'.join(mi)
        p['match_strengths'] = rm.format_strengths(st, mi)
    return added, removed


def score(p, levels):   # the dashboard's score (index.html SCORING), for the report
    S = {'about': 1, 'partly': 0.6, 'touches': 0.25}
    st = rm.parse_strengths(p.get('match_strengths'))
    ms = []
    for n in [x for x in (p.get('matched_interests') or '').split('|') if x in levels]:
        lv, s = levels[n], S[st.get(n, 'partly')]
        ms.append((3 * s if lv == 'definitely' else (1 if lv == 'probably' else 1 / 3) * s, lv == 'definitely'))
    if not ms: return {'definitely': 3, 'probably': 1, 'mildly': 0.25}.get(p.get('relevance_tier'), 0.25)
    core = max([v for v, c in ms if c] or [0]); ctx = max([v for v, c in ms if not c] or [0])
    rest = sorted([v for v, c in ms], reverse=True)
    for used in (core, ctx):
        if used in rest: rest.remove(used)
    bonus = min(0.2, 0.2 * rest[0]) if rest else 0
    return core + ctx + bonus + 0.5 * max(-1, min(1, int(p.get('residual_score') or 0)))


def label(sc): return 'Must Read' if sc >= 2.5 else 'Interesting' if sc >= 0.6 else 'Tangential'


def report():
    import copy, collections
    ap.fresh_clone()
    fields, papers = ap.read_papers()
    with open(os.path.join(CLONE, INTERESTS), newline='', encoding='utf-8') as f: irows = list(csv.DictReader(f))
    lv0 = {i['interest_name']: i['relevance_mapping'] for i in irows if i['status'] == 'confirmed'}
    before = {p['id']: label(score(p, lv0)) for p in papers if p.get('is_hidden') != 'true'}
    papers2, irows2 = copy.deepcopy(papers), copy.deepcopy(irows)
    added, removed = apply(papers2, irows2)
    lv1 = {i['interest_name']: i['relevance_mapping'] for i in irows2 if i['status'] == 'confirmed'}
    after = {p['id']: label(score(p, lv1)) for p in papers2 if p.get('is_hidden') != 'true'}
    print(f'"{OLD}" removed from {removed} papers')
    for k, v in sorted(added.items(), key=lambda x: -x[1]): print(f'  + {k}: {v} papers')
    print('labels before:', dict(collections.Counter(before.values())))
    print('labels after: ', dict(collections.Counter(after.values())))
    ch = collections.Counter((before[i], after[i]) for i in before if before[i] != after[i])
    print('changes:', dict(ch))
    lost = [p for p in papers2 if p.get('is_hidden') != 'true' and not [x for x in p['matched_interests'].split('|') if x in lv1]]
    print('papers left with no current interest:', len(lost))
    for p in lost[:8]: print('   -', p['title'][:100])


def publish():
    for attempt in range(3):
        ap.fresh_clone()
        fields, papers = ap.read_papers()
        with open(os.path.join(CLONE, INTERESTS), newline='', encoding='utf-8') as f:
            r = csv.DictReader(f); ifields = r.fieldnames; irows = list(r)
        added, removed = apply(papers, irows)
        rm.write(PAPERS, fields, papers); rm.write(INTERESTS, ifields, irows)
        ap.git('add', PAPERS, INTERESTS)
        ap.git('commit', '-q', '-m', f'Split "{OLD}" into ' + ', '.join(n['interest_name'] for n in NEW) + f' ({removed} papers re-tagged)')
        rr = ap.git('push', *(['--dry-run'] if ap.DRY_RUN else []), f'https://x-access-token:{ap.gh_token()}@{ap.REMOTE}', 'HEAD:main', check=False)
        if rr.returncode == 0: return added, removed
        if attempt == 2: raise RuntimeError('push failed: ' + (rr.stderr or '')[-300:].replace(ap.gh_token(), '***'))


if __name__ == '__main__':
    if '--rate' in sys.argv: rate()
    if '--report' in sys.argv: report()
    if '--publish' in sys.argv: print(publish())

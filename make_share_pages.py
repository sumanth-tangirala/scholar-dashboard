#!/usr/bin/env python3
"""Link previews for shared papers.

Chat apps (WhatsApp, iMessage, Slack...) build a link preview from the page's HTML without running scripts, and never
see the part of a URL after '#'. So every '#paper/<id>' link previews as the dashboard's home page. This writes a
tiny static page per paper, p/<id>/index.html, carrying that paper's title, authors and venue for the preview; a
person opening it is sent straight to the paper in the dashboard. The dashboard's Share button hands out these links.

Run by sync_to_github.sh (so digest runs keep them current); safe to run any time. Pages of papers no longer in the
database are removed. Uploaded (private) papers are never in the database, so they never get a page.
"""
import csv, html, os, re, shutil

DIR = os.path.dirname(os.path.abspath(__file__))
SITE = 'https://www.sumanthtangirala.com/scholar-dashboard/'
OUT = os.path.join(DIR, 'p')


def clean(s):
    return re.sub(r'\s+', ' ', (s or '').strip())


def short_authors(a):
    a = clean(a)
    if not a or a.lower() == 'not specified':
        return ''
    names = [n.strip() for n in a.split(',') if n.strip()]
    return ', '.join(names[:3]) + (' et al.' if len(names) > 3 else '')


def venue_line(r):
    venue, date = clean(r.get('venue')), clean(r.get('publishing_date'))
    if venue.lower() == 'not specified':
        venue = ''
    year = re.search(r'\d{4}', date)
    if venue and date and not (year and year.group(0) in venue):
        return f'{venue}, {date}'
    return venue or date


def page(r):
    pid, title = r['id'], clean(r.get('title')) or r['id']
    desc = ' · '.join(x for x in (short_authors(r.get('authors')), venue_line(r)) if x)
    e = lambda s: html.escape(s, quote=True)
    url = f'{SITE}p/{pid}/'
    target = f'../../#paper/{pid}'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Scholar Dashboard">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE}icon-512.png">
<meta name="twitter:card" content="summary">
<meta name="robots" content="noindex">
<link rel="canonical" href="{url}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>location.replace('../../' + (location.hash || '#paper/{pid}'));</script>
<style>body{{background:#111;color:#e8e8e8;font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:40px 24px}}a{{color:#fbb13c}}</style>
</head><body><p><a href="{e(target)}">{e(title)}</a></p><p>{e(desc)}</p></body></html>
'''


def main():
    rows = list(csv.DictReader(open(os.path.join(DIR, 'papers_database.csv'), newline='', encoding='utf-8')))
    keep = set()
    os.makedirs(OUT, exist_ok=True)
    for r in rows:
        pid = r.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', pid):
            continue
        keep.add(pid)
        d = os.path.join(OUT, pid)
        os.makedirs(d, exist_ok=True)
        text, f = page(r), os.path.join(d, 'index.html')
        if not os.path.exists(f) or open(f, encoding='utf-8').read() != text:
            with open(f, 'w', encoding='utf-8') as fh:
                fh.write(text)
    for name in os.listdir(OUT):
        if name not in keep and os.path.isdir(os.path.join(OUT, name)):
            shutil.rmtree(os.path.join(OUT, name))
    print(f'share pages: {len(keep)}')


if __name__ == '__main__':
    main()

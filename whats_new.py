"""
In-app "What's new" (TODO #83): the finished items of TODO.md as release
notes, newest section first. The Docker image has no .git, but TODO.md is
copied in, and every finished feature is recorded there as
`- [x] N. **Title.** text` (continuation lines indented).

`notes()` -> {version, sections: [{title, items: [{number, title, text}]}]}.
Parsed once per process (the file only changes with a new build).
"""

import os
import re

import version

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'TODO.md')
ITEM_RE = re.compile(r'^- \[x\] (\d+)\.\s+(.*)$')
TITLE_RE = re.compile(r'^\*\*(.+?)\*\*\s*(.*)$')

_cache = None


def parse(text):
    sections, current, item = [], None, None
    for line in text.splitlines():
        if line.startswith('## '):
            current = {'title': line[3:].strip(), 'items': []}
            sections.append(current)
            item = None
            continue
        m = ITEM_RE.match(line)
        if m and current is not None:
            body = m.group(2).strip()
            t = TITLE_RE.match(body)
            title, rest = (t.group(1).strip().rstrip('.'), t.group(2)) if t else (body[:80], body)
            item = {'number': int(m.group(1)), 'title': title, 'text': rest.strip()}
            current['items'].append(item)
            continue
        if item is not None and line.startswith('      ') and line.strip():
            item['text'] = (item['text'] + ' ' + line.strip()).strip()
        else:
            item = None
    return [s for s in reversed(sections) if s['items']]


def notes():
    global _cache
    if _cache is None:
        try:
            with open(PATH, encoding='utf-8') as fh:
                sections = parse(fh.read())
        except OSError:
            sections = []
        _cache = {'version': version.get(), 'sections': sections}
    return _cache

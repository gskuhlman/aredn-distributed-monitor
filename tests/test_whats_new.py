"""What's new (whats_new.py): finished TODO items, newest section first."""

import whats_new

TEXT = """# TODO
## Round 1
- [x] 1. **First thing.** Did it,
      over two lines.
- [ ] 2. **Not yet.** Open.
## Pending radio work
- [ ] Move a radio.
## Done since Round 1 (2026-11-01)
- [x] 3. **Later thing** with `code`.
"""


def test_parse_done_items_newest_first():
    s = whats_new.parse(TEXT)
    assert [x['title'] for x in s] == ['Done since Round 1 (2026-11-01)', 'Round 1']
    assert s[1]['items'] == [{'number': 1, 'title': 'First thing', 'text': 'Did it, over two lines.'}]
    assert s[0]['items'][0]['number'] == 3 and s[0]['items'][0]['title'] == 'Later thing'


def test_real_todo_parses():
    n = whats_new.notes()
    assert n['sections'] and all(i['number'] for s in n['sections'] for i in s['items'])

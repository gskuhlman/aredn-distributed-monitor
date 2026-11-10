"""
Suppressions with a review date (TODO #87). Muting alerts for a node or one
link needs a reason and a date to look at it again ("KF9MT dish re-aim due
after the snow; review 2026-12-01"). While active, notify.py logs matching
events as suppressed (`covering()`) instead of sending them; once the review
date passes the suppression keeps working but shows as overdue (header banner
on the main page, Notifications page) until someone reviews it (new date),
or ends it.

SQLite `suppressions`: node, optional peer (a link, either direction),
event types (empty = all), reason, review date, author, created / ended /
last reviewed. Local operator data, never replicated.
"""

import json
from datetime import date, datetime

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
MAX_REASON = 1000

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS suppressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_name TEXT NOT NULL, peer_node TEXT, event_types TEXT,
            reason TEXT NOT NULL, review_date TEXT NOT NULL, author TEXT,
            created_at DATETIME NOT NULL, reviewed_at DATETIME, ended_at DATETIME, end_note TEXT)''')
    _tables_ready = True


def _now(now=None):
    return (now or datetime.now()).strftime(TS_FMT)


def _date(value):
    try:
        return datetime.strptime(str(value or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('Review date must be YYYY-MM-DD')


def _row(r, today=None):
    d = dict(r)
    try:
        d['event_types'] = json.loads(d.get('event_types') or '[]')
    except ValueError:
        d['event_types'] = []
    today = today or date.today()
    d['active'] = not d.get('ended_at')
    d['overdue'] = d['active'] and _date(d['review_date']) < today
    d['days_to_review'] = (_date(d['review_date']) - today).days
    return d


def create(data, now=None):
    ensure_tables()
    data = data or {}
    node = str(data.get('node_name') or '').strip().lower()
    if not node:
        raise ValueError('Pick a node')
    reason = str(data.get('reason') or '').strip()[:MAX_REASON]
    if not reason:
        raise ValueError('A reason is required')
    review = _date(data.get('review_date'))
    if review < (now or datetime.now()).date():
        raise ValueError('The review date is in the past')
    peer = str(data.get('peer_node') or '').strip().lower() or None
    types = sorted({str(t) for t in (data.get('event_types') or []) if str(t).strip()})
    with database.get_connection() as conn:
        cur = conn.execute('''INSERT INTO suppressions (node_name, peer_node, event_types, reason, review_date, author,
                              created_at) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                           (node, peer, json.dumps(types), reason, review.isoformat(), data.get('author'), _now(now)))
        sid = cur.lastrowid
    return get(sid)


def get(sid):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM suppressions WHERE id = ?', (int(sid),)).fetchone()
    return _row(r) if r else None


def list_all(include_ended=False):
    ensure_tables()
    sql = 'SELECT * FROM suppressions' + ('' if include_ended else ' WHERE ended_at IS NULL') + ' ORDER BY review_date, id'
    with database.get_connection() as conn:
        rows = [_row(r) for r in conn.execute(sql)]
    return {'suppressions': rows, 'overdue': sum(1 for r in rows if r['overdue']),
            'active': sum(1 for r in rows if r['active'])}


def overdue():
    return [r for r in list_all()['suppressions'] if r['overdue']]


def review(sid, review_date, note=None, now=None):
    """Keep it: a new review date (and optionally a new reason)."""
    ensure_tables()
    cur = get(sid)
    if not cur or not cur['active']:
        raise LookupError('No active suppression with that id')
    d = _date(review_date)
    if d < (now or datetime.now()).date():
        raise ValueError('The review date is in the past')
    with database.get_connection() as conn:
        conn.execute('UPDATE suppressions SET review_date = ?, reviewed_at = ?, reason = COALESCE(?, reason) WHERE id = ?',
                     (d.isoformat(), _now(now), (str(note).strip()[:MAX_REASON] or None) if note else None, int(sid)))
    return get(sid)


def end(sid, note=None, now=None):
    ensure_tables()
    with database.get_connection() as conn:
        n = conn.execute('UPDATE suppressions SET ended_at = ?, end_note = ? WHERE id = ? AND ended_at IS NULL',
                         (_now(now), (str(note).strip()[:MAX_REASON] if note else None), int(sid))).rowcount
    if not n:
        raise LookupError('No active suppression with that id')
    return get(sid)


def matches(s, node, peer, event_type):
    if s['event_types'] and event_type not in s['event_types']:
        return False
    if s['peer_node']:
        return {node, peer} == {s['node_name'], s['peer_node']}
    return s['node_name'] in (node, peer)


def covering(event):
    """'suppressed: <reason> (review <date>)' when an active suppression
    covers the event, else None."""
    try:
        import notify
        node = (event.get('node_name') or '').lower()
        _, peer = notify.event_subject(event)
        for s in list_all()['suppressions']:
            if matches(s, node, peer, event.get('event_type')):
                return f"suppressed #{s['id']}: {s['reason'][:80]} (review {s['review_date']})"
    except Exception:
        pass
    return None

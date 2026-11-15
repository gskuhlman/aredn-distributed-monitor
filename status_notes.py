"""
Status notes (TODO #101): a pinned operator message such as "Collins tower
work Saturday 9-12", shown on the Status page, the wall display (/wall) and
the club status page (/export/club-status), from `starts_at` until `ends_at`
(open-ended when empty). Separate from maintenance windows, which also hold
alarms; a note only tells people. Admins add and remove them.

SQLite `status_notes`. Local operator data, never replicated.
"""

from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
MAX_TEXT = 500
LEVELS = ('info', 'warning')

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS status_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                starts_at DATETIME NOT NULL,
                ends_at DATETIME,
                created_by TEXT,
                created_at DATETIME NOT NULL
            )
        ''')
    _tables_ready = True


def _parse(value, field):
    if value in (None, ''):
        return None
    text = str(value).strip().replace('T', ' ')
    for fmt in (TS_FMT, '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f'{field} must be YYYY-MM-DD HH:MM')


def add(text, starts_at=None, ends_at=None, level='info', by=None, now=None):
    """Returns (note, error)."""
    ensure_tables()
    now = now or datetime.now()
    text = ' '.join(str(text or '').split())
    if not text:
        return None, 'Write the note'
    if len(text) > MAX_TEXT:
        return None, f'Keep it under {MAX_TEXT} characters'
    if level not in LEVELS:
        return None, 'Unknown level'
    try:
        start = _parse(starts_at, 'Start') or now
        end = _parse(ends_at, 'End')
    except ValueError as exc:
        return None, str(exc)
    if end and end <= start:
        return None, 'The end must be after the start'
    if end and end < now:
        return None, 'That end time has already passed'
    if end and end - start > timedelta(days=90):
        return None, 'A note can run at most 90 days'
    with database.get_connection() as conn:
        cur = conn.execute('''INSERT INTO status_notes (text, level, starts_at, ends_at, created_by, created_at)
                              VALUES (?, ?, ?, ?, ?, ?)''',
                           (text, level, start.strftime(TS_FMT), end.strftime(TS_FMT) if end else None, by,
                            now.strftime(TS_FMT)))
        r = conn.execute('SELECT * FROM status_notes WHERE id = ?', (cur.lastrowid,)).fetchone()
    return dict(r), None


def active(now=None):
    """Notes showing now, newest first."""
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute('''SELECT * FROM status_notes WHERE starts_at <= ?
                                                 AND (ends_at IS NULL OR ends_at > ?) ORDER BY starts_at DESC''',
                                              (now_s, now_s))]


def upcoming_and_active(now=None):
    """Active plus scheduled notes (for the editor)."""
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT * FROM status_notes WHERE ends_at IS NULL OR ends_at > ?
                                                 ORDER BY starts_at''', (now_s,))]
    for r in rows:
        r['showing'] = r['starts_at'] <= now_s
    return rows


def delete(note_id):
    ensure_tables()
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM status_notes WHERE id = ?', (int(note_id),)).rowcount > 0

"""
Operator notes on nodes, links and sites ("tilted 2° on 10/20", "call KF9MT
first"), shown wherever people troubleshoot: node page, Nodes detail panel,
network side panel, RF Stats link diagnosis, Channel Map radio panel, and as
chart markers when a note has an event_time.

Local operator data in the SQLite `notes` table (like change_log and
maintenance windows); never replicated to CouchDB. Note text is free text and
is always escaped by the UI.
"""

from datetime import datetime

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
TARGET_TYPES = ('node', 'link', 'site')
MAX_TEXT = 2000

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                node_name TEXT,
                peer_node TEXT,
                site TEXT,
                text TEXT NOT NULL,
                author TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                event_time DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notes_node ON notes(node_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notes_peer ON notes(peer_node)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notes_site ON notes(site)')
    _tables_ready = True


def _now():
    return datetime.now().strftime(TS_FMT)


def _parse_ts(value):
    if not value:
        return None
    value = str(value).strip().replace('T', ' ')
    for fmt in (TS_FMT, '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value[:19] if fmt == TS_FMT else value, fmt)
        except ValueError:
            continue
    return None


def node_site(name):
    """Site code of an infrastructure node, else None (non-infrastructure
    names like kf9mt-home-... don't follow the CALL-SITE convention)."""
    return database.get_infrastructure_nodes().get((name or '').lower()) or None


def _row(r):
    d = dict(r)
    d['pinned'] = bool(d.get('pinned'))
    return d


def get_note(note_id):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
        return _row(r) if r else None


def list_notes(node=None, peer=None, site=None, pinned=None, limit=200):
    """Notes newest first (pinned first). With node: the node's own notes,
    notes on any of its links and on its site. With node and peer: the pair's
    link notes plus both nodes' and both sites' notes. With site only: that
    site's notes. Each note gets `relation` (node / link / site) relative to
    the query."""
    ensure_tables()
    node = (node or '').strip().lower() or None
    peer = (peer or '').strip().lower() or None
    site = (site or '').strip().upper() or None
    where, params = [], []
    if node:
        names = [n for n in (node, peer) if n]
        sites = {s for s in (node_site(n) for n in names) if s}
        if site:
            sites.add(site)
        ph = ','.join('?' * len(names))
        clauses = [f"(target_type = 'node' AND node_name IN ({ph}))"]
        params += names
        if peer:
            a, b = sorted((node, peer))
            clauses.append("(target_type = 'link' AND node_name = ? AND peer_node = ?)")
            params += [a, b]
        else:
            clauses.append("(target_type = 'link' AND (node_name = ? OR peer_node = ?))")
            params += [node, node]
        if sites:
            clauses.append(f"(target_type = 'site' AND site IN ({','.join('?' * len(sites))}))")
            params += sorted(sites)
        where.append('(' + ' OR '.join(clauses) + ')')
    elif site:
        where.append("target_type = 'site' AND site = ?")
        params.append(site)
    if pinned is not None:
        where.append('pinned = ?')
        params.append(1 if pinned else 0)
    sql = 'SELECT * FROM notes'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY pinned DESC, COALESCE(event_time, created_at) DESC, id DESC LIMIT ?'
    params.append(max(1, min(2000, int(limit or 200))))
    with database.get_connection() as conn:
        rows = [_row(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['relation'] = r['target_type']
    return rows


def _clean(data, partial=False):
    """Validated column values from a POST/PATCH body. Raises ValueError."""
    out = {}
    if not partial or 'text' in data:
        text = (data.get('text') or '').strip()
        if not text:
            raise ValueError('text is required')
        if len(text) > MAX_TEXT:
            raise ValueError(f'text is longer than {MAX_TEXT} characters')
        out['text'] = text
    if 'author' in data:
        out['author'] = (data.get('author') or '').strip()[:100] or None
    if 'pinned' in data:
        out['pinned'] = 1 if data.get('pinned') else 0
    if 'event_time' in data:
        raw = data.get('event_time')
        if raw:
            when = _parse_ts(raw)
            if not when:
                raise ValueError('event_time must look like YYYY-MM-DD HH:MM')
            out['event_time'] = when.strftime(TS_FMT)
        else:
            out['event_time'] = None
    return out


def add_note(data):
    """Create a note: {target_type?, node_name?, peer_node?, site?, text,
    author?, pinned?, event_time?}. target_type defaults to link when a peer
    is given, site when only a site is given, else node."""
    ensure_tables()
    node = (data.get('node_name') or '').strip().lower() or None
    peer = (data.get('peer_node') or '').strip().lower() or None
    site = (data.get('site') or '').strip().upper() or None
    target = (data.get('target_type') or ('link' if peer else 'site' if site and not node else 'node')).lower()
    if target not in TARGET_TYPES:
        raise ValueError(f'target_type must be one of {", ".join(TARGET_TYPES)}')
    if target == 'node':
        if not node:
            raise ValueError('node_name is required')
        peer = site = None
    elif target == 'link':
        if not node or not peer or node == peer:
            raise ValueError('a link note needs node_name and a different peer_node')
        node, peer = sorted((node, peer))
        site = None
    else:
        site = site or node_site(node)
        if not site:
            raise ValueError('site is required (or a node_name of an infrastructure node)')
        node = peer = None
    values = _clean(data)
    now = _now()
    with database.get_connection() as conn:
        cur = conn.execute('''
            INSERT INTO notes (target_type, node_name, peer_node, site, text, author, pinned,
                               event_time, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (target, node, peer, site, values['text'], values.get('author'), values.get('pinned', 0),
              values.get('event_time'), now, now))
        note_id = cur.lastrowid
    return get_note(note_id)


def update_note(note_id, data):
    """Edit text / pinned / event_time / author. Returns the note or None."""
    ensure_tables()
    if not get_note(note_id):
        return None
    values = _clean(data, partial=True)
    if values:
        values['updated_at'] = _now()
        cols = ', '.join(f'{k} = ?' for k in values)
        with database.get_connection() as conn:
            conn.execute(f'UPDATE notes SET {cols} WHERE id = ?', [*values.values(), note_id])
    return get_note(note_id)


def delete_note(note_id):
    ensure_tables()
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM notes WHERE id = ?', (note_id,)).rowcount > 0

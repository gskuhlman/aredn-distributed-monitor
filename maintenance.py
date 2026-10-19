"""
Maintenance windows (#22): planned work that alarms and reports should ignore.

A window has a start, an end and a scope:

- all:   the whole mesh (every node and system event, e.g. gateway_lost)
- site:  every node whose site is the given code (infrastructure site, else
         the location code in the name: w0gq-col-rkt-s -> COL)
- nodes: an explicit node list

With ``include_rf_peers`` (the default for site/nodes scope) the nodes with a
current RF link to a covered node are covered too, so a sector's clients that
drop while the sector is re-channeled don't alarm. A link is in maintenance
when either end is. Each window's effective end is extended by
MAINTENANCE_GRACE_MINUTES for alarm purposes, so radios still rejoining after
the work don't fire right after the window closes.

Used by notify.is_suppressed (no notifications), link_watch (no one-way/noise
alarms, noise baseline restarts after the window), unreachable/reports (failed
polls inside a window are explained, not counted). Local SQLite only; times
are local 'YYYY-MM-DD HH:MM:SS' like the rest of the app.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

import config
import database

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
LINK_DETAIL_RE = re.compile(r'^[\w-]+ link to (?P<peer>\S+)')   # same shape notify.py matches
SCOPES = ('all', 'site', 'nodes')
MAX_HOURS = 7 * 24   # a single window may not run longer than a week

_schema_ready = False


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'nodes',
                site TEXT,
                nodes TEXT,
                include_rf_peers INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                author TEXT,
                change_id INTEGER,
                created_at TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_maintenance_time '
                     'ON maintenance_windows(start_time, end_time)')
    _schema_ready = True


def _now():
    return datetime.now()


def _fmt(dt):
    return dt.strftime(TS_FMT)


def _parse_ts(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    value = str(value).strip().replace('T', ' ')
    for fmt, n in ((TS_FMT, 19), ('%Y-%m-%d %H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.strptime(value[:n], fmt)
        except ValueError:
            continue
    return None


def grace():
    return timedelta(minutes=config.MAINTENANCE_GRACE_MINUTES)


# ------------------------------------------------------------- windows -----

def _row(r):
    w = dict(r)
    try:
        w['nodes'] = json.loads(w.get('nodes') or '[]')
    except (TypeError, ValueError):
        w['nodes'] = []
    w['include_rf_peers'] = bool(w.get('include_rf_peers'))
    w['effective_end'] = _fmt(_parse_ts(w['end_time']) + grace())
    return w


def _status(w, now):
    start, end = _parse_ts(w['start_time']), _parse_ts(w['end_time'])
    if now < start:
        return 'upcoming'
    if now < end:
        return 'active'
    if now < end + grace():
        return 'grace'
    return 'past'


def list_windows(days=14, now=None):
    """Windows that end after now - days, newest first, with status."""
    ensure_schema()
    now = now or _now()
    since = _fmt(now - timedelta(days=days))
    with database.get_connection() as conn:
        rows = conn.execute('SELECT * FROM maintenance_windows WHERE end_time >= ? '
                            'ORDER BY start_time DESC, id DESC', (since,)).fetchall()
    out = []
    for r in rows:
        w = _row(r)
        w['status'] = _status(w, now)
        w['covered_nodes'] = sorted(covered_nodes(w))
        out.append(w)
    return out


def get_window(window_id):
    ensure_schema()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM maintenance_windows WHERE id = ?', (int(window_id),)).fetchone()
    if not r:
        return None
    w = _row(r)
    w['status'] = _status(w, _now())
    w['covered_nodes'] = sorted(covered_nodes(w))
    return w


def windows_between(start, end, conn=None):
    """Windows whose [start, effective end) overlaps [start, end) (strings or datetimes)."""
    ensure_schema()
    s = _fmt(_parse_ts(start) - grace())
    e = _fmt(_parse_ts(end))
    sql = 'SELECT * FROM maintenance_windows WHERE start_time < ? AND end_time > ? ORDER BY start_time'
    if conn is not None:
        return [_row(r) for r in conn.execute(sql, (e, s)).fetchall()]
    with database.get_connection() as c:
        return [_row(r) for r in c.execute(sql, (e, s)).fetchall()]


def active_windows(now=None, include_grace=True):
    """Windows covering now (by default including the grace period)."""
    now = now or _now()
    out = []
    for w in windows_between(now, now + timedelta(seconds=1)):
        w['status'] = _status(w, now)
        if w['status'] == 'active' or (include_grace and w['status'] == 'grace'):
            w['covered_nodes'] = sorted(covered_nodes(w))
            out.append(w)
    return out


def _clean_nodes(value):
    if isinstance(value, str):
        value = value.replace(',', ' ').split()
    return sorted({str(n).strip().lower() for n in (value or []) if str(n).strip()})


def create_window(data, now=None):
    """Create a window from {scope, site?, nodes?, start?, end? | duration_minutes?,
    note?, author?, include_rf_peers?, change_log?: bool}. Start defaults to now."""
    ensure_schema()
    now = now or _now()
    scope = (data.get('scope') or 'nodes').lower()
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    site = (data.get('site') or '').strip().upper() or None
    nodes = _clean_nodes(data.get('nodes'))
    if scope == 'site' and not site:
        raise ValueError('site is required for site scope')
    if scope == 'nodes' and not nodes:
        raise ValueError('at least one node is required for node scope')
    start = _parse_ts(data.get('start')) if data.get('start') else now.replace(microsecond=0)
    if start is None:
        raise ValueError('start must look like YYYY-MM-DD HH:MM')
    if data.get('end'):
        end = _parse_ts(data.get('end'))
        if end is None:
            raise ValueError('end must look like YYYY-MM-DD HH:MM')
    else:
        try:
            minutes = int(data.get('duration_minutes') or 60)
        except (TypeError, ValueError):
            raise ValueError('duration_minutes must be a number')
        end = start + timedelta(minutes=minutes)
    if end <= start:
        raise ValueError('end must be after start')
    if end - start > timedelta(hours=MAX_HOURS):
        raise ValueError(f'a window may not be longer than {MAX_HOURS // 24} days')
    include_peers = data.get('include_rf_peers')
    include_peers = scope != 'all' if include_peers is None else bool(include_peers)
    note = (data.get('note') or '').strip() or None
    author = (data.get('author') or '').strip() or None

    change_id = None
    if data.get('change_log'):
        import change_log
        target = nodes[0] if nodes else None
        if scope == 'site':
            target = next(iter(sorted(n for n in covered_nodes(
                {'scope': 'site', 'site': site, 'nodes': [], 'include_rf_peers': False}))), None)
        if target:
            what = (f"site {site}" if scope == 'site' else
                    'whole mesh' if scope == 'all' else ', '.join(nodes))
            change = change_log.add_change(
                target, f"Maintenance ({what}): {note or 'planned work'}", 'other',
                _fmt(start), None, author)
            change_id = change.get('id') if change else None

    with database.get_connection() as conn:
        cur = conn.execute('''
            INSERT INTO maintenance_windows
                (start_time, end_time, scope, site, nodes, include_rf_peers, note, author, change_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (_fmt(start), _fmt(end), scope, site if scope == 'site' else None,
              json.dumps(nodes if scope == 'nodes' else []), 1 if include_peers else 0,
              note, author, change_id, _fmt(now)))
        window_id = cur.lastrowid
    _cache.clear()
    database.log_event(database.EVENT_MAINTENANCE_STARTED if start <= now
                       else database.EVENT_MAINTENANCE_SCHEDULED, None,
                       f"Maintenance window #{window_id} ({_describe_scope(scope, site, nodes)}) "
                       f"{_fmt(start)[:16]} to {_fmt(end)[:16]}" + (f": {note}" if note else ''), 'info')
    return get_window(window_id)


def update_window(window_id, data, now=None):
    """{end_now: true} | {extend_minutes: N} | {end: ts} | {note: str}."""
    now = now or _now()
    w = get_window(window_id)
    if not w:
        return None
    start, end = _parse_ts(w['start_time']), _parse_ts(w['end_time'])
    if data.get('end_now'):
        end = max(start, now.replace(microsecond=0))
    elif data.get('extend_minutes'):
        try:
            end = max(end, now) + timedelta(minutes=int(data['extend_minutes']))
        except (TypeError, ValueError):
            raise ValueError('extend_minutes must be a number')
    elif data.get('end'):
        end = _parse_ts(data['end'])
        if end is None or end <= start:
            raise ValueError('end must be a time after the start')
    if end - start > timedelta(hours=MAX_HOURS):
        raise ValueError(f'a window may not be longer than {MAX_HOURS // 24} days')
    note = data['note'].strip() or None if isinstance(data.get('note'), str) else w['note']
    with database.get_connection() as conn:
        conn.execute('UPDATE maintenance_windows SET end_time = ?, note = ? WHERE id = ?',
                     (_fmt(end), note, int(window_id)))
    _cache.clear()
    if data.get('end_now'):
        database.log_event(database.EVENT_MAINTENANCE_ENDED, None,
                           f"Maintenance window #{window_id} ({_describe_scope(w['scope'], w['site'], w['nodes'])}) "
                           f"ended at {_fmt(end)[:16]}", 'info')
    return get_window(window_id)


def delete_window(window_id):
    ensure_schema()
    with database.get_connection() as conn:
        cur = conn.execute('DELETE FROM maintenance_windows WHERE id = ?', (int(window_id),))
    _cache.clear()
    return cur.rowcount > 0


def _describe_scope(scope, site, nodes):
    if scope == 'all':
        return 'whole mesh'
    if scope == 'site':
        return f'site {site}'
    return ', '.join(nodes[:4]) + (f' +{len(nodes) - 4}' if len(nodes) > 4 else '')


# -------------------------------------------------------------- coverage ---

_cache = {}


def _cached(key, loader, ttl=60):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = loader()
    _cache[key] = (time.time(), value)
    return value


def _site_map():
    """{node: site code} for every known node: the infrastructure site, else
    the location code in the name."""
    def load():
        infra = database.get_infrastructure_nodes()
        with database.get_connection() as conn:
            names = [r[0] for r in conn.execute('SELECT name FROM nodes')]
        out = {n: (infra.get(n) or database.site_code_from_name(n)) for n in names}
        out.update({n: s or database.site_code_from_name(n) for n, s in infra.items()})
        return out
    return _cached('sites', load)


def _rf_peers():
    """{node: set(peers)} from current RF links."""
    def load():
        peers = {}
        with database.get_connection() as conn:
            for r in conn.execute('''
                SELECT source_node, target_node FROM links
                WHERE UPPER(link_type) = 'RF' AND status != 'removed'
            '''):
                peers.setdefault(r[0], set()).add(r[1])
                peers.setdefault(r[1], set()).add(r[0])
        return peers
    return _cached('peers', load)


def site_of(node):
    node = (node or '').lower()
    return _site_map().get(node) or database.site_code_from_name(node)


def covered_nodes(w):
    """Explicit node set a window covers (empty for scope 'all' = everything)."""
    if w['scope'] == 'all':
        return set()
    if w['scope'] == 'site':
        base = {n for n, s in _site_map().items() if s == w['site']}
    else:
        base = set(w['nodes'])
    if w.get('include_rf_peers'):
        peers = _rf_peers()
        base = base | {p for n in base for p in peers.get(n, ())}
    return base


def _node_covered(w, node):
    if w['scope'] == 'all':
        return True
    if not node:
        return False
    node = node.lower()
    if w['scope'] == 'site' and site_of(node) == w['site']:
        return True
    if w['scope'] == 'nodes' and node in w['nodes']:
        return True
    if w.get('include_rf_peers'):
        key = ('covered', w['id'])
        nodes = _cached(key, lambda: covered_nodes(w))
        return node in nodes
    return False


def _in_time(w, at, use_grace=True):
    start = _parse_ts(w['start_time'])
    end = _parse_ts(w['end_time']) + (grace() if use_grace else timedelta(0))
    return start <= at < end


def covering_window(node=None, at=None, peer=None, windows=None, use_grace=True):
    """The first window covering `node` (or link node<->peer) at `at`, else None.
    With node None only scope 'all' windows match (system events)."""
    at = _parse_ts(at) or _now()
    if windows is None:
        windows = windows_between(at, at + timedelta(seconds=1))
    for w in windows:
        if not _in_time(w, at, use_grace):
            continue
        if w['scope'] == 'all' or _node_covered(w, node) or (peer and _node_covered(w, peer)):
            return w
    return None


def is_in_maintenance(node_name=None, at=None, peer=None):
    """True when the node (or the link node<->peer) is in a maintenance window
    at `at` (default now), including the grace period after it."""
    return covering_window(node_name, at, peer) is not None


def intervals_for(node, start, end, windows=None):
    """[(start_dt, effective_end_dt)] maintenance intervals covering `node`
    that overlap [start, end)."""
    if windows is None:
        windows = windows_between(start, end)
    out = []
    for w in windows:
        if w['scope'] == 'all' or _node_covered(w, node):
            out.append((_parse_ts(w['start_time']), _parse_ts(w['end_time']) + grace()))
    return out


def ts_in_intervals(ts, intervals):
    t = _parse_ts(ts)
    return t is not None and any(a <= t < b for a, b in intervals)


def describe_window(w):
    return (f"maintenance #{w['id']} ({_describe_scope(w['scope'], w.get('site'), w.get('nodes') or [])})"
            + (f": {w['note']}" if w.get('note') else ''))


def event_window(event, windows):
    """The window covering a logged event (its node, or either end of a link
    event whose details start "<type> link to <peer>"), else None."""
    m = LINK_DETAIL_RE.match(event.get('details') or '')
    peer = m.group('peer').lower() if m else None
    node = (event.get('node_name') or '').lower() or None
    return covering_window(node, event.get('timestamp'), peer, windows=windows)


def report_windows(start, end, conn=None):
    """Windows overlapping [start, end) for display (Reports timeline)."""
    out = []
    for w in windows_between(start, end, conn):
        out.append({'id': w['id'], 'start_time': w['start_time'], 'end_time': w['end_time'],
                    'effective_end': w['effective_end'], 'scope': w['scope'], 'site': w['site'],
                    'nodes': w['nodes'], 'note': w['note'], 'label': describe_window(w)})
    return out

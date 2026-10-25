"""
New node welcome (Diagnostics tab "New Nodes", node page banner).

After every committed scan `after_scan()` picks up `node_discovered` events
it hasn't seen yet (the scanner logs them only for nodes with no recent
events, see scanner.should_announce_new_node) and opens a welcome for each:
watched for WELCOME_WATCH_DAYS, with the hygiene checklist (hygiene.py) and a
`new_node_welcome` event (info) whose text lists what the owner should fix,
so a notification rule on that type can greet the new node.

`get(node)` adds, while watched: polls answered, RF links with quality, and a
suggested channel: the channel of the sector it links to, or, for a located
radio with no RF link yet, the channel planner's best channel for that one
radio with everything else fixed (cached once computed).
SQLite `node_welcome`, plus `welcome_meta` (last processed event id).
"""

import json
import logging
from datetime import datetime, timedelta

import config
import database
import hygiene

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS node_welcome (
                node_name TEXT PRIMARY KEY,
                discovered_at DATETIME NOT NULL,
                watch_until DATETIME NOT NULL,
                event_id INTEGER,
                score INTEGER,
                fixes TEXT,
                suggestion TEXT,
                dismissed INTEGER NOT NULL DEFAULT 0
            )
        ''')
        conn.execute('CREATE TABLE IF NOT EXISTS welcome_meta (key TEXT PRIMARY KEY, value TEXT)')
    _tables_ready = True


def _last_id(conn):
    r = conn.execute("SELECT value FROM welcome_meta WHERE key = 'last_event_id'").fetchone()
    if r:
        return int(r[0])
    # First run: start from now, don't welcome every node discovered in the past.
    r = conn.execute('SELECT MAX(id) FROM events').fetchone()
    return r[0] or 0


def after_scan(now=None):
    """Open welcomes for newly discovered nodes. Returns events logged."""
    if not config.WELCOME_ENABLED:
        return []
    ensure_tables()
    now = now or datetime.now()
    with database.get_connection() as conn:
        last = _last_id(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT id, timestamp, node_name, details FROM events WHERE event_type = 'node_discovered' AND id > ? "
            "ORDER BY id", (last,))]
        newest = conn.execute('SELECT MAX(id) FROM events').fetchone()[0] or last
        conn.execute("INSERT OR REPLACE INTO welcome_meta (key, value) VALUES ('last_event_id', ?)", (str(newest),))
    out = []
    for e in rows:
        name = e['node_name']
        if not name or name.startswith('mac-'):
            continue
        try:
            h = hygiene.report(node=name)['nodes']
        except Exception as exc:
            logger.warning('welcome hygiene for %s failed: %s', name, exc)
            h = []
        h = h[0] if h else {'score': None, 'fixes': []}
        until = (now + timedelta(days=config.WELCOME_WATCH_DAYS)).strftime(TS_FMT)
        with database.get_connection() as conn:
            conn.execute('''INSERT OR REPLACE INTO node_welcome (node_name, discovered_at, watch_until, event_id,
                                score, fixes, suggestion, dismissed) VALUES (?, ?, ?, ?, ?, ?, NULL, 0)''',
                         (name, e['timestamp'], until, e['id'], h['score'], json.dumps(h['fixes'])))
        text = (f"Welcome {name}" + (f" ({e['details']})" if e.get('details') else '')
                + (f". Setup score {h['score']}/100" if h['score'] is not None else '')
                + ('. To fix: ' + ' '.join(h['fixes'][:4]) if h['fixes'] else '. Setup looks complete.')
                + f" Watched for {config.WELCOME_WATCH_DAYS} days.")
        database.log_event('new_node_welcome', name, text[:1000], 'info')
        out.append({'timestamp': now.strftime(TS_FMT), 'event_type': 'new_node_welcome', 'node_name': name,
                    'details': text[:1000], 'severity': 'info'})
    return out


def _suggest(conn, name):
    """Channel to use: the sector's it links to, else the planner's pick."""
    node = conn.execute('SELECT * FROM nodes WHERE name = ?', (name,)).fetchone()
    if not node or str(node['rf_status'] or '').lower() == 'off':
        return {'kind': 'none', 'text': 'No radio on this node.'}
    infra = database.get_infrastructure_nodes()
    peers = [r[0] for r in conn.execute('''
        SELECT CASE WHEN source_node = ? THEN target_node ELSE source_node END FROM links
        WHERE UPPER(link_type) = 'RF' AND status = 'good' AND ? IN (source_node, target_node)''', (name, name))]
    sectors = [p for p in peers if p in infra]
    if sectors:
        s = conn.execute('SELECT name, rf_frequency, rf_chanbw FROM nodes WHERE name = ?', (sectors[0],)).fetchone()
        return {'kind': 'sector', 'node': s['name'], 'frequency': s['rf_frequency'], 'chanbw': s['rf_chanbw'],
                'text': f"Stay on {s['name']}'s channel ({s['rf_frequency']}/{s['rf_chanbw']} MHz): it is the "
                        "sector this radio links to."}
    if node['lat'] is None:
        return {'kind': 'none', 'text': 'Set the location first; then a channel can be suggested.'}
    import channel_planner
    r = channel_planner.plan(radios=[name], move_clients=False, top=1, idle_off=False)
    if r.get('error') or not r.get('plans'):
        return {'kind': 'none', 'text': r.get('error') or 'The planner found no channel.'}
    p = r['plans'][0]
    me = next((x for x in p['radios'] if x['name'] == name), None)
    if not me or not me.get('new') or me['new'].get('off'):
        return {'kind': 'none', 'text': 'The planner found no channel.'}
    n = me['new']
    return {'kind': 'planner', 'frequency': n['frequency'], 'chanbw': n['chanbw'], 'channel': n.get('channel'),
            'changed': me.get('changed'),
            'text': f"Channel planner: {n['frequency']}/{n['chanbw']} MHz (ch {n.get('channel')})"
                    + ('' if me.get('changed') else ', its current channel') + '.'}


def _watch_stats(conn, name, since):
    polls = conn.execute('''SELECT COUNT(*), SUM(reachable) FROM node_health_history
                            WHERE node_name = ? AND timestamp >= ?''', (name, since)).fetchone()
    links = [dict(r) for r in conn.execute('''
        SELECT target_node AS peer, link_type, quality, snr, status FROM links
        WHERE source_node = ? AND status != 'removed' ORDER BY link_type, target_node''', (name,))]
    return {'polls': polls[0] or 0, 'answered': polls[1] or 0,
            'answered_pct': round(100.0 * (polls[1] or 0) / polls[0], 1) if polls[0] else None,
            'links': links}


def get(name, suggest=True):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM node_welcome WHERE node_name = ?', (name,)).fetchone()
        if not r:
            return None
        w = dict(r)
        w['fixes'] = json.loads(w.get('fixes') or '[]')
        w['watching'] = not w['dismissed'] and w['watch_until'] >= datetime.now().strftime(TS_FMT)
        w.update(_watch_stats(conn, name, w['discovered_at']))
        if w.get('suggestion'):
            w['suggestion'] = json.loads(w['suggestion'])
        elif suggest:
            try:
                w['suggestion'] = _suggest(conn, name)
            except Exception as exc:
                logger.warning('welcome channel suggestion for %s failed: %s', name, exc)
                w['suggestion'] = {'kind': 'none', 'text': f'Suggestion failed: {type(exc).__name__}'}
            if w['suggestion'].get('kind') != 'none':
                conn.execute('UPDATE node_welcome SET suggestion = ? WHERE node_name = ?',
                             (json.dumps(w['suggestion']), name))
    try:
        h = hygiene.report(node=name)['nodes']
        if h:
            w['score'], w['fixes'] = h[0]['score'], h[0]['fixes']
    except Exception:
        pass
    return w


def list_welcomes(include_past=False):
    ensure_tables()
    now = datetime.now().strftime(TS_FMT)
    with database.get_connection() as conn:
        names = [r[0] for r in conn.execute(
            'SELECT node_name FROM node_welcome ' + ('' if include_past else
                                                     'WHERE dismissed = 0 AND watch_until >= ? ')
            + 'ORDER BY discovered_at DESC LIMIT 100', () if include_past else (now,))]
    return [get(n, suggest=False) for n in names]


def dismiss(name):
    ensure_tables()
    with database.get_connection() as conn:
        return conn.execute('UPDATE node_welcome SET dismissed = 1 WHERE node_name = ?', (name,)).rowcount > 0

"""
Routing errors: nodes the mesh can't route to over a tunnel that is up.

The scanner normally queues only neighbours a polled node lists as routable
(Babel has a route through that link). A tunnel neighbour reported connected
but not routable is also polled when it answered a scanner poll or a
neighbour's mesh probe within UNROUTABLE_ANSWER_SECONDS (scanner.py). When no
polled node lists it routable, it is a routing error: the node is up and the
tunnel is up, but Babel won't route to it, so anything relying on the mesh
route (services, other nodes' paths) can't reach it.

`update()` runs after every committed full scan with the flagged nodes:
`routing_error` (warning) when a node is first flagged, `routing_restored`
(info) when a polled node lists it routable again. A node that is no longer
reported at all is forgotten quietly after FORGET_MINUTES. SQLite
`routing_errors` (one row per node). Mesh Health lists the open ones.
"""

import json
from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
FORGET_MINUTES = 60
EVENT_ROUTING_ERROR = 'routing_error'
EVENT_ROUTING_RESTORED = 'routing_restored'

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS routing_errors (
                node_name TEXT PRIMARY KEY,
                via TEXT,
                link_type TEXT,
                answered INTEGER,
                since DATETIME NOT NULL,
                last_seen DATETIME NOT NULL
            )
        ''')
    _tables_ready = True


def describe(node, via, link_type, answered):
    who = ', '.join(via) or 'its neighbours'
    kind = link_type or 'tunnel'
    if answered:
        return (f"Routing error: {who} report{'s' if len(via) == 1 else ''} a connected {kind} link to {node} "
                f"but no route to it (Babel: not routable). The node answers the scanner, so the node and "
                f"tunnel are up; check the tunnel's routing on both ends.")
    return (f"Routing error: {who} report{'s' if len(via) == 1 else ''} a connected {kind} link to {node} "
            f"but no route to it (Babel: not routable). It answered a neighbour's ping recently but not the "
            f"scanner's poll; check the tunnel's routing on both ends.")


def update(flagged, routable, now=None):
    """Record this scan's routing errors and return the transition events.

    ``flagged`` = {node: {'via': [...], 'link_type', 'answered'}};
    ``routable`` = nodes some polled node listed as routable this scan."""
    ensure_tables()
    now = now or datetime.now()
    ts = now.strftime(TS_FMT)
    events = []
    with database.get_connection() as conn:
        known = {r['node_name']: dict(r) for r in conn.execute('SELECT * FROM routing_errors')}
        for node, info in flagged.items():
            via = sorted(info.get('via') or [])
            if node not in known:
                events.append({'type': EVENT_ROUTING_ERROR, 'node': node, 'severity': 'warning',
                               'details': describe(node, via, info.get('link_type'), info.get('answered'))})
                conn.execute('''INSERT INTO routing_errors (node_name, via, link_type, answered, since, last_seen)
                                VALUES (?, ?, ?, ?, ?, ?)''',
                             (node, json.dumps(via), info.get('link_type'), int(bool(info.get('answered'))), ts, ts))
            else:
                conn.execute('UPDATE routing_errors SET via = ?, link_type = ?, answered = ?, last_seen = ? '
                             'WHERE node_name = ?',
                             (json.dumps(via), info.get('link_type'), int(bool(info.get('answered'))), ts, node))
        forget = (now - timedelta(minutes=FORGET_MINUTES)).strftime(TS_FMT)
        for node, row in known.items():
            if node in flagged:
                continue
            if node in routable:
                events.append({'type': EVENT_ROUTING_RESTORED, 'node': node, 'severity': 'info',
                               'details': f"Routing restored: {node} is routable again (open since {row['since']})."})
                conn.execute('DELETE FROM routing_errors WHERE node_name = ?', (node,))
            elif row['last_seen'] < forget:
                conn.execute('DELETE FROM routing_errors WHERE node_name = ?', (node,))
    return events


def active():
    """Open routing errors, oldest first."""
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM routing_errors ORDER BY since')]
    for r in rows:
        r['via'] = json.loads(r['via'] or '[]')
        r['answered'] = bool(r['answered'])
    return rows

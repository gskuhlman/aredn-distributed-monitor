"""
Tunnel health and dependency (TODO #57, #82, #88).

After every committed scan `record()` stores one sample per current tunnel
tracker (WireGuard / legacy, from the polled end's LQM tracker in `links`):
LQ both ways, LQM RTT, ping quality both ways and ping time, tx packets /
failures, Babel metric, routable, up (status good) -> SQLite `tunnel_samples`
(retention HISTORY_RETENTION_HOURS). `report()` per tunnel (node pair, both
directions pooled): uptime % over the window (link_state_log up/down
transitions, scanner_unreachable ignored, plus the samples), flaps (downs),
RTT median / p90, loss (100 - ping quality), a health % = 50% uptime + 30%
(100 - loss) + 20% RTT score (<= 50 ms 100, >= 300 ms 0), and what depends on
it: polled nodes cut off from the starting node without it (spof graph) and
their advertised services ("25 nodes, 32 services depend on this tunnel").

Dependency per node (#82): `rf` = in an RF/DtD-connected group with an
infrastructure node (stays on the mesh with the internet down), `tunnel` =
reachable only through a tunnel, `off_air` = not polled. `check()` after each
scan raises, with 2 scans in a row: `rf_path_lost` (warning) when an active
node that was `rf` is now reachable only through a tunnel, `rf_path_restored`;
`tunnel_degraded` (warning, node = one end, `Tunnel link to <peer>: ...`)
when a tunnel's health over the last hour falls below TUNNEL_HEALTH_PCT,
`tunnel_restored`. State in SQLite `tunnel_state`; maintenance respected.
"""

import json
from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import median

import config
import database
import path_view

TS_FMT = '%Y-%m-%d %H:%M:%S'
TUNNEL = path_view.TUNNEL_TYPES
WATCH_RUNS = 2

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS tunnel_samples (
            timestamp DATETIME NOT NULL, source_node TEXT NOT NULL, target_node TEXT NOT NULL, link_type TEXT,
            up INTEGER, lq REAL, rev_lq REAL, rtt_ms REAL, ping_quality REAL, rev_ping_quality REAL, ping_ms REAL,
            tx_packets INTEGER, tx_fail INTEGER, babel_metric REAL, routable INTEGER,
            PRIMARY KEY (timestamp, source_node, target_node))''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tunnel_samples_pair ON tunnel_samples(source_node, target_node, timestamp)')
        conn.execute('''CREATE TABLE IF NOT EXISTS tunnel_state (
            kind TEXT NOT NULL, subject TEXT NOT NULL, value TEXT, pending TEXT, count INTEGER DEFAULT 0,
            since DATETIME, PRIMARY KEY (kind, subject))''')
    _tables_ready = True


def _num(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def is_tunnel(link_type):
    return (link_type or '').upper() in TUNNEL


def record(now=None):
    """One sample per current tunnel tracker. Returns the number stored."""
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    cut = ((now or datetime.now()) - timedelta(minutes=30)).strftime(TS_FMT)
    rows = []
    with database.get_connection() as conn:
        for r in conn.execute("SELECT * FROM links WHERE status != 'removed' AND last_seen >= ?", (cut,)):
            if not is_tunnel(r['link_type']):
                continue
            try:
                t = json.loads(r['raw_tracker'] or '{}')
            except ValueError:
                t = {}
            pst = _num(t.get('ping_success_time'))
            rows.append((now_s, r['source_node'], r['target_node'], r['link_type'], 1 if r['status'] == 'good' else 0,
                         _num(t.get('lq', r['quality'])), _num(t.get('rev_lq')), _num(t.get('rtt')),
                         _num(t.get('ping_quality')), _num(t.get('rev_ping_quality')),
                         round(pst * 1000, 1) if pst is not None else None,
                         t.get('tx_packets'), t.get('tx_fail'), _num(t.get('babel_metric')),
                         None if t.get('routable') is None else (1 if t.get('routable') else 0)))
        conn.executemany('INSERT OR REPLACE INTO tunnel_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
        conn.execute('DELETE FROM tunnel_samples WHERE timestamp < ?',
                     (((now or datetime.now()) - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT),))
    return len(rows)


def _pctl(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))], 1)


def uptime_pct(states, start, end, initial=None):
    """% of [start, end] a tunnel was up from (timestamp, 'up'|'down') rows
    (chronological; others ignored) and the state before start."""
    t0, t1 = datetime.strptime(start, TS_FMT), datetime.strptime(end, TS_FMT)
    total = (t1 - t0).total_seconds()
    if total <= 0:
        return None
    cur, at, up = initial, t0, 0.0
    known = 0.0
    for ts, st in states:
        if st not in ('up', 'down'):
            continue
        t = max(t0, min(t1, datetime.strptime(ts[:19], TS_FMT)))
        if cur is not None:
            known += (t - at).total_seconds()
            if cur == 'up':
                up += (t - at).total_seconds()
        cur, at = st, t
    if cur is not None:
        known += (t1 - at).total_seconds()
        if cur == 'up':
            up += (t1 - at).total_seconds()
    return round(100 * up / known, 1) if known > 0 else None


def health_pct(uptime, loss, rtt):
    parts, weights = [], []
    if uptime is not None:
        parts.append(uptime * 0.5)
        weights.append(0.5)
    if loss is not None:
        parts.append(max(0.0, 100 - loss) * 0.3)
        weights.append(0.3)
    if rtt is not None:
        score = 100.0 if rtt <= 50 else 0.0 if rtt >= 300 else 100 * (300 - rtt) / 250
        parts.append(score * 0.2)
        weights.append(0.2)
    return round(sum(parts) / sum(weights), 1) if weights else None


def graph():
    """(seed, adjacency with types, polled set, by_name) over usable links in scope."""
    import mesh_diagnostics
    nodes = database.get_all_nodes()
    by_name = {n['name']: n for n in nodes}
    with database.get_connection() as conn:
        rows = path_view._links(conn)
    usable = [dict(r, source_node=a, target_node=b) for (a, b), r in rows.items() if r['usable']]
    seed, scope = mesh_diagnostics.compute_scope(nodes, usable, database.get_scan_boundaries())
    adj = defaultdict(dict)
    for l in usable:
        a, b = l['source_node'], l['target_node']
        if a in scope and b in scope and a != b:
            kind = 'tunnel' if is_tunnel(l['link_type']) else 'rf'
            for x, y in ((a, b), (b, a)):
                if adj[x].get(y) != 'rf':
                    adj[x][y] = kind
    polled = {n for n in scope if (by_name.get(n) or {}).get('is_active')}
    return seed, adj, polled, by_name


def _bfs(starts, adj, allow_tunnel=True, skip=None):
    seen = set(starts)
    q = deque(starts)
    while q:
        n = q.popleft()
        for p, kind in adj.get(n, {}).items():
            if p in seen or (not allow_tunnel and kind == 'tunnel') or (skip and {n, p} == skip):
                continue
            seen.add(p)
            q.append(p)
    return seen


def dependency(g=None):
    """{node: 'rf' | 'tunnel' | 'off_air'} for polled nodes plus summary counts."""
    seed, adj, polled, by_name = g or graph()
    infra = set(database.get_infrastructure_nodes())
    rf_group = _bfs([n for n in infra if n in adj or n in polled], adj, allow_tunnel=False)
    reach = _bfs([seed], adj) if seed else set()
    out = {}
    for n in sorted(set(by_name)):
        node = by_name[n]
        if n in polled and n in rf_group:
            out[n] = 'rf'
        elif n in polled and n in reach:
            out[n] = 'tunnel'
        elif n in polled:
            out[n] = 'tunnel'     # polled, so reachable some way; not over RF to the backbone
        elif node.get('is_active') is not None and not n.startswith('mac-'):
            out[n] = 'off_air'
    counts = {k: sum(1 for v in out.values() if v == k) for k in ('rf', 'tunnel', 'off_air')}
    return out, counts


def report(hours=24, now=None):
    ensure_tables()
    now = now or datetime.now()
    start = (now - timedelta(hours=hours)).strftime(TS_FMT)
    end = now.strftime(TS_FMT)
    g = graph()
    seed, adj, polled, by_name = g
    base = _bfs([seed], adj) if seed else set()
    infra = database.get_infrastructure_nodes()
    with database.get_connection() as conn:
        samples = defaultdict(list)
        for r in conn.execute('SELECT * FROM tunnel_samples WHERE timestamp >= ? ORDER BY timestamp', (start,)):
            samples[tuple(sorted((r['source_node'], r['target_node'])))].append(dict(r))
        states = defaultdict(list)
        initial = {}
        for r in conn.execute('''SELECT timestamp, source_node, target_node, link_type, state FROM link_state_log
                                 WHERE timestamp >= ? ORDER BY timestamp''', (start,)):
            if is_tunnel(r['link_type']):
                states[tuple(sorted((r['source_node'], r['target_node'])))].append((r['timestamp'], r['state']))
        current = {}
        for r in conn.execute("SELECT source_node, target_node, link_type, status, last_seen FROM links "
                              "WHERE status != 'removed'"):
            if is_tunnel(r['link_type']):
                current.setdefault(tuple(sorted((r['source_node'], r['target_node']))), []).append(dict(r))
        for pair in set(states) | set(current) | set(samples):
            r = conn.execute('''SELECT state FROM link_state_log WHERE timestamp < ? AND state IN ('up', 'down')
                                AND ((source_node = ? AND target_node = ?) OR (source_node = ? AND target_node = ?))
                                ORDER BY timestamp DESC LIMIT 1''', (start, pair[0], pair[1], pair[1], pair[0])).fetchone()
            initial[pair] = r[0] if r else ('up' if pair in current else None)
        services = defaultdict(int)
        for r in conn.execute('SELECT node_name, COUNT(*) FROM services GROUP BY node_name'):
            services[r[0]] = r[1]
    tunnels = []
    for pair in sorted(set(states) | set(current) | set(samples)):
        ss = samples.get(pair, [])
        up = uptime_pct(states.get(pair, []), start, end, initial.get(pair))
        if up is None and ss:
            up = round(100 * sum(1 for s in ss if s['up']) / len(ss), 1)
        rtts = [s['rtt_ms'] for s in ss if s['up']]
        pq = [s['ping_quality'] for s in ss if s['up']] + [s['rev_ping_quality'] for s in ss if s['up']]
        pq = [v for v in pq if v is not None]
        loss = round(100 - median(pq), 1) if pq else None
        rtt_med = _pctl(rtts, 0.5)
        flaps = sum(1 for _, st in states.get(pair, []) if st == 'down')
        cut = (base - _bfs([seed], adj, skip=set(pair))) & polled if seed and pair[0] in adj else set()
        cur = current.get(pair, [])
        status = 'up' if any(c['status'] == 'good' for c in cur) else ('down' if cur else 'gone')
        tunnels.append({
            'a': pair[0], 'b': pair[1], 'type': (cur or ss or [{'link_type': '?'}])[0].get('link_type'),
            'status': status, 'uptime_pct': up, 'flaps': flaps, 'rtt_ms': rtt_med, 'rtt_p90_ms': _pctl(rtts, 0.9),
            'loss_pct': loss, 'health_pct': health_pct(up, loss, rtt_med), 'samples': len(ss),
            'lq': _pctl([s['lq'] for s in ss], 0.5), 'babel_metric': _pctl([s['babel_metric'] for s in ss], 0.5),
            'infrastructure': pair[0] in infra or pair[1] in infra,
            'nodes_behind': sorted(cut), 'behind': len(cut), 'services_behind': sum(services[n] for n in cut),
            'series': [{'t': s['timestamp'], 'rtt': s['rtt_ms'], 'pq': s['ping_quality'], 'up': s['up']}
                       for s in ss][-300:]})
    tunnels.sort(key=lambda t: (t['status'] != 'down', t['health_pct'] if t['health_pct'] is not None else 101,
                                -t['behind']))
    dep, counts = dependency(g)
    only_tunnel = sorted(n for n, v in dep.items() if v == 'tunnel')
    return {'generated_at': end, 'hours': hours, 'seed': seed, 'tunnels': tunnels,
            'dependency': {'counts': counts, 'tunnel_only': only_tunnel,
                           'off_air': sorted(n for n, v in dep.items() if v == 'off_air')[:200]},
            'health_threshold': config.TUNNEL_HEALTH_PCT,
            'notes': ['RTT and loss come from the LQM tracker of the polled end (its own pings over the tunnel); '
                      'uptime from link state changes. "Depends on it" = polled nodes cut off from the starting '
                      'node without this tunnel.']}


def _state(conn, kind):
    return {r['subject']: dict(r) for r in conn.execute('SELECT * FROM tunnel_state WHERE kind = ?', (kind,))}


def _step(conn, kind, subject, value, now_s):
    """Hysteresis: a new value must hold WATCH_RUNS checks. Returns
    (old, new) on a confirmed change, else None."""
    st = _state(conn, kind).get(subject)
    if st is None:
        conn.execute('INSERT INTO tunnel_state (kind, subject, value, pending, count, since) VALUES (?, ?, ?, NULL, 0, ?)',
                     (kind, subject, value, now_s))
        return None
    if st['value'] == value:
        if st['pending']:
            conn.execute('UPDATE tunnel_state SET pending = NULL, count = 0 WHERE kind = ? AND subject = ?', (kind, subject))
        return None
    count = (st['count'] or 0) + 1 if st['pending'] == value else 1
    if count >= WATCH_RUNS:
        conn.execute('UPDATE tunnel_state SET value = ?, pending = NULL, count = 0, since = ? WHERE kind = ? AND subject = ?',
                     (value, now_s, kind, subject))
        return st['value'], value
    conn.execute('UPDATE tunnel_state SET pending = ?, count = ? WHERE kind = ? AND subject = ?', (value, count, kind, subject))
    return None


def check(now=None):
    """After a scan: record samples, then RF-path and tunnel-health transitions."""
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    record(now)
    try:
        import maintenance
    except Exception:
        maintenance = None

    def held(node, peer=None):
        return bool(maintenance and maintenance.covering_window(node, now_s, peer))

    events = []

    def emit(et, node, details, sev):
        database.log_event(et, node, details, sev)
        events.append({'event_type': et, 'node_name': node, 'details': details, 'severity': sev})

    dep, _ = dependency()
    with database.get_connection() as conn:
        for node, cls in dep.items():
            if cls == 'off_air':
                continue
            change = _step(conn, 'path', node, cls, now_s)
            if not change or held(node):
                continue
            old, new = change
            if old == 'rf' and new == 'tunnel':
                emit('rf_path_lost', node, 'Lost its last RF path to the backbone: reachable only through a tunnel '
                                           '(it drops off the mesh if the internet goes down)', 'warning')
            elif old == 'tunnel' and new == 'rf':
                emit('rf_path_restored', node, 'RF path to the backbone is back', 'info')
    rep = report(hours=1, now=now)
    with database.get_connection() as conn:
        for t in rep['tunnels']:
            if t['health_pct'] is None or t['status'] == 'gone':
                continue
            bad = 'bad' if t['health_pct'] < config.TUNNEL_HEALTH_PCT else 'ok'
            change = _step(conn, 'tunnel', f"{t['a']}|{t['b']}", bad, now_s)
            if not change or held(t['a'], t['b']):
                continue
            detail = (f"health {t['health_pct']:g}% (up {t['uptime_pct']}%, loss {t['loss_pct']}%, RTT {t['rtt_ms']} ms)"
                      + (f"; {t['behind']} node(s), {t['services_behind']} service(s) depend on it" if t['behind'] else ''))
            if change[1] == 'bad':
                emit('tunnel_degraded', t['a'], f"Tunnel link to {t['b']}: {detail}", 'warning')
            elif change[0] == 'bad':
                emit('tunnel_restored', t['a'], f"Tunnel link to {t['b']}: back to normal, {detail}", 'info')
    return events

"""
Route Watch (#9): periodic traceroutes along key paths, with a change log.

Key paths are kept in SQLite `route_paths`:
- auto: the starting node to one representative node of every infrastructure
  site, and site to site (every pair of sites), re-synced each run;
- manual: operator-added source -> target paths (source empty = the starting
  node).

Each run traceroutes every enabled path through the source node's own
traceroute CGI (rf_stats.traceroute_via_aredn, as the live path check does)
and stores the hops in `route_history`. A path's baseline is its most common
route over the last ROUTE_BASELINE_HOURS (timed-out hops match anything).
Events fire on transitions only, after ROUTE_CHANGE_RUNS runs in a row:

- `route_changed` (info): the route differs from the baseline;
- `route_detour` (warning): it differs and is ROUTE_DETOUR_HOPS or more hops
  longer, or it leaves the backbone (a tunnel hop or a non-infrastructure node
  the baseline didn't use);
- `route_restored` (info): back on the baseline (or the new route has become
  the usual one).

Runs are gated by gateway.py: when the starting node is unreachable the run is
aborted and nothing is recorded. Paths with an end in a maintenance window are
skipped and their counters reset. A run never blocks scans (APScheduler job).
"""

import ipaddress
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta

import config
import database
import gateway
import maintenance
import rf_stats

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
TUNNEL_NET = ipaddress.ip_network('172.31.0.0/16')   # AREDN WireGuard / vtun tunnel addresses
HOST_PREFIX_RE = re.compile(r'^(dtdlink|mid\d+|lan|wan|xlink\d+)\.', re.I)
LINK_TYPE_NAMES = {'DTD': 'DtD', 'RF': 'RF', 'WIREGUARD': 'WireGuard', 'TUN': 'TUN'}
TUNNEL_TYPES = ('WireGuard', 'TUN', 'tunnel')
MAX_SITE_PAIRS = 10
RUN_POOL = 3

_run_lock = threading.Lock()
_tables_ready = False


# ------------------------------------------------------------- schema -----

def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS route_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auto_key TEXT UNIQUE,
                label TEXT,
                source TEXT,
                target TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'manual',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS route_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                ok INTEGER NOT NULL,
                reason TEXT,
                source TEXT,
                target TEXT,
                hop_count INTEGER,
                rtt_ms REAL,
                signature TEXT,
                hops TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_route_history_path ON route_history(path_id, timestamp)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS route_state (
                path_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'learning',
                bad_count INTEGER NOT NULL DEFAULT 0,
                good_count INTEGER NOT NULL DEFAULT 0,
                since TEXT,
                last_change TEXT,
                updated_at TEXT,
                detail TEXT
            )
        ''')
    _tables_ready = True


def _now_s(now=None):
    return (now or datetime.now()).strftime(TS_FMT)


# -------------------------------------------------------------- hops -----

def _is_tunnel_ip(ip):
    try:
        return ipaddress.ip_address(ip) in TUNNEL_NET
    except (TypeError, ValueError):
        return False


def _hop_name(host, ip):
    """Node name of a hop ('dtdlink.KF9MT-Home-LiteBeam.local.mesh' ->
    'kf9mt-home-litebeam'), or its IP when it has no name."""
    host = (host or '').strip()
    if not host or host == ip:
        return ip or '*'
    host = host.lower()
    if host.endswith('.local.mesh'):
        host = host[:-len('.local.mesh')]
    return HOST_PREFIX_RE.sub('', host)


def _link_types(conn):
    """{(a, b): link_type} for current links, both directions."""
    out = {}
    for r in conn.execute("SELECT source_node, target_node, link_type FROM links WHERE status != 'removed'"):
        t = (r['link_type'] or '').upper()
        t = LINK_TYPE_NAMES.get(t, t)
        for key in ((r['source_node'], r['target_node']), (r['target_node'], r['source_node'])):
            # Prefer the wired/RF type over a tunnel when a pair has both.
            if key not in out or out[key] in TUNNEL_TYPES:
                out[key] = t
    return out


def annotate_hops(hops, source_name, infra, link_types):
    """Normalized hops: name, ip, ms, timeout, how the hop was reached ('via':
    DtD / RF / WireGuard / TUN / tunnel) and whether it is infrastructure."""
    out, prev = [], source_name
    for h in hops or []:
        if h.get('timeout'):
            out.append({'hop': h.get('hop'), 'name': '*', 'ip': None, 'ms': None, 'timeout': True,
                        'via': None, 'tunnel': False, 'infra': False})
            prev = None
            continue
        ip = h.get('ip')
        name = _hop_name(h.get('host'), ip)
        tunnel = _is_tunnel_ip(ip) and name == ip
        host = (h.get('host') or '').lower()
        if tunnel:
            via = 'tunnel'
        elif host.startswith('dtdlink.'):
            via = 'DtD'
        elif prev and prev != '*':
            via = link_types.get((prev, name)) or ('tunnel' if _is_tunnel_ip(prev) else None)
        else:
            via = None
        out.append({'hop': h.get('hop'), 'name': name, 'ip': ip, 'ms': h.get('ms'), 'timeout': False,
                    'via': via, 'tunnel': tunnel or via in TUNNEL_TYPES,
                    'infra': name in infra})
        prev = name
    return out


def _signature(hops):
    return [h['name'] for h in hops]


def _same(a, b):
    """Same route: same length and every hop equal ('*' matches anything)."""
    if a is None or b is None or len(a) != len(b):
        return False
    return all(x == y or '*' in (x, y) for x, y in zip(a, b))


def _off_backbone(hops, target):
    """Hops that take the route off the backbone: tunnel hops and named
    nodes that aren't infrastructure (the target itself excepted)."""
    return [h['name'] for h in hops
            if not h['timeout'] and h['name'] != target and (h['tunnel'] or not h['infra'])]


# ------------------------------------------------------------- paths -----

def _site_reps():
    """{site: representative node} for infrastructure sites: the starting
    node for its own site, else the first active site node with an IP."""
    infra = database.get_infrastructure_nodes()
    start_name = database.get_ip_name_map().get(gateway.starting_node_host())
    sites = {}
    for name, site in sorted(infra.items()):
        if site:
            sites.setdefault(site, []).append(name)
    reps = {}
    with database.get_connection() as conn:
        rows = {r['name']: r for r in conn.execute('SELECT name, ip, is_active, last_seen FROM nodes')}
    for site, names in sites.items():
        if start_name in names:
            reps[site] = start_name
            continue
        usable = [n for n in names if rows.get(n) and rows[n]['ip']]
        usable.sort(key=lambda n: (not rows[n]['is_active'], n))
        if usable:
            reps[site] = usable[0]
    return reps, start_name


def sync_auto_paths():
    """Create/update the automatic paths; a path whose ends changed gets a
    fresh baseline. Disabled (operator-removed) auto paths stay disabled."""
    ensure_tables()
    reps, start_name = _site_reps()
    wanted = {}
    for site, rep in sorted(reps.items()):
        if rep != start_name:
            wanted[f'start:{site}'] = (None, rep, f'Starting node → {site}')
    sites = sorted(reps)
    pairs = [(a, b) for i, a in enumerate(sites) for b in sites[i + 1:]][:MAX_SITE_PAIRS]
    for a, b in pairs:
        wanted[f'site:{a}-{b}'] = (reps[a], reps[b], f'{a} → {b}')
    now_s = _now_s()
    with database.get_connection() as conn:
        existing = {r['auto_key']: dict(r) for r in
                    conn.execute("SELECT * FROM route_paths WHERE kind = 'auto'")}
        for key, (src, tgt, label) in wanted.items():
            row = existing.get(key)
            if not row:
                conn.execute('''INSERT INTO route_paths (auto_key, label, source, target, kind, enabled, created_at)
                                VALUES (?, ?, ?, ?, 'auto', 1, ?)''', (key, label, src, tgt, now_s))
            elif (row['source'], row['target'], row['label']) != (src, tgt, label):
                conn.execute('UPDATE route_paths SET source = ?, target = ?, label = ? WHERE id = ?',
                             (src, tgt, label, row['id']))
                if (row['source'], row['target']) != (src, tgt):
                    conn.execute('DELETE FROM route_state WHERE path_id = ?', (row['id'],))


def get_paths(enabled_only=True):
    ensure_tables()
    sql = 'SELECT * FROM route_paths' + (' WHERE enabled = 1' if enabled_only else '') + ' ORDER BY kind, label, id'
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def add_path(source, target, label=None):
    """Add an operator path. source empty = the starting node."""
    ensure_tables()
    source = (source or '').strip().lower() or None
    target = (target or '').strip().lower()
    if not target:
        raise ValueError('target is required')
    for name in filter(None, (source, target)):
        if not database.get_node(name):
            raise ValueError(f'Unknown node: {name}')
    if source == target:
        raise ValueError('source and target must differ')
    label = (label or '').strip()[:80] or f"{source or 'Starting node'} → {target}"
    with database.get_connection() as conn:
        cur = conn.execute('''INSERT INTO route_paths (label, source, target, kind, enabled, created_at)
                              VALUES (?, ?, ?, 'manual', 1, ?)''', (label, source, target, _now_s()))
        return cur.lastrowid


def delete_path(path_id):
    """Delete a manual path with its history; an automatic one is disabled
    instead, so the next sync doesn't bring it back."""
    ensure_tables()
    with database.get_connection() as conn:
        row = conn.execute('SELECT kind FROM route_paths WHERE id = ?', (path_id,)).fetchone()
        if not row:
            return False
        if row['kind'] == 'auto':
            conn.execute('UPDATE route_paths SET enabled = 0 WHERE id = ?', (path_id,))
        else:
            conn.execute('DELETE FROM route_paths WHERE id = ?', (path_id,))
        conn.execute('DELETE FROM route_history WHERE path_id = ?', (path_id,))
        conn.execute('DELETE FROM route_state WHERE path_id = ?', (path_id,))
    return True


# --------------------------------------------------------------- run -----

def _trace(path, trace_fn):
    """Traceroute one path. Returns (raw trace or None, source name)."""
    source_ip = None
    if path['source']:
        node = database.get_node(path['source'])
        source_ip = node.get('ip') if node else None
        if not source_ip:
            return None, path['source']
    trace = trace_fn(path['target'], source_node_ip=source_ip)
    source_name = path['source']
    if not source_name and trace:
        m = re.search(r'Client:\s*(\S+)', trace.get('raw') or '')
        source_name = m.group(1).lower() if m else None
    return trace, source_name


def _baseline(conn, path_id, since, before_id=None):
    """Most common successful route since `since` (timed-out hops collapsed),
    or None with fewer than ROUTE_BASELINE_MIN_RUNS runs."""
    sql = 'SELECT id, signature FROM route_history WHERE path_id = ? AND ok = 1 AND timestamp >= ?'
    args = [path_id, since]
    if before_id:
        sql += ' AND id < ?'
        args.append(before_id)
    sigs = [json.loads(r['signature']) for r in conn.execute(sql, args)]
    if len(sigs) < config.ROUTE_BASELINE_MIN_RUNS:
        return None
    groups = []   # [(representative signature, count)]
    for s in sigs:
        for g in groups:
            if _same(g[0], s):
                g[1] += 1
                if '*' in g[0] and '*' not in s:
                    g[0] = s
                break
        else:
            groups.append([s, 1])
    return max(groups, key=lambda g: g[1])[0]


def _emit(events, event_type, node, details, severity):
    """Queue an event; run_paths logs it once its own connection is closed."""
    events.append({'type': event_type, 'node': node, 'details': details, 'severity': severity})


def _route_text(sig, limit=8):
    names = [n for n in sig]
    if len(names) > limit:
        names = names[:limit - 2] + ['…'] + names[-1:]
    return ' → '.join(names)


def _load_state(conn, path_id):
    r = conn.execute('SELECT * FROM route_state WHERE path_id = ?', (path_id,)).fetchone()
    if not r:
        return {'path_id': path_id, 'status': 'learning', 'bad_count': 0, 'good_count': 0,
                'since': None, 'last_change': None, 'detail': {}}
    s = dict(r)
    try:
        s['detail'] = json.loads(s.get('detail') or '{}')
    except (TypeError, ValueError):
        s['detail'] = {}
    return s


def _save_state(conn, s, now_s):
    conn.execute('''
        INSERT INTO route_state (path_id, status, bad_count, good_count, since, last_change, updated_at, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path_id) DO UPDATE SET status = excluded.status, bad_count = excluded.bad_count,
            good_count = excluded.good_count, since = excluded.since, last_change = excluded.last_change,
            updated_at = excluded.updated_at, detail = excluded.detail
    ''', (s['path_id'], s['status'], s['bad_count'], s['good_count'], s.get('since'),
          s.get('last_change'), now_s, json.dumps(s.get('detail') or {})))


def _evaluate(conn, path, record, state, now, events):
    """Advance one path's state with a stored successful run."""
    now_s = _now_s(now)
    since = _now_s(now - timedelta(hours=config.ROUTE_BASELINE_HOURS))
    base = _baseline(conn, path['id'], since, before_id=record['id'])
    sig, hops = record['signature'], record['hops']
    label = path['label']
    if base is None:
        state['status'] = 'learning'
        state['detail'] = {'baseline': None}
        return
    if state['status'] == 'learning':
        state['status'] = 'normal'
    need = config.ROUTE_CHANGE_RUNS
    differs = not _same(sig, base)
    detour = False
    reasons = []
    if differs:
        grown = len(sig) - len(base)
        if grown >= config.ROUTE_DETOUR_HOPS:
            detour = True
            reasons.append(f'{grown} more hops than usual')
        # Off-backbone hops the usual route doesn't already use.
        off = [n for n in _off_backbone(hops, path['target']) if n not in base]
        if off:
            detour = True
            reasons.append('leaves the backbone via ' + ', '.join(off[:4]))
    state['detail'].update({'baseline': base, 'reasons': reasons})
    target = path['target']
    if differs:
        state['bad_count'] += 1
        state['good_count'] = 0
        if state['bad_count'] < need:
            return
        details = (f"Route {label}: now {len(sig)} hops ({_route_text(sig)}); "
                   f"usual {len(base)} hops ({_route_text(base)})")
        if detour and state['status'] != 'detour':
            _emit(events, database.EVENT_ROUTE_DETOUR, target, f"{details}; {'; '.join(reasons)}", 'warning')
            state.update(status='detour', since=state.get('since') if state['status'] == 'changed' else now_s,
                         last_change=now_s)
            state['detail']['alarm_baseline'] = base
        elif not detour and state['status'] == 'normal':
            _emit(events, database.EVENT_ROUTE_CHANGED, target, details, 'info')
            state.update(status='changed', since=now_s, last_change=now_s)
            state['detail']['alarm_baseline'] = base
    else:
        state['good_count'] += 1
        state['bad_count'] = 0
        if state['status'] in ('changed', 'detour') and state['good_count'] >= need:
            alarm_base = state['detail'].get('alarm_baseline')
            how = ('back on its usual route' if alarm_base is None or _same(alarm_base, base)
                   else 'the new route is now the usual one')
            _emit(events, database.EVENT_ROUTE_RESTORED, target,
                  f"Route {label}: {how}, {len(sig)} hops ({_route_text(sig)})", 'info')
            state.update(status='normal', since=None, last_change=now_s)
            state['detail'].pop('alarm_baseline', None)


def _store_run(conn, path, trace, source_name, infra, link_types, now):
    hops = annotate_hops((trace or {}).get('hops'), source_name, infra, link_types)
    target = path['target']
    answered = [h for h in hops if not h['timeout']]
    reached = bool(answered) and answered[-1]['name'] == target
    ok = bool(trace) and reached
    reason = None if ok else ('no_answer' if not trace else 'target_not_reached')
    sig = _signature(hops)
    rtt = answered[-1]['ms'] if reached else None
    cur = conn.execute('''INSERT INTO route_history (path_id, timestamp, ok, reason, source, target,
                                                     hop_count, rtt_ms, signature, hops)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       (path['id'], _now_s(now), int(ok), reason, source_name, target,
                        len(hops) if trace else None, rtt, json.dumps(sig), json.dumps(hops)))
    return {'id': cur.lastrowid, 'ok': ok, 'reason': reason, 'signature': sig, 'hops': hops,
            'hop_count': len(hops), 'rtt_ms': rtt}


def _map(fn, items):
    try:
        import eventlet
        pool = eventlet.GreenPool(RUN_POOL)
        return list(pool.imap(fn, items))
    except ImportError:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=RUN_POOL) as pool:
            return list(pool.map(fn, items))


def run_paths(paths=None, now=None, trace_fn=None):
    """Traceroute paths (default: all enabled, after an auto-path sync) and
    advance their state. Returns {'events', 'ran', 'skipped', 'aborted'}."""
    ensure_tables()
    trace_fn = trace_fn or rf_stats.traceroute_via_aredn
    if not _run_lock.acquire(blocking=False):
        return {'events': [], 'ran': 0, 'skipped': [], 'aborted': 'busy'}
    try:
        if not gateway.is_reachable():
            return {'events': [], 'ran': 0, 'skipped': [], 'aborted': gateway.unreachable_message()}
        if paths is None:
            sync_auto_paths()
            paths = get_paths()
        now = now or datetime.now()
        todo, skipped = [], []
        for p in paths:
            if any(n and maintenance.is_in_maintenance(n, at=now) for n in (p['source'], p['target'])):
                skipped.append(p['id'])
            else:
                todo.append(p)
        results = _map(lambda p: _trace(p, trace_fn), todo)
        if any(t is None for t, _ in results) and gateway.failure_is_ours():
            return {'events': [], 'ran': 0, 'skipped': skipped, 'aborted': gateway.unreachable_message()}
        infra = set(database.get_infrastructure_node_names())
        events = []
        with database.get_connection() as conn:
            link_types = _link_types(conn)
            for pid in skipped:
                s = _load_state(conn, pid)
                s['bad_count'] = s['good_count'] = 0
                _save_state(conn, s, _now_s(now))
            for p, (trace, source_name) in zip(todo, results):
                record = _store_run(conn, p, trace, source_name, infra, link_types, now)
                state = _load_state(conn, p['id'])
                state['detail']['last_ok'] = record['ok']
                if record['ok']:
                    _evaluate(conn, p, record, state, now, events)
                _save_state(conn, state, _now_s(now))
            cutoff = _now_s(now - timedelta(days=config.ROUTE_HISTORY_RETENTION_DAYS))
            conn.execute('DELETE FROM route_history WHERE timestamp < ?', (cutoff,))
        for e in events:
            database.log_event(e['type'], node_name=e['node'], details=e['details'], severity=e['severity'])
        return {'events': events, 'ran': len(todo), 'skipped': skipped, 'aborted': None}
    finally:
        _run_lock.release()


def run_one(path_id, trace_fn=None):
    ensure_tables()
    with database.get_connection() as conn:
        row = conn.execute('SELECT * FROM route_paths WHERE id = ?', (path_id,)).fetchone()
    if not row:
        return None
    return run_paths([dict(row)], trace_fn=trace_fn)


# ------------------------------------------------------------ report -----

def _diff(hops, base):
    """Mark hops that aren't on the baseline route."""
    base_set = set(base or [])
    for h in hops:
        h['new'] = bool(base) and not h['timeout'] and h['name'] not in base_set
    return hops


def list_report(hours=24):
    """Every path with its state, last run, baseline and recent hop counts."""
    ensure_tables()
    since = _now_s(datetime.now() - timedelta(hours=hours))
    out = []
    with database.get_connection() as conn:
        for p in [dict(r) for r in conn.execute('SELECT * FROM route_paths ORDER BY kind, label, id')]:
            s = _load_state(conn, p['id'])
            last = conn.execute('SELECT * FROM route_history WHERE path_id = ? ORDER BY id DESC LIMIT 1',
                                (p['id'],)).fetchone()
            series = [{'t': r['timestamp'], 'hops': r['hop_count'], 'ok': bool(r['ok'])} for r in conn.execute(
                'SELECT timestamp, hop_count, ok FROM route_history WHERE path_id = ? AND timestamp >= ? ORDER BY id',
                (p['id'], since))]
            base = s['detail'].get('baseline')
            last_d = None
            if last:
                hops = json.loads(last['hops'] or '[]')
                last_d = {'timestamp': last['timestamp'], 'ok': bool(last['ok']), 'reason': last['reason'],
                          'hop_count': last['hop_count'], 'rtt_ms': last['rtt_ms'], 'source': last['source'],
                          'hops': _diff(hops, base), 'off_backbone': _off_backbone(hops, p['target'])}
            out.append({**p, 'enabled': bool(p['enabled']), 'status': s['status'], 'since': s.get('since'),
                        'last_change': s.get('last_change'), 'baseline': base,
                        'baseline_hops': len(base) if base else None,
                        'reasons': s['detail'].get('reasons') or [], 'last': last_d, 'series': series})
    return {'generated_at': _now_s(), 'hours': hours, 'interval_minutes': config.ROUTE_WATCH_INTERVAL_MINUTES,
            'enabled': config.ROUTE_WATCH_ENABLED, 'paths': out}


def path_history(path_id, hours=24):
    """Runs of one path, plus the runs where the route changed from the run
    before (with hops marked against the current baseline)."""
    ensure_tables()
    since = _now_s(datetime.now() - timedelta(hours=hours))
    with database.get_connection() as conn:
        p = conn.execute('SELECT * FROM route_paths WHERE id = ?', (path_id,)).fetchone()
        if not p:
            return None
        s = _load_state(conn, path_id)
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM route_history WHERE path_id = ? AND timestamp >= ? ORDER BY id', (path_id, since))]
    base = s['detail'].get('baseline')
    runs, changes, prev = [], [], None
    for r in rows:
        sig = json.loads(r['signature'] or '[]')
        run = {'timestamp': r['timestamp'], 'ok': bool(r['ok']), 'reason': r['reason'],
               'hop_count': r['hop_count'], 'rtt_ms': r['rtt_ms']}
        runs.append(run)
        if r['ok'] and (prev is None or not _same(prev, sig)):
            hops = json.loads(r['hops'] or '[]')
            changes.append({**run, 'route': sig, 'hops': _diff(hops, base),
                            'baseline': bool(base) and _same(sig, base),
                            'off_backbone': _off_backbone(hops, p['target'])})
        if r['ok']:
            prev = sig
    return {'path': dict(p), 'status': s['status'], 'baseline': base, 'hours': hours,
            'runs': runs, 'changes': changes}

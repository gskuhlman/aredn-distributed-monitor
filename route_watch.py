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
  the usual one);
- `route_flapping` (warning) / `route_steady` (info): ROUTE_FLAP_CHANGES or
  more route switches in 24 h (even between equally good routes: each switch
  can cost a voice call a gap or reordered packets), clearing at half that.
  Switches that only differ between radios of one infrastructure site
  (`_site_internal`) don't count.

Paths have a role (`path_role`): site-to-site and operator paths are the
backbone and raise the events above; the starting node's own paths
(`start:<site>`) are access/context: tracked and shown, but they never log
events (the starting node switches between tunnels often).

`why_changed()` explains each switch: where the two routes part and rejoin,
and the likely cause from what happened around it on the old route
(maintenance, reboot, node or link down, a hop's quality/SNR/Babel cost worse),
on the new one (link back, cheaper), an incomplete trace, a switch inside one
site, or a near-tie (nothing behind it); plus the routes used with their
delay, flaps per day and the hops where the routes part most often.

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


def path_role(path):
    """'access' for the starting node's own paths (context: they show how this
    PC reaches a site and never raise warnings), 'backbone' for site-to-site
    and operator paths."""
    return 'access' if (path.get('auto_key') or '').startswith('start:') else 'backbone'


def _divergence(source, old, new):
    """Where two routes part and meet again: (node before the split, old
    segment, new segment, node where they rejoin). '*' matches anything."""
    a, b = [source or '?'] + list(old), [source or '?'] + list(new)
    eq = lambda x, y: x == y or '*' in (x, y)
    i = 0
    while i < min(len(a), len(b)) and eq(a[i], b[i]):
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and eq(a[-1 - j], b[-1 - j]):
        j += 1
    return (a[i - 1] if i else None, a[i:len(a) - j], b[i:len(b) - j],
            a[len(a) - j] if j else None)


def _site_internal(source, old, new, sites):
    """True when the routes only differ between radios of one infrastructure
    site (e.g. two DtD-cabled radios on a mast): no difference for traffic."""
    div, old_seg, new_seg, rejoin = _divergence(source, old, new)
    nodes = [div, rejoin] + old_seg + new_seg
    if None in nodes or '*' in nodes:
        return False
    found = {sites.get(n) for n in nodes}
    return len(found) == 1 and None not in found


def _flaps(conn, path_id, since, sites):
    """(route switches, of which inside one site) between successful runs since `since`."""
    n = internal = 0
    prev = None
    for r in conn.execute('SELECT source, signature FROM route_history WHERE path_id = ? AND ok = 1 '
                          'AND timestamp >= ? ORDER BY id', (path_id, since)):
        sig = json.loads(r['signature'] or '[]')
        if prev is not None and not _same(prev, sig):
            if _site_internal(r['source'], prev, sig, sites):
                internal += 1
            else:
                n += 1
        prev = sig
    return n, internal


def _evaluate(conn, path, record, state, now, events, sites=None):
    """Advance one path's state with a stored successful run."""
    now_s = _now_s(now)
    since = _now_s(now - timedelta(hours=config.ROUTE_BASELINE_HOURS))
    base = _baseline(conn, path['id'], since, before_id=record['id'])
    sig, hops = record['signature'], record['hops']
    label = path['label']
    backbone = path_role(path) == 'backbone'
    if base is None:
        state['status'] = 'learning'
        state['detail'] = {'baseline': None}
        return
    if state['status'] == 'learning':
        state['status'] = 'normal'
    target = path['target']
    # Switching routes often, even between equally good ones, can cost voice
    # calls a gap or reordered packets at each switch; switches between radios
    # of one site don't count.
    flaps, internal = _flaps(conn, path['id'], _now_s(now - timedelta(hours=24)), sites or {})
    state['detail'].update({'flaps_24h': flaps, 'flaps_internal_24h': internal})
    if backbone:
        if flaps >= config.ROUTE_FLAP_CHANGES and not state['detail'].get('flapping'):
            state['detail']['flapping'] = now_s
            _emit(events, database.EVENT_ROUTE_FLAPPING, target,
                  f"Route {label}: switched route {flaps} times in 24 h; see Route Watch, Why did it change?",
                  'warning')
        elif state['detail'].get('flapping') and flaps <= config.ROUTE_FLAP_CHANGES // 2:
            state['detail'].pop('flapping')
            _emit(events, database.EVENT_ROUTE_STEADY, target,
                  f"Route {label}: steady again ({flaps} route switches in 24 h)", 'info')
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
    # The starting node's own paths are context: never a detour warning.
    detour = detour and backbone
    if not backbone and state['status'] == 'detour':
        state['status'] = 'changed'     # stored before access paths stopped warning
    state['detail'].update({'baseline': base, 'reasons': reasons})
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
            state['detail'].update(alarm_baseline=base, alarm_reasons=reasons)
        elif not detour and state['status'] == 'normal':
            if backbone:
                _emit(events, database.EVENT_ROUTE_CHANGED, target, details, 'info')
            state.update(status='changed', since=now_s, last_change=now_s)
            state['detail'].update(alarm_baseline=base, alarm_reasons=reasons)
    else:
        state['good_count'] += 1
        state['bad_count'] = 0
        if state['status'] in ('changed', 'detour') and state['good_count'] >= need:
            alarm_base = state['detail'].get('alarm_baseline')
            how = ('back on its usual route' if alarm_base is None or _same(alarm_base, base)
                   else 'the new route is now the usual one')
            if backbone:
                _emit(events, database.EVENT_ROUTE_RESTORED, target,
                      f"Route {label}: {how}, {len(sig)} hops ({_route_text(sig)})", 'info')
            state.update(status='normal', since=None, last_change=now_s)
            state['detail'].pop('alarm_baseline', None)
            state['detail'].pop('alarm_reasons', None)


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
        sites = {n: s for n, s in database.get_infrastructure_nodes().items() if s}
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
                    _evaluate(conn, p, record, state, now, events, sites)
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
            d = s['detail']
            # While a path is still changed/detour its latest run may already be
            # back on the usual route (clearing needs ROUTE_CHANGE_RUNS runs):
            # show why the alarm was raised, not an empty reason list.
            reasons = d.get('reasons') or []
            on_usual = bool(last_d and last_d['ok'] and base and _same(json.loads(last['signature'] or '[]'), base))
            if not reasons and s['status'] in ('changed', 'detour'):
                reasons = d.get('alarm_reasons') or []
            out.append({**p, 'enabled': bool(p['enabled']), 'role': path_role(p),
                        'status': s['status'], 'since': s.get('since'),
                        'last_change': s.get('last_change'), 'baseline': base,
                        'baseline_hops': len(base) if base else None,
                        'reasons': reasons, 'back_on_usual': on_usual and s['status'] in ('changed', 'detour'),
                        'flaps_24h': d.get('flaps_24h'), 'flaps_internal_24h': d.get('flaps_internal_24h'),
                        'flapping': bool(d.get('flapping')), 'flapping_since': d.get('flapping'),
                        'last': last_d, 'series': series})
    return {'generated_at': _now_s(), 'hours': hours, 'interval_minutes': config.ROUTE_WATCH_INTERVAL_MINUTES,
            'enabled': config.ROUTE_WATCH_ENABLED, 'flap_changes': config.ROUTE_FLAP_CHANGES, 'paths': out}


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


# -------------------------------------------------- why did it change -----

# Causes, most specific first: the first one with evidence wins.
CAUSE_LABELS = {
    'maintenance': 'Maintenance window',
    'node_rebooted': 'A node on the old route rebooted',
    'link_failed': 'A link on the old route went down',
    'node_trouble': 'A node on the old route stopped answering fully',
    'link_degraded': 'A link on the old route got worse',
    'link_restored': 'A link on the new route came back',
    'better_route': 'The new route got cheaper',
    'trace_incomplete': 'Trace incomplete (hops did not answer)',
    'site_internal': 'Switch between radios inside one site',
    'near_tie': 'Near-tie: nothing failed, routes about equal',
    'unknown': 'No cause found',
}
WHY_PAD = timedelta(minutes=10)        # look this far before the last run on the old route
WHY_AFTER = timedelta(minutes=5)       # ... and after the first run on the new one
NEAR_TIE_PCT = 25                      # Babel route costs within this = a near-tie
COST_RISE_PCT = 50                     # a link's Babel cost up this much = got worse
QUALITY_DROP = 20                      # link quality points
SNR_DROP_DB = 6
VOIP_RTT_SPREAD_MS = 30                # routes this far apart in delay: a switch is felt in a call


def _link_cost(rx, tx):
    """Babel's cost of a direct link (as babel_view._link_cost)."""
    costs = [float(c) for c in (rx, tx) if c is not None]
    if not costs:
        return None
    return max(max(costs), costs[0] * costs[1] / 256.0) if len(costs) == 2 else costs[0]


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    m = len(values) // 2
    return values[m] if len(values) % 2 else (values[m - 1] + values[m]) / 2


def _pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def _round(v, digits=1):
    return round(v, digits) if v is not None else None


def _ts_s(ts, delta=None):
    t = datetime.strptime(ts[:19], TS_FMT)
    return _now_s(t + delta) if delta else ts


def _pairs(div, seg, rejoin):
    chain = [n for n in [div] + list(seg) + [rejoin] if n]
    return [(a, b) for a, b in zip(chain, chain[1:]) if '*' not in (a, b)]


def _link_samples(conn, a, b, lo, hi):
    """Scan samples of a's tracker of b, else b's of a (RF links only: DtD and
    tunnel links have no history)."""
    for src, dst in ((a, b), (b, a)):
        rows = [dict(r) for r in conn.execute(
            '''SELECT timestamp, quality, snr, txcost, rxcost, routable FROM link_history
               WHERE source_node = ? AND target_node = ? AND timestamp BETWEEN ? AND ?
                 AND sample_type = 'scan' ORDER BY timestamp''', (src, dst, lo, hi))]
        if rows:
            for r in rows:
                r['cost'] = _link_cost(r['rxcost'], r['txcost'])
            return rows
    return []


def _link_view(conn, a, b, t0, t1, link_types):
    """One hop of a segment around the change: the last sample before the old
    route's last run (t0), the worst sample between t0 and the new route's
    first run (t1), and the newest one by then."""
    rows = _link_samples(conn, a, b, _ts_s(t0, -timedelta(minutes=45)), _ts_s(t1, WHY_AFTER))
    before = [r for r in rows if r['timestamp'] <= t0]
    after = [r for r in rows if r['timestamp'] > t0]
    view = {'a': a, 'b': b, 'type': link_types.get((a, b)) or ('RF' if rows else None),
            'before': before[-1] if before else None, 'after': after[-1] if after else None,
            'worst': min(after, key=lambda r: r['quality'] if r['quality'] is not None else 999) if after else None}
    return view


def _degraded(view):
    """Why a hop got worse between before and after, or None."""
    b, w, a = view['before'], view['worst'], view['after']
    if not b or not w:
        return None
    out = []
    if b['quality'] is not None and w['quality'] is not None:
        if b['quality'] - w['quality'] >= QUALITY_DROP or (b['quality'] >= 50 > w['quality']):
            out.append(f"quality {b['quality']:.0f}% → {w['quality']:.0f}%")
    if b['snr'] is not None and w['snr'] is not None and b['snr'] - w['snr'] >= SNR_DROP_DB:
        out.append(f"SNR {b['snr']:.0f} → {w['snr']:.0f} dB")
    if b['cost'] and a and a['cost'] and a['cost'] >= b['cost'] * (1 + COST_RISE_PCT / 100):
        out.append(f"Babel cost {b['cost']:.0f} → {a['cost']:.0f}")
    if b['routable'] and a and a['routable'] == 0:
        out.append('no longer routable')
    return ', '.join(out) or None


def _improved(view):
    b, a = view['before'], view['after']
    if b and a and b['cost'] and a['cost'] and a['cost'] * (1 + COST_RISE_PCT / 100) <= b['cost']:
        return f"Babel cost {b['cost']:.0f} → {a['cost']:.0f}"
    return None


def _segment_cost(views, key='after'):
    """Sum of the hops' Babel costs, or None when a hop has none (DtD, tunnel,
    or no sample)."""
    total = 0.0
    for v in views:
        s = v[key] or v['before']
        if not s or not s['cost']:
            return None
        total += s['cost']
    return total


def _route_groups(runs, base):
    """Distinct routes (timed-out hops collapsed) with how often each was
    used and its delay to the target."""
    groups = []
    for r in runs:
        for g in groups:
            if _same(g['route'], r['sig']):
                break
        else:
            g = {'route': r['sig'], 'runs': [], 'first_seen': r['timestamp']}
            groups.append(g)
        if '*' in g['route'] and '*' not in r['sig']:
            g['route'] = r['sig']
        g['runs'].append(r)
    total = len(runs) or 1
    out = []
    for g in groups:
        rtts = [r['rtt_ms'] for r in g['runs']]
        out.append({'route': g['route'], 'hop_count': len(g['route']), 'runs': len(g['runs']),
                    'share_pct': round(100.0 * len(g['runs']) / total, 1),
                    'rtt_median': _round(_median(rtts)), 'rtt_p90': _round(_pct(rtts, 0.9)),
                    'usual': bool(base) and _same(g['route'], base),
                    'first_seen': g['first_seen'], 'last_seen': g['runs'][-1]['timestamp']})
    out.sort(key=lambda g: -g['runs'])
    for i, g in enumerate(out):
        g['id'] = i
    return out


def _group_of(groups, sig):
    for g in groups:
        if _same(g['route'], sig):
            return g
    return None


def _explain(conn, old, new, sites, link_types, events, states):
    """One route change: where the routes part, what happened there around
    the change, and the cause."""
    t0, t1 = old['timestamp'], new['timestamp']
    lo, hi = _ts_s(t0, -WHY_PAD), _ts_s(t1, WHY_AFTER)
    source = new['source'] or old['source']
    div, old_seg, new_seg, rejoin = _divergence(source, old['sig'], new['sig'])
    old_nodes = [n for n in [div] + old_seg if n and n != '*']
    new_nodes = [n for n in new_seg if n != '*']
    old_pairs, new_pairs = _pairs(div, old_seg, rejoin), _pairs(div, new_seg, rejoin)
    old_views = [_link_view(conn, a, b, t0, t1, link_types) for a, b in old_pairs]
    new_views = [_link_view(conn, a, b, t0, t1, link_types) for a, b in new_pairs]
    in_window = lambda ts: lo <= ts <= hi
    found = []          # (cause, evidence text)

    for n in set(old_nodes + new_nodes):
        try:
            if maintenance.is_in_maintenance(n, at=datetime.strptime(t1, TS_FMT)):
                found.append(('maintenance', f'{n} is in a maintenance window'))
        except Exception:   # maintenance is optional context
            pass
    for e in events:
        if not in_window(e['timestamp']) or e['node_name'] not in old_nodes + ([rejoin] if rejoin else []):
            continue
        if e['event_type'] == 'node_reboot':
            found.append(('node_rebooted', f"{e['node_name']} rebooted ({e['timestamp'][11:16]})"))
        elif e['event_type'] == 'node_offline':
            found.append(('node_trouble', f"{e['node_name']} stopped answering ({e['timestamp'][11:16]})"))
        elif e['event_type'] == 'node_degraded' and 'timed out' in (e['details'] or ''):
            found.append(('node_trouble', f"{e['node_name']}: full sysinfo timed out ({e['timestamp'][11:16]})"))
    old_keys = {frozenset(p) for p in old_pairs}
    new_keys = {frozenset(p) for p in new_pairs}
    seen = set()
    for s in states:
        key = frozenset((s['source_node'], s['target_node']))
        if not in_window(s['timestamp']) or (key, s['state']) in seen:
            continue
        a, b = sorted(key) if len(key) == 2 else (s['source_node'], s['target_node'])
        if key in old_keys and s['state'] == 'down':
            seen.add((key, s['state']))
            found.append(('link_failed', f"{s['link_type'] or ''} link {a} ↔ {b} went down ({s['timestamp'][11:16]}, "
                                         f"{'reported by the node' if s['origin'] == 'node_reported' else 'seen by the scanner'})"))
        elif key in new_keys and s['state'] == 'up':
            seen.add((key, s['state']))
            found.append(('link_restored', f"link {a} ↔ {b} came back ({s['timestamp'][11:16]})"))
    for v in old_views:
        why = _degraded(v)
        if why:
            found.append(('link_degraded', f"{v['a']} → {v['b']}: {why}"))
        elif v['before'] and not v['after'] and v['type'] == 'RF':
            found.append(('link_degraded', f"{v['a']} → {v['b']}: no tracker in the scans after {t0[11:16]}"))
    for v in new_views:
        why = _improved(v)
        if why:
            found.append(('better_route', f"{v['a']} → {v['b']}: {why}"))
    if '*' in old_seg + new_seg:
        found.append(('trace_incomplete', 'a hop near the change did not answer the trace'))
    internal = _site_internal(source, old['sig'], new['sig'], sites)
    if internal:
        found.append(('site_internal', f"both routes stay inside site {sites.get(div)}"))

    old_cost, new_cost = _segment_cost(old_views), _segment_cost(new_views)
    if old_cost and new_cost:
        gap = abs(old_cost - new_cost) / max(old_cost, new_cost) * 100
        if gap <= NEAR_TIE_PCT:
            found.append(('near_tie', f'Babel costs of the two segments within {gap:.0f}% '
                                      f'({old_cost:.0f} vs {new_cost:.0f})'))
    elif not [c for c, _ in found]:
        nocost = [v for v in old_views + new_views if not (v['after'] or v['before'] or {}).get('cost')]
        found.append(('near_tie', 'no link failed or got worse' + (
            f'; {len(nocost)} hop(s) have no recorded Babel cost (DtD, tunnel or not a direct link), '
            'so the costs could not be compared' if nocost else '')))
    order = list(CAUSE_LABELS)
    cause = min((c for c, _ in found), key=order.index) if found else 'unknown'
    return {
        'at': t1, 'prev_at': t0,
        'from_route': old['sig'], 'to_route': new['sig'],
        'divergence': {'node': div, 'rejoin': rejoin, 'old': old_seg, 'new': new_seg},
        'site_internal': internal,
        'cause': cause, 'cause_label': CAUSE_LABELS[cause],
        'evidence': [text for c, text in sorted(found, key=lambda f: order.index(f[0]))],
        'links': [{'side': side, 'a': v['a'], 'b': v['b'], 'type': v['type'],
                   'before': v['before'], 'after': v['after']}
                  for side, views in (('old', old_views), ('new', new_views)) for v in views],
        'cost_old': old_cost, 'cost_new': new_cost,
    }


def why_changed(path_id, hours=72, limit=300):
    """"Why did it change?" for one path: every route it used in the window
    (share, delay), each switch between routes with the hop where they part
    and the likely cause from events, link state changes and Babel costs
    around it, and how often it switches (voice calls feel each switch)."""
    ensure_tables()
    now = datetime.now()
    since = _now_s(now - timedelta(hours=hours))
    sites = {n: s for n, s in database.get_infrastructure_nodes().items() if s}
    with database.get_connection() as conn:
        p = conn.execute('SELECT * FROM route_paths WHERE id = ?', (path_id,)).fetchone()
        if not p:
            return None
        p = dict(p)
        s = _load_state(conn, path_id)
        base = s['detail'].get('baseline')
        runs = []
        for r in conn.execute('SELECT * FROM route_history WHERE path_id = ? AND timestamp >= ? ORDER BY id',
                              (path_id, since)):
            runs.append({'timestamp': r['timestamp'], 'ok': bool(r['ok']), 'source': r['source'],
                         'sig': json.loads(r['signature'] or '[]'), 'rtt_ms': r['rtt_ms']})
        ok_runs = [r for r in runs if r['ok']]
        groups = _route_groups(ok_runs, base)
        switches = [(a, b) for a, b in zip(ok_runs, ok_runs[1:]) if not _same(a['sig'], b['sig'])]
        link_types = _link_types(conn)
        events = [dict(r) for r in conn.execute(
            '''SELECT timestamp, event_type, node_name, details FROM events WHERE timestamp >= ?
               AND event_type IN ('node_reboot', 'node_offline', 'node_degraded')''',
            (_ts_s(since, -WHY_PAD),))]
        states = [dict(r) for r in conn.execute(
            '''SELECT timestamp, source_node, target_node, link_type, state, origin FROM link_state_log
               WHERE timestamp >= ? AND state IN ('down', 'up')''', (_ts_s(since, -WHY_PAD),))]
        changes = [_explain(conn, a, b, sites, link_types, events, states) for a, b in switches[-limit:]]
    for c in changes:
        g_from, g_to = _group_of(groups, c['from_route']), _group_of(groups, c['to_route'])
        c['from_group'], c['to_group'] = (g_from or {}).get('id'), (g_to or {}).get('id')
        c['to_usual'] = bool(g_to and g_to['usual'])
        rf, rt = (g_from or {}).get('rtt_median'), (g_to or {}).get('rtt_median')
        c['rtt_delta_ms'] = round(rt - rf, 1) if rf is not None and rt is not None else None
    changes.reverse()

    days = max(hours / 24.0, 1 / 24.0)
    internal_all = sum(1 for a, b in switches if _site_internal(b['source'] or a['source'], a['sig'], b['sig'], sites))
    counted = [c for c in changes if not c['site_internal']]
    common = [g for g in groups if g['runs'] >= 2 and g['rtt_median'] is not None]
    spread = (max(g['rtt_median'] for g in common) - min(g['rtt_median'] for g in common)) if len(common) > 1 else 0
    flaps_day = round((len(switches) - internal_all) / days, 1)
    notes = []
    if flaps_day >= config.ROUTE_FLAP_CHANGES:
        notes.append(f'Switches route about {flaps_day:g} times a day: a voice call through it can hear a short '
                     'gap or reordered packets at each switch.')
    if spread >= VOIP_RTT_SPREAD_MS:
        notes.append(f'Its routes differ by {spread:.0f} ms in delay (median), so each switch is also a delay '
                     'step the far end hears as jitter.')
    near = sum(1 for c in counted if c['cause'] == 'near_tie')
    if counted and near * 2 >= len(counted):
        notes.append(f'{near} of {len(counted)} switches had no failure behind them (near-ties): Babel is '
                     'choosing between routes of about equal cost. Making one clearly better (a stronger link, '
                     'or a tunnel/DtD cost change at the node where they part) would keep it steady.')
    counts = Counter(c['cause'] for c in changes)
    parts = Counter((c['divergence']['node'], tuple(c['divergence']['old']), tuple(c['divergence']['new']))
                    for c in counted)
    hotspots = [{'node': k[0], 'old': list(k[1]), 'new': list(k[2]), 'count': n}
                for k, n in parts.most_common(5) if n >= 2]
    return {
        'path': {**p, 'role': path_role(p)}, 'status': s['status'], 'baseline': base, 'hours': hours,
        'runs': len(runs), 'failed_runs': len(runs) - len(ok_runs),
        'routes': groups, 'changes': changes, 'switches': len(switches),
        'switches_internal': internal_all,
        'flaps_per_day': flaps_day, 'rtt_spread_ms': round(spread, 1),
        'voip_risk': flaps_day >= config.ROUTE_FLAP_CHANGES or spread >= VOIP_RTT_SPREAD_MS,
        'causes': [{'cause': c, 'label': CAUSE_LABELS[c], 'count': n} for c, n in counts.most_common()],
        'hotspots': hotspots, 'notes': notes,
    }

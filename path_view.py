"""
Path view (Diagnostics tab "Path View").

The route between two nodes, each hop coloured by the quality of the link it
crossed:
  - expected: lowest-cost path over the current links table (RF cost from link
    quality like ETX, DtD nearly free, tunnels expensive), what the mesh should
    use when everything is healthy;
  - live: a traceroute from the source node's CGI (starting node when the
    source is empty), gated by the starting node;
  - history: the routes Route Watch recorded for a watched path with the same
    ends (`route_history`), each with when it was first/last used.
Per segment: link type, current quality and SNR in both directions, and the
median quality over `hours` from `link_history` (RF only).

Read-only, except live() which runs a traceroute on the mesh.
"""

import heapq
import json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import database
import gateway
import mesh_diagnostics
import rf_stats
import route_watch

TS_FMT = '%Y-%m-%d %H:%M:%S'
TUNNEL_TYPES = {'WIREGUARD', 'WG', 'TUN', 'TUNNEL', 'VTUN'}
GOOD_Q, FAIR_Q = 80, 50
DTD_COST, TUNNEL_COST = 0.1, 3.0


def _now():
    return datetime.now()


def _grade(q):
    if q is None:
        return 'unknown'
    if q >= GOOD_Q:
        return 'good'
    if q >= FAIR_Q:
        return 'fair'
    return 'poor'


def starting_node_name():
    return mesh_diagnostics.find_seed_node(database.get_all_nodes())


def _links(conn):
    """{(a, b): row} for links that aren't removed, keyed by direction.

    `usable` = good, or dropped but seen within an hour of the newest sighting
    (a scanner that stopped, or one bad scan, shouldn't erase the mesh)."""
    newest = conn.execute("SELECT MAX(last_seen) FROM links").fetchone()[0]
    cutoff = None
    if newest:
        cutoff = (datetime.strptime(newest[:19], TS_FMT) - timedelta(hours=1)).strftime(TS_FMT)
    out = {}
    for r in conn.execute("SELECT source_node, target_node, link_type, quality, snr, status, rev_snr, last_seen "
                          "FROM links WHERE status != 'removed'"):
        row = dict(r)
        row['usable'] = row['status'] == 'good' or bool(cutoff and (row['last_seen'] or '') >= cutoff)
        out[(r['source_node'], r['target_node'])] = row
    return out


def _segment(conn, links, a, b, since):
    """Quality of the hop a -> b: both directions' current row plus the
    window median for RF."""
    fwd, rev = links.get((a, b)), links.get((b, a))
    types = {(x['link_type'] or '').upper() for x in (fwd, rev) if x}
    ltype = 'DTD' if 'DTD' in types else 'RF' if 'RF' in types else (sorted(types)[0] if types else None)
    seg = {'from': a, 'to': b, 'type': ltype,
           'quality': fwd.get('quality') if fwd else None, 'rev_quality': rev.get('quality') if rev else None,
           'snr': fwd.get('snr') if fwd else None, 'rev_snr': rev.get('snr') if rev else None,
           'status': (fwd or rev or {}).get('status'), 'known': bool(fwd or rev)}
    if ltype == 'RF':
        rows = [r[0] for r in conn.execute('''
            SELECT COALESCE(quality, lq) FROM link_history
            WHERE ((source_node = ? AND target_node = ?) OR (source_node = ? AND target_node = ?))
              AND timestamp >= ? AND COALESCE(quality, lq) IS NOT NULL''', (a, b, b, a, since))]
        seg['median_quality'] = round(median(rows)) if rows else None
        seg['samples'] = len(rows)
    qs = [q for q in (seg['quality'], seg['rev_quality']) if q is not None]
    if ltype == 'DTD':
        seg['grade'] = 'good' if seg['known'] else 'unknown'
    elif ltype in TUNNEL_TYPES:
        seg['grade'] = 'tunnel'
    else:
        seg['grade'] = _grade(min(qs) if qs else seg.get('median_quality'))
    return seg


def _weight(row):
    t = (row['link_type'] or '').upper()
    if t == 'DTD':
        return DTD_COST
    if t in TUNNEL_TYPES:
        return TUNNEL_COST
    q = row.get('quality') or 0
    return 100.0 / max(q, 5)


def expected_path(conn, links, source, target):
    """Lowest-cost path over current good links (Dijkstra), or None."""
    adj = defaultdict(list)
    for (a, b), row in links.items():
        if row['usable']:
            adj[a].append((b, _weight(row)))
            # Treat the link as usable both ways (the far end may not be polled).
            if (b, a) not in links:
                adj[b].append((a, _weight(row)))
    dist, prev = {source: 0.0}, {}
    heap = [(0.0, source)]
    while heap:
        d, n = heapq.heappop(heap)
        if n == target:
            break
        if d > dist.get(n, 1e18):
            continue
        for m, w in adj[n]:
            nd = d + w
            if nd < dist.get(m, 1e18):
                dist[m], prev[m] = nd, n
                heapq.heappush(heap, (nd, m))
    if target not in dist:
        return None
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    return list(reversed(path)), round(dist[target], 2)


def _segments_for(conn, links, names, since):
    segs = []
    prev = None
    for name in names:
        if prev and name != '*' and prev != '*':
            segs.append(_segment(conn, links, prev, name, since))
        elif prev:
            segs.append({'from': prev, 'to': name, 'type': None, 'grade': 'unknown', 'known': False})
        prev = name
    return segs


def _summary(segs):
    grades = [s['grade'] for s in segs]
    worst = None
    for g in ('poor', 'tunnel', 'fair', 'unknown', 'good'):
        if g in grades:
            worst = g
            break
    weakest = None
    rf = [s for s in segs if s.get('type') == 'RF']
    if rf:
        weakest = min(rf, key=lambda s: min([q for q in (s.get('quality'), s.get('rev_quality')) if q is not None]
                                             or [s.get('median_quality') or 0]))
    return {'worst': worst, 'hops': len(segs), 'tunnels': grades.count('tunnel'),
            'weakest': {'from': weakest['from'], 'to': weakest['to']} if weakest else None}


def _watched(conn, source, target):
    route_watch.ensure_tables()
    seed = starting_node_name()
    out = []
    for p in conn.execute('SELECT * FROM route_paths WHERE target = ?', (target,)):
        p = dict(p)
        src = p['source'] or seed
        if src == source:
            out.append(p)
    return out


def view(source, target, hours=24):
    """Expected path, watched routes with history, segment qualities."""
    source = (source or '').strip().lower() or starting_node_name()
    target = (target or '').strip().lower()
    if not source or not target:
        raise ValueError('source and target are required')
    since = (_now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        links = _links(conn)
        known = {n for pair in links for n in pair}
        out = {'source': source, 'target': target, 'hours': hours,
               'known': {'source': source in known, 'target': target in known}}
        exp = expected_path(conn, links, source, target)
        if exp:
            names, cost = exp
            segs = _segments_for(conn, links, names, since)
            out['expected'] = {'nodes': names, 'cost': cost, 'segments': segs, 'summary': _summary(segs)}
        else:
            out['expected'] = None

        history = []
        for p in _watched(conn, source, target):
            routes = {}
            for r in conn.execute('''SELECT timestamp, signature, hops, rtt_ms FROM route_history
                                     WHERE path_id = ? AND ok = 1 AND timestamp >= ? ORDER BY id''',
                                  (p['id'], since)):
                sig = tuple(json.loads(r['signature'] or '[]'))
                entry = routes.setdefault(sig, {'first': r['timestamp'], 'last': r['timestamp'], 'runs': 0,
                                                'rtt': [], 'hops': json.loads(r['hops'] or '[]')})
                entry['last'] = r['timestamp']
                entry['runs'] += 1
                if r['rtt_ms'] is not None:
                    entry['rtt'].append(r['rtt_ms'])
            for sig, e in routes.items():
                names = [source] + list(sig)
                segs = _segments_for(conn, links, names, since)
                history.append({'path_id': p['id'], 'label': p['label'], 'nodes': names, 'first': e['first'],
                                'last': e['last'], 'runs': e['runs'],
                                'rtt_ms': round(median(e['rtt']), 1) if e['rtt'] else None,
                                'hops': e['hops'], 'segments': segs, 'summary': _summary(segs)})
        history.sort(key=lambda h: h['last'], reverse=True)
        out['history'] = history
        out['watched'] = [{'id': p['id'], 'label': p['label'], 'enabled': bool(p['enabled'])}
                          for p in _watched(conn, source, target)]
    return out


def live(source, target, hours=24, trace_fn=None):
    """Traceroute source -> target now and grade every hop. Returns
    {'error'} when the starting node is unreachable or the trace fails."""
    source = (source or '').strip().lower()
    target = (target or '').strip().lower()
    if not target:
        raise ValueError('target is required')
    if not gateway.is_reachable():
        return {'error': gateway.unreachable_message(), 'gateway_unreachable': True}
    source_ip = None
    if source:
        node = database.get_node(source)
        source_ip = node.get('ip') if node else None
        if not source_ip:
            return {'error': f'no IP known for {source}'}
    trace = (trace_fn or rf_stats.traceroute_via_aredn)(target, source_node_ip=source_ip)
    if not trace:
        if gateway.failure_is_ours():
            return {'error': gateway.unreachable_message(), 'gateway_unreachable': True}
        return {'error': f'traceroute to {target} failed'}
    src_name = source or starting_node_name()
    since = (_now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        links = _links(conn)
        infra = set(database.get_infrastructure_nodes())
        hops = route_watch.annotate_hops(trace['hops'], src_name, infra, route_watch._link_types(conn))
        names = [src_name] + [h['name'] for h in hops]
        segs = _segments_for(conn, links, names, since)
    for seg, hop in zip(segs, hops):
        seg['ms'] = hop['ms']
        seg['via'] = hop['via']
        if hop['tunnel']:
            seg['grade'] = 'tunnel'
    reached = bool(hops) and hops[-1]['name'] == target
    return {'source': src_name, 'target': target, 'timestamp': _now().strftime(TS_FMT), 'reached': reached,
            'nodes': names, 'hops': hops, 'segments': segs, 'summary': _summary(segs)}

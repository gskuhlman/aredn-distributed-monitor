"""
Babel cost view (Diagnostics tab "Babel Costs"): where routing doesn't do
what the RF links suggest.

From each polled node's LQM trackers (links.raw_tracker: rxcost, txcost,
babel_metric = the metric of the node's selected route to that neighbour,
babel_route_count, routable) and Route Watch traces:
  unroutable      a good RF link (quality >= GOOD_QUALITY, SNR >= GOOD_SNR)
                  that Babel won't route over (routable false or an infinite
                  cost);
  detour          the node's route to an RF neighbour costs much more than the
                  direct link (metric > DETOUR_FACTOR x link cost + 100):
                  traffic to the neighbour goes another way;
  tunnel_cheaper  a node whose tunnels cost less than its good RF links
                  (AREDN tunnels default to a lower Babel cost than one RF
                  hop), so a destination reachable both ways goes over the
                  internet;
  tunnel_route    a Route Watch path whose last trace used a tunnel while a
                  route over good RF/DtD links exists (path_view, tunnels left
                  out).
Plus a per-node neighbour table (type, LQ, quality, SNR, costs, metric, route
count). Read-only.
"""

import json
from collections import defaultdict
from datetime import datetime

import database
import path_view

TS_FMT = '%Y-%m-%d %H:%M:%S'
GOOD_QUALITY = 80
GOOD_SNR = 20
DETOUR_FACTOR = 1.5
INFINITE = 65535
RF_GOOD_SEGMENT = 70


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _type(t):
    t = (t or '').upper()
    return 'TUNNEL' if t in path_view.TUNNEL_TYPES else t


def neighbours(conn):
    """{node: [neighbour rows]} from current trackers."""
    out = defaultdict(list)
    for r in conn.execute('''SELECT source_node, target_node, link_type, quality, snr, raw_tracker, last_seen
                             FROM links WHERE status = 'good' '''):
        try:
            t = json.loads(r['raw_tracker']) if r['raw_tracker'] else {}
        except ValueError:
            t = {}
        out[r['source_node']].append({
            'node': r['target_node'], 'type': _type(r['link_type']), 'link_type': r['link_type'],
            'quality': _num(r['quality']), 'snr': _num(r['snr']), 'lq': _num(t.get('lq')),
            'rxcost': _num(t.get('rxcost')), 'txcost': _num(t.get('txcost')),
            'metric': _num(t.get('babel_metric')), 'routes': t.get('babel_route_count'),
            'routable': t.get('routable'), 'last_seen': r['last_seen']})
    for rows in out.values():
        rows.sort(key=lambda x: (x['type'] != 'RF', x['type'], x['node']))
    return out


def _link_cost(n):
    """Babel's cost of the direct link: ETX-style rxcost x txcost / 256 (the
    metric a direct route shows), never below the larger of the two."""
    costs = [c for c in (n['rxcost'], n['txcost']) if c is not None]
    if not costs:
        return None
    if len(costs) == 2:
        return max(max(costs), costs[0] * costs[1] / 256.0)
    return costs[0]


def _findings(nbrs, traces, conn):
    import ghost_trackers
    ghosts = ghost_trackers.excluded_pairs(conn)   # radio off / cabled: not RF neighbours
    f = []
    for node, rows in nbrs.items():
        rf_good = [n for n in rows if n['type'] == 'RF' and (node, n['node']) not in ghosts
                   and (n['quality'] or 0) >= GOOD_QUALITY
                   and (n['snr'] is None or n['snr'] >= GOOD_SNR) and not n['node'].startswith('mac-')]
        for n in rf_good:
            cost = _link_cost(n)
            if n['routable'] is False or (cost is not None and cost >= INFINITE):
                f.append({'severity': 'warning', 'kind': 'unroutable', 'nodes': [node, n['node']],
                          'title': f"{node} won't route over its good RF link to {n['node']}",
                          'detail': f"Quality {n['quality']:.0f}%, SNR {n['snr']}, but Babel "
                                    + ('marks it unroutable' if n['routable'] is False else f'cost {cost:.0f}')
                                    + '. Check LQM blocks (distance/SNR limits) and the neighbour\'s identity.'})
            elif cost and n['metric'] and n['metric'] > DETOUR_FACTOR * cost + 100:
                f.append({'severity': 'info', 'kind': 'detour', 'nodes': [node, n['node']],
                          'title': f"{node} reaches {n['node']} another way",
                          'detail': f"The direct RF link costs {cost:.0f} (quality {n['quality']:.0f}%), but the "
                                    f"selected route has metric {n['metric']:.0f}: traffic to {n['node']} "
                                    "takes a longer or tunnel path."})
        tunnels = [n for n in rows if n['type'] == 'TUNNEL' and n['rxcost'] is not None and n['routable'] is not False]
        rf_costs = [_link_cost(n) for n in rf_good if _link_cost(n)]
        if tunnels and rf_costs:
            t_min = min(n['rxcost'] for n in tunnels)
            r_min = min(rf_costs)
            if t_min < r_min:
                f.append({'severity': 'info', 'kind': 'tunnel_cheaper', 'nodes': [node],
                          'title': f"{node}: tunnels cost less than its best RF link",
                          'detail': f"Tunnel cost {t_min:.0f} vs. RF {r_min:.0f} "
                                    f"({len(tunnels)} tunnel(s), {len(rf_good)} good RF link(s)). Anything "
                                    "reachable both ways goes over the internet; raise the tunnel metric on "
                                    "the node (Tunnels > metric) if RF should be preferred."})
    links = path_view._links(conn)
    no_tunnels = {k: v for k, v in links.items() if _type(v['link_type']) != 'TUNNEL'}
    for tr in traces:
        hops = tr['hops']
        if not any(h.get('tunnel') or h.get('via') in ('Wireguard', 'Tunnel', 'tunnel') for h in hops):
            continue
        src = tr['source']
        route = path_view.expected_path(conn, no_tunnels, src, tr['target']) if src else None
        if not route:
            continue
        names, _ = route
        segs = [path_view._segment(conn, no_tunnels, a, b, tr['timestamp']) for a, b in zip(names, names[1:])]
        qs = [min(q for q in (s['quality'], s['rev_quality']) if q is not None)
              for s in segs if s['type'] == 'RF' and (s['quality'] is not None or s['rev_quality'] is not None)]
        if all(q >= RF_GOOD_SEGMENT for q in qs):
            f.append({'severity': 'warning', 'kind': 'tunnel_route', 'nodes': [src, tr['target']],
                      'title': f"Route {tr['label'] or src + ' → ' + tr['target']} uses a tunnel",
                      'detail': f"Last trace ({tr['timestamp'][5:16]}): "
                                + ' → '.join(h.get('name') or h.get('ip') or '?' for h in hops)
                                + f". An RF/DtD route exists ({' → '.join(names)}"
                                + (f", weakest RF hop {min(qs):.0f}%" if qs else '') + ').',
                      'route': names})
    order = {'warning': 0, 'info': 1}
    f.sort(key=lambda x: (order.get(x['severity'], 9), x['kind'], x['nodes']))
    return f


def _traces(conn):
    out = []
    for p in conn.execute('SELECT id, source, target, label FROM route_paths WHERE enabled = 1'):
        r = conn.execute('''SELECT timestamp, source, hops FROM route_history
                            WHERE path_id = ? AND ok = 1 ORDER BY id DESC LIMIT 1''', (p['id'],)).fetchone()
        if not r:
            continue
        try:
            hops = json.loads(r['hops'] or '[]')
        except ValueError:
            hops = []
        out.append({'path_id': p['id'], 'label': p['label'], 'source': r['source'] or p['source'],
                    'target': p['target'], 'timestamp': r['timestamp'], 'hops': hops})
    return out


def report(node=None):
    with database.get_connection() as conn:
        nbrs = neighbours(conn)
        try:
            traces = _traces(conn)
        except Exception:
            traces = []
        findings = _findings(nbrs, traces, conn)
    if node:
        findings = [x for x in findings if node in x['nodes']]
    nodes = [{'node': n, 'neighbours': rows,
              'rf': sum(1 for x in rows if x['type'] == 'RF'),
              'tunnels': sum(1 for x in rows if x['type'] == 'TUNNEL')}
             for n, rows in sorted(nbrs.items()) if not node or n == node]
    counts = defaultdict(int)
    for x in findings:
        counts[x['kind']] += 1
    return {'generated_at': datetime.now().strftime(TS_FMT), 'findings': findings,
            'counts': dict(counts), 'nodes': nodes}

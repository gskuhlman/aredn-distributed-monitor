"""
Weakest-link / single-point-of-failure ranking (Diagnostics tab "Single
Points of Failure", Channel Map overlay).

Over the current links (good, or seen within an hour of the newest sighting,
see `path_view._links`) and the scan scope from the starting node
(`mesh_diagnostics.compute_scope`):
  - links: every node pair whose loss alone cuts nodes off from the starting
    node (a bridge: removing all links between the pair), with how many polled
    nodes are behind it, how many of those are infrastructure, the link types
    and current/median quality, and a risk = nodes behind x weakness
    (1 - quality/100, tunnels counted as 0.5, DtD as 0.1);
  - nodes: every node whose failure cuts others off (an articulation point).
With exclude_tunnels the tunnels are left out first (internet down), so the
ranking shows what the RF/DtD backbone alone has to carry.
Positions are included so the Channel Map can draw the ranked links.

Read-only. Brute-force BFS per candidate is fine at mesh sizes.
"""

from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import median

import database
import mesh_diagnostics
import path_view

TS_FMT = '%Y-%m-%d %H:%M:%S'
TYPE_WEAKNESS = {'DTD': 0.1, 'TUNNEL': 0.5}


def _graph(exclude_tunnels=False):
    nodes = database.get_all_nodes()
    by_name = {n['name']: n for n in nodes}
    boundaries = database.get_scan_boundaries()
    with database.get_connection() as conn:
        rows = path_view._links(conn)
    usable = [dict(r, source_node=a, target_node=b) for (a, b), r in rows.items() if r['usable']
              and not (exclude_tunnels and (r['link_type'] or '').upper() in path_view.TUNNEL_TYPES)]
    seed, scope = mesh_diagnostics.compute_scope(nodes, usable, boundaries)
    adj = defaultdict(set)
    pairs = defaultdict(list)
    for l in usable:
        a, b = l['source_node'], l['target_node']
        if a in scope and b in scope and a != b:
            adj[a].add(b)
            adj[b].add(a)
            pairs[tuple(sorted((a, b)))].append(l)
    polled = {n for n in scope if (by_name.get(n) or {}).get('is_active')}
    return seed, scope, adj, pairs, polled, by_name


def _reach(seed, adj, skip_node=None, skip_edge=None):
    seen = {seed}
    q = deque([seed])
    while q:
        n = q.popleft()
        for p in adj.get(n, ()):
            if p == skip_node or p in seen:
                continue
            if skip_edge and {n, p} == skip_edge:
                continue
            seen.add(p)
            q.append(p)
    return seen


def _kind(types):
    # A cabled pair can keep an old RF tracker: the cable is what carries it.
    t = {x.upper() for x in types}
    if 'DTD' in t:
        return 'DTD'
    if 'RF' in t:
        return 'RF'
    return 'TUNNEL'


def report(hours=24, exclude_tunnels=False):
    """exclude_tunnels: the mesh as it would be with the internet down
    (WireGuard/legacy tunnels gone): what the RF/DtD backbone alone carries."""
    seed, scope, adj, pairs, polled, by_name = _graph(exclude_tunnels)
    infra = database.get_infrastructure_nodes()
    out = {'generated_at': datetime.now().strftime(TS_FMT), 'seed': seed, 'hours': hours,
           'exclude_tunnels': bool(exclude_tunnels),
           'nodes_in_scope': len(polled), 'links': [], 'nodes': []}
    if seed is None:
        out['error'] = 'starting node not found among the scanned nodes'
        return out
    base = _reach(seed, adj)
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)

    def pos(name):
        n = by_name.get(name) or {}
        return (n.get('lat'), n.get('lon')) if n.get('lat') is not None and n.get('lon') is not None else None

    with database.get_connection() as conn:
        for pair, rows in pairs.items():
            if not (set(pair) <= base):
                continue
            kept = _reach(seed, adj, skip_edge=set(pair))
            lost = (base - kept) & polled
            if not lost:
                continue
            kind = _kind([r['link_type'] or '' for r in rows])
            qs = [r['quality'] for r in rows if (r['link_type'] or '').upper() == 'RF' and r['quality'] is not None]
            med = None
            if kind == 'RF':
                vals = [v[0] for v in conn.execute('''
                    SELECT COALESCE(quality, lq) FROM link_history
                    WHERE ((source_node = ? AND target_node = ?) OR (source_node = ? AND target_node = ?))
                      AND timestamp >= ? AND COALESCE(quality, lq) IS NOT NULL''',
                    (pair[0], pair[1], pair[1], pair[0], since))]
                med = round(median(vals)) if vals else None
            if kind == 'RF':
                q = min(qs) if qs else med
                weakness = 1 - (q if q is not None else 50) / 100
            else:
                q = None
                weakness = TYPE_WEAKNESS.get(kind, 0.5)
            # The side cut off: the end that is no longer reachable.
            far = pair[0] if pair[0] not in kept else pair[1]
            out['links'].append({
                'a': pair[0], 'b': pair[1], 'far_end': far, 'type': kind,
                'types': sorted({(r['link_type'] or '?') for r in rows}),
                'quality': min(qs) if qs else None, 'median_quality': med,
                'behind': len(lost), 'behind_infra': sum(1 for n in lost if n in infra),
                'nodes_behind': sorted(lost), 'risk': round(len(lost) * weakness, 1),
                'a_pos': pos(pair[0]), 'b_pos': pos(pair[1]),
                'infrastructure': pair[0] in infra or pair[1] in infra})

    for node in base:
        if node == seed or len(adj.get(node, ())) < 2:
            continue
        lost = (base - _reach(seed, adj, skip_node=node) - {node}) & polled
        if lost:
            out['nodes'].append({'name': node, 'behind': len(lost), 'behind_infra': sum(1 for n in lost if n in infra),
                                 'nodes_behind': sorted(lost), 'infrastructure': node in infra,
                                 'pos': pos(node)})
    out['links'].sort(key=lambda x: (-x['risk'], -x['behind'], x['a']))
    out['nodes'].sort(key=lambda x: (-x['behind'], x['name']))
    out['counts'] = {'links': len(out['links']), 'nodes': len(out['nodes']),
                     'rf_links': sum(1 for x in out['links'] if x['type'] == 'RF'),
                     'max_behind': max([x['behind'] for x in out['links']] or [0])}
    return out


def node_down(name, exclude_tunnels=False):
    """What loses connectivity to the starting node if `name` goes down
    (Map page overlay). Polled nodes cut off, with and without tunnels."""
    seed, scope, adj, pairs, polled, by_name = _graph(exclude_tunnels)
    if seed is None:
        return {'error': 'starting node not found among the scanned nodes'}
    if name == seed:
        return {'node': name, 'seed': seed, 'is_seed': True, 'lost': sorted(polled - {name}),
                'exclude_tunnels': bool(exclude_tunnels)}
    base = _reach(seed, adj)
    lost = (base - _reach(seed, adj, skip_node=name) - {name}) & polled
    infra = database.get_infrastructure_nodes()
    return {'node': name, 'seed': seed, 'is_seed': False, 'exclude_tunnels': bool(exclude_tunnels),
            'reachable_now': name in base, 'lost': sorted(lost),
            'lost_infra': sorted(n for n in lost if n in infra)}

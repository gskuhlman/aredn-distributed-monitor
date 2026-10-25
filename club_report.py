"""
Club report (/report/club?days=30): a one-page printable summary for
meetings (Print / Save as PDF in the browser).

Sections, each built independently so one failure doesn't lose the page:
  headline   starting-node availability, nodes answering, really down /
             intermittent, warnings, reboots (weekly_report._headline);
  map        located nodes and current RF/DtD links drawn as an SVG
             schematic (equirectangular around the densest cluster);
  health     weakest links and nodes by health score;
  changes    changes made (change log, manual and planner entries) with their
             automatic before/after verdicts, and finished rollouts;
  new nodes  welcomes opened in the period;
  next       what's next: open rollout steps, upcoming maintenance, the
             Infrastructure Review's critical findings, owners with the most
             setup fixes (hygiene), services down.
Read-only.
"""

import logging
import math
from datetime import datetime, timedelta

import database

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
MAP_W, MAP_H = 640, 360
CLUSTER_KM = 40


def _section(out, key, fn):
    try:
        out[key] = fn()
    except Exception as exc:
        logger.exception('club report section %s failed', key)
        out[key] = {'error': f'{type(exc).__name__}: {exc}'}


def _map():
    import link_diagnostics as ld
    with database.get_connection() as conn:
        nodes = {r['name']: dict(r) for r in conn.execute(
            'SELECT name, lat, lon, is_active FROM nodes WHERE lat IS NOT NULL AND lon IS NOT NULL')}
        links = [dict(r) for r in conn.execute('''
            SELECT DISTINCT source_node, target_node, link_type, quality FROM links
            WHERE status = 'good' AND UPPER(link_type) IN ('RF', 'DTD')''')]
    infra = database.get_infrastructure_nodes()
    pts = [n for n in nodes.values() if (n['lat'] or n['lon'])]
    if not pts:
        return {'svg_nodes': [], 'svg_links': [], 'width': MAP_W, 'height': MAP_H}
    # Densest cluster, so far tunnel ends and bad coordinates don't shrink the map.
    best = max(pts, key=lambda p: sum(1 for q in pts if (ld._haversine_m(p['lat'], p['lon'], q['lat'], q['lon'])
                                                          or 0) <= CLUSTER_KM * 1000))
    near = [q for q in pts if (ld._haversine_m(best['lat'], best['lon'], q['lat'], q['lon']) or 0) <= CLUSTER_KM * 1000]
    lat0, lat1 = min(p['lat'] for p in near), max(p['lat'] for p in near)
    lon0, lon1 = min(p['lon'] for p in near), max(p['lon'] for p in near)
    k = math.cos(math.radians((lat0 + lat1) / 2))
    span_x, span_y = max((lon1 - lon0) * k, 1e-3), max(lat1 - lat0, 1e-3)
    scale = min((MAP_W - 40) / span_x, (MAP_H - 40) / span_y)

    def xy(p):
        return (round(20 + (p['lon'] - lon0) * k * scale, 1), round(MAP_H - 20 - (p['lat'] - lat0) * scale, 1))
    shown = {p['name']: xy(p) for p in near}
    svg_links, seen = [], set()
    for l in links:
        a, b = l['source_node'], l['target_node']
        key = tuple(sorted((a, b)))
        if key in seen or a not in shown or b not in shown:
            continue
        seen.add(key)
        rf = (l['link_type'] or '').upper() == 'RF'
        q = l['quality']
        color = '#2874a6' if not rf else ('#27ae60' if (q or 0) >= 80 else '#d4ac0d' if (q or 0) >= 50 else '#c0392b')
        svg_links.append({'x1': shown[a][0], 'y1': shown[a][1], 'x2': shown[b][0], 'y2': shown[b][1],
                          'color': color, 'rf': rf})
    svg_nodes = [{'name': n, 'x': x, 'y': y, 'infra': n in infra, 'up': bool(nodes[n]['is_active'])}
                 for n, (x, y) in sorted(shown.items())]
    return {'svg_nodes': svg_nodes, 'svg_links': svg_links, 'width': MAP_W, 'height': MAP_H,
            'off_map': len(pts) - len(near), 'radius_km': CLUSTER_KM}


def _changes(start, end):
    import change_verdict
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT id, timestamp, node_name, peer_node, category, description, source FROM change_log
            WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp''', (start, end))]
    verdicts = {v['change_id']: v for v in change_verdict.list_verdicts(change_ids=[r['id'] for r in rows])} if rows else {}
    for r in rows:
        v = verdicts.get(r['id'])
        r['verdict'] = v['verdict'] if v else None
        r['verdict_text'] = v['text'] if v else None
    return rows


def _rollouts(start):
    import rollout
    return [{'id': r['id'], 'name': r['name'], 'status': r['status'], 'progress': r['progress'],
             'finished_at': r['finished_at'], 'created_at': r['created_at']}
            for r in rollout.list_rollouts() if r['created_at'] >= start or r['status'] == 'active']


def _new_nodes(start):
    import welcome
    return [{'node': w['node_name'], 'discovered_at': w['discovered_at'], 'score': w['score']}
            for w in welcome.list_welcomes(include_past=True) if w['discovered_at'] >= start]


def _next():
    import hygiene
    import maintenance
    import rollout
    import service_check
    out = {}
    out['rollout_steps'] = [{'rollout': r['name'], 'node': s['node_name'], 'status': s['status'],
                             'detail': s.get('detail'),
                             'to': 'off' if s['turn_off'] else f"{s['new_frequency']:g}/{(s['new_chanbw'] or 0):g} MHz"}
                            for r in rollout.list_rollouts(include_finished=False) for s in r['steps']
                            if s['status'] not in ('verified', 'skipped')][:15]
    now = datetime.now().strftime(TS_FMT)
    out['maintenance'] = [{'start': w['start_time'], 'end': w['end_time'], 'note': w.get('note')}
                          for w in maintenance.list_windows(days=30) if w['end_time'] >= now][:8]
    try:
        import infra_audit
        r = infra_audit.run_review(refresh=False)
        out['critical'] = [f['title'] for f in r.get('findings', []) if f['severity'] == 'critical'][:8]
    except Exception:
        out['critical'] = []
    h = hygiene.report()
    out['owners'] = [o for o in h['owners'] if o['fixes']][:6]
    out['services_down'] = [{'node': s['node_name'], 'service': s['service_name'], 'error': s['last_error']}
                            for s in service_check.report()['services'] if s['state'] == 'alarm'][:10]
    return out


def build(days=30):
    import weekly_report as wr
    days = max(1, min(int(days or 30), 92))
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    s, e = start.strftime(TS_FMT), end.strftime(TS_FMT)
    out = {'start': s, 'end': e, 'days': days, 'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M')}
    _section(out, 'headline', lambda: wr._headline(start, end))
    _section(out, 'map', _map)
    _section(out, 'worst_links', lambda: wr._worst(start, end, 'link', 6))
    _section(out, 'worst_nodes', lambda: wr._worst(start, end, 'node', 6))
    _section(out, 'changes', lambda: _changes(s, e))
    _section(out, 'rollouts', lambda: _rollouts(s))
    _section(out, 'new_nodes', lambda: _new_nodes(s))
    _section(out, 'next', _next)
    return out

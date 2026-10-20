"""
Site dashboard (`/sites/<code>`, e.g. /sites/COL).

One page per infrastructure site: the site's radios (infrastructure nodes
with that site code plus other radios within `infra_audit.SITE_METERS` of
them) with their channel, antenna and health; a spectrum chart (pairs graded
by `infra_audit.pair_relation`); clients per sector (current RF neighbours
outside the site, quality/SNR both ways and the window median); uplink paths
(Route Watch paths touching the site and the expected path from the starting
node); what the site depends on with the internet down (`spof`); events,
health scores, notes and maintenance.

Read-only; built from the other modules.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import database
import health_score
import infra_audit
import maintenance
import notes
import path_view
import route_watch
import spof

TS_FMT = '%Y-%m-%d %H:%M:%S'
EVENT_TYPES = ('node_offline', 'node_online', 'node_reboot', 'node_degraded', 'link_dropped', 'link_restored',
               'radio_config_change', 'frequency_change', 'firmware_change', 'noise_jump', 'one_way_link',
               'one_way_link_cleared', 'route_detour', 'route_changed', 'route_restored')


def sites():
    """[{code, name, nodes}] for every infrastructure site."""
    names = database.get_site_names()
    by_site = defaultdict(list)
    for node, code in database.get_infrastructure_nodes().items():
        if code:
            by_site[code].append(node)
    return [{'code': c, 'name': names.get(c, c), 'nodes': sorted(n)} for c, n in sorted(by_site.items())]


def build(code, hours=24):
    code = (code or '').strip().upper()
    infra = database.get_infrastructure_nodes()
    members = sorted(n for n, s in infra.items() if (s or '').upper() == code)
    if not members:
        return None
    now = datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)

    with database.get_connection() as conn:
        radios = infra_audit._radios(conn)
        links = path_view._links(conn)
        site_radios = [radios.get(n) or {'name': n} for n in members]
        anchor = next((r for r in site_radios if r.get('lat') is not None), None)
        others = [r for r in radios.values() if r['name'] not in members and anchor
                  and infra_audit._rf_on(r) and (infra_audit._dist(anchor, r) or 1e9) <= infra_audit.SITE_METERS]
        at_site = {r['name'] for r in site_radios} | {r['name'] for r in others}

        health = {}
        for name, polls, ok, resp in conn.execute(f'''
                SELECT node_name, COUNT(*), SUM(CASE WHEN reachable = 1 THEN 1 ELSE 0 END), AVG(response_ms)
                FROM node_health_history
                WHERE timestamp >= ? AND probe_via IS NULL AND node_name IN ({','.join('?' * len(at_site))})
                GROUP BY node_name''', [since] + sorted(at_site)):
            health[name] = {'polls': polls, 'ok': ok, 'reach_pct': round(ok / polls * 100) if polls else None,
                            'response_ms': round(resp) if resp is not None else None}
        node_rows = {r['name']: dict(r) for r in conn.execute(
            f"SELECT name, uptime_seconds, load1, mem_free, mem_total, is_active, last_seen FROM nodes "
            f"WHERE name IN ({','.join('?' * len(at_site))})", sorted(at_site))}
        events = [dict(r) for r in conn.execute(f'''
            SELECT timestamp, event_type, node_name, details, severity FROM events
            WHERE timestamp >= ? AND node_name IN ({','.join('?' * len(at_site))})
              AND event_type IN ({','.join('?' * len(EVENT_TYPES))})
            ORDER BY timestamp DESC LIMIT 200''', [since] + sorted(at_site) + list(EVENT_TYPES))]
        reboots = defaultdict(int)
        for e in events:
            if e['event_type'] == 'node_reboot':
                reboots[e['node_name']] += 1

        # Clients per sector: current RF neighbours outside the site.
        sectors = []
        for name in sorted(at_site):
            clients = []
            for (a, b), row in links.items():
                if a != name or (row['link_type'] or '').upper() != 'RF' or not row['usable'] or b in at_site:
                    continue
                if b.startswith('mac-'):
                    continue
                rev = links.get((b, a))
                vals = [v[0] for v in conn.execute('''
                    SELECT COALESCE(quality, lq) FROM link_history
                    WHERE source_node = ? AND target_node = ? AND timestamp >= ?
                      AND COALESCE(quality, lq) IS NOT NULL''', (a, b, since))]
                peer = radios.get(b) or {}
                clients.append({'name': b, 'quality': row['quality'], 'snr': row['snr'],
                                'rev_quality': rev['quality'] if rev else None,
                                'rev_snr': rev['snr'] if rev else row.get('rev_snr'),
                                'median_quality': round(median(vals)) if vals else None,
                                'distance_km': round(infra_audit._dist(radios.get(name) or {}, peer) / 1000, 2)
                                if infra_audit._dist(radios.get(name) or {}, peer) else None,
                                'infrastructure': b in infra, 'site': infra.get(b)})
            if clients:
                clients.sort(key=lambda c: (c['quality'] if c['quality'] is not None else 101))
                sectors.append({'name': name, 'clients': clients})

    radio_out = []
    for r in site_radios + [dict(r, not_infrastructure=True) for r in others]:
        span = infra_audit.rf_span_info({'rf_frequency': r.get('frequency'), 'rf_chanbw': r.get('chanbw'),
                                         'rf_channel': r.get('channel')}) if r.get('frequency') else None
        nr = node_rows.get(r['name'], {})
        radio_out.append({**r, 'span': span, 'health': health.get(r['name']), 'reboots': reboots.get(r['name'], 0),
                          'uptime_seconds': nr.get('uptime_seconds'), 'load1': nr.get('load1'),
                          'mem_free': nr.get('mem_free'), 'mem_total': nr.get('mem_total'),
                          'is_active': nr.get('is_active', r.get('is_active')), 'last_seen': nr.get('last_seen')})
    radio_out.sort(key=lambda r: (r.get('frequency') or 1e9, r['name']))

    pairs = []
    on = [r for r in radio_out if infra_audit._rf_on(r)]
    for i, a in enumerate(on):
        for b in on[i + 1:]:
            pairs.append({'a': a['name'], 'b': b['name'], 'gap_mhz': infra_audit.spectral_gap(a, b),
                          'relation': infra_audit.pair_relation(a, b)})

    # Uplinks: watched paths touching the site, and the expected path from the starting node.
    rw = route_watch.list_report(hours=hours)
    seed = path_view.starting_node_name()
    paths = [p for p in rw['paths'] if p['enabled'] and (p['target'] in at_site or (p['source'] or seed) in at_site)]
    rep = next((n for n in members if (node_rows.get(n) or {}).get('is_active')), members[0])
    expected = None
    if seed and seed not in at_site:
        try:
            expected = path_view.view('', rep, hours=hours).get('expected')
        except ValueError:
            expected = None

    # What the site depends on with the internet down.
    dep = spof.report(hours=hours, exclude_tunnels=True)
    depends = [l for l in dep.get('links', []) if at_site & set(l['nodes_behind'])]
    carries = [l for l in dep.get('links', []) if (l['a'] in at_site or l['b'] in at_site)
               and not (at_site & set(l['nodes_behind']))]

    scores = health_score.scores((now - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M'),
                                 now.strftime('%Y-%m-%dT%H:%M'), kind='node', limit=10000)
    score_rows = [s for s in scores['items'] if s['name'] in at_site]

    windows = [w for w in maintenance.list_windows(days=7)
               if w['status'] in ('active', 'grace', 'upcoming')
               and (w['scope'] == 'all' or any(maintenance._node_covered(w, n) for n in at_site))]

    return {
        'code': code, 'name': database.get_site_names().get(code, code), 'generated_at': now.strftime(TS_FMT),
        'hours': hours, 'members': members, 'radios': radio_out, 'pairs': pairs, 'sectors': sectors,
        'paths': paths, 'expected_path': expected, 'starting_node': seed,
        'depends_on': depends, 'carries': carries,
        'scores': {'window': scores['window'], 'items': score_rows},
        'events': events[:60], 'event_counts': _count(events),
        'notes': notes.list_notes(site=code, limit=50),
        'maintenance': windows,
        'sites': sites(),
    }


def _count(events):
    out = defaultdict(int)
    for e in events:
        out[e['event_type']] += 1
    return dict(out)

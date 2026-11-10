"""
Noise source finder (TODO #56). When the noise floor jumps on several radios
at about the same time (Link Watch `noise_jump` events within
NOISE_SOURCE_WINDOW_MINUTES of each other, or radios whose noise sits
NOISE_JUMP_DB+ above baseline right now), their antenna beams are
intersected to point at the likely interference area.

A grid (1 km cells, NOISE_SOURCE_RADIUS_KM around the radios) is scored: +1
for each jumped radio whose beam (channel_map beamwidth, the radio's own
azimuth) covers the cell (an omni counts 0.5 within 10 km), and -0.5 for each
quiet radio on an overlapping channel whose beam covers it (it would have
heard the source too). The best-scoring cells give the estimate (centroid and
spread), with the channel span every jumped radio shares (the interferer is
probably inside it). Needs 2+ located radios with at least one directional.
`check()` after each scan logs `noise_source` (warning) once per new
cluster. Read-only otherwise; the Channel Map draws `estimate` and the beams.
"""

import math
from datetime import datetime, timedelta

import channel_map
import config
import database
import link_diagnostics as ld

TS_FMT = '%Y-%m-%d %H:%M:%S'
CELL_KM = 1.0
OMNI_KM = 10.0
TOP_SHARE = 0.9          # cells scoring at least this share of the best make the area

_seen_clusters = set()


def _jumps(conn, since):
    rows = conn.execute('''SELECT timestamp, node_name, details FROM events WHERE event_type = 'noise_jump'
                           AND timestamp >= ? ORDER BY timestamp''', (since,)).fetchall()
    return [dict(r) for r in rows]


def clusters_of(jumps, window_minutes):
    """Group jump events: each cluster spans <= window from its first event;
    one entry per radio (its first jump)."""
    out, cur = [], None
    for j in jumps:
        t = datetime.strptime(j['timestamp'][:19], TS_FMT)
        if cur is None or (t - cur['start']).total_seconds() > window_minutes * 60:
            cur = {'start': t, 'end': t, 'radios': {}}
            out.append(cur)
        cur['end'] = t
        cur['radios'].setdefault(j['node_name'], j)
    return out


def _covers(r, lat, lon):
    d = ld._haversine_m(r['lat'], r['lon'], lat, lon) / 1000
    if r['beamwidth'] >= 360 or r.get('azimuth') is None:
        return 0.5 if d <= OMNI_KM else 0.0
    if d > config.NOISE_SOURCE_RADIUS_KM:
        return 0.0
    if d < 0.2:
        return 1.0
    brg = ld._bearing(r['lat'], r['lon'], lat, lon)
    return 1.0 if ld._angle_diff(brg, r['azimuth']) <= r['beamwidth'] / 2 else 0.0


def _overlaps(a, b):
    if a.get('frequency') is None or b.get('frequency') is None:
        return False
    wa, wb = (a.get('chanbw') or 20) / 2, (b.get('chanbw') or 20) / 2
    return a['frequency'] - wa < b['frequency'] + wb and b['frequency'] - wb < a['frequency'] + wa


def estimate(jumped, quiet):
    """{lat, lon, radius_km, score, cells, max_score} or None."""
    located = [r for r in jumped if r.get('lat') is not None and r.get('lon') is not None]
    if len(located) < 2 or not any(r['beamwidth'] < 360 and r.get('azimuth') is not None for r in located):
        return None
    lat0 = sum(r['lat'] for r in located) / len(located)
    lon0 = sum(r['lon'] for r in located) / len(located)
    km_lat = 111.0
    km_lon = 111.0 * math.cos(math.radians(lat0))
    n = int(config.NOISE_SOURCE_RADIUS_KM / CELL_KM)
    quiet = [q for q in quiet if q.get('lat') is not None and any(_overlaps(q, j) for j in located)]
    cells = []
    for i in range(-n, n + 1):
        for k in range(-n, n + 1):
            lat, lon = lat0 + i * CELL_KM / km_lat, lon0 + k * CELL_KM / km_lon
            s = sum(_covers(r, lat, lon) for r in located) - 0.5 * sum(_covers(q, lat, lon) for q in quiet)
            if s > 0:
                cells.append((s, lat, lon))
    if not cells:
        return None
    best = max(c[0] for c in cells)
    if best < 1.5:
        return None
    top = [c for c in cells if c[0] >= best * TOP_SHARE]
    lat = sum(c[1] for c in top) / len(top)
    lon = sum(c[2] for c in top) / len(top)
    spread = max(ld._haversine_m(lat, lon, c[1], c[2]) for c in top) / 1000
    return {'lat': round(lat, 5), 'lon': round(lon, 5), 'radius_km': round(max(spread, CELL_KM / 2), 1),
            'score': round(best, 1), 'max_score': len(located), 'cells': len(top),
            'top_cells': [[round(c[1], 4), round(c[2], 4)] for c in top[:400]]}


def _span(radios):
    lows = [r['frequency'] - (r.get('chanbw') or 20) / 2 for r in radios if r.get('frequency') is not None]
    highs = [r['frequency'] + (r.get('chanbw') or 20) / 2 for r in radios if r.get('frequency') is not None]
    if not lows:
        return None
    lo, hi = max(lows), min(highs)
    return [lo, hi] if lo < hi else None


def _radio_out(r, extra=None):
    out = {k: r.get(k) for k in ('name', 'lat', 'lon', 'azimuth', 'beamwidth', 'beamwidth_assumed', 'frequency',
                                 'chanbw', 'is_infrastructure', 'site_code')}
    out.update(extra or {})
    return out


def report(hours=24, now=None):
    now = now or datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        radios, _ = channel_map._load(conn)
        jumps = _jumps(conn, since)
    try:
        import link_watch
        current = {r['node']: r for r in link_watch.noise_report(hours=24)['radios']}
    except Exception:
        current = {}
    groups = clusters_of(jumps, config.NOISE_SOURCE_WINDOW_MINUTES)
    now_bad = [n for n, r in current.items() if r.get('alarm') or (r.get('delta') or 0) >= config.NOISE_JUMP_DB]
    if len(now_bad) >= 2:
        groups.append({'start': now, 'end': now, 'radios': {n: {'node_name': n, 'details': None} for n in now_bad},
                       'current': True})
    out = []
    for g in groups:
        names = [n for n in g['radios'] if n in radios]
        if len(names) < 2:
            continue
        jumped = [radios[n] for n in names]
        quiet = [r for n, r in radios.items() if n not in g['radios'] and channel_map._transmitting(r)
                 and (current.get(n) or {}).get('delta') is not None and current[n]['delta'] < config.NOISE_CLEAR_DB]
        est = estimate(jumped, quiet)
        span = _span(jumped)
        key = (g['start'].strftime(TS_FMT), tuple(sorted(names)))
        text = (f"{len(names)} radios jumped together ({', '.join(sorted(names))})"
                + (f"; their beams cross about {est['radius_km']:g} km around {est['lat']:.4f}, {est['lon']:.4f}"
                   if est else '; their beams do not single out an area (omnis, or beams that do not cross)')
                + (f"; shared channel span {span[0]:g}-{span[1]:g} MHz" if span else ''))
        out.append({'key': f"{key[0]}|{'|'.join(key[1])}", 'start': g['start'].strftime(TS_FMT),
                    'end': g['end'].strftime(TS_FMT), 'current': bool(g.get('current')),
                    'radios': [_radio_out(radios[n], {'delta': (current.get(n) or {}).get('delta'),
                                                      'details': g['radios'][n].get('details')}) for n in names],
                    'quiet': [_radio_out(q, {'delta': current[q['name']]['delta']}) for q in quiet
                              if est and any(_overlaps(q, j) for j in jumped)][:30],
                    'estimate': est, 'span': span, 'text': text})
    out.sort(key=lambda c: (not c['current'], c['start']), reverse=False)
    out = sorted(out, key=lambda c: c['start'], reverse=True)
    return {'generated_at': now.strftime(TS_FMT), 'hours': hours, 'clusters': out,
            'window_minutes': config.NOISE_SOURCE_WINDOW_MINUTES,
            'notes': ['Needs two or more radios with a location whose noise jumped within '
                      f'{config.NOISE_SOURCE_WINDOW_MINUTES} min, at least one with an aimed antenna; '
                      'an estimate is only as good as the azimuth and beamwidth settings.']}


def check(now=None):
    """After a scan: log `noise_source` once for each new cluster with an estimate."""
    events = []
    for c in report(hours=2, now=now)['clusters']:
        if c['current'] or not c['estimate'] or c['key'] in _seen_clusters:
            continue
        # Survives a restart: the same cluster text logged in the last 3 h counts as seen.
        since = ((now or datetime.now()) - timedelta(hours=3)).strftime(TS_FMT)
        with database.get_connection() as conn:
            if conn.execute("SELECT 1 FROM events WHERE event_type = 'noise_source' AND details = ? AND timestamp >= ?",
                            (c['text'], since)).fetchone():
                _seen_clusters.add(c['key'])
                continue
        _seen_clusters.add(c['key'])
        node = c['radios'][0]['name']
        database.log_event('noise_source', node, c['text'], 'warning')
        events.append({'event_type': 'noise_source', 'node_name': node, 'details': c['text'],
                       'severity': 'warning'})
    return events

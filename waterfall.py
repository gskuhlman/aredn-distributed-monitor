"""
Spectrum waterfall per site (site dashboard "Spectrum over time").

For one infrastructure site (its radios plus radios within
infra_audit.SITE_METERS), hourly buckets over `days` and 5 MHz slots across
the band the site's radios use:
  occupancy  how many site radios cover the slot in that hour; each radio's
             channel over time is rebuilt backwards from its current
             frequency through its `frequency_change` events (width: the
             current one);
  noise      median measured noise (signal - SNR of its RF trackers, scan
             samples) of the radios covering the slot that hour, in dBm;
  marks      channel changes (events and change-log entries), maintenance
             windows and pending rollout steps for the site's radios, by
             hour, so planned and past changes line up with the picture.
Read-only.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import database
import infra_audit
import link_watch
import maintenance

TS_FMT = '%Y-%m-%d %H:%M:%S'
SLOT_MHZ = 5
FREQ_EVENT = re.compile(r'(\d+(?:\.\d+)?)\s*MHz\s*->\s*(\d+(?:\.\d+)?)\s*MHz')


def _site_radios(conn, code):
    infra = database.get_infrastructure_nodes()
    members = sorted(n for n, s in infra.items() if (s or '').upper() == code)
    if not members:
        return None
    radios = infra_audit._radios(conn)
    site = [radios[n] for n in members if n in radios]
    anchor = next((r for r in site if r.get('lat') is not None), None)
    others = [r for r in radios.values() if r['name'] not in members and anchor
              and infra_audit._rf_on(r) and (infra_audit._dist(anchor, r) or 1e9) <= infra_audit.SITE_METERS]
    return [r for r in site + others if r.get('frequency')]


def _history(conn, radio, start, end):
    """[(from_ts, to_ts, freq)] for one radio over [start, end)."""
    changes = [(r['timestamp'], r['details']) for r in conn.execute(
        '''SELECT timestamp, details FROM events WHERE event_type = 'frequency_change' AND node_name = ?
           AND timestamp >= ? ORDER BY timestamp DESC''', (radio['name'], start))]
    spans, upper, freq = [], end, radio['frequency']
    for ts, details in changes:
        m = FREQ_EVENT.search(details or '')
        if not m:
            continue
        spans.append((ts, upper, freq))
        upper, freq = ts, float(m.group(1))
    spans.append((start, upper, freq))
    return spans


def build(code, days=7):
    code = (code or '').strip().upper()
    days = max(1, min(int(days or 7), 31))
    now = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start_dt = now - timedelta(days=days)
    start, end = start_dt.strftime(TS_FMT), now.strftime(TS_FMT)
    nb = days * 24
    with database.get_connection() as conn:
        radios = _site_radios(conn, code)
        if radios is None:
            return None
        hist = {r['name']: _history(conn, r, start, end) for r in radios}
        noise = link_watch._noise_buckets(conn, start)
        events = [dict(r) for r in conn.execute(f'''
            SELECT timestamp, event_type, node_name, details FROM events
            WHERE timestamp >= ? AND event_type IN ('frequency_change', 'radio_config_change')
              AND node_name IN ({",".join("?" * len(radios))})''', [start] + [r['name'] for r in radios])] if radios else []
        changes = [dict(r) for r in conn.execute(f'''
            SELECT timestamp, node_name, category, description FROM change_log
            WHERE timestamp >= ? AND source != 'auto' AND node_name IN ({",".join("?" * len(radios))})''',
            [start] + [r['name'] for r in radios])] if radios else []
        try:
            pending = [dict(r) for r in conn.execute(f'''
                SELECT s.node_name, s.new_frequency, s.new_chanbw, s.turn_off, r.id AS rollout_id, r.name AS rollout
                FROM rollout_steps s JOIN rollouts r ON r.id = s.rollout_id
                WHERE r.status = 'active' AND s.status = 'pending'
                  AND s.node_name IN ({",".join("?" * len(radios))})''', [r['name'] for r in radios])] if radios else []
        except Exception:
            pending = []
        windows = maintenance.windows_between(start_dt, now, conn=conn)

    if not radios:
        return {'site': code, 'radios': [], 'slots': [], 'buckets': [], 'occupancy': [], 'noise': [], 'marks': []}
    widths = {r['name']: float(r.get('chanbw') or 20) for r in radios}
    freqs = [f for h in hist.values() for _, _, f in h] + [p['new_frequency'] for p in pending if p['new_frequency']]
    pad = max(widths.values())
    lo = int((min(freqs) - pad) // SLOT_MHZ * SLOT_MHZ)
    hi = int(-(-(max(freqs) + pad) // SLOT_MHZ) * SLOT_MHZ)
    slots = list(range(lo, hi, SLOT_MHZ))
    ns = len(slots)

    def idx(ts):
        return int((datetime.strptime(ts[:19], TS_FMT) - start_dt).total_seconds() // 3600)

    occ = [[0] * ns for _ in range(nb)]
    noise_cells = [[[] for _ in range(ns)] for _ in range(nb)]
    radio_noise = {}
    for name, samples in noise.items():
        if name not in widths:
            continue
        per = defaultdict(list)
        for ts, v, _ in samples:
            i = idx(ts)
            if 0 <= i < nb:
                per[i].append(v)
        radio_noise[name] = {i: median(v) for i, v in per.items()}
    for r in radios:
        w = widths[r['name']]
        for a, b, f in hist[r['name']]:
            ia, ib = max(0, idx(a)), min(nb, idx(b) + 1)
            covered = [k for k, s in enumerate(slots) if s < f + w / 2 and s + SLOT_MHZ > f - w / 2]
            for i in range(ia, ib):
                v = radio_noise.get(r['name'], {}).get(i)
                for k in covered:
                    occ[i][k] += 1
                    if v is not None:
                        noise_cells[i][k].append(v)
    noise_out = [[round(median(c), 1) if c else None for c in row] for row in noise_cells]

    marks = []
    for e in events:
        marks.append({'bucket': idx(e['timestamp']), 'time': e['timestamp'], 'kind': 'change',
                      'text': f"{e['node_name']}: {e['details']}"})
    for c in changes:
        marks.append({'bucket': idx(c['timestamp']), 'time': c['timestamp'], 'kind': 'logged',
                      'text': f"{c['node_name']}: {c['category']} — {c['description'] or ''}"})
    names = {r['name'] for r in radios}
    for w in windows:
        covered = set(maintenance.covered_nodes(w)) & names if w.get('scope') != 'all' else names
        if covered:
            marks.append({'bucket': max(0, idx(w['start_time'])), 'time': w['start_time'], 'kind': 'maintenance',
                          'text': f"Maintenance #{w['id']}: {w.get('note') or ', '.join(sorted(covered))}"})
    planned = [{'node': p['node_name'], 'frequency': p['new_frequency'], 'chanbw': p['new_chanbw'],
                'off': bool(p['turn_off']), 'rollout': p['rollout'], 'rollout_id': p['rollout_id']} for p in pending]
    return {'site': code, 'days': days, 'slot_mhz': SLOT_MHZ, 'slots': slots,
            'buckets': [(start_dt + timedelta(hours=i)).strftime(TS_FMT) for i in range(nb)],
            'radios': [{'name': r['name'], 'frequency': r['frequency'], 'chanbw': widths[r['name']],
                        'history': [{'from': a, 'to': b, 'frequency': f} for a, b, f in hist[r['name']]]}
                       for r in radios],
            'occupancy': occ, 'noise': noise_out, 'marks': sorted(marks, key=lambda m: m['time']),
            'planned': planned}

"""
Spectrum by location (Spectrum tab): frequency across, locations down.

One row per site (radios within SITE_METERS, as on the Channel Map), and under
it one row per radio at the site with a WiFi survey (what that antenna hears,
facing its azimuth). Each row is cut into BIN_MHZ slots, and each slot gets:

- ours: a transmitting radio at the site covers it (what-if overrides apply);
- guard: within GUARD_MHZ of one of ours (a new channel there would be
  adjacent on the mast);
- busy / some: WiFi access points from the site's surveys plus mesh radios at
  other sites within channel_map.CONFLICT_KM that face the site or that a site
  radio faces (strength as wifi_survey.ap_cost; mesh by distance and facing);
- free: none of that;
- unsurveyed: free of mesh radios, but nothing at the site was scanned, so
  WiFi is unknown (the planner's typical ranges, shown in their own row, are
  the only guide).

Survey APs whose width the scan didn't give (the scan-page table) are drawn
20 MHz wide plus the rest of their usual 80 MHz WiFi block as "may extend",
which counts as some, never busy. RF-linked peers of a site radio share its
channel and count as ours, not as congestion.

History (TODO #70): with `at`, surveys are the newest ones taken by then and
each radio's channel is rebuilt backwards from its `frequency_change` events
(drawn like what-if bars, today's channel dashed). Every scan row carries what
changed since that radio's previous scan (APs new / gone per slot).
Scan reminders (TODO #71): transmitting radios at infrastructure sites never
scanned or scanned more than SCAN_REMINDER_DAYS ago, with the RF links a scan
would pause; they can be queued for tonight's survey run
(wifi_survey.queue_tonight). Read-only apart from that queue.
"""

import re
from datetime import datetime

import channel_map
import channel_planner
import config
import database
import link_diagnostics as ld
import wifi_survey

BIN_MHZ = 5
GUARD_MHZ = 10
DEFAULT_BAND = (5640.0, 5930.0)
TS_FMT = '%Y-%m-%d %H:%M:%S'
FREQ_EVENT = re.compile(r'(\d+(?:\.\d+)?)\s*MHz\s*->\s*(\d+(?:\.\d+)?)\s*MHz')
BUSY_SCORE = 1.0
# Standard 80 MHz WiFi blocks: where an AP of unknown width may extend.
WIFI_BLOCKS = [(5170, 5250), (5250, 5330), (5490, 5570), (5570, 5650), (5650, 5730),
               (5735, 5815), (5815, 5895)]


def _ap_strength(sig):
    sig = sig if sig is not None else -80.0
    return min(1.5, max(0.1, (sig + 95.0) / 30.0))


def _mesh_weight(dist_km, mutual):
    if mutual and dist_km <= 8:
        return 1.5
    if mutual or dist_km <= 15:
        return 0.8
    return 0.4


def _block(freq):
    for lo, hi in WIFI_BLOCKS:
        if lo < freq < hi:
            return lo, hi
    return None


def _aps(rows):
    """Survey rows -> APs with a known span or a 20 MHz core plus its block."""
    out = []
    for ap in rows:
        f = ap.get('frequency')
        if ap.get('is_mesh') or not f:
            continue
        item = {'ssid': ap.get('ssid'), 'bssid': ap.get('bssid'), 'frequency': f,
                'signal': ap.get('signal'), 'node': ap.get('node_name')}
        if ap.get('low_mhz') and ap.get('high_mhz'):
            item.update(low=ap['low_mhz'], high=ap['high_mhz'], width_known=True)
        else:
            item.update(low=f - 10, high=f + 10, width_known=False)
            blk = _block(f)
            if blk and (blk[0] < item['low'] or blk[1] > item['high']):
                item['may_low'], item['may_high'] = blk
        out.append(item)
    return out


def _bins(band):
    lo, hi = band
    f, out = lo, []
    while f < hi:
        out.append((f, min(hi, f + BIN_MHZ)))
        f += BIN_MHZ
    return out


def _overlaps(a_lo, a_hi, b_lo, b_hi):
    return min(a_hi, b_hi) - max(a_lo, b_lo) > 0


def _lane(bins, ours, mesh, aps, surveyed):
    """Per-bin status and contents for one row."""
    cells = []
    for lo, hi in bins:
        own = [r['name'] for r in ours if _overlaps(lo, hi, r['low'], r['high'])]
        guard = not own and any(_overlaps(lo, hi, r['low'] - GUARD_MHZ, r['high'] + GUARD_MHZ) for r in ours)
        m = [x for x in mesh if _overlaps(lo, hi, x['low'], x['high'])]
        w = [i for i, a in enumerate(aps) if _overlaps(lo, hi, a['low'], a['high'])]
        may = [i for i, a in enumerate(aps)
               if i not in w and a.get('may_low') is not None and _overlaps(lo, hi, a['may_low'], a['may_high'])]
        score = sum(x['weight'] for x in m) + sum(_ap_strength(aps[i]['signal']) for i in w)
        if own:
            status = 'ours'
        elif score >= BUSY_SCORE:
            status = 'busy'
        elif score > 0 or may:
            status = 'some'
        elif guard:
            status = 'guard'
        else:
            status = 'free' if surveyed else 'unsurveyed'
        cells.append({'low': lo, 'high': hi, 'status': status, 'score': round(score, 2), 'guard': guard,
                      'ours': own, 'mesh': [x['name'] for x in m], 'wifi': w, 'may': may})
    return cells


def _runs(bins, ok):
    """Contiguous spans (>= 20 MHz) of bins where ok(i)."""
    runs, start = [], None
    for i, (lo, hi) in enumerate(bins + [(None, None)]):
        good = lo is not None and ok(i)
        if good and start is None:
            start = lo
        if not good and start is not None:
            end = bins[i - 1][1]
            if end - start >= 20:
                runs.append({'low': start, 'high': end, 'mhz': end - start})
            start = None
    return runs


def channels_at(at):
    """{radio: frequency} for radios whose channel at `at` differs from now,
    rebuilt backwards through their `frequency_change` events."""
    out = {}
    with database.get_connection() as conn:
        now_freq = {r['name']: r['rf_frequency'] for r in conn.execute(
            'SELECT name, rf_frequency FROM nodes WHERE rf_frequency IS NOT NULL')}
        rows = conn.execute("""SELECT node_name, timestamp, details FROM events
                               WHERE event_type = 'frequency_change' AND timestamp > ?
                               ORDER BY timestamp DESC""", (at.strftime(TS_FMT),)).fetchall()
    for r in rows:
        m = FREQ_EVENT.search(r['details'] or '')
        if m and r['node_name']:
            out[r['node_name']] = float(m.group(1))  # the oldest change after `at` is applied last
    return {n: f for n, f in out.items() if _num(now_freq.get(n)) != f}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ap_key(ap):
    return ap.get('bssid') or f"{ap.get('ssid')}@{ap.get('frequency')}"


def scan_changes(conn, node, aps_now, bins):
    """What changed since this radio's previous scan: per bin whether APs
    are new / gone, plus the lists."""
    rows = wifi_survey.previous(conn, node, aps_now[0]['taken_at']) if aps_now else []
    if not rows:
        return None
    before = {_ap_key(a): a for a in _aps(rows)}
    after = {_ap_key(a): a for a in _aps(aps_now)}
    new = [after[k] for k in after if k not in before]
    gone = [before[k] for k in before if k not in after]
    cells = []
    for lo, hi in bins:
        n = sum(1 for a in new if _overlaps(lo, hi, a['low'], a['high']))
        g = sum(1 for a in gone if _overlaps(lo, hi, a['low'], a['high']))
        cells.append('both' if n and g else 'new' if n else 'gone' if g else None)
    return {'previous_taken_at': rows[0]['taken_at'], 'new': new, 'gone': gone, 'cells': cells}


def reminders(rows, linked, surveys_all, queue, now=None):
    """Scan to-dos (TODO #71): transmitting radios at infrastructure sites
    never scanned, or last scanned more than SCAN_REMINDER_DAYS ago."""
    now = now or datetime.now()
    out = []
    for row in rows:
        if not row['codes']:
            continue
        for r in row['ours']:
            last = surveys_all.get(r['name'])
            age = (now - datetime.strptime(last[:19], TS_FMT)).days if last else None
            if last and age <= config.SCAN_REMINDER_DAYS:
                continue
            peers = sorted(linked.get(r['name'], set()))
            out.append({'radio': r['name'], 'site': row['name'], 'last_scan': last, 'age_days': age,
                        'why': ('never scanned: WiFi at this site is a guess (the typical ranges)' if not last else
                                f'last scanned {age} days ago (older than {config.SCAN_REMINDER_DAYS})'),
                        'links': peers,
                        'impact': ('the radio leaves its channel for about 5-15 s; '
                                   + (f"its {len(peers)} RF link(s) ({', '.join(peers[:4])}"
                                      f"{'...' if len(peers) > 4 else ''}) pause that long" if peers
                                      else 'it has no RF links to interrupt')),
                        'queued': r['name'] in queue})
    out.sort(key=lambda x: (x['last_scan'] is not None, x['site'], x['radio']))
    return out


def build(scope='infrastructure', band=None, overrides=None, include_inactive=False, at=None):
    band = tuple(float(x) for x in (band or DEFAULT_BAND))
    history = {}
    if at:
        history = channels_at(at)
        merged = {n: {'frequency': f} for n, f in history.items()}
        merged.update(channel_map.parse_overrides(overrides) if overrides else {})
        overrides = merged
    data = channel_map.build_map(scope='all', overrides=overrides or None, include_inactive=include_inactive)
    radios = {r['name']: r for r in data['radios']}
    bins = _bins(band)
    linked = {}
    for l in data['links']:
        linked.setdefault(l['a'], set()).add(l['b'])
        linked.setdefault(l['b'], set()).add(l['a'])
    surveys, surveys_all, queue = {}, {}, set()
    try:
        surveys = wifi_survey.latest(at=at)
        with database.get_connection() as conn:
            surveys_all = wifi_survey.last_scans(conn)
        queue = {q['node_name'] for q in wifi_survey.queued()}
    except Exception:
        pass

    def on_air(r):
        return (channel_map._transmitting(r) and (include_inactive or r['recent'])
                and r.get('low') is not None and _overlaps(band[0], band[1], r['low'], r['high']))

    sites = []
    for s in data['sites']:
        members = [radios[m] for m in s['members'] if m in radios]
        if scope != 'all' and not s['codes'] and not any(m['name'] in surveys for m in members):
            continue
        ours = [r for r in members if on_air(r)]
        if not ours and not any(m['name'] in surveys for m in members):
            continue
        sites.append((s, members, ours))

    rows, mesh_info = [], {}
    for s, members, ours in sites:
        names = {m['name'] for m in members}
        peers = set().union(*(linked.get(n, set()) for n in names)) if names else set()
        mesh = []
        for b in radios.values():
            if b['name'] in names or b['name'] in peers or not on_air(b) or b['lat'] is None:
                continue
            best = None
            for a in members:
                if a['lat'] is None:
                    continue
                d = ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])
                if d is None or d > channel_map.CONFLICT_KM * 1000:
                    continue
                a_sees = channel_map._in_beam(a, b, d) if on_air(a) else False
                b_sees = channel_map._in_beam(b, a, d)
                if not (a_sees or b_sees):
                    continue
                w = _mesh_weight(d / 1000, a_sees and b_sees)
                if best is None or w > best['weight'] or (w == best['weight'] and d < best['distance_m']):
                    best = {'name': b['name'], 'low': b['low'], 'high': b['high'], 'weight': w,
                            'distance_m': round(d), 'facing': 'both' if a_sees and b_sees
                            else ('site radio faces it' if a_sees else 'it faces the site'), 'via': a['name']}
            if best:
                mesh.append(best)
                mesh_info[b['name']] = {'frequency': b['frequency'], 'chanbw': b['chanbw'],
                                        'low': b['low'], 'high': b['high']}
        scanned = [m for m in members if m['name'] in surveys]
        site_aps = []
        seen = set()
        for m in scanned:
            for ap in _aps(surveys[m['name']]):
                key = ap['bssid'] or (ap['ssid'], ap['frequency'])
                if key in seen:
                    continue
                seen.add(key)
                site_aps.append(ap)
        own = [{'name': r['name'], 'low': r['low'], 'high': r['high'], 'frequency': r['frequency'],
                'chanbw': r['chanbw'], 'channel': r['channel'], 'azimuth': r['azimuth'],
                'beamwidth': r['beamwidth'], 'role': r['role'], 'is_infrastructure': r['is_infrastructure'],
                'what_if': bool(r.get('what_if')), 'original': r.get('original')} for r in ours]
        cells = _lane(bins, own, mesh, site_aps, bool(scanned))
        row = {'kind': 'site', 'id': s['id'], 'name': s['name'], 'codes': s['codes'], 'ours': own,
               'mesh': sorted(mesh, key=lambda x: x['low']), 'aps': site_aps, 'cells': cells,
               'surveyed': bool(scanned), 'members': [m['name'] for m in members],
               'free': _runs(bins, lambda i: cells[i]['status'] == 'free'),
               'scans': []}
        for m in scanned:
            aps = _aps(surveys[m['name']])
            taken = (surveys[m['name']] or [{}])[0].get('taken_at')
            sub = _lane(bins, [], [], aps, True)
            try:
                with database.get_connection() as conn:
                    changes = scan_changes(conn, m['name'], surveys[m['name']], bins)
            except Exception:
                changes = None
            row['scans'].append({'kind': 'scan', 'radio': m['name'], 'azimuth': m['azimuth'],
                                 'beamwidth': m['beamwidth'], 'taken_at': taken, 'aps': aps, 'cells': sub,
                                 'free': _runs(bins, lambda i, sub=sub: sub[i]['status'] == 'free'),
                                 'changes': changes})
        rows.append(row)

    rows.sort(key=lambda r: (not r['codes'], not r['surveyed'], r['name'].lower()))
    surveyed_rows = [r for r in rows if r['surveyed']]
    typical = []
    for lo, hi in bins:
        hits = [n['label'] for n in channel_planner.NOISE_RANGES if _overlaps(lo, hi, n['low'], n['high'])]
        cost = max([n['cost'] for n in channel_planner.NOISE_RANGES if _overlaps(lo, hi, n['low'], n['high'])] or [0])
        typical.append({'low': lo, 'high': hi, 'labels': hits,
                        'status': 'busy' if cost >= 20 else ('some' if cost > 0 else 'free')})
    common = [all(r['cells'][i]['status'] in ('free', 'unsurveyed') for r in rows) for i in range(len(bins))]
    common_surveyed = [all(r['cells'][i]['status'] == 'free' for r in surveyed_rows) if surveyed_rows else False
                       for i in range(len(bins))]
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'band': {'low': band[0], 'high': band[1]}, 'ham_band': data['ham_band'],
        'bin_mhz': BIN_MHZ, 'guard_mhz': GUARD_MHZ, 'scope': scope,
        'rows': rows, 'typical': typical, 'mesh_radios': mesh_info,
        'common': {'cells': [{'low': lo, 'high': hi, 'free': common[i], 'free_surveyed': common_surveyed[i]}
                             for i, (lo, hi) in enumerate(bins)],
                   'free': _runs(bins, lambda i: common[i]),
                   'free_surveyed': _runs(bins, lambda i: common_surveyed[i]),
                   'surveyed_sites': [r['name'] for r in surveyed_rows]},
        'what_if': None if at else data.get('what_if'),
        'as_of': at.strftime(TS_FMT) if at else None,
        'history_channels': history,
        'reminders': [] if at else reminders(rows, linked, surveys_all, queue),
        'scan_times': _scan_times(rows),
        'survey': {'enabled': config.WIFI_SURVEY_ENABLED, 'hour': config.WIFI_SURVEY_HOUR,
                   'queued': sorted(queue), 'reminder_days': config.SCAN_REMINDER_DAYS},
    }


def _scan_times(rows):
    """Times of the scans shown (newest first) for the history picker."""
    names = {s['radio'] for r in rows for s in r['scans']} | {m for r in rows for m in r['members']}
    try:
        with database.get_connection() as conn:
            times = wifi_survey.scan_times(conn, names)
    except Exception:
        return []
    return sorted({t for v in times.values() for t in v}, reverse=True)[:60]

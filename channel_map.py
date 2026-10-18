"""
Area channel map: every located radio with its channel, antenna beam and the
radios it conflicts with, for the Channel Map tab.

A conflict is a pair of powered radios on overlapping, adjacent or near
channels (not the same channel, which just shares airtime) that are colocated
(one mast/site) or within range with at least one aimed at the other. The
severity combines spectral relation, distance and whether the beams face each
other. Optional what-if overrides (frequency/width per radio) are applied
in memory only, never stored, so a proposed channel plan can be tested.

Read-only over SQLite.
"""

import math
import re
from datetime import datetime, timedelta

import database
import infra_audit as ia
import link_diagnostics as ld
import reports

SITE_METERS = ia.SITE_METERS        # radios this close are one site / mast
CONFLICT_KM = 25.0                  # ignore pairs farther apart than this
HAM_5GHZ = (5650.0, 5925.0)         # US amateur 5 GHz allocation
SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}
BEAM_MARGIN_DEG = 5.0
RECENT_DAYS = 7                     # inactive radios seen this recently still count

# Busy WiFi ranges drawn under the frequency legend (MHz).
WIFI_BANDS = [
    {'low': 5490, 'high': 5730, 'label': 'WiFi ch 100–144 (DFS)', 'busy': 'moderate'},
    {'low': 5735, 'high': 5835, 'label': 'WiFi ch 149–165', 'busy': 'heavy'},
    {'low': 5835, 'high': 5895, 'label': 'WiFi 6E/7 ch 169–177 (UNII-4)', 'busy': 'light'},
    {'low': 5895, 'high': 5925, 'label': 'C-V2X (vehicles)', 'busy': 'light'},
]

_DESC_BEAM = re.compile(r'(\d+(?:\.\d+)?)\s*°')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def band_of(freq):
    if freq is None:
        return None
    if 2300 <= freq < 2500:
        return '2.4'
    if 3300 <= freq < 3800:
        return '3'
    if 5000 <= freq < 6000:
        return '5-ham' if HAM_5GHZ[0] - 20 <= freq <= HAM_5GHZ[1] else '5'
    if 900 <= freq < 930:
        return '0.9'
    return 'other'


def beamwidth_of(r):
    """(beamwidth, assumed): stored value, else parsed from the antenna
    description, else estimated from gain; no azimuth means omni."""
    bw = _num(r.get('beamwidth'))
    if bw:
        return bw, False
    desc = r.get('antenna') or ''
    if 'omni' in desc.lower():
        return 360.0, False
    m = _DESC_BEAM.search(desc)
    if m:
        return float(m.group(1)), False
    if r.get('azimuth') is None:
        return 360.0, True
    gain = _num(r.get('gain'))
    if gain is None:
        return 60.0, True
    for min_gain, width in ((25, 8), (22, 15), (18, 25), (15, 30), (12, 45)):
        if gain >= min_gain:
            return float(width), True
    return 90.0, True


def _in_beam(a, b, dist_m):
    """True when b lies inside a's beam (an omni sees everything)."""
    if dist_m is not None and dist_m <= SITE_METERS:
        return True
    if a['beamwidth'] >= 360 or a.get('azimuth') is None:
        return True
    brg = ld._bearing(a['lat'], a['lon'], b['lat'], b['lon'])
    return ld._angle_diff(brg, a['azimuth']) <= a['beamwidth'] / 2 + BEAM_MARGIN_DEG


def _severity(rel, colocated, dist_km, a_sees, b_sees):
    if colocated:
        return {'overlap': 'critical', 'adjacent': 'warning', 'near': 'info'}.get(rel)
    if not (a_sees or b_sees):
        return None
    mutual = a_sees and b_sees
    if rel == 'overlap':
        if mutual and dist_km <= 8:
            return 'critical'
        return 'warning' if (mutual or dist_km <= 15) else 'info'
    if rel == 'adjacent':
        return 'warning' if mutual and dist_km <= 3 else 'info'
    if rel == 'near':
        return 'info' if mutual and dist_km <= 2 else None
    return None


def _sites(radios, infra, site_names):
    """Group radios within SITE_METERS of each other (union-find)."""
    names = [n for n, r in radios.items() if r['lat'] is not None]
    parent = {n: n for n in names}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = ld._haversine_m(radios[a]['lat'], radios[a]['lon'], radios[b]['lat'], radios[b]['lon'])
            if d is not None and d <= SITE_METERS:
                parent[find(a)] = find(b)
    groups = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    sites = []
    for members in groups.values():
        members.sort()
        codes = sorted({infra[m] for m in members if infra.get(m)})
        if codes:
            label = ', '.join(site_names.get(c, c) for c in codes)
        else:
            label = members[0]
        sid = f"site-{len(sites) + 1}"
        for i, m in enumerate(members):
            radios[m]['site_id'] = sid
            radios[m]['site_index'] = i
            radios[m]['site_size'] = len(members)
        sites.append({'id': sid, 'name': label, 'codes': codes, 'members': members,
                      'lat': sum(radios[m]['lat'] for m in members) / len(members),
                      'lon': sum(radios[m]['lon'] for m in members) / len(members)})
    return sites


def _load(conn):
    radios = {}
    infra = database.get_infrastructure_nodes()
    selected = set(database.get_selected_node_names())
    for name, r in ia._radios(conn).items():
        lat, lon = _num(r.get('lat')), _num(r.get('lon'))
        freq = _num(r.get('frequency'))
        width = _num(r.get('chanbw'))
        item = {
            'name': name, 'lat': lat, 'lon': lon, 'model': r.get('model'),
            'firmware': r.get('firmware'), 'active': r.get('is_active') == 1,
            'last_seen': r.get('last_seen'), 'rf_status': r.get('rf_status'),
            'frequency': freq, 'chanbw': width, 'width_assumed': freq is not None and not width,
            'channel': r.get('channel') or ia.freq_to_channel(freq),
            'mode': r.get('mode'), 'role': ia._role(r.get('mode')), 'ssid': r.get('ssid'),
            'azimuth': _num(r.get('azimuth')), 'elevation': _num(r.get('elevation')),
            'height': _num(r.get('height')), 'gain': _num(r.get('gain')),
            'antenna': r.get('antenna'), 'beamwidth_raw': r.get('beamwidth'),
            'is_infrastructure': name in infra, 'site_code': infra.get(name),
            'is_selected': name in selected, 'config_at': r.get('config_at'),
        }
        item['beamwidth'], item['beamwidth_assumed'] = beamwidth_of(
            {**r, 'azimuth': item['azimuth'], 'gain': item['gain']})
        radios[name] = item
    return radios, infra


def _apply_span(r):
    f = r.get('frequency')
    if f is None:
        r.update(low=None, high=None, band=None, low_channel=None, high_channel=None)
        return
    width = r.get('chanbw') or 20.0
    r['low'], r['high'] = f - width / 2, f + width / 2
    r['band'] = band_of(f)
    r['low_channel'], r['high_channel'] = ia.freq_to_channel(r['low']), ia.freq_to_channel(r['high'])
    r['outside_ham'] = r['band'] == '5-ham' and (r['low'] < HAM_5GHZ[0] or r['high'] > HAM_5GHZ[1])


def _transmitting(r):
    return r.get('frequency') is not None and r.get('rf_status') in (None, 'on') and r.get('lat') is not None


def _recent(r):
    """Active now, or seen within RECENT_DAYS: a node the scanner can't reach
    right now is usually still on the air."""
    if r['active']:
        return True
    try:
        seen = datetime.strptime((r.get('last_seen') or '')[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return datetime.now() - seen <= timedelta(days=RECENT_DAYS)


def _conflicts(radios, include_inactive=False):
    live = [r for r in radios.values() if _transmitting(r) and (include_inactive or r['recent'])]
    out = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if (a['band'] or '')[:1] != (b['band'] or '')[:1]:
                continue  # different bands (5 GHz ham and non-ham parts compare)
            span_a = {'frequency': a['frequency'], 'chanbw': a['chanbw']}
            span_b = {'frequency': b['frequency'], 'chanbw': b['chanbw']}
            rel = ia.pair_relation(span_a, span_b)
            if rel not in ('overlap', 'adjacent', 'near'):
                continue
            d = ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])
            if d is None or d > CONFLICT_KM * 1000:
                continue
            colocated = d <= SITE_METERS
            a_sees, b_sees = _in_beam(a, b, d), _in_beam(b, a, d)
            sev = _severity(rel, colocated, d / 1000, a_sees, b_sees)
            if not sev:
                continue
            gap = ia.spectral_gap(span_a, span_b)
            out.append({
                'a': a['name'], 'b': b['name'], 'relation': rel, 'gap_mhz': gap,
                'distance_m': round(d), 'colocated': colocated, 'severity': sev,
                'a_sees_b': a_sees, 'b_sees_a': b_sees,
                'key': '|'.join(sorted((a['name'], b['name']))),
                'summary': (f"{a['name']} {a['low']:.0f}–{a['high']:.0f} MHz vs {b['name']} "
                            f"{b['low']:.0f}–{b['high']:.0f} MHz: "
                            + (f"{-gap:.0f} MHz overlap" if gap < 0 else f"{gap:.0f} MHz gap")
                            + (', same site' if colocated else f", {d / 1000:.1f} km"
                               + (', facing each other' if a_sees and b_sees
                                  else f", only {a['name'] if a_sees else b['name']} aimed at the other"))),
            })
    out.sort(key=lambda c: (SEVERITY_ORDER[c['severity']], c['gap_mhz'], c['distance_m']))
    return out


def _rf_links(conn, radios):
    rows = conn.execute('''
        SELECT source_node, target_node, quality, snr, signal, status, last_seen
        FROM links WHERE UPPER(link_type) = 'RF' AND status != 'removed'
    ''').fetchall()
    pairs = {}
    for src, tgt, q, snr, sig, status, last_seen in rows:
        if src not in radios or tgt not in radios:
            continue
        if radios[src]['lat'] is None or radios[tgt]['lat'] is None:
            continue
        key = tuple(sorted((src, tgt)))
        p = pairs.setdefault(key, {'a': key[0], 'b': key[1], 'directions': {}, 'status': status,
                                   'last_seen': last_seen})
        p['directions'][f'{src}>{tgt}'] = {'quality': q, 'snr': snr, 'signal': sig, 'status': status}
        if status == 'good' or p['status'] != 'good':
            p['status'] = status
        if last_seen and (not p['last_seen'] or last_seen > p['last_seen']):
            p['last_seen'] = last_seen
    links = []
    for p in pairs.values():
        qs = [d['quality'] for d in p['directions'].values() if d['quality'] is not None]
        snrs = [d['snr'] for d in p['directions'].values() if d['snr'] is not None]
        p['quality'] = min(qs) if qs else None
        p['snr'] = min(snrs) if snrs else None
        p['distance_m'] = round(ld._haversine_m(radios[p['a']]['lat'], radios[p['a']]['lon'],
                                                radios[p['b']]['lat'], radios[p['b']]['lon']) or 0)
        links.append(p)
    return links


def parse_overrides(raw):
    """{name: {frequency, chanbw}} from a dict or 'name freq [width]' lines."""
    out = {}
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = []
        for line in str(raw or '').splitlines():
            parts = line.replace(',', ' ').split()
            if len(parts) >= 2 and not parts[0].startswith('#'):
                items.append((parts[0], {'frequency': parts[1],
                                         'chanbw': parts[2] if len(parts) > 2 else None}))
    for name, spec in items:
        spec = spec or {}
        freq, width = _num(spec.get('frequency')), _num(spec.get('chanbw'))
        if freq is None and width is None:
            continue
        out[name.strip().lower()] = {'frequency': freq, 'chanbw': width}
    return out


def build_map(scope='all', max_km=None, center=None, overrides=None, include_inactive=False):
    """Radios, sites, conflicts and RF links for the Channel Map.

    With overrides (what-if), conflicts are computed on the overridden
    channels and compared against the stored ones (nothing is saved).
    """
    with database.get_connection() as conn:
        radios, infra = _load(conn)
        allowed = reports.resolve_scope(conn, scope or 'all')
        links = _rf_links(conn, radios)

    center_radio = radios.get((center or '').lower()) if center else None
    max_m = _num(max_km) * 1000 if _num(max_km) else None

    def keep(r):
        if r['lat'] is None or r['lon'] is None:
            return False
        if allowed is not None and r['name'] not in allowed:
            return False
        if max_m and center_radio and center_radio['lat'] is not None:
            d = ld._haversine_m(center_radio['lat'], center_radio['lon'], r['lat'], r['lon'])
            if d is None or d > max_m:
                return False
        return True

    radios = {n: r for n, r in radios.items() if keep(r)}
    for r in radios.values():
        _apply_span(r)
        r['recent'] = _recent(r)
    sites =_sites(radios, infra, database.get_site_names())

    baseline = _conflicts(radios, include_inactive)
    overrides = parse_overrides(overrides) if overrides else {}
    applied, unknown = {}, []
    if overrides:
        for name, spec in overrides.items():
            r = radios.get(name)
            if not r:
                unknown.append(name)
                continue
            r['original'] = {'frequency': r['frequency'], 'chanbw': r['chanbw'], 'channel': r['channel']}
            if spec['frequency'] is not None:
                r['frequency'] = spec['frequency']
                r['channel'] = ia.freq_to_channel(spec['frequency'])
            if spec['chanbw'] is not None:
                r['chanbw'] = spec['chanbw']
            r['width_assumed'] = not r['chanbw']
            r['what_if'] = True
            _apply_span(r)
            applied[name] = {'frequency': r['frequency'], 'chanbw': r['chanbw'], 'channel': r['channel']}
        conflicts = _conflicts(radios, include_inactive)
    else:
        conflicts = baseline

    by_radio = {}
    for c in conflicts:
        for n in (c['a'], c['b']):
            by_radio.setdefault(n, []).append(c['key'])
    for r in radios.values():
        keys = by_radio.get(r['name'], [])
        r['conflict_count'] = len(keys)
        sevs = [c['severity'] for c in conflicts if c['key'] in set(keys)]
        r['worst_conflict'] = min(sevs, key=SEVERITY_ORDER.get) if sevs else None

    for s in sites:
        members = set(s['members'])
        s['conflicts'] = [c['key'] for c in conflicts if c['colocated'] and c['a'] in members]

    links = [l for l in links if l['a'] in radios and l['b'] in radios]
    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scope': scope, 'center': center_radio['name'] if center_radio else None, 'max_km': _num(max_km),
        'radios': sorted(radios.values(), key=lambda r: r['name']),
        'sites': sites, 'conflicts': conflicts, 'links': links,
        'counts': _counts(conflicts),
        'ham_band': {'low': HAM_5GHZ[0], 'high': HAM_5GHZ[1]}, 'wifi_bands': WIFI_BANDS,
        'site_meters': SITE_METERS, 'conflict_km': CONFLICT_KM,
    }
    if overrides:
        base_keys = {c['key']: c for c in baseline}
        new_keys = {c['key']: c for c in conflicts}
        result['what_if'] = {
            'applied': applied, 'unknown': unknown,
            'baseline_counts': _counts(baseline),
            'resolved': [base_keys[k] for k in base_keys if k not in new_keys],
            'introduced': [new_keys[k] for k in new_keys if k not in base_keys],
            'changed': [{'before': base_keys[k], 'after': new_keys[k]} for k in new_keys
                        if k in base_keys and base_keys[k]['severity'] != new_keys[k]['severity']],
        }
    return result


def _counts(conflicts):
    counts = {'critical': 0, 'warning': 0, 'info': 0, 'colocated': 0}
    for c in conflicts:
        counts[c['severity']] += 1
        counts['colocated'] += 1 if c['colocated'] else 0
    return counts

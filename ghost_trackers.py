"""
Ghost tracker check (Diagnostics tab "Ghost Trackers", Mesh Health): RF
trackers that can't be what they claim.

For every RF tracker (links row, not removed, seen within RECENT_DAYS):
  radio_off        the named neighbour reports its radio off (rf_status 'off'),
                   or the tracker holder's own radio is off: it is a leftover
                   or another radio announcing the same name;
  cabled           the two nodes are also DtD-cabled: a side path or a leftover
                   from before the cable, not an RF neighbour;
  channel_mismatch both radios report channels that don't overlap at all
                   (read in the last day), yet the tracker is current;
  distance         the node-reported LQM distance disagrees with the stored
                   coordinates by more than DISTANCE_SLACK_M and DISTANCE_RATIO
                   (another radio with the same name somewhere else, or a stale
                   location setting: aim_check says which);
  mac_conflict     one MAC address tracked under two different names.
radio_off and cabled trackers are `exclude`d: `excluded_pairs()` is used by
Mesh Health, Link Watch, the Channel Map links, Site Matrix and Sector
Airtime to leave them out of their RF checks. The other kinds are reported
for a person to look at. Read-only.
"""

from collections import defaultdict
from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
RECENT_DAYS = 7
DISTANCE_SLACK_M = 1000
DISTANCE_RATIO = 0.5
EXCLUDE_KINDS = ('radio_off', 'cabled')

KIND_TEXT = {
    'radio_off': 'radio off',
    'cabled': 'also cabled (DtD)',
    'channel_mismatch': "channels don't overlap",
    'distance': 'distance disagrees with coordinates',
    'mac_conflict': 'one MAC, two names',
}


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _span(node):
    f, w = _num(node.get('rf_frequency')), _num(node.get('rf_chanbw')) or 10.0
    if f is None:
        return None
    return f - w / 2, f + w / 2


def _haversine_m(a, b):
    import link_diagnostics as ld
    return ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])


def find(conn=None, now=None):
    """Every suspicious RF tracker, worst first."""
    if conn is None:
        with database.get_connection() as c:
            return find(c, now)
    now = now or datetime.now()
    recent = (now - timedelta(days=RECENT_DAYS)).strftime(TS_FMT)
    day = (now - timedelta(days=1)).strftime(TS_FMT)
    nodes = {r['name']: dict(r) for r in conn.execute(
        'SELECT name, lat, lon, rf_status, rf_frequency, rf_chanbw, last_seen, radio_config_at FROM nodes')}
    cabled = set()
    for a, b in conn.execute('''SELECT source_node, target_node FROM links
                                WHERE UPPER(link_type) = 'DTD' AND status != 'removed' AND last_seen >= ?''',
                             (recent,)):
        cabled.update({(a, b), (b, a)})
    rows = [dict(r) for r in conn.execute('''
        SELECT source_node, target_node, quality, snr, distance, mac_address, status, last_seen
        FROM links WHERE UPPER(link_type) = 'RF' AND status != 'removed' AND last_seen >= ?
    ''', (recent,))]
    names_by_mac = defaultdict(set)
    for r in rows:
        mac = (r['mac_address'] or '').lower().strip()
        if mac and not r['target_node'].startswith('mac-'):
            names_by_mac[mac].add(r['target_node'])

    out = []
    for r in rows:
        src, tgt = r['source_node'], r['target_node']
        s, t = nodes.get(src) or {}, nodes.get(tgt) or {}
        kinds, notes = [], []
        if str(t.get('rf_status') or '').lower() == 'off':
            kinds.append('radio_off')
            notes.append(f"{tgt} reports its radio off, so this RF entry at {src} is not {tgt}'s radio")
        elif str(s.get('rf_status') or '').lower() == 'off':
            kinds.append('radio_off')
            notes.append(f"{src} reports its own radio off; this RF entry is left over")
        if (src, tgt) in cabled:
            kinds.append('cabled')
            notes.append(f"{src} and {tgt} are cabled (DtD); their traffic uses the cable")
        sa, ta = _span(s), _span(t)
        if (sa and ta and r['status'] == 'good' and 'radio_off' not in kinds
                and (s.get('last_seen') or '') >= day and (t.get('last_seen') or '') >= day
                and (sa[1] <= ta[0] or ta[1] <= sa[0])):
            kinds.append('channel_mismatch')
            notes.append(f"{src} is on {s['rf_frequency']}/{s.get('rf_chanbw') or '?'} MHz and {tgt} on "
                         f"{t['rf_frequency']}/{t.get('rf_chanbw') or '?'} MHz: they can't hear each other")
        d_lqm = _num(r['distance'])
        if (d_lqm and s.get('lat') is not None and t.get('lat') is not None
                and (s['lat'] or s['lon']) and (t['lat'] or t['lon'])):
            d_geo = _haversine_m(s, t)
            if d_geo is not None and abs(d_lqm - d_geo) > max(DISTANCE_SLACK_M, DISTANCE_RATIO * d_geo):
                kinds.append('distance')
                notes.append(f"LQM says {d_lqm / 1000:.1f} km, the stored coordinates {d_geo / 1000:.1f} km")
        mac = (r['mac_address'] or '').lower().strip()
        others = sorted(names_by_mac.get(mac, set()) - {tgt}) if mac else []
        if others:
            kinds.append('mac_conflict')
            notes.append(f"MAC {mac} is also tracked as {', '.join(others)}")
        if not kinds:
            continue
        exclude = any(k in EXCLUDE_KINDS for k in kinds)
        out.append({'source': src, 'target': tgt, 'kinds': kinds,
                    'labels': [KIND_TEXT[k] for k in kinds], 'notes': notes,
                    'exclude': exclude, 'quality': r['quality'], 'snr': r['snr'],
                    'distance_m': d_lqm, 'mac': r['mac_address'], 'status': r['status'],
                    'last_seen': r['last_seen'],
                    'severity': 'info' if exclude and r['status'] != 'good' else
                                ('warning' if not exclude or 'channel_mismatch' in kinds else 'info')})
    out.sort(key=lambda x: (not x['exclude'], x['source'], x['target']))
    return out


def excluded_pairs(conn=None):
    """{(source, target)} directional RF trackers to leave out of RF checks."""
    return {(g['source'], g['target']) for g in find(conn) if g['exclude']}


def excluded_unordered(conn=None):
    """Sorted pairs with ghost trackers (both kinds that exclude are symmetric:
    a cable joins both ends, and a radio that is off is off for both)."""
    return {tuple(sorted(p)) for p in excluded_pairs(conn)}


def report():
    items = find()
    counts = defaultdict(int)
    for g in items:
        for k in g['kinds']:
            counts[k] += 1
    return {'generated_at': datetime.now().strftime(TS_FMT), 'ghosts': items,
            'counts': dict(counts), 'excluded': sum(1 for g in items if g['exclude']),
            'kinds': KIND_TEXT}

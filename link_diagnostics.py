"""
Pair diagnostics: everything we know about the RF link between two nodes, and
an attempt to say what is wrong with it.

Looks at both directions of the link (each end's LQM tracker for the other),
including a neighbor the far end only knows by MAC address, plus history,
state changes, both nodes' health and configuration, and colocated radios.
Read-only over SQLite except `run_live_checks`, which pings and traceroutes
between the two nodes through their own CGI.

Each check appends findings: {severity, category, title, detail, evidence,
recommendation}. Severity is critical | warning | info | ok.
"""

import json
import math
import re
from datetime import datetime, timedelta

import config
import database
import gateway
import link_health
import rf_stats

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2, 'ok': 3}

TX_FAIL_WARNING = 10.0      # % of frames the sender gives up on
TX_FAIL_CRITICAL = 50.0
RETRY_WARNING = 30.0        # % of frames needing retries
LQ_POOR = 50                # % of the sender's hellos the receiver hears
SIGNAL_HOT = -40            # dBm: strong enough to overload a receiver
SIGNAL_WEAK = -80           # dBm
COLOCATED_METERS = 150
NEARBY_KM = 10.0            # how far to look for interfering transmitters
BEAM_HALF_ANGLE = 30.0      # degrees either side of a neighbor's bearing
MIN_BEAM_LINK_M = 300       # shorter links don't say where an antenna points
HOT_SNR = 40.0              # dB: stronger than any link needs
HOT_SIGNAL_DBM = -45.0
NEAR_FAR_GAP = 10.0         # dB between the loud neighbor and a struggling one
NEAR_FAR_GAP_STRONG = 25.0
TARGET_SNR = 30.0           # comfortable SNR to turn a loud transmitter down to
RECEIVE_OK_LQ = 80
BABEL_INFINITY = 65535
RELEASE_FW = re.compile(r'^\d+\.\d+\.\d+\.\d+$')
# Highest 2x2 802.11ac PHY rate per channel width (MCS9, short GI): a rate above
# the 20/40 MHz ceiling proves the link runs at least the next width up.
WIDTH_CEILINGS = ((20, 173.4), (40, 400.0), (80, 866.7))


# ------------------------------------------------------------- helpers ----

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


TS_FMT = '%Y-%m-%d %H:%M:%S'


def _parse_ts(value):
    try:
        return datetime.strptime(value[:19], TS_FMT) if value else None
    except (TypeError, ValueError):
        return None


def _fmt_age(seconds):
    if seconds is None:
        return 'never'
    if seconds < 120:
        return f'{int(seconds)} s ago'
    if seconds < 7200:
        return f'{int(seconds // 60)} min ago'
    if seconds < 172800:
        return f'{seconds / 3600:.1f} h ago'
    return f'{seconds / 86400:.1f} days ago'


def _mac_key(mac):
    """Last five octets of a MAC: the first octet changes when a radio derives
    a locally administered address for a new interface (e.g. fe:.. vs d0:..)."""
    parts = re.split('[:-]', (mac or '').lower())
    return ':'.join(parts[1:]) if len(parts) == 6 else None


def _haversine_m(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def channel_relation(delta_mhz, width_mhz):
    """How a radio delta_mhz away relates to a channel width_mhz wide (assumes
    the other radio uses the same width)."""
    if delta_mhz is None:
        return 'unknown'
    width = width_mhz or 20
    if delta_mhz == 0:
        return 'same'
    if delta_mhz < width:
        return 'overlap'
    if delta_mhz <= width + 20:
        return 'adjacent'
    if delta_mhz <= width + 40:
        return 'near'
    return 'clear'


def _fspl_db(distance_m, freq_mhz):
    if not distance_m or not freq_mhz:
        return None
    return round(20 * math.log10(distance_m / 1000.0) + 20 * math.log10(freq_mhz) + 32.44, 1)


def _stale_after_seconds():
    try:
        poll = int(database.get_setting('poll_interval', str(config.POLL_INTERVAL)))
    except (TypeError, ValueError):
        poll = config.POLL_INTERVAL
    return max(600, 3 * poll)


def _infer_width(bitrates):
    top = max([b for b in bitrates if b] or [0])
    if not top:
        return None
    for width, ceiling in WIDTH_CEILINGS:
        if top <= ceiling:
            return width
    return 160


def _counter_rate(rows, part_key, whole_key):
    """% of `whole` that was `part` across consecutive cumulative-counter
    samples, skipping counter resets (reboots)."""
    part = whole = 0
    prev = None
    for row in rows:
        p, w = row.get(part_key), row.get(whole_key)
        if p is None or w is None:
            continue
        if prev and w >= prev[1] and p >= prev[0]:
            part += p - prev[0]
            whole += w - prev[1]
        prev = (p, w)
    return (_pct(part, whole), whole) if whole else (None, 0)


# ------------------------------------------------------------- gather -----

def _load_tracker(row):
    try:
        tracker = json.loads(row.get('raw_tracker') or '{}')
    except (TypeError, ValueError):
        tracker = {}
    return tracker if isinstance(tracker, dict) else {}


def _find_direction_row(cur, src, dst, dst_macs):
    """The link row in which `src` reports `dst`; falls back to a MAC-only
    neighbor of `src` whose MAC matches one `dst` is known by."""
    row = cur.execute('SELECT * FROM links WHERE source_node = ? AND target_node = ?',
                      (src, dst)).fetchone()
    named = dict(row) if row else None
    wired = None
    if named and (named.get('link_type') or '').upper() != 'RF':
        # A DtD cable or tunnel between the pair, not an RF tracker.
        wired, named = named, None
    alias = None
    if dst_macs:
        for r in cur.execute('''
            SELECT * FROM links WHERE source_node = ? AND target_node != ?
              AND UPPER(link_type) = 'RF'
              AND (target_node LIKE 'mac-%' OR identity_status != 'named')
            ORDER BY last_seen DESC
        ''', (src, dst)):
            # Only a current alias counts; old MAC-only entries are history.
            if _mac_key(r['mac_address']) in dst_macs and r['status'] != 'removed':
                alias = dict(r)
                break
    return named, alias, wired


def _direction(cur, src, dst, dst_macs, start, now, stale_after):
    named, alias, wired = _find_direction_row(cur, src, dst, dst_macs)

    def age(row):
        seen = _parse_ts(row.get('last_seen')) if row else None
        return (now - seen).total_seconds() if seen else None

    # Prefer whichever row is current: a named row that went stale while a
    # MAC-only alias is fresh means the far end lost the peer's identity.
    row, via_alias = named, False
    if alias and (named is None or named.get('status') == 'removed'
                  or (age(alias) or 1e12) < (age(named) or 1e12)):
        row, via_alias = alias, True

    d = {
        'source': src, 'target': dst, 'found': row is not None,
        'via_alias': row['target_node'] if via_alias else None,
        'named_row': None,
        'wired_link_type': wired.get('link_type') if wired else None,
    }
    if named and via_alias:
        d['named_row'] = {'status': named.get('status'), 'last_seen': named.get('last_seen'),
                          'tracker': _load_tracker(named)}
    if not row:
        return d

    t = _load_tracker(row)
    seen_age = age(row)
    avg_tx = _num(t.get('avg_tx_packets'))
    d.update({
        'row_target': row['target_node'],
        'status': row.get('status'),
        'first_seen': row.get('first_seen'),
        'last_seen': row.get('last_seen'),
        'age_seconds': seen_age,
        'fresh': seen_age is not None and seen_age <= stale_after and row.get('status') != 'removed',
        'drop_count': row.get('drop_count'),
        'link_type': row.get('link_type'),
        'mac': row.get('mac_address'),
        'quality': _num(t.get('quality', row.get('quality'))),
        'lq': _num(t.get('lq')),
        'tx_quality': _num(t.get('tx_quality')),
        'ping_quality': _num(t.get('ping_quality')),
        'snr': _num(t.get('snr', row.get('snr'))),
        'rev_snr': _num(t.get('rev_snr', row.get('rev_snr'))),
        'signal': _num(t.get('signal', row.get('signal'))),
        'noise': _num(t.get('noise', row.get('noise'))),
        'tx_bitrate': _num(t.get('tx_bitrate')),
        'rx_bitrate': _num(t.get('rx_bitrate')),
        'tx_packets': _num(t.get('tx_packets')),
        'tx_fail_pct_lifetime': _pct(_num(t.get('tx_fail')), _num(t.get('tx_packets'))),
        'tx_retry_pct_lifetime': _pct(_num(t.get('tx_retries')), _num(t.get('tx_packets'))),
        'tx_fail_pct_recent': _pct(_num(t.get('avg_tx_fail')), avg_tx) if avg_tx else None,
        'tx_retry_pct_recent': _pct(_num(t.get('avg_tx_retries')), avg_tx) if avg_tx else None,
        'routable': t.get('routable'),
        'txcost': _num(t.get('txcost')),
        'rxcost': _num(t.get('rxcost')),
        'route_count': _num(t.get('route_count', t.get('babel_route_count'))),
        'lqm_rtt_ms': _num(t.get('rtt')),
        'device': t.get('device'),
        'subdevice': t.get('subdevice'),
        'mode': t.get('mode'),
        'band': t.get('band'),
        'distance_m': _num(t.get('distance', row.get('distance'))),
        'hostname_known': bool(t.get('hostname')),
        'peer_firmware': t.get('firmware_version'),
        'peer_model': t.get('model'),
        'user_blocks': t.get('user_blocks'),
        'blocked': row.get('blocked'),
        'blocked_reason': row.get('blocked_reason'),
        'lqm_status_message': row.get('lqm_status_message'),
        'connected_seconds': _num(t.get('connected_time')),
    })

    # History over the window, keyed by whichever name this direction is under.
    hist = [dict(r) for r in cur.execute('''
        SELECT timestamp, sample_type, quality, snr, rev_snr, signal, lq, tx_quality,
               ping_quality, tx_packets, tx_fail, tx_retries, tx_bitrate, rx_bitrate,
               txcost, routable, ping_avg, ping_loss, jitter, throughput_tx, throughput_rx
        FROM link_history
        WHERE source_node = ? AND target_node IN (?, ?) AND timestamp >= ?
        ORDER BY timestamp
    ''', (src, dst, row['target_node'], start))]
    lqm_rows = [h for h in hist if h.get('tx_packets') is not None]
    fail_pct, frames = _counter_rate(lqm_rows, 'tx_fail', 'tx_packets')
    retry_pct, _ = _counter_rate(lqm_rows, 'tx_retries', 'tx_packets')

    def stat(key, fn):
        vals = [h[key] for h in hist if h.get(key) is not None]
        return round(fn(vals), 1) if vals else None

    avg = lambda v: sum(v) / len(v)
    routable_vals = [h['routable'] for h in lqm_rows if h.get('routable') is not None]
    d['window'] = {
        'samples': len(hist),
        'lqm_samples': len(lqm_rows),
        'quality_avg': stat('quality', avg), 'quality_min': stat('quality', min),
        'lq_avg': stat('lq', avg), 'tx_quality_avg': stat('tx_quality', avg),
        'ping_quality_avg': stat('ping_quality', avg),
        'snr_avg': stat('snr', avg), 'snr_min': stat('snr', min), 'snr_max': stat('snr', max),
        'signal_avg': stat('signal', avg), 'signal_min': stat('signal', min),
        'signal_max': stat('signal', max),
        'tx_bitrate_avg': stat('tx_bitrate', avg), 'tx_bitrate_max': stat('tx_bitrate', max),
        'rx_bitrate_avg': stat('rx_bitrate', avg), 'rx_bitrate_max': stat('rx_bitrate', max),
        'tx_fail_pct': fail_pct, 'tx_retry_pct': retry_pct, 'tx_frames': frames,
        'routable_pct': _pct(sum(1 for v in routable_vals if v), len(routable_vals)),
        'ping_avg': stat('ping_avg', avg), 'ping_loss': stat('ping_loss', avg),
        'jitter': stat('jitter', avg), 'ping_samples': sum(1 for h in hist if h.get('ping_avg') is not None),
        'iperf': [{'t': h['timestamp'], 'tx': h['throughput_tx'], 'rx': h['throughput_rx']}
                  for h in hist if h.get('throughput_tx') is not None],
    }
    d['history'] = hist
    return d


def _node(cur, name, start, now):
    row = cur.execute('SELECT * FROM nodes WHERE name = ?', (name,)).fetchone()
    n = dict(row) if row else {'name': name}
    h = cur.execute('''
        SELECT COUNT(*) AS polls, SUM(reachable = 0) AS unreachable, SUM(degraded = 1) AS degraded,
               AVG(CASE WHEN reachable = 1 THEN response_ms END) AS avg_response_ms,
               MAX(CASE WHEN reachable = 1 THEN response_ms END) AS max_response_ms,
               MAX(load1) AS max_load1, AVG(channel_busy) AS channel_busy
        FROM node_health_history
        WHERE node_name = ? AND timestamp >= ? AND probe_via IS NULL
    ''', (name, start)).fetchone()
    n['health'] = dict(h)
    n['health']['unreachable_pct'] = _pct(h['unreachable'] or 0, h['polls'])
    n['reboots'] = [dict(r) for r in cur.execute('''
        SELECT timestamp, details FROM events
        WHERE node_name = ? AND event_type = 'node_reboot' AND timestamp >= ?
        ORDER BY timestamp
    ''', (name, start))]
    seen = _parse_ts(n.get('last_seen'))
    up = n.get('uptime_seconds')
    n['booted_at'] = (seen - timedelta(seconds=up)).strftime(TS_FMT) if seen and up else None
    n['known_macs'] = sorted({_mac_key(r[0]) for r in cur.execute(
        'SELECT mac_address FROM links WHERE target_node = ? AND mac_address IS NOT NULL',
        (name,)) if _mac_key(r[0])})
    return n


def _neighbors(cur, name, nodes_geo, stale_after, now):
    """Every RF neighbor `name` currently hears, with how well each side hears
    the other: [{node, snr, signal, lq, quality, ..., their_lq, their_snr}]."""
    me = nodes_geo.get(name)
    out = []
    rows = cur.execute('''
        SELECT * FROM links WHERE source_node = ? AND UPPER(link_type) = 'RF' AND status != 'removed'
    ''', (name,)).fetchall()
    for r in rows:
        seen = _parse_ts(r['last_seen'])
        if not seen or (now - seen).total_seconds() > stale_after:
            continue
        t = _load_tracker(dict(r))
        back = cur.execute('''
            SELECT raw_tracker, snr, last_seen FROM links
            WHERE source_node = ? AND target_node = ? AND UPPER(link_type) = 'RF' AND status != 'removed'
        ''', (r['target_node'], name)).fetchone()
        bt = _load_tracker(dict(back)) if back else {}
        other = nodes_geo.get(r['target_node'])
        dist = brg = None
        if me and other:
            dist = _haversine_m(me[0], me[1], other[0], other[1])
            brg = _bearing(me[0], me[1], other[0], other[1]) if dist and dist > 1 else None
        out.append({
            'node': r['target_node'],
            'mac_only': r['target_node'].startswith('mac-'),
            'snr': _num(t.get('snr', r['snr'])),
            'signal': _num(t.get('signal', r['signal'])),
            'lq': _num(t.get('lq')),
            'quality': _num(t.get('quality', r['quality'])),
            'tx_quality': _num(t.get('tx_quality')),
            'blocked': bool(r['blocked']) or bool(t.get('user_blocks')),
            'routable': t.get('routable'),
            'their_lq': _num(bt.get('lq')),
            'their_snr': _num(bt.get('snr', back['snr'] if back else None)),
            'distance_m': round(dist) if dist is not None else None,
            'bearing': round(brg) if brg is not None else None,
        })
    for n in out:
        n['hot'] = ((n['snr'] or 0) >= HOT_SNR) or ((n['signal'] if n['signal'] is not None else -999) >= HOT_SIGNAL_DBM)
        n['poor'] = ((n['lq'] is not None and n['lq'] < LQ_POOR)
                     or (n['quality'] is not None and n['quality'] < LQ_POOR) or n['blocked'])
    return sorted(out, key=lambda n: -(n['snr'] or -99))


def _span_text(r):
    if r.get('low') is None:
        return f"{r['frequency']:.0f} MHz" if r.get('frequency') else 'channel unknown'
    return f"{r['low']:.0f}–{r['high']:.0f} MHz"


def _nearby_radios(name, cm_radios, neighbors, link_freq, width, exclude, cabled=()):
    """Radios that can interfere with `name`'s receiver, judged like the Channel
    Map: radios at its site (within COLOCATED_METERS, or cabled to it by DtD
    even without a location), radios its antenna faces (toward its RF
    neighbours, or inside its stored beam) and radios whose antenna faces it,
    within channel_map.CONFLICT_KM. Each radio's own channel width is used for
    the relation; radios seen in the last 7 days count (`recent`)."""
    import channel_map as cm  # channel_map imports this module
    import infra_audit as ia
    me = cm_radios.get(name) or {'name': name, 'lat': None, 'lon': None}
    mine = {'frequency': me.get('frequency') or link_freq,
            'chanbw': me.get('chanbw') or (width if not me.get('frequency') else None)}
    my_lo, my_hi = ia._span(mine) or (None, None)
    located = me.get('lat') is not None and me.get('lon') is not None
    beams = [n for n in neighbors if n.get('bearing') is not None
             and (n.get('distance_m') or 0) >= MIN_BEAM_LINK_M]
    out = []
    for r in cm_radios.values():
        if r['name'] in exclude or r['name'] == name:
            continue
        is_cabled = r['name'] in cabled
        r_located = r.get('lat') is not None and r.get('lon') is not None
        dist = _haversine_m(me['lat'], me['lon'], r['lat'], r['lon']) if located and r_located else None
        if dist is None and not is_cabled:
            continue
        if dist is not None and dist > cm.CONFLICT_KM * 1000 and not is_cabled:
            continue
        site = is_cabled or dist <= COLOCATED_METERS
        toward = None
        if not site and dist is not None:
            brg = _bearing(me['lat'], me['lon'], r['lat'], r['lon'])
            for n in beams:
                if _angle_diff(brg, n['bearing']) <= BEAM_HALF_ANGLE and dist <= 1.5 * n['distance_m']:
                    toward = n
                    break
        if dist is not None and not site:
            faces = bool(toward) or (me.get('azimuth') is not None and cm._in_beam(me, r, dist))
            aims = r.get('azimuth') is not None and cm._in_beam(r, me, dist)
            omni_faces = me.get('azimuth') is None and cm._in_beam(me, r, dist)
        else:
            faces = aims = omni_faces = site
        if not (site or faces or aims or omni_faces):
            continue
        theirs = {'frequency': r.get('frequency'), 'chanbw': r.get('chanbw')}
        transmitting = r.get('frequency') is not None and r.get('rf_status') in (None, 'on')
        rel = ia.pair_relation(mine, theirs) if transmitting and mine['frequency'] else 'unknown'
        if rel == 'unknown' and not site:
            continue  # unknown channel and not at the site: nothing to say
        if rel in ('clear', 'same') and not (site or toward):
            continue  # only list far radios that matter
        gap = ia.spectral_gap(mine, theirs) if rel != 'unknown' else None
        geo_sev = None
        if rel in ('overlap', 'adjacent', 'near'):
            geo_sev = cm._severity(rel, site, (dist or 0) / 1000, faces or omni_faces, aims)
            if geo_sev is None and not site and not toward:
                continue
        level = None
        if toward:
            # Received level relative to the neighbor in that beam, assuming
            # similar transmit power toward us (free-space distance ratio).
            level = round(20 * math.log10(toward['distance_m'] / max(dist, 10.0)), 1)
        lo, hi = ia._span(theirs) or (None, None)
        out.append({
            'name': r['name'], 'model': r.get('model'), 'frequency': r.get('frequency'),
            'chanbw': r.get('chanbw'), 'low': lo, 'high': hi,
            'channel': r.get('channel'), 'is_active': bool(r.get('active')),
            'recent': bool(r.get('recent', r.get('active'))),
            'rf_off': r.get('frequency') is not None and not transmitting,
            'distance_m': round(dist) if dist is not None else None,
            'delta_mhz': abs(r['frequency'] - mine['frequency'])
                         if r.get('frequency') and mine['frequency'] else None,
            'gap_mhz': gap, 'relation': rel, 'site': site, 'cabled': is_cabled,
            'in_beam_of': toward['node'] if toward else None,
            'faces_end': bool(faces), 'aims_at_end': bool(aims and not site),
            'omni': (r.get('beamwidth') or 360) >= 360 or r.get('azimuth') is None,
            'geometry_severity': geo_sev, 'level_vs_neighbor_db': level,
        })
    order = {'overlap': 0, 'adjacent': 1, 'same': 2, 'near': 3, 'clear': 4, 'unknown': 5}
    out.sort(key=lambda c: (order[c['relation']], c['distance_m'] if c['distance_m'] is not None else 0))
    return {'radios': out, 'span': {'low': my_lo, 'high': my_hi, 'frequency': mine['frequency'],
                                     'chanbw': mine['chanbw'], 'width_from_settings': bool(me.get('chanbw')),
                                     'located': located, 'cabled': len(cabled)}}


# ------------------------------------------------------------- checks -----

def _add(findings, severity, category, title, detail, evidence=None, recommendation=None):
    findings.append({'severity': severity, 'category': category, 'title': title,
                     'detail': detail, 'evidence': evidence or [],
                     'recommendation': recommendation})


def _check_visibility(ctx, f):
    wired = [d['wired_link_type'] for d in ctx['directions'] if d.get('wired_link_type')]
    if wired:
        a, b = ctx['pair']
        _add(f, 'info', 'visibility', f'{a} and {b} are also connected by {wired[0]}',
             f"Babel prefers the {wired[0]} connection, so the RF path between them is a side path "
             f"and does not carry their traffic. What matters for the mesh is how each one's radio "
             f"does with its other RF neighbors (see below).")
    for d in ctx['directions']:
        src, dst = d['source'], d['target']
        if not d['found'] and wired:
            _add(f, 'info', 'visibility', f'{src} does not hear {dst} over RF',
                 f"Expected when they are wired together and their antennas point elsewhere.")
            continue
        if not d['found']:
            _add(f, 'critical', 'visibility', f'{src} does not list {dst} as a neighbor',
                 f"{src}'s LQM has no tracker for {dst} (by name or by any MAC {dst} is known by). "
                 f"{src} is not hearing {dst} at all, so the link is one-way at best.",
                 recommendation=f"Check that {dst} is transmitting on the same channel/width/SSID "
                                f"mode as {src}, and look at {src}'s Neighbor Status page.")
            continue
        if d['via_alias']:
            named = d.get('named_row') or {}
            _add(f, 'warning', 'visibility', f'{src} sees {dst} only by MAC address',
                 f"{src} tracks {dst} as '{d['via_alias']}' (MAC {d['mac']}) without a hostname. "
                 f"{dst}'s hostname/IP announcements are not reaching {src}, which is what happens "
                 f"when {src} receives too little of {dst}'s traffic.",
                 [f"Named entry for {dst} last seen {named.get('last_seen') or 'never'} "
                  f"({named.get('status') or 'missing'})",
                  f"MAC-only entry last seen {d['last_seen']}"],
                 f"Treat '{d['via_alias']}' as {dst}. Fixing the receive problem at {src} restores the name.")
        if not d['fresh']:
            _add(f, 'critical', 'visibility', f'{src} has not heard {dst} recently',
                 f"{src}'s tracker for {dst} was last updated {_fmt_age(d['age_seconds'])} "
                 f"(status {d['status']}).",
                 recommendation=f"Confirm {dst} is up and on-channel; check {src}'s receive path.")


def _check_delivery(ctx, f):
    """Per transmit direction: does src's traffic reach dst? Uses src's TX
    failure counters and dst's LQ (share of src's hellos dst hears)."""
    dirs = {(d['source'], d['target']): d for d in ctx['directions']}
    for d in ctx['directions']:
        if not d['found'] or not d['fresh']:
            continue
        src, dst = d['source'], d['target']
        back = dirs.get((dst, src)) or {}
        w = d.get('window') or {}
        fail = w.get('tx_fail_pct')
        if fail is None:
            fail = d.get('tx_fail_pct_recent')
        retry = w.get('tx_retry_pct') if w.get('tx_retry_pct') is not None else d.get('tx_retry_pct_recent')
        heard = back.get('lq') if back.get('found') and back.get('fresh') else None
        evidence = []
        if fail is not None:
            evidence.append(f"{src} TX failure {fail}% of frames"
                            + (f" ({int(w['tx_frames'])} frames in window)" if w.get('tx_frames') else ''))
        if d.get('tx_fail_pct_lifetime') is not None:
            evidence.append(f"{src} TX failure since association: {d['tx_fail_pct_lifetime']}%")
        if retry is not None:
            evidence.append(f"{src} TX retries {retry}% of frames")
        if heard is not None:
            evidence.append(f"{dst} hears {heard:.0f}% of {src}'s hellos (LQ)")
        if d.get('tx_quality') is not None:
            evidence.append(f"{src} tx_quality {d['tx_quality']:.1f}%")

        broken = (fail is not None and fail >= TX_FAIL_CRITICAL) or (heard is not None and heard < LQ_POOR)
        lossy = (fail is not None and fail >= TX_FAIL_WARNING) or (retry is not None and retry >= RETRY_WARNING)
        if (broken or lossy) and any(x.get('wired_link_type') for x in ctx['directions']):
            d['delivery'] = 'side-path'
            _add(f, 'info', 'delivery', f'RF from {src} to {dst} is poor, but they are wired',
                 f"Their traffic uses the cable, so this side path does not matter by itself.", evidence)
            continue
        if broken:
            d['delivery'] = 'broken'
            _add(f, 'critical', 'delivery', f'Traffic from {src} to {dst} is not getting through',
                 f"{dst} is failing to receive most of what {src} transmits. The reverse direction "
                 f"is judged separately below.", evidence,
                 f"Look for a problem at {dst}'s receiver (interference, a colocated transmitter, "
                 f"desense) or at {src}'s transmitter (TX power, chain, configuration).")
        elif lossy:
            d['delivery'] = 'lossy'
            _add(f, 'warning', 'delivery', f'Lossy delivery from {src} to {dst}',
                 f"A significant share of {src}'s frames to {dst} need retries or fail.", evidence,
                 'Check for interference near the receiver and confirm the distance/ACK timing setting.')
        elif fail is not None or heard is not None:
            d['delivery'] = 'ok'
            _add(f, 'ok', 'delivery', f'{src} → {dst} delivery is healthy',
                 'Frames are being delivered with few failures.', evidence)


def _check_rf(ctx, f):
    dirs = [d for d in ctx['directions'] if d['found']]
    if not dirs:
        return
    for d in dirs:
        snr, sig = d.get('snr'), d.get('signal')
        if snr is not None:
            rating = link_health.classify_snr({'snr': snr})['rating']
            if rating == 'poor':
                _add(f, 'critical', 'rf', f"{d['source']} receives {d['target']} at only {snr:.0f} dB SNR",
                     'SNR this low cannot sustain a reliable link.',
                     [f"signal {sig} dBm" if sig is not None else 'signal unknown'],
                     'Check antenna alignment, obstructions (Fresnel zone), and cabling.')
            elif rating == 'marginal':
                _add(f, 'warning', 'rf', f"{d['source']} receives {d['target']} at a marginal {snr:.0f} dB SNR",
                     'Usable but leaves little fade margin.', [],
                     'Check alignment and path clearance.')
        if sig is not None and sig > SIGNAL_HOT:
            _add(f, 'warning', 'rf', f"Signal at {d['source']} is very strong ({sig:.0f} dBm)",
                 'Signals this hot can overload the receiver and cause errors.', [],
                 'Reduce TX power on the far end.')
        elif sig is not None and sig < SIGNAL_WEAK:
            _add(f, 'warning', 'rf', f"Weak signal at {d['source']} ({sig:.0f} dBm)",
                 'Little margin above the noise.', [], 'Check alignment and path.')
        w = d.get('window') or {}
        if w.get('snr_max') is not None and w.get('snr_min') is not None and w['snr_max'] - w['snr_min'] >= 10:
            _add(f, 'warning', 'rf', f"SNR at {d['source']} swings {w['snr_max'] - w['snr_min']:.0f} dB",
                 'Large SNR swings suggest fading, intermittent interference, or a moving obstruction.',
                 [f"SNR range {w['snr_min']}–{w['snr_max']} dB in window"])

    # Directional SNR imbalance between the two ends' views of each other.
    if len(dirs) == 2 and all(d.get('snr') is not None for d in dirs):
        a, b = dirs
        gap = abs(a['snr'] - b['snr'])
        if gap >= link_health.SNR_ASYMMETRY_POOR:
            _add(f, 'warning', 'rf', f'SNR differs by {gap:.0f} dB between directions',
                 'Unequal TX power, a damaged chain, or local noise at one end.',
                 [f"{a['source']} hears {a['target']} at {a['snr']:.0f} dB",
                  f"{b['source']} hears {b['target']} at {b['snr']:.0f} dB"],
                 'Match TX power and check the weaker side for local noise.')

    # Path-loss sanity: is the received signal what the distance predicts?
    # Prefer the nodes' own coordinates; LQM's distance can be wrong.
    d0 = next((d for d in dirs if d.get('signal') is not None), None)
    geo = ctx.get('geo_distance_m')
    dist_m = geo if geo is not None else (d0 or {}).get('distance_m')
    if d0 and geo is not None and d0.get('distance_m') and abs(d0['distance_m'] - geo) > max(500, 0.5 * geo):
        _add(f, 'info', 'rf', 'LQM distance disagrees with node locations',
             f"LQM reports {d0['distance_m'] / 1000:.2f} km but the nodes' coordinates are "
             f"{geo / 1000:.2f} km apart. One of the node locations is probably wrong.")
    freq = ctx['link_freq']
    if d0 and freq and dist_m and dist_m >= 1000:
        d0 = dict(d0, distance_m=dist_m)
        fspl = _fspl_db(d0['distance_m'], freq)
        budget = round(d0['signal'] + fspl, 1)  # = TX power + both antenna gains - losses
        ctx['rf_budget'] = {'distance_m': d0['distance_m'], 'frequency_mhz': freq,
                            'fspl_db': fspl, 'signal_dbm': d0['signal'],
                            'implied_power_plus_gains_db': budget}
        snrs = [d['snr'] for d in dirs if d.get('snr') is not None]
        strong = len(snrs) == 2 and min(snrs) >= link_health.SNR_GOOD
        if budget < 35:
            _add(f, 'warning', 'rf', 'Received signal is low for this distance',
                 f"{d0['distance_m'] / 1000:.1f} km at {freq:.0f} MHz has ~{fspl:.0f} dB free-space loss; "
                 f"a {d0['signal']:.0f} dBm signal implies only {budget:.0f} dB of TX power + antenna gain.",
                 [], 'Check alignment, obstructions, and cabling.')
        elif strong:
            _add(f, 'ok', 'rf', 'RF path is strong in both directions',
                 f"SNR {', '.join(f'{s:.0f}' for s in snrs)} dB; signal {d0['signal']:.0f} dBm over "
                 f"{d0['distance_m'] / 1000:.1f} km (~{fspl:.0f} dB free-space loss, {budget:.0f} dB TX power + "
                 f"antenna gain), consistent with aligned antennas. Weak signal or misalignment "
                 f"is not the problem.")


def _check_routing(ctx, f):
    for d in ctx['directions']:
        if not d['found'] or not d['fresh']:
            continue
        unusable = (d.get('routable') is False
                    or (d.get('txcost') or 0) >= BABEL_INFINITY
                    or (d.get('rxcost') or 0) >= BABEL_INFINITY)
        if unusable:
            ev = [f"routable: {d.get('routable')}"]
            for key in ('txcost', 'rxcost', 'route_count'):
                if d.get(key) is not None:
                    ev.append(f"{key}: {d[key]:.0f}" + (' (infinite)' if d[key] >= BABEL_INFINITY else ''))
            if d.get('lqm_status_message'):
                ev.append(d['lqm_status_message'])
            _add(f, 'critical', 'routing', f"{d['source']} will not route to {d['target']} over this link",
                 'Babel considers the link unusable in this direction, so traffic between the '
                 'two nodes takes another path (or none).', ev,
                 'This follows from the delivery problem: fix delivery and the cost recovers.')
    n = ctx['nodes']
    depths = [n[x].get('scan_depth') for x in ctx['pair']]
    if None not in depths and abs(depths[0] - depths[1]) > 1:
        far, near = (ctx['pair'][0], ctx['pair'][1]) if depths[0] > depths[1] else (ctx['pair'][1], ctx['pair'][0])
        _add(f, 'warning', 'routing', 'The collector reaches the two ends by very different paths',
             f"{near} is {min(depths)} hop(s) from the scan seed but {far} is {max(depths)}. "
             f"If this link carried traffic they would be at most one hop apart.",
             [f"{x}: scan depth {n[x].get('scan_depth')}" for x in ctx['pair']])


def _check_rates(ctx, f):
    for d in ctx['directions']:
        if not d['found'] or not d['fresh']:
            continue
        tx, rx = d.get('tx_bitrate'), d.get('rx_bitrate')
        if tx and rx and min(tx, rx) / max(tx, rx) < 0.3:
            low = 'transmit' if tx < rx else 'receive'
            _add(f, 'warning', 'rate', f"{d['source']}'s {low} rate collapsed ({tx:.0f} vs {rx:.0f} Mbps)",
                 f"{d['source']} sends at {tx:.0f} Mbps but receives at {rx:.0f} Mbps. Rate control "
                 f"drops the PHY rate when frames fail, so a {low} rate far below the other "
                 f"direction marks the bad direction.",
                 [f"window avg TX {d['window'].get('tx_bitrate_avg')} / RX {d['window'].get('rx_bitrate_avg')} Mbps"])
    tests = [t for d in ctx['directions'] for t in (d.get('window') or {}).get('iperf', [])]
    rates = [d.get('rx_bitrate') for d in ctx['directions'] if d.get('rx_bitrate')]
    if tests and rates:
        best = max(t['tx'] or 0 for t in tests)
        if best < 0.05 * max(rates):
            _add(f, 'warning', 'rate', f'iPerf throughput is tiny ({best:.2f} Mbps)',
                 f"Measured throughput is under 5% of the {max(rates):.0f} Mbps PHY rate, so the test "
                 f"traffic is crossing a lossy or detoured path.",
                 [f"{len(tests)} iPerf test(s) in window"])


def _check_quality_breakdown(ctx, f):
    for d in ctx['directions']:
        if not d['found'] or not d['fresh'] or d.get('quality') is None or d['quality'] >= 70:
            continue
        parts = {k: d.get(k) for k in ('tx_quality', 'ping_quality', 'lq') if d.get(k) is not None}
        if not parts:
            continue
        worst = min(parts, key=parts.get)
        explain = {
            'tx_quality': f"{d['source']}'s own transmissions to {d['target']} are failing",
            'ping_quality': f"pings across the link from {d['source']} are being lost",
            'lq': f"{d['source']} is missing {d['target']}'s hello packets",
        }[worst]
        _add(f, 'info', 'quality', f"Why {d['source']} rates the link {d['quality']:.0f}%",
             f"LQM quality combines TX success, ping success and hello reception. The weakest "
             f"part is {worst.replace('_', ' ')} ({parts[worst]:.1f}%): {explain}.",
             [f"{k}: {v:.1f}%" for k, v in parts.items()])


def _check_flaps(ctx, f):
    log = ctx['state_log']
    downs = [r for r in log if r['state'] in ('down', 'blocked') and r['origin'] == 'node_reported']
    lost = [r for r in log if r['state'] == 'scanner_unreachable']
    if len(downs) >= link_health.FLAP_THRESHOLD_WARNING:
        sev = 'critical' if len(downs) >= link_health.FLAP_THRESHOLD_CRITICAL else 'warning'
        _add(f, sev, 'stability', f'Link dropped {len(downs)} times in the window',
             'The nodes themselves reported the link going away repeatedly.',
             [f"{r['timestamp']}: {r['source_node']} → {r['target_node']} {r['state']}"
              f"{' (' + r['blocked_reason'] + ')' if r.get('blocked_reason') else ''}" for r in downs[-6:]])
    if lost:
        _add(f, 'info', 'stability', f'Collector lost sight of an end {len(lost)} time(s)',
             "These are the collector failing to poll a node, not the link itself dropping.",
             [f"{r['timestamp']}: {r['source_node']}" for r in lost[-4:]])
    for d in ctx['directions']:
        if d.get('user_blocks') or d.get('blocked'):
            _add(f, 'critical', 'stability', f"{d['source']} has the link to {d['target']} blocked",
                 f"Blocked ({d.get('blocked_reason') or 'user block'}).", [],
                 'Unblock it on the node if the block is not intended.')


def _check_config(ctx, f):
    a, b = (ctx['nodes'][x] for x in ctx['pair'])
    if a.get('rf_frequency') and b.get('rf_frequency') and a['rf_frequency'] != b['rf_frequency']:
        _add(f, 'critical', 'config', 'The two radios are on different frequencies',
             f"{a['name']} {a['rf_frequency']} MHz (ch {a.get('rf_channel')}) vs "
             f"{b['name']} {b['rf_frequency']} MHz (ch {b.get('rf_channel')}).", [],
             'Put both ends on the same channel and width.')
    fa, fb = a.get('firmware_version'), b.get('firmware_version')
    if fa and fb and fa != fb:
        _add(f, 'warning', 'config', 'The two ends run different firmware',
             f"{a['name']}: {fa}; {b['name']}: {fb}.", [], 'Run the same release on both ends.')
    dev = [n['name'] for n in (a, b) if n.get('firmware_version') and not RELEASE_FW.match(n['firmware_version'])]
    if dev:
        _add(f, 'info', 'config', 'Development (nightly) firmware in use',
             f"{', '.join(dev)} run a nightly build ({(a if a['name'] in dev else b)['firmware_version']}). "
             f"Nightly builds change radio drivers and link modes and are a common source of "
             f"new link problems.", [],
             'If the problem started after the upgrade, try the latest release on both ends.')

    # Firmware the far end recorded for the peer when it last heard it by name.
    for d in ctx['directions']:
        named = d.get('named_row') or {}
        old_fw = (named.get('tracker') or {}).get('firmware_version')
        now_fw = ctx['nodes'][d['target']].get('firmware_version')
        if old_fw and now_fw and old_fw != now_fw:
            _add(f, 'info', 'config', f"{d['target']}'s firmware changed since {d['source']} last knew it by name",
                 f"{d['source']} last identified {d['target']} on {named.get('last_seen')} running {old_fw}; "
                 f"it now runs {now_fw}.", [],
                 'Correlate the start of the problem with the upgrade.')

    # Interface/mode: mesh vs PtP/PtMP access point vs station. Prefer the mode
    # each node reports; otherwise infer from the tracker: an AP talks to a
    # station through a per-station WDS interface (wlan0.staN).
    roles = {}
    for name in ctx['pair']:
        role = RF_MODE_ROLES.get((ctx['nodes'][name].get('rf_mode') or '').lower())
        if role:
            roles[name] = role
    for d in ctx['directions']:
        if d['found'] and d['fresh'] and d['source'] not in roles:
            sub = d.get('subdevice') or ''
            roles[d['source']] = ('access point' if '.sta' in sub else
                                  'mesh' if (d.get('mode') or '') in ('meshsta', 'adhoc', 'mesh') else None)
    old_modes = {(d.get('named_row') or {}).get('tracker', {}).get('mode') for d in ctx['directions']} - {None}
    if roles and any(r in ('station', 'access point') for r in roles.values()):
        desc = ', '.join(f"{n} as {r}" for n, r in roles.items() if r)
        detail = f"The link runs in point-to-point AP/station mode ({desc})."
        if old_modes:
            detail += f" It previously ran in mesh mode ({', '.join(sorted(old_modes))})."
        _add(f, 'info', 'config', 'Link mode is AP/station', detail, [],
             "Confirm one end is configured as the PtP access point and the other as its station, "
             "with matching SSID, channel and width.")
    _check_pair_settings(ctx, f, roles)



def _receive_impaired(ctx, end):
    """Is `end` measurably failing to receive? (its LQ of the peer, delivery
    into it, or a receiver-wide problem)."""
    if end in ctx['receiver_problem']:
        return True
    for d in ctx['directions']:
        if d['source'] == end and d['found'] and d.get('lq') is not None and d['lq'] < RECEIVE_OK_LQ:
            return True
        if d['target'] == end and d.get('delivery') in ('broken', 'lossy'):
            return True
    return False


def _check_receivers(ctx, f):
    """An end that loses most of what all its neighbors send, while they hear
    it fine, has a receive-side problem that is not about this one link."""
    for end in ctx['pair']:
        nbrs = [n for n in ctx['neighbors'].get(end, []) if n.get('lq') is not None]
        poor = [n for n in nbrs if n['lq'] < LQ_POOR]
        if len(nbrs) < 2 or len(poor) < 2 or len(poor) < 0.6 * len(nbrs):
            continue
        hear_us = [n for n in poor if (n.get('their_lq') or 0) >= RECEIVE_OK_LQ]
        ctx['receiver_problem'].add(end)
        _add(f, 'critical', 'receiver', f"{end}'s receiver is missing traffic from most of its RF neighbors",
             f"{end} hears only a fraction of the hello packets from {len(poor)} of its {len(nbrs)} RF "
             f"neighbors" + (f", while {len(hear_us)} of them hear {end} fine" if hear_us else '') +
             f". The problem is on {end}'s receive side, not one particular link.",
             [f"{end} hears {n['node']} at SNR {n['snr']:.0f} dB, LQ {n['lq']:.0f}%"
              + (f"; {n['node']} hears {end} at LQ {n['their_lq']:.0f}%" if n.get('their_lq') is not None else '')
              for n in poor],
             f"Look for something overpowering {end}'s receiver: a very strong neighbor (see near-far "
             f"findings) or radios on overlapping/adjacent channels at its site or in its beam "
             f"(see 'Radios near each end'). Also check {end}'s cabling and radio.")


def _check_near_far(ctx, f):
    """A neighbor so strong at a receiver that the receiver loses other
    (weaker) neighbors, or even the strong one: the fix is lowering the loud
    transmitter's power."""
    pair = set(ctx['pair'])
    reported = set()
    for receiver, nbrs in ctx['neighbors'].items():
        measured = [n for n in nbrs if n.get('snr') is not None]
        for hot in [n for n in measured if n['hot']]:
            if receiver not in pair and hot['node'] not in pair:
                continue
            victims = [n for n in measured if n is not hot and n['poor']
                       and hot['snr'] - n['snr'] >= NEAR_FAR_GAP]
            self_poor = hot.get('lq') is not None and hot['lq'] < LQ_POOR
            key = (receiver, hot['node'])
            if key in reported:
                continue
            reported.add(key)
            cut = max(0, round(hot['snr'] - TARGET_SNR))
            level = f"{hot['snr']:.0f} dB SNR" + (f", {hot['signal']:.0f} dBm" if hot.get('signal') is not None else '')
            fix = _near_far_fix(ctx, receiver, hot['node'], cut)
            # Link to the power-cut advisor, prefilled, when the loud end may be cut.
            advisor = {'node': hot['node'], 'cut_db': cut} if hot['node'] not in ctx['infra'] else None
            if not victims and not self_poor:
                _add(f, 'info', 'near_far', f"{hot['node']} is louder than needed at {receiver} ({level})",
                     f"Nothing at {receiver} is suffering from it yet, but a signal this strong leaves "
                     f"little room for weaker neighbors.", [], fix)
                f[-1]['power_advisor'] = advisor
                continue
            gap = max([hot['snr'] - v['snr'] for v in victims] or [0])
            sev = 'critical' if (gap >= NEAR_FAR_GAP_STRONG or any(v['blocked'] for v in victims)) else 'warning'
            parts = []
            if victims:
                parts.append(f"{receiver}'s other neighbors are heard poorly: " + ', '.join(
                    f"{v['node']} (SNR {v['snr']:.0f} dB, LQ {v['lq']:.0f}%)" if v.get('lq') is not None
                    else f"{v['node']} (SNR {v['snr']:.0f} dB)" for v in victims[:5]) + '.')
            if self_poor:
                parts.append(f"Even {hot['node']} itself gets through only {hot['lq']:.0f}% of the time "
                             f"despite the strong signal, which points to the receiver being overloaded.")
            _add(f, sev, 'near_far', f"{hot['node']} may be overpowering {receiver}'s receiver",
                 f"{receiver} hears {hot['node']} at {level}, far more than a link needs. " + ' '.join(parts) +
                 f" A transmitter this close or this hot can desensitize the receiver and crowd out "
                 f"weaker stations.",
                 [f"{n['node']}: SNR {n['snr']:.0f} dB, signal {n['signal'] if n['signal'] is not None else '?'} dBm, "
                  f"LQ {n['lq'] if n['lq'] is not None else '?'}%" + (' (loud)' if n['hot'] else '')
                  + (' (struggling)' if n['poor'] else '') for n in measured[:8]],
                 fix)
            f[-1]['power_advisor'] = advisor


def _near_far_fix(ctx, receiver, loud, cut):
    """What to change when `loud` is too strong at `receiver`. Infrastructure
    nodes (towers) keep their power; the fix moves to the other end."""
    infra = ctx['infra']
    if loud not in infra:
        return (f"Lower {loud}'s TX power by about {cut} dB (to ~{TARGET_SNR:.0f} dB SNR at {receiver}), "
                f"then re-check {receiver}'s other neighbors. Reducing the closer, louder node's power is "
                f"what lets the others be heard.")
    if receiver not in infra:
        return (f"{loud} is infrastructure, so keep its power. Fix it at {receiver}: aim {receiver}'s "
                f"antenna slightly off {loud}, use a lower-gain antenna, or move {receiver} to a channel "
                f"or sector further from {loud}'s. Lowering {receiver}'s own TX power does not reduce what "
                f"it receives, but if {receiver} is also very loud at {loud} it helps there.")
    return (f"Both are infrastructure, so neither should lose power: increase their separation "
            f"(vertical spacing, aim away from each other, shielding) or move them further apart in "
            f"frequency.")


def _where(r, end):
    if r['site']:
        return (f"cabled (DtD) to {end}" if r['cabled'] and r['distance_m'] is None
                else f"{r['distance_m']} m away at {end}'s site" + (' (DtD-cabled)' if r['cabled'] else ''))
    text = f"{r['distance_m'] / 1000:.1f} km away"
    if r['in_beam_of']:
        text += (f" in {end}'s beam toward {r['in_beam_of']}, est. {r['level_vs_neighbor_db'] + 0.0:+.0f} dB "
                 f"vs {r['in_beam_of']}")
        if not r['omni'] and not r['aims_at_end']:
            text += " (but aimed elsewhere, so weaker)"
    elif r['omni']:
        text += ", omni" + (f" in {end}'s beam" if r['faces_end'] else '')
    elif r['faces_end'] and r['aims_at_end']:
        text += f", it and {end} face each other"
    elif r['aims_at_end']:
        text += f", its antenna points at {end}"
    else:
        text += f", in {end}'s antenna pattern"
    return text


def _rel_text(r):
    gap = r['gap_mhz']
    if gap is None:
        return r['relation']
    return f"{r['relation']}, " + (f"{-gap:.0f} MHz overlap" if gap < 0 else f"{gap:.0f} MHz gap")


def _check_interference(ctx, f):
    """Radios on overlapping / adjacent (and, at the site, near) channels at
    each end's site, in its antenna's beam, or aimed at it."""
    import infra_audit as ia
    checked, closest = 0, None
    any_bad = False
    for end in ctx['pair']:
        info = ctx['nearby'][end]
        if not info['span']['frequency']:
            continue
        radios = [r for r in info['radios'] if r['recent'] and not r['rf_off']]
        checked += len(radios)
        for r in radios:
            if r['gap_mhz'] is not None and r['relation'] != 'same' and (
                    closest is None or r['gap_mhz'] < closest[1]['gap_mhz']):
                closest = (end, r)
        impaired = _receive_impaired(ctx, end)
        bad = [r for r in radios if r['relation'] in ('overlap', 'adjacent')
               or (r['relation'] == 'near' and r['site'])]
        if bad:
            any_bad = True
            loud = [r for r in bad if r['relation'] == 'overlap'
                    and (r['site'] or r['geometry_severity'] == 'critical'
                         or (r['level_vs_neighbor_db'] or -99) >= -6)]
            geo_bad = any(r['geometry_severity'] in ('critical', 'warning') for r in bad)
            if impaired:
                sev = 'critical' if loud else 'warning'
            else:
                sev = 'warning' if loud or geo_bad else 'info'
            span = info['span']
            guard = ia.GUARD_MHZ
            idle = [r['name'] for r in bad if not r['is_active']]
            _add(f, sev, 'interference',
                 f"{len(bad)} radio(s) on overlapping/adjacent channels near {end}",
                 f"{end} receives on {_span_text(span)}"
                 + ('' if span['width_from_settings'] else ' (width estimated)')
                 + f". These radios transmit on spectrum that overlaps it or leaves less than "
                 f"{guard} MHz clear ({ia.NEAR_GUARD_MHZ} MHz for radios on the same mast). Overlapping "
                 f"channels can't be decoded and raise the noise floor; strong adjacent ones bleed in and "
                 f"desensitize the receiver, and either can act as a hidden node. "
                 + (f"{end}'s reception is impaired, so these are likely contributors."
                    if impaired else f"{end}'s reception looks fine now, so no impact is visible yet"
                    + (" but the geometry makes it a risk." if loud or geo_bad else "."))
                 + (f" Not answering now but seen in the last 7 days: {', '.join(idle)}." if idle else ''),
                 [f"{r['name']} {_span_text(r)} ({_rel_text(r)}), {_where(r, end)}"
                  + (f" [{r['geometry_severity']} on the Channel Map]" if r['geometry_severity'] else '')
                  for r in bad],
                 (f"Move this link or the listed radios apart so at least {guard} MHz stays clear "
                  f"(more on a shared mast), or narrow a channel. Level estimates assume similar power "
                  f"toward {end}; sectors aimed away will be weaker. The Channel Map planner can "
                  f"suggest a plan.") if (impaired or loud or geo_bad) else None)
        shared = [r['name'] for r in radios if r['relation'] == 'same' and r['site']]
        if shared:
            _add(f, 'info', 'interference', f"{len(shared)} radio(s) at {end}'s site share this channel",
                 f"{', '.join(shared)} use the same channel as this link. Radios on one channel take "
                 f"turns, so they compete for airtime; that matters if the channel is busy.")
    unlocated = [e for e in ctx['pair'] if not ctx['nearby'][e]['span']['located']]
    if not any_bad and ctx['link_freq']:
        unknown = sorted({r['name'] for e in ctx['pair'] for r in ctx['nearby'][e]['radios']
                          if r['relation'] == 'unknown'})
        detail = (f"Checked {checked} radio(s) at both ends' sites, in their antennas' beams, or aimed "
                  f"at them (within 25 km, seen in the last 7 days): none overlap or sit adjacent to "
                  f"this link's channel." if checked else
                  "No other radios with a known channel at either end's site, in their antennas' beams, or "
                  "aimed at them within 25 km.")
        if closest:
            end, r = closest
            detail += f" Closest in frequency: {r['name']} {_span_text(r)} ({_rel_text(r)}), {_where(r, end)}."
        if unlocated:
            detail += (f" No location stored for {' or '.join(unlocated)}, so only radios cabled to it "
                       f"(DtD) were checked there; set its location to check the area.")
        if unknown:
            detail += f" No channel stored for {', '.join(unknown)} at the site (2.4 GHz-only or radio off?)."
        _add(f, 'ok', 'interference', 'No nearby radios on overlapping or adjacent channels', detail)


def _check_noise_band(ctx, f):
    """Is the link's channel in a part of the band with known outside users
    (weather radar, busy Wi-Fi)? Uses the Channel planner's noise ranges."""
    import channel_planner as cp
    a = ctx['nearby'][ctx['pair'][0]]['span']
    if not a['frequency'] or a['low'] is None:
        return
    impaired = any(_receive_impaired(ctx, e) for e in ctx['pair'])
    hits = []
    for rng in cp.NOISE_RANGES:
        ov = min(a['high'], rng['high']) - max(a['low'], rng['low'])
        if ov > 0:
            hits.append((rng, ov))
    if not hits:
        return
    tdwr = any(r['low'] == 5600 and r['high'] == 5650 for r, _ in hits)
    heavy = any('heavy' in r['label'] for r, _ in hits)
    if tdwr:
        sev = 'critical' if impaired else 'warning'
    elif heavy:
        sev = 'warning' if impaired else 'info'
    else:
        sev = 'info'
    noise = [d.get('noise') for d in ctx['directions'] if d.get('noise') is not None]
    _add(f, sev, 'interference', f"Channel overlaps {hits[0][0]['label']}"
         + (f" and {len(hits) - 1} more noise range(s)" if len(hits) > 1 else ''),
         f"This link uses {_span_text(a)}. Outside users there are not mesh nodes, so they never show "
         f"up as neighbours; they only raise the noise floor or hit in bursts. "
         + ("Its reception is impaired, so check the noise floor over time (Link Watch) and consider "
            "moving the link." if impaired else "Its reception looks fine now.")
         + (f" Current noise estimate: {max(noise):.0f} dBm." if noise else ''),
         [f"{r['label']}: {ov:.0f} MHz of the channel" for r, ov in hits],
         (f"Keep the whole channel above 5650 MHz: TDWR airport weather radar (5600–5650) is strong and "
          f"pulsed, and AREDN radios don't avoid it. At {a['chanbw'] or 20:.0f} MHz wide that means a "
          f"centre of {5650 + (a['chanbw'] or 20) / 2 + 5:.0f} MHz or higher (or a narrower channel)."
          if tdwr else ("Prefer channels away from UNII-3 Wi-Fi (5735–5835) if noise is high."
                        if heavy and impaired else None)))


RF_MODE_ROLES = {'ptp': 'access point', 'ptmp': 'access point', 'ap': 'access point',
                 'sta': 'station', 'adhoc': 'mesh', 'meshsta': 'mesh', 'mesh': 'mesh'}


def _check_pair_settings(ctx, f, roles):
    """Do the two radios' reported settings fit together, and do they point at
    each other?"""
    a, b = (ctx['nodes'][x] for x in ctx['pair'])
    if not any(d['found'] for d in ctx['directions']):
        return
    ra, rb = roles.get(a['name']), roles.get(b['name'])
    if ra and rb:
        if ra == rb == 'station':
            _add(f, 'critical', 'config', 'Both ends are configured as stations',
                 'A PtP/PtMP link needs one access point; two stations cannot associate with each other.',
                 recommendation='Set one end to PtP/PtMP access point mode.')
        elif ra == rb == 'access point':
            _add(f, 'critical', 'config', 'Both ends are configured as access points',
                 'A PtP/PtMP link needs one station; two access points will not associate.',
                 recommendation='Set the far end to station mode.')
        elif 'mesh' in (ra, rb) and ra != rb:
            _add(f, 'critical', 'config', 'One end is in mesh mode and the other in AP/station mode',
                 f"{a['name']} is {ra}, {b['name']} is {rb}; these modes do not interoperate.",
                 recommendation='Use the same link mode on both ends.')
    if a.get('rf_ssid') and b.get('rf_ssid') and a['rf_ssid'] != b['rf_ssid']:
        _add(f, 'critical', 'config', 'The two radios use different SSIDs',
             f"{a['name']}: {a['rf_ssid']}; {b['name']}: {b['rf_ssid']}.",
             recommendation='Match the SSID (mesh/PtP network name) on both ends.')
    if a.get('rf_chanbw') and b.get('rf_chanbw') and a['rf_chanbw'] != b['rf_chanbw']:
        _add(f, 'critical', 'config', 'The two radios use different channel widths',
             f"{a['name']}: {a['rf_chanbw']:.0f} MHz; {b['name']}: {b['rf_chanbw']:.0f} MHz.",
             recommendation='Use the same channel width on both ends.')
    if None not in (a.get('lat'), a.get('lon'), b.get('lat'), b.get('lon')) and (ctx.get('geo_distance_m') or 0) >= MIN_BEAM_LINK_M:
        for me, other in ((a, b), (b, a)):
            az, beam = me.get('antenna_azimuth'), me.get('antenna_beamwidth')
            if az is None or not beam or beam >= 360:
                continue
            want = _bearing(me['lat'], me['lon'], other['lat'], other['lon'])
            off = _angle_diff(az, want)
            if off > beam / 2:
                _add(f, 'warning', 'config', f"{me['name']}'s azimuth setting does not point at {other['name']}",
                     f"{me['name']} is set to {az:.0f}° with a {beam:.0f}° beam, but {other['name']} is at "
                     f"{want:.0f}° ({off:.0f}° off). Either the azimuth setting is wrong or the antenna is "
                     f"mis-aimed.", [f"{me.get('antenna_desc') or 'antenna'}"],
                     f"Check the physical aim; correct the azimuth in {me['name']}'s settings if it is just a "
                     f"data-entry error.")


def _check_nodes(ctx, f):
    a, b = (ctx['nodes'][x] for x in ctx['pair'])
    for n in (a, b):
        h = n.get('health') or {}
        if h.get('unreachable_pct') and h['unreachable_pct'] >= 5:
            _add(f, 'warning', 'node', f"Collector could not poll {n['name']} {h['unreachable_pct']}% of the time",
                 'The collector reaches each node over the mesh; this reflects the path from the '
                 'collector, which may not include this link.', [f"{h['unreachable']} of {h['polls']} polls failed"])
        if h.get('avg_response_ms') and h['avg_response_ms'] > 2000:
            _add(f, 'warning', 'node', f"{n['name']} answers polls slowly ({h['avg_response_ms']:.0f} ms avg)",
                 'Slow sysinfo responses mean a loaded node or a long/lossy path from the collector.')
        if h.get('max_load1') and h['max_load1'] > 3:
            _add(f, 'warning', 'node', f"{n['name']} load reached {h['max_load1']:.1f}",
                 'High CPU load can delay LQM/Babel processing.')
        if n.get('reboots'):
            _add(f, 'warning', 'node', f"{n['name']} rebooted {len(n['reboots'])} time(s) in the window",
                 'Reboots drop the link and reset its counters.', [r['timestamp'] for r in n['reboots']])
    ba, bb = _parse_ts(a.get('booted_at')), _parse_ts(b.get('booted_at'))
    if ba and bb and abs((ba - bb).total_seconds()) < 1800:
        _add(f, 'info', 'node', 'Both ends booted at about the same time',
             f"{a['name']} booted ~{a['booted_at']}, {b['name']} ~{b['booted_at']}: typically a "
             f"coordinated firmware upgrade or a shared power event.")


def _check_ping(ctx, f):
    for d in ctx['directions']:
        w = d.get('window') or {}
        if not w.get('ping_samples'):
            continue
        ev = [f"avg {w['ping_avg']} ms, loss {w['ping_loss']}%, jitter {w['jitter']} ms "
              f"over {w['ping_samples']} samples"]
        if (w.get('ping_loss') or 0) >= link_health.LOSS_MARGINAL:
            _add(f, 'warning', 'ping', f"Pings to {d['target']} lose {w['ping_loss']}%",
                 f"Pings from the collector (and incident probes from {d['source']}) to {d['target']} "
                 f"are being lost.", ev)
        elif (w.get('ping_avg') or 0) >= link_health.LATENCY_MARGINAL:
            lqm = d.get('lqm_rtt_ms')
            _add(f, 'warning', 'ping', f"Pings to {d['target']} are slow ({w['ping_avg']} ms)",
                 'High latency for a single hop points to a detoured or congested path.'
                 + (f" LQM measures {lqm:.0f} ms across the link itself." if lqm is not None else ''), ev)


# --- Link Watch findings (link_watch.py state: one-way alarm, noise jumps) ---
def _check_watch(ctx, f):
    try:
        import link_watch
        w = link_watch.pair_watch_findings(*ctx['pair'])
    except Exception:
        return
    one = w.get('one_way')
    if one:
        d = one.get('detail') or {}
        _add(f, 'critical' if one.get('escalated') else 'warning', 'watch',
             f"Link Watch: one-way link alarm since {one.get('since')}",
             'The continuous one-way detector has flagged this pair on consecutive scans.',
             d.get('reasons') or [])
    for node, s in (w.get('noise') or {}).items():
        d = s.get('detail') or {}
        if d.get('delta') is None:
            continue
        _add(f, 'warning', 'watch', f"Link Watch: noise floor jump at {node} since {s.get('since')}",
             f"{node}'s noise floor is {d.get('current')} dBm, {d['delta']:+.1f} dB above its "
             f"baseline {d.get('baseline')} dBm. That lowers SNR on every link {node} receives.",
             recommendation=f"Look for a new transmitter or WiFi near {node} on or next to its channel.")
# --- end Link Watch findings ---


CHECKS = (_check_visibility, _check_delivery, _check_receivers, _check_rf, _check_near_far,
          _check_routing, _check_rates, _check_quality_breakdown, _check_flaps, _check_config,
          _check_interference, _check_noise_band, _check_nodes, _check_ping, _check_watch)


# ------------------------------------------------------------ verdict -----

def _verdict(ctx, findings):
    crit = [x for x in findings if x['severity'] == 'critical']
    causes = [x for x in findings if x['category'] in ('near_far', 'interference')
              and x['severity'] in ('critical', 'warning')]
    if ctx['receiver_problem']:
        end = sorted(ctx['receiver_problem'])[0]
        text = (f"{end}'s receiver is losing traffic from most of its RF neighbors while they hear it fine, "
                f"so the problem is at {end}'s receive side rather than this one link.")
        if any(d.get('wired_link_type') for d in ctx['directions']):
            text = (f"{ctx['pair'][0]} and {ctx['pair'][1]} are wired together, so their direct RF path "
                    f"is not used. " + text)
        if causes:
            text += ' Likely causes: ' + '; '.join(c['title'].lower() for c in causes[:3]) + ' (see below).'
        return {'severity': 'critical', 'text': text}
    broken = [d for d in ctx['directions'] if d.get('delivery') == 'broken']
    strong = any(x['category'] == 'rf' and x['severity'] == 'ok' for x in findings)
    if broken:
        d = broken[0]
        text = (f"Frames from {d['source']} are not reaching {d['target']}"
                + (f" while {d['target']} → {d['source']} works" if len(broken) == 1 and any(
                    x.get('delivery') == 'ok' for x in ctx['directions']) else '') + '.')
        if strong:
            text += (f" The signal is strong both ways, so this is not alignment or path loss: "
                     f"{d['target']}'s receiver is losing {d['source']}'s frames.")
        if causes:
            text += f" Most likely cause: {causes[0]['title'].lower()} (see below)."
        elif strong:
            text += (" Look for interference at that site or a configuration mismatch "
                     "(mode, width, distance/ACK setting, firmware).")
        if any(x['category'] == 'routing' and x['severity'] == 'critical' for x in findings):
            text += " As a result Babel won't use the link and traffic detours."
        return {'severity': 'critical', 'text': text}
    if crit:
        return {'severity': 'critical', 'text': crit[0]['title'] + '. ' + crit[0]['detail']}
    warn = [x for x in findings if x['severity'] == 'warning']
    if warn:
        return {'severity': 'warning', 'text': f"{len(warn)} issue(s) found; the most significant: "
                                               f"{warn[0]['title']}."}
    return {'severity': 'ok', 'text': 'No problems detected between these nodes in this window.'}


# --------------------------------------------------------------- series ---

def _series(ctx, start_dt, end_dt):
    span = (end_dt - start_dt).total_seconds()
    bucket = next((s for s in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600)
                   if span / s <= 200), 21600)
    out = {'bucket_seconds': bucket, 'directions': {}, 'nodes': {}}

    def bucket_of(ts):
        t = _parse_ts(ts)
        return int((t - start_dt).total_seconds() // bucket) if t else None

    for d in ctx['directions']:
        rows = d.pop('history', [])
        buckets = {}
        prev = None
        for r in rows:
            b = bucket_of(r['timestamp'])
            if b is None:
                continue
            acc = buckets.setdefault(b, {'n': {}, 's': {}, 'fail': 0, 'retry': 0, 'frames': 0})
            for k in ('quality', 'lq', 'tx_quality', 'ping_quality', 'snr', 'signal',
                      'tx_bitrate', 'rx_bitrate', 'ping_avg', 'ping_loss'):
                if r.get(k) is not None:
                    acc['s'][k] = acc['s'].get(k, 0) + r[k]
                    acc['n'][k] = acc['n'].get(k, 0) + 1
            if r.get('tx_packets') is not None and r.get('tx_fail') is not None:
                if prev and r['tx_packets'] >= prev['tx_packets'] and r['tx_fail'] >= prev['tx_fail']:
                    acc['frames'] += r['tx_packets'] - prev['tx_packets']
                    acc['fail'] += r['tx_fail'] - prev['tx_fail']
                    if r.get('tx_retries') is not None and prev.get('tx_retries') is not None \
                            and r['tx_retries'] >= prev['tx_retries']:
                        acc['retry'] += r['tx_retries'] - prev['tx_retries']
                prev = r
        series = []
        for b in sorted(buckets):
            acc = buckets[b]
            point = {'t': (start_dt + timedelta(seconds=b * bucket)).strftime(TS_FMT)}
            for k, total in acc['s'].items():
                point[k] = round(total / acc['n'][k], 2)
            if acc['frames']:
                point['tx_fail_pct'] = _pct(acc['fail'], acc['frames'])
                point['tx_retry_pct'] = _pct(acc['retry'], acc['frames'])
            series.append(point)
        out['directions'][f"{d['source']}>{d['target']}"] = series

    with database.get_connection() as conn:
        for name in ctx['pair']:
            out['nodes'][name] = [dict(r) for r in conn.execute(f'''
                SELECT MIN(timestamp) AS t,
                       AVG(CASE WHEN reachable = 1 THEN response_ms END) AS response_ms,
                       SUM(reachable = 0) AS unreachable, COUNT(*) AS polls
                FROM node_health_history
                WHERE node_name = ? AND timestamp >= ? AND probe_via IS NULL
                GROUP BY CAST((julianday(timestamp) - julianday(?)) * 86400 / {bucket} AS INTEGER)
                ORDER BY t
            ''', (name, start_dt.strftime(TS_FMT), start_dt.strftime(TS_FMT)))]
    return out


# ---------------------------------------------------------------- entry ---

def diagnose_pair(a, b, hours=24):
    a, b = a.lower(), b.lower()
    now = datetime.now()
    start_dt = now - timedelta(hours=hours)
    start = start_dt.strftime(TS_FMT)
    stale_after = _stale_after_seconds()

    with database.get_connection() as conn:
        cur = conn.cursor()
        nodes = {n: _node(cur, n, start, now) for n in (a, b)}
        directions = [
            _direction(cur, a, b, set(nodes[b]['known_macs']), start, now, stale_after),
            _direction(cur, b, a, set(nodes[a]['known_macs']), start, now, stale_after),
        ]
        names = {a, b} | {d['via_alias'] for d in directions if d.get('via_alias')}
        state_log = [dict(r) for r in cur.execute(f'''
            SELECT timestamp, source_node, target_node, state, quality, snr, rev_snr,
                   blocked_reason, detail, origin
            FROM link_state_log
            WHERE source_node IN ({','.join('?' * len(names))})
              AND target_node IN ({','.join('?' * len(names))})
              AND timestamp >= ?
            ORDER BY timestamp
        ''', list(names) * 2 + [start])]
        link_freq = _num(nodes[a].get('rf_frequency')) or _num(nodes[b].get('rf_frequency'))
        radios = [dict(r, frequency=_num(r['rf_frequency'])) for r in cur.execute('''
            SELECT name, model, rf_frequency, rf_channel AS channel, is_active, lat, lon FROM nodes
            WHERE lat IS NOT NULL AND lon IS NOT NULL
        ''')]
        nodes_geo = {r['name']: (r['lat'], r['lon']) for r in radios}
        neighbors = {n: _neighbors(cur, n, nodes_geo, stale_after, now) for n in (a, b)}
        # Also look at receivers where either end is very loud (near-far there).
        for n in (a, b):
            loud_at = cur.execute('''
                SELECT source_node FROM links
                WHERE target_node = ? AND UPPER(link_type) = 'RF' AND status != 'removed'
                  AND (CAST(snr AS REAL) >= ? OR CAST(signal AS REAL) >= ?)
            ''', (n, HOT_SNR, HOT_SIGNAL_DBM)).fetchall()
            for r in loud_at:
                if r[0] not in neighbors:
                    neighbors[r[0]] = _neighbors(cur, r[0], nodes_geo, stale_after, now)
        width = _infer_width(
            [x for d in directions for x in (d.get('rx_bitrate'), d.get('tx_bitrate'),
                                             (d.get('window') or {}).get('rx_bitrate_max'))]
            + [_num(_load_tracker(dict(r)).get('rx_bitrate')) for r in cur.execute('''
                SELECT raw_tracker FROM links WHERE source_node IN (?, ?) AND UPPER(link_type) = 'RF'
                  AND status != 'removed'
            ''', (a, b))])
        import channel_map as cm  # channel_map imports this module
        cm_radios, _ = cm._load(conn)
        for r in cm_radios.values():
            cm._apply_span(r)
            r['recent'] = cm._recent(r)
        cabled = {}
        for n in (a, b):
            cabled[n] = {r[0] for r in cur.execute('''
                SELECT target_node FROM links WHERE source_node = ? AND UPPER(link_type) = 'DTD'
                  AND status != 'removed' AND last_seen >= ?
            ''', (n, (now - timedelta(days=cm.RECENT_DAYS)).strftime(TS_FMT)))}
        nearby = {n: _nearby_radios(n, cm_radios, neighbors.get(n, []), link_freq, width, {a, b},
                                    cabled[n])
                  for n in (a, b)}
        ga, gb = nodes_geo.get(a), nodes_geo.get(b)
        geo_distance = round(_haversine_m(ga[0], ga[1], gb[0], gb[1])) if ga and gb else None

    ctx = {'pair': (a, b), 'nodes': nodes, 'directions': directions, 'state_log': state_log,
           'neighbors': neighbors, 'nearby': nearby, 'link_freq': link_freq, 'rf_budget': None,
           'inferred_width_mhz': width, 'geo_distance_m': geo_distance,
           'receiver_problem': set(), 'infra': set(database.get_infrastructure_node_names())}
    findings = []
    for check in CHECKS:
        check(ctx, findings)
    findings.sort(key=lambda x: SEVERITY_ORDER[x['severity']])
    series = _series(ctx, start_dt, now)
    for n in nodes.values():
        n.pop('known_macs', None)

    return {
        'pair': [a, b],
        'hours': hours,
        'generated_at': now.strftime(TS_FMT),
        'verdict': _verdict(ctx, findings),
        'findings': findings,
        'directions': directions,
        'nodes': nodes,
        'neighbors': {n: neighbors[n] for n in (a, b)},
        'infrastructure': sorted(ctx['infra'] & ({a, b} | set(neighbors))),
        'nearby': {n: nearby[n]['radios'] for n in (a, b)},
        'nearby_span': {n: nearby[n]['span'] for n in (a, b)},
        'geo_distance_m': geo_distance,
        'link_frequency_mhz': link_freq,
        'inferred_width_mhz': ctx['inferred_width_mhz'],
        'rf_budget': ctx['rf_budget'],
        'state_log': state_log,
        'series': series,
    }


def _probe_pair(src_name, src_ip, dst_name, dst_ip):
    ping = rf_stats.ping_via_aredn(dst_ip, source_node_ip=src_ip)
    trace = rf_stats.traceroute_via_aredn(dst_ip, source_node_ip=src_ip)
    hops = (trace or {}).get('hops') or []
    answered = [h for h in hops if not h.get('timeout')]
    direct = bool(answered) and len(hops) == 1 and answered[-1].get('ip') == dst_ip
    return {
        'source': src_name, 'target': dst_name, 'ping': ping, 'trace': trace,
        'hop_count': len(hops), 'direct': direct,
        'path': [h.get('host') or h.get('ip') or '*' for h in hops],
    }


def run_live_checks(a, b):
    """Ping and traceroute from each end to the other through the nodes' own
    CGI, to see whether traffic actually crosses the link directly."""
    a, b = a.lower(), b.lower()
    if not gateway.is_reachable():
        return {'error': gateway.unreachable_message(), 'gateway_unreachable': True}
    na, nb = database.get_node(a), database.get_node(b)
    if not (na and na.get('ip') and nb and nb.get('ip')):
        return {'error': 'Both nodes need a known IP address'}
    jobs = [(a, na['ip'], b, nb['ip']), (b, nb['ip'], a, na['ip'])]
    try:
        import eventlet
        pool = eventlet.GreenPool(2)
        results = list(pool.starmap(_probe_pair, jobs))
    except ImportError:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda j: _probe_pair(*j), jobs))

    findings = []
    for r in results:
        if not r['trace'] and not r['ping']:
            if gateway.failure_is_ours():
                return {'error': gateway.unreachable_message(), 'gateway_unreachable': True}
            _add(findings, 'warning', 'live', f"Could not run checks from {r['source']}",
                 f"{r['source']}'s ping/traceroute CGI did not answer.")
            continue
        loss = (r['ping'] or {}).get('loss')
        if r['direct']:
            _add(findings, 'ok', 'live', f"{r['source']} → {r['target']} goes direct over the link",
                 f"One hop; ping avg {(r['ping'] or {}).get('avg')} ms, loss {loss}%.")
        elif r['hop_count']:
            _add(findings, 'warning', 'live',
                 f"{r['source']} → {r['target']} takes {r['hop_count']} hops instead of 1",
                 'Traffic between the two nodes is not using the direct link.',
                 [' → '.join(r['path'])])
        if loss is not None and loss >= link_health.LOSS_MARGINAL:
            _add(findings, 'warning', 'live', f"{r['source']} → {r['target']} ping loss {loss}%",
                 'Measured from the node itself, so the collector path is not involved.')
    findings.sort(key=lambda x: SEVERITY_ORDER[x['severity']])
    return {'results': results, 'findings': findings}

"""
Sector airtime and hidden-node check (Diagnostics tab "Sector Airtime").
Read-only over SQLite.

A "sector" is any transmitting radio with two or more current RF neighbours
(AREDN meshes in adhoc mode, so a hub is simply a radio several others link
to). For each one this reports:

- its clients, with SNR / LQ / quality, bitrates and tx-fail / retry rates
  from the cumulative LQM counters in link_history (deltas over the window);
- a hearing matrix (does A hold an LQM tracker for B) and hidden pairs:
  clients that both talk to the hub but can't hear each other, confirmed by
  the radios' own LQM hidden-node lists when the scanner has stored them;
- a contention group: radios on the same or an overlapping channel that the
  hub can hear, share a mast with it, or face it within range (channel_map
  geometry), including radios that aren't linked to anything;
- a rough airtime estimate (packets/s x an assumed frame size / bitrate).

Masts (channel_map sites) get their own view: radios sharing a channel there
share airtime (colocated antennas hear each other even off the back), radios
on overlapping channels interfere without coordination, and clients linked to
a sector that isn't aimed at them are called out.

Optional what-if overrides (Channel Map syntax, e.g. the planned Collins
plan) are applied in memory, so contention can be previewed before changing
channels. Measured hearing, counters and rates always come from today's
channels. Never recommends TX power changes on infrastructure.
"""

import json
from datetime import datetime, timedelta

import channel_map as cm
import database
import infra_audit as ia
import link_diagnostics as ld

FRAME_BYTES = 600             # assumed average frame for the airtime estimate
FAIL_WARN_PCT = 10.0          # tx fail rate that makes a link/sector a warning
FAIL_INFO_PCT = 5.0
LOAD_LEVELS = ((50, 'busy'), (20, 'moderate'), (5, 'light'))   # estimated airtime %
BETTER_SECTOR_DEG = 30        # another sector this much closer to boresight is "better aimed"
SECTOR_MIN_BEAM_DEG = 45      # narrower antennas are PtP dishes, not sectors a client could move to
SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2, 'ok': 3}
TS_FMT = '%Y-%m-%d %H:%M:%S'


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hearing(conn):
    """{(a, b): tracker facts} for every RF tracker a holds for b. 'current'
    is False for a pair that is also cabled (DtD): one end may keep an old RF
    tracker for a node whose radio is off, but they aren't sharing a channel."""
    cabled = set()
    for a, b in conn.execute('''SELECT source_node, target_node FROM links
                                WHERE UPPER(link_type) = 'DTD' AND status != 'removed' '''):
        cabled.update({(a, b), (b, a)})
    import ghost_trackers
    cabled |= ghost_trackers.excluded_pairs(conn)   # radio-off ghosts too
    out = {}
    for r in conn.execute('''
            SELECT source_node, target_node, snr, quality, status, blocked, blocked_reason
            FROM links WHERE UPPER(link_type) = 'RF' AND status != 'removed' '''):
        key = (r['source_node'], r['target_node'])
        out[key] = {
            'snr': r['snr'], 'quality': r['quality'], 'status': r['status'],
            'blocked': bool(r['blocked']) if r['blocked'] is not None else None,
            'blocked_reason': r['blocked_reason'], 'cabled': key in cabled,
            'current': r['status'] == 'good' and key not in cabled}
    return out


def _polled(conn):
    """Radios whose own trackers we have: only for them is 'no tracker for X'
    evidence that they don't hear X."""
    return {r[0] for r in conn.execute("SELECT DISTINCT source_node FROM links WHERE status != 'removed'")}


def _counter_rows(conn, start):
    rows = {}
    for r in conn.execute('''
            SELECT source_node, target_node, timestamp, tx_packets, tx_fail, tx_retries,
                   tx_bitrate, rx_bitrate, lq
            FROM link_history
            WHERE sample_type = 'scan' AND UPPER(link_type) = 'RF' AND timestamp >= ?
            ORDER BY timestamp''', (start,)):
        rows.setdefault((r['source_node'], r['target_node']), []).append(dict(r))
    return rows


def _delta(values):
    """Sum of positive steps of a cumulative counter (survives reboots)."""
    total, prev = 0.0, None
    for v in values:
        if v is None:
            continue
        if prev is not None and v >= prev:
            total += v - prev
        prev = v
    return total


def _rates(rows):
    """Tx packets/s, fail and retry rates and bitrates of one direction."""
    if not rows:
        return None
    with_counters = [r for r in rows if r['tx_packets'] is not None]
    out = {'samples': len(rows), 'pps': None, 'fail_pct': None, 'retry_pct': None,
           'retries_reported': any((r['tx_retries'] or 0) > 0 for r in with_counters),
           'tx_bitrate': None, 'rx_bitrate': None, 'lq': None}
    if len(with_counters) >= 2:
        packets = _delta(r['tx_packets'] for r in with_counters)
        fails = _delta(r['tx_fail'] for r in with_counters)
        retries = _delta(r['tx_retries'] for r in with_counters)
        t0 = ld._parse_ts(with_counters[0]['timestamp'])
        t1 = ld._parse_ts(with_counters[-1]['timestamp'])
        secs = (t1 - t0).total_seconds() if t0 and t1 else 0
        if secs > 0:
            out['pps'] = round(packets / secs, 2)
        if packets > 0:
            out['fail_pct'] = round(fails / packets * 100, 1)
            out['retry_pct'] = round(retries / packets * 100, 1) if out['retries_reported'] else None
        out['packets'] = int(packets)
    recent = rows[-12:]
    tx = [r['tx_bitrate'] for r in recent if r['tx_bitrate']]
    rx = [r['rx_bitrate'] for r in recent if r['rx_bitrate']]
    lq = [r['lq'] for r in recent if r['lq'] is not None]
    out['tx_bitrate'] = round(sum(tx) / len(tx), 1) if tx else None
    out['rx_bitrate'] = round(sum(rx) / len(rx), 1) if rx else None
    out['lq'] = round(sum(lq) / len(lq)) if lq else None
    return out


def _airtime_pct(rate):
    """Rough airtime share of one direction: packets/s x FRAME_BYTES at its
    tx bitrate. A load indicator, not a measurement."""
    if not rate or rate.get('pps') is None or not rate.get('tx_bitrate'):
        return None
    return round(rate['pps'] * FRAME_BYTES * 8 / (rate['tx_bitrate'] * 1e6) * 100, 2)


def _load_level(pct):
    if pct is None:
        return 'unknown'
    for limit, label in LOAD_LEVELS:
        if pct >= limit:
            return label
    return 'idle'


def _relation(a, b):
    return ia.pair_relation({'frequency': a['frequency'], 'chanbw': a['chanbw']},
                            {'frequency': b['frequency'], 'chanbw': b['chanbw']})


def _gap(a, b):
    return round(ia.spectral_gap({'frequency': a['frequency'], 'chanbw': a['chanbw']},
                                 {'frequency': b['frequency'], 'chanbw': b['chanbw']}), 1)


def _dist(a, b):
    if not a or not b or a.get('lat') is None or b.get('lat') is None:
        return None
    return ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])


def _aimed(r):
    return r is not None and r.get('azimuth') is not None and (r.get('beamwidth') or 360) < 360


def _hidden_lists(conn):
    """{radio: set(hostnames)} from the radios' own LQM hidden-node lists."""
    out = {}
    for name, raw in conn.execute('SELECT name, lqm_hidden_nodes FROM nodes WHERE lqm_hidden_nodes IS NOT NULL'):
        try:
            out[name] = {(h.get('hostname') or '').lower() for h in json.loads(raw) if h.get('hostname')}
        except (TypeError, ValueError, AttributeError):
            continue
    return out


class _Findings:
    def __init__(self):
        self.items = []

    def add(self, severity, category, title, detail, evidence=None, recommendation=None, nodes=None):
        self.items.append({'severity': severity, 'category': category, 'title': title, 'detail': detail,
                           'evidence': evidence or [], 'recommendation': recommendation,
                           'nodes': nodes or []})


def run_report(scope='all', hours=24, overrides=None):
    """Sector airtime / hidden-node report. overrides: Channel Map what-if."""
    hours = max(1, min(int(hours or 24), 24 * 31))
    start = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    base = cm.build_map(scope=scope or 'all', overrides=overrides or None)
    radios = {r['name']: r for r in base['radios']}
    with database.get_connection() as conn:
        hear = _hearing(conn)
        polled = _polled(conn)
        counters = _counter_rows(conn, start)
        hidden_lists = _hidden_lists(conn)
        busy_known = conn.execute('SELECT COUNT(*) FROM nodes WHERE channel_busy IS NOT NULL').fetchone()[0]
        hidden_known = conn.execute('SELECT COUNT(*) FROM nodes WHERE lqm_hidden_nodes IS NOT NULL').fetchone()[0]

    rates = {k: _rates(v) for k, v in counters.items()}
    # Current RF neighbours (either end holds a fresh tracker for the other).
    neighbours = {}
    for (a, b), t in hear.items():
        if t['current']:
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)

    def live(name):
        r = radios.get(name)
        return r is not None and cm._transmitting(r) and r.get('recent', True)

    def hears(a, b):
        """True / False / None (unknown: we don't have a's trackers)."""
        if (a, b) in hear:
            return True
        return False if a in polled else None

    f = _Findings()
    sectors = []
    for hub in sorted(n for n in neighbours if live(n) and len(neighbours[n]) >= 2):
        sectors.append(_sector(hub, radios, neighbours, hear, hears, rates, hidden_lists, polled, f))

    masts = _masts(base, radios, neighbours, hear, hears, rates, live, f)
    broken = _what_if_breaks(radios, hear) if base.get('what_if') else []
    if broken:
        f.add('warning', 'what_if', f'The plan would strand {len(broken)} RF link(s)',
              'Both ends of an RF link must share a channel; with this plan these ends end up apart.',
              [f"{b['a']} ({b['a_channel']}) ↔ {b['b']} ({b['b_channel']})" for b in broken],
              'Add the far end to the plan on the same channel, or confirm the link is no longer needed '
              '(and turn that radio off).',
              sorted({n for b in broken for n in (b['a'], b['b'])}))

    notes = []
    if not busy_known:
        notes.append('Channel busy / airtime is not reported: AREDN sysinfo.json has no channel-busy or '
                     'survey field, so the stored channel_busy column is empty. Airtime here is estimated from '
                     f'packet counts at an assumed {FRAME_BYTES}-byte average frame and the link bitrate.')
    if not any(r and r.get('retries_reported') for r in rates.values()):
        notes.append('tx_retries is reported as 0 by these radios\' drivers, so retry rates are unknown; '
                     'tx_fail (frames dropped after all retries) is used instead.')
    if not hidden_known:
        notes.append('The radios\' own LQM hidden-node lists are stored from the next scan on; until then '
                     'hidden pairs are derived from which radios hold trackers for each other.')
    notes.append('LQM trackers only show stations a radio decodes; weaker transmitters that still raise '
                 'its noise floor are invisible here (see Link Watch noise).')

    findings = sorted(f.items, key=lambda x: (SEVERITY_ORDER.get(x['severity'], 9), x['title']))
    counts = {s: sum(1 for x in findings if x['severity'] == s) for s in ('critical', 'warning', 'info')}
    out = {
        'generated_at': datetime.now().strftime(TS_FMT), 'hours': hours, 'scope': scope,
        'frame_bytes': FRAME_BYTES, 'sectors': sectors, 'masts': masts,
        'findings': findings, 'counts': counts, 'data_notes': notes, 'what_if_breaks': broken,
    }
    if base.get('what_if'):
        out['what_if'] = {'applied': base['what_if']['applied'], 'unknown': base['what_if']['unknown']}
    return out


def _what_if_breaks(radios, hear):
    """Current RF links whose ends the what-if puts on different channels."""
    def chan(r):
        return f"{r['frequency']:g}/{r['chanbw'] or 20:g}" if cm._transmitting(r) else 'off'

    out, seen = [], set()
    for (a, b), t in hear.items():
        key = tuple(sorted((a, b)))
        if not t['current'] or key in seen or a not in radios or b not in radios:
            continue
        seen.add(key)
        ra, rb = radios[key[0]], radios[key[1]]
        if not (ra.get('what_if') or rb.get('what_if')):
            continue
        if chan(ra) == 'off' or chan(rb) == 'off' or _relation(ra, rb) != 'same':
            out.append({'a': key[0], 'b': key[1], 'a_channel': chan(ra), 'b_channel': chan(rb)})
    return out


def _client_row(hub, c, radios, hear, rates):
    hr, cr = radios.get(hub), radios.get(c)
    d = _dist(hr, cr)
    row = {'name': c, 'located': cr is not None and cr.get('lat') is not None,
           'distance_m': round(d) if d is not None else None,
           'frequency': cr.get('frequency') if cr else None, 'chanbw': cr.get('chanbw') if cr else None,
           'hub_hears': hear.get((hub, c)), 'hears_hub': hear.get((c, hub)),
           'down': rates.get((hub, c)), 'up': rates.get((c, hub))}
    if d is not None and hr and cr:
        brg = ld._bearing(hr['lat'], hr['lon'], cr['lat'], cr['lon'])
        row['bearing'] = round(brg)
        if _aimed(hr):
            row['off_boresight'] = round(ld._angle_diff(brg, hr['azimuth']))
        row['in_hub_beam'] = cm._in_beam(hr, cr, d)
    row['airtime_pct'] = round(sum(x for x in (_airtime_pct(row['down']), _airtime_pct(row['up'])) if x), 2) \
        if (_airtime_pct(row['down']) or _airtime_pct(row['up'])) else None
    fails = [x['fail_pct'] for x in (row['down'], row['up']) if x and x.get('fail_pct') is not None]
    row['fail_pct'] = max(fails) if fails else None
    return row


def _sector(hub, radios, neighbours, hear, hears, rates, hidden_lists, polled, f):
    hr = radios[hub]
    clients = sorted(neighbours[hub])
    rows = [_client_row(hub, c, radios, hear, rates) for c in clients]

    names = [hub] + clients
    matrix = {}
    for a in names:
        for b in names:
            if a != b:
                t = hear.get((a, b))
                matrix[f'{a}|{b}'] = ({'snr': t['snr'], 'quality': t['quality'], 'status': t['status'],
                                       'current': t['current']}
                                      if t else (False if a in polled else None))

    hidden = []
    for i, a in enumerate(clients):
        for b in clients[i + 1:]:
            ab, ba = hears(a, b), hears(b, a)
            if ab or ba:
                continue
            aredn = b in hidden_lists.get(a, set()) or a in hidden_lists.get(b, set())
            hidden.append({'a': a, 'b': b, 'aredn': aredn,
                           'known': ab is False or ba is False or aredn})

    # Hub totals, weighted by packets.
    pk = fl = 0.0
    for c in clients:
        for key in ((hub, c), (c, hub)):
            r = rates.get(key)
            if r and r.get('packets') and r.get('fail_pct') is not None:
                pk += r['packets']
                fl += r['packets'] * r['fail_pct'] / 100
    fail_pct = round(fl / pk * 100, 1) if pk else None
    airtime = round(sum(r['airtime_pct'] or 0 for r in rows), 2) if any(r['airtime_pct'] for r in rows) else None
    bitrates = [x for r in rows for x in ((r['down'] or {}).get('tx_bitrate'), (r['up'] or {}).get('tx_bitrate')) if x]

    contention = _contention(hub, hr, set(clients), radios, hear)
    sector = {
        'name': hub, 'site_code': hr.get('site_code'), 'is_infrastructure': hr.get('is_infrastructure'),
        'frequency': hr['frequency'], 'chanbw': hr['chanbw'], 'channel': hr.get('channel'),
        'what_if': bool(hr.get('what_if')), 'azimuth': hr.get('azimuth'), 'beamwidth': hr.get('beamwidth'),
        'antenna': hr.get('antenna'), 'clients': rows, 'matrix_names': names, 'matrix': matrix,
        'hidden_pairs': hidden, 'aredn_hidden': sorted(hidden_lists.get(hub, set())),
        'totals': {'fail_pct': fail_pct, 'airtime_pct': airtime, 'load': _load_level(airtime),
                   'min_bitrate': min(bitrates) if bitrates else None},
        'contention': contention,
    }

    # ---- findings ----
    known = [p for p in hidden if p['known']]
    if known:
        worst_client = max((r['fail_pct'] or 0 for r in rows), default=0)
        suffering = (fail_pct or 0) >= FAIL_WARN_PCT or worst_client >= FAIL_WARN_PCT
        f.add('warning' if suffering else 'info', 'hidden_node',
              f'{len(known)} hidden pair(s) around {hub}',
              f'{hub} talks to both stations of each pair, but they can\'t hear each other, so they can '
              f'transmit at the same time and collide at {hub}.'
              + (f' {hub} is showing {max(fail_pct or 0, worst_client):g}% tx failures.' if suffering else
                 ' Failures are low right now, so this costs little at the current load.'),
              [f"{p['a']} / {p['b']}" + (' (also in their own LQM hidden-node list)' if p['aredn'] else '')
               for p in known],
              'Check the node\'s LQM status lists the pair as hidden: current AREDN firmware uses that list to '
              'turn on RTS/CTS by itself (this app can\'t see the RTS state). If failures stay high, give the '
              'stations a path to each other, put one on its own narrow-beam link or channel, or reduce how '
              'many stations share this radio.',
              [hub] + sorted({n for p in known for n in (p['a'], p['b'])}))
    if fail_pct is not None and fail_pct >= FAIL_INFO_PCT:
        bad = [f"{r['name']}: {r['fail_pct']:g}% fail" for r in rows if (r['fail_pct'] or 0) >= FAIL_INFO_PCT]
        f.add('warning' if fail_pct >= FAIL_WARN_PCT else 'info', 'retries',
              f'{hub} drops {fail_pct:g}% of frames after retries',
              f'Over the window {fail_pct:g}% of frames to and from {hub}\'s clients failed after all retries '
              f'(estimated airtime {airtime if airtime is not None else "?"}%).',
              bad or [f'sector total {fail_pct:g}%'],
              'Look at the hidden pairs and contention group below and at Link Watch noise for these radios. '
              'Slow clients (low bitrate) take the most airtime.' +
              ('' if not hr.get('is_infrastructure') else ' Fix it at the client end; never lower this '
               'infrastructure radio\'s power.'),
              [hub])
    hot = [c for c in contention if c['relation'] == 'overlap' and (c['colocated'] or c['hub_hears'] or c['mutual_beam'])]
    if hot:
        f.add('warning', 'contention', f'{hub} shares spectrum with {len(hot)} radio(s) on an overlapping channel',
              f'Overlapping (not identical) channels don\'t coordinate: {hub} can\'t decode them, so it doesn\'t wait '
              'for them and their energy lands on its receiver as noise.',
              [_contention_text(c) for c in hot],
              'Move one side to a clear channel or to exactly the same channel (Channel Map → Plan channels).',
              [hub] + [c['name'] for c in hot])
    return sector


def _contention(hub, hr, clients, radios, hear):
    out = []
    for name, r in radios.items():
        if name == hub or name in clients or not cm._transmitting(r) or not r.get('recent', True):
            continue
        if (r.get('band') or '')[:1] != (hr.get('band') or '')[:1]:
            continue
        rel = _relation(hr, r)
        if rel not in ('same', 'overlap'):
            continue
        d = _dist(hr, r)
        colocated = d is not None and d <= cm.SITE_METERS
        hub_sees = d is not None and cm._in_beam(hr, r, d)
        sees_hub = d is not None and cm._in_beam(r, hr, d)
        heard = hear.get((hub, name))
        in_range = d is not None and d <= cm.CONFLICT_KM * 1000
        if not (colocated or heard or (in_range and (hub_sees or sees_hub))):
            continue
        out.append({'name': name, 'relation': rel, 'gap_mhz': _gap(hr, r),
                    'frequency': r['frequency'], 'chanbw': r['chanbw'],
                    'distance_m': round(d) if d is not None else None, 'colocated': colocated,
                    'hub_hears': {'snr': heard['snr'], 'status': heard['status']} if heard else None,
                    'hub_sees': hub_sees, 'sees_hub': sees_hub, 'mutual_beam': hub_sees and sees_hub,
                    'is_infrastructure': r.get('is_infrastructure'),
                    'kind': ('shared airtime (same channel: they take turns)' if rel == 'same'
                             else 'interference (overlapping channel: no coordination)')})
    out.sort(key=lambda c: (not c['colocated'], c['relation'] != 'overlap', c['distance_m'] or 0))
    return out


def _contention_text(c):
    where = 'same mast' if c['colocated'] else (f"{c['distance_m'] / 1000:.1f} km" if c['distance_m'] is not None else '?')
    heard = f", heard at SNR {c['hub_hears']['snr']}" if c['hub_hears'] and c['hub_hears'].get('snr') is not None else ''
    return f"{c['name']} {c['frequency']:g}/{c['chanbw'] or 20:g} ({c['relation']}, {where}{heard})"


def _masts(base, radios, neighbours, hear, hears, rates, live, f):
    """Per mast: shared-channel groups, overlapping pairs, wrong-sector clients."""
    masts = []
    for site in base['sites']:
        members = [m for m in site['members'] if live(m)]
        if len(members) < 2:
            continue
        mem = set(site['members'])
        info = []
        for m in members:
            r = radios[m]
            clients = sorted(n for n in neighbours.get(m, ()) if n not in mem)
            shares = [x for c in clients for x in (_airtime_pct(rates.get((m, c))), _airtime_pct(rates.get((c, m)))) if x]
            air = round(sum(shares), 2) if shares else None
            info.append({'name': m, 'frequency': r['frequency'], 'chanbw': r['chanbw'], 'channel': r.get('channel'),
                         'azimuth': r.get('azimuth'), 'beamwidth': r.get('beamwidth'), 'what_if': bool(r.get('what_if')),
                         'clients': clients, 'airtime_pct': air, 'load': _load_level(air)})
        pairs = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                ra, rb = radios[a], radios[b]
                if (ra.get('band') or '')[:1] != (rb.get('band') or '')[:1]:
                    continue
                rel = _relation(ra, rb)
                if rel not in ('same', 'overlap', 'adjacent'):
                    continue
                apart = round(ld._angle_diff(ra['azimuth'], rb['azimuth'])) if _aimed(ra) and _aimed(rb) else None
                overlap = None if apart is None else apart < (ra['beamwidth'] + rb['beamwidth']) / 2
                t = hear.get((a, b)) or hear.get((b, a))
                measured = True if t else (False if hears(a, b) is False or hears(b, a) is False else None)
                pairs.append({'a': a, 'b': b, 'relation': rel, 'gap_mhz': _gap(ra, rb), 'angle_apart': apart,
                              'beams_overlap': overlap, 'hear_measured': measured,
                              'hear_snr': t['snr'] if t else None,
                              'what_if': bool(ra.get('what_if') or rb.get('what_if'))})
        # Radios on one property that just link to each other aren't sectors
        # sharing airtime; only count those that serve stations elsewhere.
        serving = {m for m in members if any(n not in mem for n in neighbours.get(m, ()))}
        groups = [g for g in _shared_groups(members, radios) if len(set(g['members']) & serving) >= 2]
        cross = _cross_sector(members, mem, radios, neighbours, live)
        wrong = _wrong_sector(members, mem, radios, neighbours)
        if not (groups or cross or wrong or any(p['relation'] != 'same' for p in pairs)):
            continue
        masts.append({'name': site['name'], 'codes': site['codes'], 'members': info, 'pairs': pairs,
                      'shared_groups': groups, 'cross_sector': cross, 'wrong_sector': wrong})
        _mast_findings(site['name'], pairs, groups, cross, wrong, {m['name']: m for m in info}, f)
    return masts


def _shared_groups(members, radios):
    """Members on exactly the same channel, grouped."""
    by_chan = {}
    for m in members:
        r = radios[m]
        by_chan.setdefault((r['frequency'], r['chanbw'] or 20.0), []).append(m)
    return [{'frequency': k[0], 'chanbw': k[1], 'channel': ia.freq_to_channel(k[0]), 'members': sorted(v)}
            for k, v in sorted(by_chan.items()) if len(v) > 1]


def _cross_sector(members, mem, radios, neighbours, live):
    """Clients of one mast radio that sit on the same channel as another mast
    radio: inside its beam they hear (and may link to) both; outside it only
    a back/side lobe reaches them."""
    out = []
    for m in members:
        for c in sorted(neighbours.get(m, ())):
            cr = radios.get(c)
            if c in mem or not cr or not live(c):
                continue
            for other in members:
                if other == m:
                    continue
                orad = radios[other]
                if _relation(cr, orad) != 'same':
                    continue
                d = _dist(orad, cr)
                if d is None:
                    continue
                in_beam = cm._in_beam(orad, cr, d)
                off = round(ld._angle_diff(ld._bearing(orad['lat'], orad['lon'], cr['lat'], cr['lon']),
                                           orad['azimuth'])) if _aimed(orad) else None
                out.append({'client': c, 'sector': m, 'other': other, 'in_beam': in_beam, 'off_boresight': off,
                            'distance_m': round(d), 'what_if': bool(cr.get('what_if') or orad.get('what_if'))})
    return out


def _wrong_sector(members, mem, radios, neighbours):
    """Clients linked to a mast sector whose beam doesn't cover them, or with a
    clearly better-aimed sector on the same mast."""
    sectors = [m for m in members if _aimed(radios[m]) and radios[m]['beamwidth'] >= SECTOR_MIN_BEAM_DEG]
    out = []
    for m in sectors:
        r = radios[m]
        for c in sorted(neighbours.get(m, ())):
            cr = radios.get(c)
            if c in mem or not cr or cr.get('lat') is None:
                continue
            brg = ld._bearing(r['lat'], r['lon'], cr['lat'], cr['lon'])
            off = ld._angle_diff(brg, r['azimuth'])
            best = min(sectors, key=lambda s: ld._angle_diff(brg, radios[s]['azimuth']))
            best_off = ld._angle_diff(brg, radios[best]['azimuth'])
            outside = off > r['beamwidth'] / 2 + cm.BEAM_MARGIN_DEG
            better = best != m and best_off + BETTER_SECTOR_DEG < off
            if outside or better:
                out.append({'client': c, 'sector': m, 'bearing': round(brg), 'off_boresight': round(off),
                            'outside_beam': outside, 'best_sector': best, 'best_off': round(best_off)})
    return out


def _mast_findings(site, pairs, groups, cross, wrong, members, f):
    for g in groups:
        ps = [p for p in pairs if p['a'] in g['members'] and p['b'] in g['members']]
        overlap = [p for p in ps if p['beams_overlap']]
        heard = [p for p in ps if p['hear_measured']]
        names = ', '.join(g['members'])
        loads = {m: members[m]['load'] for m in g['members']}
        busy = [m for m, load in loads.items() if load in ('moderate', 'busy')]
        f.add('warning' if overlap or busy else 'info', 'shared_channel',
              f"{names} share {g['frequency']:g}/{g['chanbw']:g} (ch {g['channel']}) on {site}",
              'Same channel on one mast is shared airtime, not channel reuse: antennas a few metres apart hear '
              'each other well above the carrier-sense level even off the back, so they take turns (CSMA) '
              'instead of colliding. That is far better than adjacent channels on a mast, but each radio gets '
              'only part of the airtime when they are busy. AREDN\'s channel-planning guidance (3.25.10.0, '
              '"Collocated Nodes") warns that colocated nodes sharing a channel can cut site throughput by half '
              'or more, and recommends a different channel per sector, joined by DtD links.'
              + (' Their beams also overlap, so clients between them hear both.' if overlap else
                 ' Their antennas face apart, so each client mostly hears its own sector.'),
              [f"{p['a']} / {p['b']}: " + (f"{p['angle_apart']}° apart" if p['angle_apart'] is not None else 'azimuth unknown')
               + ('; beams overlap' if p['beams_overlap'] else '')
               + (f"; they hear each other now (SNR {p['hear_snr']})" if p['hear_measured'] else
                  '; not measurable on current channels' if p['what_if'] else '')
               for p in ps] + ([f'measured: {len(heard)} pair(s) already hear each other'] if heard else [])
              + [f"{m}: estimated airtime {members[m]['airtime_pct'] if members[m]['airtime_pct'] is not None else '?'}% "
                 f"({loads[m]})" for m in g['members']],
              'AREDN recommends a separate channel per colocated sector with DtD links between them; where the '
              'band has no room for that, sharing is acceptable while the sectors are lightly loaded. Give a '
              'sector its own channel as soon as it gets busy. Expect the radios to form an RF link with each '
              'other (harmless: routing prefers their cable).'
              if not overlap else 'Neighbouring sectors whose beams overlap should not share a channel: put '
              'opposite sectors together instead (Channel Map → Plan channels).',
              g['members'])
    for p in pairs:
        if p['relation'] in ('overlap', 'adjacent'):
            f.add('critical' if p['relation'] == 'overlap' else 'warning', 'mast_interference',
                  f"{p['a']} and {p['b']} are on {'overlapping' if p['relation'] == 'overlap' else 'adjacent'} "
                  f"channels on {site}",
                  'Radios on one mast with overlapping or adjacent (under 10 MHz apart) channels can\'t decode '
                  'each other, so they don\'t take turns: each one\'s transmissions land on the other\'s '
                  'receiver as noise and can overload it.',
                  [f"{abs(p['gap_mhz']):g} MHz {'overlap' if p['gap_mhz'] < 0 else 'gap'}"
                   + (f", antennas {p['angle_apart']}° apart" if p['angle_apart'] is not None else '')],
                  'Use the same channel (if they face apart) or at least 20 MHz between them '
                  '(Channel Map → Plan channels).',
                  [p['a'], p['b']])
    in_beam = [c for c in cross if c['in_beam']]
    lobe = [c for c in cross if not c['in_beam']]
    if in_beam:
        f.add('warning', 'cross_sector', f'{len(in_beam)} client(s) on {site} sit inside another same-channel sector',
              'These clients are inside the beam of a second sector on their channel, so they hear both, may '
              'link to the second one, and their traffic competes for both sectors\' airtime.',
              [f"{c['client']} (linked to {c['sector']}) is {c['off_boresight']}° off {c['other']}'s boresight"
               if c['off_boresight'] is not None else f"{c['client']} is in {c['other']}'s coverage" for c in in_beam],
              'Give the second sector a different channel, or re-aim the client toward its own sector.',
              sorted({n for c in in_beam for n in (c['client'], c['other'])}))
    if lobe:
        f.add('info', 'cross_sector', f'{len(lobe)} client(s) on {site} only see another same-channel sector off its back',
              'Only a back or side lobe of the other sector reaches these clients (typically 20–30 dB down), so '
              'they hear their own sector far louder. LQM should block any weak link that forms to the other one.',
              [f"{c['client']} (linked to {c['sector']}) is {c['off_boresight']}° off {c['other']}'s boresight, "
               f"{c['distance_m'] / 1000:.1f} km" if c['off_boresight'] is not None else c['client'] for c in lobe],
              'After the change, check RF Stats for links from these clients to the other sector.',
              sorted({n for c in lobe for n in (c['client'], c['other'])}))
    for w in wrong:
        r = radios.get(w['sector'], {})
        f.add('warning' if w['outside_beam'] else 'info', 'wrong_sector',
              f"{w['client']} is linked to {w['sector']}" + (' from outside its beam' if w['outside_beam'] else
                                                             f", but {w['best_sector']} is better aimed"),
              f"{w['client']} is at {w['bearing']}° from the mast, {w['off_boresight']}° off {w['sector']}'s boresight "
              f"(beam {r.get('beamwidth', 0):g}°); {w['best_sector']} is {w['best_off']}° off.",
              [f"link {w['client']} ↔ {w['sector']}"],
              f"Move {w['client']} to {w['best_sector']}'s channel (and aim it there), or check the stored "
              'azimuths. Don\'t raise power to make up for it.',
              [w['client'], w['sector'], w['best_sector']])

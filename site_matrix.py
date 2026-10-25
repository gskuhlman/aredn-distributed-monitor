"""
Site neighbor matrix (TODO #16): every radio at a site (rows) against every
radio it hears (columns: the site's own radios first, then remote radios),
with both directions of each pair in one cell. Read-only over SQLite.

Sources:
- `links`: each radio's current LQM trackers (including blocked, dropped and
  MAC-only entries) and the tracker's view of the far end (rev_snr,
  rev_quality), so a neighbor we never poll still shows both directions;
- `link_history` (scan samples in the window): medians, so a neighbor heard
  only part of the time still appears;
- Link Watch: one-way verdicts (`link_watch.evaluate_pair`) and the active
  one-way alarm state, so the matrix and the alarms agree.

Flags: one-way, lopsided SNR (> LOPSIDED_DB between directions), LQM
blocked, MAC-only identity, colocated radios hearing each other (expected for
back-to-back sectors sharing a channel), a client heard by a site radio that
isn't its serving radio, and site radios that hear nothing.

Optional what-if overrides (Channel Map "name freq width" / "name off"
lines) add each pair's planned channel relation next to the current one.
"""

import logging
from datetime import datetime, timedelta
from statistics import median

import channel_map as cm
import config
import database
import infra_audit as ia
import link_diagnostics as ld
import link_watch

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
LOPSIDED_DB = 10.0          # SNR difference between the two directions
WEAK_SNR = 10.0
ALSO_HEARD_CLOSE_DB = 10.0  # a non-serving site radio this close to the serving SNR competes for the client
SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 1) if vals else None


def _site_members(code, radios):
    """Radios of an infrastructure site code plus every radio within
    SITE_METERS of one of them (unmarked radios on the same mast)."""
    code = code.strip().upper()
    core = [r for r in radios.values() if r.get('site_code') == code]
    members = {r['name'] for r in core}
    located = [r for r in core if r['lat'] is not None]
    for r in radios.values():
        if r['lat'] is None or r['name'] in members:
            continue
        if any((ld._haversine_m(r['lat'], r['lon'], c['lat'], c['lon']) or 1e9) <= cm.SITE_METERS for c in located):
            members.add(r['name'])
    return members


def _name_mac_key(name):
    """MAC key of a MAC-only placeholder name (mac-d0ea116e9c2c)."""
    hexs = name[4:].replace('-', '').replace(':', '') if name.startswith('mac-') else ''
    if len(hexs) != 12:
        return None
    return ld._mac_key(':'.join(hexs[i:i + 2] for i in range(0, 12, 2)))


def _apply_overrides(radios, overrides):
    """Planned channel per radio from what-if text: {name: (freq, width) | 'off'}."""
    planned = {}
    for name, spec in cm.parse_overrides(overrides).items():
        r = radios.get(name)
        if not r:
            continue
        if spec.get('off'):
            planned[name] = 'off'
        else:
            planned[name] = (spec['frequency'] or r['frequency'], spec['chanbw'] or r['chanbw'])
    return planned


def _relation(a, b, planned=None):
    """Channel relation of two radios, current or under the planned channels."""
    def chan(r):
        if planned and r['name'] in planned:
            p = planned[r['name']]
            return None if p == 'off' else {'frequency': p[0], 'chanbw': p[1]}
        if r.get('rf_status') == 'off':
            return None
        return {'frequency': r.get('frequency'), 'chanbw': r.get('chanbw')}
    ca, cb = chan(a), chan(b)
    if not ca or not cb or not ca['frequency'] or not cb['frequency']:
        return 'off' if (ca is None or cb is None) else 'unknown'
    return ia.pair_relation(ca, cb)


def _history(cur, names, start):
    """{(source, target): {snr, quality, lq, signal, noise, samples, first, last}}
    medians of scan samples in the window."""
    out = {}
    if not names:
        return out
    marks = ','.join('?' * len(names))
    rows = cur.execute(f'''
        SELECT source_node, target_node, timestamp, snr, quality, lq, signal
        FROM link_history
        WHERE sample_type = 'scan' AND UPPER(link_type) = 'RF' AND timestamp >= ?
          AND (source_node IN ({marks}) OR target_node IN ({marks}))
    ''', [start, *names, *names]).fetchall()
    acc = {}
    for src, tgt, ts, snr, q, lq, sig in rows:
        a = acc.setdefault((src, tgt), {'snr': [], 'quality': [], 'lq': [], 'signal': [], 'noise': [],
                                        'first': ts, 'last': ts})
        a['snr'].append(snr)
        a['quality'].append(q)
        a['lq'].append(lq)
        a['signal'].append(sig)
        if sig is not None and snr is not None and snr > 0:
            a['noise'].append(sig - snr)
        a['first'] = min(a['first'], ts)
        a['last'] = max(a['last'], ts)
    for key, a in acc.items():
        out[key] = {'snr': _median(a['snr']), 'quality': _median(a['quality']), 'lq': _median(a['lq']),
                    'signal': _median(a['signal']), 'noise': _median(a['noise']),
                    'samples': len(a['snr']), 'first': a['first'], 'last': a['last']}
    return out


def _tracker_view(row, now, stale_after):
    """This radio's current view of a neighbor from its links row."""
    t = ld._load_tracker(row)
    seen = ld._parse_ts(row.get('last_seen'))
    age = (now - seen).total_seconds() if seen else None
    num = ld._num
    return {
        'status': row.get('status'), 'last_seen': row.get('last_seen'), 'age_seconds': age,
        'fresh': age is not None and age <= stale_after and row.get('status') != 'removed',
        'snr': num(t.get('snr', row.get('snr'))), 'signal': num(t.get('signal', row.get('signal'))),
        'quality': num(t.get('quality', row.get('quality'))), 'lq': num(t.get('lq')),
        'noise': (num(t.get('signal', row.get('signal'))) - num(t.get('snr', row.get('snr'))))
        if num(t.get('signal', row.get('signal'))) is not None and num(t.get('snr', row.get('snr'))) else None,
        # The tracker's view of the far end (what it reports hearing of us).
        'rev_snr': num(t.get('rev_snr', row.get('rev_snr'))), 'rev_quality': num(t.get('rev_quality')),
        'txcost': num(t.get('txcost')), 'rxcost': num(t.get('rxcost')),
        'routable': t.get('routable'), 'tx_bitrate': num(t.get('tx_bitrate')),
        'blocked': bool(row.get('blocked')) or bool(t.get('user_blocks')),
        'blocked_reason': row.get('blocked_reason') or ('user block' if t.get('user_blocks') else None),
        'mac': row.get('mac_address'), 'identity': row.get('identity_status'),
        'hostname_known': bool(t.get('hostname')),
    }


def build(site=None, radios=None, hours=24, overrides=None, now=None):
    """Matrix for a site code or a radio list. Returns rows, columns, cells
    {row: {col: cell}}, row/column summaries and findings."""
    now = now or datetime.now()
    hours = max(1, min(int(hours or 24), 24 * 31))
    stale_after = ld._stale_after_seconds()
    with database.get_connection() as conn:
        cur = conn.cursor()
        all_radios, _ = cm._load(conn)
        # Judge "current" as of the latest scan: when scanning has stopped
        # (app down, starting node lost) every tracker would otherwise look
        # stale and every radio deaf.
        last_scan = ld._parse_ts(cur.execute('SELECT MAX(last_seen) FROM nodes').fetchone()[0])
        if last_scan and (now - last_scan).total_seconds() > stale_after:
            now = last_scan + timedelta(seconds=1)
        start = (now - timedelta(hours=hours)).strftime(TS_FMT)

        if site:
            row_names = _site_members(site, all_radios)
            if not row_names:
                return {'error': f'No radios at site {site.upper()}'}
        else:
            row_names = {n.strip().lower() for n in (radios or []) if n and n.strip()}
            missing = sorted(n for n in row_names if n not in all_radios)
            row_names -= set(missing)
            if not row_names:
                return {'error': 'None of the radios were found'}
        row_names = sorted(row_names)
        planned = _apply_overrides(all_radios, overrides) if overrides else {}

        # Every RF tracker of a row radio within the window, and every tracker
        # in which some radio hears a row radio (the reverse direction).
        marks = ','.join('?' * len(row_names))
        link_rows = [dict(r) for r in cur.execute(f'''
            SELECT * FROM links
            WHERE UPPER(link_type) = 'RF' AND last_seen >= ?
              AND (source_node IN ({marks}) OR target_node IN ({marks}))
        ''', [start, *row_names, *row_names])]
        wired = {tuple(sorted((r['source_node'], r['target_node']))) for r in cur.execute(f'''
            SELECT source_node, target_node FROM links
            WHERE UPPER(link_type) != 'RF' AND status != 'removed'
              AND (source_node IN ({marks}) OR target_node IN ({marks}))
        ''', [*row_names, *row_names])}
        hist = _history(cur, row_names, start)
        fresh_nodes = link_watch._fresh_nodes(cur, now, stale_after)

        # MAC -> node name, so a MAC-only column can say who it probably is.
        mac_owner = {}
        for r in cur.execute('''
            SELECT target_node, mac_address FROM links
            WHERE mac_address IS NOT NULL AND target_node NOT LIKE 'mac-%'
        '''):
            key = ld._mac_key(r[1])
            if key:
                mac_owner.setdefault(key, r[0])

        import ghost_trackers
        ghosts = ghost_trackers.excluded_pairs(conn)
        link_rows = [r for r in link_rows if (r['source_node'], r['target_node']) not in ghosts]
        views = {}
        for r in link_rows:
            views[(r['source_node'], r['target_node'])] = _tracker_view(r, now, stale_after)

        # Columns: whatever a row radio hears (tracker or history), plus row
        # radios themselves.
        col_names = set(row_names)
        for (src, tgt) in list(views) + list(hist):
            if src in row_names:
                col_names.add(tgt)

        # Link Watch verdicts for named pairs touching the site.
        one_way = {}
        watch_state = link_watch._load_state(link_watch.KIND_ONE_WAY)
        for a in row_names:
            for b in col_names:
                if a == b or b.startswith('mac-') or (a > b and b in row_names):
                    continue
                if not ((a, b) in views or (b, a) in views):
                    continue
                key = tuple(sorted((a, b)))
                if key in one_way:
                    continue
                try:
                    one_way[key] = link_watch.evaluate_pair(cur, key[0], key[1], now, stale_after, fresh_nodes)
                except Exception as exc:   # one odd pair must not stop the matrix
                    logger.debug('site matrix one-way check failed for %s/%s: %s', a, b, exc)
                    one_way[key] = None

    def radio_info(name):
        r = all_radios.get(name) or {'name': name, 'lat': None, 'lon': None}
        p = planned.get(name)
        return {
            'name': name, 'mac_only': name.startswith('mac-'),
            'probably': mac_owner.get(_name_mac_key(name)),
            'frequency': r.get('frequency'), 'chanbw': r.get('chanbw'), 'channel': r.get('channel'),
            'rf_status': r.get('rf_status'), 'mode': r.get('mode'), 'ssid': r.get('ssid'),
            'azimuth': r.get('azimuth'), 'beamwidth': r.get('beamwidth'), 'antenna': r.get('antenna'),
            'is_infrastructure': r.get('is_infrastructure'), 'site_code': r.get('site_code'),
            'model': r.get('model'), 'active': r.get('active'), 'last_seen': r.get('last_seen'),
            'planned': None if p is None else ('off' if p == 'off' else {'frequency': p[0], 'chanbw': p[1],
                                                                          'channel': ia.freq_to_channel(p[0])}),
        }

    def geometry(a, b):
        ra, rb = all_radios.get(a), all_radios.get(b)
        if not ra or not rb or ra['lat'] is None or rb['lat'] is None:
            return {'distance_m': None, 'colocated': False, 'a_beam': None, 'b_beam': None}
        d = ld._haversine_m(ra['lat'], ra['lon'], rb['lat'], rb['lon'])
        return {'distance_m': round(d) if d is not None else None,
                'colocated': d is not None and d <= cm.SITE_METERS,
                'a_beam': cm._in_beam(ra, rb, d), 'b_beam': cm._in_beam(rb, ra, d)}

    cells, findings = {}, []
    heard_by = {c: [] for c in col_names}
    for a in row_names:
        cells[a] = {}
        for b in col_names:
            if a == b:
                continue
            fwd, back = views.get((a, b)), views.get((b, a))
            hf, hb = hist.get((a, b)), hist.get((b, a))
            if not (fwd or hf or back or hb):
                continue
            hears = bool((fwd and fwd['fresh']) or (hf and hf['samples']))
            if not hears and not (back and back['fresh']):
                continue
            geo = geometry(a, b)
            ra, rb = all_radios.get(a), all_radios.get(b)
            rel = _relation(ra, rb) if ra and rb else 'unknown'
            rel_plan = _relation(ra, rb, planned) if planned and ra and rb else None
            # This radio's view: current tracker, else the window median.
            snr = fwd['snr'] if fwd and fwd['fresh'] else (hf['snr'] if hf else None)
            # The far end's view: its own tracker when polled, else what our
            # tracker reports it hears (rev_snr).
            their_snr = back['snr'] if back and back['fresh'] else (
                fwd['rev_snr'] if fwd and fwd['fresh'] else (hb['snr'] if hb else None))
            ow = one_way.get(tuple(sorted((a, b))))
            flags = []
            if ow and ow.get('one_way'):
                flags.append('one_way')
            key = '|'.join(sorted((a, b)))
            alarm = watch_state.get(key) or {}
            if alarm.get('active'):
                flags.append('one_way_alarm')
            if snr is not None and their_snr is not None and abs(snr - their_snr) > LOPSIDED_DB:
                flags.append('lopsided')
            if (fwd and fwd['blocked']) or (back and back['blocked']):
                flags.append('blocked')
            if b.startswith('mac-') or (fwd and fwd['fresh'] and not fwd['hostname_known']):
                flags.append('mac_only')
            if geo['colocated'] and hears:
                flags.append('colocated')
            if tuple(sorted((a, b))) in wired:
                flags.append('wired')
            cell = {
                'hears': hears, 'snr': snr, 'their_snr': their_snr,
                'quality': fwd['quality'] if fwd and fwd['fresh'] else (hf['quality'] if hf else None),
                'their_quality': back['quality'] if back and back['fresh'] else (fwd['rev_quality'] if fwd else None),
                'lq': fwd['lq'] if fwd and fwd['fresh'] else (hf['lq'] if hf else None),
                'their_lq': back['lq'] if back and back['fresh'] else None,
                'signal': fwd['signal'] if fwd and fwd['fresh'] else (hf['signal'] if hf else None),
                'noise': fwd['noise'] if fwd and fwd['fresh'] else (hf['noise'] if hf else None),
                'status': fwd['status'] if fwd else None,
                'blocked_reason': (fwd or {}).get('blocked_reason') or (back or {}).get('blocked_reason'),
                'last_seen': fwd['last_seen'] if fwd else (hf['last'] if hf else None),
                'current': bool(fwd and fwd['fresh']),
                'window': hf, 'reverse_window': hb,
                'txcost': fwd['txcost'] if fwd else None, 'routable': fwd['routable'] if fwd else None,
                'relation': rel, 'planned_relation': rel_plan,
                'one_way_reasons': ow['reasons'] if ow and ow.get('one_way') else [],
                'one_way_since': alarm.get('since') if alarm.get('active') else None,
                'flags': flags, **geo,
            }
            cells[a][b] = cell
            if hears:
                heard_by[b].append(a)

    # Serving radio for each remote column heard by more than one site radio:
    # the best two-way link (both SNRs) wins; the others "also hear" it.
    for b, hearers in heard_by.items():
        if b in row_names or len(hearers) < 2:
            continue
        def strength(a):
            c = cells[a][b]
            return min(c['snr'] if c['snr'] is not None else -99, c['their_snr'] if c['their_snr'] is not None else -99)
        serving = max(hearers, key=strength)
        for a in hearers:
            c = cells[a][b]
            c['serving'] = a == serving
            if a != serving:
                c['flags'].append('also_heard')
                c['serving_radio'] = serving

    # Findings.
    def add(severity, category, title, detail, evidence=None, recommendation=None, pair=None):
        findings.append({'severity': severity, 'category': category, 'title': title, 'detail': detail,
                         'evidence': evidence or [], 'recommendation': recommendation, 'pair': pair})

    done = set()
    for a in row_names:
        for b, c in cells[a].items():
            key = tuple(sorted((a, b)))
            fl = c['flags']
            if 'one_way' in fl and key not in done:
                done.add(key)
                add('critical' if 'one_way_alarm' in fl else 'warning', 'one_way',
                    f'One-way: {a} ↔ {b}', '; '.join(c['one_way_reasons']) or 'one end does not hear the other',
                    [f"{a} hears {b}: SNR {c['snr']} dB, quality {c['quality']}",
                     f"{b} hears {a}: SNR {c['their_snr']} dB, quality {c['their_quality']}"]
                    + ([f"Link Watch alarm since {c['one_way_since']}"] if c.get('one_way_since') else []),
                    'Open the pair in RF Stats for the full diagnosis.', pair=[a, b])
            if 'lopsided' in fl and (a, b, 'lopsided') not in done and (b, a, 'lopsided') not in done:
                done.add((a, b, 'lopsided'))
                add('info' if 'wired' in fl else 'warning', 'lopsided',
                    f'Lopsided SNR: {a} ↔ {b}',
                    f"{a} hears {b} at {c['snr']} dB, {b} hears {a} at {c['their_snr']} dB "
                    f"({abs((c['snr'] or 0) - (c['their_snr'] or 0)):.0f} dB apart).",
                    recommendation='Check TX power, antenna aim and cabling on the weaker side; a one-sided '
                                   'noise source near the deaf end also does this.', pair=[a, b])
            if 'blocked' in fl and (a, b, 'blocked') not in done and (b, a, 'blocked') not in done:
                done.add((a, b, 'blocked'))
                add('info', 'blocked', f'LQM blocked: {a} ↔ {b}',
                    f"Blocked ({c['blocked_reason'] or 'reason not reported'}); traffic doesn't use this pair.",
                    pair=[a, b])
            if 'mac_only' in fl and c['current']:
                guess = mac_owner.get(_name_mac_key(b)) if b.startswith('mac-') else b
                add('warning', 'mac_only', f'{a} knows a neighbor only by MAC',
                    f"{a} hears {b} (SNR {c['snr']} dB) but has no hostname for it"
                    + (f'; it is probably {guess}' if guess else '') + '. Its announcements are not reaching '
                    f'{a}, which usually means a one-way or very lossy path.', pair=[a, b])
            elif 'mac_only' in fl and c['window']:
                w = c['window']
                guess = mac_owner.get(_name_mac_key(b)) if b.startswith('mac-') else b
                add('info', 'mac_only', f'{a} knew a neighbor only by MAC earlier in the window',
                    f"{a} listed {b}" + (f' (probably {guess})' if guess else '') +
                    f" without a hostname in {w['samples']} scans between {w['first']} and {w['last']} "
                    f"(median SNR {w['snr']} dB, LQ {w['lq']}%). Not in the latest scan.",
                    pair=[a, guess] if guess and not guess.startswith('mac-') else None)
            if 'colocated' in fl and b in row_names and a < b and c['hears']:
                same = c['relation'] == 'same'
                add('info' if same else 'warning', 'colocated',
                    f'Radios on the same mast hear each other: {a} ↔ {b}',
                    ('Same channel, so they share airtime. Expected when they are meant to share it (back-to-back '
                     'sectors, two access radios); routing should prefer the cable between them.') if same else
                    (f"Channel relation is {c['relation']}: they decode each other although their channels "
                     'differ, which points to too little guard band on the mast.'),
                    [f"SNR {c['snr']} / {c['their_snr']} dB", f"distance {c['distance_m']} m"]
                    + (['also wired together'] if 'wired' in fl else []), pair=[a, b])
            if 'also_heard' in fl and c['hears']:
                serving = c['serving_radio']
                sc = cells[serving][b]
                close = c['snr'] is not None and sc['snr'] is not None and sc['snr'] - c['snr'] <= ALSO_HEARD_CLOSE_DB
                add('warning' if close else 'info', 'also_heard',
                    f'{b} is also heard by {a} (serving: {serving})',
                    f"{a} hears {b} at {c['snr']} dB, its serving radio {serving} at {sc['snr']} dB"
                    + ('; close enough to compete for airtime or attract routes.' if close else '.'),
                    [f"{b} hears {a} at {c['their_snr']} dB" if c['their_snr'] is not None else f'{b} does not report hearing {a}',
                     f"beam: {a} {'covers' if c['a_beam'] else 'does not cover'} {b}"
                     if c['a_beam'] is not None else 'no antenna aim data'],
                    'Expected for clients near a sector boundary; if it links over the weaker sector, '
                    'check the client\'s aim or let LQM block the weaker link.' if close else None, pair=[a, b])
    for a in row_names:
        r = all_radios.get(a) or {}
        heard = [b for b, c in cells[a].items() if c['current']]
        if not heard and r.get('rf_status') != 'off' and r.get('frequency') and planned.get(a) != 'off':
            add('warning', 'deaf', f'{a} hears no RF neighbors',
                'No current RF tracker. Either nothing is on its channel and SSID in its beam, or it is '
                'not working.', [f"channel {r.get('frequency')}/{r.get('chanbw')} MHz",
                                 f"last polled {r.get('last_seen')}"],
                'A point-to-point antenna whose far end does not exist yet only adds noise: turn it off.'
                if r.get('azimuth') is not None and (r.get('beamwidth') or 360) <= 45 else
                'An omni or sector may just have no users right now; check it is on the channel, width and '
                'SSID its users expect.')
    if planned:
        for a in row_names:
            for b in row_names:
                if a >= b:
                    continue
                geo = geometry(a, b)
                ra, rb = all_radios.get(a), all_radios.get(b)
                if not (geo['colocated'] and ra and rb):
                    continue
                before, after = _relation(ra, rb), _relation(ra, rb, planned)
                if after == 'same' and before != 'same':
                    add('info', 'planned', f'After the plan {a} and {b} share a channel',
                        'Radios on one mast on the same channel hear each other and take turns (shared airtime).',
                        pair=[a, b])
                elif after in ('overlap', 'adjacent') and before not in ('overlap', 'adjacent'):
                    add('warning', 'planned', f'After the plan {a} and {b} are {after} on the mast',
                        'Less than 10 MHz between colocated radios: expect desense.', pair=[a, b])
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f['severity'], 9), f['category'], f['title']))

    # Summaries.
    row_out = []
    for a in row_names:
        heard = [c for c in cells[a].values() if c['hears']]
        snrs = [c['snr'] for c in heard if c['snr'] is not None]
        noises = [c['noise'] for c in heard if c['noise'] is not None]
        row_out.append({**radio_info(a), 'heard': len(heard),
                        'current': sum(1 for c in heard if c['current']),
                        'best_snr': max(snrs) if snrs else None, 'worst_snr': min(snrs) if snrs else None,
                        'noise': _median(noises)})
    remote = sorted((b for b in col_names if b not in row_names),
                    key=lambda b: (-len(heard_by[b]),
                                   -max([cells[a][b]['snr'] or -99 for a in heard_by[b]] or [-99]), b))
    col_out = []
    for b in list(row_names) + remote:
        if b not in row_names and not heard_by[b]:
            continue
        info = radio_info(b)
        info.update({'is_site': b in row_names, 'heard_by': len(heard_by[b])})
        col_out.append(info)

    counts = {'critical': 0, 'warning': 0, 'info': 0}
    for f in findings:
        counts[f['severity']] = counts.get(f['severity'], 0) + 1
    return {
        'generated_at': datetime.now().strftime(TS_FMT), 'as_of': now.strftime(TS_FMT),
        'site': site.upper() if site else None,
        'hours': hours, 'start': start, 'what_if': bool(planned),
        'rows': row_out, 'columns': col_out, 'cells': cells,
        'findings': findings, 'counts': counts, 'lopsided_db': LOPSIDED_DB,
        'one_way_thresholds': {'high_lq': config.ONE_WAY_HIGH_LQ, 'low_lq': config.ONE_WAY_LOW_LQ},
    }

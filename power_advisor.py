"""
Power-cut advisor: predicts what every RF neighbour would hear from a radio
after a TX power cut, so a near-far fix doesn't cost anyone their link.
Read-only over SQLite; nothing is sent to any radio.

A power cut lowers the radio's signal at every receiver by the same number of
dB, and noise at the receiver doesn't change, so each neighbour's SNR of the
radio drops by exactly the cut:

- the neighbour's view of this radio comes from its own tracker (links /
  link_history rows source = neighbour, target = radio), falling back to the
  radio's tracker `rev_snr` when the neighbour isn't polled;
- median SNR over `hours` is the working level, the 10th percentile the fade
  level (what the link sees on a bad scan);
- a neighbour is `ok` while median - cut >= MIN_SNR + margin and the fade
  level stays >= MIN_SNR, `marginal` while the median stays >= MIN_SNR, and
  `lost` below that (AREDN LQM's default minimum SNR blocks it);
- the bitrate after the cut is an estimate: the current measured rate scaled
  by an 802.11ac MCS table (rate per required SNR), not a measurement.

The suggested cut is the largest whole-dB cut that keeps every intended peer
(current, unblocked RF neighbours; cabled DtD peers' leftover RF trackers are
ignored) `ok`; the comfortable cut keeps the weakest intended peer near
link diagnostics' TARGET_SNR (30 dB). Receivers where the radio is "hot" (link
diagnostics' near-far thresholds) show how much the cut helps them: the loud
signal and its gap to that receiver's weakest neighbour both drop.

Infrastructure radios (towers) are refused: their power is never cut; the
fix belongs at the non-infrastructure end, as everywhere else in the app.
AREDN sysinfo doesn't report TX power, so cuts are relative dB; the stored
`rf_txpower` column is used for display when a firmware ever reports it.
"""

from datetime import datetime, timedelta
from statistics import median

import config
import database
import link_diagnostics as ld

MAX_CUT_DB = 20

# 802.11ac (VHT) per-stream rate at 20 MHz, long guard (Mbps), and the rough
# SNR each MCS needs. Only the ratio between two rows is used.
MCS_TABLE = [
    (5.0, 6.5), (8.0, 13.0), (11.0, 19.5), (14.0, 26.0), (17.0, 39.0),
    (20.0, 52.0), (22.0, 58.5), (25.0, 65.0), (28.0, 78.0), (31.0, 86.7),
]


def _mcs_rate(snr):
    """Table rate for an SNR (0 below MCS0)."""
    rate = 0.0
    for need, r in MCS_TABLE:
        if snr is not None and snr >= need:
            rate = r
    return rate


def _estimate_rate(current_rate, snr_now, snr_after):
    """Measured rate scaled by the MCS table ratio; None when unknown."""
    if current_rate is None or snr_now is None:
        return None
    now = _mcs_rate(snr_now)
    if not now:
        return None
    return round(current_rate * _mcs_rate(snr_after) / now, 1)


def _percentile(values, pct):
    vals = sorted(values)
    if not vals:
        return None
    k = (len(vals) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _status(n, cut, min_snr, margin):
    if n['snr'] is None:
        return 'unknown'
    med = n['snr'] - cut
    fade = (n['snr_fade'] if n['snr_fade'] is not None else n['snr']) - cut
    if med >= min_snr + margin and fade >= min_snr:
        return 'ok'
    if med >= min_snr:
        return 'marginal'
    return 'lost'


def _history(cur, name, start):
    """{neighbour: {'heard': [snr...], 'rev': [snr...], 'tx': [rate...], 'rx': [rate...]}}"""
    out = {}
    rows = cur.execute('''
        SELECT source_node, target_node, snr, rev_snr, tx_bitrate, rx_bitrate FROM link_history
        WHERE timestamp >= ? AND UPPER(link_type) = 'RF' AND (sample_type = 'scan' OR sample_type IS NULL)
          AND (source_node = ? OR target_node = ?)
    ''', (start, name, name)).fetchall()
    for r in rows:
        if r['target_node'] == name:      # the neighbour's own view of this radio
            h = out.setdefault(r['source_node'], {'heard': [], 'rev': [], 'tx': [], 'rx': []})
            if r['snr'] is not None and r['snr'] > 0:
                h['heard'].append(float(r['snr']))
            if r['rx_bitrate']:
                h['rx'].append(float(r['rx_bitrate']))
        elif r['source_node'] == name:    # this radio's tracker of the neighbour
            h = out.setdefault(r['target_node'], {'heard': [], 'rev': [], 'tx': [], 'rx': []})
            if r['rev_snr'] is not None and r['rev_snr'] > 0:
                h['rev'].append(float(r['rev_snr']))
            if r['tx_bitrate']:
                h['tx'].append(float(r['tx_bitrate']))
    return out


def _neighbours(cur, name, hours, now, stale_after):
    start = (now - timedelta(hours=hours)).strftime(ld.TS_FMT)
    hist = _history(cur, name, start)
    links = {}
    for r in cur.execute('''
        SELECT * FROM links WHERE UPPER(link_type) = 'RF' AND status != 'removed'
          AND (source_node = ? OR target_node = ?)
    ''', (name, name)).fetchall():
        peer = r['target_node'] if r['source_node'] == name else r['source_node']
        side = 'mine' if r['source_node'] == name else 'theirs'
        links.setdefault(peer, {})[side] = dict(r)

    # Peers also reached over a cable: their RF tracker is a leftover (e.g. a
    # house node with its radio off), not a link that depends on our power.
    cabled = set()
    for r in cur.execute('''
        SELECT source_node, target_node, last_seen FROM links
        WHERE UPPER(link_type) = 'DTD' AND status != 'removed' AND (source_node = ? OR target_node = ?)
    ''', (name, name)).fetchall():
        seen = ld._parse_ts(r['last_seen'])
        if seen and (now - seen).total_seconds() <= stale_after:
            cabled.add(r['target_node'] if r['source_node'] == name else r['source_node'])

    out = []
    for peer in sorted(set(links) | set(hist)):
        if peer in cabled:
            continue
        sides = links.get(peer, {})
        mine, theirs = sides.get('mine'), sides.get('theirs')
        recent = [s for s in (mine, theirs) if s and (seen := ld._parse_ts(s.get('last_seen')))
                  and (now - seen).total_seconds() <= stale_after]
        h = hist.get(peer, {'heard': [], 'rev': [], 'tx': [], 'rx': []})
        if not recent and not h['heard'] and not h['rev']:
            continue
        mt = ld._load_tracker(mine) if mine else {}
        tt = ld._load_tracker(theirs) if theirs else {}
        samples, source = h['heard'], 'neighbour'
        if len(samples) < 3:
            samples, source = h['heard'] + h['rev'], 'reported'
        if not samples:
            now_snr = ld._num(tt.get('snr', theirs.get('snr') if theirs else None)) \
                if theirs else ld._num(mt.get('rev_snr', mine.get('rev_snr') if mine else None))
            samples = [now_snr] if now_snr else []
            source = 'current'
        signal = ld._num(tt.get('signal', theirs.get('signal') if theirs else None)) if theirs else None
        rates = h['tx'] or h['rx']
        rate = median(rates) if rates else ld._num(mt.get('tx_bitrate') or tt.get('rx_bitrate'))
        blocked = any(bool(s.get('blocked')) for s in (mine, theirs) if s) \
            or bool(mt.get('user_blocks')) or bool(tt.get('user_blocks'))
        # Intended = a current, unblocked RF neighbour. Not routable on one
        # side (e.g. a one-way link) still counts: that link is what a cut
        # must not make worse.
        good = any(s.get('status') == 'good' for s in recent)
        out.append({
            'node': peer,
            'snr': round(median(samples), 1) if samples else None,
            'snr_fade': round(_percentile(samples, 10), 1) if samples else None,
            'snr_min': min(samples) if samples else None,
            'samples': len(samples), 'source': source,
            'signal': signal, 'rate': round(rate, 1) if rate else None,
            'blocked': blocked, 'current': bool(recent) and good,
            'intended': bool(recent) and good and not blocked,
            'routable': mt.get('routable', tt.get('routable')),
            'distance_m': (mine or theirs or {}).get('distance'),
        })
    return out


def _receiver_context(cur, receiver, name, now, stale_after):
    """The receiver's other current RF neighbours (its view of them)."""
    others = []
    for r in cur.execute('''
        SELECT target_node, snr, signal, last_seen, blocked, quality FROM links
        WHERE source_node = ? AND target_node != ? AND UPPER(link_type) = 'RF' AND status != 'removed'
    ''', (receiver, name)).fetchall():
        seen = ld._parse_ts(r['last_seen'])
        if seen and (now - seen).total_seconds() <= stale_after and r['snr'] is not None:
            others.append({'node': r['target_node'], 'snr': float(r['snr']),
                           'signal': ld._num(r['signal']), 'blocked': bool(r['blocked'])})
    return sorted(others, key=lambda o: o['snr'])


def _near_far(cur, name, neighbours, cut, now, stale_after):
    """Receivers where this radio is loud: what the cut does for them."""
    out = []
    for n in neighbours:
        if n['snr'] is None:
            continue
        hot = n['snr'] >= ld.HOT_SNR or (n['signal'] is not None and n['signal'] >= ld.HOT_SIGNAL_DBM)
        if not hot:
            continue
        others = _receiver_context(cur, n['node'], name, now, stale_after)
        weakest = others[0] if others else None
        gap = n['snr'] - weakest['snr'] if weakest else None
        after_snr = n['snr'] - cut
        after_signal = n['signal'] - cut if n['signal'] is not None else None
        out.append({
            'receiver': n['node'], 'snr': n['snr'], 'signal': n['signal'],
            'snr_after': round(after_snr, 1), 'signal_after': after_signal,
            'hot_after': after_snr >= ld.HOT_SNR or (after_signal is not None and after_signal >= ld.HOT_SIGNAL_DBM),
            'overload': n['signal'] is not None and n['signal'] >= ld.SIGNAL_HOT,
            'weakest': weakest, 'gap_db': round(gap, 1) if gap is not None else None,
            'gap_after_db': round(max(0.0, gap - cut), 1) if gap is not None else None,
            'other_neighbours': len(others),
            'target_cut_db': max(0, round(n['snr'] - ld.TARGET_SNR)),
        })
    return out


def _evaluate(neighbours, cut, min_snr, margin):
    rows = []
    for n in neighbours:
        after = n['snr'] - cut if n['snr'] is not None else None
        rows.append({**n, 'snr_after': round(after, 1) if after is not None else None,
                     'fade_after': round(n['snr_fade'] - cut, 1) if n['snr_fade'] is not None else None,
                     'status_now': _status(n, 0, min_snr, margin),
                     'status': _status(n, cut, min_snr, margin),
                     'rate_after': _estimate_rate(n['rate'], n['snr'], after)})
    return rows


def advise(node_name, cut_db=None, hours=24):
    """Predicted neighbour SNR after a TX power cut on node_name. Without
    cut_db: the curve for 0..MAX_CUT_DB and the suggested safe cut."""
    name = (node_name or '').strip().lower()
    min_snr, margin = config.POWER_ADVISOR_MIN_SNR, config.POWER_ADVISOR_MARGIN_DB
    stale_after = ld._stale_after_seconds()
    infra = set(database.get_infrastructure_node_names())
    node = database.get_observed_node(name)
    if not node:
        return {'error': f'Unknown node {name}'}
    with database.get_connection() as conn:
        cur = conn.cursor()
        # Judge "current" as of the latest scan when the scanner has stopped
        # (otherwise every link would look stale).
        now = datetime.now()
        latest = ld._parse_ts((cur.execute('SELECT MAX(last_seen) FROM links').fetchone() or [None])[0])
        if latest and (now - latest).total_seconds() > stale_after:
            now = latest
        neighbours = _neighbours(cur, name, hours, now, stale_after)
        base = {
            'node': name, 'generated_at': datetime.now().strftime(ld.TS_FMT), 'as_of': now.strftime(ld.TS_FMT),
            'hours': hours,
            'is_infrastructure': name in infra,
            'tx_power_dbm': node.get('rf_txpower'),
            'thresholds': {'min_snr': min_snr, 'margin_db': margin, 'hot_snr': ld.HOT_SNR,
                           'hot_signal_dbm': ld.HOT_SIGNAL_DBM, 'target_snr': ld.TARGET_SNR,
                           'max_cut_db': MAX_CUT_DB},
            'notes': ['Cuts are relative dB: AREDN sysinfo does not report TX power.'
                      if node.get('rf_txpower') is None else
                      f"Current TX power {node['rf_txpower']:g} dBm (as reported)."],
        }
        if name in infra:
            # What this radio hears from each non-infrastructure neighbour:
            # those are the ends whose power can be cut instead.
            heard = _receiver_context(cur, name, '', now, stale_after)
            alternatives = [{'node': o['node'], 'snr_at_us': o['snr'], 'signal_at_us': o['signal'],
                             'target_cut_db': max(0, round(o['snr'] - ld.TARGET_SNR))}
                            for o in sorted(heard, key=lambda o: -o['snr']) if o['node'] not in infra]
            return {**base, 'refused': True,
                    'reason': f'{name} is infrastructure: its TX power is never cut. Fix near-far problems at '
                              'the non-infrastructure end (lower that radio\'s power, re-aim it, or use a '
                              'lower-gain antenna there).',
                    'neighbours': _evaluate(neighbours, 0, min_snr, margin),
                    'alternatives': alternatives}
        if not any(n['snr'] is not None for n in neighbours):
            return {**base, 'refused': False, 'neighbours': [], 'curve': [], 'suggested_cut_db': None,
                    'near_far': [], 'reason': f'No RF neighbour reports hearing {name} in the last {hours} h.'}

        curve, suggested = [], 0
        intended = [n for n in neighbours if n['intended'] and n['snr'] is not None]
        for cut in range(0, MAX_CUT_DB + 1):
            statuses = {n['node']: _status(n, cut, min_snr, margin) for n in neighbours if n['snr'] is not None}
            curve.append({'cut_db': cut,
                          'snr': {n['node']: round(n['snr'] - cut, 1) for n in neighbours if n['snr'] is not None},
                          'ok': sum(1 for s in statuses.values() if s == 'ok'),
                          'marginal': sum(1 for s in statuses.values() if s == 'marginal'),
                          'lost': sum(1 for s in statuses.values() if s == 'lost'),
                          'intended_ok': all(statuses[n['node']] == 'ok' for n in intended)})
        # Largest cut that keeps every intended peer ok (0 if one isn't ok even now).
        for c in curve:
            if c['intended_ok']:
                suggested = c['cut_db']
            else:
                break
        # Comfortable cut: the weakest intended peer keeps ~TARGET_SNR (the
        # level link diagnostics turns a loud transmitter down to).
        weakest = min((n['snr'] for n in intended), default=None)
        comfortable = min(suggested, max(0, round(weakest - ld.TARGET_SNR))) if weakest is not None else 0
        cut = suggested if cut_db is None else max(0, min(60, float(cut_db)))
        # Who goes marginal / lost at bigger cuts than suggested.
        thresholds = []
        for n in neighbours:
            if n['snr'] is None:
                continue
            first_marginal = next((c['cut_db'] for c in curve
                                   if _status(n, c['cut_db'], min_snr, margin) != 'ok'), None)
            first_lost = next((c['cut_db'] for c in curve
                               if _status(n, c['cut_db'], min_snr, margin) == 'lost'), None)
            thresholds.append({'node': n['node'], 'intended': n['intended'],
                               'marginal_from_db': first_marginal, 'lost_from_db': first_lost})
        return {**base, 'refused': False, 'cut_db': cut, 'suggested_cut_db': suggested,
                'comfortable_cut_db': comfortable,
                'neighbours': _evaluate(neighbours, cut, min_snr, margin),
                'near_far': _near_far(cur, name, neighbours, cut, now, stale_after),
                'thresholds_by_neighbour': thresholds, 'curve': curve,
                'intended_count': len(intended)}

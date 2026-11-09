"""
Link potential plot (TODO #69, UISP style; Diagnostics "Link Potential").

Every current RF link direction with both ends located becomes a dot:
distance across, measured SNR (or rate) up. Each direction also gets what
the link budget says it could do -- free-space prediction with each radio's
stored gain/azimuth/beamwidth (link_budget._radio/_direction, no terrain so
the whole mesh is quick and offline) -- and the PHY rate that predicted SNR
would support (HT MCS steps, 2 streams, scaled by channel width). The dot is
coloured by measured / theoretical rate: links far below the curve are the
ones to re-aim, re-channel or look at for obstruction. An iPerf capacity
(used only without a PHY rate) is compared with IPERF_EFFICIENCY of it.

Measured rate: the LQM transmit PHY rate (median over `hours`), else the
iPerf capacity from capacity.py (shown too; it also hits node CPU and the
path, so it is a poor RF measure). A reference curve is the
predicted SNR against distance for a typical pair (median gains, frequency
and noise of the plotted links, on boresight).
Read-only.
"""

import math
from datetime import datetime, timedelta
from statistics import median

import database
import link_budget
import link_diagnostics as ld
import path_view

TS_FMT = '%Y-%m-%d %H:%M:%S'
# Per-stream 20 MHz HT rates (Mbps, long GI) for MCS 0-7 and the SNR each needs.
HT20_RATES = [6.5, 13, 19.5, 26, 39, 52, 58.5, 65]
MCS_SNR = [2, 5, 9, 11, 15, 18, 20, 25]
WIDTH_FACTOR = {5: 0.25, 10: 0.5, 20: 1.0, 40: 2.077, 80: 4.5}
STREAMS = 2
IPERF_EFFICIENCY = 0.6
# Real links rarely pass MCS 4-5 of 2 streams (~60% of the table maximum),
# so the grades are set below the table: good >= 50%, far below < 25%.
GOOD_RATIO = 0.5
POOR_RATIO = 0.25


def rate_for_snr(snr, chanbw=20):
    """PHY rate (Mbps) the SNR supports: the highest HT MCS it clears."""
    if snr is None or snr < MCS_SNR[0]:
        return 0.0
    mcs = max(i for i, need in enumerate(MCS_SNR) if snr >= need)
    return round(HT20_RATES[mcs] * STREAMS * WIDTH_FACTOR.get(int(chanbw or 20), (chanbw or 20) / 20), 1)


# Per-stream VHT rates (Mbps, long GI) for MCS 0-9; HT MCS 0-7 are the first
# eight at 20/40 MHz. 5/10 MHz scale the 20 MHz row. MCS 8/9 need about 29/31 dB.
VHT_RATES = {20: [6.5, 13, 19.5, 26, 39, 52, 58.5, 65, 78, 86.7],
             40: [13.5, 27, 40.5, 54, 81, 108, 121.5, 135, 162, 180],
             80: [29.3, 58.5, 87.8, 117, 175.5, 234, 263.3, 292.5, 351, 390]}
VHT_SNR = MCS_SNR + [29, 31]
RATE_MATCH = 0.03


def _per_stream(chanbw):
    bw = int(chanbw or 20)
    return VHT_RATES.get(bw) or [r * bw / 20 for r in VHT_RATES[20]]


def mcs_from_rate(rate_mbps, chanbw=20, streams=STREAMS, nearest=True):
    """(streams, MCS) pairs whose PHY rate (long or short GI) matches the rate
    within RATE_MATCH, fewest streams first; else the nearest one ([] with
    nearest=False: e.g. a legacy 802.11a rate). A rate can fit two: 54 Mbps
    at 40 MHz is MCS 3 on one stream or MCS 1 on two."""
    if not rate_mbps or rate_mbps <= 0:
        return []
    fits = []
    for nss in range(1, streams + 1):
        for mcs, r in enumerate(_per_stream(chanbw)):
            err = min(abs(r * nss * gi - rate_mbps) / rate_mbps for gi in (1.0, 10 / 9))
            fits.append((err, nss, mcs))
    close = [(nss, mcs) for err, nss, mcs in fits if err <= RATE_MATCH]
    return close or ([min(fits)[1:]] if nearest else [])


def supported_mcs(snr):
    """Highest per-stream MCS (0-9) the SNR clears, None below MCS 0."""
    if snr is None or snr < VHT_SNR[0]:
        return None
    return max(i for i, need in enumerate(VHT_SNR) if snr >= need)


def _median(conn, sql, params):
    vals = [r[0] for r in conn.execute(sql, params) if r[0] is not None]
    return round(median(vals), 1) if vals else None


def report(hours=24, metric='snr'):
    infra = database.get_infrastructure_nodes()
    try:
        import capacity
        caps = {(r['source'], r['target']): r['capacity'] for r in capacity.report(hours=168)['links']
                if r.get('capacity') is not None}
    except Exception:
        caps = {}
    points, skipped = [], 0
    with database.get_connection() as conn:
        # Window ends at the newest scan sample, so a stopped scanner still plots.
        newest = conn.execute('SELECT MAX(timestamp) FROM link_history').fetchone()[0]
        end = datetime.strptime(newest[:19], TS_FMT) if newest else datetime.now()
        since = (end - timedelta(hours=hours)).strftime(TS_FMT)
        links = path_view._links(conn)
        radios = {}

        def radio(name):
            if name not in radios:
                radios[name] = link_budget._radio(conn, name)
            return radios[name]

        for (src, dst), row in links.items():
            # row = how src hears dst; the transmitter is dst.
            if (row['link_type'] or '').upper() != 'RF' or not row['usable'] or src.startswith('mac-') \
                    or dst.startswith('mac-'):
                continue
            rx, tx = radio(src), radio(dst)
            if not rx or not tx or None in (rx['lat'], rx['lon'], tx['lat'], tx['lon']):
                skipped += 1
                continue
            dist = ld._haversine_m(tx['lat'], tx['lon'], rx['lat'], rx['lon'])
            freq = tx['frequency'] or rx['frequency']
            if not dist or dist < 1 or not freq:
                skipped += 1
                continue
            bw = tx['chanbw'] or rx['chanbw'] or 20
            measured = link_budget._measured(conn, src, dst, since)
            d = link_budget._direction(tx, rx, dist, freq, bw, 0.0, measured)
            snr = measured['snr'] if measured['snr'] is not None else ld._num(row['snr'])
            phy = _median(conn, '''SELECT tx_bitrate FROM link_history WHERE source_node = ? AND target_node = ?
                                   AND timestamp >= ?''', (dst, src, since))
            if phy is None:  # no recent samples: the sender's current tracker
                cur = conn.execute('SELECT tx_rate FROM links WHERE source_node = ? AND target_node = ?',
                                   (dst, src)).fetchone()
                phy = ld._num(cur['tx_rate']) if cur else None
            cap = caps.get((dst, src))
            theo = rate_for_snr(d['predicted_snr'], bw)
            # The radio's own PHY rate is the RF measure; iPerf (about
            # IPERF_EFFICIENCY of PHY at best) also hits node CPU and the path.
            have = phy if phy is not None else cap
            expect = theo * (1.0 if phy is not None else IPERF_EFFICIENCY)
            ratio = round(have / expect, 2) if have is not None and expect else None
            points.append({
                'from': dst, 'to': src, 'distance_km': round(dist / 1000, 2), 'frequency': freq, 'chanbw': bw,
                'snr': snr, 'predicted_snr': d['predicted_snr'],
                'snr_gap_db': round(snr - d['predicted_snr'], 1) if snr is not None else None,
                'rate_mbps': have, 'rate_basis': 'LQM PHY rate' if phy is not None else ('iperf capacity' if cap is not None else None),
                'iperf_mbps': cap,
                'theoretical_mbps': theo, 'expected_mbps': round(expect, 1), 'ratio': ratio,
                'grade': None if ratio is None else 'good' if ratio >= GOOD_RATIO else 'fair' if ratio >= POOR_RATIO else 'poor',
                'infrastructure': src in infra or dst in infra,
                'assumed': rx['gain_assumed'] or tx['gain_assumed'] or tx['tx_assumed'],
                'gain_assumed': rx['gain_assumed'] or tx['gain_assumed'],
            })
    gains = [radios[n]['gain'] for n in radios if radios[n]]
    freqs = [p['frequency'] for p in points]
    noise = -95.0
    curve = []
    if points:
        g, f = median(gains), median(freqs)
        tx_dbm = median(radios[n]['tx_dbm'] for n in radios if radios[n])
        top = max(p['distance_km'] for p in points)
        steps = 40
        for i in range(1, steps + 1):
            km = top * 1.1 * i / steps
            snr = tx_dbm + 2 * g - link_budget.fspl_db(km * 1000, f) - 2 * link_budget.CABLE_LOSS_DB - noise
            curve.append({'distance_km': round(km, 2), 'snr': round(snr, 1),
                          'rate_mbps': rate_for_snr(snr, median(p['chanbw'] for p in points))})
    below = sorted([p for p in points if (p['ratio'] is not None and p['ratio'] < POOR_RATIO)
                    or (p['snr_gap_db'] is not None and p['snr_gap_db'] <= -link_budget.WEAKER_DB)],
                   key=lambda p: (p['ratio'] if p['ratio'] is not None else 1, p['snr_gap_db'] or 0))
    return {'generated_at': datetime.now().strftime(TS_FMT), 'hours': hours, 'metric': metric,
            'points': sorted(points, key=lambda p: p['distance_km']), 'curve': curve, 'below': below,
            'skipped': skipped, 'noise_assumed': noise,
            'notes': ['Predicted with free-space loss and stored antenna settings; terrain, trees and buildings '
                      'are left out (RF Stats "Link budget & terrain" adds them for one link).',
                      'Theoretical rate assumes 2 streams and HT MCS steps; measured rate is the overnight iPerf '
                      'capacity when there is one, else the radio\'s PHY rate.']}


# --------------------------------------------- far below potential (watch) --
# An infrastructure RF link whose rate or SNR is far below its potential both
# over UNDER_HOURS and the last UNDER_RECENT_HOURS gets a Mesh Health and
# Infrastructure Review finding and, from the hourly watch, an event.
UNDER_HOURS = 24
UNDER_RECENT_HOURS = 3
EVENT_UNDER = 'link_underperforming'
EVENT_UNDER_CLEARED = 'link_underperforming_cleared'


def poor_reasons(p):
    """Why one plotted direction is far below its potential ([] = it isn't).
    The SNR gap counts only when both antenna gains are set: a guessed gain
    can be several dB off."""
    out = []
    if p.get('ratio') is not None and p['ratio'] < POOR_RATIO and p.get('rate_mbps') is not None:
        out.append(f"{p['rate_mbps']:g} Mbps is {round(p['ratio'] * 100)}% of the {p['expected_mbps']:g} Mbps "
                   f"its predicted {p['predicted_snr']:g} dB SNR supports at {p['chanbw']:g} MHz")
    if p.get('snr_gap_db') is not None and p['snr_gap_db'] <= -link_budget.WEAKER_DB and not p.get('gain_assumed'):
        out.append(f"SNR {p['snr']:g} dB is {-p['snr_gap_db']:g} dB below the link budget ({p['predicted_snr']:g} dB)")
    return out


def underperforming(hours=UNDER_HOURS, recent_hours=UNDER_RECENT_HOURS, infra_only=True):
    """Directions far below their potential over `hours` and still over the
    last `recent_hours` (so a link fixed this morning drops out at once).
    [{from, to, reasons, rate_mbps, expected_mbps, ratio, snr, predicted_snr, snr_gap_db, chanbw, ...}]."""
    recent = {(p['from'], p['to']): p for p in report(hours=recent_hours)['points']}
    out = []
    for p in report(hours=hours)['points']:
        if infra_only and not p['infrastructure']:
            continue
        now = recent.get((p['from'], p['to']))
        reasons = poor_reasons(p)
        if reasons and now is not None and poor_reasons(now):
            out.append(dict(p, reasons=reasons, recent=poor_reasons(now)))
    return sorted(out, key=lambda p: (p['ratio'] if p['ratio'] is not None else 1, p['from'], p['to']))


def recommendation(p):
    slow = p.get('ratio') is not None and p['ratio'] < POOR_RATIO and p.get('rate_mbps')
    single = slow and any(nss == 1 for nss, _ in mcs_from_rate(p['rate_mbps'], p['chanbw'], nearest=False))
    if not slow:
        return ('The rate is fine but the signal is well under the link budget: check the aim at both ends, '
                'the path (trees, new buildings) and the stored antenna gain and height.')
    return ('Check the aim at both ends, then each chain: a 2x2 link on one stream at a good SNR usually '
            'means one polarisation is off (aim, a loose or wet connector, a bad cable on one chain). '
            'If the signal matches the link budget, look for interference on the channel instead.'
            if single else
            'Check the aim at both ends and the path (trees, new buildings), then interference on the '
            'channel (Spectrum tab, WiFi survey).')


def ensure_tables():
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS link_potential_state (
                            node_a TEXT NOT NULL, node_b TEXT NOT NULL, since TEXT, detail TEXT,
                            PRIMARY KEY (node_a, node_b))''')


def active():
    ensure_tables()
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM link_potential_state ORDER BY since')]


def watch(now=None):
    """Hourly: raise `link_underperforming` (warning) for an infrastructure
    pair far below its potential (`underperforming`), clear it once neither
    direction is poor over the last UNDER_RECENT_HOURS. Pairs in maintenance
    are left alone. Returns the logged events."""
    import maintenance
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    pairs = {}
    for p in underperforming():
        pairs.setdefault(tuple(sorted((p['from'], p['to']))), []).append(p)
    recent = {tuple(sorted((p['from'], p['to']))) for p in report(hours=UNDER_RECENT_HOURS)['points']}
    events = []
    with database.get_connection() as conn:
        state = {(r['node_a'], r['node_b']): dict(r) for r in conn.execute('SELECT * FROM link_potential_state')}
        for key, dirs in pairs.items():
            if key in state or maintenance.is_in_maintenance(key[0], peer=key[1]):
                continue
            detail = (f"RF link to {key[1]}: far below its potential. "
                      + ' '.join(f"{p['from']} → {p['to']}: {'; '.join(p['reasons'])}." for p in dirs)
                      + ' ' + recommendation(dirs[0]))
            conn.execute('INSERT INTO link_potential_state VALUES (?, ?, ?, ?)', (key[0], key[1], now_s, detail))
            events.append((EVENT_UNDER, key[0], detail, 'warning'))
        for key, st in state.items():
            # Cleared only on fresh samples that look fine, never because the link went quiet.
            if key not in pairs and key in recent and not maintenance.is_in_maintenance(key[0], peer=key[1]):
                conn.execute('DELETE FROM link_potential_state WHERE node_a = ? AND node_b = ?', key)
                events.append((EVENT_UNDER_CLEARED, key[0],
                               f"RF link to {key[1]}: back near its potential (far below since {st['since']})", 'info'))
    for ev, node, text, sev in events:
        database.log_event(ev, node, text, sev)
    return [{'timestamp': now_s, 'event_type': ev, 'node_name': n, 'details': t, 'severity': s}
            for ev, n, t, s in events]


def install(scheduler):
    import config
    if not config.LINK_POTENTIAL_WATCH_ENABLED:
        return
    scheduler.add_job(lambda: watch(), 'interval', minutes=60, id='link_potential_watch', replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=5))

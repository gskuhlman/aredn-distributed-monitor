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

"""
Health checks on the SSH radio details (ssh_collect.py). Read-only over the
ssh_* tables; ssh_collect.evaluate() raises and clears the alarms (2 runs in
a row, maintenance respected) and logs the events in KINDS.

Per station (`RF link to <peer>: ...`):
  chain       one chain much weaker, or one stream at a good SNR (ssh_collect)
  retries     the radio resends each frame SSH_RETRY_RATIO+ times on average
              (tx retries / tx packets between collections, SSH_RETRY_MIN_PPS+
              packets/s), with the link's LQM SNR and the radio's missing-ACK
              share (ath10k fw_stats) in the text
  reconnects  the 802.11 association dropped and rejoined (station "connected
              time" went back without a node reboot) SSH_RECONNECTS_PER_DAY+
              times in 24 h -- short dropouts Babel may never notice
Per radio / node:
  interference  airtime busy while the radio neither receives a frame nor
              sends (energy it can't decode) at SSH_EXT_BUSY_PCT+ %
  rx_errors   SSH_RX_ERROR_PCT+ % of received MPDUs fail FCS/MIC/decrypt
              (ath10k fw_stats deltas)
  ethernet    carrier changes (SSH_ETH_FLAPS_PER_HOUR+ in an hour), growing
              CRC / rx / tx errors, or a port linked slower than it has been
  coverage    the radio's ACK window (`iw phy` coverage class) is shorter
              than its longest RF link (LQM distance): ACKs from the far end
              arrive after the radio stops waiting, so delivered frames count
              as lost and are resent. AREDN sets it from LQM's distance and
              rounds down, so most links are a little past it (listed in
              Settings Drift); alarmed only while that link also has heavy retries
  clock       node clock SSH_CLOCK_SKEW_SECONDS+ off this PC's
  fw_crash    the radio firmware crashed or needed a cold restart in the last
              24 h (ath10k fw_reset_stats, kept by the driver; warm restarts
              are not counted: every normal interface restart is one)
  phy_resets  the firmware resets the radio's PHY SSH_PHY_RESETS_PER_HOUR+
              times an hour (ath10k fw_stats "PDEV resets"; median of the
              last intervals, or the last two reads of a backed-off radio)
  memory      MemAvailable under RESOURCE_MEM_LOW_PCT, only for nodes
              Prometheus doesn't cover (same events as prom_watch)
"""

import json
import re
from datetime import datetime, timedelta
from statistics import median

import config
import database
import ssh_collect as sc

TS_FMT = sc.TS_FMT
# kind -> (raise event, clear event, clear text)
KINDS = {
    'chain': (sc.EVENT_ALARM, sc.EVENT_CLEARED, 'both chains and streams back to normal'),
    'retries': ('radio_retries_high', 'radio_retries_cleared', 'retries back to normal'),
    'reconnects': ('radio_reconnects', 'radio_reconnects_cleared', 'no more reconnects in 24 h'),
    'interference': ('radio_interference', 'radio_interference_cleared', 'airtime used by others back to normal'),
    'rx_errors': ('radio_rx_errors', 'radio_rx_errors_cleared', 'receive errors back to normal'),
    'ethernet': ('ethernet_problem', 'ethernet_cleared', 'Ethernet port back to normal'),
    'coverage': ('radio_distance_short', 'radio_distance_ok', 'ACK window covers the longest RF link again'),
    'clock': ('node_clock_skew', 'node_clock_ok', 'node clock back in step'),
    'memory': ('resource_alarm', 'resource_cleared', 'memory back to normal (SSH)'),
    'fw_crash': ('radio_fw_crash', 'radio_fw_crash_cleared', 'no radio firmware crash in 24 h'),
    'phy_resets': ('radio_phy_resets', 'radio_phy_resets_cleared', 'PHY resets back to normal'),
}
LINK_KINDS = {'chain', 'retries', 'reconnects'}
# Mesh Health category per kind
CATEGORY = {'chain': 'chains', 'retries': 'retries', 'reconnects': 'reconnects', 'interference': 'airtime',
            'rx_errors': 'rx_errors', 'ethernet': 'ethernet', 'coverage': 'radio_settings', 'clock': 'clock',
            'memory': 'resources', 'fw_crash': 'radio_log', 'phy_resets': 'radio_settings'}
ADVICE = {
    'retries': ("Frames are resent far more than the SNR explains. Compare both directions and the missing-ACK "
                "share: a short ACK window (radio distance), interference only the far end hears, or a "
                "driver/firmware state (a reboot changes it). LQM counts retries against the link's quality, so "
                "Babel may route around a link that still carries traffic well."),
    'reconnects': 'Look for power dips, firmware crashes or interference bursts at either end around those times.',
    'interference': ('Other transmitters share this channel: look at the WiFi scan and the Spectrum tab, and '
                     'consider a quieter channel (the channel planner counts this airtime).'),
    'rx_errors': 'Interference or collisions at this receiver (hidden nodes), or multipath: compare with the far end.',
    'ethernet': ('Check the cable, connectors, PoE injector and surge protector; a port that renegotiates '
                 'to 100 Mb/s usually has a bad pair.'),
    'coverage': ("Set the node's distance a little longer than the link (Advanced: radio distance / LQM) or check "
                 "the location so LQM's distance is right; then watch the retries."),
    'clock': 'Check NTP on the node (Basic setup: time server) and that it can reach one.',
    'memory': 'Reboot to recover; look for a leaking package or too many services.',
    'fw_crash': ('Note the firmware version and the dmesg lines around the crash (node page, Radio Details '
                 '(SSH) snapshots). Repeated crashes are worth a report to the AREDN team; check power and '
                 'temperature, and whether a WiFi scan or a settings change came just before.'),
    'phy_resets': ("The radio's firmware keeps resetting its transmit/receive chain. On these radios it follows "
                   "excessive retries: frames to a peer that stops answering (one-way or near-far link, wrong "
                   "distance setting) or interference. Check the radio's weakest or one-way client first, then "
                   "the channel (Spectrum, WiFi scan)."),
    'chain': ('Check the aim at both ends, then each chain: connectors, cables and water in either polarisation; '
              'swap the cables at the radio to see whether the weak chain follows the cable.'),
}


def _ts(s):
    return datetime.strptime(s, TS_FMT)


def _window(now, runs):
    return (now - timedelta(minutes=config.SSH_INTERVAL_MINUTES * runs + 5)).strftime(TS_FMT)


def _fresh(now):
    return _window(now, 1)


def _deltas(rows, fields, total=None):
    """Per-interval deltas of cumulative counters in chronological rows; an
    interval where `total` (or any field) went down (reset) is skipped."""
    out = []
    for p, q in zip(rows, rows[1:]):
        dt = (_ts(q['timestamp']) - _ts(p['timestamp'])).total_seconds()
        if dt <= 0 or any(p[f] is None or q[f] is None or q[f] < p[f] for f in fields):
            continue
        out.append(dict({f: q[f] - p[f] for f in fields}, dt=dt, t=q['timestamp']))
    return out


# ------------------------------------------------------------- stations --

def _station_rows(conn, since):
    rows = {}
    for r in conn.execute('''SELECT * FROM ssh_station_samples WHERE timestamp >= ? ORDER BY timestamp''', (since,)):
        rows.setdefault((r['node_name'], r['peer'] or r['mac']), []).append(dict(r))
    return rows


def station_rates(rows):
    """[{t, pps, retry_ratio, fails}] per collection interval."""
    out = []
    for d in _deltas(rows, ('tx_packets', 'tx_retries', 'tx_failed')):
        if d['tx_packets'] <= 0:
            continue
        out.append({'t': d['t'], 'pps': round(d['tx_packets'] / d['dt'], 1),
                    'retry_ratio': round(d['tx_retries'] / d['tx_packets'], 2), 'fails': d['tx_failed']})
    return out


def _lqm_snr(conn, node, peer):
    row = conn.execute('''SELECT rev_snr, snr FROM links WHERE source_node = ? AND target_node = ?
                          AND link_type = 'RF' ''', (node, peer)).fetchone()
    return next((float(v) for v in (row or ()) if v not in (None, '')), None)


def fw_rates(conn, node, since):
    """{phy: [{t, rx_error_pct, no_ack_pct, ppdu_noack_pct, rx_mpdus_ps}]} from fw_stats deltas.
    no_ack_pct = missed ACKs per 100 frames queued: every attempt that got no
    ACK counts, retries too, so it passes 100 on a link that resends a lot."""
    rows = {}
    for r in conn.execute('''SELECT * FROM ssh_fw_samples WHERE node_name = ? AND timestamp >= ?
                             ORDER BY timestamp''', (node, since)):
        rows.setdefault(r['phy'], []).append(dict(r))
    out = {}
    for phy, rs in rows.items():
        pts = []
        # Not every ath10k firmware lists the ppdu counters: their share is optional.
        ppdu = {d['t']: d for d in _deltas(rs, ('ppdu_aggr', 'ppdu_noack'))}
        # Good frames arrive by two paths: HTT, or straight to the stack (most of
        # them on some APs: 68M to the stack vs 3.5M to HTT on an SXTsq AP).
        stack = {d['t']: d['rx_mpdus_stack'] for d in _deltas(rs, ('rx_mpdus_stack',))}
        has_stack = any(r.get('rx_mpdus_stack') is not None for r in rs)
        for d in _deltas(rs, ('rx_mpdus', 'rx_mpdu_errors', 'tx_mpdus', 'mpdu_no_ack')):
            if has_stack and d['t'] not in stack:
                continue  # collected before the stack count was read
            rx = d['rx_mpdus'] + stack.get(d['t'], 0) + d['rx_mpdu_errors']
            pp = ppdu.get(d['t'])
            pts.append({'t': d['t'], 'rx_mpdus_ps': round(rx / d['dt'], 1),
                        'rx_error_pct': round(100 * d['rx_mpdu_errors'] / rx, 1) if rx else None,
                        'no_ack_pct': round(100 * d['mpdu_no_ack'] / d['tx_mpdus'], 1) if d['tx_mpdus'] else None,
                        'ppdu_noack_pct': round(100 * pp['ppdu_noack'] / pp['ppdu_aggr'], 1)
                        if pp and pp['ppdu_aggr'] else None,
                        'rx_mpdus': rx})
        out[phy] = pts
    return out


def retry_conditions(conn, now):
    since = _window(now, sc.ALARM_SAMPLES + 1)
    fresh = _fresh(now)
    bad, evaluated = {}, set()
    for (node, peer), rows in _station_rows(conn, since).items():
        if rows[-1]['timestamp'] < fresh:
            continue
        rates = station_rates(rows)[-sc.ALARM_SAMPLES:]
        if len(rates) < 2:
            continue  # too few intervals: keep the alarm as it is
        evaluated.add((node, peer))
        ratio, pps = median(r['retry_ratio'] for r in rates), median(r['pps'] for r in rates)
        if pps < config.SSH_RETRY_MIN_PPS or ratio < config.SSH_RETRY_RATIO:
            continue
        last = rows[-1]
        snr = _lqm_snr(conn, node, peer)
        fw = [p for pts in fw_rates(conn, node, since).values() for p in pts if p['no_ack_pct'] is not None]
        bits = [f"{node} resends each frame {ratio:.2f} times on average ({pps:.0f} packets/s"]
        if last.get('tx_rate'):
            mcs = f", {last['tx_kind']}-MCS {last['tx_mcs']} x{last['tx_nss']}" if last.get('tx_mcs') is not None else ''
            bits[0] += f" at {last['tx_rate']:g} Mbps{mcs}"
        bits[0] += ')'
        if snr is not None:
            bits.append(f"at {snr:g} dB SNR")
        if fw:
            bits.append(f"{median(p['no_ack_pct'] for p in fw[-sc.ALARM_SAMPLES:]):.0f} missed ACKs per 100 "
                        "frames it sends")
        bad[(node, peer)] = (f"RF link to {peer}: " + ', '.join(bits), 'warning')
    return bad, evaluated


def _uptimes(conn, since):
    out = {}
    for r in conn.execute('SELECT node_name, timestamp, uptime_s FROM ssh_node_samples WHERE timestamp >= ?', (since,)):
        out[(r['node_name'], r['timestamp'])] = r['uptime_s']
    return out


def reconnects(rows, uptimes):
    """Times the station's connected time went back without a node reboot."""
    out = []
    for p, q in zip(rows, rows[1:]):
        if p.get('connected_s') is None or q.get('connected_s') is None or q['connected_s'] >= p['connected_s']:
            continue
        up_p, up_q = uptimes.get((q['node_name'], p['timestamp'])), uptimes.get((q['node_name'], q['timestamp']))
        if up_p is not None and up_q is not None and up_q < up_p:
            continue  # the node rebooted: not a reconnect
        out.append(q['timestamp'])
    return out


def reconnect_conditions(conn, now):
    since = (now - timedelta(hours=24)).strftime(TS_FMT)
    fresh = _fresh(now)
    uptimes = _uptimes(conn, since)
    bad, evaluated = {}, set()
    for (node, peer), rows in _station_rows(conn, since).items():
        if rows[-1]['timestamp'] < fresh:
            continue
        evaluated.add((node, peer))
        times = reconnects(rows, uptimes)
        if len(times) >= config.SSH_RECONNECTS_PER_DAY:
            bad[(node, peer)] = (f"RF link to {peer}: the 802.11 link dropped and reconnected {len(times)} times "
                                 f"in 24 h (last at {times[-1]}), seen by {node}", 'warning')
    return bad, evaluated


# ---------------------------------------------------------------- radios --

def _survey_by_channel(conn, since):
    """{(node, frequency): rows} using one interface per node and channel (an
    AP's WDS sub-interfaces repeat the radio's survey)."""
    rows, chosen = {}, {}
    for r in conn.execute('SELECT * FROM ssh_survey_samples WHERE timestamp >= ? ORDER BY iface, timestamp', (since,)):
        key = (r['node_name'], r['frequency'])
        if chosen.setdefault(key, r['iface']) == r['iface']:
            rows.setdefault(key, []).append(dict(r))
    return rows


def busy_intervals(rows):
    return [b for b in (sc._busy(rows[i - 1:i + 1]) for i in range(1, len(rows))) if b]


def interference_conditions(conn, now):
    since = _window(now, sc.ALARM_SAMPLES + 1)
    fresh = _fresh(now)
    bad, evaluated = {}, set()
    for (node, freq), rows in _survey_by_channel(conn, since).items():
        if rows[-1]['timestamp'] < fresh:
            continue
        key = (node, str(freq))
        ivs = busy_intervals(rows)[-sc.ALARM_SAMPLES:]
        if len(ivs) < 2:
            continue
        evaluated.add(key)
        others = median(b['others_pct'] for b in ivs)
        if others >= config.SSH_EXT_BUSY_PCT:
            m = {k: round(median(b[k] for b in ivs), 1) for k in ('busy_pct', 'rx_pct', 'tx_pct')}
            bad[key] = (f"{node}: {others:g}% of the airtime on {freq} MHz is busy with energy it can't decode "
                        f"(other networks or non-WiFi sources; medians over the last {len(ivs)} collections: busy "
                        f"{m['busy_pct']:g}%, receiving {m['rx_pct']:g}%, sending {m['tx_pct']:g}%)", 'warning')
    return bad, evaluated


def rx_error_conditions(conn, now):
    since = _window(now, sc.ALARM_SAMPLES + 1)
    fresh = _fresh(now)
    nodes = [r[0] for r in conn.execute('SELECT DISTINCT node_name FROM ssh_fw_samples WHERE timestamp >= ?', (fresh,))]
    bad, evaluated = {}, set()
    for node in nodes:
        for phy, pts in fw_rates(conn, node, since).items():
            key = (node, phy)
            pts = [p for p in pts if p['rx_error_pct'] is not None and p['rx_mpdus'] >= 1000][-sc.ALARM_SAMPLES:]
            if len(pts) < 2:
                continue
            evaluated.add(key)
            pct = median(p['rx_error_pct'] for p in pts)
            if pct >= config.SSH_RX_ERROR_PCT:
                bad[key] = (f"{node}: {pct:g}% of the frames it receives fail their checksum or decryption "
                            f"({pts[-1]['rx_mpdus_ps']:g} frames/s; frames overheard from other networks on the "
                            "channel and collisions count too)", 'warning')
    return bad, evaluated


# ------------------------------------------------------------ ethernet --

def ethernet_conditions(conn, now):
    fresh = _fresh(now)
    hour = (now - timedelta(hours=1, minutes=5)).strftime(TS_FMT)
    month = (now - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT)
    rows = {}
    for r in conn.execute('SELECT * FROM ssh_eth_samples WHERE timestamp >= ? ORDER BY timestamp', (hour,)):
        rows.setdefault((r['node_name'], r['iface']), []).append(dict(r))
    fastest = {(r[0], r[1]): r[2] for r in conn.execute(
        '''SELECT node_name, iface, MAX(speed) FROM ssh_eth_samples WHERE timestamp >= ? AND carrier = 1
           GROUP BY node_name, iface''', (month,))}
    bad, evaluated = {}, set()
    for key, rs in rows.items():
        last = rs[-1]
        if last['timestamp'] < fresh:
            continue
        evaluated.add(key)
        node, iface = key
        problems = []
        flaps = [r['carrier_changes'] for r in rs if r['carrier_changes'] is not None]
        if len(flaps) >= 2 and flaps[-1] - flaps[0] >= config.SSH_ETH_FLAPS_PER_HOUR:
            problems.append(f"link went down/up {flaps[-1] - flaps[0]} times in the last hour")
        if len(rs) >= 2:
            p = rs[-2]
            grew = [f"{label} +{last[col] - p[col]}" for col, label in
                    (('rx_crc_errors', 'CRC errors'), ('rx_errors', 'receive errors'), ('tx_errors', 'send errors'))
                    if last[col] is not None and p[col] is not None and last[col] > p[col]]
            if grew:
                problems.append('errors since the last collection: ' + ', '.join(grew))
        best = fastest.get(key)
        if last['carrier'] == 1 and last['speed'] and best and last['speed'] < best:
            problems.append(f"linked at {last['speed']} Mb/s{' ' + last['duplex'] if last['duplex'] else ''}, "
                            f"was {best} Mb/s")
        if problems:
            bad[key] = (f"{node} port {iface}: " + '; '.join(problems), 'warning')
    return bad, evaluated


# -------------------------------------------------------------- settings --

def longest_links(conn):
    """{node: (peer, metres)}: each node's longest current RF link by LQM distance."""
    out = {}
    for r in conn.execute('''SELECT source_node, target_node, distance FROM links WHERE link_type = 'RF'
                             AND status = 'good' AND distance > 0 AND target_node NOT LIKE 'mac-%' '''):
        if r[2] > out.get(r[0], (None, 0))[1]:
            out[r[0]] = (r[1], r[2])
    return out


def coverage_conditions(conn, now, retry_bad=()):
    longest = longest_links(conn)
    active = {(r[0], r[1]) for r in conn.execute("SELECT node_name, peer FROM ssh_alarm_state WHERE kind = 'retries' AND active = 1")}
    bad, evaluated = {}, set()
    for s in conn.execute('SELECT node_name, coverage_class, coverage_m FROM ssh_settings WHERE coverage_m IS NOT NULL'):
        node = s['node_name']
        key = (node, 'coverage')
        evaluated.add(key)
        peer, dist = longest.get(node, (None, 0))
        if not peer or s['coverage_m'] >= dist:
            continue
        if (node, peer) not in active and (node, peer) not in retry_bad:
            continue
        sev = 'warning'
        bad[key] = (f"{node}: ACK window (coverage class {s['coverage_class']}, up to {s['coverage_m']} m) is shorter "
                    f"than its {dist:.0f} m RF link to {peer}: ACKs from {peer} can arrive after {node} stops "
                    "waiting, so delivered frames count as lost and are resent", sev)
    return bad, evaluated


def _latest_node_samples(conn, now):
    return [dict(r) for r in conn.execute('''SELECT s.* FROM ssh_node_samples s JOIN (
        SELECT node_name, MAX(timestamp) AS t FROM ssh_node_samples GROUP BY node_name) n
        ON n.node_name = s.node_name AND n.t = s.timestamp WHERE s.timestamp >= ?''', (_fresh(now),))]


def clock_conditions(conn, now):
    bad, evaluated = {}, set()
    for r in _latest_node_samples(conn, now):
        key = (r['node_name'], 'clock')
        evaluated.add(key)
        off = r['clock_offset_s']
        if off is not None and abs(off) >= config.SSH_CLOCK_SKEW_SECONDS:
            bad[key] = (f"{r['node_name']}: clock is {abs(off):.0f} s {'ahead of' if off > 0 else 'behind'} this "
                        "PC's: log times and anything scheduled on the node are off", 'warning')
    return bad, evaluated


def memory_conditions(conn, now):
    hour = (now - timedelta(hours=1)).strftime(TS_FMT)
    try:
        prom = {r[0] for r in conn.execute('SELECT DISTINCT node_name FROM prom_node_samples WHERE timestamp >= ?', (hour,))}
    except Exception:  # Prometheus tables not created
        prom = set()
    bad, evaluated = {}, set()
    for r in _latest_node_samples(conn, now):
        if r['node_name'] in prom or not r['mem_total_kb'] or r['mem_avail_kb'] is None:
            continue
        key = (r['node_name'], 'memory')
        evaluated.add(key)
        pct = 100 * r['mem_avail_kb'] / r['mem_total_kb']
        if pct < config.RESOURCE_MEM_LOW_PCT:
            bad[key] = (f"Memory low on {r['node_name']}: {r['mem_avail_kb'] / 1024:.0f} of "
                        f"{r['mem_total_kb'] / 1024:.0f} MB available ({pct:.0f}%, SSH)", 'warning')
    return bad, evaluated


def _fw_rows(conn, since):
    rows = {}
    for r in conn.execute('SELECT * FROM ssh_fw_samples WHERE timestamp >= ? ORDER BY timestamp', (since,)):
        rows.setdefault((r['node_name'], r['phy']), []).append(dict(r))
    return rows


def fw_crash_conditions(conn, now):
    """Radios whose firmware crash or cold-restart counter went up in 24 h."""
    rows = _fw_rows(conn, (now - timedelta(hours=24, minutes=5)).strftime(TS_FMT))
    fresh = _fresh(now)
    bad, evaluated = {}, set()
    for (node, phy), rs in rows.items():
        rs = [r for r in rs if r.get('fw_crashes') is not None]
        if not rs or rs[-1]['timestamp'] < fresh:
            continue
        evaluated.add((node, phy))
        crashes = cold = warm = 0
        for p, q in zip(rs, rs[1:]):
            if q['fw_crashes'] < p['fw_crashes']:
                continue  # rebooted
            crashes += q['fw_crashes'] - p['fw_crashes']
            cold += max(0, (q['fw_cold_resets'] or 0) - (p['fw_cold_resets'] or 0))
            warm += max(0, (q['fw_warm_resets'] or 0) - (p['fw_warm_resets'] or 0))
        if crashes or cold:
            last = rs[-1]
            bad[(node, phy)] = (
                f"{node}: radio firmware crashed {crashes} time(s)" + (f", {cold} cold restart(s)" if cold else '')
                + (f" and {warm} warm restart(s)" if warm else '') + f" in 24 h ({last['fw_crashes']} crashes, "
                f"{last['fw_warm_resets']} warm restarts since boot)", 'warning')
    return bad, evaluated


def phy_reset_rates(rows):
    """[(t, resets/h, excessive retries/h)] from consecutive fw_stats reads."""
    out = []
    for d in _deltas(rows, ('pdev_resets',)):
        if d['dt'] >= 300:
            out.append((d['t'], 3600 * d['pdev_resets'] / d['dt'], None))
    retries = {d['t']: 3600 * d['excessive_retries'] / d['dt'] for d in _deltas(rows, ('pdev_resets', 'excessive_retries'))}
    return [(t, r, retries.get(t)) for t, r, _ in out]


def phy_reset_conditions(conn, now):
    """Radios resetting their PHY SSH_PHY_RESETS_PER_HOUR+ times an hour:
    median over the last ALARM_SAMPLES intervals of the last 26 h (a radio
    backed off to daily fw_stats reads is judged on its last two reads)."""
    rows = _fw_rows(conn, (now - timedelta(hours=26)).strftime(TS_FMT))
    bad, evaluated = {}, set()
    for (node, phy), rs in rows.items():
        rs = [r for r in rs if r.get('pdev_resets') is not None]
        rates = phy_reset_rates(rs)[-sc.ALARM_SAMPLES:]
        if not rates:
            continue
        evaluated.add((node, phy))
        per_h = median(r for _, r, _ in rates)
        if per_h >= config.SSH_PHY_RESETS_PER_HOUR:
            retries = [x for _, _, x in rates if x is not None]
            bad[(node, phy)] = (
                f"{node}: the radio firmware resets its PHY about {per_h:.0f} times an hour"
                + (f" ({median(retries):.0f} frames an hour given up after excessive retries)" if retries else '')
                + (f"; read over {len(rates)} interval(s) to {rates[-1][0]}"), 'warning')
    return bad, evaluated


def fw_stats_status(conn):
    """{node: timeouts in a row} for nodes whose fw_stats read is failing."""
    sc.ensure_tables()
    return {r[0]: r[1] for r in conn.execute('SELECT node_name, fw_stats_fails FROM ssh_status WHERE fw_stats_fails > 0')}


def conditions(conn, now):
    """{kind: (bad {(node, subject): (detail, severity)}, evaluated keys)}."""
    retries = retry_conditions(conn, now)
    return {'chain': sc.chain_conditions(conn, now), 'retries': retries,
            'reconnects': reconnect_conditions(conn, now), 'interference': interference_conditions(conn, now),
            'rx_errors': rx_error_conditions(conn, now), 'ethernet': ethernet_conditions(conn, now),
            'coverage': coverage_conditions(conn, now, retries[0]), 'clock': clock_conditions(conn, now),
            'memory': memory_conditions(conn, now), 'fw_crash': fw_crash_conditions(conn, now),
            'phy_resets': phy_reset_conditions(conn, now)}


# --------------------------------------------------------------- readers --

def tx_power(node):
    """The mesh radio's TX power (dBm) from its newest `iw dev info` in the
    last 2 days, or None."""
    sc.ensure_tables()
    since = (datetime.now() - timedelta(days=2)).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT * FROM ssh_radio_samples WHERE node_name = ? AND timestamp = (
            SELECT MAX(timestamp) FROM ssh_radio_samples WHERE node_name = ? AND timestamp >= ?)''', (node, node, since))]
    mesh = sc._mesh_info(rows)
    return mesh['txpower_dbm'] if mesh else None


def settings(node=None):
    """ssh_settings rows (LQM options decoded), one node or all."""
    sc.ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM ssh_settings')]
    for r in rows:
        r['lqm'] = json.loads(r['lqm'] or '{}')
    return [r for r in rows if node is None or r['node_name'] == node]


FW_VER_RE = re.compile(r'(ath1[01]k)_\w+ (\S+): firmware ver (\S+)(?: api \d+)?(?: features (\S+))?')
FW_CHIP_RE = re.compile(r'ath1[01]k_\w+ (\S+): ([a-z]+\d+\w*) hw([\d.]+) ', re.I)


def parse_radio_firmware(text):
    """{driver_ct, radios: [{device, driver, chip, version, ct, features}],
    files: [{path, md5, size, overlay}]} from a `radiofw` snapshot or a dmesg / logread
    text (version lines only; the newest load of each device wins)."""
    radios, files = {}, {}
    for m in FW_CHIP_RE.finditer(text or ''):
        radios.setdefault(m.group(1), {})['chip'] = f"{m.group(2).lower()} hw{m.group(3)}"
    for m in FW_VER_RE.finditer(text or ''):
        feats = m.group(4).split(',') if m.group(4) else []
        radios.setdefault(m.group(2), {}).update(driver=m.group(1), version=m.group(3), features=feats,
                                                 ct=any(x.endswith('-CT') for x in feats))
    for line in (text or '').splitlines():
        line = line.strip()
        m = re.match(r'^([0-9a-f]{32})\s+(/\S+)$', line)
        if m:
            files.setdefault(m.group(2), {})['md5'] = m.group(1)
        m = re.match(r'^size (/\S+) (\d+)$', line)
        if m:
            files.setdefault(m.group(1), {})['size'] = int(m.group(2))
        m = re.match(r'^overlay (/\S+)$', line)
        if m:
            files.setdefault(m.group(1), {})['overlay'] = True
    return {'driver_ct': 'optimized for CT firmware' in (text or ''),
            'radios': [dict(device=d, chip=r.get('chip'), driver=r.get('driver'), version=r.get('version'),
                            ct=r.get('ct', False), features=r.get('features', []))
                       for d, r in sorted(radios.items()) if r.get('version')],
            'files': [dict(path=p, md5=f.get('md5'), size=f.get('size'), overlay=f.get('overlay', False))
                      for p, f in sorted(files.items())]}


def radio_firmware():
    """{node (lower case): {driver_ct, radios, files, read_at, source}}: the
    files from the newest `radiofw` snapshot, the versions from the newest
    snapshot (radiofw, dmesg or logread) that still has the boot lines."""
    sc.ensure_tables()
    out = {}
    with database.get_connection() as conn:
        rows = conn.execute("""SELECT node_name, kind, timestamp, text FROM ssh_snapshots
                               WHERE kind IN ('radiofw', 'dmesg', 'logread') ORDER BY timestamp DESC, id DESC""").fetchall()
    for node, kind, ts, text in rows:
        e = out.setdefault(node.lower(), {'driver_ct': False, 'radios': [], 'files': [], 'read_at': None,
                                          'source': None, '_files': False})
        if e['radios'] and e['_files']:
            continue
        p = parse_radio_firmware(text)
        e['driver_ct'] = e['driver_ct'] or p['driver_ct']
        if kind == 'radiofw' and not e['_files']:
            e.update(files=p['files'], _files=True, source='radiofw', read_at=e['read_at'] or ts)
        if p['radios'] and not e['radios']:
            e['radios'] = p['radios']
            if not e['source']:
                e.update(source=kind, read_at=ts)
    return {n: {k: v for k, v in e.items() if k != '_files'} for n, e in out.items() if e['radios'] or e['files']}


def busy_samples(hours=72):
    """{node: {frequency, others_pct (median over the window), samples}} for
    the channel planner."""
    sc.ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    out = {}
    with database.get_connection() as conn:
        for (node, freq), rows in _survey_by_channel(conn, since).items():
            ivs = busy_intervals(rows)
            if len(ivs) >= 2 and len(ivs) > len(out.get(node, {}).get('ivs', [])):
                out[node] = {'frequency': freq, 'ivs': ivs}
    return {n: {'frequency': v['frequency'], 'samples': len(v['ivs']),
                'others_pct': round(median(b['others_pct'] for b in v['ivs']), 1)} for n, v in out.items()}


def node_summary(node, hours=24):
    """Node page extras: system, radios, Ethernet, fw_stats rates, settings,
    per-station retry rate and reconnects."""
    sc.ensure_tables()
    now = datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    day = (now - timedelta(hours=24)).strftime(TS_FMT)
    with database.get_connection() as conn:
        system = conn.execute('SELECT * FROM ssh_node_samples WHERE node_name = ? ORDER BY timestamp DESC LIMIT 1',
                              (node,)).fetchone()
        newest = conn.execute('SELECT MAX(timestamp) FROM ssh_radio_samples WHERE node_name = ?', (node,)).fetchone()[0]
        radios = [dict(r) for r in conn.execute('SELECT * FROM ssh_radio_samples WHERE node_name = ? AND timestamp = ?',
                                                (node, newest))]
        eth_rows = {}
        for r in conn.execute('SELECT * FROM ssh_eth_samples WHERE node_name = ? AND timestamp >= ? ORDER BY timestamp',
                              (node, since)):
            eth_rows.setdefault(r['iface'], []).append(dict(r))
        fw = fw_rates(conn, node, since)
        uptimes = _uptimes(conn, day)
        stations = {}
        for (n, peer), rows in _station_rows(conn, day).items():
            if n != node:
                continue
            rates = station_rates(rows)
            stations[peer] = {'retry_ratio': rates[-1]['retry_ratio'] if rates else None,
                              'pps': rates[-1]['pps'] if rates else None,
                              'retry_series': [{'t': r['t'], 'retry_ratio': r['retry_ratio'], 'pps': r['pps']}
                                               for r in rates],
                              'reconnects_24h': len(reconnects(rows, uptimes))}
        longest = longest_links(conn).get(node)
    ethernet = []
    for iface, rs in sorted(eth_rows.items()):
        last = dict(rs[-1])
        changes = [r['carrier_changes'] for r in rs if r['carrier_changes'] is not None]
        last['flaps'] = changes[-1] - changes[0] if len(changes) >= 2 and changes[-1] >= changes[0] else 0
        ethernet.append(last)
    st = settings(node)
    s = st[0] if st else None
    if s and longest:
        s['longest_link'] = {'peer': longest[0], 'distance_m': longest[1]}
    sysd = dict(system) if system else None
    if sysd and sysd.get('mem_total_kb') and sysd.get('mem_avail_kb') is not None:
        sysd['mem_avail_pct'] = round(100 * sysd['mem_avail_kb'] / sysd['mem_total_kb'], 1)
    return {'system': sysd, 'radios': radios, 'ethernet': ethernet,
            'fw': {phy: {'last': pts[-1] if pts else None, 'series': pts} for phy, pts in fw.items()},
            'settings': s, 'station_rates': stations}

"""
Alarms on the Prometheus samples (TODO #66, #68, #74), run after every
prom_source snapshot. Events fire on transitions only, after WATCH_SCANS
snapshots in a row (raise) or clear; state in SQLite `prom_watch_state` so
restarts don't re-fire. Radios in a maintenance window are not raised.

Per node (resources, #74):
  mem_low     MemAvailable below RESOURCE_MEM_LOW_PCT of MemTotal
              (critical below half that);
  mem_leak    MemAvailable falling steadily since boot over at least
              RESOURCE_LEAK_DAYS (least-squares slope, r^2 >= 0.6) and on
              course to reach the low line within RESOURCE_LEAK_HORIZON_DAYS:
              a leak, before the reboot it usually ends in;
  flash_low   overlay (settings / packages) below RESOURCE_FLASH_LOW_PCT free;
  tmp_full    /tmp (RAM) more than 90% used;
  cpu_busy    CPU busy at or above RESOURCE_CPU_BUSY_PCT in the last 3 samples.
Per node (Babel, #68):
  route_drop  Babel route count ROUTE_DROP_PCT or more below its 24 h median
              (last hour left out), or neighbours down to 0: the mesh was cut
              in two before nodes turn unreachable.
Per RF station (#66):
  mcs_drop    tx rate MCS_RATE_DROP_PCT or more below its 24 h median while the
              LQM SNR stayed within MCS_SNR_HOLD_DB: interference or a hidden
              node, not path loss (the path would lower SNR too).

The exporter has no conntrack or file-descriptor counts, so those can't alarm.
Events: resource_alarm / resource_cleared, route_count_drop /
route_count_restored, mcs_drop / mcs_restored (`RF link to <peer>: ...`).
"""

import logging
from datetime import datetime, timedelta
from statistics import median

import config
import database
import maintenance
import prom_source

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
WATCH_SCANS = 2
_tables_ready = False

LABELS = {
    'mem_low': 'Low memory', 'mem_leak': 'Memory falling (leak?)', 'flash_low': 'Flash nearly full',
    'tmp_full': '/tmp nearly full', 'cpu_busy': 'CPU pegged', 'route_drop': 'Babel route count drop',
    'mcs_drop': 'MCS drop with steady SNR',
}
EVENTS = {  # kind -> (raise event, clear event)
    'route_drop': ('route_count_drop', 'route_count_restored'),
    'mcs_drop': ('mcs_drop', 'mcs_restored'),
}


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS prom_watch_state (
                kind TEXT NOT NULL, key TEXT NOT NULL, node_name TEXT, peer TEXT,
                active INTEGER NOT NULL DEFAULT 0, bad_count INTEGER NOT NULL DEFAULT 0,
                good_count INTEGER NOT NULL DEFAULT 0, since DATETIME, updated DATETIME,
                severity TEXT, detail TEXT,
                PRIMARY KEY (kind, key))''')
    _tables_ready = True


def _ts(value):
    return datetime.strptime(value[:19], TS_FMT)


def _pct(part, whole):
    return 100.0 * part / whole if whole else None


def leak_trend(samples, now):
    """Least-squares MemAvailable trend since the last boot:
    {'mb_per_day', 'r2', 'days', 'days_to_low'} or None."""
    if not samples:
        return None
    boot = samples[-1].get('boot_time')
    pts = [(_ts(s['timestamp']), s['mem_avail']) for s in samples
           if s.get('mem_avail') is not None and (boot is None or s.get('boot_time') is None
                                                 or abs((s['boot_time'] or 0) - boot) < 120)]
    if len(pts) < 12:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _ in pts]
    ys = [v / 1048576 for _, v in pts]
    span = xs[-1] - xs[0]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    total = samples[-1].get('mem_total')
    low_mb = (total or 0) / 1048576 * config.RESOURCE_MEM_LOW_PCT / 100
    now_mb = my + slope * (xs[-1] - mx)
    days_to_low = (now_mb - low_mb) / -slope if slope < 0 and now_mb > low_mb else None
    return {'mb_per_day': round(slope, 2), 'r2': round(r2, 2), 'days': round(span, 1),
            'days_to_low': None if days_to_low is None else round(days_to_low, 1),
            'now_mb': round(now_mb, 1), 'low_mb': round(low_mb, 1)}


def node_conditions(latest, history, now):
    """{kind: (severity, detail)} for one node that are bad right now."""
    out = {}
    total, avail = latest.get('mem_total'), latest.get('mem_avail')
    free_pct = _pct(avail, total) if avail is not None else None
    if free_pct is not None and free_pct < config.RESOURCE_MEM_LOW_PCT:
        out['mem_low'] = ('critical' if free_pct < config.RESOURCE_MEM_LOW_PCT / 2 else 'warning',
                          f'{round(avail / 1048576, 1)} MB of {round(total / 1048576)} MB available '
                          f'({round(free_pct, 1)}%)')
    trend = leak_trend(history, now)
    if (trend and trend['days'] >= config.RESOURCE_LEAK_DAYS and trend['r2'] >= 0.6 and trend['mb_per_day'] < 0
            and trend['days_to_low'] is not None and trend['days_to_low'] <= config.RESOURCE_LEAK_HORIZON_DAYS):
        out['mem_leak'] = ('warning', f"available memory falling {abs(trend['mb_per_day'])} MB/day over "
                                      f"{trend['days']} days since boot (r²={trend['r2']}); about "
                                      f"{trend['days_to_low']} days until it is below {trend['low_mb']} MB")
    fsize, favail = latest.get('flash_size'), latest.get('flash_avail')
    fpct = _pct(favail, fsize) if favail is not None else None
    if fpct is not None and fpct < config.RESOURCE_FLASH_LOW_PCT:
        out['flash_low'] = ('warning', f'{round(favail / 1024)} KB of {round(fsize / 1024)} KB flash free '
                                       f'({round(fpct, 1)}%): settings or package changes may fail')
    tsize, tavail = latest.get('tmp_size'), latest.get('tmp_avail')
    if tsize and tavail is not None and tavail / tsize < 0.1:
        out['tmp_full'] = ('warning', f'/tmp {round(100 - 100 * tavail / tsize)}% used '
                                      f'({round(tavail / 1048576, 1)} MB free); /tmp is RAM')
    recent = [h.get('cpu_busy') for h in history[-3:]]
    if len(recent) == 3 and all(v is not None and v >= config.RESOURCE_CPU_BUSY_PCT for v in recent):
        out['cpu_busy'] = ('warning', f'CPU {round(recent[-1])}% busy for the last 3 samples')
    # Babel routes (#68)
    cut = now - timedelta(hours=1)
    base = [h['routes'] for h in history if h.get('routes') is not None
            and now - timedelta(hours=24) <= _ts(h['timestamp']) < cut]
    routes = latest.get('routes')
    if routes is not None and len(base) >= 6:
        usual = median(base)
        if usual >= 20 and routes <= usual * (1 - config.ROUTE_DROP_PCT / 100):
            out['route_drop'] = ('warning', f'Babel routes {int(routes)} vs usual {int(usual)} '
                                            f'({round(100 - 100 * routes / usual)}% fewer): part of the mesh '
                                            f'is no longer reachable from here')
    if latest.get('neighbours') == 0 and any((h.get('neighbours') or 0) > 0 for h in history[-12:-1]):
        out['route_drop'] = ('warning', 'no Babel neighbours left (it had some in the last 2 hours)')
    return out


def station_conditions(now, conn):
    """{(node, peer): (severity, detail)} for RF stations whose rate fell while
    SNR held."""
    out = {}
    since = (now - timedelta(hours=24)).strftime(TS_FMT)
    recent_cut = now - timedelta(minutes=max(30, 3 * config.PROMETHEUS_POLL_MINUTES))
    base_cut = now - timedelta(hours=1)
    sta = {}
    for r in conn.execute('''SELECT node_name, peer, timestamp, tx_rate, tx_mcs FROM prom_station_samples
                             WHERE timestamp >= ? AND peer IS NOT NULL AND tx_rate IS NOT NULL
                             ORDER BY timestamp''', (since,)):
        sta.setdefault((r['node_name'], r['peer']), []).append(dict(r))
    snr = {}
    for r in conn.execute('''SELECT node_name, peer, timestamp, snr FROM prom_lqm_samples
                             WHERE timestamp >= ? AND snr IS NOT NULL ORDER BY timestamp''', (since,)):
        snr.setdefault((r['node_name'], r['peer']), []).append(dict(r))
    for key, rows in sta.items():
        cur = [r for r in rows if _ts(r['timestamp']) >= recent_cut]
        base = [r for r in rows if _ts(r['timestamp']) < base_cut]
        if not cur or len(base) < 12:
            continue
        rate_now, rate_usual = median(r['tx_rate'] for r in cur), median(r['tx_rate'] for r in base)
        if not rate_usual or rate_now > rate_usual * (1 - config.MCS_RATE_DROP_PCT / 100):
            continue
        s = snr.get(key) or []
        s_cur = [r['snr'] for r in s if _ts(r['timestamp']) >= recent_cut]
        s_base = [r['snr'] for r in s if _ts(r['timestamp']) < base_cut]
        if not s_cur or len(s_base) < 6:
            continue
        snr_now, snr_usual = median(s_cur), median(s_base)
        if abs(snr_now - snr_usual) > config.MCS_SNR_HOLD_DB:
            continue  # SNR moved too: path, not interference
        mcs_now = median(r['tx_mcs'] for r in cur if r['tx_mcs'] is not None) if any(
            r['tx_mcs'] is not None for r in cur) else None
        mcs_usual = median(r['tx_mcs'] for r in base if r['tx_mcs'] is not None) if any(
            r['tx_mcs'] is not None for r in base) else None
        out[key] = ('warning', f"RF link to {key[1]}: transmit rate {round(rate_now / 1e6, 1)} Mbps vs usual "
                               f"{round(rate_usual / 1e6, 1)} Mbps"
                               + (f" (MCS {int(mcs_now)} vs {int(mcs_usual)})" if mcs_now is not None
                                  and mcs_usual is not None else '')
                               + f" while SNR held at {round(snr_now)} dB (usual {round(snr_usual)}): "
                                 "interference or a hidden node, not path loss")
    return out


def _load(conn):
    return {(r['kind'], r['key']): dict(r) for r in conn.execute('SELECT * FROM prom_watch_state')}


def evaluate(now=None):
    """Update alarm state from the stored samples; returns logged events."""
    ensure_tables()
    prom_source.ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    latest = prom_source.latest_nodes()
    bad = {}
    since = (now - timedelta(days=max(config.RESOURCE_LEAK_DAYS * 2, 2))).strftime(TS_FMT)
    with database.get_connection() as conn:
        hist = {}
        for r in conn.execute('SELECT * FROM prom_node_samples WHERE timestamp >= ? ORDER BY timestamp', (since,)):
            hist.setdefault(r['node_name'], []).append(dict(r))
        for node, row in latest.items():
            for kind, (sev, detail) in node_conditions(row, hist.get(node, []), now).items():
                bad[(kind, node)] = {'node': node, 'peer': None, 'severity': sev, 'detail': detail}
        for (node, peer), (sev, detail) in station_conditions(now, conn).items():
            bad[('mcs_drop', f'{node}|{peer}')] = {'node': node, 'peer': peer, 'severity': sev, 'detail': detail}
        states = _load(conn)
    evaluated_nodes = set(latest)
    events, rows = [], []
    keys = set(bad) | {k for k, s in states.items() if s['active'] or s['bad_count']}
    for key in keys:
        kind, k = key
        st = states.get(key) or {'active': 0, 'bad_count': 0, 'good_count': 0, 'since': None,
                                 'severity': None, 'detail': None}
        info = bad.get(key)
        node = info['node'] if info else (st.get('node_name') or k.split('|')[0])
        peer = info['peer'] if info else st.get('peer')
        if info is None and node not in evaluated_nodes:
            continue  # no fresh sample: leave as is
        raise_ev, clear_ev = EVENTS.get(kind, ('resource_alarm', 'resource_cleared'))
        if info:
            st['bad_count'] += 1
            st['good_count'] = 0
            st['detail'], st['severity'] = info['detail'], info['severity']
            if not st['active'] and st['bad_count'] >= WATCH_SCANS:
                if maintenance.is_in_maintenance(node, peer=peer):
                    st['bad_count'] = 0
                else:
                    st['active'], st['since'] = 1, now_s
                    text = info['detail'] if kind == 'mcs_drop' else f"{LABELS[kind]}: {info['detail']}"
                    events.append((raise_ev, node, text, info['severity']))
        else:
            st['good_count'] += 1
            st['bad_count'] = 0
            if st['active'] and st['good_count'] >= WATCH_SCANS:
                st['active'], st['since'] = 0, None
                text = (f'RF link to {peer}: transmit rate back to usual' if kind == 'mcs_drop'
                        else f'{LABELS[kind]} cleared')
                events.append((clear_ev, node, text, 'info'))
        rows.append((kind, k, node, peer, st['active'], st['bad_count'], st['good_count'], st['since'],
                     now_s, st['severity'], st['detail']))
    with database.get_connection() as conn:
        conn.executemany('INSERT OR REPLACE INTO prom_watch_state VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
    out = []
    for ev, node, text, sev in events:
        database.log_event(ev, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': ev, 'node_name': node, 'details': text, 'severity': sev})
    return out


def active(node=None):
    """Open alarms (Mesh Health, node page)."""
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM prom_watch_state WHERE active = 1 ORDER BY since')]
    for r in rows:
        r['label'] = LABELS.get(r['kind'], r['kind'])
    return [r for r in rows if node is None or r['node_name'] == node]


# AREDN radios report HT MCS indexes (0-7 one stream, 8-15 two); the SNR each
# per-stream MCS needs, roughly (802.11n receiver sensitivity steps).
MCS_SNR = [2, 5, 9, 11, 15, 18, 20, 25]


def low_mcs_for_snr(conn, now=None, hours=6):
    """RF stations stuck at a low MCS (0-3 per stream) over `hours` although
    their SNR (25 dB or more, 15 dB above what that MCS needs) supports far
    more: a standing interference or hidden-node problem, as on COL <-> MMC.
    [{node, peer, mcs, snr, supports}]."""
    now = now or datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    mcs = {}
    for r in conn.execute('''SELECT node_name, peer, tx_mcs, tx_rate FROM prom_station_samples
                             WHERE timestamp >= ? AND peer IS NOT NULL AND tx_mcs IS NOT NULL''', (since,)):
        mcs.setdefault((r['node_name'], r['peer']), []).append((r['tx_mcs'], r['tx_rate']))
    snr = {}
    for r in conn.execute('''SELECT node_name, peer, snr FROM prom_lqm_samples
                             WHERE timestamp >= ? AND snr IS NOT NULL''', (since,)):
        snr.setdefault((r['node_name'], r['peer']), []).append(r['snr'])
    out = []
    for key, vals in mcs.items():
        if len(vals) < 3 or key not in snr:
            continue
        m = median(v[0] for v in vals)
        s = median(snr[key])
        per_stream = int(m) % 8
        supports = max(i for i, need in enumerate(MCS_SNR) if s >= need) if s >= MCS_SNR[0] else 0
        # Only the plain case: a low modulation (MCS 0-3 per stream) with SNR to
        # spare; above that, rates short of the table are normal on real paths.
        if per_stream <= 3 and s >= max(25, MCS_SNR[per_stream] + 15) and supports - per_stream >= 3:
            rates = [v[1] for v in vals if v[1]]
            out.append({'node': key[0], 'peer': key[1], 'mcs': int(m), 'snr': round(s),
                        'supports': supports, 'rate_mbps': round(median(rates) / 1e6, 1) if rates else None})
    return out

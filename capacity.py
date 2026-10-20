"""
Capacity view + scheduled iPerf (Reports tab "Capacity").

Every iPerf result (the 5-minute queue, manual tests from RF Stats, and the
overnight schedule) is logged in SQLite `iperf_results`, failures included.
Results stored before this table existed are imported once from
`link_history.throughput_tx`.

Scheduled iPerf: from IPERF_SCHEDULE_START, IPERF_SCHEDULE_ROUNDS passes over
the key links, both directions, one test at a time (sharing rf_stats' iPerf
lock with the queue), no new test after IPERF_SCHEDULE_END. Key links = RF
links with an infrastructure end (re-synced each run) plus operator-added
pairs in `iperf_schedule`; links in a maintenance window are skipped. Gated
by gateway.py: a run stops when the starting node is unreachable.

Capacity per key link direction:
  - capacity: median of scheduled (overnight, quiet) runs over the last
    7 nights, else the 90th percentile of all runs in the window;
  - PHY rate (median tx bitrate from LQM) and efficiency (median of each
    run's iPerf / the PHY rate recorded with it);
  - traffic: median and 90th percentile of tx packets/s between scans over
    the last TRAFFIC_HOURS (LQM counters) x FRAME_BYTES, skipping intervals
    with an iPerf test; spare = capacity - median traffic (AREDN reports no
    channel busy, so this is an estimate);
  - nightly medians for a trend, flagged when last night is
    CAPACITY_DROP_PCT below the usual.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import config
import database
import gateway
import maintenance
import path_view

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
FRAME_BYTES = 600          # same assumption as airtime.py
TRAFFIC_HOURS = 6          # window for the traffic estimate
_tables_ready = False
_schedule_lock = threading.Lock()
schedule_state = {'running': False, 'started': None, 'done': 0, 'planned': 0, 'last_run': None}


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS iperf_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'queue',
                ok INTEGER NOT NULL,
                mbps REAL,
                error TEXT,
                quality INTEGER,
                snr INTEGER,
                tx_bitrate REAL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_iperf_results_pair ON iperf_results(source, target, timestamp)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_iperf_results_ts ON iperf_results(timestamp)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS iperf_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'manual',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                UNIQUE(source, target)
            )
        ''')
        conn.execute('CREATE TABLE IF NOT EXISTS capacity_meta (key TEXT PRIMARY KEY, value TEXT)')
        done = conn.execute("SELECT value FROM capacity_meta WHERE key = 'backfilled'").fetchone()
        if not done:
            # One-time import of iPerf results stored only in link_history so far.
            conn.execute('''
                INSERT INTO iperf_results (timestamp, source, target, kind, ok, mbps, quality, snr, tx_bitrate)
                SELECT timestamp, source_node, target_node, 'queue', 1, throughput_tx, quality, snr, tx_bitrate
                FROM link_history WHERE throughput_tx IS NOT NULL''')
            conn.execute("INSERT INTO capacity_meta (key, value) VALUES ('backfilled', ?)",
                         (datetime.now().strftime(TS_FMT),))
    _tables_ready = True


def _now():
    return datetime.now()


def record(source, target, result, kind='queue', error=None):
    """Log one iPerf attempt (result None = failed)."""
    try:
        ensure_tables()
        with database.get_connection() as conn:
            link = conn.execute('SELECT quality, snr FROM links WHERE source_node = ? AND target_node = ?',
                                (source, target)).fetchone()
            rate = conn.execute('''SELECT tx_bitrate FROM link_history WHERE source_node = ? AND target_node = ?
                                   AND tx_bitrate IS NOT NULL ORDER BY id DESC LIMIT 1''',
                                (source, target)).fetchone()
            conn.execute('''INSERT INTO iperf_results (timestamp, source, target, kind, ok, mbps, error,
                                                       quality, snr, tx_bitrate)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (_now().strftime(TS_FMT), source, target, kind, 1 if result else 0,
                          (result or {}).get('tx_mbps'), None if result else (error or 'failed'),
                          link['quality'] if link else None, link['snr'] if link else None,
                          rate['tx_bitrate'] if rate else None))
    except Exception as exc:  # never break an iPerf caller over bookkeeping
        logger.error('capacity.record failed: %s', exc)


def cleanup(days=None):
    ensure_tables()
    days = days or max(1, config.HISTORY_RETENTION_HOURS // 24)
    cutoff = (_now() - timedelta(days=days)).strftime(TS_FMT)
    with database.get_connection() as conn:
        conn.execute('DELETE FROM iperf_results WHERE timestamp < ?', (cutoff,))


# ------------------------------------------------------------ key links --

def key_links():
    """[(source, target, kind)] both directions of every key pair: RF links
    with an infrastructure end plus enabled operator pairs."""
    ensure_tables()
    infra = database.get_infrastructure_nodes()
    out, seen = [], set()
    with database.get_connection() as conn:
        rows = path_view._links(conn)
        disabled = {(r['source'], r['target']) for r in conn.execute(
            'SELECT source, target FROM iperf_schedule WHERE enabled = 0')}
        manual = [(r['source'], r['target']) for r in conn.execute(
            'SELECT source, target FROM iperf_schedule WHERE enabled = 1 ORDER BY id')]
    for (a, b), r in sorted(rows.items()):
        if (r['link_type'] or '').upper() != 'RF' or not r['usable'] or b.startswith('mac-'):
            continue
        if a not in infra and b not in infra:
            continue
        for key in ((a, b), (b, a)):
            if key not in seen and key not in disabled:
                seen.add(key)
                out.append((key[0], key[1], 'auto'))
    for a, b in manual:
        for key in ((a, b), (b, a)):
            if key not in seen and key not in disabled:
                seen.add(key)
                out.append((key[0], key[1], 'manual'))
    return out


def add_link(source, target):
    source, target = (source or '').strip().lower(), (target or '').strip().lower()
    if not source or not target or source == target:
        raise ValueError('source and target are required')
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO iperf_schedule (source, target, kind, enabled, created_at)
                        VALUES (?, ?, 'manual', 1, ?)
                        ON CONFLICT(source, target) DO UPDATE SET enabled = 1''',
                     (source, target, _now().strftime(TS_FMT)))


def set_enabled(source, target, enabled):
    """Turn a key pair (both directions) on or off for the schedule."""
    ensure_tables()
    with database.get_connection() as conn:
        for a, b in ((source, target), (target, source)):
            conn.execute('''INSERT INTO iperf_schedule (source, target, kind, enabled, created_at)
                            VALUES (?, ?, 'override', ?, ?)
                            ON CONFLICT(source, target) DO UPDATE SET enabled = excluded.enabled''',
                         (a, b, 1 if enabled else 0, _now().strftime(TS_FMT)))


# ------------------------------------------------------------- schedule --

def _hhmm(value, default):
    try:
        h, m = (int(x) for x in str(value).split(':')[:2])
        return h, m
    except (TypeError, ValueError):
        return default


def window_end(now=None):
    """Datetime after which no scheduled test starts (today's or tomorrow's)."""
    now = now or _now()
    sh, sm = _hhmm(config.IPERF_SCHEDULE_START, (2, 0))
    eh, em = _hhmm(config.IPERF_SCHEDULE_END, (4, 30))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    end = start.replace(hour=eh, minute=em)
    if end <= start:
        end += timedelta(days=1)
    return end


def run_schedule(test_fn=None, deadline=None, rounds=None, gap=None, sleep=time.sleep):
    """One night's scheduled run. Returns a summary dict."""
    import rf_stats  # local import: rf_stats imports capacity for record()
    ensure_tables()
    if not _schedule_lock.acquire(blocking=False):
        return {'aborted': 'already running'}
    rounds = rounds or max(1, config.IPERF_SCHEDULE_ROUNDS)
    gap = config.IPERF_SCHEDULE_GAP_SECONDS if gap is None else gap
    deadline = deadline or window_end()
    summary = {'started': _now().strftime(TS_FMT), 'tests': 0, 'ok': 0, 'failed': 0, 'skipped': [],
               'aborted': None}
    try:
        links = key_links()
        schedule_state.update(running=True, started=summary['started'], done=0, planned=len(links) * rounds)
        test = test_fn or (lambda a, b: rf_stats.run_iperf_test(
            (database.get_node(b) or {}).get('ip') or f'{b}.local.mesh',
            duration=config.IPERF_DURATION, bandwidth_limit=config.IPERF_BANDWIDTH,
            source_node_ip=(database.get_node(a) or {}).get('ip'), source_node_name=a, target_node_name=b))
        for rnd in range(rounds):
            for a, b, _kind in links:
                if _now() >= deadline:
                    summary['aborted'] = 'schedule window ended'
                    return summary
                if not gateway.is_reachable():
                    summary['aborted'] = gateway.unreachable_message()
                    return summary
                if maintenance.is_in_maintenance(a, peer=b):
                    if rnd == 0:
                        summary['skipped'].append(f'{a} → {b} (maintenance)')
                    continue
                # Share the queue's lock so only one iPerf runs at a time.
                for _ in range(120):
                    with rf_stats.iperf_lock:
                        if not rf_stats.iperf_running:
                            rf_stats.iperf_running = True
                            break
                    sleep(1)
                else:
                    summary['skipped'].append(f'{a} → {b} (iPerf busy)')
                    continue
                try:
                    result = test(a, b)
                finally:
                    with rf_stats.iperf_lock:
                        rf_stats.iperf_running = False
                if not result and gateway.failure_is_ours():
                    summary['aborted'] = gateway.unreachable_message()
                    return summary
                record(a, b, result, kind='scheduled')
                if result:
                    database.update_link_history_throughput(a, b, result.get('tx_mbps'), result.get('rx_mbps'))
                summary['tests'] += 1
                summary['ok' if result else 'failed'] += 1
                schedule_state['done'] += 1
                if gap:
                    sleep(gap)
        return summary
    finally:
        summary['finished'] = _now().strftime(TS_FMT)
        schedule_state.update(running=False, last_run=summary)
        _schedule_lock.release()


def install(scheduler, job):
    """Add the nightly cron job (job = app-level wrapper calling run_schedule)."""
    if not config.IPERF_SCHEDULE_ENABLED or not config.RF_STATS_ENABLED:
        return
    h, m = _hhmm(config.IPERF_SCHEDULE_START, (2, 0))
    scheduler.add_job(job, 'cron', hour=h, minute=m, id='iperf_schedule', replace_existing=True)
    logger.info('Scheduled iPerf nightly at %02d:%02d on key links', h, m)


# --------------------------------------------------------------- report --

def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _night(ts):
    """The night a timestamp belongs to (runs after midnight count for the
    evening before), as YYYY-MM-DD."""
    dt = datetime.strptime(ts[:19], TS_FMT)
    return (dt - timedelta(hours=12)).strftime('%Y-%m-%d')


def _traffic(conn, a, b, since, tests):
    """(median, 90th percentile) Mbps sent a -> b between consecutive scans
    since `since`, from LQM tx packet counters x FRAME_BYTES. Intervals with
    an iPerf test on the pair (its own traffic) or longer than 30 minutes
    (scanner gaps) are skipped. None when there is nothing to go on."""
    rows = [(datetime.strptime(r['timestamp'][:19], TS_FMT), r['tx_packets']) for r in conn.execute('''
        SELECT timestamp, tx_packets FROM link_history
        WHERE source_node = ? AND target_node = ? AND timestamp >= ? AND tx_packets IS NOT NULL
          AND COALESCE(sample_type, 'scan') = 'scan'
        ORDER BY timestamp''', (a, b, since))]
    rates = []
    for (t0, p0), (t1, p1) in zip(rows, rows[1:]):
        secs = (t1 - t0).total_seconds()
        if secs <= 0 or secs > 1800 or p1 < p0:
            continue
        if any(t0 - timedelta(seconds=60) <= t <= t1 for t in tests):
            continue
        rates.append((p1 - p0) / secs * FRAME_BYTES * 8 / 1e6)
    if not rates:
        return None, None
    return round(median(rates), 3), round(_pct(rates, 90), 3)


def report(hours=168):
    ensure_tables()
    now = _now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    week = (now - timedelta(days=7)).strftime(TS_FMT)
    traffic_since = (now - timedelta(hours=TRAFFIC_HOURS)).strftime(TS_FMT)
    infra = database.get_infrastructure_nodes()
    keys = key_links()
    key_set = {(a, b) for a, b, _ in keys}
    results = defaultdict(list)
    with database.get_connection() as conn:
        for r in conn.execute('SELECT * FROM iperf_results WHERE timestamp >= ? ORDER BY timestamp',
                              (min(since, week),)):
            results[(r['source'], r['target'])].append(dict(r))
        rows_out = []
        pairs = set(key_set) | {k for k, v in results.items() if any(x['timestamp'] >= since for x in v)}
        for a, b in pairs:
            runs = results.get((a, b), [])
            in_win = [x for x in runs if x['timestamp'] >= since]
            ok = [x for x in in_win if x['ok'] and x['mbps'] is not None]
            sched = [x for x in runs if x['kind'] == 'scheduled' and x['ok'] and x['mbps'] is not None
                     and x['timestamp'] >= week]
            nights = defaultdict(list)
            for x in sched:
                nights[_night(x['timestamp'])].append(x['mbps'])
            nightly = [{'night': n, 'median': round(median(v), 1), 'runs': len(v)} for n, v in sorted(nights.items())]
            if sched:
                cap, basis = round(median(x['mbps'] for x in sched), 1), 'overnight median (7 nights)'
            elif ok:
                cap, basis = round(_pct([x['mbps'] for x in ok], 90), 1), '90th percentile of all runs'
            else:
                cap, basis = None, None
            rate_rows = [x['tx_bitrate'] for x in conn.execute('''
                SELECT tx_bitrate FROM link_history WHERE source_node = ? AND target_node = ?
                  AND timestamp >= ? AND tx_bitrate IS NOT NULL''', (a, b, since))]
            phy = round(median(rate_rows), 1) if rate_rows else None
            tests = [datetime.strptime(x['timestamp'][:19], TS_FMT)
                     for x in results.get((a, b), []) + results.get((b, a), []) if x['timestamp'] >= traffic_since]
            traffic, traffic_peak = _traffic(conn, a, b, traffic_since, tests)
            eff = [x['mbps'] / x['tx_bitrate'] * 100 for x in ok if x['tx_bitrate']]
            flag = None
            if len(nightly) >= 3:
                usual = median(n['median'] for n in nightly[:-1])
                last = nightly[-1]['median']
                if usual and last < usual * (1 - config.CAPACITY_DROP_PCT / 100):
                    flag = f'last night {last} Mbps vs usual {round(usual, 1)} Mbps ({round((1 - last / usual) * 100)}% lower)'
            day = [x['mbps'] for x in ok if x['kind'] != 'scheduled'
                   and 7 <= int(x['timestamp'][11:13]) < 22]
            rows_out.append({
                'source': a, 'target': b, 'key': (a, b) in key_set,
                'backbone': a in infra and b in infra, 'infrastructure': a in infra or b in infra,
                'runs': len(in_win), 'failed': sum(1 for x in in_win if not x['ok']),
                'latest': ok[-1]['mbps'] if ok else None, 'latest_at': ok[-1]['timestamp'] if ok else None,
                'median': round(median(x['mbps'] for x in ok), 1) if ok else None,
                'daytime_median': round(median(day), 1) if day else None,
                'capacity': cap, 'capacity_basis': basis, 'phy_mbps': phy,
                'efficiency_pct': round(median(eff)) if eff else None,
                'traffic_mbps': traffic, 'traffic_peak_mbps': traffic_peak,
                'spare_mbps': round(max(0.0, cap - traffic), 1) if cap is not None and traffic is not None else None,
                'utilization_pct': round(traffic / cap * 100, 1) if cap and traffic is not None else None,
                'nightly': nightly, 'flag': flag,
            })
    rows_out.sort(key=lambda r: (not r['backbone'], not r['key'], r['source'], r['target']))
    sh, sm = _hhmm(config.IPERF_SCHEDULE_START, (2, 0))
    return {'generated_at': now.strftime(TS_FMT), 'hours': hours, 'links': rows_out,
            'schedule': {'enabled': config.IPERF_SCHEDULE_ENABLED and config.RF_STATS_ENABLED,
                         'start': f'{sh:02d}:{sm:02d}', 'end': config.IPERF_SCHEDULE_END,
                         'rounds': config.IPERF_SCHEDULE_ROUNDS, 'key_links': len(keys),
                         'state': dict(schedule_state)},
            'frame_bytes': FRAME_BYTES, 'drop_pct': config.CAPACITY_DROP_PCT}


def link_series(source, target, hours=168):
    ensure_tables()
    since = (_now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT timestamp, kind, ok, mbps, error, quality, snr, tx_bitrate FROM iperf_results
            WHERE source = ? AND target = ? AND timestamp >= ? ORDER BY timestamp''', (source, target, since))]
    return {'source': source, 'target': target, 'hours': hours, 'runs': rows}

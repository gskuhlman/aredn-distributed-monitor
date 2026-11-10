"""
History rollup (TODO #48): hourly aggregates of link_history scan samples, so
raw samples can be dropped after LINK_HISTORY_RAW_DAYS (30) while trends
(slow degradation, monthly reports) keep reaching back ROLLUP_RETENTION_DAYS.

SQLite `link_history_hourly`, one row per (hour, source, target): sample
count; median / 10th / 90th percentile SNR and LQ; medians of reverse SNR,
signal, quality, tx quality, ping quality, ping time, jitter and bitrates;
mean ping loss; share of samples routable / blocked; and the growth of the
cumulative LQM counters (tx packets, retries, failures) inside the hour
(intervals where a counter went backwards, a reboot, are skipped).

`run()` (hourly job, and before every raw cleanup) rolls up every complete
hour after the last one done (`rollup_meta`), a day at a time; the first run
backfills all raw history. `cleanup()` deletes raw link_history older than
LINK_HISTORY_RAW_DAYS, never an hour not rolled up yet, and hourly rows older
than ROLLUP_RETENTION_DAYS (0 = keep forever). Only `scan` samples are rolled
up; incident-mode samples are high-rate bursts and keep the raw retention.
"""

import logging
from datetime import datetime, timedelta

import config
import database

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
HOUR_FMT = '%Y-%m-%d %H:00:00'

MEDIANS = ('rev_snr', 'signal', 'quality', 'tx_quality', 'ping_quality', 'ping_avg', 'jitter',
           'tx_bitrate', 'rx_bitrate')
COUNTERS = ('tx_packets', 'tx_retries', 'tx_fail')
COLUMNS = (('hour', 'DATETIME NOT NULL'), ('source_node', 'TEXT NOT NULL'), ('target_node', 'TEXT NOT NULL'),
           ('link_type', 'TEXT'), ('samples', 'INTEGER'),
           ('snr_med', 'REAL'), ('snr_p10', 'REAL'), ('snr_p90', 'REAL'),
           ('lq_med', 'REAL'), ('lq_p10', 'REAL'), ('lq_p90', 'REAL')) \
    + tuple((f'{c}_med', 'REAL') for c in MEDIANS) \
    + (('ping_loss_avg', 'REAL'), ('routable_pct', 'REAL'), ('blocked_pct', 'REAL')) \
    + tuple((f'{c}_delta', 'INTEGER') for c in COUNTERS)
NAMES = tuple(c for c, _ in COLUMNS)

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute(f'''CREATE TABLE IF NOT EXISTS link_history_hourly (
            {', '.join(f'{c} {t}' for c, t in COLUMNS)},
            PRIMARY KEY (hour, source_node, target_node))''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_lhh_pair ON link_history_hourly(source_node, target_node, hour)')
        conn.execute('CREATE TABLE IF NOT EXISTS rollup_meta (key TEXT PRIMARY KEY, value TEXT)')
    _tables_ready = True


def _num(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def quantile(values, q):
    """Linear-interpolated quantile of the non-None values, else None."""
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    pos = (len(vs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (pos - lo), 2)


def _counter_delta(rows, field):
    total, seen = 0, False
    prev = None
    for r in rows:
        v = _num(r.get(field))
        if v is None:
            continue
        if prev is not None and v >= prev:
            total += v - prev
            seen = True
        prev = v
    return int(total) if seen else None


def aggregate(hour, source, target, rows):
    """One hourly row from the raw samples (dicts, chronological) of one
    direction in one hour."""
    col = lambda f: [_num(r.get(f)) for r in rows]
    out = {'hour': hour, 'source_node': source, 'target_node': target,
           'link_type': rows[-1].get('link_type'), 'samples': len(rows)}
    for f, key in (('snr', 'snr'), ('lq', 'lq')):
        vals = col(f)
        out[f'{key}_med'], out[f'{key}_p10'], out[f'{key}_p90'] = (quantile(vals, q) for q in (0.5, 0.1, 0.9))
    for f in MEDIANS:
        out[f'{f}_med'] = quantile(col(f), 0.5)
    losses = [v for v in col('ping_loss') if v is not None]
    out['ping_loss_avg'] = round(sum(losses) / len(losses), 2) if losses else None
    routable = [v for v in col('routable') if v is not None]
    out['routable_pct'] = round(100 * sum(1 for v in routable if v) / len(routable), 1) if routable else None
    out['blocked_pct'] = round(100 * sum(1 for r in rows if r.get('blocked')) / len(rows), 1)
    for f in COUNTERS:
        out[f'{f}_delta'] = _counter_delta(rows, f)
    return out


def _meta(conn, key, value=None):
    if value is None:
        r = conn.execute('SELECT value FROM rollup_meta WHERE key = ?', (key,)).fetchone()
        return r[0] if r else None
    conn.execute('INSERT OR REPLACE INTO rollup_meta (key, value) VALUES (?, ?)', (key, value))


def rollup_range(conn, start, end):
    """Roll up raw scan samples with start <= timestamp < end (whole hours).
    Returns the number of hourly rows written."""
    rows = conn.execute('''SELECT * FROM link_history WHERE sample_type = 'scan' AND timestamp >= ? AND timestamp < ?
                           ORDER BY source_node, target_node, timestamp''', (start, end)).fetchall()
    groups = {}
    for r in rows:
        d = dict(r)
        groups.setdefault((d['timestamp'][:13] + ':00:00', d['source_node'], d['target_node']), []).append(d)
    out = [aggregate(h, s, t, rs) for (h, s, t), rs in groups.items()]
    if out:
        conn.executemany(f"INSERT OR REPLACE INTO link_history_hourly ({', '.join(NAMES)}) "
                         f"VALUES ({', '.join('?' * len(NAMES))})", [[o.get(c) for c in NAMES] for o in out])
    return len(out)


def run(now=None):
    """Roll up every complete hour not done yet. Returns {hours, rows}."""
    ensure_tables()
    now = now or datetime.now()
    upto = now.replace(minute=0, second=0, microsecond=0)
    written = 0
    with database.get_connection() as conn:
        done = _meta(conn, 'rolled_until')
        if done:
            start = datetime.strptime(done, TS_FMT)
        else:
            first = conn.execute("SELECT MIN(timestamp) FROM link_history WHERE sample_type = 'scan'").fetchone()[0]
            if not first:
                _meta(conn, 'rolled_until', upto.strftime(TS_FMT))
                return {'hours': 0, 'rows': 0}
            start = datetime.strptime(first[:13], '%Y-%m-%d %H')
    hours = max(0, int((upto - start).total_seconds() // 3600))
    t = start
    while t < upto:
        step = min(upto, t + timedelta(days=1))
        with database.get_connection() as conn:
            written += rollup_range(conn, t.strftime(TS_FMT), step.strftime(TS_FMT))
            _meta(conn, 'rolled_until', step.strftime(TS_FMT))
        t = step
    if written:
        logger.info('Rolled up %d hour(s) of link history into %d hourly rows', hours, written)
    return {'hours': hours, 'rows': written}


def cleanup(now=None):
    """Delete raw link_history past LINK_HISTORY_RAW_DAYS (only hours already
    rolled up) and hourly rows past ROLLUP_RETENTION_DAYS. Returns counts."""
    ensure_tables()
    now = now or datetime.now()
    run(now)
    cut = now - timedelta(days=config.LINK_HISTORY_RAW_DAYS)
    with database.get_connection() as conn:
        done = _meta(conn, 'rolled_until')
        if done:
            cut = min(cut, datetime.strptime(done, TS_FMT))
        raw = conn.execute('DELETE FROM link_history WHERE timestamp < ?', (cut.strftime(TS_FMT),)).rowcount
        hourly = 0
        if config.ROLLUP_RETENTION_DAYS > 0:
            hcut = (now - timedelta(days=config.ROLLUP_RETENTION_DAYS)).strftime(TS_FMT)
            hourly = conn.execute('DELETE FROM link_history_hourly WHERE hour < ?', (hcut,)).rowcount
    return {'raw_deleted': raw, 'hourly_deleted': hourly}


def hourly(source, target, start, end=None):
    """Hourly rows of one direction, oldest first."""
    ensure_tables()
    end = end or datetime.now().strftime(TS_FMT)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM link_history_hourly WHERE source_node = ? AND target_node = ? AND hour >= ? AND hour < ? '
            'ORDER BY hour', (source, target, start, end))]


def all_hourly(start, end=None, link_type='RF'):
    """{(source, target): [hourly rows]} for every direction in the range."""
    ensure_tables()
    end = end or datetime.now().strftime(TS_FMT)
    out = {}
    with database.get_connection() as conn:
        for r in conn.execute('SELECT * FROM link_history_hourly WHERE hour >= ? AND hour < ? AND link_type = ? '
                              'ORDER BY source_node, target_node, hour', (start, end, link_type)):
            d = dict(r)
            out.setdefault((d['source_node'], d['target_node']), []).append(d)
    return out


def status():
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT COUNT(*), MIN(hour), MAX(hour) FROM link_history_hourly').fetchone()
        return {'rows': r[0], 'oldest': r[1], 'newest': r[2], 'rolled_until': _meta(conn, 'rolled_until'),
                'raw_days': config.LINK_HISTORY_RAW_DAYS, 'retention_days': config.ROLLUP_RETENTION_DAYS}


def install(scheduler):
    scheduler.add_job(lambda: run(), 'interval', minutes=60, id='history_rollup', replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=3))

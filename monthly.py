"""
Long-term monthly rollups, kept forever (TODO #83; Reports page "Long-term").

Raw history is kept about a month (node_health_history, link_history) and the
hourly/daily rollups about 400 days, so month-by-month trends need their own
rows. Three monthly tables, never pruned:

  monthly_nodes     (month, node)            up / down / unchecked minutes, reboots, uptime %
  monthly_links     (month, source, target)  hours with samples, SNR median and low hours
                                             (10th percentile of the hourly medians), LQ median,
                                             mean ping loss; from rollup.link_history_hourly
  monthly_services  (month, node, service)   checks, answered, mean response; from
                                             service_check.service_check_daily

Node uptime goes through a daily table first (`node_uptime_daily`, kept
DAILY_RETENTION_DAYS) because raw polls only reach back ~31 days: every day is
judged by `uptime.node_uptime` the day after, and a month is the sum of its days.

`run()` (daily job, and a few minutes after start) fills every complete day
and month not done yet (`monthly_meta`); the first run backfills what the raw
and rollup tables still hold. `report(months)` returns the stored months plus
the current month so far (computed on the fly, `partial`). Read-only otherwise.
"""

import logging
from datetime import datetime, timedelta
from statistics import median

import database
import rollup
import service_check
import uptime

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
DAILY_RETENTION_DAYS = 400

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS node_uptime_daily (
            day TEXT NOT NULL, node_name TEXT NOT NULL,
            up_minutes REAL, down_minutes REAL, unchecked_minutes REAL, reboots INTEGER,
            PRIMARY KEY (day, node_name))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS monthly_nodes (
            month TEXT NOT NULL, node_name TEXT NOT NULL,
            up_minutes REAL, down_minutes REAL, unchecked_minutes REAL, reboots INTEGER,
            uptime_pct REAL, days INTEGER,
            PRIMARY KEY (month, node_name))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS monthly_links (
            month TEXT NOT NULL, source_node TEXT NOT NULL, target_node TEXT NOT NULL,
            link_type TEXT, hours INTEGER, samples INTEGER,
            snr_med REAL, snr_low REAL, lq_med REAL, ping_loss_avg REAL,
            PRIMARY KEY (month, source_node, target_node))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS monthly_services (
            month TEXT NOT NULL, node_name TEXT NOT NULL, service_name TEXT NOT NULL,
            checks INTEGER, oks INTEGER, ms_avg REAL,
            PRIMARY KEY (month, node_name, service_name))''')
        conn.execute('CREATE TABLE IF NOT EXISTS monthly_meta (key TEXT PRIMARY KEY, value TEXT)')
    _tables_ready = True


def _meta(conn, key, value=None):
    if value is None:
        r = conn.execute('SELECT value FROM monthly_meta WHERE key = ?', (key,)).fetchone()
        return r[0] if r else None
    conn.execute('INSERT OR REPLACE INTO monthly_meta (key, value) VALUES (?, ?)', (key, value))


def month_start(dt):
    return datetime(dt.year, dt.month, 1)


def next_month(dt):
    return datetime(dt.year + (dt.month == 12), dt.month % 12 + 1, 1)


def _q(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 1)


# ---------------------------------------------------------------- daily ---

def run_daily(now=None):
    """Judge every complete day not done yet. Returns days done."""
    ensure_tables()
    now = now or datetime.now()
    today = datetime(now.year, now.month, now.day)
    with database.get_connection() as conn:
        last = _meta(conn, 'daily_until')
        if last:
            day = datetime.strptime(last, '%Y-%m-%d')
        else:
            first = conn.execute('SELECT MIN(timestamp) FROM node_health_history').fetchone()[0]
            if not first:
                return 0
            f = datetime.strptime(first[:10], '%Y-%m-%d')
            day = f + timedelta(days=1)        # the first day is usually partial
        done = 0
        while day < today:
            nxt = day + timedelta(days=1)
            rows = uptime.node_uptime(day.strftime(TS_FMT), nxt.strftime(TS_FMT), conn=conn)
            conn.execute('DELETE FROM node_uptime_daily WHERE day = ?', (day.strftime('%Y-%m-%d'),))
            conn.executemany('''INSERT INTO node_uptime_daily (day, node_name, up_minutes, down_minutes,
                                    unchecked_minutes, reboots) VALUES (?, ?, ?, ?, ?, ?)''',
                             [(day.strftime('%Y-%m-%d'), n, r['up_minutes'], r['down_minutes'],
                               r['unchecked_minutes'], r['reboots']) for n, r in rows.items()])
            _meta(conn, 'daily_until', nxt.strftime('%Y-%m-%d'))
            day, done = nxt, done + 1
        conn.execute('DELETE FROM node_uptime_daily WHERE day < ?',
                     ((today - timedelta(days=DAILY_RETENTION_DAYS)).strftime('%Y-%m-%d'),))
    return done


# -------------------------------------------------------------- monthly ---

def _month_nodes(conn, start, end):
    rows = conn.execute('''SELECT node_name, SUM(up_minutes), SUM(down_minutes), SUM(unchecked_minutes),
                                  SUM(reboots), COUNT(*)
                           FROM node_uptime_daily WHERE day >= ? AND day < ? GROUP BY node_name''',
                        (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))).fetchall()
    out = []
    for n, up, down, unchecked, reboots, days in rows:
        judged = (up or 0) + (down or 0)
        out.append({'node': n, 'up_minutes': round(up or 0, 1), 'down_minutes': round(down or 0, 1),
                    'unchecked_minutes': round(unchecked or 0, 1), 'reboots': reboots or 0,
                    'uptime_pct': round(100.0 * up / judged, 2) if judged else None, 'days': days})
    return out


def _month_links(conn, start, end):
    rollup.ensure_tables()
    rows = conn.execute('''SELECT source_node, target_node, link_type, samples, snr_med, lq_med, ping_loss_avg
                           FROM link_history_hourly WHERE hour >= ? AND hour < ?''',
                        (start.strftime(TS_FMT), end.strftime(TS_FMT))).fetchall()
    by = {}
    for r in rows:
        by.setdefault((r[0], r[1]), []).append(r)
    out = []
    for (a, b), rs in by.items():
        snr = [r[4] for r in rs]
        loss = [r[6] for r in rs if r[6] is not None]
        out.append({'source': a, 'target': b, 'link_type': rs[-1][2], 'hours': len(rs),
                    'samples': sum(r[3] or 0 for r in rs),
                    'snr_med': _q(snr, 0.5), 'snr_low': _q(snr, 0.1), 'lq_med': _q([r[5] for r in rs], 0.5),
                    'ping_loss_avg': round(sum(loss) / len(loss), 2) if loss else None})
    return out


def _month_services(conn, start, end):
    service_check.ensure_tables()
    rows = conn.execute('''SELECT node_name, service_name, SUM(checks), SUM(oks), SUM(ms_total)
                           FROM service_check_daily WHERE day >= ? AND day < ?
                           GROUP BY node_name, service_name''',
                        (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))).fetchall()
    return [{'node': n, 'service': s, 'checks': c or 0, 'oks': o or 0,
             'ms_avg': round(ms / o, 1) if o else None} for n, s, c, o, ms in rows]


def _first_month(conn):
    firsts = [conn.execute(q).fetchone()[0] for q in (
        'SELECT MIN(day) FROM node_uptime_daily',
        'SELECT MIN(hour) FROM link_history_hourly',
        'SELECT MIN(day) FROM service_check_daily')]
    firsts = [f for f in firsts if f]
    return month_start(datetime.strptime(min(firsts)[:10], '%Y-%m-%d')) if firsts else None


def run_monthly(now=None):
    """Store every complete month not stored yet. Returns months stored."""
    ensure_tables()
    rollup.ensure_tables()
    service_check.ensure_tables()
    now = now or datetime.now()
    this_month = month_start(now)
    with database.get_connection() as conn:
        last = _meta(conn, 'monthly_until')
        m = datetime.strptime(last, '%Y-%m') if last else _first_month(conn)
        done = 0
        while m and m < this_month:
            end = next_month(m)
            key = m.strftime('%Y-%m')
            for table in ('monthly_nodes', 'monthly_links', 'monthly_services'):
                conn.execute(f'DELETE FROM {table} WHERE month = ?', (key,))
            conn.executemany('''INSERT INTO monthly_nodes (month, node_name, up_minutes, down_minutes,
                                    unchecked_minutes, reboots, uptime_pct, days) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                             [(key, r['node'], r['up_minutes'], r['down_minutes'], r['unchecked_minutes'],
                               r['reboots'], r['uptime_pct'], r['days']) for r in _month_nodes(conn, m, end)])
            conn.executemany('''INSERT INTO monthly_links (month, source_node, target_node, link_type, hours,
                                    samples, snr_med, snr_low, lq_med, ping_loss_avg)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             [(key, r['source'], r['target'], r['link_type'], r['hours'], r['samples'],
                               r['snr_med'], r['snr_low'], r['lq_med'], r['ping_loss_avg'])
                              for r in _month_links(conn, m, end)])
            conn.executemany('''INSERT INTO monthly_services (month, node_name, service_name, checks, oks, ms_avg)
                                VALUES (?, ?, ?, ?, ?, ?)''',
                             [(key, r['node'], r['service'], r['checks'], r['oks'], r['ms_avg'])
                              for r in _month_services(conn, m, end)])
            _meta(conn, 'monthly_until', end.strftime('%Y-%m'))
            m, done = end, done + 1
    return done


def run(now=None):
    try:
        days = run_daily(now)
        months = run_monthly(now)
        if days or months:
            logger.info("Monthly rollup: %d day(s), %d month(s)", days, months)
        return {'days': days, 'months': months}
    except Exception as exc:
        logger.error("Monthly rollup failed: %s", exc)
        return {'error': str(exc)}


def install(scheduler):
    scheduler.add_job(lambda: run(), 'cron', hour=0, minute=25, id='monthly_rollup', replace_existing=True)
    scheduler.add_job(lambda: run(), 'date', run_date=datetime.now() + timedelta(minutes=6),
                      id='monthly_rollup_start', replace_existing=True)


# --------------------------------------------------------------- report ---

def _summary(month, nodes, links, services, partial=False):
    up = [n['uptime_pct'] for n in nodes if n['uptime_pct'] is not None]
    rf = [l for l in links if (l['link_type'] or '').upper() == 'RF' and l['snr_med'] is not None]
    checks = sum(s['checks'] for s in services)
    oks = sum(s['oks'] for s in services)
    return {'month': month, 'partial': partial, 'nodes': len(nodes),
            'uptime_avg': round(sum(up) / len(up), 2) if up else None,
            'uptime_median': round(median(up), 2) if up else None,
            'nodes_below_95': sum(1 for v in up if v < 95),
            'reboots': sum(n['reboots'] or 0 for n in nodes),
            'rf_links': len(rf), 'rf_snr_median': _q([l['snr_med'] for l in rf], 0.5),
            'services': len(services), 'service_checks': checks,
            'service_availability': round(100.0 * oks / checks, 2) if checks else None}


def report(months=12, now=None):
    """Mesh summary per month plus per-node uptime and per-link SNR grids,
    oldest month first; the current month so far is last (partial)."""
    ensure_tables()
    rollup.ensure_tables()
    service_check.ensure_tables()
    now = now or datetime.now()
    months = max(1, min(int(months or 12), 120))
    with database.get_connection() as conn:
        keys = [r[0] for r in conn.execute(
            'SELECT DISTINCT month FROM monthly_nodes UNION SELECT DISTINCT month FROM monthly_links '
            'UNION SELECT DISTINCT month FROM monthly_services ORDER BY 1 DESC LIMIT ?', (months - 1,))][::-1]
        stored = {}
        for k in keys:
            nodes = [dict(node=r[0], uptime_pct=r[1], reboots=r[2], days=r[3]) for r in conn.execute(
                'SELECT node_name, uptime_pct, reboots, days FROM monthly_nodes WHERE month = ?', (k,))]
            links = [dict(source=r[0], target=r[1], link_type=r[2], snr_med=r[3], snr_low=r[4], hours=r[5])
                     for r in conn.execute('''SELECT source_node, target_node, link_type, snr_med, snr_low, hours
                                              FROM monthly_links WHERE month = ?''', (k,))]
            services = [dict(node=r[0], service=r[1], checks=r[2], oks=r[3]) for r in conn.execute(
                'SELECT node_name, service_name, checks, oks FROM monthly_services WHERE month = ?', (k,))]
            stored[k] = (nodes, links, services)
        m, end = month_start(now), next_month(now)
        cur_key = m.strftime('%Y-%m')
        cur = (_month_nodes(conn, m, end), _month_links(conn, m, end), _month_services(conn, m, end))
    all_keys = keys + [cur_key]
    data = dict(stored, **{cur_key: cur})
    summary = [_summary(k, *data[k], partial=(k == cur_key)) for k in all_keys]
    node_grid, link_grid = {}, {}
    for k in all_keys:
        nodes, links, _ = data[k]
        for n in nodes:
            node_grid.setdefault(n['node'], {})[k] = n['uptime_pct']
        for l in links:
            if (l['link_type'] or '').upper() != 'RF':
                continue
            a, b = sorted((l['source'], l['target']))
            cell = link_grid.setdefault(f'{a} ↔ {b}', {})
            # Both directions: keep the weaker median for the pair.
            if l['snr_med'] is not None and (cell.get(k) is None or l['snr_med'] < cell[k]):
                cell[k] = l['snr_med']
    with database.get_connection() as conn:
        meta = {'daily_until': _meta(conn, 'daily_until'), 'monthly_until': _meta(conn, 'monthly_until')}
    return {'months': all_keys, 'summary': summary, 'nodes': node_grid, 'links': link_grid, 'meta': meta}

"""
Slow degradation watch (TODO #53). Per RF link direction, the trend of daily
median SNR and LQ over the last DEGRADE_DAYS (hourly rollups, rollup.py), so
a link slipping a few dB a month (antenna moving, corrosion, foliage, a new
neighbour) shows up before it fails.

Per direction: daily medians, a robust slope (Theil-Sen: median of the
pairwise slopes, per 30 days), how well a straight line fits (r²), the drop
between the first and last week, and the gap to the free-space link budget
(link_potential.report, no terrain). A channel / config / antenna change in
the window restarts the trend after the change (`since_change`). Flagged
`degrading` when SNR falls DEGRADE_DB_PER_MONTH+ dB per month with r² >=
DEGRADE_MIN_R2 and the weekly drop confirms it, or LQ falls
DEGRADE_LQ_PER_MONTH+ points; needs DEGRADE_MIN_DAYS days of data.

`watch()` (daily job) logs `link_degrading` (warning, `RF link to <peer>:
...`, node = the receiving end) for newly flagged directions and
`link_degrading_cleared` when one stops; state in SQLite `degradation_state`.
Read-only otherwise.
"""

import logging
from datetime import datetime, timedelta
from statistics import median

import config
import database
import rollup

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
CHANGE_EVENTS = ('frequency_change', 'radio_config_change', 'firmware_change')

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS degradation_state (
            source_node TEXT NOT NULL, target_node TEXT NOT NULL, flagged INTEGER NOT NULL DEFAULT 0,
            since DATETIME, detail TEXT, updated_at DATETIME,
            PRIMARY KEY (source_node, target_node))''')
    _tables_ready = True


def theil_sen(xs, ys):
    """(slope, intercept) robust to outliers; None with < 2 distinct x."""
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    slopes = [(b[1] - a[1]) / (b[0] - a[0]) for i, a in enumerate(pts) for b in pts[i + 1:] if b[0] != a[0]]
    if not slopes:
        return None
    m = median(slopes)
    return m, median(y - m * x for x, y in pts)


def r_squared(xs, ys, slope, intercept):
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pts) < 3:
        return None
    mean = sum(y for _, y in pts) / len(pts)
    tot = sum((y - mean) ** 2 for _, y in pts)
    res = sum((y - (slope * x + intercept)) ** 2 for x, y in pts)
    return round(max(0.0, 1 - res / tot), 2) if tot > 0 else 0.0


def daily(rows, field):
    """[(day index, day, median)] of an hourly field, oldest first."""
    days = {}
    for r in rows:
        v = r.get(field)
        if v is not None:
            days.setdefault(r['hour'][:10], []).append(v)
    if not days:
        return []
    first = datetime.strptime(min(days), '%Y-%m-%d')
    return [((datetime.strptime(d, '%Y-%m-%d') - first).days, d, round(median(v), 1)) for d, v in sorted(days.items())]


def trend(points):
    """{slope_month, r2, first_week, last_week, drop, days} of daily points."""
    if len(points) < 2:
        return None
    xs, ys = [p[0] for p in points], [p[2] for p in points]
    fit = theil_sen(xs, ys)
    if not fit:
        return None
    slope, icpt = fit
    last_x = xs[-1]
    first_week = [y for x, y in zip(xs, ys) if x <= xs[0] + 6]
    last_week = [y for x, y in zip(xs, ys) if x >= last_x - 6]
    fw, lw = median(first_week), median(last_week)
    return {'slope_month': round(slope * 30, 1), 'r2': r_squared(xs, ys, slope, icpt),
            'first_week': round(fw, 1), 'last_week': round(lw, 1), 'drop': round(fw - lw, 1),
            'days': len(points), 'span_days': last_x - xs[0] + 1}


def _changes(conn, since):
    """{node: newest change time} from change-log entries and change events."""
    out = {}
    for r in conn.execute(f'''SELECT node_name, MAX(timestamp) FROM events WHERE timestamp >= ?
                              AND event_type IN ({', '.join('?' * len(CHANGE_EVENTS))}) GROUP BY node_name''',
                          (since, *CHANGE_EVENTS)):
        out[r[0]] = r[1]
    try:
        for r in conn.execute('''SELECT node_name, MAX(timestamp) FROM change_log WHERE timestamp >= ?
                                 AND category IN ('channel', 'antenna', 'power') GROUP BY node_name''', (since,)):
            out[r[0]] = max(out.get(r[0]) or '', r[1])
    except Exception:
        pass
    return out


def judge(snr_t, lq_t):
    """Reasons a direction is degrading (empty = fine)."""
    out = []
    if snr_t and snr_t['days'] >= config.DEGRADE_MIN_DAYS and snr_t['slope_month'] <= -config.DEGRADE_DB_PER_MONTH \
            and (snr_t['r2'] or 0) >= config.DEGRADE_MIN_R2 and snr_t['drop'] >= config.DEGRADE_DB_PER_MONTH / 2:
        out.append(f"SNR falling {-snr_t['slope_month']:g} dB a month ({snr_t['first_week']:g} -> "
                   f"{snr_t['last_week']:g} dB over {snr_t['span_days']} days, fit r² {snr_t['r2']:g})")
    if lq_t and lq_t['days'] >= config.DEGRADE_MIN_DAYS and lq_t['slope_month'] <= -config.DEGRADE_LQ_PER_MONTH \
            and lq_t['drop'] >= config.DEGRADE_LQ_PER_MONTH / 2:
        out.append(f"LQ falling {-lq_t['slope_month']:g} points a month ({lq_t['first_week']:g} -> "
                   f"{lq_t['last_week']:g}%)")
    return out


def report(days=None, now=None, with_budget=True):
    days = days or config.DEGRADE_DAYS
    now = now or datetime.now()
    start = (now - timedelta(days=days)).strftime(TS_FMT)
    data = rollup.all_hourly(start, now.strftime(TS_FMT))
    infra = database.get_infrastructure_nodes()
    budget = {}
    if with_budget:
        try:
            import link_potential
            for p in link_potential.report(hours=24)['points']:
                budget[(p['to'], p['from'])] = p.get('predicted_snr')
        except Exception as exc:
            logger.debug('link budget for degradation failed: %s', exc)
    with database.get_connection() as conn:
        changes = _changes(conn, start)
    links, flagged = [], []
    for (src, tgt), rows in data.items():
        if src.startswith('mac-') or tgt.startswith('mac-'):
            continue
        changed = max(changes.get(src) or '', changes.get(tgt) or '') or None
        if changed:
            rows = [r for r in rows if r['hour'] >= changed[:13] + ':00:00']
        snr_pts, lq_pts = daily(rows, 'snr_med'), daily(rows, 'lq_med')
        if len(snr_pts) < 2 and len(lq_pts) < 2:
            continue
        snr_t, lq_t = trend(snr_pts), trend(lq_pts)
        reasons = judge(snr_t, lq_t)
        pred = budget.get((src, tgt))
        cur = snr_pts[-1][2] if snr_pts else None
        item = {'source': src, 'target': tgt, 'infrastructure': src in infra or tgt in infra,
                'snr': snr_t, 'lq': lq_t, 'snr_now': cur, 'predicted_snr': pred,
                'budget_gap_db': round(cur - pred, 1) if cur is not None and pred is not None else None,
                'since_change': changed, 'reasons': reasons, 'degrading': bool(reasons),
                'series': [{'day': d, 'snr': v} for _, d, v in snr_pts]}
        links.append(item)
        if reasons:
            flagged.append(item)
    links.sort(key=lambda x: (not x['degrading'], (x['snr'] or {}).get('slope_month') or 0))
    return {'generated_at': now.strftime(TS_FMT), 'days': days, 'links': links, 'flagged': flagged,
            'thresholds': {'db_per_month': config.DEGRADE_DB_PER_MONTH, 'lq_per_month': config.DEGRADE_LQ_PER_MONTH,
                           'min_days': config.DEGRADE_MIN_DAYS, 'min_r2': config.DEGRADE_MIN_R2},
            'notes': ['Trends use hourly rollups of the scan samples; a channel, radio-config or antenna change '
                      'restarts the trend after it.']}


def watch(now=None):
    """Daily: log link_degrading / link_degrading_cleared on transitions."""
    ensure_tables()
    now = now or datetime.now()
    rep = report(now=now, with_budget=False)
    current = {(x['source'], x['target']): x for x in rep['links']}
    events = []
    with database.get_connection() as conn:
        state = {(r['source_node'], r['target_node']): dict(r) for r in conn.execute('SELECT * FROM degradation_state')}
    try:
        import maintenance
    except Exception:
        maintenance = None
    for key, x in current.items():
        was = bool((state.get(key) or {}).get('flagged'))
        if x['degrading'] == was:
            continue
        src, tgt = key
        if maintenance and maintenance.covering_window(src, now.strftime(TS_FMT), tgt):
            continue
        detail = '; '.join(x['reasons']) if x['degrading'] else 'trend back within limits'
        et, sev = ('link_degrading', 'warning') if x['degrading'] else ('link_degrading_cleared', 'info')
        database.log_event(et, src, f'RF link to {tgt}: {detail}', sev)
        events.append({'event_type': et, 'node_name': src, 'details': f'RF link to {tgt}: {detail}', 'severity': sev})
        with database.get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO degradation_state VALUES (?, ?, ?, ?, ?, ?)',
                         (src, tgt, 1 if x['degrading'] else 0, now.strftime(TS_FMT), detail, now.strftime(TS_FMT)))
    return events


def install(scheduler):
    if not config.DEGRADE_WATCH_ENABLED:
        return
    scheduler.add_job(lambda: watch(), 'cron', hour=6, minute=15, id='degradation_watch', replace_existing=True)

"""
Continuous call quality per node (TODO #78). The ping rounds (rf_stats) ping
every RF link's far end from this PC each PING_INTERVAL; per node that gives
latency, jitter (ping mdev; Windows ping has none, so half the min-max spread)
and loss along the collector -> node path. From those, per node and per
interval, the E-model MOS (link_health.compute_mos, codec VOIP_CODEC).

`report(hours)`: per node the latest, median and worst-hour jitter / MOS /
latency / loss with a bucketed series, graded ok / warning (jitter >
CALL_JITTER_WARN_MS or MOS <= CALL_MOS_WARN) / critical (> CALL_JITTER_CRIT_MS
or <= CALL_MOS_CRIT), plus the Route Watch flapping state of paths ending at
the node (route switches are a common cause of jitter bursts).

`check()` after each scan, on the last 30 minutes' median, 2 checks in a row
to raise or clear: `call_quality_poor` (warning / critical) and
`call_quality_ok`; state in SQLite `call_quality_state`, maintenance
respected. Read-only otherwise.
"""

from datetime import datetime, timedelta
from statistics import median

import config
import database
import link_health

TS_FMT = '%Y-%m-%d %H:%M:%S'
WATCH_RUNS = 2
RECENT_MINUTES = 30

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS call_quality_state (
            node_name TEXT PRIMARY KEY, grade TEXT, pending TEXT, count INTEGER DEFAULT 0, since DATETIME)''')
    _tables_ready = True


def grade(jitter, mos):
    if (jitter is not None and jitter > config.CALL_JITTER_CRIT_MS) or (mos is not None and mos <= config.CALL_MOS_CRIT):
        return 'critical'
    if (jitter is not None and jitter > config.CALL_JITTER_WARN_MS) or (mos is not None and mos <= config.CALL_MOS_WARN):
        return 'warning'
    return 'ok' if jitter is not None or mos is not None else None


def _samples(conn, start, node=None):
    sql = '''SELECT timestamp, target_node, ping_avg, jitter, ping_loss FROM link_history
             WHERE timestamp >= ? AND ping_avg IS NOT NULL AND sample_type = 'scan' '''
    args = [start]
    if node:
        sql += ' AND target_node = ?'
        args.append(node)
    out = {}
    for r in conn.execute(sql + ' ORDER BY timestamp', args):
        out.setdefault(r['target_node'], []).append(dict(r))
    return out


def _mos(s):
    return link_health.compute_mos(s['ping_avg'], s['jitter'] or 0, s['ping_loss'] or 0,
                                   codec=getattr(config, 'VOIP_CODEC', None))


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 2) if vals else None


def summarize(samples, now, bucket_minutes=60):
    """Per node stats of one node's samples (chronological)."""
    for s in samples:
        s['mos'] = _mos(s)
    recent_cut = (now - timedelta(minutes=RECENT_MINUTES)).strftime(TS_FMT)
    recent = [s for s in samples if s['timestamp'] >= recent_cut]
    buckets = {}
    for s in samples:
        t = datetime.strptime(s['timestamp'][:19], TS_FMT)
        key = t.replace(minute=(t.minute // bucket_minutes) * bucket_minutes if bucket_minutes < 60 else 0,
                        second=0).strftime(TS_FMT)
        buckets.setdefault(key, []).append(s)
    series = [{'t': k, 'jitter': _med([s['jitter'] for s in v]), 'mos': _med([s['mos'] for s in v]),
               'latency': _med([s['ping_avg'] for s in v]), 'loss': _med([s['ping_loss'] for s in v])}
              for k, v in sorted(buckets.items())]
    worst = max(series, key=lambda b: (b['jitter'] or 0)) if series else None
    rj, rm = _med([s['jitter'] for s in recent]), _med([s['mos'] for s in recent])
    return {'samples': len(samples), 'recent_jitter': rj, 'recent_mos': rm,
            'recent_latency': _med([s['ping_avg'] for s in recent]), 'recent_loss': _med([s['ping_loss'] for s in recent]),
            'jitter': _med([s['jitter'] for s in samples]), 'mos': _med([s['mos'] for s in samples]),
            'latency': _med([s['ping_avg'] for s in samples]), 'loss': _med([s['ping_loss'] for s in samples]),
            'worst_hour': worst, 'grade': grade(rj, rm) if recent else None,
            'window_grade': grade(_med([s['jitter'] for s in samples]), _med([s['mos'] for s in samples])),
            'series': series}


def report(hours=24, node=None, now=None):
    ensure_tables()
    now = now or datetime.now()
    start = (now - timedelta(hours=hours)).strftime(TS_FMT)
    bucket = 15 if hours <= 12 else 60
    with database.get_connection() as conn:
        data = _samples(conn, start, node)
        state = {r['node_name']: dict(r) for r in conn.execute('SELECT * FROM call_quality_state')}
    flapping = {}
    try:
        import route_watch
        for p in route_watch.list_report(hours=24).get("paths", []):
            if p.get('flapping'):
                flapping[p.get('target')] = p.get('flaps_24h')
    except Exception:
        pass
    infra = database.get_infrastructure_nodes()
    nodes = []
    for name, samples in data.items():
        if name.startswith('mac-'):
            continue
        s = summarize(samples, now, bucket)
        s.update(node=name, is_infrastructure=name in infra, alarm=(state.get(name) or {}).get('grade') in
                 ('warning', 'critical'), route_flaps_24h=flapping.get(name))
        nodes.append(s)
    order = {'critical': 0, 'warning': 1, 'ok': 2, None: 3}
    nodes.sort(key=lambda n: (order.get(n['grade'], 3), -(n['recent_jitter'] or 0), n['node']))
    return {'generated_at': now.strftime(TS_FMT), 'hours': hours, 'nodes': nodes,
            'thresholds': {'jitter_warn_ms': config.CALL_JITTER_WARN_MS, 'jitter_crit_ms': config.CALL_JITTER_CRIT_MS,
                           'mos_warn': config.CALL_MOS_WARN, 'mos_crit': config.CALL_MOS_CRIT},
            'counts': {g: sum(1 for n in nodes if n['grade'] == g) for g in ('critical', 'warning', 'ok')},
            'notes': ['Measured from this PC to each node by the ping rounds (collector -> node path). '
                      'Windows ping reports no mdev, so jitter is half the min-max spread of each round.']}


def check(now=None):
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    with database.get_connection() as conn:
        data = _samples(conn, (now - timedelta(minutes=RECENT_MINUTES)).strftime(TS_FMT))
        state = {r['node_name']: dict(r) for r in conn.execute('SELECT * FROM call_quality_state')}
    try:
        import maintenance
    except Exception:
        maintenance = None
    events = []
    for name, samples in data.items():
        if name.startswith('mac-'):
            continue
        jit = _med([s['jitter'] for s in samples])
        mos = _med([_mos(s) for s in samples])
        g = grade(jit, mos)
        if g is None:
            continue
        st = state.get(name)
        with database.get_connection() as conn:
            if st is None:
                conn.execute('INSERT INTO call_quality_state VALUES (?, ?, NULL, 0, ?)', (name, g if g == 'ok' else 'ok', now_s))
                st = {'grade': 'ok', 'pending': None, 'count': 0}
            if st['grade'] == g:
                conn.execute('UPDATE call_quality_state SET pending = NULL, count = 0 WHERE node_name = ?', (name,))
                continue
            count = (st['count'] or 0) + 1 if st['pending'] == g else 1
            if count < WATCH_RUNS:
                conn.execute('UPDATE call_quality_state SET pending = ?, count = ? WHERE node_name = ?', (g, count, name))
                continue
            conn.execute('UPDATE call_quality_state SET grade = ?, pending = NULL, count = 0, since = ? WHERE node_name = ?',
                         (g, now_s, name))
        if maintenance and maintenance.covering_window(name, now_s):
            continue
        detail = f"jitter {jit:g} ms, MOS {mos:g} (last {RECENT_MINUTES} min, from this PC)"
        if g == 'ok':
            et, sev = 'call_quality_ok', 'info'
            detail = 'Call quality back to normal: ' + detail
        else:
            et, sev = 'call_quality_poor', g
            detail = f'Call quality {"bad" if g == "critical" else "poor"}: ' + detail
        if et == 'call_quality_ok' and st['grade'] == 'ok':
            continue
        database.log_event(et, name, detail, sev)
        events.append({'event_type': et, 'node_name': name, 'details': detail, 'severity': sev})
    return events

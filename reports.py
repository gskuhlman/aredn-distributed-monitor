"""
Time-window reporting over the local SQLite history.

Backs the Reports tab: given a [start, end) window and the same filters the
other pages use (node scope, link type, search, event category, severity,
sample type), return everything collected in that window -- per-node poll
health, per-link RF stats, link state transitions, events, and VoIP results --
plus bucketed time series for charts.

Read-only. Timestamps in these tables are local-time 'YYYY-MM-DD HH:MM:SS'
strings, so the window is compared in that form too.
"""

from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
MAX_WINDOW = timedelta(days=32)
TARGET_BUCKETS = 240
NICE_BUCKETS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]
MAX_ROWS = 5000

LINK_TYPE_BUCKETS = {
    'rf': ('RF',),
    'dtd': ('DTD',),
    'wireguard': ('WIREGUARD', 'WG'),
    'tunnel': ('TUN', 'TUNNEL', 'VTUN'),
}

EVENT_CATEGORIES = {
    'node': ('node_discovered', 'node_offline', 'node_online', 'node_degraded', 'node_reboot',
             'radio_config_change',
             'noise_jump', 'noise_cleared'),
    'link': ('link_new', 'link_dropped', 'link_removed', 'link_restored',
             'link_blocked', 'link_unblocked',
             'one_way_link', 'one_way_link_cleared'),
    'freq': ('frequency_change',),
    'system': ('gateway_lost', 'gateway_restored'),
}

# Link state-log states that mean the link was impaired.
TROUBLE_STATES = ('down', 'blocked', 'scanner_unreachable')


class ReportError(ValueError):
    pass


def parse_ts(value, name):
    """Parse a datetime-local style string ('YYYY-MM-DDTHH:MM[:SS]')."""
    if not value:
        raise ReportError(f'{name} is required')
    text = value.strip().replace('T', ' ')
    for fmt in (TS_FMT, '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    raise ReportError(f'{name} must look like YYYY-MM-DDTHH:MM')


def parse_window(start, end):
    start_dt = parse_ts(start, 'start')
    end_dt = parse_ts(end, 'end')
    if end_dt <= start_dt:
        raise ReportError('end must be after start')
    if end_dt - start_dt > MAX_WINDOW:
        raise ReportError('window is limited to 32 days')
    return start_dt, end_dt


def bucket_seconds(start_dt, end_dt):
    span = (end_dt - start_dt).total_seconds()
    for size in NICE_BUCKETS:
        if span / size <= TARGET_BUCKETS:
            return size
    return NICE_BUCKETS[-1]


def _bucket_expr(column='timestamp'):
    """SQL expression giving a row's bucket index; binds (start, bucket)."""
    return f"CAST((julianday({column}) - julianday(?)) * 86400 / ? AS INTEGER)"


def _in(values):
    return ','.join('?' * len(values))


def _link_type_sql(link_type, column='link_type'):
    types = LINK_TYPE_BUCKETS.get(link_type)
    if not types:
        return '', []
    return f" AND UPPER({column}) IN ({_in(types)})", list(types)


# ---------------------------------------------------------------- scope ----

def resolve_scope(conn, scope='all', link_type='all', search=''):
    """Return the set of node names the filters allow, or None for no limit.

    Mirrors the Nodes/RF Stats/Diagnostics filters: scope is one of all,
    selected, selected-connected, infrastructure, infrastructure-connected,
    rf (nodes with an RF link), active, inactive;
    link_type further limits to nodes with a link of that type; search matches
    name/IP/model/description.
    """
    cur = conn.cursor()
    names = None

    def narrow(found):
        nonlocal names
        found = set(found)
        names = found if names is None else names & found

    if scope in ('selected', 'selected-connected', 'infrastructure', 'infrastructure-connected'):
        table = 'selected_nodes' if scope.startswith('selected') else 'infrastructure_nodes'
        selected = {r[0] for r in cur.execute(f'SELECT node_name FROM {table}')}
        if scope.endswith('-connected') and selected:
            rows = cur.execute(f'''
                SELECT source_node, target_node FROM links
                WHERE UPPER(link_type) IN ('RF', 'DTD')
                AND (source_node IN ({_in(selected)}) OR target_node IN ({_in(selected)}))
            ''', list(selected) * 2)
            for src, tgt in rows:
                selected.update((src, tgt))
        narrow(selected)
    elif scope == 'rf':
        narrow(r[0] for r in cur.execute('''
            SELECT source_node FROM links WHERE UPPER(link_type) = 'RF'
            UNION SELECT target_node FROM links WHERE UPPER(link_type) = 'RF'
        '''))
    elif scope == 'active':
        narrow(r[0] for r in cur.execute('SELECT name FROM nodes WHERE is_active = 1'))
    elif scope == 'inactive':
        narrow(r[0] for r in cur.execute('SELECT name FROM nodes WHERE is_active != 1'))

    if link_type in LINK_TYPE_BUCKETS:
        types = LINK_TYPE_BUCKETS[link_type]
        narrow(r[0] for r in cur.execute(f'''
            SELECT source_node FROM links WHERE UPPER(link_type) IN ({_in(types)})
            UNION SELECT target_node FROM links WHERE UPPER(link_type) IN ({_in(types)})
        ''', list(types) * 2))

    search = (search or '').strip().lower()
    if search:
        like = f'%{search}%'
        found = {r[0] for r in cur.execute('''
            SELECT name FROM nodes
            WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(ip, '')) LIKE ?
               OR LOWER(COALESCE(model, '')) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ?
        ''', (like,) * 4)}
        # Link-only neighbors aren't in the nodes table; match them by name.
        found.update(r[0] for r in cur.execute('''
            SELECT source_node FROM links WHERE LOWER(source_node) LIKE ?
            UNION SELECT target_node FROM links WHERE LOWER(target_node) LIKE ?
        ''', (like, like)))
        narrow(found)

    if names is not None:
        cur.execute('CREATE TEMP TABLE IF NOT EXISTS scope_nodes (name TEXT PRIMARY KEY)')
        cur.execute('DELETE FROM scope_nodes')
        cur.executemany('INSERT OR IGNORE INTO scope_nodes VALUES (?)', [(n,) for n in names])
    return names


def _node_scope_sql(names, column):
    return '' if names is None else f' AND {column} IN (SELECT name FROM scope_nodes)'


def _link_scope_sql(names, src='source_node', tgt='target_node'):
    if names is None:
        return ''
    return (f' AND ({src} IN (SELECT name FROM scope_nodes)'
            f' OR {tgt} IN (SELECT name FROM scope_nodes))')


def _sample_type_sql(sample_type):
    if sample_type in ('scan', 'incident'):
        return ' AND sample_type = ?', [sample_type]
    return '', []


def _event_type_filter(categories):
    """Event types allowed by the selected categories, or None for all."""
    cats = [c for c in (categories or []) if c in EVENT_CATEGORIES]
    if not categories or set(cats) == set(EVENT_CATEGORIES):
        return None
    known = {t for types in EVENT_CATEGORIES.values() for t in types}
    allowed = {t for c in cats for t in EVENT_CATEGORIES[c]}
    return allowed, known


def _event_sql(categories, severity):
    sql, params = '', []
    filt = _event_type_filter(categories)
    if filt is not None:
        allowed, known = filt
        # Uncategorized event types ride along with 'system'.
        if 'system' in categories:
            sql += f' AND (event_type IN ({_in(allowed)}) OR event_type NOT IN ({_in(known)}))'
            params += list(allowed) + list(known)
        elif allowed:
            sql += f' AND event_type IN ({_in(allowed)})'
            params += list(allowed)
        else:
            sql += ' AND 0'
    if severity == 'warning':
        sql += " AND severity IN ('warning', 'error', 'critical')"
    return sql, params


# --------------------------------------------------------------- report ----

def window_report(start, end, scope='all', link_type='all', search='',
                  sample_type='all', event_categories=None, severity='all'):
    start_dt, end_dt = parse_window(start, end)
    s, e = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)
    bucket = bucket_seconds(start_dt, end_dt)
    win = (s, e)

    with database.get_connection() as conn:
        cur = conn.cursor()
        names = resolve_scope(conn, scope, link_type, search)
        lt_sql, lt_params = _link_type_sql(link_type)
        st_sql, st_params = _sample_type_sql(sample_type)
        ev_sql, ev_params = _event_sql(event_categories, severity)
        node_sql = _node_scope_sql(names, 'node_name')
        link_sql = _link_scope_sql(names)
        # System events (no node) always pass the node scope.
        ev_node_sql = '' if names is None else \
            ' AND (node_name IS NULL OR node_name IN (SELECT name FROM scope_nodes))'

        # ---- nodes: direct polls vs neighbor-relayed mesh probes
        node_rows = {r['node_name']: dict(r) for r in cur.execute(f'''
            SELECT node_name,
                SUM(probe_via IS NULL) AS polls,
                SUM(probe_via IS NULL AND reachable = 0) AS unreachable,
                SUM(probe_via IS NULL AND degraded = 1) AS degraded,
                AVG(CASE WHEN probe_via IS NULL AND reachable = 1 THEN response_ms END) AS avg_response_ms,
                MAX(CASE WHEN probe_via IS NULL AND reachable = 1 THEN response_ms END) AS max_response_ms,
                AVG(load1) AS avg_load1, MAX(load1) AS max_load1,
                MIN(mem_free) AS min_mem_free,
                AVG(channel_busy) AS avg_channel_busy,
                SUM(probe_via IS NOT NULL) AS mesh_probes,
                SUM(probe_via IS NOT NULL AND reachable = 1) AS mesh_probes_ok,
                MIN(timestamp) AS first_sample, MAX(timestamp) AS last_sample
            FROM node_health_history
            WHERE timestamp >= ? AND timestamp < ?{node_sql}
            GROUP BY node_name
        ''', win)}

        event_counts = {}
        for r in cur.execute(f'''
            SELECT node_name, event_type, severity, COUNT(*) AS n FROM events
            WHERE timestamp >= ? AND timestamp < ? AND node_name IS NOT NULL{node_sql}
            GROUP BY node_name, event_type, severity
        ''', win):
            c = event_counts.setdefault(r['node_name'], {'events': 0, 'warnings': 0, 'reboots': 0})
            c['events'] += r['n']
            if r['severity'] in ('warning', 'error', 'critical'):
                c['warnings'] += r['n']
            if r['event_type'] == 'node_reboot':
                c['reboots'] += r['n']

        node_meta = {r['name']: dict(r) for r in cur.execute(
            'SELECT name, ip, model, firmware_version, is_active, last_seen FROM nodes')}
        node_names = set(node_rows) | set(event_counts)
        if names is not None:
            node_names |= {n for n in names if n in node_meta}
        nodes = []
        for name in sorted(node_names):
            row = node_rows.get(name, {'node_name': name, 'polls': 0, 'unreachable': 0,
                                       'degraded': 0, 'mesh_probes': 0, 'mesh_probes_ok': 0})
            row['name'] = row.pop('node_name')
            row.update(event_counts.get(name, {'events': 0, 'warnings': 0, 'reboots': 0}))
            meta = node_meta.get(name, {})
            row.update({k: meta.get(k) for k in ('ip', 'model', 'firmware_version', 'is_active')})
            polls = row.get('polls') or 0
            row['reachable_pct'] = (round(100.0 * (polls - (row.get('unreachable') or 0)) / polls, 1)
                                    if polls else None)
            nodes.append(row)

        # ---- links
        transitions_by_link = {}
        for r in cur.execute(f'''
            SELECT source_node, target_node, state, COUNT(*) AS n FROM link_state_log
            WHERE timestamp >= ? AND timestamp < ?{lt_sql}{link_sql}
            GROUP BY source_node, target_node, state
        ''', list(win) + lt_params):
            t = transitions_by_link.setdefault((r['source_node'], r['target_node']),
                                               {'transitions': 0, 'trouble_transitions': 0})
            t['transitions'] += r['n']
            if r['state'] in TROUBLE_STATES:
                t['trouble_transitions'] += r['n']

        links = []
        for r in cur.execute(f'''
            SELECT source_node, target_node, MAX(link_type) AS link_type,
                COUNT(*) AS samples,
                SUM(sample_type = 'incident') AS incident_samples,
                AVG(quality) AS avg_quality, MIN(quality) AS min_quality,
                AVG(snr) AS avg_snr, MIN(snr) AS min_snr, AVG(rev_snr) AS avg_rev_snr,
                COUNT(ping_avg) AS ping_samples,
                AVG(ping_avg) AS avg_ping, MAX(ping_max) AS max_ping,
                AVG(ping_loss) AS avg_loss, AVG(jitter) AS avg_jitter,
                COUNT(throughput_tx) AS iperf_tests,
                AVG(throughput_tx) AS avg_throughput_tx, AVG(throughput_rx) AS avg_throughput_rx,
                SUM(blocked = 1) AS blocked_samples,
                MIN(timestamp) AS first_sample, MAX(timestamp) AS last_sample
            FROM link_history
            WHERE timestamp >= ? AND timestamp < ?{lt_sql}{st_sql}{link_sql}
            GROUP BY source_node, target_node
        ''', list(win) + lt_params + st_params):
            row = dict(r)
            row.update(transitions_by_link.pop((row['source_node'], row['target_node']),
                                               {'transitions': 0, 'trouble_transitions': 0}))
            links.append(row)
        # Links that only changed state (no samples) in the window.
        for (src, tgt), t in transitions_by_link.items():
            links.append({'source_node': src, 'target_node': tgt, 'samples': 0, **t})
        links.sort(key=lambda l: (l['source_node'], l['target_node']))

        # ---- transitions and events (newest first, capped)
        transitions = [dict(r) for r in cur.execute(f'''
            SELECT timestamp, source_node, target_node, link_type, state, quality, snr,
                   rev_snr, blocked_reason, detail, origin
            FROM link_state_log
            WHERE timestamp >= ? AND timestamp < ?{lt_sql}{link_sql}
            ORDER BY timestamp DESC LIMIT {MAX_ROWS + 1}
        ''', list(win) + lt_params)]
        events = [dict(r) for r in cur.execute(f'''
            SELECT timestamp, event_type, node_name, details, severity FROM events
            WHERE timestamp >= ? AND timestamp < ?{ev_sql}{ev_node_sql}
            ORDER BY timestamp DESC LIMIT {MAX_ROWS + 1}
        ''', list(win) + ev_params)]

        # ---- VoIP (session timestamps are ISO with offset; compare the local part)
        voip_tests = [dict(r) for r in cur.execute(f'''
            SELECT source_node, target_node, codec, ran_at, mos, mos_label, latency_ms,
                   jitter_ms, loss_pct, worst_segment
            FROM voip_test_results
            WHERE ran_at >= ? AND ran_at < ?{_link_scope_sql(names)}
            ORDER BY ran_at DESC
        ''', win)]
        voip_sessions = [dict(r) for r in cur.execute('''
            SELECT id, mode, source, target, remote_host, started_at, ended_at
            FROM voip_sessions
            WHERE REPLACE(SUBSTR(started_at, 1, 19), 'T', ' ') < ?
              AND REPLACE(SUBSTR(COALESCE(ended_at, started_at), 1, 19), 'T', ' ') >= ?
            ORDER BY started_at DESC
        ''', (e, s))]

        # ---- timeline buckets
        timeline = {}

        def add(rows, key):
            for r in rows:
                timeline.setdefault(r[0], {}).__setitem__(key, r[1])

        b = (s, bucket)
        add(cur.execute(f'''
            SELECT {_bucket_expr()} AS b, COUNT(*) FROM events
            WHERE timestamp >= ? AND timestamp < ?
              AND severity IN ('warning', 'error', 'critical'){ev_sql}{ev_node_sql}
            GROUP BY b
        ''', list(b) + list(win) + ev_params), 'warnings')
        add(cur.execute(f'''
            SELECT {_bucket_expr()} AS b, COUNT(*) FROM events
            WHERE timestamp >= ? AND timestamp < ?
              AND severity NOT IN ('warning', 'error', 'critical'){ev_sql}{ev_node_sql}
            GROUP BY b
        ''', list(b) + list(win) + ev_params), 'info')
        add(cur.execute(f'''
            SELECT {_bucket_expr()} AS b, COUNT(*) FROM link_state_log
            WHERE timestamp >= ? AND timestamp < ?
              AND state IN ({_in(TROUBLE_STATES)}){lt_sql}{link_sql}
            GROUP BY b
        ''', list(b) + list(win) + list(TROUBLE_STATES) + lt_params), 'link_trouble')
        add(cur.execute(f'''
            SELECT {_bucket_expr()} AS b, SUM(reachable = 0) FROM node_health_history
            WHERE timestamp >= ? AND timestamp < ? AND probe_via IS NULL{node_sql}
            GROUP BY b
        ''', list(b) + list(win)), 'unreachable')
        add(cur.execute(f'''
            SELECT {_bucket_expr()} AS b, AVG(quality) FROM link_history
            WHERE timestamp >= ? AND timestamp < ? AND quality IS NOT NULL
              {lt_sql}{st_sql}{link_sql}
            GROUP BY b
        ''', list(b) + list(win) + lt_params + st_params), 'avg_quality')

    n_buckets = int((end_dt - start_dt).total_seconds() // bucket) + 1
    timeline_rows = []
    for i in range(n_buckets):
        t = start_dt + timedelta(seconds=i * bucket)
        if t >= end_dt:
            break
        row = timeline.get(i, {})
        timeline_rows.append({
            't': t.strftime(TS_FMT),
            'warnings': row.get('warnings') or 0,
            'info': row.get('info') or 0,
            'link_trouble': row.get('link_trouble') or 0,
            'unreachable': row.get('unreachable') or 0,
            'avg_quality': round(row['avg_quality'], 1) if row.get('avg_quality') is not None else None,
        })

    polls = sum(n.get('polls') or 0 for n in nodes)
    unreachable = sum(n.get('unreachable') or 0 for n in nodes)
    quality = [l['avg_quality'] for l in links if l.get('avg_quality') is not None]
    gateway_outages = sum(1 for ev in events if ev['event_type'] == 'gateway_lost')

    return {
        'window': {'start': s, 'end': e, 'bucket_seconds': bucket},
        'filters': {'scope': scope, 'link_type': link_type, 'search': search,
                    'sample_type': sample_type, 'event_categories': event_categories,
                    'severity': severity},
        'scope_node_count': None if names is None else len(names),
        'summary': {
            'nodes': len(nodes),
            'links': len(links),
            'polls': polls,
            'unreachable_polls': unreachable,
            'reachable_pct': round(100.0 * (polls - unreachable) / polls, 1) if polls else None,
            'link_samples': sum(l.get('samples') or 0 for l in links),
            'avg_quality': round(sum(quality) / len(quality), 1) if quality else None,
            'iperf_tests': sum(l.get('iperf_tests') or 0 for l in links),
            'transitions': len(transitions[:MAX_ROWS]),
            'events': len(events[:MAX_ROWS]),
            'warnings': sum(1 for ev in events[:MAX_ROWS]
                            if ev['severity'] in ('warning', 'error', 'critical')),
            'gateway_outages': gateway_outages,
            'voip_tests': len(voip_tests),
            'voip_sessions': len(voip_sessions),
        },
        'timeline': timeline_rows,
        'nodes': nodes,
        'links': links,
        'transitions': transitions[:MAX_ROWS],
        'transitions_truncated': len(transitions) > MAX_ROWS,
        'events': events[:MAX_ROWS],
        'events_truncated': len(events) > MAX_ROWS,
        'voip_tests': voip_tests,
        'voip_sessions': voip_sessions,
    }


# --------------------------------------------------------------- series ----

def node_series(name, start, end):
    """Bucketed poll health for one node over the window."""
    start_dt, end_dt = parse_window(start, end)
    s, e = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)
    bucket = bucket_seconds(start_dt, end_dt)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(f'''
            SELECT {_bucket_expr()} AS b, MIN(timestamp) AS t,
                SUM(probe_via IS NULL) AS polls,
                SUM(probe_via IS NULL AND reachable = 0) AS unreachable,
                SUM(probe_via IS NULL AND degraded = 1) AS degraded,
                AVG(CASE WHEN probe_via IS NULL AND reachable = 1 THEN response_ms END) AS avg_response_ms,
                MAX(CASE WHEN probe_via IS NULL AND reachable = 1 THEN response_ms END) AS max_response_ms,
                AVG(load1) AS load1, MIN(mem_free) AS mem_free, AVG(channel_busy) AS channel_busy,
                MAX(uptime_seconds) AS uptime_seconds,
                SUM(probe_via IS NOT NULL AND reachable = 1) AS mesh_ok,
                SUM(probe_via IS NOT NULL AND reachable = 0) AS mesh_fail
            FROM node_health_history
            WHERE node_name = ? AND timestamp >= ? AND timestamp < ?
            GROUP BY b ORDER BY b
        ''', (s, bucket, name, s, e))]
        events = [dict(r) for r in conn.execute('''
            SELECT timestamp, event_type, details, severity FROM events
            WHERE node_name = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
        ''', (name, s, e))]
    return {'node': name, 'window': {'start': s, 'end': e, 'bucket_seconds': bucket},
            'series': rows, 'events': events}


def link_series(source, target, start, end, sample_type='all'):
    """Bucketed RF stats for one link (avg/min/max per bucket), every iperf
    result as its own point, and the link's state transitions."""
    start_dt, end_dt = parse_window(start, end)
    s, e = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)
    bucket = bucket_seconds(start_dt, end_dt)
    st_sql, st_params = _sample_type_sql(sample_type)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(f'''
            SELECT {_bucket_expr()} AS b, MIN(timestamp) AS t, COUNT(*) AS samples,
                AVG(quality) AS quality, MIN(quality) AS min_quality,
                AVG(snr) AS snr, MIN(snr) AS min_snr, AVG(rev_snr) AS rev_snr,
                AVG(ping_avg) AS ping_avg, MIN(ping_min) AS ping_min, MAX(ping_max) AS ping_max,
                AVG(ping_loss) AS ping_loss, AVG(jitter) AS jitter,
                SUM(blocked = 1) AS blocked
            FROM link_history
            WHERE source_node = ? AND target_node = ? AND timestamp >= ? AND timestamp < ?{st_sql}
            GROUP BY b ORDER BY b
        ''', [s, bucket, source, target, s, e] + st_params)]
        throughput = [dict(r) for r in conn.execute('''
            SELECT timestamp AS t, throughput_tx, throughput_rx FROM link_history
            WHERE source_node = ? AND target_node = ? AND timestamp >= ? AND timestamp < ?
              AND (throughput_tx IS NOT NULL OR throughput_rx IS NOT NULL)
            ORDER BY timestamp
        ''', (source, target, s, e))]
        transitions = [dict(r) for r in conn.execute('''
            SELECT timestamp, state, quality, snr, blocked_reason, detail, origin
            FROM link_state_log
            WHERE source_node = ? AND target_node = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
        ''', (source, target, s, e))]
    return {'source': source, 'target': target,
            'window': {'start': s, 'end': e, 'bucket_seconds': bucket},
            'series': rows, 'throughput': throughput, 'transitions': transitions}

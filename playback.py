"""
Map playback (Map page time slider): how an outage spread.

For a window (as the Reports tab: start/end, max 32 days) and the Reports
bucket size, returns per bucket:
  nodes   up (every scanner poll answered), partial (some), down (none),
          or null (not polled in that bucket), from node_health_history
          scanner polls (mesh probes left out);
  links   swimlanes.build() lanes (good / degraded / flapping / down /
          unknown / none per bucket);
  events  warning/critical events per bucket (offline, dropped, one-way,
          reboots ...) to list beside the slider.
The Map page draws them on the node positions it already has. Read-only.
"""

from collections import defaultdict
from datetime import timedelta

import database
import reports
import swimlanes

TS_FMT = '%Y-%m-%d %H:%M:%S'
EVENT_TYPES = ('node_offline', 'node_online', 'link_dropped', 'link_restored', 'node_reboot', 'one_way_link',
               'gateway_lost', 'gateway_restored', 'noise_jump', 'route_detour', 'service_down')


def build(start, end):
    start_dt, end_dt = reports.parse_window(start, end)
    size = reports.bucket_seconds(start_dt, end_dt)
    n = max(1, int((end_dt - start_dt).total_seconds() // size))
    start_s, end_s = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)
    lanes = swimlanes.build(start, end, limit=100000)
    b = reports._bucket_expr()
    nodes = defaultdict(lambda: [None] * n)
    events = []
    with database.get_connection() as conn:
        for name, idx, polls, ok in conn.execute(f'''
                SELECT node_name, {b} AS bi, COUNT(*), SUM(reachable) FROM node_health_history
                WHERE timestamp >= ? AND timestamp < ? AND (probe_via IS NULL OR probe_via = '')
                GROUP BY node_name, bi''', (start_s, size, start_s, end_s)):
            if idx is None or not 0 <= idx < n:
                continue
            nodes[name][idx] = 'up' if ok == polls else ('down' if not ok else 'partial')
        for ts, etype, who, details, sev, idx in conn.execute(f'''
                SELECT timestamp, event_type, node_name, details, severity, {b} AS bi FROM events
                WHERE timestamp >= ? AND timestamp < ? AND event_type IN ({",".join("?" * len(EVENT_TYPES))})
                ORDER BY timestamp LIMIT 5000''', (start_s, size, start_s, end_s, *EVENT_TYPES)):
            if idx is not None and 0 <= idx < n:
                events.append({'time': ts, 'type': etype, 'node': who, 'details': (details or '')[:200],
                               'severity': sev, 'bucket': idx})
    return {
        'window': {'start': start_s, 'end': end_s, 'bucket_seconds': size,
                   'buckets': [(start_dt + timedelta(seconds=size * i)).strftime(TS_FMT) for i in range(n)]},
        'nodes': dict(nodes),
        'links': [{'a': l['a'], 'b': l['b'], 'type': l['type'], 'cells': l['cells']} for l in lanes['lanes']],
        'events': events,
    }

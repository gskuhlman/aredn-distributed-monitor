"""
Node x hour heatmap (Reports tab "Heatmap").

One row per node, one column per hour of the Reports window (or, folded, per
hour of day), coloured by reachability (scanner polls answered / polls) or by
link quality (mean quality the node reports for its RF neighbours). Rows are
grouped by site so a whole site dropping together shows as a block, and the
hour-of-day fold shows nightly patterns.

Group: the infrastructure site for infrastructure nodes, else the callsign
(the name up to the first '-'), so one operator's radios sit together.

Read-only; filtered by the same scope/link type/search as the Reports tab.
"""

from collections import defaultdict
from datetime import timedelta

import database
import maintenance
import reports

TS_FMT = '%Y-%m-%d %H:%M:%S'
METRICS = ('reach', 'lq')


def _group(name, infra):
    if name in infra:
        return infra[name] or name.split('-')[0]
    return name.split('-')[0]


def build(start, end, metric='reach', fold=False, scope='all', link_type='all', search=''):
    """{rows: [{name, group, infrastructure, cells: [value|None], polls: [...]}], columns, ...}.

    cells are 0-100 (None = no data). With fold, columns are hours of day
    0..23 and each cell aggregates every day in the window.
    """
    if metric not in METRICS:
        metric = 'reach'
    start_dt, end_dt = reports.parse_window(start, end)
    start_dt = start_dt.replace(minute=0, second=0)
    hours = max(1, int((end_dt - start_dt).total_seconds() // 3600) + (1 if end_dt.minute else 0))
    start_s, end_s = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)
    ncols = 24 if fold else hours

    def col(idx):
        return (start_dt.hour + idx) % 24 if fold else idx

    agg = defaultdict(lambda: [0.0, 0])   # (node, col) -> [sum, count]
    with database.get_connection() as conn:
        names = reports.resolve_scope(conn, scope, link_type, search)
        infra = {r[0]: r[1] for r in conn.execute('SELECT node_name, site FROM infrastructure_nodes')}
        b = reports._bucket_expr()
        if metric == 'reach':
            sql = f'''SELECT node_name, {b} AS bi, SUM(CASE WHEN reachable = 1 THEN 1 ELSE 0 END), COUNT(*)
                      FROM node_health_history
                      WHERE timestamp >= ? AND timestamp < ? AND probe_via IS NULL
                      {reports._node_scope_sql(names, 'node_name')}
                      GROUP BY node_name, bi'''
        else:
            sql = f'''SELECT source_node, {b} AS bi, SUM(COALESCE(quality, lq)), COUNT(COALESCE(quality, lq))
                      FROM link_history
                      WHERE timestamp >= ? AND timestamp < ? AND UPPER(link_type) = 'RF'
                        AND COALESCE(sample_type, 'scan') = 'scan'
                      {reports._node_scope_sql(names, 'source_node')}
                      GROUP BY source_node, bi'''
        for name, idx, total, count in conn.execute(sql, (start_s, 3600, start_s, end_s)):
            if idx is None or not 0 <= idx < hours or not count:
                continue
            if metric == 'reach':
                value = total / count * 100
            else:
                value = total / count
            cell = agg[(name, col(idx))]
            cell[0] += value * count
            cell[1] += count
        windows = maintenance.windows_between(start_dt, end_dt, conn=conn)

    by_node = defaultdict(dict)
    for (name, c), (total, count) in agg.items():
        by_node[name][c] = (round(total / count), count)

    rows = []
    for name, cells in by_node.items():
        values = [cells.get(c, (None, 0))[0] for c in range(ncols)]
        have = [v for v in values if v is not None]
        maint = [False] * ncols
        if windows and not fold:
            for lo, hi in maintenance.intervals_for(name, start_dt, end_dt, windows=windows):
                for c in range(ncols):
                    b0 = start_dt + timedelta(hours=c)
                    if b0 < hi and b0 + timedelta(hours=1) > lo:
                        maint[c] = True
        rows.append({'name': name, 'group': _group(name, infra), 'infrastructure': name in infra,
                     'cells': values, 'samples': [cells.get(c, (None, 0))[1] for c in range(ncols)],
                     'maint': maint, 'avg': round(sum(have) / len(have)) if have else None,
                     'min': min(have) if have else None})

    # Infrastructure groups first, then groups by name; worst rows first in a group.
    infra_groups = {r['group'] for r in rows if r['infrastructure']}
    rows.sort(key=lambda r: (r['group'] not in infra_groups, r['group'],
                             r['avg'] if r['avg'] is not None else 101, r['name']))

    # Columns where several nodes of one group were bad together.
    together = []
    groups = defaultdict(list)
    for r in rows:
        groups[r['group']].append(r)
    for g, members in groups.items():
        if len(members) < 2:
            continue
        for c in range(ncols):
            vals = [m['cells'][c] for m in members if m['cells'][c] is not None]
            bad = [v for v in vals if v < 50]
            if len(vals) >= 2 and len(bad) == len(vals):
                together.append({'group': g, 'column': c, 'nodes': len(vals)})

    if fold:
        columns = [f'{h:02d}:00' for h in range(24)]
    else:
        columns = [(start_dt + timedelta(hours=i)).strftime(TS_FMT) for i in range(hours)]
    return {
        'metric': metric, 'fold': bool(fold),
        'window': {'start': start_s, 'end': end_s},
        'columns': columns, 'rows': rows,
        'groups': len(groups),
        'together': together[:200],
    }

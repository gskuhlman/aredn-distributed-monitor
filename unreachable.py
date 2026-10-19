"""
Unreachable classifier: why did the scanner fail to poll a node?

A failed sysinfo poll is not proof a node is down. This module sorts every
node with failed scanner polls in a time window into a cause, so reports can
count only nodes that are really down and explain the rest:

- maintenance: every failed poll fell inside a maintenance window covering
  the node (maintenance.py); failures inside a window never count elsewhere
- starting_node_outage: failures while the collector's starting node was lost
  (or legacy rows keyed to an IP fragment like "10" from before the gate)
- placeholder: a ``mac-…`` name the LQM reported without a hostname
- excluded: the node is now an 'exclude' scan boundary
- legacy_tunnel_only: only reported over old-style (vtun) "Tunnel" links,
  whose routes do not reach this collector
- pings_no_http: a neighbor-relayed mesh probe reaches it but the scanner's
  sysinfo fetch does not (up, but its web server is unreachable from here)
- intermittent: some polls answered, some failed (a real, partial problem)
- stale: no successful poll for over STALE_DAYS (or never)
- not_routable: every neighbor reporting it says it is not routable
- behind_supernode: only reported by supernodes
- never_resolves: the fetch fails at name resolution
- really_down: answered recently and now nothing answers, probes included

Read-only over SQLite. The scanner records ``fail_reason``, ``reported_by``
and ``via_link_type`` on failed polls in node_health_history; older rows
without them are classified from links/nodes state instead.
"""

import re
from datetime import datetime, timedelta

import database
import maintenance

TS_FMT = '%Y-%m-%d %H:%M:%S'
STALE_DAYS = 30
RECENT_HOURS = 24
DOWN_AFTER_FAILS = 3      # consecutive failures (polls or probes) that mean "down now"
LEGACY_TUNNEL_TYPES = {'TUNNEL', 'TUN', 'VTUN'}

CAUSES = {
    'maintenance': ('Maintenance', True, 'info',
                    'Every failed poll fell inside a planned maintenance window covering this node.'),
    'starting_node_outage': ('Starting node lost', True, 'info',
                             'The collector lost its starting node (its way onto the mesh), so these '
                             'polls say nothing about this node.'),
    'placeholder': ('MAC-only placeholder', True, 'info',
                    'A neighbor reported this radio only by MAC address; there is no hostname or IP to poll.'),
    'excluded': ('Excluded from scanning', True, 'info',
                 'The node is now an "exclude" scan boundary; these failures predate that.'),
    'legacy_tunnel_only': ('Legacy tunnel only', True, 'info',
                           'Only reported over old-style (vtun) tunnel links. Their routes are not carried '
                           'back to this part of the mesh, so the scanner cannot reach it even when it is up.'),
    'pings_no_http': ('Up, no HTTP from here', True, 'warning',
                      'A neighbor-relayed mesh probe reaches the node, but the scanner\'s sysinfo request '
                      'does not. The node is up; the path or its web server is unreachable from here.'),
    'stale': ('Stale entry', True, 'info',
              f'No successful poll in over {STALE_DAYS} days. A neighbor still lists it, but it has not '
              'answered in a long time.'),
    'not_routable': ('Not routable', True, 'info',
                     'Every neighbor that reports it says it is not currently routable.'),
    'behind_supernode': ('Behind a supernode', True, 'info',
                         'Only reported by supernodes; it belongs to another mesh.'),
    'never_resolves': ('Name does not resolve', True, 'warning',
                       'The poll fails at name resolution (DNS), before reaching the node.'),
    'intermittent': ('Intermittent', False, 'warning',
                     'Some polls in the window answered and some failed.'),
    'really_down': ('Down', False, 'critical',
                    'It answered recently and now nothing reaches it, including mesh probes.'),
}

# Causes where excluding the node from scanning removes pure noise.
EXCLUDABLE = {'legacy_tunnel_only', 'stale', 'placeholder'}


def _ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19].replace('T', ' '), TS_FMT)
    except ValueError:
        return None


def _in(values):
    return ','.join('?' for _ in values)


def gateway_outages(conn, start, end):
    """[(lost, restored_or_None)] starting-node outage windows overlapping [start, end)."""
    rows = conn.execute('''
        SELECT timestamp, event_type FROM events
        WHERE event_type IN (?, ?) AND timestamp < ?
        ORDER BY timestamp
    ''', (database.EVENT_GATEWAY_LOST, database.EVENT_GATEWAY_RESTORED, end)).fetchall()
    windows, lost = [], None
    for r in rows:
        if r['event_type'] == database.EVENT_GATEWAY_LOST:
            lost = lost or r['timestamp']
        elif lost:
            windows.append((lost, r['timestamp']))
            lost = None
    if lost:
        windows.append((lost, None))
    return [(a, b) for a, b in windows if b is None or b >= start]


def _in_outage(ts, outages):
    return any(a <= ts and (b is None or ts < b) for a, b in outages)


def classify(start, end, names=None, conn=None):
    """Classify every node with failed scanner polls in [start, end).

    ``start``/``end`` are 'YYYY-MM-DD HH:MM:SS' local strings. ``names``
    optionally limits the nodes. Returns {name: result} (see _result)."""
    if conn is None:
        with database.get_connection() as c:
            return classify(start, end, names, c)

    scope_sql, scope_params = '', []
    if names is not None:
        names = list(names)
        if not names:
            return {}
        scope_sql = f' AND node_name IN ({_in(names)})'
        scope_params = names

    # Every sample of nodes with any failure in the window: direct scanner
    # polls and neighbor-relayed mesh probes (a node the scanner no longer
    # queues, e.g. not routable, shows up only through failing probes).
    samples = {}
    for r in conn.execute(f'''
        SELECT node_name, timestamp, reachable, probe_via, fail_reason, reported_by, via_link_type
        FROM node_health_history
        WHERE timestamp >= ? AND timestamp < ?{scope_sql}
          AND node_name IN (SELECT node_name FROM node_health_history
                            WHERE timestamp >= ? AND timestamp < ? AND reachable = 0)
        ORDER BY timestamp
    ''', [start, end] + scope_params + [start, end]):
        samples.setdefault(r['node_name'], []).append(r)
    if not samples:
        return {}
    failing = list(samples)

    windows = maintenance.windows_between(start, end, conn)
    stats, reasons, via, fail_times = {}, {}, {}, {}
    for name, rows in samples.items():
        st = {'polls': 0, 'failed': 0, 'answered': 0, 'probe_ok': 0, 'probe_fail': 0,
              'first_fail': None, 'last_fail': None, 'last_ok_any': None, 'fails_since_ok': 0,
              'maint_failed': 0, 'maint_probe_fail': 0}
        intervals = maintenance.intervals_for(name, start, end, windows) if windows else []
        for r in rows:
            direct = r['probe_via'] is None
            if direct:
                st['polls'] += 1
            if r['reachable']:
                st['answered' if direct else 'probe_ok'] += 1
                st['last_ok_any'] = r['timestamp']
                st['fails_since_ok'] = 0
                continue
            # Failures inside a maintenance window are set aside entirely.
            if intervals and maintenance.ts_in_intervals(r['timestamp'], intervals):
                st['maint_failed' if direct else 'maint_probe_fail'] += 1
                continue
            st['failed' if direct else 'probe_fail'] += 1
            st['first_fail'] = st['first_fail'] or r['timestamp']
            st['last_fail'] = r['timestamp']
            st['fails_since_ok'] += 1
            fail_times.setdefault(name, []).append(r['timestamp'])
            # Failure details recorded at scan time (newer rows only).
            if direct and r['fail_reason']:
                d = reasons.setdefault(name, {})
                d[r['fail_reason']] = d.get(r['fail_reason'], 0) + 1
            if direct and r['reported_by']:
                via[name] = {'reported_by': r['reported_by'], 'link_type': r['via_link_type']}
        stats[name] = st

    # Last direct answer at or before the window end (history, then nodes.last_seen).
    last_ok = {r['node_name']: r['ts'] for r in conn.execute(f'''
        SELECT node_name, MAX(timestamp) AS ts FROM node_health_history
        WHERE probe_via IS NULL AND reachable = 1 AND timestamp < ?
          AND node_name IN ({_in(failing)})
        GROUP BY node_name
    ''', [end] + failing)}
    meta = {r['name']: dict(r) for r in conn.execute(f'''
        SELECT name, ip, last_seen, is_active, is_supernode FROM nodes WHERE name IN ({_in(failing)})
    ''', failing)}
    supernodes = {r['name'] for r in conn.execute('SELECT name FROM nodes WHERE is_supernode = 1')}

    reporters = {}
    for r in conn.execute(f'''
        SELECT source_node, target_node, link_type, status, routability_status, last_seen
        FROM links WHERE status != 'removed' AND target_node IN ({_in(failing)})
    ''', failing):
        reporters.setdefault(r['target_node'], []).append(dict(r))

    boundaries = database.get_scan_boundaries()
    outages = gateway_outages(conn, start, end)

    end_dt = _ts(end)
    out = {}
    for name in failing:
        s = stats[name]
        m = meta.get(name, {})
        last_success = max(filter(None, [last_ok.get(name), m.get('last_seen')]), default=None)
        reps = reporters.get(name, [])
        ctx = {
            'stats': s, 'meta': m, 'last_success': last_success, 'reporters': reps,
            'reasons': reasons.get(name, {}), 'via': via.get(name),
            'boundary': boundaries.get(name), 'end_dt': end_dt, 'supernodes': supernodes,
            'all_in_outage': bool(outages) and bool(fail_times.get(name)) and all(
                _in_outage(t, outages) for t in fail_times[name]),
        }
        cause, evidence = _cause(name, ctx)
        if cause:
            out[name] = _result(name, cause, evidence, ctx)
    return out


def _cause(name, ctx):
    s, reps, reasons = ctx['stats'], ctx['reporters'], ctx['reasons']
    ev = []
    if not s['failed'] and not s['probe_fail']:
        if s['maint_failed'] or s['maint_probe_fail']:
            return 'maintenance', [f'{s["maint_failed"]} poll(s) and {s["maint_probe_fail"]} probe(s) '
                                   'failed inside a maintenance window']
    # The scanner itself never failed and only a stray mesh probe did: not unreachable.
    if not s['failed'] and s['fails_since_ok'] < DOWN_AFTER_FAILS and (s['answered'] or s['probe_ok']):
        return None, []
    if re.fullmatch(r'[\d.]+', name):
        return 'starting_node_outage', [f'"{name}" is an IP fragment, recorded for the starting node '
                                        'by the scanner before the starting-node check existed']
    if ctx['all_in_outage']:
        return 'starting_node_outage', ['every failed poll fell inside a starting-node outage']
    if name.startswith('mac-'):
        return 'placeholder', [f'reported by {", ".join(sorted({r["source_node"] for r in reps})) or "a neighbor"} '
                               'without a hostname']
    if ctx['boundary'] == 'exclude':
        return 'excluded', ['scan boundary: exclude']

    types = {str(r['link_type'] or '').upper() for r in reps}
    via = ctx['via']
    via_type = str((via or {}).get('link_type') or '').upper()
    if (types and types <= LEGACY_TUNNEL_TYPES) or (not types and via_type in LEGACY_TUNNEL_TYPES):
        srcs = sorted({r['source_node'] for r in reps}) or [via['reported_by']]
        ev.append(f'reported only over legacy Tunnel links by {", ".join(srcs)}')
        if s['probe_ok']:
            ev.append(f'{s["probe_ok"]} mesh probe(s) from a neighbor reached it')
        return 'legacy_tunnel_only', ev

    if s['probe_ok'] and not s['answered']:
        ev.append(f'{s["probe_ok"]} mesh probe(s) reached it; {s["failed"]} direct poll(s) failed')
        if reasons:
            ev.append('poll failures: ' + _reason_text(reasons))
        return 'pings_no_http', ev

    if (s['answered'] or s['probe_ok']) and s['fails_since_ok'] >= DOWN_AFTER_FAILS:
        ev.append(f'answered until {s["last_ok_any"]}; the last {s["fails_since_ok"]} poll(s)/probe(s) failed')
        if reasons:
            ev.append('failures: ' + _reason_text(reasons))
        return 'really_down', ev

    if s['answered'] or s['probe_ok']:
        ev.append(f'{s["answered"]} of {s["polls"]} poll(s) answered'
                  + (f', {s["probe_ok"]} of {s["probe_ok"] + s["probe_fail"]} mesh probe(s)'
                     if s['probe_ok'] + s['probe_fail'] else ''))
        if reasons:
            ev.append('failures: ' + _reason_text(reasons))
        return 'intermittent', ev

    last = _ts(ctx['last_success'])
    end_dt = ctx['end_dt']
    if last is None or (end_dt and end_dt - last > timedelta(days=STALE_DAYS)):
        ev.append(f'last answered {ctx["last_success"] or "never"}')
        if reps:
            ev.append('still listed by ' + ', '.join(sorted({r['source_node'] for r in reps})))
        return 'stale', ev

    first_fail = _ts(s['first_fail'])
    if first_fail and first_fail - last <= timedelta(hours=RECENT_HOURS):
        ev.append(f'last answered {ctx["last_success"]}, failing since {s["first_fail"]}')
        if s['probe_fail']:
            ev.append(f'{s["probe_fail"]} mesh probe(s) also failed')
        if reasons:
            ev.append('failures: ' + _reason_text(reasons))
        return 'really_down', ev

    if reps and all(r['routability_status'] == 'not_routable' for r in reps):
        ev.append('not routable per ' + ', '.join(sorted({r['source_node'] for r in reps})))
        ev.append(f'last answered {ctx["last_success"]}')
        return 'not_routable', ev
    if reps and all(r['source_node'] in ctx['supernodes'] for r in reps):
        ev.append('reported only by ' + ', '.join(sorted({r['source_node'] for r in reps})))
        return 'behind_supernode', ev
    if reasons and reasons.get('dns', 0) * 2 > sum(reasons.values()):
        ev.append('failures: ' + _reason_text(reasons))
        return 'never_resolves', ev

    ev.append(f'last answered {ctx["last_success"]}')
    if reasons:
        ev.append('failures: ' + _reason_text(reasons))
    return 'really_down', ev


def _reason_text(reasons):
    return ', '.join(f'{k.replace("_", " ")} ×{v}' for k, v in
                     sorted(reasons.items(), key=lambda kv: -kv[1]))


def _result(name, cause, evidence, ctx):
    label, explained, severity, detail = CAUSES[cause]
    s = ctx['stats']
    return {
        'name': name,
        'cause': cause,
        'cause_label': label,
        'explained': explained,
        'severity': severity,
        'detail': detail,
        'evidence': evidence,
        'polls': s['polls'] or 0,
        'failed_polls': (s['failed'] or 0) + (s['maint_failed'] if cause == 'maintenance' else 0),
        'maintenance_failed_polls': s['maint_failed'] or 0,
        'answered_polls': s['answered'] or 0,
        'probe_ok': s['probe_ok'] or 0,
        'probe_fail': s['probe_fail'] or 0,
        'first_fail': s['first_fail'],
        'last_fail': s['last_fail'],
        'last_success': ctx['last_success'],
        'fail_reasons': ctx['reasons'],
        'reported_by': sorted({f"{r['source_node']} ({r['link_type']})" for r in ctx['reporters']}),
        'boundary': ctx['boundary'],
        'can_exclude': cause in EXCLUDABLE and ctx['boundary'] != 'exclude',
        'ip': ctx['meta'].get('ip'),
    }


def summarize(results):
    """Counts for a classify() result."""
    by_cause = {}
    for r in results.values():
        by_cause[r['cause']] = by_cause.get(r['cause'], 0) + 1
    explained = [r for r in results.values() if r['explained']]
    real = [r for r in results.values() if not r['explained']]
    return {
        'nodes': len(results),
        'really_down_nodes': sum(1 for r in real if r['cause'] == 'really_down'),
        'intermittent_nodes': sum(1 for r in real if r['cause'] == 'intermittent'),
        'explained_nodes': len(explained),
        'real_failed_polls': sum(r['failed_polls'] for r in real),
        'explained_failed_polls': sum(r['failed_polls'] for r in explained),
        'by_cause': by_cause,
        'causes': {k: {'label': v[0], 'explained': v[1], 'severity': v[2]} for k, v in CAUSES.items()},
    }


def report_rows(results):
    """classify() results as a list: unexplained first, then by severity and failures."""
    rank = {'critical': 0, 'warning': 1, 'info': 2}
    return sorted(results.values(), key=lambda r: (r['explained'], rank.get(r['severity'], 3),
                                                   -(r['failed_polls'] + r['probe_fail']), r['name']))


def report(start, end, names=None):
    """Full classifier payload for the API."""
    results = classify(start, end, names)
    return {'window': {'start': start, 'end': end}, 'summary': summarize(results),
            'nodes': report_rows(results)}


def node_cause(name, hours=RECENT_HOURS):
    """Cause for one node over the last ``hours`` (None if no failed polls)."""
    end = datetime.now()
    start = end - timedelta(hours=hours)
    return classify(start.strftime(TS_FMT), end.strftime(TS_FMT), [name]).get(name)

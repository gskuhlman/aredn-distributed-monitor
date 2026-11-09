"""
Node uptime % (Status page "Node Uptime", Reports tab node table).

The share of the checked time each node was up (running and answering), from
scanner polls and mesh probes in `node_health_history`. Every stretch between
two samples of a node (and from the window start to its first sample) is one of:

- down: a short gap (<= `SHORT_GAP`) next to a failed scanner poll, even when
  the node's uptime shows it kept running (on but unreachable), or the time
  before a reboot (the uptime counter went backwards) its new uptime doesn't
  cover;
- up: between two answers, covered by an answered poll's uptime (a poll at t
  reporting `uptime_seconds` u proves the node ran over [t - u, t]: it bridges
  gaps nobody polled) or a short gap;
- unchecked: a longer gap no uptime covers, or next to a failed poll (nobody
  polled it: scanner stopped, starting node unreachable, node out of scan range
  or no longer listed).

Uptime % = up / (up + down); unchecked time is left out, and so is time in a
maintenance window covering the node (planned). A node is judged up to its own
last poll (`checked_until`; `stale` when that is well before the newest scan):
after that it wasn't checked, which is not the same as down (CLAUDE.md status
rules). Older rows hold whole days of uptime only (the parser dropped the
hours until it was fixed), which is still a valid lower bound.

In `report()`, `uptime_pct` is None when the node's failed polls are explained
(unreachable.py: placeholder, tunnel-only, pings but no HTTP ...), since then
the monitor can't tell. `answered`/`polls` count direct scanner polls. Read-only.
"""

from bisect import bisect_right
from datetime import datetime, timedelta

import database
import maintenance
import reports
import unreachable as unreachable_causes

TS_FMT = '%Y-%m-%d %H:%M:%S'
# Two answered polls this close together: up in between.
SHORT_GAP = timedelta(minutes=30)
# A node not polled within this of the newest scan is reported as stale.
RECENT_POLL = timedelta(minutes=15)


def _ts(value):
    if isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:19].replace('T', ' '), TS_FMT)


def _merge(spans):
    """Sorted, non-overlapping union of (start, end) spans."""
    out = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _split(a, b, merged, starts):
    """Parts of [a, b] inside / outside the merged spans, as (inside, outside)."""
    inside, outside = [], []
    cur = a
    k = max(0, bisect_right(starts, a) - 1)
    while cur < b and k < len(merged):
        ms, me = merged[k]
        if me <= cur:
            k += 1
            continue
        if ms >= b:
            break
        if ms > cur:
            outside.append((cur, ms))
            cur = ms
        end = min(me, b)
        inside.append((cur, end))
        cur = end
        k += 1
    if cur < b:
        outside.append((cur, b))
    return inside, outside


def _minus(segments, holes):
    """Total seconds of the segments outside the holes."""
    total = 0.0
    for a, b in segments:
        length = (b - a).total_seconds()
        for hs, he in holes:
            lo, hi = max(a, hs), min(b, he)
            if hi > lo:
                length -= (hi - lo).total_seconds()
        total += max(0.0, length)
    return total


def _planned(s, e, conn):
    """Maintenance windows as (all-scope holes, {node: holes})."""
    holes, holes_all = {}, []
    try:
        for w in maintenance.windows_between(s, e, conn):
            hole = (_ts(w['start_time']), _ts(w['end_time']) + maintenance.grace())
            if w['scope'] == 'all':
                holes_all.append(hole)
            for n in maintenance.covered_nodes(w):
                holes.setdefault(n, []).append(hole)
    except Exception:   # maintenance is optional context; never break the report
        return [], {}
    return holes_all, holes


def judge(samples, s, holes=()):
    """Up / down / unchecked seconds for one node.

    `samples`: time-ordered (t, ok, uptime_seconds or None, direct) tuples.
    Returns (up_s, down_s, unchecked_s, reboots)."""
    points = []             # (t, ok, reboot)
    up_spans = []
    reboots = 0
    prev_up = None
    for t, ok, up_s, direct in samples:
        if not ok and not direct:
            continue        # a neighbour's failed probe says little
        reboot = False
        if ok and direct and up_s is not None:
            up_s = float(up_s)
            up_spans.append((t - timedelta(seconds=up_s), t))
            # A reboot is the counter going backwards (as database.py's check).
            if prev_up is not None and up_s + 30 < prev_up:
                reboot = True
                reboots += 1
            prev_up = up_s
        points.append((t, bool(ok), reboot))
    if not points:
        return 0.0, 0.0, 0.0, reboots
    merged = _merge(up_spans)
    starts = [a for a, _ in merged]
    up, down, unchecked = [], [], []
    # Window start to the first sample: up where an uptime covers it.
    inside, outside = _split(s, points[0][0], merged, starts)
    up += inside
    unchecked += outside
    for (a, a_ok, _), (b, b_ok, b_reboot) in zip(points, points[1:]):
        if not a_ok or not b_ok:
            # Not answering: down even when its uptime shows it kept running
            # (on but unreachable); a long silence next to it can't be judged.
            (down if b - a <= SHORT_GAP else unchecked).append((a, b))
            continue
        inside, outside = _split(a, b, merged, starts)
        up += inside
        if not outside:
            continue
        if b - a > SHORT_GAP:
            unchecked += outside    # nobody looked for a long while
        elif b_reboot:
            down += outside
        else:
            up += outside
    holes = list(holes)
    return _minus(up, holes), _minus(down, holes), _minus(unchecked, holes), reboots


def node_uptime(start, end, names=None, conn=None):
    """{node: {uptime_pct, up_minutes, down_minutes, unchecked_minutes, reboots,
    checked_until, stale, answered, polls}}. `names` limits the nodes (None =
    every node with samples in the window)."""
    if conn is None:
        with database.get_connection() as c:
            return node_uptime(start, end, names, c)
    s, e = _ts(start), _ts(end)
    args = [s.strftime(TS_FMT), e.strftime(TS_FMT)]
    name_sql = ''
    if names is not None:
        names = sorted(names)
        if not names:
            return {}
        name_sql = f" AND node_name IN ({','.join('?' * len(names))})"
        args += names
    rows = conn.execute(
        'SELECT node_name, timestamp, reachable, uptime_seconds, probe_via FROM node_health_history '
        f'WHERE timestamp >= ? AND timestamp < ?{name_sql} ORDER BY node_name, timestamp', args).fetchall()
    if not rows:
        return {}
    newest = conn.execute('SELECT MAX(timestamp) FROM node_health_history WHERE timestamp >= ? AND timestamp < ?',
                          args[:2]).fetchone()[0]
    newest = _ts(newest) if newest else e
    holes_all, holes = _planned(s, e, conn)

    by_node = {}
    for name, ts, reachable, up_s, via in rows:
        by_node.setdefault(name, []).append((_ts(ts), bool(reachable), up_s, via is None))

    out = {}
    for name, samples in by_node.items():
        up_s, down_s, unchecked_s, reboots = judge(samples, s, holes_all + holes.get(name.lower(), []))
        judged = up_s + down_s
        last_poll = samples[-1][0]
        out[name] = {
            'uptime_pct': round(100.0 * up_s / judged, 2) if judged > 0 else None,
            'up_minutes': round(up_s / 60, 1),
            'down_minutes': round(down_s / 60, 1),
            'unchecked_minutes': round(unchecked_s / 60, 1),
            'reboots': reboots,
            'checked_until': last_poll.strftime(TS_FMT),
            'stale': newest - last_poll > RECENT_POLL,
            'answered': sum(1 for _, ok, _, direct in samples if ok and direct),
            'polls': sum(1 for *_, direct in samples if direct),
        }
    return out


def report(start, end, scope='all', link_type='all', search=''):
    """Status page Node Uptime: every in-scope node, lowest uptime first."""
    s, e = _ts(start).strftime(TS_FMT), _ts(end).strftime(TS_FMT)
    with database.get_connection() as conn:
        names = reports.resolve_scope(conn, scope, link_type, search)
        up = node_uptime(s, e, names, conn)
        causes = unreachable_causes.classify(s, e, names, conn)
        meta = {r[0]: r for r in conn.execute('SELECT name, is_active, last_seen FROM nodes')}
        infra = {r[0] for r in conn.execute('SELECT node_name FROM infrastructure_nodes')}
    rows = []
    for name, u in up.items():
        c = causes.get(name) or {}
        # Explained failures (tunnel-only, pings but no HTTP, placeholder ...)
        # aren't the node being down: the monitor can't tell.
        if c.get('explained'):
            u['uptime_pct'] = None
        rows.append({'name': name, **u, 'cause': c.get('cause'), 'cause_label': c.get('cause_label'),
                     'is_infrastructure': name in infra,
                     'is_active': bool(meta[name][1]) if name in meta else None,
                     'last_seen': meta[name][2] if name in meta else None})
    rows.sort(key=lambda r: (r['uptime_pct'] is None, r['uptime_pct'] if r['uptime_pct'] is not None else 0, r['name']))
    known = [r['uptime_pct'] for r in rows if r['uptime_pct'] is not None]
    return {
        'start': s, 'end': e,
        'nodes': rows,
        'summary': {
            'nodes': len(rows),
            'average_pct': round(sum(known) / len(known), 2) if known else None,
            'below_99': sum(1 for p in known if p < 99),
            'below_90': sum(1 for p in known if p < 90),
            'unknown': sum(1 for r in rows if r['uptime_pct'] is None),
        },
    }

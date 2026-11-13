"""
Link swimlanes (Reports tab "Link Swimlanes").

One lane per link (both directions together) across the Reports window, each
bucket coloured:
  - down: the link was down (node-reported `down` in `link_state_log`) for at
    least half the bucket;
  - flapping: two or more node-reported down transitions in the bucket;
  - degraded: blocked by LQM, or (RF) mean link quality below DEGRADED_QUALITY;
  - unknown: only the scanner lost the node (`scanner_unreachable` /
    scanner-inferred down), so nothing is known about the link itself;
  - good: seen up; none: no data (link not known yet / already gone).
Markers: reboots of either end, radio/frequency/firmware changes, change-log
entries, and maintenance windows covering either end.

Read-only; filtered like the Reports tab. Worst lanes first.
"""

from collections import defaultdict
from datetime import datetime, timedelta

import database
import maintenance
import reports

TS_FMT = '%Y-%m-%d %H:%M:%S'
DEGRADED_QUALITY = 50
MAX_LANES = 150
STATES = ('good', 'degraded', 'flapping', 'down', 'unknown', 'none')
MARKER_EVENTS = {'node_reboot': 'reboot', 'radio_config_change': 'config',
                 'frequency_change': 'config', 'firmware_change': 'config'}


def _parse(ts):
    return datetime.strptime(ts[:19], TS_FMT)


def build(start, end, scope='all', link_type='all', search='', limit=MAX_LANES):
    start_dt, end_dt = reports.parse_window(start, end)
    size = reports.bucket_seconds(start_dt, end_dt)
    n = max(1, int((end_dt - start_dt).total_seconds() // size))
    start_s, end_s = start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)

    with database.get_connection() as conn:
        cur = conn.cursor()
        names = reports.resolve_scope(conn, scope, link_type, search)
        lt_sql, lt_params = reports._link_type_sql(link_type)
        scope_sql = reports._link_scope_sql(names)

        types, current = {}, {}
        for src, tgt, ltype, first, last, status in cur.execute(f'''
                SELECT source_node, target_node, link_type, first_seen, last_seen, status FROM links
                WHERE 1 = 1 {lt_sql} {scope_sql}''', lt_params):
            pair = tuple(sorted((src, tgt)))
            types[pair] = (ltype or '').upper()
            current[pair] = (first, last, status)

        # State log: the state before the window, then transitions inside it.
        log = defaultdict(list)
        for src, tgt, ltype, ts, state, origin in cur.execute(f'''
                SELECT source_node, target_node, link_type, timestamp, state, COALESCE(origin, 'node_reported')
                FROM link_state_log
                WHERE timestamp < ? AND timestamp >= ? {lt_sql} {scope_sql}
                ORDER BY timestamp''', [end_s, (start_dt - timedelta(days=7)).strftime(TS_FMT)] + lt_params):
            pair = tuple(sorted((src, tgt)))
            types.setdefault(pair, (ltype or '').upper())
            log[pair].append((ts, state, origin))

        quality = defaultdict(lambda: [0.0, 0])
        blocked = set()
        b = reports._bucket_expr()
        if link_type in ('all', 'rf'):
            for src, tgt, idx, q, nq, blk in cur.execute(f'''
                    SELECT source_node, target_node, {b} AS bi, SUM(COALESCE(quality, lq)),
                           COUNT(COALESCE(quality, lq)), MAX(COALESCE(blocked, 0))
                    FROM link_history
                    WHERE timestamp >= ? AND timestamp < ? AND UPPER(link_type) = 'RF' {scope_sql}
                    GROUP BY source_node, target_node, bi''', (start_s, size, start_s, end_s)):
                if idx is None or not 0 <= idx < n:
                    continue
                pair = tuple(sorted((src, tgt)))
                types.setdefault(pair, 'RF')
                if nq:
                    quality[(pair, idx)][0] += q
                    quality[(pair, idx)][1] += nq
                if blk:
                    blocked.add((pair, idx))

        events = defaultdict(list)
        for ts, etype, who, details in cur.execute(f'''
                SELECT timestamp, event_type, node_name, details FROM events
                WHERE timestamp >= ? AND timestamp < ? AND event_type IN ({reports._in(MARKER_EVENTS)})''',
                [start_s, end_s] + list(MARKER_EVENTS)):
            events[who].append({'time': ts, 'kind': MARKER_EVENTS[etype],
                                'title': f'{who}: {etype.replace("_", " ")}', 'detail': details or ''})
        for ts, who, cat, desc, src in cur.execute('''
                SELECT timestamp, node_name, category, description, source FROM change_log
                WHERE timestamp >= ? AND timestamp < ?''', (start_s, end_s)):
            if src != 'auto':
                events[who].append({'time': ts, 'kind': 'change', 'title': f'{who}: {cat} change',
                                    'detail': desc or ''})
        windows = maintenance.windows_between(start_dt, end_dt, conn=conn)

    def idx_of(dt):
        return int((dt - start_dt).total_seconds() // size)

    lanes = []
    for pair, ltype in types.items():
        cur_info = current.get(pair)
        entries = log.get(pair, [])
        before = [e for e in entries if e[0] < start_s]
        inside = [e for e in entries if e[0] >= start_s]
        has_quality = ltype != 'DTD' and any((pair, i) in quality for i in range(n))
        if not inside and not has_quality and not (cur_info and cur_info[1] and cur_info[1] >= start_s):
            continue

        # Reconstruct the state over time: 'up', 'down', 'unknown'.
        def classify(state, origin):
            if state in ('up', 'unblocked'):
                return 'up'
            if state == 'scanner_unreachable' or origin == 'scanner_inferred':
                return 'unknown'
            if state in ('down', 'blocked'):
                return 'down'
            return 'up'
        state = classify(*before[-1][1:]) if before else None
        if state is None and cur_info and cur_info[0] and cur_info[0] < start_s:
            state = 'up'
        down_s = [0.0] * n
        unknown_s = [0.0] * n
        known_s = [0.0] * n
        flaps = [0] * n
        t = start_dt
        timeline = [(_parse(ts), classify(st, origin), st, origin) for ts, st, origin in inside]
        timeline.append((end_dt, None, None, None))
        for at, new_state, raw, origin in timeline:
            # Spread the [t, at) interval in the current state over buckets.
            if state is not None:
                a = t
                while a < at:
                    i = idx_of(a)
                    if i >= n:
                        break
                    b_end = min(at, start_dt + timedelta(seconds=size * (i + 1)))
                    sec = (b_end - a).total_seconds()
                    known_s[i] += sec
                    if state == 'down':
                        down_s[i] += sec
                    elif state == 'unknown':
                        unknown_s[i] += sec
                    a = b_end
            if new_state is None:
                break
            if raw == 'down' and origin == 'node_reported':
                i = idx_of(at)
                if 0 <= i < n:
                    flaps[i] += 1
            state, t = new_state, at

        cells = []
        counts = defaultdict(int)
        for i in range(n):
            # A DtD-cabled pair can keep an old RF tracker; its quality means nothing.
            q = quality.get((pair, i)) if ltype != 'DTD' else None
            qv = q[0] / q[1] if q and q[1] else None
            if flaps[i] >= 2:
                c = 'flapping'
            elif down_s[i] >= size / 2:
                c = 'down'
            elif (pair, i) in blocked or (qv is not None and qv < DEGRADED_QUALITY):
                c = 'degraded'
            elif qv is not None or (known_s[i] and unknown_s[i] < size / 2):
                c = 'good'
            elif unknown_s[i]:
                c = 'unknown'
            else:
                c = 'none'
            if c == 'good' and down_s[i]:
                c = 'degraded'
            cells.append(c)
            counts[c] += 1
        if counts['none'] == n:
            continue
        marks = []
        for who in pair:
            for e in events.get(who, []):
                i = idx_of(_parse(e['time']))
                if 0 <= i < n:
                    marks.append({**e, 'bucket': i})
        maint = [False] * n
        for who in pair:
            for lo, hi in maintenance.intervals_for(who, start_dt, end_dt, windows=windows):
                for i in range(n):
                    b0 = start_dt + timedelta(seconds=size * i)
                    if b0 < hi and b0 + timedelta(seconds=size) > lo:
                        maint[i] = True
        badness = counts['down'] * 3 + counts['flapping'] * 3 + counts['degraded']
        # Never up in the window (a radio that is off, a removed link): listed
        # after the lanes that changed, hidden by default in the UI.
        never_up = not counts['good'] and not counts['degraded'] and not counts['flapping']
        lanes.append({'a': pair[0], 'b': pair[1], 'type': ltype or '?', 'cells': cells,
                      'counts': dict(counts), 'marks': sorted(marks, key=lambda m: m['time']),
                      'maint': maint, 'flaps': sum(flaps), 'badness': badness, 'never_up': never_up,
                      'current': (cur_info or (None, None, None))[2]})

    lanes.sort(key=lambda l: (l['never_up'], -l['badness'], -l['flaps'], l['type'] != 'RF', l['a'], l['b']))
    return {
        'window': {'start': start_s, 'end': end_s, 'bucket_seconds': size,
                   'buckets': [(start_dt + timedelta(seconds=size * i)).strftime(TS_FMT) for i in range(n)]},
        'lanes': lanes[:limit], 'total': len(lanes), 'never_up': sum(1 for l in lanes if l['never_up']),
        'states': STATES, 'degraded_quality': DEGRADED_QUALITY,
    }

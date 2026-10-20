"""
Health score + "what changed?" (Reports tab "Health Score").

Scores every node and RF link 0-100 per time bucket over a window, so a drop
stands out, and lists what happened around a drop.

Node score per bucket (components that have data are averaged by weight):
  - reachability (50): successful scanner polls / scanner polls
    (`node_health_history`, direct polls only; neighbour probes are ignored);
  - RF quality (50): mean link quality the node reports for its RF neighbours
    (`link_history` scan samples);
  - minus penalties: a reboot (`node_reboot`) 20, each dropped link 5 (max 20).

Link score per bucket (both directions of an RF pair together):
  - quality (50): mean link quality;
  - SNR (30): median SNR mapped 5 dB -> 0 .. 30 dB -> 100;
  - ping (20): 100 - mean ping loss, when pings exist;
  - minus 15 per down/blocked transition the nodes reported (max 45).

Buckets that fall inside a maintenance window covering the node / either end
are flagged (`maint`) and left out of the min/avg and drop detection.

`what_changed()` collects, for a node (or a pair) and a time range: new and
lost neighbours, reboots (events and uptime going backwards), radio/frequency/
firmware changes, change-log entries, noise jumps, one-way alarms, route
changes, starting-node outages, maintenance windows and notes with a time.

Read-only. Timestamps are local 'YYYY-MM-DD HH:MM:SS' like the other tables.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import database
import maintenance
import reports

TS_FMT = '%Y-%m-%d %H:%M:%S'
NODE_WEIGHTS = {'reach': 0.5, 'rf': 0.5}
LINK_WEIGHTS = {'quality': 0.5, 'snr': 0.3, 'ping': 0.2}
REBOOT_PENALTY = 20
DROP_PENALTY, DROP_PENALTY_MAX = 5, 20
TRANSITION_PENALTY, TRANSITION_PENALTY_MAX = 15, 45
SNR_FLOOR, SNR_GOOD = 5.0, 30.0
DROP_MIN_POINTS = 15        # smallest score drop worth listing
MAX_ITEMS = 300

# Event types listed by what_changed, with the kind they are shown as.
CHANGE_EVENTS = {
    'node_reboot': 'reboot',
    'radio_config_change': 'config', 'frequency_change': 'config', 'firmware_change': 'config',
    'noise_jump': 'noise', 'noise_cleared': 'noise',
    'one_way_link': 'one_way', 'one_way_link_cleared': 'one_way',
    'route_changed': 'route', 'route_detour': 'route', 'route_restored': 'route',
    'link_new': 'neighbor', 'link_removed': 'neighbor',
    'link_dropped': 'link', 'link_restored': 'link', 'link_blocked': 'link', 'link_unblocked': 'link',
    'node_offline': 'reachability', 'node_online': 'reachability', 'node_degraded': 'reachability',
    'gateway_lost': 'starting_node', 'gateway_restored': 'starting_node',
}
KIND_LABELS = {
    'reboot': 'Reboot', 'config': 'Radio / firmware change', 'change_log': 'Change log',
    'noise': 'Noise', 'one_way': 'One-way link', 'route': 'Route', 'neighbor': 'Neighbour',
    'link': 'Link state', 'reachability': 'Reachability', 'starting_node': 'Starting node',
    'maintenance': 'Maintenance', 'note': 'Note',
}


def _bucket_size(start_dt, end_dt):
    span = (end_dt - start_dt).total_seconds()
    floor = 900 if span <= 12 * 3600 else 3600
    return max(floor, reports.bucket_seconds(start_dt, end_dt))


def _clamp(v):
    return max(0.0, min(100.0, v))


def _weighted(parts, weights):
    have = {k: v for k, v in parts.items() if v is not None}
    if not have:
        return None
    total = sum(weights[k] for k in have)
    return sum(v * weights[k] for k, v in have.items()) / total


def _snr_score(snr):
    if snr is None:
        return None
    return _clamp((snr - SNR_FLOOR) / (SNR_GOOD - SNR_FLOOR) * 100)


def _bucket_times(start_dt, size, n):
    return [(start_dt + timedelta(seconds=size * i)).strftime(TS_FMT) for i in range(n)]


def _summarize(series, maint):
    """now/avg/min and the largest drop from a series of (score|None)."""
    vals = [(i, s) for i, s in enumerate(series) if s is not None and not maint[i]]
    if not vals:
        return {'now': None, 'avg': None, 'min': None, 'drop': None}
    best_drop, peak = None, None
    for i, s in vals:
        if peak is None or s > peak[1]:
            peak = (i, s)
        d = peak[1] - s
        if d >= DROP_MIN_POINTS and (best_drop is None or d > best_drop[2]):
            best_drop = (peak[0], i, d)
    last = [s for _, s in vals][-1]
    out = {'now': round(last), 'avg': round(sum(s for _, s in vals) / len(vals)),
           'min': round(min(s for _, s in vals)), 'drop': None}
    if best_drop:
        out['drop'] = {'from_index': best_drop[0], 'index': best_drop[1], 'points': round(best_drop[2]),
                       'from_score': round(series[best_drop[0]]), 'to_score': round(series[best_drop[1]])}
    return out


def _maint_flags(names, start_dt, size, n, windows):
    flags = [False] * n
    if not windows:
        return flags
    for name in names:
        for lo, hi in maintenance.intervals_for(name, start_dt, start_dt + timedelta(seconds=size * n),
                                                windows=windows):
            for i in range(n):
                b0 = start_dt + timedelta(seconds=size * i)
                if b0 < hi and b0 + timedelta(seconds=size) > lo:
                    flags[i] = True
    return flags


# ------------------------------------------------------------------ nodes --

def _node_scores(conn, start_s, start_dt, size, n, names):
    cur = conn.cursor()
    b = reports._bucket_expr()
    scope = reports._node_scope_sql(names, 'node_name')
    polls = defaultdict(lambda: [0, 0])
    for name, idx, ok, total in cur.execute(f'''
            SELECT node_name, {b} AS bi, SUM(CASE WHEN reachable = 1 THEN 1 ELSE 0 END), COUNT(*)
            FROM node_health_history
            WHERE timestamp >= ? AND timestamp < ? AND probe_via IS NULL {scope}
            GROUP BY node_name, bi''', (start_s, size, start_s, _end(start_dt, size, n))):
        if 0 <= idx < n:
            polls[(name, idx)] = [ok, total]
    rf = {}
    scope = reports._node_scope_sql(names, 'source_node')
    for name, idx, q in cur.execute(f'''
            SELECT source_node, {b} AS bi, AVG(COALESCE(quality, lq))
            FROM link_history
            WHERE timestamp >= ? AND timestamp < ? AND UPPER(link_type) = 'RF'
              AND COALESCE(sample_type, 'scan') = 'scan' {scope}
            GROUP BY source_node, bi''', (start_s, size, start_s, _end(start_dt, size, n))):
        if 0 <= idx < n and q is not None:
            rf[(name, idx)] = q
    penalty = defaultdict(float)
    drops = defaultdict(int)
    scope = reports._node_scope_sql(names, 'node_name')
    for name, idx, etype, count in cur.execute(f'''
            SELECT node_name, {b} AS bi, event_type, COUNT(*)
            FROM events
            WHERE timestamp >= ? AND timestamp < ? AND event_type IN ('node_reboot', 'link_dropped') {scope}
            GROUP BY node_name, bi, event_type''', (start_s, size, start_s, _end(start_dt, size, n))):
        if not 0 <= idx < n:
            continue
        if etype == 'node_reboot':
            penalty[(name, idx)] += REBOOT_PENALTY * count
        else:
            drops[(name, idx)] += count
    for key, count in drops.items():
        penalty[key] += min(DROP_PENALTY_MAX, DROP_PENALTY * count)

    nodes = defaultdict(dict)
    for key in set(polls) | set(rf):
        name, idx = key
        ok, total = polls.get(key, (0, 0))
        reach = ok / total * 100 if total else None
        score = _weighted({'reach': reach, 'rf': rf.get(key)}, NODE_WEIGHTS)
        if score is None:
            continue
        nodes[name][idx] = {'score': _clamp(score - penalty.get(key, 0)), 'reach': reach,
                            'rf': rf.get(key), 'polls': total, 'penalty': penalty.get(key, 0)}
    return nodes


def _end(start_dt, size, n):
    return (start_dt + timedelta(seconds=size * n)).strftime(TS_FMT)


# ------------------------------------------------------------------ links --

def _link_scores(conn, start_s, start_dt, size, n, names):
    cur = conn.cursor()
    b = reports._bucket_expr()
    scope = reports._link_scope_sql(names)
    samples = defaultdict(lambda: {'q': [], 'snr': [], 'loss': []})
    for src, tgt, idx, q, snr, loss in cur.execute(f'''
            SELECT source_node, target_node, {b} AS bi, COALESCE(quality, lq), snr, ping_loss
            FROM link_history
            WHERE timestamp >= ? AND timestamp < ? AND UPPER(link_type) = 'RF' {scope}''',
            (start_s, size, start_s, _end(start_dt, size, n))):
        if not 0 <= idx < n:
            continue
        s = samples[(tuple(sorted((src, tgt))), idx)]
        if q is not None:
            s['q'].append(q)
        if snr is not None:
            s['snr'].append(snr)
        if loss is not None:
            s['loss'].append(loss)
    trans = defaultdict(int)
    for src, tgt, idx, count in cur.execute(f'''
            SELECT source_node, target_node, {b} AS bi, COUNT(*)
            FROM link_state_log
            WHERE timestamp >= ? AND timestamp < ? AND state IN ('down', 'blocked')
              AND COALESCE(origin, 'node_reported') = 'node_reported'
              AND UPPER(COALESCE(link_type, 'RF')) = 'RF' {scope}
            GROUP BY source_node, target_node, bi''', (start_s, size, start_s, _end(start_dt, size, n))):
        if 0 <= idx < n:
            trans[(tuple(sorted((src, tgt))), idx)] += count

    links = defaultdict(dict)
    for key in set(samples) | set(trans):
        pair, idx = key
        s = samples.get(key) or {'q': [], 'snr': [], 'loss': []}
        parts = {'quality': sum(s['q']) / len(s['q']) if s['q'] else None,
                 'snr': _snr_score(median(s['snr'])) if s['snr'] else None,
                 'ping': 100 - sum(s['loss']) / len(s['loss']) if s['loss'] else None}
        score = _weighted(parts, LINK_WEIGHTS)
        pen = min(TRANSITION_PENALTY_MAX, TRANSITION_PENALTY * trans.get(key, 0))
        if score is None:
            if not pen:
                continue
            score = 50.0
        links[pair][idx] = {'score': _clamp(score - pen), 'quality': parts['quality'],
                            'snr': median(s['snr']) if s['snr'] else None,
                            'loss': sum(s['loss']) / len(s['loss']) if s['loss'] else None,
                            'transitions': trans.get(key, 0)}
    return links


# ----------------------------------------------------------------- report --

def scores(start, end, kind='node', scope='all', link_type='all', search='', limit=MAX_ITEMS):
    """Per-bucket health scores for nodes (kind='node') or RF links ('link')."""
    start_dt, end_dt = reports.parse_window(start, end)
    size = _bucket_size(start_dt, end_dt)
    n = max(1, int((end_dt - start_dt).total_seconds() // size))
    start_s = start_dt.strftime(TS_FMT)
    with database.get_connection() as conn:
        names = reports.resolve_scope(conn, scope, link_type, search)
        infra = {r[0] for r in conn.execute('SELECT node_name FROM infrastructure_nodes')}
        windows = maintenance.windows_between(start_dt, end_dt, conn=conn)
        data = (_node_scores if kind == 'node' else _link_scores)(conn, start_s, start_dt, size, n, names)

    items = []
    for key, buckets in data.items():
        members = [key] if kind == 'node' else list(key)
        maint = _maint_flags(members, start_dt, size, n, windows)
        series = [buckets[i]['score'] if i in buckets else None for i in range(n)]
        summary = _summarize(series, maint)
        if summary['now'] is None and summary['avg'] is None:
            continue
        item = {'series': [None if s is None else round(s) for s in series], 'maint': maint, **summary,
                'infrastructure': any(m in infra for m in members)}
        if kind == 'node':
            item['name'] = key
        else:
            item['a'], item['b'] = key
        if summary['drop']:
            i = summary['drop']['index']
            item['drop']['at'] = (start_dt + timedelta(seconds=size * i)).strftime(TS_FMT)
            item['drop']['detail'] = {k: (round(v, 1) if isinstance(v, float) else v)
                                      for k, v in buckets[i].items()}
        # A node that never answered in the window has no drop to explain
        # (legacy tunnels, stale entries: see Unreachable Nodes); list it last.
        item['never_ok'] = summary['avg'] == 0 and summary['min'] == 0
        items.append(item)
    # Biggest drops first, then the lowest minimum; never-answering nodes last.
    items.sort(key=lambda x: (x['never_ok'], -((x['drop'] or {}).get('points') or 0),
                              x['min'] if x['min'] is not None else 101))
    return {
        'kind': kind,
        'window': {'start': start_s, 'end': end_dt.strftime(TS_FMT), 'bucket_seconds': size,
                   'buckets': _bucket_times(start_dt, size, n)},
        'items': items[:limit],
        'total': len(items),
        'with_drop': sum(1 for x in items if x['drop']),
        'weights': {'node': NODE_WEIGHTS, 'link': LINK_WEIGHTS},
        'penalties': {'reboot': REBOOT_PENALTY, 'link_drop': DROP_PENALTY, 'link_drop_max': DROP_PENALTY_MAX,
                      'transition': TRANSITION_PENALTY, 'transition_max': TRANSITION_PENALTY_MAX},
    }


# ----------------------------------------------------------- what changed --

def _mentions(details, names):
    text = (details or '').lower()
    return any(n.lower() in text for n in names)


def what_changed(node, start, end, peer=None, pad_minutes=None):
    """Everything that happened to a node (or a pair) between start and end.

    The range is widened by pad_minutes (default: a quarter of its length,
    at least 15 minutes) on both sides, since a cause often precedes the drop.
    Each item: {time, kind, label, title, detail, node, severity, source}.
    """
    start_dt, end_dt = reports.parse_window(start, end)
    pad = timedelta(minutes=pad_minutes if pad_minutes is not None
                    else max(15, (end_dt - start_dt).total_seconds() / 60 / 4))
    lo, hi = start_dt - pad, end_dt + pad
    lo_s, hi_s = lo.strftime(TS_FMT), hi.strftime(TS_FMT)
    focus = [n for n in (node, peer) if n]
    items = []

    def add(ts, kind, title, detail='', who=None, severity='info', source='event'):
        items.append({'time': ts, 'kind': kind, 'label': KIND_LABELS.get(kind, kind), 'title': title,
                      'detail': detail or '', 'node': who, 'severity': severity or 'info', 'source': source})

    with database.get_connection() as conn:
        cur = conn.cursor()
        # RF/DtD neighbours during the range: their reboots and config changes count too.
        neighbours = set()
        for src, tgt in cur.execute(f'''
                SELECT DISTINCT source_node, target_node FROM link_history
                WHERE timestamp >= ? AND timestamp < ?
                  AND (source_node IN ({reports._in(focus)}) OR target_node IN ({reports._in(focus)}))''',
                [lo_s, hi_s] + focus * 2):
            neighbours.update((src, tgt))
        neighbours -= set(focus)

        types = list(CHANGE_EVENTS)
        for ts, etype, who, details, sev in cur.execute(f'''
                SELECT timestamp, event_type, node_name, details, severity FROM events
                WHERE timestamp >= ? AND timestamp < ? AND event_type IN ({reports._in(types)})
                ORDER BY timestamp''', [lo_s, hi_s] + types):
            kind = CHANGE_EVENTS[etype]
            if kind == 'starting_node':
                add(ts, kind, etype.replace('_', ' '), details, who, sev)
            elif who in focus or _mentions(details, focus):
                add(ts, kind, etype.replace('_', ' '), details, who, sev)
            elif who in neighbours and kind in ('reboot', 'config', 'noise'):
                add(ts, kind, f'neighbour {who}: {etype.replace("_", " ")}', details, who, sev)

        # Reboots the event log missed: uptime going backwards between polls.
        for name in focus:
            prev = None
            for ts, up in cur.execute('''
                    SELECT timestamp, uptime_seconds FROM node_health_history
                    WHERE node_name = ? AND timestamp >= ? AND timestamp < ? AND uptime_seconds IS NOT NULL
                      AND probe_via IS NULL ORDER BY timestamp''', (name, lo_s, hi_s)):
                if prev and up + 60 < prev[1]:
                    if not any(i['kind'] == 'reboot' and i['node'] == name and abs(
                            (datetime.strptime(i['time'], TS_FMT) - datetime.strptime(ts, TS_FMT))
                            .total_seconds()) < 1800 for i in items):
                        add(ts, 'reboot', 'uptime reset', f'{name} uptime went from {prev[1] // 60} min '
                            f'to {up // 60} min', name, 'warning', 'node_health')
                prev = (ts, up)

        # Neighbours first seen / last seen inside the range.
        for src, tgt, ltype, first, last in cur.execute(f'''
                SELECT source_node, target_node, link_type, first_seen, last_seen FROM links
                WHERE (source_node IN ({reports._in(focus)}) OR target_node IN ({reports._in(focus)}))''',
                focus * 2):
            other = tgt if src in focus else src
            if first and lo_s <= first < hi_s:
                add(first, 'neighbor', f'new {ltype} neighbour {other}', f'{src} → {tgt} first seen', src,
                    'info', 'links')

        # Change log entries.
        for ts, who, peer_node, cat, desc, src in cur.execute(f'''
                SELECT timestamp, node_name, peer_node, category, description, source FROM change_log
                WHERE timestamp >= ? AND timestamp < ?
                  AND (node_name IN ({reports._in(focus)}) OR peer_node IN ({reports._in(focus)}))''',
                [lo_s, hi_s] + focus * 2):
            if src == 'auto':
                continue  # already listed from its event
            add(ts, 'change_log', f'{cat} change on {who}', desc, who, 'info', 'change_log')

        # Starting-node outages recorded by collector_health.
        try:
            rows = cur.execute('''
                SELECT timestamp, error_class, error FROM collector_health
                WHERE timestamp >= ? AND timestamp < ? AND ok = 0 ORDER BY timestamp''',
                (lo_s, hi_s)).fetchall()
        except Exception:
            rows = []
        if rows:
            add(rows[0][0], 'starting_node', f'starting node unreachable ({len(rows)} failed check(s))',
                rows[0][1] or rows[0][2] or '', None, 'warning', 'collector_health')

        # Notes with a time.
        try:
            for ts, who, peer_node, text in cur.execute(f'''
                    SELECT COALESCE(event_time, created_at), node_name, peer_node, text FROM notes
                    WHERE COALESCE(event_time, created_at) >= ? AND COALESCE(event_time, created_at) < ?
                      AND (node_name IN ({reports._in(focus)}) OR peer_node IN ({reports._in(focus)}))''',
                    [lo_s, hi_s] + focus * 2):
                add(ts, 'note', f'note on {who}' + (f' ↔ {peer_node}' if peer_node else ''), text, who, 'info',
                    'notes')
        except Exception:
            pass

        windows = maintenance.windows_between(lo, hi, conn=conn)
    for w in windows:
        if any(maintenance._node_covered(w, n) for n in focus):
            add(w['start_time'], 'maintenance', 'maintenance window', maintenance.describe_window(w), None,
                'info', 'maintenance')

    items.sort(key=lambda i: i['time'])
    counts = defaultdict(int)
    for i in items:
        counts[i['kind']] += 1
    return {'node': node, 'peer': peer, 'start': start_dt.strftime(TS_FMT), 'end': end_dt.strftime(TS_FMT),
            'searched': {'start': lo_s, 'end': hi_s}, 'neighbours': sorted(neighbours),
            'items': items, 'counts': dict(counts), 'labels': KIND_LABELS}

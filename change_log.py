"""
Change log and before/after comparison (Changes tab).

Records channel, power, antenna, firmware and other changes to a node (or a
specific link), either entered by the operator or imported from the
scanner's events (frequency_change, radio_config_change, firmware_change).
`compare()` then measures every RF link of the changed node over equal
windows before and after the change, so an operator can see whether, say,
moving a sector's channel actually improved its clients' LQ, SNR and noise.

Read-only over link_history / link_state_log / node_health_history; writes
only to the change_log table.
"""

import re
from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
CATEGORIES = ('channel', 'power', 'antenna', 'firmware', 'other')

# Events imported as automatic change entries, and the category they map to.
AUTO_EVENT_TYPES = {
    database.EVENT_FREQUENCY_CHANGE: 'channel',
    database.EVENT_RADIO_CONFIG_CHANGE: 'channel',
    database.EVENT_FIRMWARE_CHANGE: 'firmware',
}

# (key, label, better direction, absolute threshold, relative threshold)
# direction: +1 = higher is better, -1 = lower is better, 0 = informational.
METRICS = (
    ('lq', 'LQ (%)', 1, 5, None),
    ('quality', 'Quality (%)', 1, 5, None),
    ('snr', 'SNR (dB)', 1, 2, None),
    ('noise', 'Noise (dBm)', -1, 2, None),
    ('signal', 'Signal (dBm)', 0, 2, None),
    ('retry_rate', 'TX retries (%)', -1, 2, None),
    ('fail_rate', 'TX failures (%)', -1, 1, None),
    ('tx_bitrate', 'TX bitrate (Mbps)', 1, None, 0.10),
    ('rx_bitrate', 'RX bitrate (Mbps)', 1, None, 0.10),
    ('ping_avg', 'Ping avg (ms)', -1, 2, 0.20),
    ('ping_loss', 'Ping loss (%)', -1, 2, None),
    ('flaps', 'Peer downs', -1, 2, None),
)
# Metrics that decide a link's overall verdict.
KEY_METRICS = ('lq', 'quality', 'snr', 'noise', 'retry_rate', 'fail_rate', 'ping_loss', 'flaps')


def _now():
    return datetime.now().strftime(TS_FMT)


def _parse_ts(value):
    if not value:
        return None
    value = str(value).strip().replace('T', ' ')
    for fmt in (TS_FMT, '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value[:19] if fmt == TS_FMT else value, fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ CRUD ----

def _event_category(event_type, details):
    if event_type == database.EVENT_RADIO_CONFIG_CHANGE and details:
        if re.search(r'\bmode\b|\bssid\b', details, re.I):
            return 'other'
    return AUTO_EVENT_TYPES.get(event_type, 'other')


def sync_from_events():
    """Import new change-type events as 'auto' entries (idempotent)."""
    types = list(AUTO_EVENT_TYPES)
    placeholders = ','.join('?' * len(types))
    with database.get_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(f'''
            SELECT e.id, e.timestamp, e.event_type, e.node_name, e.details
            FROM events e
            LEFT JOIN change_log c ON c.event_id = e.id
            WHERE e.event_type IN ({placeholders}) AND c.id IS NULL AND e.node_name IS NOT NULL
        ''', types).fetchall()
        for r in rows:
            cursor.execute('''
                INSERT OR IGNORE INTO change_log
                    (timestamp, node_name, category, description, source, event_id)
                VALUES (?, ?, ?, ?, 'auto', ?)
            ''', (r['timestamp'], r['node_name'], _event_category(r['event_type'], r['details']),
                  r['details'], r['id']))
        return len(rows)


def list_changes(node=None, hours=None, limit=500):
    """Changes newest first, optionally for one node (as node or peer)."""
    sync_from_events()
    where, params = ["source != 'deleted'"], []
    if node:
        where.append('(node_name = ? OR peer_node = ?)')
        params += [node, node]
    if hours:
        where.append('timestamp >= ?')
        params.append((datetime.now() - timedelta(hours=hours)).strftime(TS_FMT))
    sql = 'SELECT * FROM change_log'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY timestamp DESC, id DESC LIMIT ?'
    params.append(limit)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_change(change_id):
    with database.get_connection() as conn:
        row = conn.execute('SELECT * FROM change_log WHERE id = ?', (change_id,)).fetchone()
        return dict(row) if row else None


def add_change(node_name, description, category='other', timestamp=None,
               peer_node=None, author=None):
    """Record a manual change. Raises ValueError on bad input."""
    node_name = (node_name or '').strip().lower()
    if not node_name:
        raise ValueError('node_name is required')
    category = (category or 'other').strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f'category must be one of {", ".join(CATEGORIES)}')
    when = _parse_ts(timestamp) if timestamp else datetime.now()
    if not when:
        raise ValueError('timestamp must look like YYYY-MM-DD HH:MM')
    peer_node = (peer_node or '').strip().lower() or None
    with database.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO change_log (timestamp, node_name, peer_node, category, description, source, author)
            VALUES (?, ?, ?, ?, ?, 'manual', ?)
        ''', (when.strftime(TS_FMT), node_name, peer_node, category,
              (description or '').strip() or None, (author or '').strip() or None))
        change_id = cursor.lastrowid
    return get_change(change_id)


def delete_change(change_id):
    """Delete an entry. Auto entries stay deleted (their event_id is kept as a
    tombstone so sync doesn't re-import them)."""
    change = get_change(change_id)
    if not change:
        return False
    with database.get_connection() as conn:
        if change.get('event_id'):
            conn.execute("UPDATE change_log SET source = 'deleted' WHERE id = ?", (change_id,))
        else:
            conn.execute('DELETE FROM change_log WHERE id = ?', (change_id,))
    return True


# ------------------------------------------------------------- compare ----

def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _counter_rate(rows, num_key, den_key):
    """Rate from cumulative counters, summing positive deltas only so a
    counter reset (reboot) doesn't produce a huge negative step."""
    num = den = 0
    prev = None
    for r in rows:
        n, d = r.get(num_key), r.get(den_key)
        if n is None or d is None:
            prev = None
            continue
        if prev is not None:
            dn, dd = n - prev[0], d - prev[1]
            if dn >= 0 and dd > 0:
                num += dn
                den += dd
        prev = (n, d)
    return round(100.0 * num / den, 2) if den else None


def _direction_rows(conn, source, target, start, end):
    return [dict(r) for r in conn.execute('''
        SELECT timestamp, sample_type, quality, snr, signal, lq, tx_packets, tx_fail,
               tx_retries, tx_bitrate, rx_bitrate, ping_avg, ping_loss
        FROM link_history
        WHERE source_node = ? AND target_node = ? AND link_type = 'RF'
          AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    ''', (source, target, start, end)).fetchall()]


def _window_stats(conn, source, target, start, end):
    rows = _direction_rows(conn, source, target, start, end)
    scan = [r for r in rows if r['sample_type'] != 'incident']
    pings = [r for r in rows if r['ping_avg'] is not None or r['ping_loss'] is not None]
    noise = [r['signal'] - r['snr'] for r in scan
             if r['signal'] is not None and r['snr'] is not None]
    flaps = conn.execute('''
        SELECT COUNT(*) FROM link_state_log
        WHERE source_node = ? AND target_node = ? AND state = 'down'
          AND origin = 'node_reported' AND timestamp >= ? AND timestamp < ?
    ''', (source, target, start, end)).fetchone()[0]
    return {
        'samples': len(scan),
        'lq': _avg(r['lq'] for r in scan),
        'quality': _avg(r['quality'] for r in scan),
        'snr': _avg(r['snr'] for r in scan),
        'noise': _avg(noise),
        'signal': _avg(r['signal'] for r in scan),
        'retry_rate': _counter_rate(scan, 'tx_retries', 'tx_packets'),
        'fail_rate': _counter_rate(scan, 'tx_fail', 'tx_packets'),
        'tx_bitrate': _avg(r['tx_bitrate'] for r in scan),
        'rx_bitrate': _avg(r['rx_bitrate'] for r in scan),
        'ping_avg': _avg(r['ping_avg'] for r in pings),
        'ping_loss': _avg(r['ping_loss'] for r in pings),
        'flaps': flaps if scan or flaps else None,
    }


def _verdict(key, before, after):
    """better / worse / same / n/a for one metric."""
    spec = next(m for m in METRICS if m[0] == key)
    _, _, direction, abs_t, rel_t = spec
    if before is None or after is None:
        return 'n/a'
    delta = after - before
    if direction == 0:
        return 'info'
    threshold = abs_t or 0
    if rel_t is not None:
        threshold = max(threshold, abs(before) * rel_t)
    if abs(delta) < threshold or delta == 0:
        return 'same'
    return 'better' if (delta > 0) == (direction > 0) else 'worse'


def _series(conn, source, target, start, end, change_at, side, buckets=96):
    """Bucketed LQ/SNR/noise/quality keyed by hours from the window start, so
    the before and after windows overlay on one axis."""
    rows = [r for r in _direction_rows(conn, source, target, start, end)
            if r['sample_type'] != 'incident']
    if not rows:
        return []
    window_s = (_parse_ts(end) - _parse_ts(start)).total_seconds()
    size = max(60.0, window_s / buckets)
    origin = _parse_ts(start)
    groups = {}
    for r in rows:
        t = _parse_ts(r['timestamp'])
        if not t:
            continue
        b = int((t - origin).total_seconds() // size)
        groups.setdefault(b, []).append(r)
    out = []
    for b in sorted(groups):
        g = groups[b]
        out.append({
            'h': round(b * size / 3600.0, 3),
            'lq': _avg(r['lq'] for r in g),
            'quality': _avg(r['quality'] for r in g),
            'snr': _avg(r['snr'] for r in g),
            'noise': _avg(r['signal'] - r['snr'] for r in g
                          if r['signal'] is not None and r['snr'] is not None),
        })
    return out


def _reachability(conn, node, start, end):
    row = conn.execute('''
        SELECT COUNT(*) n, SUM(reachable) ok FROM node_health_history
        WHERE node_name = ? AND timestamp >= ? AND timestamp < ?
    ''', (node, start, end)).fetchone()
    return round(100.0 * (row['ok'] or 0) / row['n'], 1) if row['n'] else None


def _rf_peers(conn, node, start, end):
    rows = conn.execute('''
        SELECT DISTINCT CASE WHEN source_node = ? THEN target_node ELSE source_node END AS peer
        FROM link_history
        WHERE link_type = 'RF' AND (source_node = ? OR target_node = ?)
          AND timestamp >= ? AND timestamp < ?
    ''', (node, node, node, start, end)).fetchall()
    return sorted(r['peer'] for r in rows if r['peer'] and r['peer'] != node)


def compare(node, change_time, window_hours=24, peer=None):
    """Before/after stats for every RF link of `node` (or just node<->peer)
    over equal windows on each side of `change_time`."""
    at = _parse_ts(change_time) if not isinstance(change_time, datetime) else change_time
    if not at:
        raise ValueError('invalid change time')
    window_hours = max(1, min(int(window_hours or 24), 24 * 14))
    now = datetime.now()
    before_start = at - timedelta(hours=window_hours)
    after_end = min(at + timedelta(hours=window_hours), now)
    after_hours = max(0.0, (after_end - at).total_seconds() / 3600.0)
    bs, cs, ae = (before_start.strftime(TS_FMT), at.strftime(TS_FMT), after_end.strftime(TS_FMT))
    # Compare the after window against the same length of "before" when the
    # after window hasn't fully elapsed yet, so averages are like for like.
    before_cmp_start = (at - timedelta(hours=after_hours)).strftime(TS_FMT) if after_hours < window_hours else bs

    with database.get_connection() as conn:
        peers = [peer] if peer else _rf_peers(conn, node, bs, ae)
        links = []
        for p in peers:
            directions = []
            for src, dst in ((node, p), (p, node)):
                before = _window_stats(conn, src, dst, before_cmp_start, cs)
                after = _window_stats(conn, src, dst, cs, ae)
                if not before['samples'] and not after['samples']:
                    continue
                metrics = []
                for key, label, direction, _, _ in METRICS:
                    b, a = before.get(key), after.get(key)
                    metrics.append({
                        'key': key, 'label': label, 'before': b, 'after': a,
                        'delta': round(a - b, 2) if a is not None and b is not None else None,
                        'verdict': _verdict(key, b, a),
                    })
                directions.append({
                    'source': src, 'target': dst,
                    'before_samples': before['samples'], 'after_samples': after['samples'],
                    'metrics': metrics,
                    'series': {
                        'before': _series(conn, src, dst, before_cmp_start, cs, at, 'before'),
                        'after': _series(conn, src, dst, cs, ae, at, 'after'),
                    },
                })
            if not directions:
                continue
            verdicts = [m['verdict'] for d in directions for m in d['metrics'] if m['key'] in KEY_METRICS]
            better, worse = verdicts.count('better'), verdicts.count('worse')
            overall = ('better' if better > worse else 'worse' if worse > better
                       else 'same' if better or worse or verdicts.count('same') else 'n/a')
            links.append({'peer': p, 'directions': directions, 'better': better,
                          'worse': worse, 'verdict': overall})

        reach = {n: {'before': _reachability(conn, n, before_cmp_start, cs),
                     'after': _reachability(conn, n, cs, ae)}
                 for n in [node] + [l['peer'] for l in links]}

    order = {'worse': 0, 'better': 1, 'same': 2, 'n/a': 3}
    links.sort(key=lambda l: (order.get(l['verdict'], 9), l['peer']))
    summary = {v: sum(1 for l in links if l['verdict'] == v) for v in ('better', 'worse', 'same', 'n/a')}
    return {
        'node': node, 'peer': peer, 'change_time': cs, 'window_hours': window_hours,
        'before_start': before_cmp_start, 'after_end': ae,
        'after_hours_available': round(after_hours, 2),
        'partial': after_hours < window_hours,
        'links': links, 'reachability': reach, 'summary': summary,
        'metrics': [{'key': m[0], 'label': m[1], 'direction': m[2]} for m in METRICS],
    }


def compare_change(change_id, window_hours=24):
    change = get_change(change_id)
    if not change:
        return None
    result = compare(change['node_name'], change['timestamp'], window_hours, change.get('peer_node'))
    result['change'] = change
    return result

"""
Reachability tape per node (TODO #76): one slot per poll interval over the
window (heard / degraded / missed / none), from the scanner's own polls in
node_health_history, for the Nodes list, node page and wall display.

`report(hours, scope...)`: per node a slot string (`h` heard, `d` degraded =
answered bare sysinfo or slowly, `m` missed, `-` no poll; a slot shows its
worst poll), heard %, last poll, and the callsign group (name up to the first
'-'); groups with their own counts; headline "N of M on the air" (nodes whose
latest poll answered, of the nodes polled in the window). Nodes with only
explained failures (unreachable.py: placeholder, excluded, behind a
supernode...) are listed with `explained`. Read-only.
"""

from datetime import datetime, timedelta

import config
import database
import reports

TS_FMT = '%Y-%m-%d %H:%M:%S'
RANK = {'-': 0, 'h': 1, 'd': 2, 'm': 3}
MAX_SLOTS = 288


def slot_minutes(hours):
    """Slot length: the scan interval, but no more than MAX_SLOTS slots."""
    poll = max(1, round(config.POLL_INTERVAL / 60))
    return max(poll, -(-hours * 60 // MAX_SLOTS))


def callsign(name):
    head = (name or '').split('-')[0].upper()
    return head if head and head[0].isalpha() and any(c.isdigit() for c in head) else 'OTHER'


def build(rows, start, slots, minutes):
    """{node: {'tape', 'heard', 'degraded', 'missed', 'last', 'last_ok'}} from
    (node, timestamp, reachable, degraded) rows, oldest first."""
    t0 = datetime.strptime(start, TS_FMT)
    out = {}
    for node, ts, ok, deg in rows:
        d = out.setdefault(node, {'tape': ['-'] * slots, 'heard': 0, 'degraded': 0, 'missed': 0,
                                  'last': None, 'last_ok': None})
        i = int((datetime.strptime(ts[:19], TS_FMT) - t0).total_seconds() // (minutes * 60))
        state = 'm' if not ok else 'd' if deg else 'h'
        d['missed' if state == 'm' else 'degraded' if state == 'd' else 'heard'] += 1
        if 0 <= i < slots and RANK[state] > RANK[d['tape'][i]]:
            d['tape'][i] = state
        d['last'], d['last_ok'] = ts, bool(ok)
    for d in out.values():
        d['tape'] = ''.join(d['tape'])
        polls = d['heard'] + d['degraded'] + d['missed']
        d['polls'] = polls
        d['heard_pct'] = round(100 * (d['heard'] + d['degraded']) / polls, 1) if polls else None
    return out


def report(hours=24, scope='all', link_type='all', search='', node=None, now=None):
    hours = max(1, min(int(hours or 24), 24 * 31))
    now = now or datetime.now()
    minutes = slot_minutes(hours)
    slots = -(-hours * 60 // minutes)
    start_dt = now - timedelta(minutes=minutes * slots)
    start = start_dt.strftime(TS_FMT)
    with database.get_connection() as conn:
        allowed = {node} if node else reports.resolve_scope(conn, scope, link_type, search)
        rows = conn.execute('''SELECT node_name, timestamp, reachable, degraded FROM node_health_history
                               WHERE timestamp >= ? AND probe_via IS NULL ORDER BY timestamp''', (start,)).fetchall()
        meta = {r['name']: dict(r) for r in conn.execute('SELECT name, is_active, description FROM nodes')}
    rows = [tuple(r) for r in rows if allowed is None or r[0] in allowed]
    tapes = build(rows, start, slots, minutes)
    explained = {}
    try:
        import unreachable
        for name, r in unreachable.classify(start, now.strftime(TS_FMT), sorted(tapes)).items():
            if r.get('explained'):
                explained[name] = r.get('cause_label') or r.get('cause')
    except Exception:
        pass
    infra = database.get_infrastructure_nodes()
    nodes, groups = [], {}
    for name, d in tapes.items():
        if name.startswith('mac-'):
            continue
        item = dict(d, name=name, callsign=callsign(name), is_infrastructure=name in infra,
                    site=infra.get(name), explained=explained.get(name))
        nodes.append(item)
        g = groups.setdefault(item['callsign'], {'callsign': item['callsign'], 'nodes': 0, 'on_air': 0})
        g['nodes'] += 1
        g['on_air'] += 1 if d['last_ok'] else 0
    nodes.sort(key=lambda n: (n['callsign'] == 'OTHER', n['callsign'], n['name']))
    on_air = sum(1 for n in nodes if n['last_ok'])
    return {'generated_at': now.strftime(TS_FMT), 'start': start, 'end': now.strftime(TS_FMT), 'hours': hours,
            'slot_minutes': minutes, 'slots': slots, 'nodes': nodes,
            'groups': sorted(groups.values(), key=lambda g: (g['callsign'] == 'OTHER', g['callsign'])),
            'headline': {'on_air': on_air, 'polled': len(nodes),
                         'text': f'{on_air} of {len(nodes)} on the air'}}

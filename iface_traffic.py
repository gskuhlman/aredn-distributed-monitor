"""
Interface traffic history (TODO #80). Each fast SSH run (ssh_collect) also
reads /proc/net/dev and which devices carry wan / lan / wifi / dtdlink
(`uci -q get network.<n>.device`); `store()` keeps one row per interface
(bytes and packets each way, cumulative) in SQLite `ssh_iface_samples`
(retention HISTORY_RETENTION_HOURS, cleaned with the other SSH samples).

`report(node, hours)`: per interface its role (WAN, LAN, mesh RF, DtD,
WireGuard / legacy tunnel with the peer node from the node's LQM tracker
`device`), rates per interval (counter resets skipped), the window's totals
and peak, and a series for the chart ("how much went over the MMC tunnel this
week"). VLAN sub-interfaces and bridges members are kept; lo is skipped.
"""

import json
from datetime import datetime, timedelta

import config
import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
SKIP = ('lo',)
EXTRA_LABELS = {'wireguard': 'WireGuard tunnel', 'tun': 'Legacy tunnel', 'port': 'Ethernet port', 'other': 'Other'}


def _st():
    import ssh_traffic   # imports ssh_collect, which imports this module
    return ssh_traffic


def role_label(role):
    return dict(_st().ROLE_LABELS, **EXTRA_LABELS).get(role, role)

_tables_ready = False


def ensure_tables(conn=None):
    global _tables_ready
    if _tables_ready:
        return
    if conn is None:
        with database.get_connection() as c:
            return ensure_tables(c)
    conn.execute('''CREATE TABLE IF NOT EXISTS ssh_iface_samples (
        node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, role TEXT,
        rx_bytes INTEGER, rx_packets INTEGER, tx_bytes INTEGER, tx_packets INTEGER,
        PRIMARY KEY (node_name, timestamp, iface))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ssh_iface_node ON ssh_iface_samples(node_name, iface, timestamp)')
    _tables_ready = True


SCRIPT = '''echo "{mark} netdev"; cat /proc/net/dev
echo "{mark} roles"; for n in wan lan wifi dtdlink; do echo "$n $(uci -q get network.$n.device)"; done
'''


def parse_netdev(text):
    """{iface: (rx_bytes, rx_packets, tx_bytes, tx_packets)} from /proc/net/dev."""
    _, out = _st().parse_dev('0\n' + (text or ''))
    return out


def store(conn, node, now_s, sections):
    """Store the netdev section of one run (sections = ssh_collect.split_sections)."""
    ensure_tables(conn)
    netdev = next((b for k, _, b in sections if k == 'netdev'), None)
    if netdev is None:
        return 0
    roles = _st().parse_roles(next((b for k, _, b in sections if k == 'roles'), ''))
    rows = []
    for iface, (rxb, rxp, txb, txp) in parse_netdev(netdev).items():
        if iface in SKIP:
            continue
        rows.append((node, now_s, iface, _st().iface_role(iface, roles), rxb, rxp, txb, txp))
    conn.executemany('INSERT OR REPLACE INTO ssh_iface_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
    return len(rows)


def cleanup(conn, cutoff):
    ensure_tables(conn)
    return conn.execute('DELETE FROM ssh_iface_samples WHERE timestamp < ?', (cutoff,)).rowcount


def tunnel_peers(conn, node):
    """{device: peer node} from the node's tunnel trackers (LQM `device`)."""
    out = {}
    for r in conn.execute('SELECT target_node, link_type, raw_tracker FROM links WHERE source_node = ?', (node,)):
        try:
            t = json.loads(r['raw_tracker'] or '{}')
        except ValueError:
            continue
        if t.get('device') and (r['link_type'] or '').upper() not in ('RF', 'DTD'):
            out[t['device']] = r['target_node']
    return out


def rates(rows):
    """[(t, rx_bps, tx_bps, rx_bytes, tx_bytes)] per interval; resets skipped."""
    out = []
    for p, q in zip(rows, rows[1:]):
        dt = (datetime.strptime(q['timestamp'], TS_FMT) - datetime.strptime(p['timestamp'], TS_FMT)).total_seconds()
        if dt <= 0 or None in (p['rx_bytes'], q['rx_bytes'], p['tx_bytes'], q['tx_bytes']):
            continue
        drx, dtx = q['rx_bytes'] - p['rx_bytes'], q['tx_bytes'] - p['tx_bytes']
        if drx < 0 or dtx < 0:
            continue
        out.append((q['timestamp'], round(8 * drx / dt), round(8 * dtx / dt), drx, dtx))
    return out


def report(node, hours=24, now=None):
    ensure_tables()
    now = now or datetime.now()
    start = (now - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT * FROM ssh_iface_samples WHERE node_name = ? AND timestamp >= ?
                                                 ORDER BY iface, timestamp''', (node, start))]
        peers = tunnel_peers(conn, node)
    by_iface = {}
    for r in rows:
        by_iface.setdefault(r['iface'], []).append(r)
    out = []
    for iface, rs in by_iface.items():
        rt = rates(rs)
        role = rs[-1]['role']
        item = {'iface': iface, 'role': role, 'role_label': role_label(role), 'peer': peers.get(iface),
                'samples': len(rs), 'rx_total': sum(x[3] for x in rt), 'tx_total': sum(x[4] for x in rt),
                'rx_bps_avg': None, 'tx_bps_avg': None,
                'rx_bps_peak': max((x[1] for x in rt), default=None), 'tx_bps_peak': max((x[2] for x in rt), default=None),
                'series': [{'t': t, 'rx_bps': a, 'tx_bps': b} for t, a, b, _, _ in rt]}
        span = sum((datetime.strptime(q['timestamp'], TS_FMT) - datetime.strptime(p['timestamp'], TS_FMT)).total_seconds()
                   for p, q in zip(rs, rs[1:]) if q['rx_bytes'] is not None and p['rx_bytes'] is not None
                   and q['rx_bytes'] >= p['rx_bytes'])
        if span > 0:
            item['rx_bps_avg'] = round(8 * item['rx_total'] / span)
            item['tx_bps_avg'] = round(8 * item['tx_total'] / span)
        out.append(item)
    order = {'wan': 0, 'wireguard': 1, 'tun': 1, 'wifi': 2, 'dtdlink': 3, 'lan': 4}
    out.sort(key=lambda i: (order.get(i['role'], 5), -(i['rx_total'] + i['tx_total']), i['iface']))
    return {'node': node, 'hours': hours, 'generated_at': now.strftime(TS_FMT), 'interfaces': out,
            'tunnels': [i for i in out if i['role'] in ('wireguard', 'tun')],
            'notes': [] if rows else ['No interface samples yet: they are read on each SSH collection '
                                      f'(every {config.SSH_INTERVAL_MINUTES} min) of infrastructure nodes.']}

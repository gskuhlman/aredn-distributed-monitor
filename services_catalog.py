"""
Services catalog and devices (TODO #81).

Every advertised service grouped by category (cameras, weather, phones,
Winlink, chat, time, maps, files, monitoring, info, other) from the name,
link and protocol; `sync()` after each committed scan keeps first / last seen
per (node, service, ip) in SQLite `service_seen`, so the catalog shows a "new
today" badge (first seen in the last 24 h) and services that left: not
advertised any more while their node still answers the scanner, kept for
LEFT_DAYS. Service-check state (service_check.py) is added when probed.

Devices: the hosts plugged into each node, from the mesh host list (sysinfo
`hosts`, SQLite `mesh_hosts`, phones.py) attached to the node whose LAN block
holds their address (phones.owner_node), with first / last seen. The node's
own names (`dtdlink.`, `mid*.`, the node name) are left out. Read-only apart
from `service_seen`.
"""

import re
from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
LEFT_DAYS = 7
NEW_HOURS = 24
CATEGORIES = [
    ('cameras', 'Cameras', r'cam|camera|rtsp|ipcam|video|nvr|blue ?iris|frigate|webcam|zoneminder|motion'),
    ('phones', 'Phones & PBX', r'phone|pbx|sip|voip|asterisk|ext\b|extension|direct ip|yealink|grandstream|polycom|cisco'),
    ('weather', 'Weather', r'weather|\bwx|davis|ambient|weewx|cumulus|lightning|rain'),
    ('winlink', 'Winlink & email', r'winlink|\brms\b|\bpat\b|mail|smtp|imap|pop3'),
    ('chat', 'Chat & messaging', r'chat|meshchat|irc|mattermost|matrix|element|jabber|xmpp|teams|rocket\.?chat|meshtastic'),
    ('time', 'Time', r'\bntp\b|time server|\btime\b|gps time|chrony'),
    ('maps', 'Maps & tracking', r'map|aprs|tracker|gis|tile'),
    ('files', 'Files & sharing', r'file|nextcloud|owncloud|ftp|share|samba|smb|drop|library|kiwix|wiki'),
    ('monitoring', 'Monitoring', r'grafana|prometheus|monitor|status|uptime|librenms|zabbix|nagios|meshwatch|commdeck|noc|log'),
    ('streaming', 'Audio & streaming', r'stream|jellyfin|plex|radio|scanner|sdr|icecast|audio|music|broadcastify'),
    ('remote', 'Remote access', r'ssh|vnc|rdp|guacamole|remote|console|terminal'),
]
_COMPILED = [(k, label, re.compile(p, re.I)) for k, label, p in CATEGORIES]
LABELS = dict((k, label) for k, label, _ in CATEGORIES) | {'other': 'Other'}

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS service_seen (
            node_name TEXT NOT NULL, name TEXT NOT NULL, ip TEXT NOT NULL DEFAULT '',
            protocol TEXT, link TEXT, category TEXT, first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL,
            PRIMARY KEY (node_name, name, ip))''')
    _tables_ready = True


def category(name, link=None, protocol=None):
    text = ' '.join(str(x or '') for x in (name, link, protocol))
    for key, _, rx in _COMPILED:
        if rx.search(text):
            return key
    return 'other'


def sync(now=None):
    """Note every currently advertised service (after a committed scan)."""
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = conn.execute('SELECT node_name, name, protocol, link, ip FROM services').fetchall()
        for r in rows:
            cat = category(r['name'], r['link'], r['protocol'])
            conn.execute('''INSERT INTO service_seen (node_name, name, ip, protocol, link, category, first_seen, last_seen)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(node_name, name, ip) DO UPDATE SET last_seen = excluded.last_seen,
                              protocol = excluded.protocol, link = excluded.link, category = excluded.category''',
                         (r['node_name'], r['name'], r['ip'] or '', r['protocol'], r['link'], cat, now_s, now_s))
        conn.execute('DELETE FROM service_seen WHERE last_seen < ?',
                     (((now or datetime.now()) - timedelta(days=60)).strftime(TS_FMT),))
    return len(rows)


def catalog(now=None, search=''):
    ensure_tables()
    now = now or datetime.now()
    new_cut = (now - timedelta(hours=NEW_HOURS)).strftime(TS_FMT)
    left_cut = (now - timedelta(days=LEFT_DAYS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        current = {(r['node_name'], r['name'], r['ip'] or ''): dict(r)
                   for r in conn.execute('SELECT * FROM services')}
        seen = {(r['node_name'], r['name'], r['ip']): dict(r) for r in conn.execute('SELECT * FROM service_seen')}
        # Services already there when tracking started are not "new".
        tracking_since = conn.execute('SELECT MIN(first_seen) FROM service_seen').fetchone()[0] or ''
        nodes = {r['name']: dict(r) for r in conn.execute('SELECT name, is_active, last_seen FROM nodes')}
        checks = {}
        try:
            for r in conn.execute('SELECT node_name, service_name, ok, alarm, last_error, last_check FROM service_checks'):
                d = dict(r)
                d['state'] = 'down' if d['alarm'] else 'up' if d['ok'] else 'failing' if d['last_check'] else None
                checks[(d['node_name'], d['service_name'])] = d
        except Exception:
            pass
    q = (search or '').lower()
    services, left = [], []
    for key, s in current.items():
        node, name, ip = key
        info = seen.get(key) or {}
        cat = category(name, s.get('link'), s.get('protocol'))
        chk = checks.get((node, name)) or {}
        item = {'node': node, 'name': name, 'ip': ip or None, 'protocol': s.get('protocol'), 'link': s.get('link'),
                'category': cat, 'category_label': LABELS[cat], 'first_seen': info.get('first_seen'),
                'new': bool(info.get('first_seen') and info['first_seen'] >= new_cut
                            and info['first_seen'] > tracking_since),
                'node_active': bool((nodes.get(node) or {}).get('is_active')),
                'check_state': chk.get('state'), 'check_detail': chk.get('last_error')}
        if q and q not in f"{node} {name} {ip} {s.get('link') or ''}".lower():
            continue
        services.append(item)
    for key, info in seen.items():
        if key in current or info['last_seen'] < left_cut:
            continue
        node_seen = (nodes.get(info['node_name']) or {}).get('last_seen') or ''
        # Left = the node answered after the service was last advertised.
        if node_seen <= info['last_seen']:
            continue
        if q and q not in f"{info['node_name']} {info['name']} {info['ip']}".lower():
            continue
        left.append({'node': info['node_name'], 'name': info['name'], 'ip': info['ip'] or None,
                     'category': info['category'], 'category_label': LABELS.get(info['category'], 'Other'),
                     'first_seen': info['first_seen'], 'last_seen': info['last_seen']})
    groups = {}
    for s in services:
        g = groups.setdefault(s['category'], {'category': s['category'], 'label': s['category_label'], 'services': []})
        g['services'].append(s)
    order = [k for k, _, _ in CATEGORIES] + ['other']
    out = sorted(groups.values(), key=lambda g: order.index(g['category']))
    for g in out:
        g['services'].sort(key=lambda s: (not s['new'], s['name'].lower(), s['node']))
        g['count'] = len(g['services'])
    left.sort(key=lambda s: s['last_seen'], reverse=True)
    return {'generated_at': now.strftime(TS_FMT), 'groups': out, 'left': left,
            'counts': {'services': len(services), 'new': sum(1 for s in services if s['new']), 'left': len(left)},
            'new_hours': NEW_HOURS, 'left_days': LEFT_DAYS}


def devices(node=None, search=''):
    """Hosts on each node's LAN from the mesh host list."""
    import phones
    phones.ensure_tables()
    with database.get_connection() as conn:
        node_rows = {r['name']: dict(r) for r in conn.execute('SELECT name, ip, is_active FROM nodes')}
        hosts = [dict(r) for r in conn.execute('SELECT * FROM mesh_hosts ORDER BY name')]
    node_ips = {n: r['ip'] for n, r in node_rows.items() if r['ip']}
    q = (search or '').lower()
    by_node = {}
    for h in hosts:
        name = (h.get('name') or '').lower()
        owner = phones.owner_node(h['ip'], node_ips)
        if not owner or (node and owner != node):
            continue
        if name.startswith(('dtdlink.', 'mid', 'lan.', 'xlink')) or name.split('.')[0] in node_rows \
                or h['ip'] == node_ips.get(owner):
            continue
        if q and q not in f"{name} {h['ip']} {owner}".lower():
            continue
        by_node.setdefault(owner, []).append({'name': h.get('name'), 'ip': h['ip'], 'first_seen': h.get('first_seen'),
                                              'last_seen': h.get('last_seen')})
    out = [{'node': n, 'active': bool((node_rows.get(n) or {}).get('is_active')), 'devices': d}
           for n, d in sorted(by_node.items())]
    return {'nodes': out, 'count': sum(len(n['devices']) for n in out)}

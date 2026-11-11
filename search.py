"""
Global search (TODO #62): one box in the header for node, site, MAC, IP,
callsign, service, phone or LAN device, answering with where to go.

`search(q)` returns ranked results, each {type, label, detail, url, score}:

  node      name, IP, description or model -> /nodes/<name>
  callsign  the first part of node names (w0gq) -> the Nodes page filtered
            to it, plus each of its nodes as a node result
  site      infrastructure site code or name (COL, Collins) -> /sites/<code>
  mac       an RF tracker MAC (links.mac_address, the far end's radio) or an
            SSH station MAC -> the node that owns it
  device    a host on a node's LAN (mesh_hosts, attached to the node whose LAN
            block holds its address) by name or IP -> its node
  service   an advertised service by name, link or IP -> its node
  phone     a phone by name, extension or IP -> VoIP & Phones

Read-only. MACs match with any separator (aa:bb, aa-bb, aabb); IPs match
exactly or by prefix (10.42.).
"""

import re

import database

LIMIT = 30
PER_TYPE = 8
MIN_LEN = 2

_MAC_CHARS = re.compile(r'^[0-9a-f:.\-]+$')
_IP_PREFIX = re.compile(r'^\d{1,3}(\.\d{0,3}){1,3}$')
_CALLSIGN = re.compile(r'^[a-z]{1,2}\d[a-z]{1,4}$')
_HOST_SKIP = ('dtdlink.', 'mid', 'lan.', 'xlink')


def _mac_key(text):
    return re.sub(r'[^0-9a-f]', '', (text or '').lower())


def _looks_like_mac(q):
    """At least two hex pairs with separators, or 6+ bare hex digits that
    aren't all decimal (so callsigns and numbers don't count)."""
    if not _MAC_CHARS.match(q):
        return False
    key = _mac_key(q)
    if len(key) < 4:
        return False
    return bool(re.search(r'[:\-]', q)) or (len(key) >= 6 and not key.isdigit())


def _text_score(q, value, exact=100, prefix=80, contains=50):
    v = (value or '').lower()
    if not v:
        return 0
    if v == q:
        return exact
    if v.startswith(q):
        return prefix
    if q in v:
        return contains
    return 0


def _ip_score(q, ip):
    if not ip:
        return 0
    if ip == q:
        return 95
    if _IP_PREFIX.match(q) and ip.startswith(q):
        return 60
    return 0


def search(q, limit=LIMIT):
    q = (q or '').strip().lower()
    if len(q) < MIN_LEN:
        return {'query': q, 'results': []}
    results = []
    is_mac = _looks_like_mac(q)
    mac = _mac_key(q) if is_mac else None
    import phones
    phones.ensure_tables()  # mesh_hosts and phones

    with database.get_connection() as conn:
        nodes = [dict(r) for r in conn.execute(
            'SELECT name, ip, description, model, is_active FROM nodes')]
        infra = {r[0].lower(): r[1] for r in conn.execute('SELECT node_name, site FROM infrastructure_nodes')}
        node_names = {n['name'].lower() for n in nodes if n['name']}
        node_ips = {n['name']: n['ip'] for n in nodes if n['name'] and n['ip']}

        # Nodes and callsigns
        calls = {}
        for n in nodes:
            name = (n['name'] or '').lower()
            if not name:
                continue
            call = name.split('-')[0]
            if _CALLSIGN.match(call):
                calls.setdefault(call, []).append(n)
            score = max(_text_score(q, name), _ip_score(q, n['ip']),
                        70 if call == q else 0,
                        _text_score(q, n['description'], 60, 35, 30),
                        _text_score(q, n['model'], 40, 25, 20))
            if score:
                site = infra.get(name)
                detail = ' · '.join(x for x in (
                    n['ip'], n['model'], f'site {site}' if site else None,
                    'active' if n['is_active'] else 'inactive') if x)
                results.append({'type': 'node', 'label': n['name'], 'detail': detail,
                                'url': f'/nodes/{name}', 'score': score + (2 if n['is_active'] else 0)})
        for call, members in calls.items():
            score = _text_score(q, call, 98, 75, 0)
            if score:
                active = sum(1 for m in members if m['is_active'])
                results.append({'type': 'callsign', 'label': call.upper(),
                                'detail': f'{len(members)} node{"s" if len(members) != 1 else ""}, {active} active',
                                'url': f'/?nodes-search={call}#/nodes', 'score': score})

        # Sites
        for code, sname in database.get_site_names().items():
            score = max(_text_score(q, code, 99, 85, 0), _text_score(q, sname, 90, 75, 55))
            if score:
                count = sum(1 for s in infra.values() if (s or '').upper() == code.upper())
                results.append({'type': 'site', 'label': f'{code} — {sname}',
                                'detail': f'{count} infrastructure node{"s" if count != 1 else ""}',
                                'url': f'/sites/{code.upper()}', 'score': score})

        # MACs: RF trackers (a tracker's MAC is the far end's radio) and SSH stations
        if mac:
            seen = set()
            for r in conn.execute('SELECT DISTINCT mac_address, target_node, source_node FROM links '
                                  'WHERE mac_address IS NOT NULL'):
                key = _mac_key(r[0])
                if mac in key and (key, r[1]) not in seen:
                    seen.add((key, r[1]))
                    results.append({'type': 'mac', 'label': r[0], 'detail': f'radio of {r[1]} (heard by {r[2]})',
                                    'url': f'/nodes/{(r[1] or "").lower()}', 'score': 97 if key == mac else 65})
            try:
                rows = conn.execute('SELECT DISTINCT mac, peer, node_name FROM ssh_station_samples '
                                    'WHERE peer IS NOT NULL').fetchall()
            except Exception:  # table appears with the first SSH collection
                rows = []
            for r in rows:
                key = _mac_key(r[0])
                if mac in key and (key, (r[1] or '').lower()) not in seen:
                    seen.add((key, (r[1] or '').lower()))
                    results.append({'type': 'mac', 'label': r[0], 'detail': f'station {r[1]} (on {r[2]})',
                                    'url': f'/nodes/{(r[1] or "").lower()}', 'score': 96 if key == mac else 64})

        # LAN devices from the mesh host list
        for r in conn.execute('SELECT ip, name FROM mesh_hosts'):
            name = (r['name'] or '').lower()
            if name.startswith(_HOST_SKIP) or name.split('.')[0] in node_names:
                continue
            score = max(_text_score(q, name, 90, 70, 45), _ip_score(q, r['ip']))
            if not score:
                continue
            owner = phones.owner_node(r['ip'], node_ips)
            if not owner or r['ip'] == node_ips.get(owner):
                continue
            results.append({'type': 'device', 'label': r['name'], 'detail': f'{r["ip"]} on {owner}',
                            'url': f'/nodes/{owner.lower()}', 'score': score})

        # Services
        for r in conn.execute('SELECT node_name, name, link, ip FROM services'):
            score = max(_text_score(q, r['name'], 85, 65, 45), _ip_score(q, r['ip']),
                        30 if q in (r['link'] or '').lower() else 0)
            if score:
                results.append({'type': 'service', 'label': r['name'],
                                'detail': ' · '.join(x for x in (f'on {r["node_name"]}', r['link'] or r['ip']) if x),
                                'url': f'/nodes/{(r["node_name"] or "").lower()}', 'score': score})

        # Phones
        for r in conn.execute('SELECT name, extension, ip, node_name FROM phones WHERE NOT ignored'):
            score = max(_text_score(q, r['name'], 85, 65, 45), _text_score(q, r['extension'], 95, 60, 0),
                        _ip_score(q, r['ip']))
            if score:
                results.append({'type': 'phone', 'label': r['name'] or r['ip'],
                                'detail': ' · '.join(x for x in (
                                    f'ext {r["extension"]}' if r['extension'] else None, r['ip'],
                                    f'on {r["node_name"]}' if r['node_name'] else None) if x),
                                'url': '/#/voip', 'score': score})

    results.sort(key=lambda r: (-r['score'], r['type'], r['label'].lower()))
    out, per = [], {}
    for r in results:
        if per.get(r['type'], 0) >= PER_TYPE:
            continue
        per[r['type']] = per.get(r['type'], 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return {'query': q, 'results': out}

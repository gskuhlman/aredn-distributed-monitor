"""
Traffic along a Route Watch path (Routing page "Traffic" button, /routes/<id>/traffic).

On request only, nothing stored. Every node on the path's latest trace (plus
its two ends) that the monitor can reach over SSH -- the infrastructure nodes
and SSH_NODES, which have its key -- is read at the same time with
ssh_traffic's read-only script; their connection-tracking tables
(nf_conntrack) list every connection the node routes or terminates, with
protocol, ports and, when the kernel counts them, bytes and packets each way.

Each connection is named and classified:
  ends      between the path's two ends: the source and target node, or their
            whole infrastructure site, including hosts on those nodes' LANs
  transit   passing along the route between other places (it crosses at
            least one of the path's nodes without starting or ending there)
  own       to or from the reading node itself (Babel, DNS, our SSH session,
            its own services) or local broadcast: hidden by default
with the service guessed from the ports (SIP, RTP, DNS, web, SSH, Babel,
WireGuard, RTSP video ...), source and destination with their names and the
node each sits behind, and the hops it was seen at. The same connection seen
at several hops is listed once.

What it can't see: nodes without the monitor's key, and connections a node
doesn't track (conntrack off or full; the page says so per hop). Byte counts
need nf_conntrack_acct on the node; otherwise only the connections are listed.
"""

import ipaddress
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import database
import route_watch
import ssh_collect
import ssh_traffic

TS_FMT = '%Y-%m-%d %H:%M:%S'
LOCAL_CLASSES = ('node', 'broadcast', 'linklocal')


def _path(path_id):
    route_watch.ensure_tables()
    with database.get_connection() as conn:
        p = conn.execute('SELECT * FROM route_paths WHERE id = ?', (path_id,)).fetchone()
        if not p:
            return None, None
        last = conn.execute('SELECT * FROM route_history WHERE path_id = ? AND ok = 1 ORDER BY id DESC LIMIT 1',
                            (path_id,)).fetchone()
    return dict(p), dict(last) if last else None


def _sites():
    with database.get_connection() as conn:
        return {r[0].lower(): (r[1] or '').upper() for r in conn.execute('SELECT node_name, site FROM infrastructure_nodes')}


def end_nodes(name, sites):
    """The nodes that count as one end: its whole site for an infrastructure node."""
    name = (name or '').lower()
    site = sites.get(name)
    if site:
        return {n for n, s in sites.items() if s == site}, site
    return {name}, None


class Owners:
    """IP -> the node it belongs to: a node's own addresses (also its dtdlink /
    tunnel names in the mesh host list), else the node whose LAN holds it."""

    def __init__(self):
        import phones
        phones.ensure_tables()  # mesh_hosts
        self.exact = {}
        with database.get_connection() as conn:
            names = set()
            for name, ip in conn.execute('SELECT name, ip FROM nodes WHERE ip IS NOT NULL'):
                self.exact[ip] = name.lower()
                names.add(name.lower())
            for ip, host in conn.execute('SELECT ip, name FROM mesh_hosts'):
                node = route_watch._hop_name(host, ip)
                if node in names:
                    self.exact.setdefault(ip, node)
        self.homes = ssh_traffic._home_nodes()

    def __call__(self, ip):
        if not ip:
            return None
        if ip in self.exact:
            return self.exact[ip]
        if ':' in ip:   # IPv6 (link-local neighbours): AREDN LANs are IPv4
            return None
        home = ssh_traffic.home_of(ip, self.homes)
        return home.lower() if home else None


def _label(f):
    if f.get('service'):
        return f['service']
    proto = (f.get('proto') or '?').upper()
    if f.get('proto') in ('tcp', 'udp', 'sctp', 'udplite'):
        port = min(p for p in (f.get('sport'), f.get('dport')) if p) if (f.get('sport') or f.get('dport')) else None
        return f'{proto} {port}' if port else proto
    return proto


def _add(a, b):
    return None if a is None and b is None else (a or 0) + (b or 0)


def classify(flows_by_hop, a_nodes, b_nodes, owner):
    """Merge the hops' flows into one list of connections with their category."""
    merged = {}
    for hop, flows in flows_by_hop:
        for f in flows:
            key = (f['proto'], f['src'], f['sport'], f['dst'], f['dport'])
            src_node, dst_node = owner(f['src']), owner(f['dst'])
            if f.get('src_class') in LOCAL_CLASSES or f.get('dst_class') in LOCAL_CLASSES:
                category, direction = 'own', None
            elif src_node in a_nodes and dst_node in b_nodes:
                category, direction = 'ends', 'a_to_b'
            elif src_node in b_nodes and dst_node in a_nodes:
                category, direction = 'ends', 'b_to_a'
            else:
                category, direction = 'transit', None
            m = merged.get(key)
            if m is None:
                m = merged[key] = {
                    'proto': f['proto'], 'service': _label(f), 'state': f.get('state'),
                    'unreplied': f.get('unreplied'),
                    'src': f['src'], 'sport': f['sport'], 'src_name': f.get('src_name'), 'src_node': src_node,
                    'dst': f['dst'], 'dport': f['dport'], 'dst_name': f.get('dst_name'), 'dst_node': dst_node,
                    'category': category, 'direction': direction, 'nat': bool(f.get('nat')),
                    'bytes_out': f.get('bytes_out'), 'bytes_in': f.get('bytes_in'),
                    'packets_out': f.get('packets_out'), 'packets_in': f.get('packets_in'), 'seen_at': [hop]}
                continue
            m['seen_at'].append(hop)
            # A connection the route carries, not just a hop's own, wins.
            if m['category'] == 'own' and category != 'own':
                m['category'], m['direction'] = category, direction
            for k in ('bytes_out', 'bytes_in', 'packets_out', 'packets_in'):
                if f.get(k) is not None and (m[k] is None or f[k] > m[k]):
                    m[k] = f[k]
            m['src_name'] = m['src_name'] or f.get('src_name')
            m['dst_name'] = m['dst_name'] or f.get('dst_name')
    out = list(merged.values())
    for m in out:
        m['bytes'] = _add(m['bytes_out'], m['bytes_in'])
        m['packets'] = _add(m['packets_out'], m['packets_in'])
    order = {'ends': 0, 'transit': 1, 'own': 2}
    out.sort(key=lambda m: (order[m['category']], -(m['bytes'] or 0), -(m['packets'] or 0), m['service']))
    return out


def summarize(flows):
    """Per category: connections and bytes by service, and by host pair."""
    out = {}
    for cat in ('ends', 'transit', 'own'):
        rows = [f for f in flows if f['category'] == cat]
        svc, pairs = {}, {}
        for f in rows:
            s = svc.setdefault(f['service'], {'service': f['service'], 'flows': 0, 'bytes': None, 'packets': None})
            s['flows'] += 1
            s['bytes'], s['packets'] = _add(s['bytes'], f['bytes']), _add(s['packets'], f['packets'])
            src = f['src_name'] or f['src']
            dst = f['dst_name'] or f['dst']
            p = pairs.setdefault((src, dst), {'src': src, 'src_node': f['src_node'], 'dst': dst, 'dst_node': f['dst_node'],
                                              'flows': 0, 'bytes': None, 'services': {}})
            p['flows'] += 1
            p['bytes'] = _add(p['bytes'], f['bytes'])
            p['services'][f['service']] = p['services'].get(f['service'], 0) + 1
        for p in pairs.values():
            p['services'] = [k for k, _ in sorted(p['services'].items(), key=lambda kv: -kv[1])]
        key = lambda r: (-(r['bytes'] or 0), -r['flows'])
        out[cat] = {'flows': len(rows), 'bytes': sum(f['bytes'] or 0 for f in rows) if any(f['bytes'] is not None for f in rows) else None,
                    'services': sorted(svc.values(), key=key), 'pairs': sorted(pairs.values(), key=key)[:40]}
    return out


def _capture(node, seconds):
    try:
        return node, ssh_traffic.traffic(node, seconds)
    except Exception as exc:   # one bad hop never loses the others
        return node, {'node': node, 'error': str(exc)[:300]}


def route_traffic(path_id, seconds=ssh_traffic.DEFAULT_SECONDS, capture=None):
    """Read every reachable node on the path now; {error} when nothing can be read."""
    path, last = _path(path_id)
    if path is None:
        return {'error': 'Route not found', 'error_kind': 'not_found'}
    if not ssh_collect.key_present():
        return {'error': 'No SSH key yet: create one on the Admin page (SSH).', 'error_kind': 'no_key'}
    sites = _sites()
    source = (last['source'] if last and last.get('source') else path.get('source') or '').lower()
    target = (path['target'] or '').lower()
    hops = [h['name'] for h in json.loads(last['hops'] or '[]') if not h.get('timeout')] if last else []
    route = [n for n in [source] + hops if n] if source and (not hops or hops[0] != source) else hops
    if target and (not route or route[-1] != target):
        route.append(target)
    a_nodes, a_site = end_nodes(source, sites)
    b_nodes, b_site = end_nodes(target, sites)
    reachable = set(ssh_collect.targets())
    readable = [n for n in dict.fromkeys(route) if n in reachable]
    seconds = max(1, min(ssh_traffic.MAX_SECONDS, int(seconds or ssh_traffic.DEFAULT_SECONDS)))
    base = {'path': {'id': path['id'], 'label': path['label'], 'source': source or None, 'target': target},
            'ends': {'a': {'node': source or None, 'site': a_site, 'nodes': sorted(a_nodes)},
                     'b': {'node': target, 'site': b_site, 'nodes': sorted(b_nodes)}},
            'route': route, 'traced_at': last['timestamp'] if last else None, 'seconds': seconds,
            'generated_at': datetime.now().strftime(TS_FMT)}
    if not readable:
        return dict(base, error='None of the nodes on this route accept the monitor\'s SSH key, so their traffic '
                                'can\'t be read. Infrastructure nodes (and SSH_NODES) are read over SSH.',
                    error_kind='no_ssh_nodes', hops=[{'node': n, 'readable': False} for n in route])
    cap = capture or _capture
    with ThreadPoolExecutor(max_workers=len(readable)) as pool:
        results = dict(pool.map(lambda n: cap(n, seconds), readable))
    hop_rows, flows_by_hop = [], []
    for n in route:
        r = results.get(n)
        if r is None:
            hop_rows.append({'node': n, 'readable': False})
            continue
        if r.get('error'):
            hop_rows.append({'node': n, 'readable': True, 'error': r['error']})
            continue
        ct = r.get('conntrack') or {}
        hop_rows.append({'node': n, 'readable': True, 'available': ct.get('available'),
                         'accounting': ct.get('accounting'), 'connections': ct.get('count'),
                         'truncated': ct.get('truncated')})
        flows_by_hop.append((n, r.get('flows') or []))
    if not flows_by_hop:
        return dict(base, error='No node on this route answered over SSH.', error_kind='no_answer', hops=hop_rows)
    flows = classify(flows_by_hop, a_nodes, b_nodes, Owners())
    return dict(base, hops=hop_rows, flows=flows, summary=summarize(flows),
                accounting=any(h.get('accounting') for h in hop_rows))

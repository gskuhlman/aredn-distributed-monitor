"""
Node traffic over SSH (node page "Traffic (SSH)", on request only).

One SSH session (ssh_collect's key, host-key pinning and transport) runs a
fixed, read-only script on the node: interface addresses, the uci device of
the wan / lan / wifi / dtdlink networks, /proc/net/dev and `wg show all
transfer` twice `seconds` apart, WireGuard endpoints, the connection-tracking
table (/proc/net/nf_conntrack, else `conntrack -L`; first MAX_FLOWS lines) and
the DHCP leases. Nothing is changed on the node and nothing is stored here:
the answer goes straight back to the page (it names hosts and addresses, so
the route needs a login when a password is set).

From that:
- interfaces: per interface its role (WAN, LAN, mesh RF, DtD, WireGuard /
  legacy tunnel, port) and receive/transmit bytes and packets per second;
- wireguard: per tunnel peer its endpoint and throughput over the interval;
- flows: every tracked connection with both ends named (this node, its LAN
  hosts by DHCP lease or mesh host name, mesh nodes, tunnel addresses, the
  WAN network, the internet), protocol, ports with a service hint (SIP, RTP
  range, DNS, web, SSH, Babel, WireGuard ...), state, NAT (a connection
  leaving through the WAN with its source rewritten = mesh or LAN traffic
  using this node's internet) and bytes/packets each way when the kernel
  counts them (nf_conntrack_acct);
- summary: flows and bytes per "from -> to" kind.
"""

import ipaddress
import re
import subprocess

import database
import ssh_collect

MARK = ssh_collect.MARK
MAX_FLOWS = 4000
DEFAULT_SECONDS = 5
MAX_SECONDS = 15
TUNNEL_NET = ipaddress.ip_network('172.31.0.0/16')
MESH_NET = ipaddress.ip_network('10.0.0.0/8')
PROTOS = ('tcp', 'udp', 'icmp', 'icmpv6', 'gre', 'esp', 'sctp', 'udplite', 'unknown')
ROLE_LABELS = {'wan': 'WAN (internet)', 'lan': 'LAN', 'wifi': 'Mesh RF', 'dtdlink': 'DtD',
               'wireguard': 'WireGuard tunnel', 'tun': 'Legacy tunnel', 'port': 'Switch port / device',
               'other': 'Other'}
CLASS_LABELS = {'node': 'this node', 'lan': 'LAN', 'wan': 'WAN network', 'mesh': 'mesh', 'dtdlink': 'DtD',
                'tunnel': 'tunnel', 'internet': 'internet', 'private': 'private network',
                'linklocal': 'link-local (neighbour)',
                'broadcast': 'broadcast'}
SERVICES = {53: 'DNS', 67: 'DHCP', 68: 'DHCP', 80: 'web', 443: 'web (TLS)', 8080: 'web', 22: 'SSH',
            2222: 'SSH', 23: 'telnet', 123: 'NTP', 5060: 'SIP', 5061: 'SIP (TLS)', 6696: 'Babel',
            698: 'OLSR', 161: 'SNMP', 9100: 'metrics', 5201: 'iPerf', 5525: 'WireGuard',
            1194: 'OpenVPN', 5353: 'mDNS', 8765: 'VoIP agent', 8766: 'VoIP probe',
            554: 'RTSP (video)', 8554: 'RTSP (video)', 1935: 'RTMP (video)', 3478: 'STUN'}


def script(seconds):
    return f'''export LC_ALL=C
echo "{MARK} addr"; ip -o -4 addr show 2>&1
echo "{MARK} roles"; for n in wan lan wifi dtdlink; do echo "$n $(uci -q get network.$n.device)"; done
echo "{MARK} dev1"; cat /proc/uptime; cat /proc/net/dev
echo "{MARK} wg1"; wg show all transfer 2>/dev/null
sleep {int(seconds)}
echo "{MARK} dev2"; cat /proc/uptime; cat /proc/net/dev
echo "{MARK} wg2"; wg show all transfer 2>/dev/null
echo "{MARK} wgend"; wg show all endpoints 2>/dev/null
echo "{MARK} acct"; cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null
echo "{MARK} conntrack"; if [ -r /proc/net/nf_conntrack ]; then head -n {MAX_FLOWS} /proc/net/nf_conntrack; else conntrack -L 2>/dev/null | head -n {MAX_FLOWS}; fi
echo "{MARK} leases"; cat /tmp/dhcp.leases 2>/dev/null
echo "{MARK} end"
'''


# ---------------------------------------------------------------- parsing --

def parse_dev(text):
    """(uptime seconds, {iface: (rx_bytes, rx_packets, tx_bytes, tx_packets)})."""
    lines = (text or '').splitlines()
    up = None
    if lines:
        try:
            up = float(lines[0].split()[0])
        except (ValueError, IndexError):
            pass
    out = {}
    for line in lines:
        if ':' not in line or '|' in line:
            continue
        name, rest = line.split(':', 1)
        f = rest.split()
        if len(f) >= 10:
            try:
                out[name.strip()] = (int(f[0]), int(f[1]), int(f[8]), int(f[9]))
            except ValueError:
                pass
    return up, out


def parse_addr(text):
    """[(iface, interface network, address)] from `ip -o -4 addr show`."""
    out = []
    for line in (text or '').splitlines():
        m = re.match(r'\d+:\s+(\S+)\s+inet\s+(\S+)', line)
        if m:
            try:
                iface = ipaddress.ip_interface(m.group(2))
                out.append((m.group(1).split('@')[0], iface.network, iface.ip))
            except ValueError:
                pass
    return out


def parse_roles(text):
    roles = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 2:
            roles[parts[1]] = parts[0]
    return roles


def parse_wg(text):
    """{(iface, peer): (rx_bytes, tx_bytes)} or, for endpoints, {(iface, peer): endpoint}."""
    out = {}
    for line in (text or '').splitlines():
        f = line.split()
        if len(f) == 4:
            try:
                out[(f[0], f[1])] = (int(f[2]), int(f[3]))
            except ValueError:
                pass
        elif len(f) == 3:
            out[(f[0], f[1])] = f[2]
    return out


def parse_conntrack(text):
    """Flows from /proc/net/nf_conntrack or `conntrack -L` lines."""
    flows = []
    for line in (text or '').splitlines():
        tokens = line.split()
        proto = next((t for t in tokens[:4] if t in PROTOS), None)
        if not proto:
            continue
        sides, cur, state = [], None, None
        for t in tokens:
            if t.startswith(('src=', 'dst=')):
                try:   # conntrack writes IPv6 in full: fe80:0000:...
                    t = t[:4] + ipaddress.ip_address(t[4:]).compressed
                except ValueError:
                    pass
            if '=' not in t:
                if t.isupper() and t not in ('[ASSURED]', '[UNREPLIED]'):
                    state = state or t
                continue
            k, v = t.split('=', 1)
            if k == 'src':
                cur = {}
                sides.append(cur)
            if cur is not None and k in ('src', 'dst', 'sport', 'dport', 'packets', 'bytes', 'type'):
                cur[k] = v
        if not sides:
            continue
        orig = sides[0]
        reply = sides[1] if len(sides) > 1 else {}
        num = lambda d, k: int(d[k]) if d.get(k, '').isdigit() else None
        flows.append({'proto': proto, 'state': state, 'unreplied': '[UNREPLIED]' in tokens,
                      'src': orig.get('src'), 'dst': orig.get('dst'),
                      'sport': num(orig, 'sport'), 'dport': num(orig, 'dport'),
                      'reply_src': reply.get('src'), 'reply_dst': reply.get('dst'),
                      'bytes_out': num(orig, 'bytes'), 'packets_out': num(orig, 'packets'),
                      'bytes_in': num(reply, 'bytes'), 'packets_in': num(reply, 'packets')})
    return flows


def parse_leases(text):
    """{ip: hostname} from /tmp/dhcp.leases (expiry mac ip name clientid)."""
    out = {}
    for line in (text or '').splitlines():
        f = line.split()
        if len(f) >= 4 and f[3] != '*':
            out[f[2]] = f[3]
    return out


# ------------------------------------------------------------- classify --

def iface_role(name, roles):
    if name in roles:
        return roles[name]
    if name.startswith('wg'):
        return 'wireguard'
    if name.startswith('tun'):
        return 'tun'
    if re.match(r'^(eth|lan|wan|br0|switch)', name):
        return 'port'
    return 'other'


class Namer:
    """Classify and name addresses as seen from the node."""

    def __init__(self, addrs, roles, leases, names):
        self.own = {str(ip) for _, _, ip in addrs}
        self.nets = []
        for iface, net, _ in addrs:
            role = iface_role(iface, roles)
            if net.prefixlen < 32 and iface != 'lo':
                self.nets.append((net, {'wifi': 'mesh', 'wireguard': 'tunnel', 'tun': 'tunnel'}.get(role, role)))
        self.nets.sort(key=lambda n: -n[0].prefixlen)
        self.leases = leases
        self.names = names

    def classify(self, ip):
        if not ip:
            return None
        if ip in self.own:
            return 'node'
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if a.is_multicast or ip.endswith('.255'):
            return 'broadcast'
        if a.is_link_local:
            return 'linklocal'
        for net, role in self.nets:
            if a in net and role in CLASS_LABELS:
                return role
        if a in TUNNEL_NET:
            return 'tunnel'
        if a in MESH_NET:
            return 'mesh'
        if a.is_private:
            return 'private'
        return 'internet'

    def name(self, ip):
        return self.leases.get(ip) or self.names.get(ip)


def _known_names():
    """{ip: name} from mesh nodes, the mesh host list and phones."""
    out = {}
    with database.get_connection() as conn:
        for sql in ("SELECT ip, name FROM mesh_hosts", "SELECT ip, name FROM phones",
                    "SELECT ip, name FROM nodes WHERE ip IS NOT NULL"):
            try:
                for ip, name in conn.execute(sql):
                    if ip and name:
                        out[ip] = name
            except Exception:   # an optional table may not exist yet
                continue
    return out


def service_hint(proto, sport, dport):
    for p in (dport, sport):
        if p in SERVICES:
            return SERVICES[p]
    if proto == 'udp' and any(p and 10000 <= p <= 20000 for p in (sport, dport)):
        return 'RTP (voice)?'
    if proto == 'udp' and any(p and 5525 <= p <= 5600 for p in (sport, dport)):
        return 'WireGuard'
    return None


def analyse(sections, seconds, names=None):
    by = {}
    for kind, _, body in sections:
        by.setdefault(kind, body)
    up1, dev1 = parse_dev(by.get('dev1'))
    up2, dev2 = parse_dev(by.get('dev2'))
    elapsed = (up2 - up1) if up1 is not None and up2 is not None and up2 > up1 else float(seconds)
    roles = parse_roles(by.get('roles'))
    addrs = parse_addr(by.get('addr'))
    addr_of = {}
    for iface, net, ip in addrs:
        addr_of.setdefault(iface, f'{ip}/{net.prefixlen}')
    interfaces = []
    for name, b in sorted(dev2.items()):
        a = dev1.get(name)
        if name == 'lo' or not a:
            continue
        rate = lambda i: max(0.0, (b[i] - a[i]) / elapsed)
        role = iface_role(name, roles)
        interfaces.append({'name': name, 'role': role, 'role_label': ROLE_LABELS.get(role, role),
                           'address': addr_of.get(name),
                           'rx_bps': round(rate(0) * 8), 'tx_bps': round(rate(2) * 8),
                           'rx_pps': round(rate(1), 1), 'tx_pps': round(rate(3), 1),
                           'rx_total_bytes': b[0], 'tx_total_bytes': b[2]})
    interfaces.sort(key=lambda i: -(i['rx_bps'] + i['tx_bps']))

    wg1, wg2, ends = parse_wg(by.get('wg1')), parse_wg(by.get('wg2')), parse_wg(by.get('wgend'))
    wireguard = []
    for key, (rx, tx) in sorted(wg2.items()):
        before = wg1.get(key)
        wireguard.append({'iface': key[0], 'peer': key[1][:10] + '…', 'endpoint': ends.get(key),
                          'rx_bps': round(max(0, rx - before[0]) * 8 / elapsed) if before else None,
                          'tx_bps': round(max(0, tx - before[1]) * 8 / elapsed) if before else None,
                          'rx_total_bytes': rx, 'tx_total_bytes': tx})
    wireguard.sort(key=lambda w: -((w['rx_bps'] or 0) + (w['tx_bps'] or 0)))

    leases = parse_leases(by.get('leases'))
    namer = Namer(addrs, roles, leases, names if names is not None else _known_names())
    raw = by.get('conntrack')
    flows = parse_conntrack(raw)
    acct = (by.get('acct') or '').strip() == '1' or any(f['bytes_out'] is not None for f in flows)
    wg_ends = {}
    for (iface, _), end in ends.items():
        host, _, port = str(end).rpartition(':')
        if port.isdigit():
            wg_ends[(host.strip('[]'), int(port))] = iface
    summary = {}
    for f in flows:
        f['src_class'], f['dst_class'] = namer.classify(f['src']), namer.classify(f['dst'])
        f['src_name'], f['dst_name'] = namer.name(f['src']), namer.name(f['dst'])
        f['service'] = service_hint(f['proto'], f['sport'], f['dport'])
        tunnel = wg_ends.get((f['dst'], f['dport'])) or wg_ends.get((f['src'], f['sport']))
        if tunnel:
            f['service'] = f'WireGuard tunnel ({tunnel})'
        # Source rewritten on the way out (masquerade): this node's internet
        # (or a tunnel/WAN address) carries traffic from somewhere else.
        f['nat'] = bool(f['reply_dst'] and f['reply_dst'] != f['src'])
        if f['nat'] and f['dst_class'] in ('internet', 'wan', 'private'):
            f['via'] = 'this node\'s WAN'
        total = (f['bytes_out'] or 0) + (f['bytes_in'] or 0)
        f['bytes'] = total if acct else None
        key = f"{CLASS_LABELS.get(f['src_class'], f['src_class'] or '?')} → " \
              f"{CLASS_LABELS.get(f['dst_class'], f['dst_class'] or '?')}" + (' (NAT via WAN)' if f.get('via') else '')
        s = summary.setdefault(key, {'kind': key, 'flows': 0, 'bytes': 0 if acct else None, 'services': {}})
        s['flows'] += 1
        if acct:
            s['bytes'] += total
        if f['service']:
            s['services'][f['service']] = s['services'].get(f['service'], 0) + 1
    flows.sort(key=lambda f: (-(f['bytes'] or 0), f['proto'], f['src'] or ''))
    summary_rows = sorted(summary.values(), key=lambda s: (-(s['bytes'] or 0), -s['flows']))
    for s in summary_rows:
        s['services'] = [f'{k} ×{v}' for k, v in sorted(s['services'].items(), key=lambda kv: -kv[1])][:6]
    lines = len([ln for ln in (raw or '').splitlines() if ln.strip()])
    return {
        'seconds': round(elapsed, 1), 'interfaces': interfaces, 'wireguard': wireguard,
        'conntrack': {'available': bool(raw and raw.strip()), 'accounting': acct, 'count': len(flows),
                      'truncated': lines >= MAX_FLOWS},
        'summary': summary_rows, 'flows': flows,
        'lan_hosts': [{'ip': ip, 'name': n} for ip, n in sorted(leases.items())],
        'roles': roles,
    }


def traffic(node, seconds=DEFAULT_SECONDS):
    """Run the traffic script on `node` and return the analysis, or {error}."""
    if not ssh_collect.key_present():
        return {'node': node, 'error': 'No SSH key yet: run python ssh_collect.py --keygen on this PC.',
                'error_kind': 'no_key'}
    seconds = max(1, min(MAX_SECONDS, int(seconds or DEFAULT_SECONDS)))
    try:
        code, out, err = ssh_collect._run_ssh(node, script(seconds))
    except subprocess.TimeoutExpired:
        code, out, err = None, '', 'timed out'
    except OSError as exc:
        code, out, err = None, '', f'could not run ssh: {exc}'
    sections = ssh_collect.split_sections(out)
    if not any(k == 'end' for k, _, _ in sections):
        kind = ssh_collect.classify_error(err)
        return {'node': node, 'error': ssh_collect.ERROR_HINTS.get(kind) or (err.strip()[:300] or 'no answer'),
                'error_kind': kind}
    return {'node': node, **analyse(sections, seconds)}

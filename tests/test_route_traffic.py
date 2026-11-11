"""Traffic along a Route Watch path (route_traffic.py)."""

import json

import pytest

import config
import route_traffic as rt
import route_watch
import ssh_collect


def flow(src, dst, proto='udp', sport=40000, dport=5060, service='SIP', b_out=1000, b_in=500, src_class='mesh',
         dst_class='mesh', **kw):
    return dict({'proto': proto, 'src': src, 'dst': dst, 'sport': sport, 'dport': dport, 'service': service,
                 'src_name': None, 'dst_name': None, 'src_class': src_class, 'dst_class': dst_class,
                 'bytes_out': b_out, 'bytes_in': b_in, 'packets_out': 10, 'packets_in': 5, 'state': None,
                 'unreplied': False, 'nat': False, 'reply_dst': None}, **kw)


@pytest.fixture
def route(mesh, monkeypatch, tmp_path):
    # COL site: rkt (10.1.0.1) and ac3 (10.1.1.1, LAN host 10.1.1.5); MMC site: sq (10.2.0.1), ac2 (10.2.1.1)
    for name, ip, site in (('w0aa-col-rkt', '10.1.0.1', 'COL'), ('w0aa-col-ac3', '10.1.1.1', 'COL'),
                           ('w0aa-mmc-sq', '10.2.0.1', 'MMC'), ('w0aa-mmc-ac2', '10.2.1.1', 'MMC')):
        mesh.node(name, ip=ip)
        mesh.infra(name, site)
    mesh.node('kb0zz-home', ip='10.9.0.1')
    route_watch.ensure_tables()
    mesh.sql("INSERT INTO route_paths (id, label, source, target, kind) VALUES (1, 'COL → MMC', 'w0aa-col-rkt', 'w0aa-mmc-ac2', 'auto')")
    hops = [{'name': 'w0aa-mmc-sq'}, {'name': '*', 'timeout': True}, {'name': 'w0aa-mmc-ac2'}]
    mesh.sql("INSERT INTO route_history (path_id, timestamp, ok, source, target, hops) VALUES (1, '2026-11-11 12:00:00', 1, "
             "'w0aa-col-rkt', 'w0aa-mmc-ac2', ?)", (json.dumps(hops),))
    key = tmp_path / 'k'
    key.write_text('x')
    monkeypatch.setattr(config, 'SSH_KEY_PATH', str(key))
    monkeypatch.setattr(ssh_collect, 'targets', lambda: ['w0aa-col-rkt', 'w0aa-mmc-sq', 'w0aa-mmc-ac2'])
    return mesh


def reading(flows, acct=True):
    return {'flows': flows, 'conntrack': {'available': True, 'accounting': acct, 'count': len(flows), 'truncated': False}}


def test_classifies_and_merges(route):
    phone_call = flow('10.1.1.5', '10.2.1.9', proto='udp', sport=12000, dport=14000, service='RTP (voice)?')
    readings = {
        'w0aa-col-rkt': reading([phone_call, flow('10.9.0.5', '10.2.1.1', proto='tcp', sport=50000, dport=80, service='web'),
                                 flow('10.1.0.1', '10.2.0.1', proto='udp', sport=6696, dport=6696, service='Babel',
                                      src_class='node')]),
        'w0aa-mmc-sq': reading([dict(phone_call, bytes_out=3000)]),
        'w0aa-mmc-ac2': {'node': 'w0aa-mmc-ac2', 'error': 'no answer in time'},
    }
    seen = []

    def capture(node, seconds):
        seen.append(node)
        return node, dict(readings[node], node=node)
    r = rt.route_traffic(1, seconds=5, capture=capture)
    assert sorted(seen) == ['w0aa-col-rkt', 'w0aa-mmc-ac2', 'w0aa-mmc-sq']
    assert r['route'] == ['w0aa-col-rkt', 'w0aa-mmc-sq', 'w0aa-mmc-ac2']
    assert r['ends']['a']['site'] == 'COL' and r['ends']['b']['nodes'] == ['w0aa-mmc-ac2', 'w0aa-mmc-sq']
    by = {(f['src'], f['dst']): f for f in r['flows']}
    call = by[('10.1.1.5', '10.2.1.9')]
    # LAN host behind the COL ac3 to a host behind MMC ac2: between the ends, seen at two hops, largest count kept
    assert call['category'] == 'ends' and call['direction'] == 'a_to_b'
    assert call['src_node'] == 'w0aa-col-ac3' and call['dst_node'] == 'w0aa-mmc-ac2'
    assert call['seen_at'] == ['w0aa-col-rkt', 'w0aa-mmc-sq'] and call['bytes_out'] == 3000 and call['bytes'] == 3500
    web = by[('10.9.0.5', '10.2.1.1')]
    assert web['category'] == 'transit' and web['service'] == 'web' and web['src_node'] == 'kb0zz-home'
    assert by[('10.1.0.1', '10.2.0.1')]['category'] == 'own'
    assert [f['category'] for f in r['flows']] == ['ends', 'transit', 'own']
    s = r['summary']
    assert s['ends']['flows'] == 1 and s['ends']['services'][0]['service'] == 'RTP (voice)?'
    assert s['transit']['pairs'][0]['services'] == ['web']
    hops = {h['node']: h for h in r['hops']}
    assert hops['w0aa-mmc-ac2']['error'] == 'no answer in time' and hops['w0aa-col-rkt']['connections'] == 3


def test_unknown_service_gets_protocol_and_port(route):
    f = rt.classify([('x', [flow('10.9.0.5', '10.9.0.6', proto='tcp', sport=51234, dport=8443, service=None)])],
                    set(), set(), lambda ip: None)
    assert f[0]['service'] == 'TCP 8443'
    f = rt.classify([('x', [flow('10.9.0.5', '10.9.0.6', proto='icmp', sport=None, dport=None, service=None)])],
                    set(), set(), lambda ip: None)
    assert f[0]['service'] == 'ICMP'


def test_no_readable_nodes_and_missing_path(route, monkeypatch):
    monkeypatch.setattr(ssh_collect, 'targets', lambda: [])
    r = rt.route_traffic(1, capture=lambda n, s: pytest.fail('nothing to read'))
    assert r['error_kind'] == 'no_ssh_nodes' and all(not h['readable'] for h in r['hops'])
    assert rt.route_traffic(99)['error_kind'] == 'not_found'


def test_owner_of_lan_host_and_dtdlink_name(route):
    import phones
    phones.ensure_tables()
    route.sql("INSERT INTO mesh_hosts (ip, name, first_seen, last_seen) VALUES ('10.200.0.7', 'dtdlink.w0aa-mmc-sq.local.mesh', 'x', 'x')")
    owner = rt.Owners()
    assert owner('10.2.0.1') == 'w0aa-mmc-sq' and owner('10.200.0.7') == 'w0aa-mmc-sq'
    assert owner('10.1.1.5') == 'w0aa-col-ac3' and owner('192.0.2.1') is None
    assert owner('fe80::cf:a7ff:fe1e:9a43') is None   # IPv6 next to IPv4 node addresses

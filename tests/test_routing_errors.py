"""Tunnel neighbours Babel won't route to: polled when they answered
recently, flagged as a routing error, restored when routable again."""

import routing_errors
import scanner
from conftest import ts


def sysinfo(name, trackers=None):
    return {'node': name, 'node_details': {}, 'meshrf': {},
            'lqm': {'info': {'trackers': trackers or {}}}}


def tunnel(host, ip, routable):
    return {'hostname': host, 'canonical_ip': ip, 'type': 'Wireguard', 'routable': routable,
            'quality': 100, 'lastseen': 10}


def scan(monkeypatch, routable, answers=True):
    pages = {'10.0.0.1': sysinfo('seed', {'aa': tunnel('tun-node', '10.0.0.2', routable)})}
    if answers:
        pages['10.0.0.2'] = sysinfo('tun-node')

    def fetch(url):
        data = pages.get(scanner.url_host(url))
        return (data, False, 20) if data else (None, False, 5000)
    monkeypatch.setattr(scanner, 'fetch_node_with_fallback', fetch)
    monkeypatch.setattr(scanner, 'gateway_reachable', lambda url: True)
    return scanner.discover_network(start_url='http://10.0.0.1', max_depth=3)


def test_unroutable_tunnel_polled_and_flagged(mesh, monkeypatch):
    mesh.sql("INSERT INTO node_health_history (timestamp, node_name, reachable, probe_via) VALUES (?, 'tun-node', 1, 'seed')",
             (ts(5),))
    r = scan(monkeypatch, routable=False)
    assert r['nodes_found'] == 2
    events = [e for e in r['events'] if e['type'] == routing_errors.EVENT_ROUTING_ERROR]
    assert len(events) == 1 and events[0]['node'] == 'tun-node' and 'seed' in events[0]['details']
    assert [x['node_name'] for x in routing_errors.active()] == ['tun-node']

    r = scan(monkeypatch, routable=False)                  # still broken: no repeat event
    assert not [e for e in r['events'] if e['type'].startswith('routing_')]

    r = scan(monkeypatch, routable=True)
    assert [e['type'] for e in r['events'] if e['type'].startswith('routing_')] == ['routing_restored']
    assert routing_errors.active() == []


def test_unroutable_tunnel_not_polled_without_answers(mesh, monkeypatch):
    r = scan(monkeypatch, routable=False)
    assert r['nodes_found'] == 1
    assert routing_errors.active() == []

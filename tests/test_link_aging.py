"""A node the scan couldn't fully read keeps its links (bare sysinfo, or a
failed poll while a neighbour lists it routable); Link Health doesn't rate
drops caused by such polls."""

import database
import link_health
import scanner
from conftest import ts


def sysinfo(name, trackers=None):
    return {'node': name, 'node_details': {}, 'meshrf': {}, 'lqm': {'info': {'trackers': trackers or {}}}}


def dtd(host, ip):
    return {'hostname': host, 'canonical_ip': ip, 'type': 'DtD', 'routable': True, 'quality': 100}


def run(monkeypatch, pages, degraded=()):
    def fetch(url):
        host = scanner.url_host(url)
        data = pages.get(host)
        return (data, host in degraded, 20) if data else (None, False, 5000)
    monkeypatch.setattr(scanner, 'fetch_node_with_fallback', fetch)
    monkeypatch.setattr(scanner, 'gateway_reachable', lambda url: True)
    monkeypatch.setattr(scanner, '_held_scans', {})
    return scanner.discover_network(start_url='http://10.0.0.1', max_depth=3)


def setup(mesh):
    mesh.node('seed')
    mesh.node('tower', last_seen=ts(10))
    mesh.link('tower', 'radio', link_type='DtD', both=False, last_seen=ts(10))


def test_bare_sysinfo_holds_links(mesh, monkeypatch):
    setup(mesh)
    pages = {'10.0.0.1': sysinfo('seed', {'a': dtd('tower', '10.0.0.2')}), '10.0.0.2': sysinfo('tower')}
    r = run(monkeypatch, pages, degraded={'10.0.0.2'})
    assert r['held_nodes'] == ['tower']
    out = scanner.update_link_statuses(r['held_nodes'])
    assert out['dropped'] == 0
    assert scanner.update_link_statuses([])['dropped'] == 1     # without the hold it would age


def test_failed_poll_listed_routable_holds(mesh, monkeypatch):
    setup(mesh)
    pages = {'10.0.0.1': sysinfo('seed', {'a': dtd('tower', '10.0.0.2')})}
    r = run(monkeypatch, pages)
    assert r['held_nodes'] == ['tower']
    nodes = scanner.update_node_statuses(held_nodes=r['held_nodes'])
    assert 'tower' not in [n['name'] for n in nodes['inactive_nodes']]


def test_hold_is_capped(mesh, monkeypatch):
    setup(mesh)
    pages = {'10.0.0.1': sysinfo('seed', {'a': dtd('tower', '10.0.0.2')})}
    run(monkeypatch, pages)
    for _ in range(scanner.HOLD_MAX_SCANS):
        r = scanner.discover_network(start_url='http://10.0.0.1', max_depth=3)
    assert r['held_nodes'] == []


def test_polling_drops_not_rated(mesh):
    for minutes in (200, 150, 100, 50):
        mesh.sql("INSERT INTO node_health_history (timestamp, node_name, reachable, degraded) VALUES (?, 'tower', 1, 1)",
                 (ts(minutes),))
        database.log_event('link_dropped', 'tower', 'DtD link to radio dropped', 'warning')
        mesh.sql("UPDATE events SET timestamp = ? WHERE id = (SELECT MAX(id) FROM events)", (ts(minutes),))
        database.log_event('link_restored', 'tower', 'DtD link to radio restored (Q:100%)', 'info')
        mesh.sql("UPDATE events SET timestamp = ? WHERE id = (SELECT MAX(id) FROM events)", (ts(minutes - 5),))
    f = link_health.compute_flap_score('tower', 'radio', link_type='DtD')
    assert f['polling_flaps'] == 8 and f['flap_count'] == 0 and f['rating'] == 'good'
    assert f['cause_label'] == 'Node polling'

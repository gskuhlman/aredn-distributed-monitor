"""Scan guards: a starting node that answers only bare sysinfo aborts the
scan; a short scan holds aging once, a second short scan ages normally."""

import scanner


def test_seed_degraded_aborts_scan(mesh, monkeypatch):
    bare = {'node': 'seed-node', 'node_details': {}, 'meshrf': {}}
    monkeypatch.setattr(scanner, 'fetch_node_with_fallback', lambda url: (bare, True, 50))
    monkeypatch.setattr(scanner, 'gateway_reachable', lambda url: True)
    r = scanner.discover_network(start_url='http://10.0.0.1', max_depth=3)
    assert r['aborted'] is True
    assert r['abort_reason'] == 'seed_degraded'
    assert r['nodes_found'] == 0


def test_gateway_lost_aborts_scan(mesh, monkeypatch):
    monkeypatch.setattr(scanner, 'fetch_node_with_fallback', lambda url: (None, False, 5000))
    r = scanner.discover_network(start_url='http://10.0.0.1', max_depth=3)
    assert r['aborted'] is True
    assert r['abort_reason'] == 'gateway_lost'


def test_short_scan_holds_aging_once(monkeypatch):
    monkeypatch.setattr(scanner, '_last_scan_partial', False)
    monkeypatch.setattr(scanner, '_scan_incomplete', False)
    hold, events = scanner.partial_scan_check({'node_count_reachable': 5}, 40)
    assert hold is True and events and events[0]['severity'] == 'warning'
    hold, _ = scanner.partial_scan_check({'node_count_reachable': 5}, 40)
    assert hold is False          # second short scan in a row: the loss is real
    hold, events = scanner.partial_scan_check({'node_count_reachable': 38}, 40)
    assert hold is False and any('complete again' in e['details'] for e in events)


def test_small_mesh_never_holds(monkeypatch):
    monkeypatch.setattr(scanner, '_last_scan_partial', False)
    hold, _ = scanner.partial_scan_check({'node_count_reachable': 1}, 5)
    assert hold is False

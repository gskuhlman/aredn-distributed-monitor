"""Route Watch: path roles (site to site vs the starting node's own paths),
route flapping, and "why did it change?" (route_watch.py)."""

import json
from datetime import datetime, timedelta

import config
import database
import gateway
import maintenance
import route_watch as rw

TS_FMT = '%Y-%m-%d %H:%M:%S'


def _setup(mesh, monkeypatch):
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    monkeypatch.setattr(maintenance, 'is_in_maintenance', lambda *a, **k: False)
    for i, n in enumerate(('src', 'b', 'c', 'dst')):
        mesh.node(n, ip=f'10.0.0.{i + 1}')
    mesh.infra('src', 'AAA')
    mesh.infra('dst', 'BBB')
    mesh.infra('b', 'AAA')
    mesh.infra('c', 'AAA')
    rw.ensure_tables()


def _trace(routes):
    """A fake traceroute that walks through `routes` (lists of hop names) in turn."""
    state = {'i': 0}

    def fn(target, source_node_ip=None):
        route = routes[state['i'] % len(routes)]
        state['i'] += 1
        return {'raw': 'Client: src', 'hops': [{'hop': k + 1, 'host': f'{n}.local.mesh', 'ip': f'10.9.0.{k + 1}',
                                                'ms': 5.0 + k, 'timeout': False} for k, n in enumerate(route)]}
    return fn


def _add_path(auto_key, kind='manual'):
    with database.get_connection() as conn:
        return conn.execute('''INSERT INTO route_paths (auto_key, label, source, target, kind, enabled, created_at)
                               VALUES (?, ?, 'src', 'dst', ?, 1, ?)''',
                            (auto_key, auto_key or 'manual', kind, datetime.now().strftime(TS_FMT))).lastrowid


def _run(path_id, fn, n, start):
    events = []
    with database.get_connection() as conn:
        path = dict(conn.execute('SELECT * FROM route_paths WHERE id = ?', (path_id,)).fetchone())
    for i in range(n):
        out = rw.run_paths([path], now=start + timedelta(minutes=15 * i), trace_fn=fn)
        events += out['events']
    return events


def test_divergence_and_site_internal():
    div, old, new, rejoin = rw._divergence('a', ['b', 'c', 'e'], ['b', 'd', 'x', 'e'])
    assert (div, old, new, rejoin) == ('b', ['c'], ['d', 'x'], 'e')
    sites = {'b': 'S', 'c': 'S', 'd': 'S', 'e': 'S'}
    assert rw._site_internal('a', ['b', 'c', 'e'], ['b', 'd', 'e'], sites)
    assert not rw._site_internal('a', ['b', 'c', 'e'], ['b', 'x', 'e'], sites)


def test_role():
    assert rw.path_role({'auto_key': 'start:COL'}) == 'access'
    assert rw.path_role({'auto_key': 'site:COL-MMC'}) == 'backbone'
    assert rw.path_role({'auto_key': None}) == 'backbone'


def test_access_path_never_warns(mesh, monkeypatch):
    _setup(mesh, monkeypatch)
    pid = _add_path('start:BBB', kind='auto')
    start = datetime.now() - timedelta(hours=10)
    # Usual route over infrastructure, then a detour through a non-infrastructure node.
    mesh.node('home', ip='10.0.0.9')
    events = _run(pid, _trace([['b', 'dst']]), 4, start)
    events += _run(pid, _trace([['home', 'x1', 'x2', 'dst']]), 3, start + timedelta(hours=1))
    assert events == []
    report = {p['id']: p for p in rw.list_report()['paths']}
    assert report[pid]['role'] == 'access'
    assert report[pid]['status'] == 'changed'


def test_backbone_detour_keeps_reasons_until_cleared(mesh, monkeypatch):
    _setup(mesh, monkeypatch)
    pid = _add_path(None)
    start = datetime.now() - timedelta(hours=10)
    _run(pid, _trace([['b', 'dst']]), 4, start)
    events = _run(pid, _trace([['b', 'home', 'x1', 'dst']]), 2, start + timedelta(hours=1))
    assert [e['type'] for e in events] == ['route_detour']
    # One trace back on the usual route: still a detour, reasons still shown.
    _run(pid, _trace([['b', 'dst']]), 1, start + timedelta(hours=2))
    p = {p['id']: p for p in rw.list_report()['paths']}[pid]
    assert p['status'] == 'detour' and p['back_on_usual'] and p['reasons']


def test_backbone_flapping(mesh, monkeypatch):
    _setup(mesh, monkeypatch)
    monkeypatch.setattr(config, 'ROUTE_FLAP_CHANGES', 4)
    pid = _add_path('site:AAA-BBB', kind='auto')
    # Two equally good routes through different sites' radios, alternating.
    mesh.infra('b', 'CCC')
    events = _run(pid, _trace([['b', 'dst'], ['c', 'dst']]), 8, datetime.now() - timedelta(hours=6))
    assert 'route_flapping' in [e['type'] for e in events]
    p = {p['id']: p for p in rw.list_report()['paths']}[pid]
    assert p['flapping'] and p['flaps_24h'] >= 4


def test_switches_inside_one_site_are_not_flapping(mesh, monkeypatch):
    _setup(mesh, monkeypatch)
    monkeypatch.setattr(config, 'ROUTE_FLAP_CHANGES', 4)
    pid = _add_path('site:AAA-BBB', kind='auto')
    # Through b, directly or via c (both at the source's site): the routes only
    # differ inside site AAA.
    events = _run(pid, _trace([['b', 'dst'], ['c', 'b', 'dst']]), 8, datetime.now() - timedelta(hours=6))
    assert 'route_flapping' not in [e['type'] for e in events]
    p = {p['id']: p for p in rw.list_report()['paths']}[pid]
    assert p['flaps_24h'] == 0 and p['flaps_internal_24h'] >= 4


def _history(pid, rows):
    with database.get_connection() as conn:
        for t, route in rows:
            hops = [{'hop': i + 1, 'name': n, 'ip': None, 'ms': 5.0, 'timeout': False, 'via': None,
                     'tunnel': False, 'infra': True} for i, n in enumerate(route)]
            conn.execute('''INSERT INTO route_history (path_id, timestamp, ok, reason, source, target, hop_count,
                                                       rtt_ms, signature, hops)
                            VALUES (?, ?, 1, NULL, 'src', 'dst', ?, ?, ?, ?)''',
                         (pid, t.strftime(TS_FMT), len(route), 5.0 + 10 * len(route),
                          json.dumps(route), json.dumps(hops)))


def test_why_changed_causes(mesh, monkeypatch):
    _setup(mesh, monkeypatch)
    mesh.infra('b', 'CCC')
    pid = _add_path(None)
    t = datetime.now() - timedelta(hours=5)
    _history(pid, [(t, ['b', 'dst']), (t + timedelta(minutes=15), ['b', 'dst']),
                   (t + timedelta(minutes=30), ['c', 'dst']),                     # b-dst link went down
                   (t + timedelta(minutes=45), ['c', 'dst']),
                   (t + timedelta(minutes=60), ['b', 'dst']),                     # nothing: near-tie
                   (t + timedelta(minutes=120), ['c', 'dst'])])                   # b rebooted
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO link_state_log (timestamp, source_node, target_node, link_type, state, origin)
                        VALUES (?, 'b', 'dst', 'RF', 'down', 'node_reported')''',
                     ((t + timedelta(minutes=20)).strftime(TS_FMT),))
        conn.execute("INSERT INTO events (timestamp, event_type, node_name, details, severity) "
                     "VALUES (?, 'node_reboot', 'b', 'Node rebooted', 'warning')",
                     ((t + timedelta(minutes=110)).strftime(TS_FMT),))
    w = rw.why_changed(pid, hours=24)
    causes = [c['cause'] for c in reversed(w['changes'])]
    assert causes == ['link_failed', 'near_tie', 'node_rebooted']
    first = w['changes'][-1]
    assert first['divergence'] == {'node': 'src', 'rejoin': 'dst', 'old': ['b'], 'new': ['c']}
    assert w['switches'] == 3 and w['switches_internal'] == 0
    assert {r['share_pct'] for r in w['routes']} == {50.0}

"""Agencies board details (boards._details) and the traceroute mix-up guard (rf_stats)."""

import json
from datetime import datetime, timedelta

import boards
import rf_stats
import route_watch

NOW = datetime(2026, 11, 12, 14, 0, 0)


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')


def test_down_node_has_duration_and_problem(mesh):
    mesh.node('w0aa-eoc-a', last_seen=ts(0))
    mesh.node('w0aa-eoc-b', is_active=0, last_seen=ts(125))
    d = boards._details(['w0aa-eoc-a', 'w0aa-eoc-b'], {
        'w0aa-eoc-a': {'is_active': 1, 'last_seen': ts(0)},
        'w0aa-eoc-b': {'is_active': 0, 'last_seen': ts(125)}}, [], [], NOW)
    down = [n for n in d['nodes'] if not n['up']]
    assert [n['name'] for n in down] == ['w0aa-eoc-b'] and down[0]['down_for'] == '2 h 5 min'
    assert d['problems'] and d['problems'][0].startswith('Node w0aa-eoc-b down for 2 h 5 min')


def test_mixed_up_trace_is_explained(mesh):
    route_watch.ensure_tables()
    mesh.sql("INSERT INTO route_paths (id, label, source, target, kind) VALUES (1, 'COL → EOC', 'col', 'eoc', 'auto')")
    good = json.dumps([{'name': 'mmc'}, {'name': 'eoc'}])
    swapped = json.dumps([{'name': 'mmc'}, {'name': 'mmc-ac2'}])
    for i, (ok, hops) in enumerate([(1, good), (0, swapped), (1, good), (0, swapped), (0, swapped)]):
        mesh.sql('INSERT INTO route_history (path_id, timestamp, ok, reason, hops) VALUES (1, ?, ?, ?, ?)',
                 (ts(60 - i * 15), ok, None if ok else 'target_not_reached', hops))
    path = {'id': 1, 'label': 'COL → EOC', 'source': 'col', 'target': 'eoc', 'enabled': 1, 'status': 'normal',
            'baseline': ['mmc', 'eoc'], 'last': {'ok': False, 'timestamp': ts(0), 'reason': 'target_not_reached',
                                                 'hops': [{'name': 'mmc'}, {'name': 'mmc-ac2'}]}}
    sibling = {'id': 2, 'source': 'col', 'target': 'mmc-ac2', 'enabled': 1}
    row = boards._path_rows([path], NOW, [path, sibling])[0]
    assert row['status'] == 'failing' and row['problem'] and row['fails_24h'] == 3 and row['runs_24h'] == 5
    assert row['failing_since'] == ts(15) and row['down_for'] == '15 min'
    assert 'mixed up' in row['text'] and '3 of the 3 failed traces' in row['text']
    assert row['route_now'] == 'col → mmc → mmc-ac2' and row['usual_route'] == 'col → mmc → eoc'
    # Without a sibling path ending there it is a real failure
    assert 'mixed up' not in boards._path_rows([path], NOW, [path])[0]['text']


def test_unprobed_phone_service_uses_phone_ping(mesh):
    import phones
    phones.ensure_tables()
    mesh.sql("INSERT INTO phones (ip, name, source, enabled, ignored, ok, last_avg, last_check) "
             "VALUES ('10.2.0.38', 'Ext 1075', 'manual', 1, 0, 1, 63.2, ?)", (ts(1),))
    rows = boards._service_rows([
        {'node_name': 'mmc', 'service_name': 'Ext: 1075 Direct IP: 10.2.0.38 [phone]', 'state': 'none',
         'last_error': 'no link advertised'},
        {'node_name': 'eoc', 'service_name': 'x1000 [phone]', 'state': 'none', 'last_error': 'no link advertised'}], NOW)
    assert rows[0]['state'] == 'up' and rows[0]['checked'] and '63 ms' in rows[0]['text']
    assert not rows[1]['checked'] and rows[1]['text'].startswith('Not checked')


class Resp:
    status_code = 200

    def __init__(self, server):
        self.text = (f'SUCCESS\nClient: x\nServer: {server}\ntraceroute to {server} (10.0.0.9), 30 hops max\n'
                     f' 1  {server} (10.0.0.9)  5.0 ms\n')


def test_traceroute_rejects_another_requests_answer(monkeypatch):
    answers = [Resp('other.local.mesh'), Resp('target.local.mesh')]
    monkeypatch.setattr(rf_stats.requests, 'get', lambda url, timeout: answers.pop(0))
    monkeypatch.setattr(rf_stats, '_enrich_hops', lambda hops: None)
    r = rf_stats.traceroute_via_aredn('target', source_node_ip='10.0.0.1')
    assert r and r['hops'][0]['ip'] == '10.0.0.9' and not answers   # retried once, kept the right answer
    monkeypatch.setattr(rf_stats.requests, 'get', lambda url, timeout: Resp('other.local.mesh'))
    assert rf_stats.traceroute_via_aredn('target', source_node_ip='10.0.0.1') is None

import json

import change_log
import database
import scanner


def _link(rxcost):
    return {'raw_tracker': json.dumps({'rxcost': rxcost, 'txcost': rxcost})}


def test_tunnel_cost_change_detected():
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 1000}) == (206, 1000)
    assert scanner.link_cost_change('DtD', _link(96), {'rxcost': 200}) == (96, 200)


def test_no_change_rf_infinity_or_missing():
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 206}) is None
    assert scanner.link_cost_change('RF', _link(300), {'rxcost': 900}) is None
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 65535}) is None
    assert scanner.link_cost_change('Wireguard', _link(65535), {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', None, {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', {'raw_tracker': None}, {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', _link(206), {}) is None


def test_cost_change_event_lands_in_change_log(mesh):
    database.log_event(database.EVENT_LINK_COST_CHANGE, 'w0gq-mmc-ac2',
                       'Wireguard link to kd0cgn-prox Babel cost changed: rxcost 206 -> 1000')
    rows = change_log.list_changes()
    assert len(rows) == 1
    r = rows[0]
    assert r['category'] == 'routing' and r['peer_node'] == 'kd0cgn-prox' and r['source'] == 'auto'
    assert change_log.sync_from_events() == 0


def _msg(node, program, message, ts='2026-11-11 11:57:33'):
    return {'received_at': ts, 'node_time': ts, 'host': node, 'ip': None, 'node_name': node, 'facility': 'user',
            'severity': 6, 'program': program, 'pid': None, 'message': message, 'source': 'ssh'}


def test_entries_carry_their_source(mesh):
    import syslog_rx
    database.log_event(database.EVENT_FIRMWARE_CHANGE, 'w0aa-a', 'Firmware changed: 3.25 -> 4.0')
    change_log.add_change('w0aa-b', 'Raised power', 'power')
    change_log.add_change('w0aa-c', 'Seen in vendor log', 'other', origin='other')
    syslog_rx.store([_msg('w0aa-d', 'upgrade', 'Saving config files...'),
                     _msg('w0aa-d', 'dropbear', 'Child connection from 10.0.0.1:1234', '2026-11-11 11:58:00')])
    by_node = {c['node_name']: c for c in change_log.list_changes()}
    assert {n: c['origin'] for n, c in by_node.items()} == {
        'w0aa-a': 'scan', 'w0aa-b': 'user', 'w0aa-c': 'other', 'w0aa-d': 'syslog'}
    d = by_node['w0aa-d']
    assert d['category'] == 'firmware' and d['timestamp'] == '2026-11-11 11:57:33'
    assert change_log.sync_from_syslog() == 0          # imported once
    assert change_log.delete_change(d['id'])
    assert change_log.sync_from_syslog() == 0          # deleted stays deleted
    assert 'w0aa-d' not in {c['node_name'] for c in change_log.list_changes()}


def test_bad_origin_rejected(mesh):
    import pytest
    with pytest.raises(ValueError):
        change_log.add_change('w0aa-b', 'x', 'other', origin='rumour')

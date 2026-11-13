"""Change log sources (user / syslog / scan / other), cost-setting changes from
saved node config, and editing entries (change_log.py)."""

import pytest

import change_log
import database


UCI_OLD = """babel.tunnel.rxcost='206'
network.wgsV1KghN8c=interface
network.wgsV1KghN8c.cost='206'
network.wgsV1KghN8c.proto='wireguard'"""


def _snap(conn, node, ts, text):
    conn.execute("INSERT INTO ssh_snapshots (node_name, timestamp, kind, label, text) VALUES (?, ?, 'uci', NULL, ?)",
                 (node, ts, text))


def test_cost_setting_changes():
    old = change_log.cost_settings(UCI_OLD)
    assert old == {'babel.tunnel.rxcost': '206', 'network.wgsV1KghN8c.cost': '206'}
    renamed = change_log.cost_settings(UCI_OLD.replace('wgsV1KghN8c', 'wgsGpC4j8kE'))
    assert change_log.cost_setting_changes(old, renamed) == []           # tunnel renamed, same cost
    raised = change_log.cost_settings(UCI_OLD.replace('wgsV1KghN8c', 'wgsGpC4j8kE').replace(".cost='206'", ".cost='1000'"))
    assert change_log.cost_setting_changes(old, raised) == [('network.wgsGpC4j8kE.cost', '206', '1000')]


def test_cost_change_from_saved_config_lands_in_change_log(mesh):
    import ssh_collect
    ssh_collect.ensure_tables()
    with database.get_connection() as conn:
        _snap(conn, 'w0aa-mmc-ac2', '2026-11-11 14:41:52', UCI_OLD)
        _snap(conn, 'w0aa-mmc-ac2', '2026-11-12 14:47:01', UCI_OLD.replace(".cost='206'", ".cost='1000'"))
        _snap(conn, 'w0aa-mmc-ac2', '2026-11-13 14:47:01', UCI_OLD.replace(".cost='206'", ".cost='1000'"))
    rows = change_log.list_changes()
    assert len(rows) == 1
    r = rows[0]
    assert (r['category'], r['origin'], r['timestamp']) == ('routing', 'scan', '2026-11-12 14:47:01')
    assert 'network.wgsV1KghN8c.cost 206 -> 1000' in r['description']
    assert change_log.sync_from_config() == 0


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
    with pytest.raises(ValueError):
        change_log.add_change('w0aa-b', 'x', 'other', origin='rumour')


def test_edit_entry(mesh):
    c = change_log.add_change('w0aa-b', 'route cost to 1000', 'other')
    e = change_log.update_change(c['id'], category='routing', peer_node='W0AA-C', description=' cost 1000 ')
    assert (e['category'], e['peer_node'], e['description'], e['origin']) == ('routing', 'w0aa-c', 'cost 1000', 'user')
    assert change_log.update_change(c['id'], origin='other')['origin'] == 'other'
    with pytest.raises(ValueError):
        change_log.update_change(c['id'], category='nonsense')
    assert change_log.update_change(99999, category='routing') is None


def test_edit_requires_admin_login(mesh):
    from flask import Flask, jsonify
    import auth
    app = Flask(__name__)
    auth.install(app)

    @app.route('/api/changes/<int:change_id>', methods=['PATCH'])
    def edit(change_id):
        return jsonify({'ok': True})

    assert auth.set_password('correct horse')[0]
    assert app.test_client().patch('/api/changes/1', json={'category': 'routing'}).status_code == 401


def test_txpower_setting_change_from_saved_config(mesh):
    import ssh_collect
    ssh_collect.ensure_tables()
    with database.get_connection() as conn:
        _snap(conn, 'w0aa-rkt', '2026-11-12 07:00:00', "wireless.radio0.txpower='23'")
        _snap(conn, 'w0aa-rkt', '2026-11-13 07:00:00', "wireless.radio0.txpower='20'")
    (r,) = change_log.list_changes()
    assert r['category'] == 'power' and 'TX power setting changed: wireless.radio0.txpower 23 -> 20' in r['description']


def test_reported_txpower_and_antenna_changes_are_categorised():
    assert change_log._event_category(database.EVENT_RADIO_CONFIG_CHANGE, 'Radio TX power changed: 20 -> 23') == 'power'
    assert change_log._event_category(database.EVENT_RADIO_CONFIG_CHANGE, 'Radio antenna azimuth changed: 90 -> 120') == 'antenna'
    assert change_log._event_category(database.EVENT_RADIO_CONFIG_CHANGE, 'Radio SSID changed: a -> b') == 'other'
    assert change_log._event_category(database.EVENT_RADIO_CONFIG_CHANGE, 'Radio channel width changed: 10 -> 20') == 'channel'


def test_set_node_radio_config_reports_txpower(mesh):
    mesh.node('w0aa-rkt')
    database.set_node_radio_config('w0aa-rkt', {'rf_mode': 'adhoc', 'rf_txpower': 20.0})
    assert database.set_node_radio_config('w0aa-rkt', {'rf_mode': 'adhoc', 'rf_txpower': 23.0}) == {
        'rf_txpower': (20.0, 23.0)}

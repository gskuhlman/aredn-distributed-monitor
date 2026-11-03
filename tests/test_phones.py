"""Phone connectivity (phones.py): list building, attached node, PBX paste, per-scan pings and alarms."""

from datetime import datetime, timedelta

import config
import gateway
import phones


def test_owner_node_lan_blocks():
    nodes = {'a': '10.0.0.1', 'b': '10.0.0.9', 'big': '10.0.1.1'}
    assert phones.owner_node('10.0.0.3', nodes) == 'a'      # inside a's /29
    assert phones.owner_node('10.0.0.12', nodes) == 'b'     # inside b's /29
    assert phones.owner_node('10.0.0.9', nodes) == 'b'      # the node itself
    assert phones.owner_node('10.0.1.40', nodes) == 'big'   # /26
    assert phones.owner_node('10.0.2.5', nodes) is None
    assert phones.owner_node('not-an-ip', nodes) is None


def test_parse_pbx_text_formats():
    text = '''
  Contact:  1200/sip:1200@10.15.99.115:5060;ob             a1b2c3 Avail        12.345
Name/username             Host                                    Dyn Forcerport Comedia    ACL Port     Status
1300/1300                 10.20.30.40                              D  Auto (No)  No             5060     OK (12 ms)
1400,Shack phone,10.20.30.50
127.0.0.1 ignored
'''
    got = {p['ip']: p for p in phones.parse_pbx_text(text)}
    assert set(got) == {'10.15.99.115', '10.20.30.40', '10.20.30.50'}
    assert got['10.15.99.115']['extension'] == '1200'
    assert got['10.20.30.40']['extension'] == '1300'
    assert got['10.20.30.50']['extension'] == '1400' and got['10.20.30.50']['name'] == 'Shack phone'


def test_sync_from_hosts_and_services(mesh):
    mesh.node('kc9gsk-home', ip='10.15.99.113')
    mesh.node('w0abc-pbx', ip='10.50.0.1')
    with mesh_conn() as conn:
        conn.execute("INSERT INTO services (node_name, name, protocol, link, ip) VALUES "
                     "('w0abc-pbx', 'Hamshack PBX', 'tcp', 'http://w0abc-asterisk.local.mesh:80/', '10.50.0.1')")
    phones.note_hosts([{'name': 'ShackPhoneExt1200', 'ip': '10.15.99.115'},
                       {'name': 'w0abc-asterisk', 'ip': '10.50.0.3'},
                       {'name': 'kc9gsk-home', 'ip': '10.15.99.113'},
                       {'name': 'some-camera', 'ip': '10.15.99.116'}])
    phones.save_hosts()
    phones.sync()
    rep = phones.report()
    by_ip = {p['ip']: p for p in rep['phones']}
    assert set(by_ip) == {'10.15.99.115', '10.50.0.3'}
    assert by_ip['10.15.99.115']['attached_node'] == 'kc9gsk-home'
    assert by_ip['10.15.99.115']['source'] == 'host'
    assert by_ip['10.50.0.3']['kind'] == 'pbx' and by_ip['10.50.0.3']['source'] == 'service'
    assert by_ip['10.50.0.3']['attached_node'] == 'w0abc-pbx'
    # The mesh host search leaves listed phones out.
    assert [h['ip'] for h in phones.hosts('10.15.99')] == ['10.15.99.116']


def test_service_ip_from_link_or_name_and_unresolved(mesh):
    mesh.node('ke9fcx-haplite2', ip='10.215.168.33')
    mesh.node('w0gq-mmc-ac2', ip='10.204.160.33')
    mesh.node('w0gq-eoc-ac3', ip='10.95.35.193')
    mesh.node('kd9usw-rt', ip='10.224.199.65')
    with mesh_conn() as conn:
        conn.executemany("INSERT INTO services (node_name, name, protocol, link, ip) VALUES (?, ?, 'tcp', ?, ?)", [
            ('ke9fcx-haplite2', 'KE9FCX-cordless [phone]', 'http://KE9FCX-hAPlite2:5060/10.215.168.38', '10.215.168.33'),
            ('w0gq-mmc-ac2', 'MMC Comms Rm Ext: 1075 Direct IP: 10.204.160.38 [phone]', '', '10.204.160.33'),
            ('w0gq-eoc-ac3', 'x1000 [phone]', '', '10.95.35.193'),
            ('kd9usw-rt', 'WI AREDN Mesh Phone Book', 'http://kd9usw-pbx:80//phonebook', '10.224.199.65'),
        ])
    phones.note_hosts([{'name': 'KE9FCX-hAPlite2', 'ip': '10.215.168.33'}])
    phones.save_hosts()
    phones.sync()
    rep = phones.report()
    by_ip = {p['ip']: p for p in rep['phones']}
    assert set(by_ip) == {'10.215.168.38', '10.204.160.38'}
    assert by_ip['10.204.160.38']['extension'] == '1075'
    assert by_ip['10.215.168.38']['attached_node'] == 'ke9fcx-haplite2'
    assert [(u['node'], u['extension']) for u in rep['unresolved']] == [('w0gq-eoc-ac3', '1000')]


def mesh_conn():
    import database
    return database.get_connection()


def test_run_alarms_after_two_failed_scans_and_restores(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PHONE_PING_ENABLED', True)
    monkeypatch.setattr(config, 'PHONE_PING_FAILS', 2)
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: False)
    mesh.node('kc9gsk-home', ip='10.15.99.113')
    pid = phones.save_phone({'ip': '10.15.99.115', 'name': 'Shack', 'extension': '1200'})
    up = lambda ips: {ip: {'avg': 12.0, 'jitter': 1.0, 'loss': 0.0} for ip in ips}  # noqa: E731
    down = lambda ips: {ip: {'avg': None, 'loss': 100.0} for ip in ips}  # noqa: E731
    t0 = datetime.now()
    assert phones.run(now=t0, pinger=up)['checked'] == 1
    assert phones.run(now=t0 + timedelta(minutes=3), pinger=down)['events'] == []
    ev = phones.run(now=t0 + timedelta(minutes=6), pinger=down)['events']
    assert [e['event_type'] for e in ev] == ['phone_unreachable']
    assert ev[0]['node_name'] == 'kc9gsk-home' and 'ext 1200' in ev[0]['details']
    rep = phones.report(now=t0 + timedelta(minutes=7))
    p = next(x for x in rep['phones'] if x['id'] == pid)
    assert p['state'] == 'alarm' and p['window_checks'] == 3 and p['window_pct'] == 33.3
    ev = phones.run(now=t0 + timedelta(minutes=9), pinger=up)['events']
    assert [e['event_type'] for e in ev] == ['phone_restored']


def test_run_holds_when_node_down_or_gateway_lost(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PHONE_PING_ENABLED', True)
    monkeypatch.setattr(config, 'PHONE_PING_FAILS', 1)
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: False)
    mesh.node('kc9gsk-home', ip='10.15.99.113', is_active=0)
    phones.save_phone({'ip': '10.15.99.115'})
    down = lambda ips: {ip: None for ip in ips}  # noqa: E731
    assert phones.run(pinger=down)['events'] == []          # node down: the scanner's alarm
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: True)
    res = phones.run(pinger=down)
    assert res['aborted'] and res['checked'] == 0           # our own outage: discarded
    with mesh_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM phone_pings').fetchone()[0] == 1


def test_hosts_off_our_mesh_are_left_out_and_dropped(mesh):
    mesh.node('kc9gsk-home', ip='10.1.1.1')
    phones.note_hosts([{'name': 'yealink-t46', 'ip': '10.1.1.5'}, {'name': 'VA7FI-PHONE', 'ip': '10.119.174.211'}])
    phones.save_hosts()
    phones.sync()
    assert [p['ip'] for p in phones.report()['phones']] == ['10.1.1.5']
    mesh.sql("DELETE FROM mesh_hosts WHERE ip = '10.1.1.5'")
    phones.note_hosts([{'name': 'VA7FI-PHONE', 'ip': '10.119.174.211'}])
    phones.save_hosts()
    phones.sync(now=datetime.now() + timedelta(seconds=5))
    assert phones.report()['phones'] == []


def test_delete_hides_auto_entries(mesh):
    mesh.node('kc9gsk-home', ip='10.1.1.1')
    phones.note_hosts([{'name': 'yealink-t46', 'ip': '10.1.1.5'}])
    phones.save_hosts()
    phones.sync()
    auto = phones.report()['phones'][0]
    manual = phones.save_phone({'ip': '10.1.1.6', 'name': 'desk'})
    phones.delete_phone(auto['id'])
    phones.delete_phone(manual)
    rows = phones.report()['phones']
    assert [(p['ip'], p['state']) for p in rows] == [('10.1.1.5', 'ignored')]


def test_ping_one_is_shown_not_stored(mesh, monkeypatch):
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: False)
    monkeypatch.setattr(phones, '_ping', lambda ip: {'avg': 20.0, 'jitter': 1.0, 'loss': 0.0})
    pid = phones.save_phone({'ip': '10.1.1.6'})
    assert phones.ping_one(pid) == {'ip': '10.1.1.6', 'ok': True, 'avg': 20.0, 'jitter': 1.0, 'loss': 0.0}
    with mesh_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM phone_pings').fetchone()[0] == 0
        assert conn.execute('SELECT checks FROM phones WHERE id = ?', (pid,)).fetchone()[0] == 0
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: False)
    assert phones.ping_one(pid)['aborted']

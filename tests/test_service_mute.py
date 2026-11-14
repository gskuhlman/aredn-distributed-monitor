"""Silence / ignore one service on one node (service_check.service_mutes)."""

from datetime import datetime, timedelta

import pytest

import config
import database
import gateway
import service_check


@pytest.fixture
def svc(mesh, monkeypatch):
    mesh.node('w0aa-pbx')
    with database.get_connection() as conn:
        conn.execute("INSERT INTO services (node_name, name, protocol, link, ip) VALUES (?, ?, ?, ?, ?)",
                     ('w0aa-pbx', 'PBX', 'tcp', 'sip://w0aa-pbx:5060/', '10.0.0.1'))
    monkeypatch.setattr(gateway, 'is_reachable', lambda: True)
    monkeypatch.setattr(config, 'SERVICE_CHECK_FAILS', 2)
    state = {'ok': False, 'probed': 0}

    def probe(s):
        state['probed'] += 1
        return (state['ok'], None if state['ok'] else 'refused', 5, 'tcp')
    monkeypatch.setattr(service_check, 'probe', probe)
    monkeypatch.setattr(database, 'log_event', lambda *a, **k: None)
    monkeypatch.setattr(service_check, '_node_up', lambda node, now: True)
    return state


def _kinds(result):
    return [e['event_type'] for e in result['events']]


def test_silence_holds_alarm_then_alarms_after(svc):
    now = datetime.now()
    mute, err = service_check.set_mute('w0aa-pbx', 'PBX', 'silence', 1, now=now)
    assert not err and mute['until']
    for i in range(3):
        assert _kinds(service_check.run(now + timedelta(minutes=5 * i))) == []
    r = service_check.report()
    assert r['services'][0]['state'] == 'silenced' and r['counts'].get('alarm') is None
    # Silence over: still failing -> alarms on the next failed check, mute dropped.
    assert _kinds(service_check.run(now + timedelta(hours=1, minutes=5))) == ['service_down']
    assert service_check.report()['mutes'] == []


def test_silence_clears_existing_alarm_and_restore_is_quiet(svc):
    now = datetime.now()
    service_check.run(now)
    assert _kinds(service_check.run(now + timedelta(minutes=5))) == ['service_down']
    service_check.set_mute('w0aa-pbx', 'PBX', 'silence', None, now=now)     # until enabled again
    assert service_check.report()['counts'].get('alarm') is None
    svc['ok'] = True
    assert _kinds(service_check.run(now + timedelta(days=60))) == []
    assert service_check.report()['mutes'][0]['until'] is None


def test_ignore_skips_probe_and_enable_resumes(svc):
    now = datetime.now()
    service_check.run(now)
    service_check.set_mute('w0aa-pbx', 'PBX', 'ignore', 6, now=now)        # hours ignored for ignore
    n = svc['probed']
    for i in range(3):
        assert _kinds(service_check.run(now + timedelta(minutes=5 * (i + 1)))) == []
    assert svc['probed'] == n
    r = service_check.report()
    assert r['services'][0]['state'] == 'ignored' and r['mutes'][0]['until'] is None
    assert service_check.clear_mute('w0aa-pbx', 'PBX')
    assert not service_check.clear_mute('w0aa-pbx', 'PBX')
    service_check.run(now + timedelta(minutes=30))
    assert _kinds(service_check.run(now + timedelta(minutes=35))) == ['service_down']


def test_bad_input(svc):
    assert service_check.set_mute('w0aa-pbx', 'PBX', 'mute')[1]
    assert service_check.set_mute('w0aa-pbx', 'PBX', 'silence', 5)[1]
    assert service_check.set_mute('', 'PBX', 'silence', 1)[1]
    for h in (1, 6, 24, 168, 720, None):
        assert not service_check.set_mute('w0aa-pbx', 'PBX', 'silence', h)[1]


def test_device_mute_quiets_phone_and_counts(svc, monkeypatch):
    import phones
    import services_catalog
    phones.ensure_tables()
    with database.get_connection() as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(phones)')}
        row = {'name': 'shack-phone', 'ip': '10.0.0.9', 'enabled': 1, 'ignored': 0, 'source': 'manual'}
        row = {k: v for k, v in row.items() if k in cols}
        conn.execute(f"INSERT INTO phones ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    monkeypatch.setattr(phones, 'save_hosts', lambda now=None: None)
    monkeypatch.setattr(phones, 'sync', lambda now=None: None)
    monkeypatch.setattr(phones.gateway, 'failure_is_ours', lambda: False)
    monkeypatch.setattr(config, 'PHONE_PING_ENABLED', True)
    monkeypatch.setattr(config, 'PHONE_PING_FAILS', 1)
    pinged = []

    def pinger(ips):
        pinged.extend(ips)
        return {ip: {'loss': 100.0} for ip in ips}
    service_check.set_mute('w0aa-pbx', 'device:10.0.0.9', 'silence', 6)
    assert phones.run(pinger=pinger)['events'] == [] and pinged == ['10.0.0.9']
    service_check.set_mute('w0aa-pbx', 'device:10.0.0.9', 'ignore')
    service_check.set_mute('w0aa-pbx', 'PBX', 'silence', None)
    pinged.clear()
    phones.run(pinger=pinger)
    assert pinged == []
    r = service_check.report()
    assert r['mute_counts'] == {'silenced': 1, 'ignored': 1}
    assert {m['kind'] for m in r['mutes']} == {'device', 'service'}
    cat = services_catalog.catalog()
    assert cat['counts']['muted'] == 1
    service_check.clear_mute('w0aa-pbx', 'device:10.0.0.9')
    assert phones.run(pinger=pinger)['events'][0]['event_type'] == 'phone_unreachable'

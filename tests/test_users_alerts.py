"""User accounts (users.py), per-service and infrastructure-team alerts
(user_alerts.py), the user / admin permission split (auth.py) and status
notes (status_notes.py)."""

from datetime import datetime, timedelta

import pytest
from flask import Flask, jsonify, request

import auth
import database
import notify
import status_notes
import user_alerts
import users


@pytest.fixture
def owners(mesh):
    mesh.node('kf9mt-home-litebeam')
    mesh.node('w0gq-col-rkt-s')
    mesh.node('wonewoc-dstar')
    with database.get_connection() as conn:
        conn.execute("INSERT INTO services (node_name, name, protocol, link, ip) VALUES (?, ?, ?, ?, ?)",
                     ('kf9mt-home-litebeam', 'Cam (2)', 'tcp', 'http://kf9mt-cam:80/', '10.0.0.5'))
    assert users.sync_from_nodes() == 2          # wonewoc is not a callsign
    assert users.sync_from_nodes() == 0
    return mesh


def test_users_created_without_password(owners):
    u = users.get('kf9mt', private=True)
    assert u and not u['has_password'] and u['nodes'] == ['kf9mt-home-litebeam']
    ok, error = users.check_password('kf9mt', 'anything')
    assert not ok and 'No password' in error
    assert users.set_password('kf9mt', 'short')[1]
    assert users.set_password('kf9mt', 'correct horse')[0]
    assert users.check_password('kf9mt', 'correct horse')[0]
    assert not users.change_password('kf9mt', 'wrong', 'another pass')[0]
    assert users.change_password('kf9mt', 'correct horse', 'another pass')[0]
    # Users can't give themselves admin or team; contacts are checked.
    assert users.update('kf9mt', {'is_admin': True})[1]
    assert users.update('kf9mt', {'email': 'nope'})[1]
    u, err = users.update('kf9mt', {'email': 'kf9mt@example.com', 'ntfy_topic': 'kf9mt-alerts'})
    assert not err and u['email'] == 'kf9mt@example.com'
    assert users.update('kf9mt', {'is_admin': True}, as_admin=True)[0]['is_admin']
    assert users.delete('kf9mt')[1]                 # owns a node
    assert users.create('n0new')[0] and users.delete('n0new')[0]


def test_service_recipients_default_owner(owners):
    rec = user_alerts.recipients('kf9mt-home-litebeam', 'Cam (2)')
    assert rec == [{'callsign': 'kf9mt', 'owner': True, 'enabled': True}]
    rec, err = user_alerts.set_recipient('kf9mt-home-litebeam', 'Cam (2)', 'w0gq', True)
    assert not err and [r['callsign'] for r in rec if r['enabled']] == ['kf9mt', 'w0gq']
    rec, _ = user_alerts.set_recipient('kf9mt-home-litebeam', 'Cam (2)', 'kf9mt', False)
    assert rec[0] == {'callsign': 'kf9mt', 'owner': True, 'enabled': False}
    assert user_alerts.set_recipient('kf9mt-home-litebeam', 'Nope', 'w0gq', True)[1]
    assert user_alerts.set_recipient('kf9mt-home-litebeam', 'Cam (2)', 'zz9zz', True)[1]
    assert [s['enabled'] for s in user_alerts.my_services('kf9mt')] == [False]
    assert user_alerts.my_services('w0gq')[0]['service'] == 'Cam (2)'


def test_targets_service_and_infra_team(owners, monkeypatch):
    users.update('kf9mt', {'email': 'kf9mt@example.com'})
    users.update('w0gq', {'ntfy_topic': 'w0gq-team', 'infra_team': True, 'infra_groups': ['nodes', 'services']},
                 as_admin=True)
    people = {u['callsign']: users.raw(u['callsign']) for u in users.list_users()}
    infra = {'w0gq-col-rkt-s'}
    down = {'event_type': 'service_down', 'node_name': 'kf9mt-home-litebeam', 'severity': 'warning',
            'details': 'Service Cam (2) (http://kf9mt-cam:80/) not answering: refused; node kf9mt-home-litebeam is up'}
    assert user_alerts.service_of(down) == 'Cam (2)'
    assert [cs for cs, _ in user_alerts.targets(down, people, infra)] == ['kf9mt']
    off = {'event_type': 'node_offline', 'node_name': 'w0gq-col-rkt-s', 'severity': 'warning', 'details': 'Node offline'}
    assert [(cs, r['id']) for cs, r in user_alerts.targets(off, people, infra)] == [('w0gq', 'infra')]
    # Below the member's minimum severity, or a group they didn't pick: nothing.
    assert user_alerts.targets({**off, 'severity': 'info'}, people, infra) == []
    noise = {'event_type': 'noise_jump', 'node_name': 'w0gq-col-rkt-s', 'severity': 'warning', 'details': 'x'}
    assert user_alerts.targets(noise, people, infra) == []
    # Not an infrastructure node: the team doesn't hear about it.
    assert user_alerts.targets({**off, 'node_name': 'kf9mt-home-litebeam'}, people, infra) == []
    # Owner who also is on the team gets one message, not two.
    user_alerts.set_recipient('kf9mt-home-litebeam', 'Cam (2)', 'w0gq', True)
    t = user_alerts.targets(down, people, {'kf9mt-home-litebeam'})
    assert sorted(cs for cs, _ in t) == ['kf9mt', 'w0gq'] and all(r['id'] == 'svc' for _, r in t)
    # No destination: no alert.
    users.update('kf9mt', {'email': ''})
    people = {u['callsign']: users.raw(u['callsign']) for u in users.list_users()}
    assert 'kf9mt' not in [cs for cs, _ in user_alerts.targets(down, people, infra)]


def test_user_pipeline_sends_to_the_user(owners, monkeypatch):
    users.update('kf9mt', {'ntfy_topic': 'kf9mt-alerts'})
    sent = []
    d = notify.Dispatcher(sender=lambda ch, title, body, cfg=None, alerts=None, critical=False:
                          sent.append((ch, cfg['channels'][ch].get('topic'), title)), acks=False)
    monkeypatch.setattr(user_alerts, '_dispatcher', lambda cs: d)
    monkeypatch.setattr(notify, 'is_suppressed', lambda e: None)
    user_alerts._state['loaded'] = 0
    base = notify.get_config(raw=True)
    base['digest_seconds'] = 0
    base['flap_minutes'] = 0
    monkeypatch.setattr(notify, 'get_config', lambda raw=False: base)
    user_alerts.process({'id': 1, 'event_type': 'service_down', 'node_name': 'kf9mt-home-litebeam', 'severity': 'warning',
                         'timestamp': '2026-11-15 10:00:00',
                         'details': 'Service Cam (2) (http://kf9mt-cam:80/) not answering: refused'})
    assert sent and sent[0][:2] == ('ntfy', 'kf9mt-alerts')


@pytest.fixture
def client(owners):
    app = Flask(__name__)
    auth.install(app)

    @app.route('/api/auth/login', methods=['POST'])
    def login():
        b = request.get_json() or {}
        ok, error, code = auth.login(b.get('password'), b.get('user'))
        return jsonify({'ok': ok, 'error': error}), code

    @app.route('/api/scan', methods=['POST'])
    def scan():
        return jsonify({'ok': True})

    @app.route('/api/me', methods=['PATCH'])
    def me():
        return jsonify({'cs': auth.current_callsign(), 'admin': auth.is_admin()})
    return app.test_client()


def test_user_login_and_permissions(client):
    auth.set_password('admin password')
    auth._failures.clear()
    r = client.post('/api/auth/login', json={'user': 'KF9MT', 'password': 'x'})
    assert r.status_code == 400 and 'No password' in r.get_json()['error']
    users.set_password('kf9mt', 'correct horse')
    assert client.post('/api/auth/login', json={'user': 'kf9mt', 'password': 'correct horse'}).status_code == 200
    assert client.patch('/api/me').get_json() == {'cs': 'kf9mt', 'admin': False}
    r = client.post('/api/scan')
    assert r.status_code == 403 and r.get_json()['admin_required']
    users.update('kf9mt', {'is_admin': True}, as_admin=True)        # granted: applies at once
    assert client.post('/api/scan').status_code == 200
    users.update('kf9mt', {'is_admin': False}, as_admin=True)
    assert client.post('/api/scan').status_code == 403
    # The admin password still works and is admin.
    assert client.post('/api/auth/login', json={'password': 'admin password'}).status_code == 200
    assert client.post('/api/scan').status_code == 200
    auth._failures.clear()


def test_status_notes(mesh):
    now = datetime(2026, 11, 15, 9, 0)
    assert status_notes.add('', now=now)[1]
    assert status_notes.add('x', ends_at='2026-11-15 08:00', now=now)[1]
    n, err = status_notes.add('Collins tower work 9-12', ends_at='2026-11-15 12:00', level='warning', by='admin', now=now)
    assert not err
    later, _ = status_notes.add('Net night', starts_at='2026-11-17 19:00', now=now)
    assert [x['text'] for x in status_notes.active(now)] == ['Collins tower work 9-12']
    assert [x['showing'] for x in status_notes.upcoming_and_active(now)] == [True, False]
    assert status_notes.active(datetime(2026, 11, 15, 12, 30)) == []
    assert status_notes.delete(later['id'])


def test_owner_and_name(owners):
    assert users.owns('kf9mt', 'kf9mt-home-litebeam')
    assert not users.owns('kf9mt', 'w0gq-col-rkt-s')
    assert not users.owns(None, 'kf9mt-home-litebeam')
    u, err = users.update('kf9mt', {'name': '  Rickie   Sexton '})
    assert err is None and u['name'] == 'Rickie Sexton'
    assert any(p.match('/api/services/mute') for p in auth.USER_PATHS)

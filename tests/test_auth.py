"""Login for changes: open until a password is set; then actions need a
login, reads and preview POSTs don't."""

import pytest
from flask import Flask, jsonify

import auth


@pytest.fixture
def client(mesh):
    app = Flask(__name__)
    auth.install(app)

    @app.route('/api/thing', methods=['GET', 'POST', 'DELETE'])
    def thing():
        return jsonify({'ok': True})

    @app.route('/api/channel-planner', methods=['POST'])
    def planner():
        return jsonify({'ok': True})

    @app.route('/api/auth/login', methods=['POST'])
    def login():
        from flask import request
        ok, error, code = auth.login((request.get_json() or {}).get('password'))
        return jsonify({'ok': ok, 'error': error}), code

    return app.test_client()


def test_open_without_password(client):
    assert client.post('/api/thing').status_code == 200


def test_actions_need_login(client):
    ok, _, _ = auth.set_password('correct horse')
    assert ok
    assert client.get('/api/thing').status_code == 200
    r = client.post('/api/thing')
    assert r.status_code == 401 and r.get_json()['login_required']
    assert client.delete('/api/thing').status_code == 401
    assert client.post('/api/channel-planner').status_code == 200      # preview stays open
    assert client.post('/api/auth/login', json={'password': 'nope'}).status_code == 401
    assert client.post('/api/auth/login', json={'password': 'correct horse'}).status_code == 200
    assert client.post('/api/thing').status_code == 200


def test_short_password_refused(client):
    ok, error, code = auth.set_password('short')
    assert not ok and code == 400


def test_login_rate_limited(client):
    auth.set_password('correct horse')
    auth._failures.clear()
    for _ in range(auth.MAX_FAILS):
        client.post('/api/auth/login', json={'password': 'bad'})
    assert client.post('/api/auth/login', json={'password': 'correct horse'}).status_code == 429
    auth._failures.clear()

"""
Login for changes (Admin page). Reading stays open; when a password is set,
every action (POST/PUT/PATCH/DELETE under /api/) needs a login, except the
read-like POSTs in OPEN_POSTS (what-if previews, the planner, live
diagnostics, scans).

The password is either ADMIN_PASSWORD (environment) or a salted hash stored
in the local SQLite `auth_config` table (set on the Admin page). Like the
notification secrets it is never returned by any API, written to CouchDB or
logged. With AUTH_PROXY_HEADER set (e.g. "X-Remote-User"), a request carrying
that header from the reverse proxy counts as logged in.

The Flask session key is random per install (stored in `auth_config`), not the
fixed key in app.py, so a session cookie can't be forged from the source.
Logins are rate limited per client address.
"""

import hmac
import re
import secrets
import time
from datetime import datetime, timedelta

from flask import jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import config
import database

PROTECTED_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')
OPEN_POSTS = [re.compile(p) for p in (
    r'^/api/auth/(login|logout)$',
    r'^/api/channel-map/what-if$',
    r'^/api/channel-planner$',
    r'^/api/reports/(airtime|site-matrix|infrastructure)$',
    r'^/api/rf-stats/diagnose/[^/]+/[^/]+/live$',
    r'^/api/path/live$',
    r'^/api/(ping|traceroute)/[^/]+$',
    r'^/api/scan$',
    r'^/api/nodes/scan/[^/]+$',
    r'^/api/collector/health/check$',
    r'^/api/weather/refresh$',
    r'^/api/routes/\d+/run$',
)]
MAX_FAILS = 5
FAIL_WINDOW_S = 300

_failures = {}
_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS auth_config (key TEXT PRIMARY KEY, value TEXT)')
    _tables_ready = True


def _get(key):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT value FROM auth_config WHERE key = ?', (key,)).fetchone()
    return r[0] if r else None


def _set(key, value):
    ensure_tables()
    with database.get_connection() as conn:
        if value is None:
            conn.execute('DELETE FROM auth_config WHERE key = ?', (key,))
        else:
            conn.execute('INSERT OR REPLACE INTO auth_config (key, value) VALUES (?, ?)', (key, value))


def secret_key():
    key = _get('session_secret')
    if not key:
        key = secrets.token_hex(32)
        _set('session_secret', key)
    return key


def password_source():
    if config.ADMIN_PASSWORD:
        return 'env'
    return 'stored' if _get('password_hash') else None


def enabled():
    return bool(password_source() or config.AUTH_PROXY_HEADER)


def check_password(password):
    if config.ADMIN_PASSWORD:
        return hmac.compare_digest(str(password or ''), config.ADMIN_PASSWORD)
    h = _get('password_hash')
    return bool(h) and check_password_hash(h, str(password or ''))


def proxy_user():
    if config.AUTH_PROXY_HEADER:
        return request.headers.get(config.AUTH_PROXY_HEADER) or None
    return None


def logged_in():
    if proxy_user():
        return True
    until = session.get('auth_until')
    return bool(until and until > time.time())


def current_user():
    return proxy_user() or (session.get('auth_user') if logged_in() else None)


def _open(path):
    return any(p.match(path) for p in OPEN_POSTS)


def guard():
    """Flask before_request hook."""
    if request.method not in PROTECTED_METHODS or not request.path.startswith('/api/'):
        return None
    if _open(request.path) or not enabled() or logged_in():
        return None
    return jsonify({'error': 'Log in to make changes', 'login_required': True}), 401


def _client():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()


def login(password, user=None):
    now = time.time()
    who = _client()
    fails = [t for t in _failures.get(who, []) if now - t < FAIL_WINDOW_S]
    if len(fails) >= MAX_FAILS:
        return False, 'Too many failed logins; wait a few minutes.', 429
    if not password_source():
        return False, 'No password is set.', 400
    if not check_password(password):
        fails.append(now)
        _failures[who] = fails
        database.log_event('login_failed', None, f'Failed login from {who}', 'warning')
        return False, 'Wrong password.', 401
    _failures.pop(who, None)
    session.permanent = True
    session['auth_until'] = now + config.AUTH_SESSION_HOURS * 3600
    session['auth_user'] = (user or 'operator')[:60]
    return True, None, 200


def logout():
    session.pop('auth_until', None)
    session.pop('auth_user', None)


def set_password(new, current=None):
    """Set, change or (new='') clear the stored password."""
    if config.ADMIN_PASSWORD:
        return False, 'The password comes from ADMIN_PASSWORD in the environment; change it there.', 400
    if password_source() and not logged_in() and not check_password(current):
        return False, 'Log in (or give the current password) first.', 401
    new = str(new or '')
    if new and len(new) < 8:
        return False, 'Use at least 8 characters.', 400
    _set('password_hash', generate_password_hash(new) if new else None)
    database.log_event('auth_password_changed', None,
                       'Admin password ' + ('set' if new else 'removed: actions are open to everyone'), 'warning')
    return True, None, 200


def status():
    return {'enabled': enabled(), 'source': password_source(), 'proxy_header': config.AUTH_PROXY_HEADER or None,
            'logged_in': logged_in(), 'user': current_user(),
            'expires': (datetime.fromtimestamp(session['auth_until']).strftime('%Y-%m-%d %H:%M')
                        if logged_in() and session.get('auth_until') else None)}


def install(app):
    app.secret_key = secret_key()
    app.permanent_session_lifetime = timedelta(hours=config.AUTH_SESSION_HOURS)
    app.before_request(guard)

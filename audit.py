"""
Audit log (TODO #50): who changed what and when. A Flask after_request hook
records every POST/PUT/PATCH/DELETE under /api/ except pure previews and
read-like checks (`SKIP`: what-if, planner, live diagnostics, pings, Check now
buttons): method, path, a readable action, HTTP status (refused and failed
attempts too), client address, the logged-in user (auth.current_user) and a
short session tag, and the request body with secrets masked (keys like
password / secret / token / key / webhook) and long values cut.

SQLite `audit_log`, kept AUDIT_RETENTION_DAYS. Local operator data, never
replicated to CouchDB. `GET /api/audit?limit&q&since` (Admin page).
"""

import hashlib
import json
import re
from datetime import datetime, timedelta

import config
import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')
SKIP = [re.compile(p) for p in (
    r'^/api/channel-map/what-if$',
    r'^/api/spectrum$',
    r'^/api/channel-planner$',
    r'^/api/reports/(airtime|site-matrix)$',
    r'^/api/rf-stats/diagnose/[^/]+/[^/]+/live$',
    r'^/api/path/live$',
    r'^/api/(ping|traceroute)/[^/]+$',
    r'^/api/collector/health/check$',
    r'^/api/voip/phones/\d+/ping$',
    r'^/api/voip/session/(status|event|refresh-routes)$',
)]
SECRET_KEY = re.compile(r'pass|secret|token|key$|^key|webhook|private', re.I)
MAX_VALUE = 200
MAX_BODY = 2000

# (method, path pattern, label): first match wins; else "<METHOD> <path>".
ACTIONS = [(m, re.compile(p), label) for m, p, label in (
    ('POST', r'^/api/auth/login$', 'Log in'),
    ('POST', r'^/api/auth/logout$', 'Log out'),
    ('POST', r'^/api/auth/password$', 'Change admin password'),
    ('POST', r'^/api/settings$', 'Change settings'),
    ('POST', r'^/api/scan$', 'Start a scan'),
    ('POST', r'^/api/nodes/scan/', 'Scan one node'),
    ('POST', r'^/api/cleanup/delete$', 'Delete nodes'),
    ('POST', r'^/api/cleanup/exclude$', 'Exclude nodes from scans'),
    ('POST', r'^/api/cleanup/unexclude$', 'Un-exclude nodes'),
    ('POST', r'^/api/scan-boundaries/', 'Change a scan boundary'),
    ('POST', r'^/api/nodes/infrastructure/', 'Mark infrastructure'),
    ('DELETE', r'^/api/node', 'Delete a node'),
    ('POST', r'^/api/maintenance$', 'Create maintenance window'),
    ('PATCH', r'^/api/maintenance/', 'Change maintenance window'),
    ('DELETE', r'^/api/maintenance/', 'Cancel maintenance window'),
    ('POST', r'^/api/rollouts$', 'Start a rollout'),
    (None, r'^/api/rollouts/', 'Change a rollout'),
    ('POST', r'^/api/changes$', 'Add change-log entry'),
    ('DELETE', r'^/api/changes/', 'Delete change-log entry'),
    (None, r'^/api/notes', 'Edit notes'),
    (None, r'^/api/notifications/', 'Change notifications'),
    (None, r'^/api/suppressions', 'Change suppressions'),
    (None, r'^/api/incidents', 'Change incidents'),
    (None, r'^/api/alerts/', 'Acknowledge alert'),
    ('POST', r'^/api/admin/database/backup$', 'Back up database'),
    ('POST', r'^/api/admin/database/vacuum$', 'Vacuum database'),
    (None, r'^/api/ssh/', 'SSH collection'),
    (None, r'^/api/wifi-survey', 'WiFi survey'),
    (None, r'^/api/link-alerts', 'Change link alert rules'),
    (None, r'^/api/routes', 'Change Route Watch paths'),
    (None, r'^/api/voip/phones', 'Change phones'),
    (None, r'^/api/channel-planner/protected$', 'Change protected radios'),
    (None, r'^/api/capacity/', 'Capacity / iPerf'),
    (None, r'^/api/weekly-reports', 'Weekly report'),
    (None, r'^/api/rf-stats/', 'RF Stats action'),
)]

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            method TEXT, path TEXT, action TEXT, status INTEGER,
            client TEXT, user TEXT, session TEXT, body TEXT)''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC)')
    _tables_ready = True


def action_label(method, path):
    for m, pat, label in ACTIONS:
        if (m is None or m == method) and pat.search(path):
            return label
    return f'{method} {path}'


def redact(value, depth=0):
    """Body with secret-looking keys masked and long strings / lists cut."""
    if isinstance(value, dict):
        return {k: ('********' if SECRET_KEY.search(str(k)) and v not in (None, '', '********')
                    else redact(v, depth + 1)) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        out = [redact(v, depth + 1) for v in value[:50]]
        return out + [f'... {len(value) - 50} more'] if len(value) > 50 else out
    if isinstance(value, str) and len(value) > MAX_VALUE:
        return value[:MAX_VALUE] + f'... ({len(value)} chars)'
    return value


def body_text(req):
    if req.path.startswith('/api/auth/'):
        return None                   # never anything from the login / password forms
    data = req.get_json(silent=True)
    if data is None:
        return None
    text = json.dumps(redact(data), default=str, ensure_ascii=False)
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + '...'


def record(method, path, status, client=None, user=None, session_tag=None, body=None, now=None):
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO audit_log (timestamp, method, path, action, status, client, user, session, body)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     ((now or datetime.now()).strftime(TS_FMT), method, path, action_label(method, path), status,
                      client, user, session_tag, body))


def after_request(response):
    from flask import request, session
    try:
        if request.method in METHODS and request.path.startswith('/api/') \
                and not any(p.match(request.path) for p in SKIP):
            import auth
            user = auth.current_user()
            until = session.get('auth_until')
            tag = hashlib.sha256(f"{config.COLLECTOR_ID}:{until}".encode()).hexdigest()[:8] if until else None
            record(request.method, request.path, response.status_code, auth._client(), user, tag,
                   body_text(request))
    except Exception:
        pass                          # the audit log never breaks a request
    return response


def list_entries(limit=200, q=None, since=None):
    ensure_tables()
    sql, args = 'SELECT * FROM audit_log WHERE 1=1', []
    if since:
        sql += ' AND timestamp >= ?'
        args.append(since)
    if q:
        sql += ' AND (action LIKE ? OR path LIKE ? OR body LIKE ? OR user LIKE ? OR client LIKE ?)'
        args += [f'%{q}%'] * 5
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(max(1, min(int(limit or 200), 2000)))
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def cleanup(now=None):
    ensure_tables()
    cut = ((now or datetime.now()) - timedelta(days=config.AUDIT_RETENTION_DAYS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM audit_log WHERE timestamp < ?', (cut,)).rowcount


def install(app):
    app.after_request(after_request)

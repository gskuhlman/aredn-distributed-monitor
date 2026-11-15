"""
User accounts, one per node-owner callsign (Account page, /account).

Every callsign that starts a node name (`kf9mt-home-litebeam` -> `kf9mt`) gets
a user automatically (sync_from_nodes, after each committed scan and at
start); the user id is the callsign, lower case. A new user has no password,
so it can't log in until an admin sets one (Account page, admin section);
then the user can change it. Admins can also add users that own no node.

A user can log in with callsign + password to configure their own alerts
(user_alerts.py: e-mail / ntfy destination, which services alert them, the
infrastructure team settings). `is_admin` users have the same powers as the
admin password (auth.py); admins grant and remove admin power.

Contact details (e-mail, ntfy topic) are returned only to the user and to
admins, never in public reads, CouchDB or logs. Passwords are salted hashes.
"""

import json
import re
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
CALLSIGN_RE = re.compile(r'^[a-z]{1,2}\d{1,2}[a-z]{1,4}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TOPIC_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
MIN_PASSWORD = 8
SEVERITIES = ('info', 'warning', 'critical')
# Infrastructure-team alert groups (user_alerts.INFRA_GROUPS); defaults for a new member.
DEFAULT_INFRA_GROUPS = ['nodes', 'links', 'services']

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                callsign TEXT PRIMARY KEY,
                password_hash TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                email TEXT,
                ntfy_topic TEXT,
                alerts_enabled INTEGER NOT NULL DEFAULT 1,
                infra_team INTEGER NOT NULL DEFAULT 0,
                infra_groups TEXT,
                infra_min_severity TEXT NOT NULL DEFAULT 'warning',
                auto INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME,
                updated_at DATETIME,
                last_login DATETIME
            )
        ''')
    _tables_ready = True


def _now():
    return datetime.now().strftime(TS_FMT)


def callsign_of(node_name):
    """The owner's callsign from a node name, or None."""
    head = (node_name or '').split('-')[0].strip().lower()
    return head if CALLSIGN_RE.match(head) else None


def normalize(callsign):
    cs = (callsign or '').strip().lower()
    return cs if CALLSIGN_RE.match(cs) else None


def sync_from_nodes():
    """Create a user (no password) for every node callsign not yet known. Returns how many were added."""
    ensure_tables()
    with database.get_connection() as conn:
        have = {r[0] for r in conn.execute('SELECT callsign FROM users')}
        found = {callsign_of(r[0]) for r in conn.execute('SELECT name FROM nodes')} - {None}
        new = sorted(found - have)
        now = _now()
        conn.executemany('INSERT OR IGNORE INTO users (callsign, auto, created_at, updated_at) VALUES (?, 1, ?, ?)',
                         [(cs, now, now) for cs in new])
    return len(new)


def _public(r, nodes=None, private=False):
    u = {'callsign': r['callsign'], 'has_password': bool(r['password_hash']), 'is_admin': bool(r['is_admin']),
         'infra_team': bool(r['infra_team']), 'auto': bool(r['auto'])}
    if nodes is not None:
        u['nodes'] = nodes.get(r['callsign'], [])
    if private:
        try:
            groups = json.loads(r['infra_groups']) if r['infra_groups'] else list(DEFAULT_INFRA_GROUPS)
        except ValueError:
            groups = list(DEFAULT_INFRA_GROUPS)
        u.update({'email': r['email'] or '', 'ntfy_topic': r['ntfy_topic'] or '',
                  'alerts_enabled': bool(r['alerts_enabled']), 'infra_groups': groups,
                  'infra_min_severity': r['infra_min_severity'] or 'warning',
                  'created_at': r['created_at'], 'last_login': r['last_login']})
    return u


def _nodes_by_callsign(conn):
    out = {}
    for r in conn.execute('SELECT name FROM nodes ORDER BY name'):
        cs = callsign_of(r[0])
        if cs:
            out.setdefault(cs, []).append(r[0])
    return out


def get(callsign, private=False):
    ensure_tables()
    cs = (callsign or '').strip().lower()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM users WHERE callsign = ?', (cs,)).fetchone()
        if not r:
            return None
        return _public(r, _nodes_by_callsign(conn), private)


def list_users(private=False):
    ensure_tables()
    with database.get_connection() as conn:
        nodes = _nodes_by_callsign(conn)
        return [_public(r, nodes, private) for r in conn.execute('SELECT * FROM users ORDER BY callsign')]


def raw(callsign):
    """Full row (with contacts, no hash) for the alert router."""
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM users WHERE callsign = ?', ((callsign or '').lower(),)).fetchone()
    return _public(r, private=True) if r else None


def create(callsign):
    cs = normalize(callsign)
    if not cs:
        return None, 'Not a callsign (e.g. w0abc)'
    ensure_tables()
    with database.get_connection() as conn:
        if conn.execute('SELECT 1 FROM users WHERE callsign = ?', (cs,)).fetchone():
            return None, f'{cs.upper()} already exists'
        now = _now()
        conn.execute('INSERT INTO users (callsign, auto, created_at, updated_at) VALUES (?, 0, ?, ?)', (cs, now, now))
    return get(cs, private=True), None


def delete(callsign):
    """Remove a user that owns no node (node owners come back on the next sync)."""
    ensure_tables()
    cs = (callsign or '').lower()
    with database.get_connection() as conn:
        if _nodes_by_callsign(conn).get(cs):
            return False, f'{cs.upper()} owns nodes; remove their password instead'
        n = conn.execute('DELETE FROM users WHERE callsign = ?', (cs,)).rowcount
    return bool(n), None if n else 'No such user'


def check_password(callsign, password):
    """(ok, error). A user without a password can't log in."""
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT password_hash FROM users WHERE callsign = ?', ((callsign or '').lower(),)).fetchone()
    if not r:
        return False, 'Unknown callsign or wrong password.'
    if not r[0]:
        return False, 'No password is set for this callsign yet; ask an admin to set one.'
    if not check_password_hash(r[0], str(password or '')):
        return False, 'Unknown callsign or wrong password.'
    with database.get_connection() as conn:
        conn.execute('UPDATE users SET last_login = ? WHERE callsign = ?', (_now(), (callsign or '').lower()))
    return True, None


def set_password(callsign, new):
    """Admin: set (or with new='' remove) a user's password."""
    new = str(new or '')
    if new and len(new) < MIN_PASSWORD:
        return False, f'Use at least {MIN_PASSWORD} characters.'
    ensure_tables()
    with database.get_connection() as conn:
        n = conn.execute('UPDATE users SET password_hash = ?, updated_at = ? WHERE callsign = ?',
                         (generate_password_hash(new) if new else None, _now(), (callsign or '').lower())).rowcount
    return (True, None) if n else (False, 'No such user')


def change_password(callsign, current, new):
    """The user changes their own password (needs the current one)."""
    ok, _ = check_password(callsign, current)
    if not ok:
        return False, 'The current password is wrong.'
    if len(str(new or '')) < MIN_PASSWORD:
        return False, f'Use at least {MIN_PASSWORD} characters.'
    return set_password(callsign, new)


def update(callsign, data, as_admin=False):
    """Edit settings. Users change their contacts and alert choices; only an
    admin changes is_admin / infra_team. Returns (user, error)."""
    ensure_tables()
    cs = (callsign or '').lower()
    data = data or {}
    sets, vals = [], []
    if 'email' in data:
        email = str(data['email'] or '').strip()
        if email and not EMAIL_RE.match(email):
            return None, 'That e-mail address does not look right.'
        sets.append('email = ?'); vals.append(email or None)
    if 'ntfy_topic' in data:
        topic = str(data['ntfy_topic'] or '').strip()
        if topic and not TOPIC_RE.match(topic):
            return None, 'An ntfy topic is letters, digits, - and _ (up to 64).'
        sets.append('ntfy_topic = ?'); vals.append(topic or None)
    if 'alerts_enabled' in data:
        sets.append('alerts_enabled = ?'); vals.append(1 if data['alerts_enabled'] else 0)
    if 'infra_groups' in data:
        import user_alerts
        groups = [g for g in (data['infra_groups'] or []) if g in user_alerts.INFRA_GROUPS]
        sets.append('infra_groups = ?'); vals.append(json.dumps(groups))
    if 'infra_min_severity' in data:
        if data['infra_min_severity'] not in SEVERITIES:
            return None, 'Unknown severity'
        sets.append('infra_min_severity = ?'); vals.append(data['infra_min_severity'])
    for key in ('is_admin', 'infra_team'):
        if key in data:
            if not as_admin:
                return None, 'Only an admin can change that.'
            sets.append(f'{key} = ?'); vals.append(1 if data[key] else 0)
    if not sets:
        return get(cs, private=True), None
    sets.append('updated_at = ?'); vals.append(_now())
    with database.get_connection() as conn:
        n = conn.execute(f'UPDATE users SET {", ".join(sets)} WHERE callsign = ?', vals + [cs]).rowcount
    return (get(cs, private=True), None) if n else (None, 'No such user')


def is_admin(callsign):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT is_admin FROM users WHERE callsign = ?', ((callsign or '').lower(),)).fetchone()
    return bool(r and r[0])

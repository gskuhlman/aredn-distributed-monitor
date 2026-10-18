"""
Operator notifications (email, Discord, ntfy, generic webhook).

Events logged through database.log_event are handed to an event listener
installed by install(). The listener only enqueues (never blocks or raises);
a background worker matches events against rules and applies the anti-spam
pipeline before anything is sent:

    rules -> mute / quiet hours / is_suppressed() -> cooldown -> flap hold
          -> digest batch per channel -> send -> notifications_log

- Rules: event types + minimum severity + node scope (any, infrastructure,
  selected, or a node list) + channels.
- Cooldown: per (rule, subject, event type), where the subject is the node or
  the (sorted) link pair, so both directions of one link count once.
- Flap suppression: a "problem" event (node_offline, link_dropped, ...) is
  held for flap_minutes; if its recovery arrives in that time both are
  replaced by a single "flapped" note. Recoveries are only sent for problems
  that were actually alerted, so a restart never floods "back online" alerts.
- Digest: alerts ready to send are batched for digest_seconds per channel.

Secrets (SMTP password, Discord webhook URL, ntfy/webhook tokens) are stored
only in the local SQLite table notification_config or in env vars, are never
logged, and are masked in API responses. Nothing here touches CouchDB.
"""

import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage

import requests

import config
import database

logger = logging.getLogger(__name__)

# Event types added by the link-watch detectors (#7/#8); defined here so rules
# can name them before that module exists.
EVENT_NOISE_JUMP = 'noise_jump'
EVENT_NOISE_CLEARED = 'noise_cleared'
EVENT_ONE_WAY_LINK = 'one_way_link'
EVENT_ONE_WAY_LINK_CLEARED = 'one_way_link_cleared'

EVENT_TYPES = [
    'gateway_lost', 'gateway_restored',
    'node_offline', 'node_online', 'node_degraded', 'node_reboot', 'node_discovered',
    'link_dropped', 'link_restored', 'link_new', 'link_removed',
    'link_blocked', 'link_unblocked',
    'frequency_change', 'radio_config_change',
    EVENT_ONE_WAY_LINK, EVENT_ONE_WAY_LINK_CLEARED,
    EVENT_NOISE_JUMP, EVENT_NOISE_CLEARED,
]

# problem event -> its recovery event (flap pairing and "recovery only if alerted")
RECOVERY_OF = {
    'node_offline': 'node_online',
    'link_dropped': 'link_restored',
    'gateway_lost': 'gateway_restored',
    'link_blocked': 'link_unblocked',
    EVENT_ONE_WAY_LINK: EVENT_ONE_WAY_LINK_CLEARED,
    EVENT_NOISE_JUMP: EVENT_NOISE_CLEARED,
}
PROBLEM_OF = {v: k for k, v in RECOVERY_OF.items()}

SEVERITY_RANK = {'debug': 0, 'info': 1, 'warning': 2, 'error': 3, 'critical': 4}
SCOPES = ['any', 'infrastructure', 'selected', 'nodes']
CHANNELS = ['email', 'discord', 'ntfy', 'webhook']

MASK = '********'
# channel -> {field: env var}. An env var, when set, overrides the stored value.
SECRET_FIELDS = {
    'email': {'password': 'NOTIFY_SMTP_PASSWORD'},
    'discord': {'webhook_url': 'NOTIFY_DISCORD_WEBHOOK'},
    'ntfy': {'token': 'NOTIFY_NTFY_TOKEN'},
    'webhook': {'token': 'NOTIFY_WEBHOOK_TOKEN'},
}

DEFAULT_CONFIG = {
    'enabled': False,
    'digest_seconds': 60,
    'cooldown_minutes': 30,
    'flap_minutes': 5,
    'mute_until': None,
    'quiet_hours': {'enabled': False, 'start': '22:00', 'end': '07:00', 'allow_critical': True},
    'channels': {
        'email': {'enabled': False, 'host': '', 'port': 587, 'tls': 'starttls',
                  'user': '', 'password': '', 'from': '', 'to': ''},
        'discord': {'enabled': False, 'webhook_url': ''},
        'ntfy': {'enabled': False, 'server': 'https://ntfy.sh', 'topic': '', 'token': '',
                 'priority': 'default'},
        'webhook': {'enabled': False, 'url': '', 'token': ''},
    },
}

DEFAULT_RULES = [
    {'name': 'Starting node lost / restored', 'event_types': ['gateway_lost', 'gateway_restored'],
     'min_severity': 'info', 'scope': 'any'},
    {'name': 'Infrastructure node offline / online', 'event_types': ['node_offline', 'node_online'],
     'min_severity': 'info', 'scope': 'infrastructure'},
    {'name': 'Infrastructure link dropped', 'event_types': ['link_dropped', 'link_restored'],
     'min_severity': 'info', 'scope': 'infrastructure'},
    {'name': 'One-way link', 'event_types': [EVENT_ONE_WAY_LINK, EVENT_ONE_WAY_LINK_CLEARED],
     'min_severity': 'info', 'scope': 'any'},
    {'name': 'Noise floor jump', 'event_types': [EVENT_NOISE_JUMP, EVENT_NOISE_CLEARED],
     'min_severity': 'info', 'scope': 'any'},
]

LINK_DETAIL_RE = re.compile(r'^(?P<type>[\w-]+) link to (?P<peer>\S+)')
SEND_TIMEOUT = 15
TS_FMT = '%Y-%m-%d %H:%M:%S'


# ------------------------------------------------------------------ schema --

_schema_ready = False


def ensure_schema():
    """Create the notification tables and seed default rules (idempotent)."""
    global _schema_ready
    if _schema_ready:
        return
    with database.get_connection() as conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS notification_config (
            key TEXT PRIMARY KEY, value TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS notification_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            event_types TEXT NOT NULL,
            min_severity TEXT NOT NULL DEFAULT 'info',
            scope TEXT NOT NULL DEFAULT 'any',
            nodes TEXT,
            channels TEXT,
            created_at TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS notifications_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            rule TEXT,
            channel TEXT,
            event_ids TEXT,
            title TEXT,
            status TEXT NOT NULL,
            error TEXT)''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_notifications_log_ts ON notifications_log(timestamp)')
        seeded = cur.execute("SELECT value FROM notification_config WHERE key = 'rules_seeded'").fetchone()
        if not seeded:
            now = _now_str()
            for rule in DEFAULT_RULES:
                cur.execute('''INSERT INTO notification_rules
                    (name, enabled, event_types, min_severity, scope, nodes, channels, created_at)
                    VALUES (?, 1, ?, ?, ?, '[]', '[]', ?)''',
                            (rule['name'], json.dumps(rule['event_types']), rule['min_severity'],
                             rule['scope'], now))
            cur.execute("INSERT OR REPLACE INTO notification_config (key, value) VALUES ('rules_seeded', '1')")
    _schema_ready = True


def _now_str():
    return datetime.now().strftime(TS_FMT)


# ------------------------------------------------------------------ config --

_config_cache = {'value': None, 'at': 0.0}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def get_config(raw=False):
    """Stored config merged over defaults. raw=True includes secrets (and env
    overrides) for sending; otherwise use masked_config() for display."""
    ensure_schema()
    now = time.time()
    if _config_cache['value'] is None or now - _config_cache['at'] > 5:
        with database.get_connection() as conn:
            row = conn.execute("SELECT value FROM notification_config WHERE key = 'config'").fetchone()
        stored = {}
        if row and row['value']:
            try:
                stored = json.loads(row['value'])
            except ValueError:
                stored = {}
        _config_cache['value'] = _merge(DEFAULT_CONFIG, stored)
        _config_cache['at'] = now
    cfg = json.loads(json.dumps(_config_cache['value']))
    if raw:
        for channel, fields in SECRET_FIELDS.items():
            for field, env in fields.items():
                if os.environ.get(env):
                    cfg['channels'][channel][field] = os.environ[env]
    return cfg


def _save_config(cfg):
    with database.get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO notification_config (key, value) VALUES ('config', ?)",
                     (json.dumps(cfg),))
    _config_cache['value'] = None


def masked_config():
    """Config safe to return from the API: secrets replaced by MASK (or '')."""
    cfg = get_config()
    for channel, fields in SECRET_FIELDS.items():
        ch = cfg['channels'][channel]
        for field, env in fields.items():
            from_env = bool(os.environ.get(env))
            ch[f'{field}_set'] = bool(ch.get(field)) or from_env
            ch[f'{field}_from_env'] = from_env
            ch[field] = MASK if ch[f'{field}_set'] else ''
    return cfg


def update_config(data):
    """Merge a (partial) config update. A secret equal to MASK (or omitted)
    keeps the stored value; an empty string clears it."""
    current = get_config()
    data = data or {}
    out = json.loads(json.dumps(current))
    for key in ('enabled',):
        if key in data:
            out[key] = bool(data[key])
    for key in ('digest_seconds', 'cooldown_minutes', 'flap_minutes'):
        if key in data:
            try:
                out[key] = max(0, int(data[key]))
            except (TypeError, ValueError):
                raise ValueError(f'{key} must be a whole number')
    if 'mute_until' in data:
        out['mute_until'] = _parse_mute(data['mute_until'])
    if isinstance(data.get('quiet_hours'), dict):
        qh = data['quiet_hours']
        for key in ('start', 'end'):
            if key in qh:
                if not re.match(r'^\d{1,2}:\d{2}$', str(qh[key])):
                    raise ValueError(f'quiet_hours.{key} must be HH:MM')
                out['quiet_hours'][key] = str(qh[key])
        for key in ('enabled', 'allow_critical'):
            if key in qh:
                out['quiet_hours'][key] = bool(qh[key])
    for channel, values in (data.get('channels') or {}).items():
        if channel not in CHANNELS or not isinstance(values, dict):
            continue
        ch = out['channels'][channel]
        secrets = SECRET_FIELDS.get(channel, {})
        for field, value in values.items():
            if field not in DEFAULT_CONFIG['channels'][channel]:
                continue
            if field in secrets:
                if value is None or value == MASK:
                    continue
                ch[field] = str(value).strip()
            elif field == 'enabled':
                ch[field] = bool(value)
            elif field == 'port':
                try:
                    ch[field] = int(value)
                except (TypeError, ValueError):
                    raise ValueError('email port must be a number')
            else:
                ch[field] = str(value).strip()
    _save_config(out)
    return masked_config()


def _parse_mute(value):
    if not value:
        return None
    if isinstance(value, (int, float)):  # minutes from now
        return (datetime.now() + timedelta(minutes=float(value))).strftime(TS_FMT)
    text = str(value).strip().replace('T', ' ')
    for fmt in (TS_FMT, '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(text, fmt).strftime(TS_FMT)
        except ValueError:
            pass
    raise ValueError('mute_until must be minutes or YYYY-MM-DD HH:MM')


# ------------------------------------------------------------------- rules --

def _rule_from_row(row):
    r = dict(row)
    for key in ('event_types', 'nodes', 'channels'):
        try:
            r[key] = json.loads(r.get(key) or '[]')
        except ValueError:
            r[key] = []
    r['enabled'] = bool(r['enabled'])
    return r


def get_rules():
    ensure_schema()
    with database.get_connection() as conn:
        rows = conn.execute('SELECT * FROM notification_rules ORDER BY id').fetchall()
    return [_rule_from_row(r) for r in rows]


def save_rule(data):
    """Create (no id) or update (id) a rule. Returns the saved rule."""
    ensure_schema()
    data = data or {}
    name = str(data.get('name') or '').strip()
    types = [t for t in (data.get('event_types') or []) if isinstance(t, str) and t.strip()]
    if not name:
        raise ValueError('Rule name is required')
    if not types:
        raise ValueError('Pick at least one event type')
    severity = data.get('min_severity') or 'info'
    if severity not in SEVERITY_RANK:
        raise ValueError('Unknown severity')
    scope = data.get('scope') or 'any'
    if scope not in SCOPES:
        raise ValueError('Unknown scope')
    nodes = [str(n).strip().lower() for n in (data.get('nodes') or []) if str(n).strip()]
    if scope == 'nodes' and not nodes:
        raise ValueError('List at least one node for a node-list rule')
    channels = [c for c in (data.get('channels') or []) if c in CHANNELS]
    values = (name, 1 if data.get('enabled', True) else 0, json.dumps(types), severity, scope,
              json.dumps(nodes), json.dumps(channels))
    with database.get_connection() as conn:
        cur = conn.cursor()
        if data.get('id'):
            cur.execute('''UPDATE notification_rules SET name=?, enabled=?, event_types=?,
                min_severity=?, scope=?, nodes=?, channels=? WHERE id=?''', values + (int(data['id']),))
            if cur.rowcount == 0:
                raise LookupError('Rule not found')
            rule_id = int(data['id'])
        else:
            cur.execute('''INSERT INTO notification_rules
                (name, enabled, event_types, min_severity, scope, nodes, channels, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', values + (_now_str(),))
            rule_id = cur.lastrowid
        row = cur.execute('SELECT * FROM notification_rules WHERE id = ?', (rule_id,)).fetchone()
    return _rule_from_row(row)


def delete_rule(rule_id):
    ensure_schema()
    with database.get_connection() as conn:
        cur = conn.execute('DELETE FROM notification_rules WHERE id = ?', (int(rule_id),))
        return cur.rowcount > 0


# ------------------------------------------------------------- event model --

def event_subject(event):
    """(subject key, link peer or None). Link events key on the sorted pair so
    both directions of one link share a cooldown/flap slot."""
    node = (event.get('node_name') or '').lower()
    peer = None
    m = LINK_DETAIL_RE.match(event.get('details') or '')
    if m and event.get('event_type', '').startswith(('link_', 'one_way', 'noise')):
        peer = m.group('peer').lower()
    if peer and node:
        return '|'.join(sorted([node, peer])), peer
    return node or '(network)', peer


def link_type_of(event):
    m = LINK_DETAIL_RE.match(event.get('details') or '')
    return m.group('type') if m else None


_scope_cache = {'at': 0.0, 'infrastructure': set(), 'selected': set()}


def _scope_names(scope):
    now = time.time()
    if now - _scope_cache['at'] > 60:
        try:
            _scope_cache['infrastructure'] = set(database.get_infrastructure_node_names())
        except Exception:
            _scope_cache['infrastructure'] = set()
        try:
            _scope_cache['selected'] = set(database.get_selected_node_names())
        except Exception:
            _scope_cache['selected'] = set()
        _scope_cache['at'] = now
    return _scope_cache.get(scope, set())


def rule_matches(rule, event):
    if not rule.get('enabled'):
        return False
    if event.get('event_type') not in rule.get('event_types', []):
        return False
    if SEVERITY_RANK.get(event.get('severity') or 'info', 1) < SEVERITY_RANK.get(rule.get('min_severity'), 1):
        # Recoveries are logged as info; let them through when the rule also
        # lists the matching problem type, so "lost" and "restored" pair up.
        problem = PROBLEM_OF.get(event.get('event_type'))
        if not (problem and problem in rule.get('event_types', [])):
            return False
    scope = rule.get('scope') or 'any'
    if scope == 'any':
        return True
    node = (event.get('node_name') or '').lower()
    _, peer = event_subject(event)
    names = set(rule.get('nodes') or []) if scope == 'nodes' else _scope_names(scope)
    return bool(names) and (node in names or (peer is not None and peer in names))


def matching_rules(event, rules=None):
    return [r for r in (rules if rules is not None else get_rules()) if rule_matches(r, event)]


def describe(event):
    """(title, line) for one event. Adds the derived 'sector lost a client'."""
    etype = event.get('event_type') or 'event'
    node = event.get('node_name') or ''
    details = event.get('details') or ''
    title = f"{etype.replace('_', ' ')}: {node}" if node else etype.replace('_', ' ')
    if etype == 'link_dropped' and (link_type_of(event) or '').upper() == 'RF':
        infra = _scope_names('infrastructure')
        _, peer = event_subject(event)
        if node in infra and peer and peer not in infra:
            title = f"Sector lost a client: {node} lost {peer}"
        elif peer in infra and node not in infra:
            title = f"Sector lost a client: {peer} lost {node}"
    when = (event.get('timestamp') or _now_str())[11:16]
    sev = event.get('severity') or 'info'
    return title, f"[{sev}] {when} {title}" + (f" - {details}" if details else '')


def is_suppressed(event):
    """Hook for maintenance windows (#22). Return a reason string to suppress
    the event, or None. Nothing is suppressed yet."""
    return None


def _in_quiet_hours(cfg, now=None):
    qh = cfg.get('quiet_hours') or {}
    if not qh.get('enabled'):
        return False
    now = now or datetime.now()
    try:
        sh, sm = (int(x) for x in qh['start'].split(':'))
        eh, em = (int(x) for x in qh['end'].split(':'))
    except (KeyError, ValueError):
        return False
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def _muted(cfg, now=None):
    until = cfg.get('mute_until')
    if not until:
        return False
    try:
        return (now or datetime.now()) < datetime.strptime(until, TS_FMT)
    except ValueError:
        return False


# ----------------------------------------------------------------- senders --

class SendError(Exception):
    pass


def _scrub(text, ch):
    """Remove secrets from an error message before it is logged or stored."""
    text = str(text)
    for field in ('password', 'webhook_url', 'token'):
        secret = ch.get(field)
        if secret and len(secret) > 3:
            text = text.replace(secret, '***')
    return re.sub(r'(https?://[^\s/]*discord(?:app)?\.com/api/webhooks/)\S+', r'\1***', text)


def _send_email(ch, title, body):
    if not ch.get('host') or not ch.get('to') or not (ch.get('from') or ch.get('user')):
        raise SendError('email needs host, from and to')
    msg = EmailMessage()
    msg['Subject'] = title
    msg['From'] = ch.get('from') or ch.get('user')
    msg['To'] = ch['to']
    msg.set_content(body)
    port = int(ch.get('port') or 587)
    tls = ch.get('tls') or 'starttls'
    if tls == 'ssl':
        server = smtplib.SMTP_SSL(ch['host'], port, timeout=SEND_TIMEOUT,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(ch['host'], port, timeout=SEND_TIMEOUT)
    try:
        if tls == 'starttls':
            server.starttls(context=ssl.create_default_context())
        if ch.get('user') and ch.get('password'):
            server.login(ch['user'], ch['password'])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _check(resp):
    if resp.status_code >= 300:
        raise SendError(f'HTTP {resp.status_code}: {resp.text[:200]}')


def _send_discord(ch, title, body):
    if not ch.get('webhook_url'):
        raise SendError('discord needs a webhook URL')
    content = f"**{title}**\n{body}"
    if len(content) > 1990:
        content = content[:1980] + '\n...'
    _check(requests.post(ch['webhook_url'], json={'username': 'AREDN Monitor', 'content': content},
                         timeout=SEND_TIMEOUT))


def _send_ntfy(ch, title, body, critical=False):
    if not ch.get('topic'):
        raise SendError('ntfy needs a topic')
    url = f"{(ch.get('server') or 'https://ntfy.sh').rstrip('/')}/{ch['topic'].strip('/')}"
    headers = {'Title': title.encode('ascii', 'replace').decode('ascii'),
               'Priority': 'urgent' if critical else (ch.get('priority') or 'default'),
               'Tags': 'satellite'}
    if ch.get('token'):
        headers['Authorization'] = f"Bearer {ch['token']}"
    _check(requests.post(url, data=body.encode('utf-8'), headers=headers, timeout=SEND_TIMEOUT))


def _send_webhook(ch, title, body, alerts=None):
    if not ch.get('url'):
        raise SendError('webhook needs a URL')
    headers = {'Authorization': f"Bearer {ch['token']}"} if ch.get('token') else {}
    _check(requests.post(ch['url'], json={'source': 'aredn-monitor', 'title': title, 'text': body,
                                          'alerts': alerts or []},
                         headers=headers, timeout=SEND_TIMEOUT))


def send(channel, title, body, cfg=None, alerts=None, critical=False):
    """Send one message on one channel. Raises SendError with a scrubbed message."""
    cfg = cfg or get_config(raw=True)
    ch = cfg['channels'].get(channel)
    if ch is None:
        raise SendError(f'unknown channel {channel}')
    try:
        if channel == 'email':
            _send_email(ch, title, body)
        elif channel == 'discord':
            _send_discord(ch, title, body)
        elif channel == 'ntfy':
            _send_ntfy(ch, title, body, critical)
        elif channel == 'webhook':
            _send_webhook(ch, title, body, alerts)
    except SendError as exc:
        raise SendError(_scrub(exc, ch))
    except Exception as exc:
        raise SendError(_scrub(f'{type(exc).__name__}: {exc}', ch))


def send_test(channel):
    ensure_schema()
    title = 'AREDN Monitor test notification'
    body = f'Test message sent {_now_str()}. If you can read this, the {channel} channel works.'
    try:
        send(channel, title, body)
        log_notification('test', channel, [], title, 'sent')
        return {'success': True}
    except SendError as exc:
        log_notification('test', channel, [], title, 'failed', str(exc))
        return {'success': False, 'error': str(exc)}


# --------------------------------------------------------------------- log --

def log_notification(rule, channel, event_ids, title, status, error=None):
    try:
        with database.get_connection() as conn:
            conn.execute('''INSERT INTO notifications_log
                (timestamp, rule, channel, event_ids, title, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (_now_str(), rule, channel, ','.join(str(i) for i in event_ids if i),
                          title, status, error))
    except Exception as exc:  # never let logging a notification break anything
        logger.warning('Could not record notification: %s', type(exc).__name__)


def get_log(limit=100):
    ensure_schema()
    with database.get_connection() as conn:
        rows = conn.execute('SELECT * FROM notifications_log ORDER BY id DESC LIMIT ?',
                            (max(1, min(int(limit), 1000)),)).fetchall()
    return [dict(r) for r in rows]


def cleanup_log(days=None):
    days = days if days is not None else config.CONNECTIVITY_LOG_RETENTION_DAYS
    cutoff = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM notifications_log WHERE timestamp < ?', (cutoff,)).rowcount


# -------------------------------------------------------------- dispatcher --

class Dispatcher:
    """Anti-spam pipeline. Pure state machine driven by process()/tick() so it
    can be tested without threads; the worker calls it from a loop."""

    def __init__(self, sender=None, clock=None):
        self.sender = sender or send
        self.clock = clock or time.time
        self.last_sent = {}      # (rule_id, subject, type) -> ts
        self.held = {}           # (rule_id, subject, problem type) -> {'event','rule','at'}
        self.alerted = set()     # (rule_id, subject, problem type) problems actually alerted
        self.batches = {}        # channel -> {'first': ts, 'items': [alert]}

    # An alert is {'rule': rule, 'event': event, 'title', 'line', 'kind'}

    def process(self, event, cfg=None, rules=None):
        cfg = cfg or get_config(raw=True)
        if not cfg.get('enabled'):
            return []
        decisions = []
        for rule in matching_rules(event, rules):
            decisions.append(self._handle(rule, event, cfg))
        return decisions

    def _handle(self, rule, event, cfg):
        etype = event['event_type']
        subject, _ = event_subject(event)
        title, line = describe(event)
        now = self.clock()
        rid = rule['id']

        def suppressed(reason):
            log_notification(rule['name'], None, [event.get('id')], title, 'suppressed', reason)
            return ('suppressed', reason)

        reason = is_suppressed(event)
        if reason:
            return suppressed(reason)
        if _muted(cfg):
            return suppressed('muted')
        critical = SEVERITY_RANK.get(event.get('severity'), 1) >= SEVERITY_RANK['critical']
        if _in_quiet_hours(cfg) and not (critical and cfg['quiet_hours'].get('allow_critical')):
            return suppressed('quiet hours')

        problem = PROBLEM_OF.get(etype)
        if problem:  # a recovery
            key = (rid, subject, problem)
            if key in self.held:
                held = self.held.pop(key)
                minutes = max(1, round((now - held['at']) / 60))
                ftitle = f"Flapped: {held['title'].split(': ', 1)[-1]}"
                fline = (f"[{event.get('severity') or 'info'}] {(event.get('timestamp') or _now_str())[11:16]} "
                         f"{ftitle} - {problem.replace('_', ' ')} then {etype.replace('_', ' ')} "
                         f"within {minutes} min")
                return self._enqueue(rule, [held['event'], event], ftitle, fline, cfg, 'flap',
                                     cooldown_key=(rid, subject, 'flap'))
            if key in self.alerted:
                self.alerted.discard(key)
                return self._enqueue(rule, [event], title, line, cfg, 'recovery')
            return ('dropped', 'recovery for a problem that was not alerted')

        key = (rid, subject, etype)
        cooldown = cfg.get('cooldown_minutes', 30) * 60
        if cooldown and now - self.last_sent.get(key, -1e18) < cooldown:
            return suppressed('cooldown')
        if etype in RECOVERY_OF and cfg.get('flap_minutes', 0) > 0:
            if key not in self.held:
                self.held[key] = {'event': event, 'rule': rule, 'at': now, 'title': title, 'line': line}
            return ('held', 'flap window')
        return self._enqueue(rule, [event], title, line, cfg, 'alert', cooldown_key=key)

    def _enqueue(self, rule, events, title, line, cfg, kind, cooldown_key=None):
        now = self.clock()
        if cooldown_key is not None:
            cooldown = cfg.get('cooldown_minutes', 30) * 60
            if kind == 'flap' and cooldown and now - self.last_sent.get(cooldown_key, -1e18) < cooldown:
                log_notification(rule['name'], None, [e.get('id') for e in events], title,
                                 'suppressed', 'cooldown')
                return ('suppressed', 'cooldown')
            self.last_sent[cooldown_key] = now
        if kind == 'alert' and events[0]['event_type'] in RECOVERY_OF:
            subject, _ = event_subject(events[0])
            self.alerted.add((rule['id'], subject, events[0]['event_type']))
        channels = [c for c in (rule.get('channels') or CHANNELS)
                    if cfg['channels'].get(c, {}).get('enabled')]
        if not channels:
            log_notification(rule['name'], None, [e.get('id') for e in events], title,
                             'suppressed', 'no enabled channel')
            return ('suppressed', 'no enabled channel')
        alert = {'rule': rule['name'], 'title': title, 'line': line, 'kind': kind,
                 'event_ids': [e.get('id') for e in events],
                 'critical': any(SEVERITY_RANK.get(e.get('severity'), 1) >= SEVERITY_RANK['critical']
                                 for e in events),
                 'events': [{k: e.get(k) for k in ('id', 'timestamp', 'event_type', 'node_name',
                                                    'details', 'severity')} for e in events]}
        for channel in channels:
            batch = self.batches.setdefault(channel, {'first': now, 'items': []})
            batch['items'].append(alert)
        if not cfg.get('digest_seconds'):
            self.flush(cfg, force=True)
        return ('queued', kind)

    def tick(self, cfg=None):
        """Release expired flap holds and flush due digests."""
        cfg = cfg or get_config(raw=True)
        now = self.clock()
        flap = cfg.get('flap_minutes', 0) * 60
        for key, held in list(self.held.items()):
            if now - held['at'] >= flap:
                self.held.pop(key)
                self._enqueue(held['rule'], [held['event']], held['title'], held['line'], cfg, 'alert',
                              cooldown_key=key)
        self.flush(cfg)

    def flush(self, cfg=None, force=False):
        cfg = cfg or get_config(raw=True)
        now = self.clock()
        digest = cfg.get('digest_seconds', 60)
        sent = 0
        for channel, batch in list(self.batches.items()):
            if not batch['items'] or (not force and now - batch['first'] < digest):
                continue
            self.batches.pop(channel)
            items = batch['items']
            if len(items) == 1:
                title = f"AREDN: {items[0]['title']}"
            else:
                title = f"AREDN: {len(items)} alerts"
            body = '\n'.join(a['line'] for a in items)
            event_ids = [i for a in items for i in a['event_ids']]
            rules = ', '.join(sorted({a['rule'] for a in items}))
            try:
                self.sender(channel, title, body, cfg=cfg, alerts=[
                    {k: a[k] for k in ('rule', 'title', 'kind', 'events')} for a in items],
                    critical=any(a['critical'] for a in items))
                log_notification(rules, channel, event_ids, title, 'sent')
                sent += 1
            except Exception as exc:
                ch = cfg['channels'].get(channel, {})
                err = _scrub(exc, ch)
                logger.warning('Notification via %s failed: %s', channel, err)
                log_notification(rules, channel, event_ids, title, 'failed', err)
        return sent


# ------------------------------------------------------------------ worker --

_queue = queue.Queue(maxsize=5000)
_dispatcher = Dispatcher()
_worker = {'running': False, 'started_at': None, 'last_cleanup': 0.0, 'dropped': 0}


def _listener(event):
    """database.log_event listener: enqueue only; never block or raise."""
    try:
        if not get_config().get('enabled'):  # cached; re-read at most every 5 s
            return
        _queue.put_nowait(event)
    except queue.Full:
        _worker['dropped'] += 1
    except Exception:
        pass


def _worker_loop():
    _worker['running'] = True
    _worker['started_at'] = _now_str()
    while True:
        try:
            try:
                event = _queue.get(timeout=1.0)
            except queue.Empty:
                event = None
            if event is not None:
                _dispatcher.process(event)
            _dispatcher.tick()
            if time.time() - _worker['last_cleanup'] > 3600:
                _worker['last_cleanup'] = time.time()
                cleanup_log()
        except Exception as exc:
            logger.error('Notification worker error: %s', type(exc).__name__)
            time.sleep(1)


def install(start_task=None):
    """Register the event listener and start the worker. Called once from the
    app's startup path only, so imports/tests never send anything."""
    ensure_schema()
    database.add_event_listener(_listener)
    if _worker['running']:
        return
    _worker['running'] = True
    if start_task:
        start_task(_worker_loop)
    else:
        threading.Thread(target=_worker_loop, name='notify-worker', daemon=True).start()
    logger.info('Notifications installed (enabled=%s)', get_config().get('enabled'))


def status():
    cfg = get_config()
    return {'enabled': cfg.get('enabled'), 'muted': _muted(cfg), 'mute_until': cfg.get('mute_until'),
            'quiet_now': _in_quiet_hours(cfg), 'worker_running': _worker['running'],
            'queued': _queue.qsize(), 'dropped': _worker['dropped'],
            'held': len(_dispatcher.held),
            'pending': sum(len(b['items']) for b in _dispatcher.batches.values())}

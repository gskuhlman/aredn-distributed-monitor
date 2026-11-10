"""
Alert acknowledgement and escalation (TODO #55), used by notify.py.

Every alert notify.py sends (kind `alert`, not recoveries or flap notes) gets
a row in SQLite `alert_acks` with a random token; the message carries an
"Acknowledge:" link (`/ack/<token>`, a small page with one button). Alerts
can also be acknowledged on the Notifications page or the Status page. The
recovery of an alerted problem (node back online, link restored ...)
acknowledges it as `recovered`.

Unacknowledged alerts at or above `escalation.min_severity` (critical by
default) are, when `escalation.enabled`: re-sent every `repeat_minutes` to
the channels they went to, up to `max_repeats` times, and sent once to the
`escalation.channels` after `escalate_minutes`. Quiet hours and mute apply to
repeats like to any alert (critical ones pass quiet hours when allowed).
"""

import json
import secrets
from datetime import datetime, timedelta

import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
SEVERITY_RANK = {'debug': 0, 'info': 1, 'warning': 2, 'error': 3, 'critical': 4}

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS alert_acks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            rule TEXT, title TEXT, line TEXT, severity TEXT,
            subject TEXT, event_type TEXT, event_ids TEXT, channels TEXT,
            sent_at DATETIME NOT NULL, last_sent_at DATETIME, repeats INTEGER DEFAULT 0,
            escalated_at DATETIME, acked_at DATETIME, acked_by TEXT, ack_note TEXT)''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_alert_acks_open ON alert_acks(acked_at, sent_at)')
    _tables_ready = True


def _now(now=None):
    return (now or datetime.now()).strftime(TS_FMT)


def register(alert, channels, now=None):
    """Record a sent alert (once; the alert dict keeps the token). Returns the token."""
    if alert.get('ack_token'):
        return alert['ack_token']
    ensure_tables()
    ev = (alert.get('events') or [{}])[0]
    sev = max((e.get('severity') or 'info' for e in alert.get('events') or [{}]),
              key=lambda s: SEVERITY_RANK.get(s, 1))
    token = secrets.token_urlsafe(16)
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO alert_acks (token, rule, title, line, severity, subject, event_type, event_ids,
                        channels, sent_at, last_sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (token, alert.get('rule'), alert.get('title'), alert.get('line'), sev, alert.get('subject'),
                      ev.get('event_type'), ','.join(str(i) for i in alert.get('event_ids') or [] if i),
                      json.dumps(sorted(channels)), _now(now), _now(now)))
    alert['ack_token'] = token
    return token


def ack_url(token, cfg):
    base = ((cfg.get('escalation') or {}).get('ack_base_url') or '').rstrip('/')
    if not base:
        import socket
        import config
        base = f'http://{socket.gethostname().lower()}:{config.PORT}'
    return f'{base}/ack/{token}'


def _row(r):
    d = dict(r)
    try:
        d['channels'] = json.loads(d.get('channels') or '[]')
    except ValueError:
        d['channels'] = []
    d.pop('token', None)
    d['acked'] = bool(d.get('acked_at'))
    return d


def by_token(token):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM alert_acks WHERE token = ?', (str(token or ''),)).fetchone()
    return _row(r) if r else None


def acknowledge(token=None, ack_id=None, by=None, note=None, now=None):
    """Acknowledge by token (message link) or id (page). Returns the row, or
    None when unknown. Acknowledging twice keeps the first."""
    ensure_tables()
    with database.get_connection() as conn:
        if token is not None:
            r = conn.execute('SELECT * FROM alert_acks WHERE token = ?', (str(token),)).fetchone()
        else:
            r = conn.execute('SELECT * FROM alert_acks WHERE id = ?', (int(ack_id),)).fetchone()
        if not r:
            return None
        if not r['acked_at']:
            conn.execute('UPDATE alert_acks SET acked_at = ?, acked_by = ?, ack_note = ? WHERE id = ?',
                         (_now(now), (by or 'operator')[:60], (note or None) and str(note)[:500], r['id']))
        r = conn.execute('SELECT * FROM alert_acks WHERE id = ?', (r['id'],)).fetchone()
    return _row(r)


def recovered(subject, problem_type, now=None):
    """The problem's recovery arrived: acknowledge its open alerts."""
    ensure_tables()
    with database.get_connection() as conn:
        return conn.execute('''UPDATE alert_acks SET acked_at = ?, acked_by = 'recovered'
                               WHERE acked_at IS NULL AND subject = ? AND event_type = ?''',
                            (_now(now), subject, problem_type)).rowcount


def list_alerts(hours=72, open_only=False, limit=200):
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    sql = 'SELECT * FROM alert_acks WHERE sent_at >= ?' + (' AND acked_at IS NULL' if open_only else '')
    with database.get_connection() as conn:
        rows = [_row(r) for r in conn.execute(sql + ' ORDER BY id DESC LIMIT ?', (since, int(limit)))]
        unacked = conn.execute('SELECT COUNT(*) FROM alert_acks WHERE acked_at IS NULL AND sent_at >= ?',
                               (since,)).fetchone()[0]
    return {'alerts': rows, 'unacked': unacked}


def due(cfg, now=None):
    """[(row, 'repeat' | 'escalate')] for unacknowledged alerts that need it."""
    esc = cfg.get('escalation') or {}
    if not esc.get('enabled'):
        return []
    ensure_tables()
    now = now or datetime.now()
    min_rank = SEVERITY_RANK.get(esc.get('min_severity') or 'critical', 4)
    repeat_s = max(1, int(esc.get('repeat_minutes') or 30)) * 60
    escalate_s = max(1, int(esc.get('escalate_minutes') or 60)) * 60
    max_repeats = int(esc.get('max_repeats') if esc.get('max_repeats') is not None else 3)
    since = (now - timedelta(days=2)).strftime(TS_FMT)
    out = []
    with database.get_connection() as conn:
        for r in conn.execute('SELECT * FROM alert_acks WHERE acked_at IS NULL AND sent_at >= ?', (since,)):
            if SEVERITY_RANK.get(r['severity'], 1) < min_rank:
                continue
            sent = datetime.strptime(r['sent_at'], TS_FMT)
            last = datetime.strptime(r['last_sent_at'] or r['sent_at'], TS_FMT)
            if esc.get('channels') and not r['escalated_at'] and (now - sent).total_seconds() >= escalate_s:
                out.append((_row(r) | {'token': r['token']}, 'escalate'))
            elif (r['repeats'] or 0) < max_repeats and (now - last).total_seconds() >= repeat_s:
                out.append((_row(r) | {'token': r['token']}, 'repeat'))
    return out


def mark(ack_id, action, now=None):
    with database.get_connection() as conn:
        if action == 'escalate':
            conn.execute('UPDATE alert_acks SET escalated_at = ? WHERE id = ?', (_now(now), ack_id))
        else:
            conn.execute('UPDATE alert_acks SET repeats = COALESCE(repeats, 0) + 1, last_sent_at = ? WHERE id = ?',
                         (_now(now), ack_id))


def cleanup(days=31):
    ensure_tables()
    cut = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM alert_acks WHERE sent_at < ?', (cut,)).rowcount

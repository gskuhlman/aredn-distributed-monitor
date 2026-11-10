"""
Central syslog (TODO #85). Two sources, one store:

  udp  a receiver on SYSLOG_HOST:SYSLOG_PORT (514) when SYSLOG_ENABLED: AREDN
       nodes send there with "Remote logging" (Advanced Configuration). Off by
       default: a node sends to one server, and the W0GQ nodes send to
       NM5PB's collector today. RFC 3164 (`<PRI>Nov  9 21:58:22 host prog[pid]:
       msg`), RFC 5424 and bare `<PRI>prog: msg` are parsed; the node is the
       host field, else the sender's address looked up in nodes / mesh hosts.
  ssh  the `logread` snapshots the SSH collector takes on slow runs
       (`Sun Nov  9 21:58:22 2026 daemon.info prog[pid]: msg`), imported
       after each run, so the log page has the infrastructure radios' logs
       without any node change.

SQLite `syslog_messages` (received time, node time, node, sender IP,
facility, severity 0-7, program, pid, message, source; a hash keeps re-imports
from duplicating), kept SYSLOG_RETENTION_DAYS. `query()` filters by node,
max severity, program, chip (WireGuard / Network / Auth / Kernel / Radio),
text and time, and can collapse repeats (same node, program and message with
numbers masked) into one row with a count; `rate()` buckets counts by
severity class for the event-rate chart; `csv_rows()` for export.
"""

import csv
import hashlib
import io
import logging
import re
import socket
import threading
import time
from datetime import datetime, timedelta

import config
import database
import local_time

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
SEVERITIES = ('emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug')
FACILITIES = ('kern', 'user', 'mail', 'daemon', 'auth', 'syslog', 'lpr', 'news', 'uucp', 'cron', 'authpriv',
              'ftp', 'ntp', 'audit', 'alert', 'clock', 'local0', 'local1', 'local2', 'local3', 'local4',
              'local5', 'local6', 'local7')
CHIPS = {
    'wireguard': re.compile(r'wireguard|\bwg[sc]?\w*:|\bwg\d|vtun', re.I),
    'network': re.compile(r'netifd|odhcp|dnsmasq|babel|olsr|firewall|fw4|iptables|nft|mesh|arednlink|lqm|'
                          r'dhcp|interface|link is (up|down)', re.I),
    'auth': re.compile(r'dropbear|sshd|login|auth|password|uhttpd', re.I),
    'kernel': re.compile(r'^kernel$', re.I),
    'radio': re.compile(r'ath1[01]k|ath9k|hostapd|wpa_supplicant|mac80211|phy\d|wlan\d', re.I),
}
CHIP_LABELS = {'wireguard': 'WireGuard', 'network': 'Network', 'auth': 'Auth', 'kernel': 'Kernel', 'radio': 'Radio'}
MONTHS = {m: i for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct',
                                       'Nov', 'Dec'), 1)}
RFC3164 = re.compile(r'^<(\d{1,3})>(?:(\w{3})\s+(\d{1,2})\s+(\d\d:\d\d:\d\d)\s+)?(?:(\S+)\s+)?([^\s:\[]+)(?:\[(\d+)\])?:\s?(.*)$', re.S)
RFC5424 = re.compile(r'^<(\d{1,3})>1\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+(?:-|\[.*?\])\s?(.*)$', re.S)
LOGREAD = re.compile(r'^\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d\d:\d\d:\d\d)\s+(\d{4})\s+(\w+)\.(\w+)\s+([^\s:\[]+)(?:\[(\d+)\])?:\s?(.*)$')
NUMBERS = re.compile(r'\d+')

_tables_ready = False
_state = {'running': False, 'received': 0, 'stored': 0, 'dropped': 0, 'last': None, 'error': None, 'started_at': None}
_buffer = []
_lock = threading.Lock()


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS syslog_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at DATETIME NOT NULL, node_time DATETIME, node_name TEXT, host TEXT, ip TEXT,
            facility TEXT, severity INTEGER, program TEXT, pid INTEGER, message TEXT, source TEXT,
            hash TEXT UNIQUE)''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_syslog_time ON syslog_messages(received_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_syslog_node ON syslog_messages(node_name, received_at DESC)')
    _tables_ready = True


def _hash(*parts):
    return hashlib.sha1('\x1f'.join(str(p) for p in parts).encode('utf-8', 'replace')).hexdigest()


def _node_time(mon, day, hms, year=None, now=None):
    now = now or datetime.now()
    try:
        t = datetime(int(year) if year else now.year, MONTHS[mon], int(day),
                     *(int(x) for x in hms.split(':')))
    except (KeyError, ValueError):
        return None
    if not year and t > now + timedelta(days=2):      # December messages read in January
        t = t.replace(year=t.year - 1)
    return t.strftime(TS_FMT)


def parse(data, ip=None, now=None):
    """One datagram -> message dict (None when it isn't syslog)."""
    now = now or datetime.now()
    text = data.decode('utf-8', 'replace') if isinstance(data, bytes) else str(data)
    text = text.strip('\x00\r\n ')
    m = RFC5424.match(text)
    if m:
        pri, ts, host, app, procid, msg = m.groups()
        node_time = None
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            node_time = (dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt).strftime(TS_FMT)
        except ValueError:
            pass
        pid = int(procid) if procid.isdigit() else None
        host = None if host == '-' else host
        program = None if app == '-' else app
    else:
        m = RFC3164.match(text)
        if not m:
            return None
        pri, mon, day, hms, host, program, pid, msg = m.groups()
        node_time = _node_time(mon, day, hms, now=now) if mon else None
        pid = int(pid) if pid else None
    pri = int(pri)
    fac = FACILITIES[pri >> 3] if (pri >> 3) < len(FACILITIES) else str(pri >> 3)
    return {'received_at': now.strftime(TS_FMT), 'node_time': node_time, 'host': host, 'ip': ip,
            'facility': fac, 'severity': pri & 7, 'program': program, 'pid': pid, 'message': (msg or '').strip(),
            'source': 'udp'}


def parse_logread(text, node, now=None):
    """Messages from `logread` output (the SSH slow-run snapshot)."""
    out = []
    for line in (text or '').splitlines():
        m = LOGREAD.match(line.strip())
        if not m:
            continue
        mon, day, hms, year, fac, level, program, pid, msg = m.groups()
        sev = SEVERITIES.index(level) if level in SEVERITIES else (3 if level == 'error' else 6)
        nt = _node_time(mon, day, hms, year, now)
        out.append({'received_at': nt or (now or datetime.now()).strftime(TS_FMT), 'node_time': nt, 'host': node,
                    'ip': None, 'node_name': node, 'facility': fac, 'severity': sev, 'program': program,
                    'pid': int(pid) if pid else None, 'message': msg.strip(), 'source': 'ssh'})
    return out


def _names(conn):
    names = {}
    for r in conn.execute('SELECT name, ip FROM nodes WHERE ip IS NOT NULL'):
        names[r['ip']] = r['name']
    try:
        for r in conn.execute('SELECT ip, name FROM mesh_hosts'):
            names.setdefault(r['ip'], (r['name'] or '').split('.')[0].lower() or None)
    except Exception:
        pass
    return names


def resolve_node(msg, names):
    host = (msg.get('host') or '').strip().lower()
    if host and not re.match(r'^\d+\.\d+\.\d+\.\d+$', host) and host not in ('localhost', '-'):
        return host.split('.')[0] if host.endswith('.local.mesh') else host
    return names.get(msg.get('ip')) or names.get(host) or host or msg.get('ip')


def store(messages, conn=None):
    """Insert messages (dicts); duplicates (same node, node time, program,
    message, and for UDP the receive time) are skipped. Returns rows added."""
    ensure_tables()
    if not messages:
        return 0
    if conn is None:
        with database.get_connection() as c:
            return store(messages, c)
    names = _names(conn)
    added = 0
    for m in messages:
        node = m.get('node_name') or resolve_node(m, names)
        h = _hash(node, m.get('node_time') or m['received_at'], m.get('program'), m.get('pid'), m.get('message'),
                  m['received_at'] if m.get('source') == 'udp' and not m.get('node_time') else '')
        cur = conn.execute('''INSERT OR IGNORE INTO syslog_messages (received_at, node_time, node_name, host, ip,
                              facility, severity, program, pid, message, source, hash)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (m['received_at'], m.get('node_time'), node, m.get('host'), m.get('ip'), m.get('facility'),
                            m.get('severity'), m.get('program'), m.get('pid'), m.get('message'), m.get('source'), h))
        added += cur.rowcount
    return added


def ingest_logread(node, text, now=None):
    try:
        return store(parse_logread(text, node, now))
    except Exception as exc:
        logger.debug('logread import failed for %s: %s', node, exc)
        return 0


def import_snapshots(hours=48):
    """Import recent logread snapshots already taken by the SSH collector."""
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        try:
            rows = conn.execute("SELECT node_name, text FROM ssh_snapshots WHERE kind = 'logread' AND timestamp >= ?",
                                (since,)).fetchall()
        except Exception:
            return 0
    return sum(ingest_logread(r['node_name'], r['text']) for r in rows)


# --------------------------------------------------------------- receiver --

def _flush():
    with _lock:
        batch = list(_buffer)
        _buffer.clear()
    if batch:
        try:
            _state['stored'] += store(batch)
        except Exception as exc:
            _state['error'] = f'store: {type(exc).__name__}: {exc}'[:200]
            logger.warning('syslog store failed: %s', exc)


def _serve(sock):
    last_flush = time.time()
    while _state['running']:
        try:
            data, addr = sock.recvfrom(8192)
        except socket.timeout:
            data = None
        except OSError as exc:
            _state['error'] = str(exc)[:200]
            time.sleep(1)
            continue
        if data:
            _state['received'] += 1
            msg = parse(data, addr[0])
            if msg:
                _state['last'] = msg['received_at']
                with _lock:
                    if len(_buffer) < 20000:
                        _buffer.append(msg)
                    else:
                        _state['dropped'] += 1
        if time.time() - last_flush >= 2:
            last_flush = time.time()
            _flush()


def install(start_task=None):
    """Start the UDP receiver (only when SYSLOG_ENABLED; from __main__)."""
    if not config.SYSLOG_ENABLED or _state['running']:
        return False
    ensure_tables()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.SYSLOG_HOST, config.SYSLOG_PORT))
        sock.settimeout(1.0)
    except OSError as exc:
        _state['error'] = f'could not listen on UDP {config.SYSLOG_PORT}: {exc}'
        logger.warning('Syslog receiver: %s', _state['error'])
        return False
    _state.update(running=True, started_at=datetime.now().strftime(TS_FMT), error=None)
    if start_task:
        start_task(_serve, sock)
    else:
        threading.Thread(target=_serve, args=(sock,), name='syslog-rx', daemon=True).start()
    logger.info('Syslog receiver listening on UDP %s:%s', config.SYSLOG_HOST, config.SYSLOG_PORT)
    return True


# ----------------------------------------------------------------- queries --

def _where(node=None, max_severity=None, program=None, q=None, since=None, until=None, source=None):
    sql, args = ' WHERE 1=1', []
    if node:
        names = [n.strip().lower() for n in str(node).split(',') if n.strip()]
        sql += f" AND node_name IN ({', '.join('?' * len(names))})"
        args += names
    if max_severity is not None and str(max_severity) != '':
        sql += ' AND severity <= ?'
        args.append(int(max_severity))
    if program:
        sql += ' AND program LIKE ?'
        args.append(f'%{program}%')
    if q:
        sql += ' AND (message LIKE ? OR program LIKE ? OR node_name LIKE ?)'
        args += [f'%{q}%'] * 3
    if since:
        sql += ' AND received_at >= ?'
        args.append(since)
    if until:
        sql += ' AND received_at < ?'
        args.append(until)
    if source:
        sql += ' AND source = ?'
        args.append(source)
    return sql, args


def chips_of(row):
    text_prog = row.get('program') or ''
    out = [k for k, rx in CHIPS.items() if rx.search(text_prog if k == 'kernel' else f"{text_prog} {row.get('message') or ''}")]
    return out


def query(node=None, max_severity=None, program=None, chips=None, q=None, since=None, until=None, source=None,
          collapse=False, limit=500):
    ensure_tables()
    limit = max(1, min(int(limit or 500), 5000))
    sql, args = _where(node, max_severity, program, q, since, until, source)
    chips = [c for c in (chips or []) if c in CHIPS]
    fetch = limit * (8 if chips or collapse else 1)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM syslog_messages{sql} ORDER BY received_at DESC, id DESC '
                                              'LIMIT ?', args + [fetch])]
        total = conn.execute(f'SELECT COUNT(*) FROM syslog_messages{sql}', args).fetchone()[0]
    out = []
    for r in rows:
        r['chips'] = chips_of(r)
        r['severity_name'] = SEVERITIES[r['severity']] if r.get('severity') is not None and 0 <= r['severity'] < 8 else None
        r.pop('hash', None)
        if chips and not set(chips) & set(r['chips']):
            continue
        out.append(r)
    if collapse:
        groups = {}
        merged = []
        for r in out:
            key = (r['node_name'], r['program'], NUMBERS.sub('#', r['message'] or ''))
            g = groups.get(key)
            if g is None:
                g = dict(r, count=1, first=r['received_at'], last=r['received_at'])
                groups[key] = g
                merged.append(g)
            else:
                g['count'] += 1
                g['first'] = r['received_at']
        out = merged
    return {'messages': out[:limit], 'total': total, 'shown': min(len(out), limit), 'collapsed': bool(collapse)}


def rate(hours=24, node=None, bucket_minutes=None):
    """Counts per bucket: errors (sev <= 3), warnings (4), other."""
    ensure_tables()
    bucket = bucket_minutes or (10 if hours <= 6 else 60 if hours <= 72 else 240)
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    sql, args = _where(node=node, since=since)
    with database.get_connection() as conn:
        rows = conn.execute(f'SELECT received_at, severity FROM syslog_messages{sql}', args).fetchall()
    buckets = {}
    for ts, sev in rows:
        t = datetime.strptime(ts[:19], TS_FMT)
        start = t - timedelta(minutes=t.minute % bucket if bucket < 60 else t.minute,
                              hours=(t.hour % (bucket // 60)) if bucket >= 60 else 0, seconds=t.second)
        b = buckets.setdefault(start.strftime(TS_FMT), {'t': start.strftime(TS_FMT), 'errors': 0, 'warnings': 0, 'other': 0})
        b['errors' if sev is not None and sev <= 3 else 'warnings' if sev == 4 else 'other'] += 1
    return {'hours': hours, 'bucket_minutes': bucket, 'series': [buckets[k] for k in sorted(buckets)]}


def csv_text(**filters):
    res = query(**filters)
    buf = io.StringIO()
    w = csv.writer(buf)
    zone = local_time.abbr()
    w.writerow([f'received_at ({zone})', 'node_time (node clock)', 'node', 'ip', 'facility', 'severity', 'program', 'pid', 'message', 'source']
               + (['count'] if res['collapsed'] else []))
    for r in res['messages']:
        w.writerow([r['received_at'], r.get('node_time'), r.get('node_name'), r.get('ip'), r.get('facility'),
                    r.get('severity_name'), r.get('program'), r.get('pid'), r.get('message'), r.get('source')]
                   + ([r.get('count')] if res['collapsed'] else []))
    return buf.getvalue()


def status():
    ensure_tables()
    with database.get_connection() as conn:
        total, newest = conn.execute('SELECT COUNT(*), MAX(received_at) FROM syslog_messages').fetchone()
        nodes = [dict(r) for r in conn.execute('''SELECT node_name, source, COUNT(*) AS messages, MAX(received_at) AS last
                                                  FROM syslog_messages GROUP BY node_name, source ORDER BY node_name''')]
    return dict(_state, enabled=config.SYSLOG_ENABLED, port=config.SYSLOG_PORT, host=config.SYSLOG_HOST,
                total=total, newest=newest, nodes=nodes, chips=CHIP_LABELS, severities=list(SEVERITIES),
                retention_days=config.SYSLOG_RETENTION_DAYS)


def cleanup(now=None):
    ensure_tables()
    cut = ((now or datetime.now()) - timedelta(days=config.SYSLOG_RETENTION_DAYS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM syslog_messages WHERE received_at < ?', (cut,)).rowcount

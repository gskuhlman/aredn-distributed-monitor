"""
Service checks (Diagnostics tab "Service Checks"): probe the services nodes
advertise (web pages, PBX/phones, cameras, Winlink, chat) and alert when one
stops answering while its node is still up.

Every SERVICE_CHECK_INTERVAL_MINUTES (own APScheduler job, gated by
gateway.py: skipped when the starting node is unreachable) each advertised
service with a probe-able link is checked:
  http(s)://host:port/path   GET without following redirects; any answer
                              below 400 (or 401/403: up, needs a login) is up;
  sip/telnet/winlink/...      TCP connect to host:port;
  /a/redirect?... links       point outside the mesh: not probed (external);
  no link / udp (ntp)         not probed.
Mesh hostnames without a dot get `.local.mesh`; when the name doesn't
resolve, the service's advertised IP is tried.

A service alarm needs SERVICE_CHECK_FAILS failed checks in a row while its
node answered the scanner (is_active, seen within two poll intervals); a node
that is down is the scanner's alarm, not a service's. Events on transitions:
`service_down` (warning), `service_restored` (info); covered maintenance
windows suppress them via notifications as usual.
SQLite `service_checks` keeps per service the current state and counters.

An admin can mute one service on one node (`service_mutes`, node page):
  silence  still probed and shown, but raises no alarm or event until it
           ends (1 h .. 1 month, or until enabled again: until NULL); an
           alarm already up is cleared quietly. A service still failing when
           the silence ends alarms on its next failed check.
  ignore   not probed at all and left out of the counts, until enabled again.
Expired silences are dropped on the next run.
A device (a host on a node's LAN, Services & Devices page) is muted the same
way under service_name `device:<ip>`; phones.py honours it for the phone at
that IP (silence: no phone alarm; ignore: not pinged).
"""

import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

import config
import database
import gateway

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
TCP_PORTS = {'sip': 5060, 'telnet': 23, 'winlink': 8772, 'ssh': 22, 'rtsp': 554, 'vnc': 5900}
UDP_SCHEMES = ('ntp', 'udp', 'tftp')
TIMEOUT = 8

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_checks (
                node_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                link TEXT,
                kind TEXT,
                ok INTEGER,
                fail_count INTEGER NOT NULL DEFAULT 0,
                alarm INTEGER NOT NULL DEFAULT 0,
                since DATETIME,
                last_check DATETIME,
                last_ok DATETIME,
                last_error TEXT,
                last_ms INTEGER,
                checks INTEGER NOT NULL DEFAULT 0,
                oks INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (node_name, service_name)
            )
        ''')
        # Per-day tallies for weekly availability (service_checks only keeps running totals).
        # A check counts only while the node is up: a node outage is the scanner's, not the service's.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_check_daily (
                day TEXT NOT NULL,
                node_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                checks INTEGER NOT NULL DEFAULT 0,
                oks INTEGER NOT NULL DEFAULT 0,
                ms_total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, node_name, service_name)
            )
        ''')
        # Every probe (TODO #94): heartbeat bars, response-time charts, 24 h / 30 d uptime.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_probes (
                ts DATETIME NOT NULL,
                node_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                ok INTEGER NOT NULL,
                ms INTEGER,
                error TEXT,
                node_up INTEGER NOT NULL DEFAULT 1
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_service_probes_svc ON service_probes (node_name, service_name, ts)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_service_probes_ts ON service_probes (ts)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_mutes (
                node_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('silence', 'ignore')),
                until DATETIME,
                set_at DATETIME NOT NULL,
                PRIMARY KEY (node_name, service_name)
            )
        ''')
    _tables_ready = True


MUTE_MODES = ('silence', 'ignore')
DAILY_KEEP_DAYS = 400
SILENCE_HOURS = (1, 6, 24, 168, 720)     # 1 h, 6 h, 1 d, 1 w, 1 month; None = until enabled again


def mutes(conn, now=None):
    """{(node, service): {'mode', 'until', 'set_at'}} for mutes still in force."""
    now_s = (now or datetime.now()).strftime(TS_FMT)
    return {(r['node_name'], r['service_name']): {'mode': r['mode'], 'until': r['until'], 'set_at': r['set_at']}
            for r in conn.execute('''SELECT * FROM service_mutes
                                     WHERE until IS NULL OR until > ?''', (now_s,))}


DEVICE_PREFIX = 'device:'


def device_mutes(conn, now=None):
    """{ip: mute} for muted devices (any node)."""
    return {k[1][len(DEVICE_PREFIX):]: m for k, m in mutes(conn, now).items() if k[1].startswith(DEVICE_PREFIX)}


def mute_counts(muted):
    """{'silenced': n, 'ignored': n} over services and devices."""
    vals = list(muted.values())
    return {'silenced': sum(1 for m in vals if m['mode'] == 'silence'),
            'ignored': sum(1 for m in vals if m['mode'] == 'ignore')}


def set_mute(node, service, mode, hours=None, now=None):
    """Silence (for `hours`, or until enabled again when None) or ignore one
    service on one node. Returns (mute, error)."""
    ensure_tables()
    node, service = (node or '').strip(), (service or '').strip()
    if not node or not service:
        return None, 'node and service are required'
    if mode not in MUTE_MODES:
        return None, f"mode must be one of {', '.join(MUTE_MODES)}"
    if mode == 'ignore':
        hours = None
    if hours is not None:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return None, 'hours must be a number'
        if hours not in SILENCE_HOURS:
            return None, f"hours must be one of {', '.join(map(str, SILENCE_HOURS))} (or none: until enabled)"
    now = now or datetime.now()
    until = (now + timedelta(hours=hours)).strftime(TS_FMT) if hours else None
    with database.get_connection() as conn:
        conn.execute('''INSERT OR REPLACE INTO service_mutes (node_name, service_name, mode, until, set_at)
                        VALUES (?, ?, ?, ?, ?)''', (node, service, mode, until, now.strftime(TS_FMT)))
        # A muted service has no alarm; a silence that ends alarms afresh.
        conn.execute('''UPDATE service_checks SET alarm = 0, since = NULL
                        WHERE node_name = ? AND service_name = ?''', (node, service))
        if service.startswith(DEVICE_PREFIX):
            try:
                conn.execute('UPDATE phones SET alarm = 0, since = NULL WHERE ip = ?', (service[len(DEVICE_PREFIX):],))
            except Exception:
                pass    # no phones table yet
    return {'node_name': node, 'service_name': service, 'mode': mode, 'until': until}, None


def clear_mute(node, service):
    """Enable alerts (and checks) again. Returns True when a mute was removed."""
    ensure_tables()
    with database.get_connection() as conn:
        cur = conn.execute('DELETE FROM service_mutes WHERE node_name = ? AND service_name = ?', (node, service))
        if cur.rowcount:
            # An ignored service starts counting afresh.
            conn.execute('''UPDATE service_checks SET fail_count = 0
                            WHERE node_name = ? AND service_name = ? AND alarm = 0''', (node, service))
    return bool(cur.rowcount)


def _host(host):
    if not host:
        return host
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host) or '.' in host:
        return host
    return f'{host}.local.mesh'


def target(service):
    """How to probe one service: {kind, host, port, url} or {kind, reason}."""
    link = (service.get('link') or '').strip()
    if not link:
        return {'kind': 'none', 'reason': 'no link advertised'}
    if '/a/redirect?' in link or '/cgi-bin/redirect' in link:
        return {'kind': 'external', 'reason': 'points outside the mesh'}
    parts = urlsplit(link if '://' in link else 'tcp://' + link)
    scheme = (parts.scheme or '').lower()
    if scheme in UDP_SCHEMES:
        return {'kind': 'none', 'reason': f'{scheme} (UDP) is not probed'}
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname
    if not host:
        return {'kind': 'none', 'reason': 'no host in the link'}
    if scheme in ('http', 'https'):
        return {'kind': 'http', 'host': host, 'port': port or (443 if scheme == 'https' else 80),
                'url': f"{scheme}://{_host(host)}:{port or (443 if scheme == 'https' else 80)}"
                       f"{parts.path or '/'}" + (f'?{parts.query}' if parts.query else '')}
    port = port or TCP_PORTS.get(scheme)
    if not port:
        return {'kind': 'none', 'reason': f'no port for {scheme or "this link"}'}
    return {'kind': 'tcp', 'host': host, 'port': port}


def probe(service):
    """(ok, error, ms, kind) for one service; never raises."""
    t = target(service)
    if t['kind'] in ('none', 'external'):
        return None, t['reason'], None, t['kind']
    started = datetime.now()
    hosts = [_host(t['host'])]
    if service.get('ip'):
        hosts.append(service['ip'])
    last_error = None
    for h in hosts:
        try:
            if t['kind'] == 'http':
                url = t['url'].replace(_host(t['host']), h, 1)
                r = requests.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                                 headers={'User-Agent': 'aredn-monitor service check'})
                r.close()
                ms = int((datetime.now() - started).total_seconds() * 1000)
                if r.status_code < 400 or r.status_code in (401, 403):
                    return True, None, ms, 'http'
                return False, f'HTTP {r.status_code}', ms, 'http'
            with socket.create_connection((h, t['port']), timeout=TIMEOUT):
                pass
            return True, None, int((datetime.now() - started).total_seconds() * 1000), 'tcp'
        except requests.exceptions.ConnectTimeout:
            last_error = 'connect timeout'
        except requests.exceptions.ReadTimeout:
            last_error = 'no answer (read timeout)'
        except (requests.exceptions.ConnectionError, OSError) as exc:
            text = str(exc).lower()
            if 'getaddrinfo' in text or 'name resolution' in text or 'not known' in text or '11001' in text:
                last_error = 'name does not resolve'
                continue  # try the advertised IP
            last_error = ('refused' if 'refused' in text or '10061' in text else
                          'timeout' if 'timed out' in text or '10060' in text else 'connection error')
        except requests.exceptions.RequestException as exc:
            last_error = type(exc).__name__
        break
    return False, last_error, None, t['kind']


def _node_up(node, now):
    seen = node.get('last_seen')
    if not node.get('is_active') or not seen:
        return False
    try:
        age = (now - datetime.strptime(seen[:19], TS_FMT)).total_seconds()
    except ValueError:
        return False
    return age <= max(2 * int(config.POLL_INTERVAL or 180), 600)


def run(now=None):
    """Probe every service once. Returns {'events': [...], 'aborted': reason}."""
    ensure_tables()
    now = now or datetime.now()
    if not gateway.is_reachable():
        return {'events': [], 'aborted': 'starting node unreachable'}
    with database.get_connection() as conn:
        services = [dict(r) for r in conn.execute('SELECT node_name, name, protocol, link, ip FROM services')]
        nodes = {r['name']: dict(r) for r in conn.execute('SELECT name, is_active, last_seen FROM nodes')}
        states = {(r['node_name'], r['service_name']): dict(r) for r in conn.execute('SELECT * FROM service_checks')}
        conn.execute('DELETE FROM service_mutes WHERE until IS NOT NULL AND until <= ?', (now.strftime(TS_FMT),))
        muted = mutes(conn, now)
    advertised = {}
    for s in services:
        advertised.setdefault((s['node_name'], s['name']), s)
    # Ignored services are not probed; their rows stay as they were.
    seen = {k: s for k, s in advertised.items() if (muted.get(k) or {}).get('mode') != 'ignore'}
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = dict(zip(seen, pool.map(probe, seen.values())))
    now_s = now.strftime(TS_FMT)
    day = now.strftime('%Y-%m-%d')
    events = []
    rows = []
    daily = []
    probes = []
    for key, s in seen.items():
        ok, error, ms, kind = results[key]
        st = states.get(key) or {'fail_count': 0, 'alarm': 0, 'since': None, 'last_ok': None,
                                 'checks': 0, 'oks': 0}
        node_up = _node_up(nodes.get(s['node_name']) or {}, now)
        fail_count, alarm, since = st['fail_count'], st['alarm'], st['since']
        checks, oks, last_ok = st['checks'], st['oks'], st['last_ok']
        silenced = key in muted
        if ok is not None:
            checks += 1
        if ok:
            oks += 1
            last_ok = now_s
            if alarm and not silenced:
                events.append(('service_restored', s['node_name'],
                               f"Service {s['name']} ({s['link']}) answers again", 'info'))
            fail_count, alarm, since = 0, 0, None
        elif ok is False:
            if node_up:
                fail_count += 1
                if not alarm and not silenced and fail_count >= config.SERVICE_CHECK_FAILS:
                    alarm, since = 1, now_s
                    events.append(('service_down', s['node_name'],
                                   f"Service {s['name']} ({s['link']}) not answering: {error}; "
                                   f"node {s['node_name']} is up", 'warning'))
            # node down: the scanner reports that; keep the counters as they are
        if ok is not None:
            probes.append((now_s, s['node_name'], s['name'], 1 if ok else 0, ms if ok else None,
                           None if ok else (error or '')[:200], 1 if (ok or node_up) else 0))
        if ok or (ok is False and node_up):
            daily.append((day, s['node_name'], s['name'], 1 if ok else 0, int(ms or 0) if ok else 0))
        rows.append((s['node_name'], s['name'], s['link'], kind, None if ok is None else int(bool(ok)),
                     fail_count, alarm, since, now_s, last_ok, error, ms, checks, oks))
    with database.get_connection() as conn:
        conn.executemany('''INSERT OR REPLACE INTO service_checks (node_name, service_name, link, kind, ok,
                                fail_count, alarm, since, last_check, last_ok, last_error, last_ms, checks, oks)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', rows)
        # Services no longer advertised are dropped.
        stale = [k for k in states if k not in advertised]
        conn.executemany('DELETE FROM service_checks WHERE node_name = ? AND service_name = ?', stale)
        conn.executemany('''INSERT INTO service_check_daily (day, node_name, service_name, checks, oks, ms_total)
                            VALUES (?, ?, ?, 1, ?, ?)
                            ON CONFLICT(day, node_name, service_name) DO UPDATE SET
                                checks = checks + 1, oks = oks + excluded.oks, ms_total = ms_total + excluded.ms_total''',
                         daily)
        conn.executemany('''INSERT INTO service_probes (ts, node_name, service_name, ok, ms, error, node_up)
                            VALUES (?, ?, ?, ?, ?, ?, ?)''', probes)
        conn.execute('DELETE FROM service_probes WHERE ts < ?',
                     ((now - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT),))
        conn.execute('DELETE FROM service_check_daily WHERE day < ?',
                     ((now - timedelta(days=DAILY_KEEP_DAYS)).strftime('%Y-%m-%d'),))
    out = []
    for kind, node, text, sev in events:
        database.log_event(kind, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': kind, 'node_name': node, 'details': text, 'severity': sev})
    return {'events': out, 'aborted': None, 'checked': sum(1 for r in results.values() if r[0] is not None)}


def availability(start, end):
    """Per service over the days in [start, end) (datetimes): checks while the
    node was up, answered %, average ms, and service_down events. Sorted worst first."""
    ensure_tables()
    d0, d1 = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT node_name, service_name, SUM(checks) AS checks, SUM(oks) AS oks, SUM(ms_total) AS ms_total,
                   MIN(day) AS first_day
            FROM service_check_daily WHERE day >= ? AND day <= ?
            GROUP BY node_name, service_name''', (d0, d1))]
        downs = {}
        for r in conn.execute('''SELECT node_name, details FROM events WHERE event_type = 'service_down'
                                 AND timestamp >= ? AND timestamp < ?''', (start.strftime(TS_FMT), end.strftime(TS_FMT))):
            downs[r['node_name']] = downs.get(r['node_name'], []) + [r['details'] or '']
        muted = mutes(conn)
    for r in rows:
        r['availability_pct'] = round(100.0 * r['oks'] / r['checks'], 2) if r['checks'] else None
        r['avg_ms'] = round(r['ms_total'] / r['oks']) if r['oks'] else None
        r['outages'] = sum(1 for d in downs.get(r['node_name'], []) if d.startswith(f"Service {r['service_name']} ("))
        m = muted.get((r['node_name'], r['service_name']))
        r['mute'] = m['mode'] if m else None
        del r['ms_total']
    rows.sort(key=lambda r: (r['availability_pct'] if r['availability_pct'] is not None else 101, r['node_name']))
    return rows


BEATS = 50


def _pct(oks, checks):
    return round(100.0 * oks / checks, 2) if checks else None


def beats_and_uptime(conn, now=None, node=None, limit=BEATS):
    """{(node, service): {'beats': [[ts, state, ms, error], ...] oldest first, 'uptime': {'24h', '30d', '1y'}}}.
    state 1 = answered, 0 = failed while the node was up, -1 = failed with the node down (not counted).
    24 h / 30 d come from the probes, 1 y from the daily tallies."""
    now = now or datetime.now()
    where, args = '', []
    if node:
        where, args = 'WHERE node_name = ?', [node]
    out = {}
    for r in conn.execute(f'''
            SELECT node_name, service_name, ts, ok, ms, error, node_up FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY node_name, service_name ORDER BY ts DESC) AS rn
                FROM service_probes {where})
            WHERE rn <= ? ORDER BY node_name, service_name, ts''', args + [limit]):
        state = 1 if r['ok'] else (0 if r['node_up'] else -1)
        out.setdefault((r['node_name'], r['service_name']), {'beats': [], 'uptime': {}})['beats'].append(
            [r['ts'], state, r['ms'], r['error']])
    spans = (('24h', now - timedelta(hours=24)), ('30d', now - timedelta(days=30)))
    for key, since in spans:
        for r in conn.execute(f'''SELECT node_name, service_name, SUM(ok) AS oks,
                                         SUM(CASE WHEN ok = 1 OR node_up = 1 THEN 1 ELSE 0 END) AS checks
                                  FROM service_probes WHERE ts >= ? {'AND node_name = ?' if node else ''}
                                  GROUP BY node_name, service_name''', [since.strftime(TS_FMT)] + args):
            out.setdefault((r['node_name'], r['service_name']), {'beats': [], 'uptime': {}})['uptime'][key] = \
                _pct(r['oks'] or 0, r['checks'])
    for r in conn.execute(f'''SELECT node_name, service_name, SUM(oks) AS oks, SUM(checks) AS checks
                              FROM service_check_daily WHERE day >= ? {'AND node_name = ?' if node else ''}
                              GROUP BY node_name, service_name''',
                          [(now - timedelta(days=365)).strftime('%Y-%m-%d')] + args):
        out.setdefault((r['node_name'], r['service_name']), {'beats': [], 'uptime': {}})['uptime']['1y'] = \
            _pct(r['oks'] or 0, r['checks'])
    return out


def history(node, service, hours=24, now=None):
    """One service's probes over `hours` (response-time chart), its last beats and uptime."""
    ensure_tables()
    now = now or datetime.now()
    hours = max(1, min(int(hours or 24), config.HISTORY_RETENTION_HOURS))
    with database.get_connection() as conn:
        probes = [{'ts': r['ts'], 'ok': bool(r['ok']), 'ms': r['ms'], 'error': r['error'], 'node_up': bool(r['node_up'])}
                  for r in conn.execute('''SELECT ts, ok, ms, error, node_up FROM service_probes
                                           WHERE node_name = ? AND service_name = ? AND ts >= ? ORDER BY ts''',
                                        (node, service, (now - timedelta(hours=hours)).strftime(TS_FMT)))]
        extra = beats_and_uptime(conn, now, node).get((node, service), {'beats': [], 'uptime': {}})
        first = conn.execute('SELECT MIN(ts) FROM service_probes WHERE node_name = ? AND service_name = ?',
                             (node, service)).fetchone()[0]
    ms = sorted(p['ms'] for p in probes if p['ms'] is not None)
    counted = [p for p in probes if p['ok'] or p['node_up']]
    return {'node': node, 'service': service, 'hours': hours, 'probes': probes, 'beats': extra['beats'],
            'uptime': extra['uptime'], 'history_since': first,
            'window': {'checks': len(counted), 'answered': sum(1 for p in counted if p['ok']),
                       'uptime_pct': _pct(sum(1 for p in counted if p['ok']), len(counted)),
                       'ms_median': ms[len(ms) // 2] if ms else None,
                       'ms_p95': ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else None}}


def report(history=True):
    """Every service's state; with history, its last beats and uptime (beats_and_uptime)."""
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT c.*, n.is_active AS node_active, n.last_seen AS node_last_seen
            FROM service_checks c LEFT JOIN nodes n ON n.name = c.node_name
            ORDER BY c.alarm DESC, (c.ok = 0) DESC, c.node_name, c.service_name''')]
        now = datetime.now()
        muted = mutes(conn, now)
        hist = beats_and_uptime(conn, now) if history else {}
    for r in rows:
        h = hist.get((r['node_name'], r['service_name'])) or {}
        r['beats'] = h.get('beats', [])
        r['uptime'] = h.get('uptime', {})
        r['mute'] = muted.get((r['node_name'], r['service_name']))
        r['node_up'] = _node_up({'is_active': r['node_active'], 'last_seen': r['node_last_seen']}, now)
        r['availability_pct'] = round(100.0 * r['oks'] / r['checks'], 1) if r['checks'] else None
        r['state'] = ('alarm' if r['alarm'] else 'down' if r['ok'] == 0 and r['node_up'] else
                      'node_down' if r['ok'] == 0 else 'up' if r['ok'] else r['kind'] or 'unchecked')
        if r['mute'] and r['mute']['mode'] == 'ignore':
            r['state'] = 'ignored'
        elif r['mute'] and r['state'] in ('alarm', 'down'):
            r['state'] = 'silenced'
    mute_list = [{'node_name': k[0], 'service_name': k[1], **m,
                  'kind': 'device' if k[1].startswith(DEVICE_PREFIX) else 'service'} for k, m in muted.items()]
    counts = {}
    for r in rows:
        counts[r['state']] = counts.get(r['state'], 0) + 1
    last = max((r['last_check'] for r in rows if r['last_check']), default=None)
    return {'services': rows, 'counts': counts, 'last_check': last, 'mutes': mute_list,
            'mute_counts': mute_counts(muted),
            'silence_hours': list(SILENCE_HOURS),
            'interval_minutes': config.SERVICE_CHECK_INTERVAL_MINUTES, 'enabled': config.SERVICE_CHECK_ENABLED}


def install(scheduler, job):
    if not config.SERVICE_CHECK_ENABLED:
        return
    scheduler.add_job(job, 'interval', minutes=config.SERVICE_CHECK_INTERVAL_MINUTES, id='service_checks',
                      replace_existing=True, next_run_time=datetime.now() + timedelta(minutes=4))

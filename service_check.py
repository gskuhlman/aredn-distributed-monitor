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
    _tables_ready = True


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
    seen = {}
    for s in services:
        seen.setdefault((s['node_name'], s['name']), s)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = dict(zip(seen, pool.map(probe, seen.values())))
    now_s = now.strftime(TS_FMT)
    events = []
    rows = []
    for key, s in seen.items():
        ok, error, ms, kind = results[key]
        st = states.get(key) or {'fail_count': 0, 'alarm': 0, 'since': None, 'last_ok': None,
                                 'checks': 0, 'oks': 0}
        node_up = _node_up(nodes.get(s['node_name']) or {}, now)
        fail_count, alarm, since = st['fail_count'], st['alarm'], st['since']
        checks, oks, last_ok = st['checks'], st['oks'], st['last_ok']
        if ok is not None:
            checks += 1
        if ok:
            oks += 1
            last_ok = now_s
            if alarm:
                events.append(('service_restored', s['node_name'],
                               f"Service {s['name']} ({s['link']}) answers again", 'info'))
            fail_count, alarm, since = 0, 0, None
        elif ok is False:
            if node_up:
                fail_count += 1
                if not alarm and fail_count >= config.SERVICE_CHECK_FAILS:
                    alarm, since = 1, now_s
                    events.append(('service_down', s['node_name'],
                                   f"Service {s['name']} ({s['link']}) not answering: {error}; "
                                   f"node {s['node_name']} is up", 'warning'))
            # node down: the scanner reports that; keep the counters as they are
        rows.append((s['node_name'], s['name'], s['link'], kind, None if ok is None else int(bool(ok)),
                     fail_count, alarm, since, now_s, last_ok, error, ms, checks, oks))
    with database.get_connection() as conn:
        conn.executemany('''INSERT OR REPLACE INTO service_checks (node_name, service_name, link, kind, ok,
                                fail_count, alarm, since, last_check, last_ok, last_error, last_ms, checks, oks)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', rows)
        # Services no longer advertised are dropped.
        stale = [k for k in states if k not in seen]
        conn.executemany('DELETE FROM service_checks WHERE node_name = ? AND service_name = ?', stale)
    out = []
    for kind, node, text, sev in events:
        database.log_event(kind, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': kind, 'node_name': node, 'details': text, 'severity': sev})
    return {'events': out, 'aborted': None, 'checked': sum(1 for r in results.values() if r[0] is not None)}


def report():
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT c.*, n.is_active AS node_active, n.last_seen AS node_last_seen
            FROM service_checks c LEFT JOIN nodes n ON n.name = c.node_name
            ORDER BY c.alarm DESC, (c.ok = 0) DESC, c.node_name, c.service_name''')]
    now = datetime.now()
    for r in rows:
        r['node_up'] = _node_up({'is_active': r['node_active'], 'last_seen': r['node_last_seen']}, now)
        r['availability_pct'] = round(100.0 * r['oks'] / r['checks'], 1) if r['checks'] else None
        r['state'] = ('alarm' if r['alarm'] else 'down' if r['ok'] == 0 and r['node_up'] else
                      'node_down' if r['ok'] == 0 else 'up' if r['ok'] else r['kind'] or 'unchecked')
    counts = {}
    for r in rows:
        counts[r['state']] = counts.get(r['state'], 0) + 1
    last = max((r['last_check'] for r in rows if r['last_check']), default=None)
    return {'services': rows, 'counts': counts, 'last_check': last,
            'interval_minutes': config.SERVICE_CHECK_INTERVAL_MINUTES, 'enabled': config.SERVICE_CHECK_ENABLED}


def install(scheduler, job):
    if not config.SERVICE_CHECK_ENABLED:
        return
    scheduler.add_job(job, 'interval', minutes=config.SERVICE_CHECK_INTERVAL_MINUTES, id='service_checks',
                      replace_existing=True, next_run_time=datetime.now() + timedelta(minutes=4))

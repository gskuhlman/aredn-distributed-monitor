"""
Starting-node (collector) health: is a problem on this PC / its uplink / the
starting node, or out on the mesh?

After every scan cycle a background task measures the path to the configured
starting node in steps -- DNS resolution (OS resolver; skipped for an IP),
TCP connect, bare sysinfo over HTTP -- and, only when that fails, pings this
PC's default gateway so "this PC lost its LAN" can be told from "the starting
node is down". Each measurement is stored with the scan's outcome (aborted or
not, duration, nodes attempted/reachable) in SQLite `collector_health`.

`report()` turns a window of rows into a status summary, latency/DNS
percentiles, a bucketed series and outages (contiguous failures). History from
before this table existed comes from `gateway_lost`/`gateway_restored` events
and from failed polls of the starting node that older code stored under its
URL slug (e.g. "10" for 10.x.x.x) in `node_health_history`.

Everything here is best effort: network failures are expected and recorded,
and nothing raises into the scan.
"""

import ipaddress
import logging
import platform
import re
import socket
import subprocess
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

import config
import database
import gateway
import observations

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
CONNECT_TIMEOUT = 5
LAN_PING_TIMEOUT_MS = 1000
LAN_GATEWAY_CACHE_SECONDS = 600
# A gap between failures longer than this splits a legacy outage in two.
LEGACY_GAP_SECONDS = 20 * 60

CAUSE_LABELS = {
    'lan_down': "This PC lost its LAN (default gateway not answering)",
    'dns': "Starting node name doesn't resolve (DNS)",
    'refused': "Starting node refuses the connection (web server down?)",
    'connect_timeout': "Starting node not answering (no connection)",
    'no_route': "No route to the starting node",
    'timeout': "Starting node connected but didn't answer in time (overloaded?)",
    'http_error': "Starting node web server error",
    'bad_json': "Starting node returned invalid sysinfo",
    'connection_error': "Connection to the starting node failed",
    'lost_mid_scan': "Starting node stopped answering during the scan (answered the check after)",
    'scan_error': "Scan failed with an internal error",
    'no_answer': "Starting node didn't answer within the request timeout (older data: no detail recorded)",
    'unknown': "Starting node unreachable (cause not recorded)",
}


# ------------------------------------------------------------- schema -----

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS collector_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'scan',
                starting_node TEXT,
                host TEXT,
                node_name TEXT,
                ok INTEGER NOT NULL,
                error_class TEXT,
                error TEXT,
                dns_ms REAL,
                connect_ms REAL,
                http_ms REAL,
                lan_gateway TEXT,
                lan_ok INTEGER,
                lan_ms REAL,
                scan_aborted INTEGER,
                abort_reason TEXT,
                scan_ms INTEGER,
                nodes_attempted INTEGER,
                nodes_reachable INTEGER,
                selected_scan INTEGER
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_collector_health_ts ON collector_health(timestamp)')
    _tables_ready = True


# -------------------------------------------------------- measurement -----

def _ms(started):
    return round((time.perf_counter() - started) * 1000, 1)


def _target():
    """(starting node URL setting, host, port) of the configured starting node."""
    url = database.get_setting('starting_node', config.STARTING_NODE) or ''
    parsed = urlparse(url if '://' in url else f'http://{url}')
    return url, (parsed.hostname or '').lower(), parsed.port or 80


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


_lan_cache = {'at': 0.0, 'gateway': None}


def default_gateway():
    """This PC's IPv4 default gateway, or None (cached for a few minutes)."""
    if time.monotonic() - _lan_cache['at'] < LAN_GATEWAY_CACHE_SECONDS:
        return _lan_cache['gateway']
    gw = None
    try:
        system = platform.system()
        if system == 'Windows':
            out = subprocess.run(['route', 'print', '-4', '0.0.0.0'], capture_output=True,
                                 text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] == '0.0.0.0' and parts[1] == '0.0.0.0' and _is_ip(parts[2]):
                    gw = parts[2]
                    break
        elif system == 'Linux':
            with open('/proc/net/route') as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == '00000000':
                        gw = socket.inet_ntoa(int(parts[2], 16).to_bytes(4, 'little'))
                        break
        else:
            out = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True,
                                 text=True, timeout=5).stdout
            match = re.search(r'gateway:\s*(\S+)', out)
            gw = match.group(1) if match else None
    except Exception as exc:
        logger.debug("Default gateway lookup failed: %s", exc)
    _lan_cache.update(at=time.monotonic(), gateway=gw)
    return gw


def ping_once(host, timeout_ms=LAN_PING_TIMEOUT_MS):
    """(ok, ms) for one ICMP echo via the OS ping command."""
    if platform.system() == 'Windows':
        cmd = ['ping', '-n', '1', '-w', str(timeout_ms), host]
    else:
        cmd = ['ping', '-c', '1', '-W', str(max(1, timeout_ms // 1000)), host]
    started = time.perf_counter()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_ms / 1000 + 3).stdout
    except Exception:
        return False, None
    # Windows exits 0 for "Destination host unreachable"; a real reply has TTL.
    ok = 'ttl=' in out.lower()
    match = re.search(r'time[=<]\s*([\d.]+)\s*ms', out, re.I)
    return ok, (float(match.group(1)) if match else _ms(started)) if ok else None


def _classify(exc):
    # Same short causes the scanner records with failed polls.
    import scanner
    return scanner.fetch_fail_reason(exc)


def measure():
    """Measure the path to the starting node now. Never raises."""
    url, host, port = _target()
    m = {'starting_node': url, 'host': host, 'ok': 0, 'error_class': None, 'error': None,
         'dns_ms': None, 'connect_ms': None, 'http_ms': None, 'node_name': None,
         'lan_gateway': None, 'lan_ok': None, 'lan_ms': None}
    try:
        address = host
        if host and not _is_ip(host):
            started = time.perf_counter()
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
                address = infos[0][4][0]
                m['dns_ms'] = _ms(started)
            except OSError as exc:
                m['dns_ms'] = _ms(started)
                m.update(error_class='dns', error=str(exc)[:200])
        if not m['error_class']:
            started = time.perf_counter()
            try:
                socket.create_connection((address, port), timeout=min(CONNECT_TIMEOUT, config.REQUEST_TIMEOUT)).close()
                m['connect_ms'] = _ms(started)
            except socket.timeout as exc:
                m.update(error_class='connect_timeout', error=str(exc)[:200] or 'timed out')
            except OSError as exc:
                text = str(exc).lower()
                cls = ('refused' if 'refused' in text or '10061' in text else
                       'no_route' if any(s in text for s in ('unreachable', 'no route', '10065', '10051')) else
                       'connect_timeout' if 'timed out' in text or '10060' in text else 'connection_error')
                m.update(error_class=cls, error=str(exc)[:200])
        if not m['error_class']:
            started = time.perf_counter()
            try:
                resp = requests.get(f"http://{host}:{port}/cgi-bin/sysinfo.json" if port != 80
                                    else f"http://{host}/cgi-bin/sysinfo.json", timeout=config.REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                m['http_ms'] = _ms(started)
                m['node_name'] = (data.get('node') or '').lower() or None
                m['ok'] = 1
            except ValueError as exc:
                m.update(error_class='bad_json', error=str(exc)[:200])
            except requests.RequestException as exc:
                cls = _classify(exc)
                m.update(error_class='http_error' if cls.startswith('http_') else cls, error=str(exc)[:200])
        if not m['ok']:
            # Only on failure: is this PC still on its LAN?
            gw = default_gateway()
            m['lan_gateway'] = gw
            if gw:
                ok, ms = ping_once(gw)
                m['lan_ok'], m['lan_ms'] = int(ok), ms
    except Exception as exc:
        logger.warning("Starting-node measurement failed: %s", exc)
        m.update(error_class=m['error_class'] or 'connection_error', error=str(exc)[:200])
    return m


def record(kind='scan', result=None, scan_ms=None, error=None):
    """Measure the starting node and store one row (with the scan's outcome
    when `result` is given). Returns the stored row dict. Never raises."""
    try:
        ensure_tables()
        m = measure()
        result = result or {}
        aborted = bool(result.get('aborted')) if result or error else None
        row = {
            'timestamp': database.local_timestamp(), 'kind': kind, **m,
            'scan_aborted': (1 if error else int(aborted)) if aborted is not None else None,
            'abort_reason': ('scan_error: ' + str(error)[:180]) if error else
                            ('gateway_lost' if aborted else None),
            'scan_ms': scan_ms,
            'nodes_attempted': result.get('node_count_attempted', result.get('nodes_visited')),
            'nodes_reachable': result.get('node_count_reachable', result.get('nodes_found')),
            'selected_scan': int(bool(result.get('selected_scan'))) if result else None,
        }
        cols = list(row)
        with database.get_connection() as conn:
            conn.execute(f"INSERT INTO collector_health ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [row[c] for c in cols])
            cutoff = (datetime.now() - timedelta(hours=config.NODE_HEALTH_RETENTION_HOURS)).strftime(TS_FMT)
            conn.execute('DELETE FROM collector_health WHERE timestamp < ?', (cutoff,))
        return row
    except Exception as exc:
        logger.warning("Collector health record failed: %s", exc)
        return None


# ------------------------------------------------------------- report -----

def _parse(ts):
    return datetime.strptime(ts[:19], TS_FMT)


def _pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def _row_failed(r):
    return not r['ok'] or bool(r['scan_aborted'])


def _row_cause(r):
    if r['lan_ok'] == 0:
        return 'lan_down'
    if r['ok'] and r['scan_aborted']:
        return 'scan_error' if (r['abort_reason'] or '').startswith('scan_error') else 'lost_mid_scan'
    return r['error_class'] or 'unknown'


def _outages_from_rows(rows):
    """Contiguous failed rows -> outages, ended by the next good row."""
    outages, cur = [], None
    for r in rows:
        if _row_failed(r):
            if cur is None:
                cur = {'start': r['timestamp'], 'end': None, 'causes': {}, 'lost_scans': 0,
                       'lan_down': False, 'sources': ['collector_health']}
            cause = _row_cause(r)
            cur['causes'][cause] = cur['causes'].get(cause, 0) + 1
            cur['lost_scans'] += 1 if r['scan_aborted'] else 0
            cur['lan_down'] = cur['lan_down'] or r['lan_ok'] == 0
        elif cur is not None:
            cur['end'] = r['timestamp']
            outages.append(cur)
            cur = None
    if cur is not None:
        outages.append(cur)
    return outages


def _legacy_outages(conn, start, end, seed_slug):
    """Outages from before collector_health: gateway events, plus runs of failed
    polls the older scanner stored under the starting node's URL slug."""
    import unreachable
    outages = [{'start': a, 'end': b, 'causes': {}, 'lost_scans': 0, 'lan_down': False,
                'sources': ['gateway_events']}
               for a, b in unreachable.gateway_outages(conn, start, end)]
    if not seed_slug:
        return outages
    known = conn.execute('SELECT 1 FROM nodes WHERE name = ?', (seed_slug,)).fetchone()
    if known:
        return outages  # the slug is a real node name; can't tell seed polls apart
    margin = (_parse(start) - timedelta(hours=12)).strftime(TS_FMT)
    fails = conn.execute('''
        SELECT timestamp, response_ms, fail_reason FROM node_health_history
        WHERE node_name = ? AND reachable = 0 AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    ''', (seed_slug, margin, end)).fetchall()

    def first_success_after(ts):
        # Any successful scanner poll means the scan got onto the mesh again.
        row = conn.execute('''
            SELECT MIN(timestamp) FROM node_health_history
            WHERE timestamp > ? AND reachable = 1 AND probe_via IS NULL
        ''', (ts,)).fetchone()
        return row[0] if row else None

    timeout_ms = config.REQUEST_TIMEOUT * 1000 * 0.9
    cur, last_fail, next_ok = None, None, None
    for r in fails:
        ts = r['timestamp']
        if cur is not None and ((next_ok and ts > next_ok) or
                                (_parse(ts) - _parse(last_fail)).total_seconds() > LEGACY_GAP_SECONDS):
            cur['end'] = next_ok if next_ok and next_ok < ts else last_fail
            outages.append(cur)
            cur = None
        if cur is None:
            cur = {'start': ts, 'end': None, 'causes': {}, 'lost_scans': 0, 'lan_down': False,
                   'sources': ['legacy_polls']}
            # The first success after the run's first failure is also the first
            # after any later failure that comes before it, so one query per run.
            next_ok = first_success_after(ts)
        cause = r['fail_reason'] or ('no_answer' if (r['response_ms'] or 0) >= timeout_ms else 'unknown')
        cause = 'http_error' if cause.startswith('http_') else cause
        cur['causes'][cause] = cur['causes'].get(cause, 0) + 1
        cur['lost_scans'] += 1
        last_fail = ts
    if cur is not None:
        cur['end'] = next_ok
        outages.append(cur)
    return outages


def _merge(outages, start, end):
    """Merge overlapping outages from different sources; clip to the window."""
    out = []
    for o in sorted(outages, key=lambda o: o['start']):
        if out and o['start'] <= (out[-1]['end'] or '9999'):
            m = out[-1]
            if m['end'] is not None and (o['end'] is None or o['end'] > m['end']):
                m['end'] = o['end']
            for k, v in o['causes'].items():
                m['causes'][k] = m['causes'].get(k, 0) + v
            m['lost_scans'] += o['lost_scans']
            m['lan_down'] = m['lan_down'] or o['lan_down']
            m['sources'] = sorted(set(m['sources']) | set(o['sources']))
        else:
            out.append(dict(o, causes=dict(o['causes'])))
    now = datetime.now().strftime(TS_FMT)
    result = []
    for o in out:
        if (o['end'] or now) < start or o['start'] >= end:
            continue
        o_end = o['end'] or min(now, end)
        cause = max(o['causes'], key=o['causes'].get) if o['causes'] else 'unknown'
        if o['lan_down']:
            cause = 'lan_down'
        in_window = min(_parse(o_end), _parse(end)) - max(_parse(o['start']), _parse(start))
        result.append({
            'start': o['start'], 'end': o['end'], 'ongoing': o['end'] is None,
            'duration_min': round((_parse(o_end) - _parse(o['start'])).total_seconds() / 60, 1),
            'window_min': round(max(0, in_window.total_seconds()) / 60, 1),
            'cause': cause, 'cause_label': CAUSE_LABELS.get(cause, cause),
            'causes': o['causes'], 'lost_scans': o['lost_scans'], 'lan_down': o['lan_down'],
            'sources': o['sources'],
        })
    return result


def _hour_profile(outages, start, end):
    """Outage minutes per hour of day (0-23) inside [start, end)."""
    minutes = [0.0] * 24
    w_start, w_end = _parse(start), _parse(end)
    for o in outages:
        a = max(_parse(o['start']), w_start)
        b = min(_parse(o['end']) if o['end'] else datetime.now(), w_end)
        while a < b:
            nxt = min(b, a.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            minutes[a.hour] += (nxt - a).total_seconds() / 60
            a = nxt
    return [round(m, 1) for m in minutes]


def report(start, end):
    """Starting-node health for [start, end) ('YYYY-MM-DD HH:MM:SS' local)."""
    import reports
    ensure_tables()
    url, host, _ = _target()
    seed_slug = observations.target_from_url(url) if url else None
    bucket = reports.bucket_seconds(_parse(start), _parse(end))
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM collector_health WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (start, end)).fetchall()]
        # The row before the window decides whether the window opens mid-outage.
        before = conn.execute('SELECT * FROM collector_health WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1',
                              (start,)).fetchone()
        # ...and the first good row after it whether it ends inside an outage.
        after = conn.execute('SELECT * FROM collector_health WHERE timestamp >= ? AND ok = 1 '
                             'AND COALESCE(scan_aborted, 0) = 0 ORDER BY timestamp LIMIT 1', (end,)).fetchone()
        latest = conn.execute('SELECT * FROM collector_health ORDER BY timestamp DESC LIMIT 1').fetchone()
        last_ok = conn.execute('SELECT timestamp, http_ms, dns_ms, node_name FROM collector_health '
                               'WHERE ok = 1 AND COALESCE(scan_aborted, 0) = 0 ORDER BY timestamp DESC LIMIT 1').fetchone()
        last_fail = conn.execute('SELECT timestamp, error_class FROM collector_health '
                                 'WHERE ok = 0 OR scan_aborted = 1 ORDER BY timestamp DESC LIMIT 1').fetchone()
        first_row = conn.execute('SELECT MIN(timestamp) FROM collector_health').fetchone()[0]
        legacy = _legacy_outages(conn, start, end, seed_slug)
        # Scanner polls per bucket (new and old code): shows the mesh vanishing
        # from view during an outage on our side.
        polls = conn.execute(f'''
            SELECT {reports._bucket_expr()} AS b, COUNT(*) AS polls, SUM(reachable) AS ok
            FROM node_health_history
            WHERE timestamp >= ? AND timestamp < ? AND probe_via IS NULL
            GROUP BY b ORDER BY b
        ''', (start, bucket, start, end)).fetchall()

    seq = ([dict(before)] if before else []) + rows + ([dict(after)] if after else [])
    outages = _merge(_outages_from_rows(seq) + legacy, start, end)

    # Bucketed series of the measurements.
    w_start = _parse(start)
    buckets = {}
    for r in rows:
        b = int((_parse(r['timestamp']) - w_start).total_seconds() // bucket)
        s = buckets.setdefault(b, {'http': [], 'dns': [], 'connect': [], 'checks': 0, 'failed': 0,
                                   'aborted': 0, 'lan_down': 0})
        s['checks'] += 1
        s['failed'] += 1 if _row_failed(r) else 0
        s['aborted'] += 1 if r['scan_aborted'] else 0
        s['lan_down'] += 1 if r['lan_ok'] == 0 else 0
        for key, col in (('http', 'http_ms'), ('dns', 'dns_ms'), ('connect', 'connect_ms')):
            if r[col] is not None:
                s[key].append(r[col])
    poll_map = {p['b']: p for p in polls}
    series = []
    for b in sorted(set(buckets) | set(poll_map)):
        s = buckets.get(b, {})
        p = poll_map.get(b)
        avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        series.append({
            't': (w_start + timedelta(seconds=b * bucket)).strftime(TS_FMT),
            'http_ms': avg(s.get('http', [])), 'dns_ms': avg(s.get('dns', [])),
            'connect_ms': avg(s.get('connect', [])),
            'checks': s.get('checks', 0), 'failed': s.get('failed', 0), 'aborted': s.get('aborted', 0),
            'lan_down': s.get('lan_down', 0),
            'polls': p['polls'] if p else 0, 'polls_ok': (p['ok'] or 0) if p else 0,
        })

    window_min = (_parse(end) - w_start).total_seconds() / 60
    now = datetime.now().strftime(TS_FMT)
    covered_min = max(0.0, (min(_parse(end), _parse(now)) - w_start).total_seconds() / 60)
    outage_min = sum(o['window_min'] for o in outages)
    gw_ok = gateway._state['ok']
    gw_age = time.monotonic() - gateway._state['checked_at'] if gateway._state['checked_at'] else None
    if latest is not None:
        state = 'down' if _row_failed(latest) else 'up'
    elif gw_age is not None:
        state = 'up' if gw_ok else 'down'
    else:
        state = 'unknown'
    ok_rows = [r for r in rows if r['ok']]
    summary = {
        'state': state,
        'state_cause': CAUSE_LABELS.get(_row_cause(latest)) if latest is not None and state == 'down' else None,
        'checked_at': latest['timestamp'] if latest is not None else None,
        'last_success': last_ok['timestamp'] if last_ok else None,
        'last_failure': last_fail['timestamp'] if last_fail else None,
        'starting_node': url, 'host': host,
        'node_name': (last_ok['node_name'] if last_ok else None),
        'latest_http_ms': latest['http_ms'] if latest is not None else None,
        'latest_dns_ms': latest['dns_ms'] if latest is not None else None,
        'latest_connect_ms': latest['connect_ms'] if latest is not None else None,
        'gate_ok': gw_ok if gw_age is not None else None,
        'gate_age_s': round(gw_age) if gw_age is not None else None,
        'checks': len(rows),
        'scans': sum(1 for r in rows if r['scan_aborted'] is not None),
        'lost_scans': sum(o['lost_scans'] for o in outages),
        'aborted_scans': sum(1 for r in rows if r['scan_aborted']),
        'lan_failures': sum(1 for r in rows if r['lan_ok'] == 0),
        'outages': len(outages),
        'outage_minutes': round(outage_min, 1),
        'longest_outage_min': max((o['duration_min'] for o in outages), default=0),
        'availability_pct': round(100 * (1 - outage_min / covered_min), 2) if covered_min else None,
        'http_p50': _pct([r['http_ms'] for r in ok_rows], 50),
        'http_p95': _pct([r['http_ms'] for r in ok_rows], 95),
        'dns_p50': _pct([r['dns_ms'] for r in rows], 50),
        'dns_p95': _pct([r['dns_ms'] for r in rows], 95),
        'connect_p50': _pct([r['connect_ms'] for r in rows], 50),
        'connect_p95': _pct([r['connect_ms'] for r in rows], 95),
        'scan_ms_p50': _pct([r['scan_ms'] for r in rows if not r['scan_aborted']], 50),
        'dns_applicable': bool(host) and not _is_ip(host),
        'recording_since': first_row,
    }
    return {
        'window': {'start': start, 'end': end, 'bucket_seconds': bucket, 'minutes': round(window_min)},
        'summary': summary,
        'outages': list(reversed(outages)),
        'series': series,
        'hour_profile': _hour_profile(outages, start, end),
        'recent': [dict(r, failed=_row_failed(r), cause=_row_cause(r) if _row_failed(r) else None)
                   for r in rows[-50:]][::-1],
        'cause_labels': CAUSE_LABELS,
    }

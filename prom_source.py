"""
Extra node details from Prometheus (TODO #63-68). Our own polling is
unchanged; this only adds what AREDN's metrics page (`/cgi-bin/metrics`,
firmware 3.24.4.0+) has and sysinfo.json doesn't, read from a Prometheus that
already scrapes it (PROMETHEUS_URL, e.g. W0GSK's at w0gsk-ubuntu:9090).

Prometheus labels each node's series with the scrape address (`instance`),
not its mesh name; `node_aredn_info{node}` (else `node_uname_info{nodename}`)
maps an instance to the node name (lower case, like our nodes table). A node
scraped by two jobs is read from the first job only. Wireless stations
(`node_wifi_station_*{mac}`) are named from the same node's LQM trackers
(`node_lqm_tracker_*{mac, hostname}`).

- `node_details(name)`: read on demand for the node page (memory, CPU,
  flash, processes, Babel routes/neighbours, interface traffic, per-station
  MCS and signal, LQM ping quality, 24 h series). Never raises: an
  unreachable Prometheus or an unscraped node is reported as such.
- `snapshot()`: every PROMETHEUS_POLL_MINUTES (own job) stores the same
  figures for every scraped node in SQLite (`prom_node_samples`,
  `prom_iface_samples`, `prom_station_samples`, `prom_lqm_samples`), kept
  HISTORY_RETENTION_HOURS, then runs prom_watch (alarms).
  Mesh Health, Capacity and RF Stats read these tables, so they work offline
  from the last samples.

The exporter has no conntrack or open-file metrics; those are listed as
`missing` rather than guessed.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta

import requests

import config
import database

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
RATE = '5m'
MISSING = ['conntrack entries / limit', 'open file descriptors']
INSTANCE_TTL = 300
DETAIL_TTL = 60

_tables_ready = False
_lock = threading.Lock()
_instances = {'at': 0, 'map': {}, 'info': {}}
_detail_cache = {}
state = {'last_snapshot': None, 'last_error': None, 'nodes': 0}


class PromError(Exception):
    pass


def enabled():
    return bool((config.PROMETHEUS_URL or '').strip())


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS prom_node_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL,
                mem_total REAL, mem_avail REAL, cpu_busy REAL, load1 REAL,
                flash_avail REAL, flash_size REAL, tmp_avail REAL, tmp_size REAL,
                procs INTEGER, procs_blocked INTEGER, routes INTEGER, aredn_routes INTEGER,
                neighbours INTEGER, boot_time REAL,
                PRIMARY KEY (node_name, timestamp));
            CREATE TABLE IF NOT EXISTS prom_iface_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, kind TEXT,
                rx_bps REAL, tx_bps REAL, rx_pps REAL, tx_pps REAL,
                drops_ps REAL, errors_ps REAL, fifo_ps REAL, frame_ps REAL,
                PRIMARY KEY (node_name, timestamp, iface));
            CREATE TABLE IF NOT EXISTS prom_station_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, mac TEXT NOT NULL, peer TEXT,
                tx_mcs REAL, rx_mcs REAL, tx_rate REAL, rx_rate REAL, signal REAL,
                tx_pps REAL, rx_pps REAL,
                PRIMARY KEY (node_name, timestamp, mac));
            CREATE INDEX IF NOT EXISTS idx_prom_station_peer ON prom_station_samples (node_name, peer, timestamp);
            CREATE TABLE IF NOT EXISTS prom_lqm_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, peer TEXT NOT NULL, link_type TEXT,
                ping_quality REAL, ping_ms REAL, snr REAL, rev_snr REAL, quality REAL,
                PRIMARY KEY (node_name, timestamp, peer));
            CREATE INDEX IF NOT EXISTS idx_prom_lqm_peer ON prom_lqm_samples (node_name, peer, timestamp);
        ''')
    _tables_ready = True


# ---------------------------------------------------------------- HTTP

def _get(path, params):
    if not enabled():
        raise PromError('PROMETHEUS_URL is not set')
    url = config.PROMETHEUS_URL.rstrip('/') + path
    try:
        resp = requests.get(url, params=params, timeout=config.PROMETHEUS_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        raise PromError(f'Prometheus unreachable ({type(exc).__name__})') from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise PromError(f'Prometheus answered HTTP {resp.status_code} without JSON') from exc
    if body.get('status') != 'success':
        raise PromError(body.get('error') or f'Prometheus HTTP {resp.status_code}')
    return body['data']


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float('inf'), float('-inf')) else f


def query(expr):
    """Instant query -> [{'metric': labels, 'value': float}]."""
    data = _get('/api/v1/query', {'query': expr})
    return [{'metric': r['metric'], 'value': _float(r['value'][1])} for r in data.get('result', [])]


def query_range(expr, start, end, step):
    data = _get('/api/v1/query_range', {'query': expr, 'start': start, 'end': end, 'step': step})
    return [{'metric': r['metric'], 'values': [(float(t), _float(v)) for t, v in r['values']]}
            for r in data.get('result', [])]


# ---------------------------------------------------------------- names

def instances(force=False):
    """{(instance, job): node name} for one job per instance, plus info labels."""
    now = time.time()
    if not force and _instances['map'] and now - _instances['at'] < INSTANCE_TTL:
        return _instances['map'], _instances['info']
    mapping, info, taken = {}, {}, set()
    for metric, key in (('node_aredn_info', 'node'), ('node_uname_info', 'nodename')):
        for r in query(metric):
            m = r['metric']
            inst, name = m.get('instance'), (m.get(key) or '').strip().lower()
            if not inst or not name or inst in taken:
                continue
            taken.add(inst)
            mapping[(inst, m.get('job'))] = name
            info[name] = {k: v for k, v in m.items() if k not in ('__name__',)}
    _instances.update(at=now, map=mapping, info=info)
    return mapping, info


def _rows(results, mapping):
    """Results -> [(node, labels, value)] for the chosen job of each instance."""
    out = []
    for r in results:
        m = r['metric']
        node = mapping.get((m.get('instance'), m.get('job')))
        if node:
            out.append((node, m, r['value']))
    return out


def iface_kind(dev):
    dev = dev or ''
    if dev.startswith('wlan'):
        return 'rf'
    if dev == 'br-dtdlink':
        return 'dtd'
    if dev.startswith(('wg', 'tun')):
        return 'tunnel'
    if dev == 'br-wan':
        return 'wan'
    if dev == 'br-lan':
        return 'lan'
    if re.match(r'^eth\d+$', dev):
        return 'eth'
    return None  # lo, br0, vlans: duplicates of the bridges above


NODE_QUERIES = {
    'mem_total': 'node_memory_MemTotal_bytes',
    'mem_avail': 'node_memory_MemAvailable_bytes',
    'cpu_busy': f'100 * (1 - avg by (instance, job) (rate(node_cpu_seconds_total{{mode="idle"}}[{RATE}])))',
    'load1': 'node_load1',
    'flash_avail': 'max by (instance, job) (node_filesystem_avail_bytes{mountpoint=~"/overlay|/"})',
    'flash_size': 'max by (instance, job) (node_filesystem_size_bytes{mountpoint=~"/overlay|/"})',
    'tmp_avail': 'node_filesystem_avail_bytes{mountpoint="/tmp"}',
    'tmp_size': 'node_filesystem_size_bytes{mountpoint="/tmp"}',
    'procs': 'count by (instance, job) (node_process_name)',
    'procs_blocked': 'node_procs_blocked',
    'routes': 'node_babel_route_total',
    'aredn_routes': 'node_arednlink_routes_count_total',
    'neighbours': 'node_babel_neighbor_total',
    'boot_time': 'node_boot_time_seconds',
}
IFACE_QUERIES = {
    'rx_bps': f'8 * rate(node_network_receive_bytes_total[{RATE}])',
    'tx_bps': f'8 * rate(node_network_transmit_bytes_total[{RATE}])',
    'rx_pps': f'rate(node_network_receive_packets_total[{RATE}])',
    'tx_pps': f'rate(node_network_transmit_packets_total[{RATE}])',
    'drops_ps': f'rate(node_network_receive_drop_total[{RATE}]) + rate(node_network_transmit_drop_total[{RATE}])',
    'errors_ps': f'rate(node_network_receive_errors_total[{RATE}]) + rate(node_network_transmit_errors_total[{RATE}])',
    'fifo_ps': f'rate(node_network_receive_fifo_errors_total[{RATE}]) + rate(node_network_transmit_fifo_errors_total[{RATE}])',
    'frame_ps': f'rate(node_network_receive_frame_errors_total[{RATE}])',
}
STATION_QUERIES = {
    'tx_mcs': 'node_wifi_station_transmit_mcs',
    'rx_mcs': 'node_wifi_station_receive_mcs',
    'tx_rate': 'node_wifi_station_transmit_rate_bits_per_second',
    'rx_rate': 'node_wifi_station_receive_rate_bits_per_second',
    'signal': 'node_wifi_station_signal',
    'tx_pps': f'rate(node_wifi_station_transmit_packets_total[{RATE}])',
    'rx_pps': f'rate(node_wifi_station_receive_packets_total[{RATE}])',
}
LQM_QUERIES = {
    'ping_quality': 'node_lqm_tracker_ping_quality',
    'ping_ms': '1000 * node_lqm_tracker_ping_success_time',
    'snr': 'node_lqm_tracker_snr',
    'rev_snr': 'node_lqm_tracker_rev_snr',
    'quality': 'node_lqm_tracker_quality',
}


def collect():
    """Every scraped node's current figures:
    {node: {'node': {...}, 'ifaces': {dev: {...}}, 'stations': {mac: {...}}, 'lqm': {peer: {...}}}}."""
    mapping, info = instances()
    nodes = {}

    def slot(n):
        return nodes.setdefault(n, {'node': {}, 'ifaces': {}, 'stations': {}, 'lqm': {}, 'info': info.get(n, {})})

    for field, expr in NODE_QUERIES.items():
        for n, m, v in _rows(query(expr), mapping):
            slot(n)['node'][field] = v
    for field, expr in IFACE_QUERIES.items():
        for n, m, v in _rows(query(expr), mapping):
            dev = m.get('device')
            kind = iface_kind(dev)
            if kind:
                slot(n)['ifaces'].setdefault(dev, {'kind': kind})[field] = v
    macs = {}
    for field, expr in LQM_QUERIES.items():
        for n, m, v in _rows(query(expr), mapping):
            peer = (m.get('hostname') or '').strip().lower()
            if not peer:
                continue
            row = slot(n)['lqm'].setdefault(peer, {'link_type': m.get('type'), 'mac': m.get('mac')})
            row[field] = v
            if m.get('mac'):
                macs[(n, m['mac'].lower())] = peer
    for field, expr in STATION_QUERIES.items():
        for n, m, v in _rows(query(expr), mapping):
            mac = (m.get('mac') or '').lower()
            if not mac:
                continue
            row = slot(n)['stations'].setdefault(mac, {'peer': macs.get((n, mac)), 'device': m.get('device')})
            row[field] = v
    for n in nodes.values():
        rf = n['node']
        if rf.get('mem_total') and rf.get('mem_avail') is not None:
            rf['mem_used_pct'] = round(100 * (1 - rf['mem_avail'] / rf['mem_total']), 1)
        if rf.get('flash_size') and rf.get('flash_avail') is not None:
            rf['flash_used_pct'] = round(100 * (1 - rf['flash_avail'] / rf['flash_size']), 1)
        if rf.get('boot_time'):
            rf['uptime_s'] = int(time.time() - rf['boot_time'])
    return nodes


# ---------------------------------------------------------------- node page

def _series(expr, hours):
    end = time.time()
    start = end - hours * 3600
    step = max(60, int(hours * 3600 / 300))
    out = query_range(expr, start, end, step)
    if not out:
        return []
    return [{'t': datetime.fromtimestamp(t).strftime(TS_FMT), 'v': None if v is None else round(v, 3)}
            for t, v in out[0]['values']]


def node_details(name, hours=24):
    """On-demand extra details for one node (node page). Never raises."""
    name = (name or '').strip().lower()
    out = {'node': name, 'enabled': enabled(), 'available': False, 'scraped': False,
           'source': config.PROMETHEUS_URL, 'grafana_url': config.GRAFANA_URL or None,
           'missing': MISSING, 'hours': hours}
    if not enabled():
        out['error'] = 'Prometheus is not configured (PROMETHEUS_URL).'
        return out
    cached = _detail_cache.get((name, hours))
    if cached and time.time() - cached[0] < DETAIL_TTL:
        return cached[1]
    try:
        everything = collect()
        out['available'] = True
        mine = everything.get(name)
        out['scraped_nodes'] = sorted(everything)
        if mine:
            out['scraped'] = True
            out['info'] = mine['info']
            out['health'] = mine['node']
            out['ifaces'] = [dict(v, iface=k) for k, v in sorted(mine['ifaces'].items(),
                                                               key=lambda kv: (kv[1]['kind'], kv[0]))]
            out['stations'] = [dict(v, mac=k) for k, v in sorted(mine['stations'].items())]
            out['lqm'] = [dict(v, peer=k) for k, v in sorted(mine['lqm'].items())]
            mapping, _ = instances()
            inst = next(((i, j) for (i, j), n in mapping.items() if n == name), None)
            if inst:
                sel = f'instance="{inst[0]}",job="{inst[1]}"'
                out['series'] = {
                    'mem_avail_mb': _series(f'node_memory_MemAvailable_bytes{{{sel}}} / 1048576', hours),
                    'cpu_busy': _series(f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{sel}}}[{RATE}])))', hours),
                    'routes': _series(f'node_babel_route_total{{{sel}}}', hours),
                    'neighbours': _series(f'node_babel_neighbor_total{{{sel}}}', hours),
                    'rf_rx_mbps': _series(f'sum(8 * rate(node_network_receive_bytes_total{{device=~"wlan.*",{sel}}}[{RATE}])) / 1e6', hours),
                    'rf_tx_mbps': _series(f'sum(8 * rate(node_network_transmit_bytes_total{{device=~"wlan.*",{sel}}}[{RATE}])) / 1e6', hours),
                }
    except PromError as exc:
        out['error'] = str(exc)
    except Exception as exc:  # never break the node page
        logger.warning("Prometheus details for %s failed: %s", name, exc)
        out['error'] = f'{type(exc).__name__}: {exc}'
    _detail_cache[(name, hours)] = (time.time(), out)
    return out


# ---------------------------------------------------------------- snapshot job

def store(nodes, now=None):
    """Write one collect() result to the sample tables."""
    ensure_tables()
    ts = (now or datetime.now()).strftime(TS_FMT)
    node_rows, iface_rows, sta_rows, lqm_rows = [], [], [], []
    for name, d in nodes.items():
        h = d['node']
        node_rows.append((name, ts, h.get('mem_total'), h.get('mem_avail'), h.get('cpu_busy'), h.get('load1'),
                          h.get('flash_avail'), h.get('flash_size'), h.get('tmp_avail'), h.get('tmp_size'),
                          h.get('procs'), h.get('procs_blocked'), h.get('routes'), h.get('aredn_routes'),
                          h.get('neighbours'), h.get('boot_time')))
        tunnels = {}
        for dev, f in d['ifaces'].items():
            if f['kind'] == 'tunnel':
                for k, v in f.items():
                    if k != 'kind' and v is not None:
                        tunnels[k] = tunnels.get(k, 0.0) + v
                continue
            iface_rows.append((name, ts, dev, f['kind'], f.get('rx_bps'), f.get('tx_bps'), f.get('rx_pps'),
                               f.get('tx_pps'), f.get('drops_ps'), f.get('errors_ps'), f.get('fifo_ps'),
                               f.get('frame_ps')))
        if tunnels:
            iface_rows.append((name, ts, 'tunnels', 'tunnel', tunnels.get('rx_bps'), tunnels.get('tx_bps'),
                               tunnels.get('rx_pps'), tunnels.get('tx_pps'), tunnels.get('drops_ps'),
                               tunnels.get('errors_ps'), tunnels.get('fifo_ps'), tunnels.get('frame_ps')))
        for mac, s in d['stations'].items():
            sta_rows.append((name, ts, mac, s.get('peer'), s.get('tx_mcs'), s.get('rx_mcs'), s.get('tx_rate'),
                             s.get('rx_rate'), s.get('signal'), s.get('tx_pps'), s.get('rx_pps')))
        for peer, q in d['lqm'].items():
            lqm_rows.append((name, ts, peer, q.get('link_type'), q.get('ping_quality'), q.get('ping_ms'),
                             q.get('snr'), q.get('rev_snr'), q.get('quality')))
    with database.get_connection() as conn:
        conn.executemany('INSERT OR REPLACE INTO prom_node_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', node_rows)
        conn.executemany('INSERT OR REPLACE INTO prom_iface_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', iface_rows)
        conn.executemany('INSERT OR REPLACE INTO prom_station_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)', sta_rows)
        conn.executemany('INSERT OR REPLACE INTO prom_lqm_samples VALUES (?,?,?,?,?,?,?,?,?)', lqm_rows)
    return ts


def cleanup(now=None):
    ensure_tables()
    cut = ((now or datetime.now()) - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        for t in ('prom_node_samples', 'prom_iface_samples', 'prom_station_samples', 'prom_lqm_samples'):
            conn.execute(f'DELETE FROM {t} WHERE timestamp < ?', (cut,))


def snapshot(now=None):
    """Store every scraped node's figures, then evaluate alarms. Returns
    {'nodes', 'events', 'error'}."""
    if not enabled():
        return {'nodes': 0, 'events': [], 'error': 'disabled'}
    with _lock:
        try:
            nodes = collect()
        except PromError as exc:
            state['last_error'] = str(exc)
            logger.info("Prometheus snapshot skipped: %s", exc)
            return {'nodes': 0, 'events': [], 'error': str(exc)}
        now = now or datetime.now()
        store(nodes, now)
        state.update(last_snapshot=now.strftime(TS_FMT), last_error=None, nodes=len(nodes))
    events = []
    import prom_watch
    try:
        events = prom_watch.evaluate(now=now)
    except Exception as exc:
        logger.warning("Prometheus alarms failed: %s", exc)
    cleanup(now)
    return {'nodes': len(nodes), 'events': events, 'error': None}


def install(scheduler, job):
    if not enabled():
        return
    scheduler.add_job(job, 'interval', minutes=config.PROMETHEUS_POLL_MINUTES, id='prometheus_snapshot',
                      replace_existing=True, next_run_time=datetime.now() + timedelta(minutes=1))


# ---------------------------------------------------------------- readers

def latest_nodes(max_age_minutes=None):
    """Newest stored row per node (within max_age_minutes, default 3 polls)."""
    ensure_tables()
    max_age = max_age_minutes or max(30, 3 * config.PROMETHEUS_POLL_MINUTES)
    since = (datetime.now() - timedelta(minutes=max_age)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return {r['node_name']: dict(r) for r in conn.execute('''
            SELECT p.* FROM prom_node_samples p
            JOIN (SELECT node_name, MAX(timestamp) AS ts FROM prom_node_samples WHERE timestamp >= ?
                  GROUP BY node_name) m ON m.node_name = p.node_name AND m.ts = p.timestamp''', (since,))}


def node_history(name, hours=72):
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM prom_node_samples WHERE node_name = ? AND timestamp >= ? ORDER BY timestamp',
            (name, since))]


def link_series(a, b, hours=24):
    """Per direction (a hearing b, b hearing a): station MCS/rate/signal and
    LQM ping quality/time from the stored Prometheus samples, plus the LQM
    ping quality / RTT our own scans store (`polled`) -- RF Stats, Link Health."""
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    out = {'source': a, 'target': b, 'hours': hours, 'enabled': enabled()}
    with database.get_connection() as conn:
        for key, x, y in (('forward', a, b), ('reverse', b, a)):
            out[key] = {
                'node': x, 'peer': y,
                'stations': [dict(r) for r in conn.execute('''
                    SELECT timestamp, tx_mcs, rx_mcs, tx_rate, rx_rate, signal, tx_pps, rx_pps
                    FROM prom_station_samples WHERE node_name = ? AND peer = ? AND timestamp >= ?
                    ORDER BY timestamp''', (x, y, since))],
                'lqm': [dict(r) for r in conn.execute('''
                    SELECT timestamp, ping_quality, ping_ms, snr, rev_snr, quality
                    FROM prom_lqm_samples WHERE node_name = ? AND peer = ? AND timestamp >= ?
                    ORDER BY timestamp''', (x, y, since))],
                # Our own polls already carry LQM's ping quality / RTT (sysinfo trackers).
                'polled': [dict(r) for r in conn.execute('''
                    SELECT timestamp, ping_quality, lqm_rtt FROM link_history
                    WHERE source_node = ? AND target_node = ? AND timestamp >= ?
                      AND (ping_quality IS NOT NULL OR lqm_rtt IS NOT NULL)
                      AND COALESCE(sample_type, 'scan') = 'scan'
                    ORDER BY timestamp''', (x, y, since))],
            }
    return out


def status():
    ensure_tables()
    with database.get_connection() as conn:
        n = conn.execute('SELECT COUNT(DISTINCT node_name) FROM prom_node_samples').fetchone()[0]
        last = conn.execute('SELECT MAX(timestamp) FROM prom_node_samples').fetchone()[0]
    return {'enabled': enabled(), 'url': config.PROMETHEUS_URL, 'grafana_url': config.GRAFANA_URL,
            'poll_minutes': config.PROMETHEUS_POLL_MINUTES, 'stored_nodes': n, 'last_sample': last,
            'last_error': state['last_error'], 'missing': MISSING}

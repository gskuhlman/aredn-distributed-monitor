"""
Antenna alignment mode (/field/align, phone page): one radio's live view of
its RF neighbours for tower work.

`read(node)` fetches the radio's own sysinfo with LQM only
(`sysinfo.json?lqm=1`, lighter than the scanner's full request) and returns
every RF tracker: SNR, signal, the far end's SNR (rev_snr), LQ, quality,
bitrates. Responses are cached per radio for ALIGN_MIN_INTERVAL_SECONDS so
several phones polling the same radio send it one request. The radio's LQM
refreshes its numbers on its own schedule; the page shows when each value
last changed so the person on the tower knows how fresh it is.
Nothing is stored.
"""

import threading
import time
from datetime import datetime

import requests

import config
import database
import scanner

_cache = {}
_lock = threading.Lock()


def _address(conn, node):
    r = conn.execute('SELECT name, ip FROM nodes WHERE name = ?', (node,)).fetchone()
    if not r:
        return None, None
    return r['name'], f"{r['name']}.local.mesh"


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def parse(data):
    out = []
    trackers = scanner.lqm_trackers(data) or {}
    for mac, t in trackers.items():
        if str(t.get('type') or '').upper() != 'RF':
            continue
        _, hostname, mac_address = scanner.tracker_hostnames(mac, t)
        out.append({'node': hostname, 'mac': mac_address, 'snr': _num(t.get('snr')), 'signal': _num(t.get('signal')),
                    'rev_snr': _num(t.get('rev_snr')), 'lq': _num(t.get('lq')), 'rev_lq': _num(t.get('rev_lq')),
                    'quality': _num(t.get('quality')), 'tx_bitrate': _num(t.get('tx_bitrate')),
                    'rx_bitrate': _num(t.get('rx_bitrate')), 'distance_m': _num(t.get('distance')),
                    'blocked': bool(t.get('blocked') or t.get('user_blocks')),
                    'connected': scanner.tracker_is_connected(t)})
    out.sort(key=lambda x: -(x['snr'] or -999))
    return out


def read(node):
    with database.get_connection() as conn:
        name, host = _address(conn, node)
    if not name:
        return {'error': f'unknown node: {node}'}, 404
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < config.ALIGN_MIN_INTERVAL_SECONDS:
            return hit[1], 200
    url = f'http://{host}/cgi-bin/sysinfo.json?lqm=1'
    started = time.monotonic()
    try:
        resp = requests.get(url, timeout=config.ALIGN_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {'node': name, 'error': f'{type(exc).__name__}: could not read the radio',
                'at': datetime.now().strftime('%H:%M:%S')}, 502
    meshrf = data.get('meshrf') or {}
    out = {'node': name, 'at': datetime.now().strftime('%H:%M:%S'),
           'response_ms': int((time.monotonic() - started) * 1000),
           'frequency': meshrf.get('freq'), 'chanbw': meshrf.get('chanbw'), 'neighbours': parse(data)}
    with _lock:
        _cache[name] = (time.monotonic(), out)
    return out, 200

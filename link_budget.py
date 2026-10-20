"""
Link budget + terrain (RF Stats pair diagnosis "Link budget", Diagnostics
"Link Budget" for infrastructure links).

For an RF pair, predicts the received signal each way and compares it with
what the radios measure (`link_history.signal`, median over `hours`):

  predicted = TX power + TX gain - TX off-axis loss - FSPL
              - obstruction loss + RX gain - RX off-axis loss - cable loss

  - TX power: `nodes.rf_txpower` when a firmware reports it, else
    LINK_BUDGET_TX_DBM (AREDN sysinfo has no TX power: an assumption);
  - gains, beamwidth, azimuth, height from the node's stored antenna
    settings; a gain or height that isn't set gets a default (flagged);
  - off-axis loss from the angle between the antenna's azimuth and the far
    end: 12 (angle / beamwidth)^2 dB, at most 25 dB (horizontal only);
  - FSPL = 20 log10(km) + 20 log10(MHz) + 32.44;
  - terrain: an elevation profile between the ends (Open-Meteo elevation
    API, Copernicus 90 m DEM, cached in SQLite `elevation_cache`; set
    ELEVATION_ENABLED=0 to stay offline), earth bulge with k = 4/3, first
    Fresnel zone clearance at every sample and ITU-R P.526 single knife-edge
    loss at the worst point. Buildings and trees are not in the DEM.
Predicted SNR uses the measured noise (signal - SNR) when known, else the
thermal floor plus a 6 dB noise figure.

Verdicts compare measured with predicted: much weaker than predicted points at
aim, obstruction (trees/buildings), cable/connector, or a lower TX power;
much stronger means a setting (gain, height, location) is off.
"""

import logging
import math
from datetime import datetime, timedelta
from statistics import median

import requests

import config
import database
import link_diagnostics as ld

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
EARTH_R_M = 6371000.0
K_FACTOR = 4 / 3
CABLE_LOSS_DB = 1.0
NOISE_FIGURE_DB = 6.0
DEFAULT_GAIN_DBI = 15.0
# Built-in antenna gain by model when the node has no gain set (dBi).
MODEL_GAINS = (('hap', 2.5), ('ac2', 2.5), ('ac3', 2.5), ('litebeam', 23.0), ('powerbeam', 25.0),
               ('pb5ac', 25.0), ('sxtsq', 16.0), ('sxt', 16.0), ('nanostation', 16.0), ('nanobeam', 19.0),
               ('lhg', 24.5), ('mantbox', 19.0), ('ldf', 24.5))
DEFAULT_HEIGHT_M = 10.0
MIN_HEIGHT_M = 2.0
PROFILE_POINTS = 64
WEAKER_DB = 10.0           # measured this much below predicted = flagged
STRONGER_DB = 10.0
FRESNEL_OK = 0.6           # 60 % of the first Fresnel zone clear
_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS elevation_cache (
                            lat REAL NOT NULL, lon REAL NOT NULL, elevation REAL,
                            fetched_at TEXT, PRIMARY KEY (lat, lon))''')
    _tables_ready = True


# ------------------------------------------------------------ elevation --

def _key(lat, lon):
    return round(lat, 4), round(lon, 4)


def elevations(points, fetch=None):
    """Ground elevation (m) for [(lat, lon)], from the cache or the API.
    Returns (list with None where unknown, error text or None)."""
    ensure_tables()
    keys = [_key(*p) for p in points]
    with database.get_connection() as conn:
        cached = {}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            q = ' OR '.join(['(lat = ? AND lon = ?)'] * len(chunk))
            for r in conn.execute(f'SELECT lat, lon, elevation FROM elevation_cache WHERE {q}',
                                  [v for k in chunk for v in k]):
                cached[(r['lat'], r['lon'])] = r['elevation']
    missing = sorted({k for k in keys if k not in cached})
    error = None
    if missing and config.ELEVATION_ENABLED:
        got = {}
        try:
            for i in range(0, len(missing), 100):
                chunk = missing[i:i + 100]
                values = (fetch or _fetch)(chunk)
                got.update(zip(chunk, values))
        except Exception as exc:
            error = f'elevation lookup failed: {type(exc).__name__}: {exc}'
            logger.warning(error)
        if got:
            now = datetime.now().strftime(TS_FMT)
            with database.get_connection() as conn:
                conn.executemany('INSERT OR REPLACE INTO elevation_cache (lat, lon, elevation, fetched_at) '
                                 'VALUES (?, ?, ?, ?)', [(k[0], k[1], v, now) for k, v in got.items()])
            cached.update(got)
    elif missing:
        error = 'elevation lookups are off (ELEVATION_ENABLED=0)'
    return [cached.get(k) for k in keys], error


def _fetch(chunk):
    resp = requests.get(config.ELEVATION_API_URL, params={
        'latitude': ','.join(f'{k[0]:.4f}' for k in chunk),
        'longitude': ','.join(f'{k[1]:.4f}' for k in chunk)}, timeout=config.ELEVATION_TIMEOUT_SECONDS,
        headers={'User-Agent': config.WEATHER_USER_AGENT})
    resp.raise_for_status()
    values = resp.json().get('elevation')
    if not isinstance(values, list) or len(values) != len(chunk):
        raise ValueError('unexpected elevation response')
    return [float(v) if v is not None else None for v in values]


# --------------------------------------------------------------- physics --

def fspl_db(distance_m, freq_mhz):
    return 20 * math.log10(max(distance_m, 1) / 1000) + 20 * math.log10(freq_mhz) + 32.44


def off_axis_db(radio, bearing):
    """Horizontal off-boresight loss toward `bearing`, and the angle."""
    az, bw = radio.get('azimuth'), radio.get('beamwidth') or 360
    if az is None or bw >= 360:
        return 0.0, None
    angle = ld._angle_diff(bearing, az)
    return min(25.0, 12 * (angle / bw) ** 2), round(angle, 1)


def knife_edge_db(v):
    """ITU-R P.526 single knife-edge diffraction loss J(v)."""
    if v <= -0.78:
        return 0.0
    return 6.9 + 20 * math.log10(math.sqrt((v - 0.1) ** 2 + 1) + v - 0.1)


def _interp(a, b, t):
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def profile(a, b, freq_mhz, fetch=None):
    """Terrain profile a -> b with line of sight, Fresnel radius and clearance."""
    dist = ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])
    n = max(8, min(PROFILE_POINTS, int(dist / 50) + 2))
    pts = [_interp((a['lat'], a['lon']), (b['lat'], b['lon']), i / (n - 1)) for i in range(n)]
    elev, error = elevations(pts, fetch=fetch)
    if any(e is None for e in elev):
        return {'available': False, 'error': error or 'no elevation data', 'distance_m': round(dist)}
    lam = 299.792458 / freq_mhz
    ha = elev[0] + a['height']
    hb = elev[-1] + b['height']
    rows, worst_ratio, worst_v, worst_at = [], None, -10.0, None
    for i, e in enumerate(elev):
        d1 = dist * i / (n - 1)
        d2 = dist - d1
        bulge = d1 * d2 / (2 * K_FACTOR * EARTH_R_M)
        ground = e + bulge
        los = ha + (hb - ha) * (d1 / dist if dist else 0)
        r1 = math.sqrt(lam * d1 * d2 / dist) if 0 < i < n - 1 else 0.0
        clearance = los - ground
        rows.append({'d': round(d1), 'terrain': round(e, 1), 'ground': round(ground, 1), 'los': round(los, 1),
                     'fresnel': round(r1, 1)})
        if 0 < i < n - 1 and r1 > 0:
            ratio = clearance / r1
            v = -clearance * math.sqrt(2 / lam * (1 / d1 + 1 / d2))
            if worst_ratio is None or ratio < worst_ratio:
                worst_ratio = ratio
            if v > worst_v:
                worst_v, worst_at = v, d1
    return {'available': True, 'error': None, 'distance_m': round(dist), 'points': rows,
            'ends_elevation': [round(elev[0], 1), round(elev[-1], 1)],
            'min_fresnel_clearance': round(worst_ratio, 2) if worst_ratio is not None else None,
            'obstruction_loss_db': round(knife_edge_db(worst_v), 1),
            'worst_at_m': round(worst_at) if worst_at is not None else None,
            'los_blocked': worst_ratio is not None and worst_ratio < 0}


# ------------------------------------------------------------------ data --

def _radio(conn, name):
    r = conn.execute('SELECT * FROM nodes WHERE name = ?', (name,)).fetchone()
    if not r:
        return None
    r = dict(r)
    notes = []
    gain = ld._num(r.get('antenna_gain'))
    assumed = gain is None
    if gain is None:
        model = (r.get('model') or '').lower()
        gain = next((g for key, g in MODEL_GAINS if key in model), DEFAULT_GAIN_DBI)
        notes.append(f'antenna gain not set: {gain:g} dBi assumed'
                     + (f" from the model ({r.get('model')})" if gain != DEFAULT_GAIN_DBI else ''))
    height = ld._num(r.get('antenna_height'))
    if height is None or height < MIN_HEIGHT_M:
        notes.append(f'antenna height {"not set" if height is None else f"set to {height:g} m"}: '
                     f'{DEFAULT_HEIGHT_M:g} m assumed')
        height = DEFAULT_HEIGHT_M
    tx = ld._num(r.get('rf_txpower'))
    tx_assumed = tx is None
    if tx_assumed:
        tx = config.LINK_BUDGET_TX_DBM
    return {'name': name, 'lat': r.get('lat'), 'lon': r.get('lon'), 'gain': gain, 'height': height,
            'azimuth': ld._num(r.get('antenna_azimuth')), 'beamwidth': ld._num(r.get('antenna_beamwidth')),
            'antenna': r.get('antenna_desc'), 'frequency': ld._num(r.get('rf_frequency')),
            'chanbw': ld._num(r.get('rf_chanbw')), 'tx_dbm': tx, 'tx_assumed': tx_assumed, 'notes': notes,
            'gain_assumed': assumed,
            'model': r.get('model')}


def _measured(conn, src, dst, since):
    """Median signal/SNR/noise src hears from dst."""
    rows = conn.execute('''SELECT signal, snr FROM link_history
                           WHERE source_node = ? AND target_node = ? AND timestamp >= ?
                             AND UPPER(link_type) = 'RF' ''', (src, dst, since)).fetchall()
    sig = [r['signal'] for r in rows if r['signal'] is not None]
    snr = [r['snr'] for r in rows if r['snr'] is not None]
    noise = [r['signal'] - r['snr'] for r in rows if r['signal'] is not None and r['snr'] is not None]
    return {'signal': round(median(sig), 1) if sig else None, 'snr': round(median(snr), 1) if snr else None,
            'noise': round(median(noise), 1) if noise else None, 'samples': len(rows)}


def _direction(tx, rx, dist, freq, bw, obstruction, measured):
    brg_tx = ld._bearing(tx['lat'], tx['lon'], rx['lat'], rx['lon'])
    brg_rx = ld._bearing(rx['lat'], rx['lon'], tx['lat'], tx['lon'])
    tx_off, tx_angle = off_axis_db(tx, brg_tx)
    rx_off, rx_angle = off_axis_db(rx, brg_rx)
    fspl = fspl_db(dist, freq)
    predicted = tx['tx_dbm'] + tx['gain'] - tx_off - fspl - obstruction + rx['gain'] - rx_off - 2 * CABLE_LOSS_DB
    thermal = -174 + 10 * math.log10(bw * 1e6) + NOISE_FIGURE_DB
    noise = measured['noise'] if measured['noise'] is not None else thermal
    out = {'from': tx['name'], 'to': rx['name'], 'fspl_db': round(fspl, 1),
           'tx_off_axis_db': round(tx_off, 1), 'tx_angle': tx_angle,
           'rx_off_axis_db': round(rx_off, 1), 'rx_angle': rx_angle,
           'predicted_signal': round(predicted, 1), 'noise': round(noise, 1),
           'noise_measured': measured['noise'] is not None,
           'predicted_snr': round(predicted - noise, 1),
           'measured_signal': measured['signal'], 'measured_snr': measured['snr'], 'samples': measured['samples']}
    out['delta_db'] = round(measured['signal'] - predicted, 1) if measured['signal'] is not None else None
    return out


def budget(a, b, hours=24, fetch=None):
    """Link budget both ways for an RF pair."""
    a, b = a.lower(), b.lower()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        ra, rb = _radio(conn, a), _radio(conn, b)
        if not ra or not rb:
            return {'error': f'{a if not ra else b} is not a known node'}
        missing = [r['name'] for r in (ra, rb) if r['lat'] is None or r['lon'] is None]
        if missing:
            return {'error': f'no location for {", ".join(missing)}'}
        m_ab = _measured(conn, b, a, since)   # b hears a: direction a -> b
        m_ba = _measured(conn, a, b, since)
    freq = ra['frequency'] or rb['frequency']
    if not freq:
        return {'error': 'no frequency known for either radio'}
    bw = ra['chanbw'] or rb['chanbw'] or 20.0
    dist = ld._haversine_m(ra['lat'], ra['lon'], rb['lat'], rb['lon'])
    if dist < 1:
        return {'error': 'both radios are at the same location (colocated): no path to model'}
    terrain = profile(ra, rb, freq, fetch=fetch)
    obstruction = terrain['obstruction_loss_db'] if terrain['available'] else 0.0
    dirs = [_direction(ra, rb, dist, freq, bw, obstruction, m_ab),
            _direction(rb, ra, dist, freq, bw, obstruction, m_ba)]
    findings = []
    for r in (ra, rb):
        for n in r['notes']:
            findings.append({'severity': 'info', 'text': f"{r['name']}: {n}"})
    if ra['tx_assumed'] or rb['tx_assumed']:
        findings.append({'severity': 'info', 'text': f'TX power not reported by AREDN: {config.LINK_BUDGET_TX_DBM:g} dBm '
                                                     'assumed (LINK_BUDGET_TX_DBM); a lower power setting shows as "weaker than predicted"'})
    if terrain['available']:
        c = terrain['min_fresnel_clearance']
        if terrain['los_blocked']:
            findings.append({'severity': 'critical', 'text': f"terrain blocks the line of sight about "
                             f"{terrain['worst_at_m'] / 1000:.1f} km from {a} ({terrain['obstruction_loss_db']} dB diffraction loss)"})
        elif c is not None and c < FRESNEL_OK:
            findings.append({'severity': 'warning', 'text': f"only {round(c * 100)}% of the first Fresnel zone is clear of terrain "
                             f"about {terrain['worst_at_m'] / 1000:.1f} km from {a} (60% wanted); raise an antenna"})
        elif c is not None:
            findings.append({'severity': 'ok', 'text': f"terrain clears the first Fresnel zone ({round(c * 100)}% at the tightest "
                             "point); trees and buildings are not in the elevation data"})
    else:
        findings.append({'severity': 'info', 'text': f"no terrain profile: {terrain['error']}"})
    aimed = set()
    for d in dirs:
        for side, angle, loss in (('from', d['tx_angle'], d['tx_off_axis_db']), ('to', d['rx_angle'], d['rx_off_axis_db'])):
            name = d[side]
            if angle is not None and loss >= 3 and name not in aimed:
                aimed.add(name)
                findings.append({'severity': 'warning', 'text': f"{name} is aimed {angle:g}° off the path (about {loss:g} dB lost); "
                                 'check its azimuth setting or the antenna (Antenna Aim)'})
        if d['delta_db'] is None:
            continue
        # With a guessed gain or height the comparison is only a hint.
        guess = any(r['gain_assumed'] or any('height' in n for n in r['notes']) for r in (ra, rb))
        if d['delta_db'] <= -WEAKER_DB:
            findings.append({'severity': 'info' if guess else 'warning', 'text': f"{d['to']} hears {d['from']} {-d['delta_db']:g} dB weaker than predicted "
                             f"({d['measured_signal']:g} vs {d['predicted_signal']:g} dBm): obstruction (trees, buildings), "
                             'misaim, cable/connector, or a lower TX power'})
        elif d['delta_db'] >= STRONGER_DB:
            findings.append({'severity': 'info', 'text': f"{d['to']} hears {d['from']} {d['delta_db']:g} dB stronger than predicted: "
                             'a gain, height, TX power or location setting is probably off'})
    if all(d['delta_db'] is not None for d in dirs):
        gap = abs(dirs[0]['delta_db'] - dirs[1]['delta_db'])
        if gap >= 8:
            findings.append({'severity': 'warning', 'text': f'the two directions differ by {gap:.0f} dB against the model: '
                             'different TX power settings, or one end receives worse (noise, cable, near-far)'})
    confidence = 'low' if any(r['gain_assumed'] or any('height' in n for n in r['notes']) for r in (ra, rb))         else 'medium' if (ra['tx_assumed'] or rb['tx_assumed']) else 'high'
    return {'a': a, 'b': b, 'hours': hours, 'frequency': freq, 'confidence': confidence, 'chanbw': bw, 'distance_m': round(dist),
            'radios': [ra, rb], 'directions': dirs, 'terrain': terrain, 'findings': findings,
            'assumptions': {'tx_dbm': config.LINK_BUDGET_TX_DBM, 'cable_loss_db': CABLE_LOSS_DB,
                            'noise_figure_db': NOISE_FIGURE_DB, 'k_factor': round(K_FACTOR, 2)}}


def infrastructure_links(hours=24, fetch=None):
    """Budget summary for every current RF link with an infrastructure end."""
    infra = database.get_infrastructure_nodes()
    import path_view
    with database.get_connection() as conn:
        rows = path_view._links(conn)
    pairs = sorted({tuple(sorted((a, b))) for (a, b), r in rows.items()
                    if (r['link_type'] or '').upper() == 'RF' and r['usable'] and not b.startswith('mac-')
                    and not a.startswith('mac-') and (a in infra or b in infra)})
    out = []
    for a, b in pairs:
        r = budget(a, b, hours=hours, fetch=fetch)
        if r.get('error'):
            out.append({'a': a, 'b': b, 'error': r['error']})
            continue
        t = r['terrain']
        out.append({'a': a, 'b': b, 'distance_m': r['distance_m'],
                    'fresnel': t.get('min_fresnel_clearance') if t['available'] else None,
                    'terrain_error': None if t['available'] else t['error'],
                    'directions': [{k: d[k] for k in ('from', 'to', 'predicted_signal', 'measured_signal', 'delta_db')}
                                   for d in r['directions']],
                    'confidence': r['confidence'],
                    'worst': max((f['severity'] for f in r['findings']),
                                 key=lambda s: {'critical': 3, 'warning': 2, 'info': 1, 'ok': 0}[s], default='ok'),
                    'findings': [f for f in r['findings'] if f['severity'] in ('critical', 'warning')]})
    return {'hours': hours, 'links': out}

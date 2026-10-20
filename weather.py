"""
Weather correlation: NWS station observations (api.weather.gov) lined up
with RF link history, to spot rain fade, ice/thermal drift and antennas that
move in the wind.

Observations come from the nearest NWS station to the mesh (or
WEATHER_STATION), polled every WEATHER_POLL_MINUTES in the background and
backfilled WEATHER_BACKFILL_DAYS on first run. They are stored locally in the
SQLite `weather_observations` table (never CouchDB) and kept as long as RF
history. Network failures are expected: they are logged, recorded as the last
error and never touch scans; without data the UI simply shows no weather.

Correlation (per link direction, from that radio's own scan samples):
- rain: median SNR in samples near a wet observation (precipitation in the
  last hour, or rain/drizzle/snow/storms reported) vs. dry ones; a drop of
  RAIN_FADE_DB or more with enough wet samples is flagged;
- wind: SNR spread (per-hour standard deviation) in windy hours (gust or
  speed >= WIND_HIGH_KMH) vs. calm ones, plus the median SNR change;
- temperature: correlation of hourly SNR with temperature (ice and thermal
  drift), only judged over a wide enough temperature range.
"""

import json
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone

import requests

import config
import database
import link_diagnostics as ld

logger = logging.getLogger(__name__)

API = 'https://api.weather.gov'
TS_FMT = '%Y-%m-%d %H:%M:%S'
MATCH_MINUTES = 45          # a link sample uses the nearest observation within this
MAX_PAGES = 12              # observation pages per fetch (about 500 observations)
CENTRE_RADIUS_KM = 60       # located nodes this close to the median set the mesh centre

WET_WORDS = ('rain', 'drizzle', 'snow', 'thunder', 'storm', 'sleet', 'freezing', 'hail', 'showers')
RAIN_FADE_DB = 3.0          # median SNR drop while wet to flag rain fade
MIN_WET_SAMPLES = 6
MIN_WET_HOURS = 2
WIND_HIGH_KMH = 30.0        # gust or sustained speed for a "windy" hour
WIND_CALM_KMH = 15.0
MIN_WIND_HOURS = 6          # windy hours needed before judging wind (afternoon wind and warmth go together)
WIND_SPREAD_RATIO = 1.5     # windy SNR spread vs calm to flag wind sensitivity
WIND_SPREAD_DB = 1.5
TEMP_MIN_RANGE_C = 8.0      # temperature span needed to judge a correlation
TEMP_R = 0.6
MIN_HOURS = 6               # hourly points needed for any correlation

_tables_ready = False
_last = {'fetched_at': None, 'error': None, 'added': None}


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS weather_observations (
                station TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                ts_local TEXT NOT NULL,
                temp_c REAL,
                dewpoint_c REAL,
                humidity REAL,
                wind_kmh REAL,
                gust_kmh REAL,
                wind_dir REAL,
                precip_1h_mm REAL,
                precip_3h_mm REAL,
                precip_6h_mm REAL,
                wet INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                raw TEXT,
                PRIMARY KEY (station, ts_utc)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_local ON weather_observations(ts_local)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS weather_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
    _tables_ready = True


def _meta(key, default=None):
    ensure_tables()
    with database.get_connection() as conn:
        row = conn.execute('SELECT value FROM weather_meta WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def _set_meta(key, value):
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('INSERT INTO weather_meta (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))


# ------------------------------------------------------------- fetching ----

def _get(url, params=None):
    resp = requests.get(url, params=params, timeout=config.WEATHER_TIMEOUT_SECONDS,
                        headers={'User-Agent': config.WEATHER_USER_AGENT, 'Accept': 'application/geo+json'})
    resp.raise_for_status()
    return resp.json()


def mesh_centre():
    """Median position of infrastructure nodes (or of located nodes near the
    overall median when none are marked)."""
    infra = set(database.get_infrastructure_nodes())
    with database.get_connection() as conn:
        rows = conn.execute('SELECT name, lat, lon FROM nodes WHERE lat IS NOT NULL AND lon IS NOT NULL').fetchall()
    points = [(r['name'], float(r['lat']), float(r['lon'])) for r in rows
              if r['lat'] and r['lon'] and -90 <= float(r['lat']) <= 90 and -180 <= float(r['lon']) <= 180]
    if not points:
        return None
    chosen = [p for p in points if p[0] in infra] or points
    lat = statistics.median(p[1] for p in chosen)
    lon = statistics.median(p[2] for p in chosen)
    near = [p for p in chosen if (ld._haversine_m(lat, lon, p[1], p[2]) or 0) <= CENTRE_RADIUS_KM * 1000]
    if near and len(near) != len(chosen):
        lat, lon = statistics.median(p[1] for p in near), statistics.median(p[2] for p in near)
    return round(lat, 4), round(lon, 4)


def station():
    """The station in use: {id, name, lat, lon, distance_km, source}. Resolved
    once from /points (nearest listed station) and cached."""
    if config.WEATHER_STATION:
        cached = json.loads(_meta('station', '{}') or '{}')
        if cached.get('id') == config.WEATHER_STATION:
            return dict(cached, source='config')
        return {'id': config.WEATHER_STATION, 'name': None, 'lat': None, 'lon': None,
                'distance_km': None, 'source': 'config'}
    cached = json.loads(_meta('station', '{}') or '{}')
    return cached or None


def resolve_station():
    """Pick the nearest NWS station to the mesh centre (network call)."""
    if config.WEATHER_STATION:
        st = {'id': config.WEATHER_STATION, 'name': None, 'lat': None, 'lon': None, 'distance_km': None}
        try:
            props = _get(f'{API}/stations/{config.WEATHER_STATION}')
            lon, lat = props['geometry']['coordinates'][:2]
            st.update(name=props['properties'].get('name'), lat=lat, lon=lon)
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.info(f'Weather: station details for {config.WEATHER_STATION} unavailable: {exc}')
        _set_meta('station', json.dumps(st))
        return dict(st, source='config')
    centre = mesh_centre()
    if not centre:
        raise RuntimeError('no located nodes to find the nearest weather station')
    point = _get(f'{API}/points/{centre[0]},{centre[1]}')
    stations = _get(point['properties']['observationStations'])
    best = None
    for f in stations.get('features', []):
        lon, lat = f['geometry']['coordinates'][:2]
        d = ld._haversine_m(centre[0], centre[1], lat, lon) / 1000
        if best is None or d < best['distance_km']:
            best = {'id': f['properties']['stationIdentifier'], 'name': f['properties'].get('name'),
                    'lat': lat, 'lon': lon, 'distance_km': round(d, 1)}
    if not best:
        raise RuntimeError('NWS listed no observation stations near the mesh')
    best['centre'] = list(centre)
    _set_meta('station', json.dumps(best))
    return dict(best, source='nearest')


def _val(props, key):
    v = props.get(key)
    if isinstance(v, dict):
        v = v.get('value')
    return float(v) if isinstance(v, (int, float)) else None


def _is_wet(props, p1):
    if p1 and p1 > 0:
        return True
    words = ' '.join([str(props.get('textDescription') or '')] +
                     [str((w or {}).get('weather') or '') for w in props.get('presentWeather') or []]).lower()
    return any(w in words for w in WET_WORDS)


def parse_observation(feature, station_id):
    """One NWS observation feature -> a weather_observations row (dict)."""
    p = feature.get('properties') or {}
    ts = p.get('timestamp')
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    p1 = _val(p, 'precipitationLastHour')
    keep = {k: p.get(k) for k in ('timestamp', 'textDescription', 'presentWeather', 'rawMessage')}
    return {
        'station': station_id,
        'ts_utc': t.astimezone(timezone.utc).strftime(TS_FMT),
        'ts_local': t.astimezone().strftime(TS_FMT),
        'temp_c': _val(p, 'temperature'), 'dewpoint_c': _val(p, 'dewpoint'),
        'humidity': _val(p, 'relativeHumidity'),
        'wind_kmh': _val(p, 'windSpeed'), 'gust_kmh': _val(p, 'windGust'), 'wind_dir': _val(p, 'windDirection'),
        'precip_1h_mm': p1, 'precip_3h_mm': _val(p, 'precipitationLast3Hours'),
        'precip_6h_mm': _val(p, 'precipitationLast6Hours'),
        'wet': 1 if _is_wet(p, p1) else 0,
        'description': p.get('textDescription') or None,
        'raw': json.dumps(keep)[:2000],
    }


def store(rows):
    ensure_tables()
    cols = ('station', 'ts_utc', 'ts_local', 'temp_c', 'dewpoint_c', 'humidity', 'wind_kmh', 'gust_kmh',
            'wind_dir', 'precip_1h_mm', 'precip_3h_mm', 'precip_6h_mm', 'wet', 'description', 'raw')
    with database.get_connection() as conn:
        before = conn.total_changes
        conn.executemany(
            f"INSERT OR REPLACE INTO weather_observations ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
        return conn.total_changes - before


def fetch(days=None):
    """Fetch observations since the newest stored one (minus 2 h for late
    corrections), or the last `days` / WEATHER_BACKFILL_DAYS when empty.
    Returns {station, fetched, stored} or {error}."""
    ensure_tables()
    try:
        st = station() or resolve_station()
        if config.WEATHER_STATION and not st.get('name') and _meta('station') is None:
            st = resolve_station()
        with database.get_connection() as conn:
            newest = conn.execute('SELECT MAX(ts_utc) AS t FROM weather_observations WHERE station = ?',
                                  (st['id'],)).fetchone()['t']
        if days or not newest:
            start = datetime.now(timezone.utc) - timedelta(days=days or config.WEATHER_BACKFILL_DAYS)
        else:
            start = datetime.strptime(newest, TS_FMT).replace(tzinfo=timezone.utc) - timedelta(hours=2)
        url = f"{API}/stations/{st['id']}/observations"
        params = {'start': start.strftime('%Y-%m-%dT%H:%M:%SZ')}
        rows = []
        for _ in range(MAX_PAGES):
            data = _get(url, params)
            features = data.get('features') or []
            rows.extend(r for r in (parse_observation(f, st['id']) for f in features) if r)
            nxt = (data.get('pagination') or {}).get('next')
            oldest = min((r['ts_utc'] for r in rows), default=None)
            if not features or not nxt or (oldest and oldest <= start.strftime(TS_FMT)):
                break
            url, params = nxt, None
        stored = store(rows) if rows else 0
        prune()
        _last.update(fetched_at=datetime.now().strftime(TS_FMT), error=None, added=stored)
        return {'station': st, 'fetched': len(rows), 'stored': stored}
    except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
        _last.update(fetched_at=datetime.now().strftime(TS_FMT), error=str(exc)[:300], added=None)
        logger.info(f'Weather fetch failed (expected when offline): {exc}')
        return {'error': str(exc)[:300]}


def poll():
    """Scheduler job: never raises."""
    if not config.WEATHER_ENABLED:
        return
    try:
        fetch()
    except Exception as exc:  # never let weather break the scheduler
        logger.warning(f'Weather poll error: {exc}')


def install(scheduler):
    """Register the poller (called from start_scheduler, never in tests)."""
    if not config.WEATHER_ENABLED:
        return
    scheduler.add_job(poll, 'interval', minutes=max(5, config.WEATHER_POLL_MINUTES), id='weather_poll',
                      replace_existing=True, next_run_time=datetime.now() + timedelta(seconds=20))
    logger.info(f'Weather: polling NWS every {config.WEATHER_POLL_MINUTES} min')


def prune():
    cutoff = (datetime.now() - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        conn.execute('DELETE FROM weather_observations WHERE ts_local < ?', (cutoff,))


# --------------------------------------------------------------- reading ----

def observations(start, end, station_id=None):
    """Observations between local timestamps (strings), oldest first."""
    ensure_tables()
    st = station_id or (station() or {}).get('id')
    sql = ('SELECT station, ts_local, ts_utc, temp_c, dewpoint_c, humidity, wind_kmh, gust_kmh, wind_dir, '
           'precip_1h_mm, precip_3h_mm, precip_6h_mm, wet, description FROM weather_observations '
           'WHERE ts_local >= ? AND ts_local <= ?')
    args = [start, end]
    if st:
        sql += ' AND station = ?'
        args.append(st)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql + ' ORDER BY ts_local', args)]


def status():
    ensure_tables()
    st = station()
    with database.get_connection() as conn:
        row = conn.execute('SELECT COUNT(*) AS n, MIN(ts_local) AS first, MAX(ts_local) AS last '
                           'FROM weather_observations' + (' WHERE station = ?' if st else ''),
                           ((st['id'],) if st else ())).fetchone()
        latest = conn.execute('SELECT * FROM weather_observations' + (' WHERE station = ?' if st else '') +
                              ' ORDER BY ts_local DESC LIMIT 1', ((st['id'],) if st else ())).fetchone()
    latest = dict(latest) if latest else None
    if latest:
        latest.pop('raw', None)
    return {'enabled': config.WEATHER_ENABLED, 'station': st, 'observations': row['n'],
            'first': row['first'], 'last': row['last'], 'latest': latest,
            'poll_minutes': config.WEATHER_POLL_MINUTES, 'last_fetch': dict(_last),
            'user_agent_default': config.WEATHER_USER_AGENT == 'aredn-monitor (local)'}


# ----------------------------------------------------------- correlation ----

def _ts(s):
    return datetime.strptime(s[:19], TS_FMT)


def _matcher(obs):
    """Function: local timestamp -> nearest observation within MATCH_MINUTES."""
    times = [_ts(o['ts_local']) for o in obs]
    window = timedelta(minutes=MATCH_MINUTES)

    def match(ts):
        if not times:
            return None
        t = _ts(ts)
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        best = None
        for i in (lo - 1, lo):
            if 0 <= i < len(times) and abs(times[i] - t) <= window:
                if best is None or abs(times[i] - t) < abs(times[best] - t):
                    best = i
        return obs[best] if best is not None else None
    return match


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 2)


def _windy(o):
    w = max(o['wind_kmh'] or 0, o['gust_kmh'] or 0)
    return 'windy' if w >= WIND_HIGH_KMH else 'calm' if w < WIND_CALM_KMH else 'breezy'


def _direction_stats(samples, match):
    """samples: [(ts_local, snr, quality)] for one direction."""
    wet, dry = [], []
    hours = {}
    for ts, snr, q in samples:
        if snr is None:
            continue
        o = match(ts)
        if not o:
            continue
        (wet if o['wet'] else dry).append(snr)
        h = hours.setdefault(ts[:13], {'snr': [], 'obs': o})
        h['snr'].append(snr)
    out = {'samples': len(wet) + len(dry), 'wet_samples': len(wet), 'dry_samples': len(dry),
           'wet_hours': sum(1 for h in hours.values() if h['obs']['wet']), 'hours': len(hours)}
    # Rain
    if wet and dry:
        out['snr_dry'] = round(statistics.median(dry), 1)
        out['snr_wet'] = round(statistics.median(wet), 1)
        out['rain_drop_db'] = round(out['snr_dry'] - out['snr_wet'], 1)
    out['rain_fade'] = bool(out.get('rain_drop_db') is not None and out['rain_drop_db'] >= RAIN_FADE_DB
                            and len(wet) >= MIN_WET_SAMPLES and out['wet_hours'] >= MIN_WET_HOURS)
    # Wind: per-hour SNR spread, windy vs calm
    spread = {'windy': [], 'calm': []}
    level = {'windy': [], 'calm': []}
    for h in hours.values():
        kind = _windy(h['obs'])
        if kind in spread and len(h['snr']) >= 2:
            spread[kind].append(statistics.pstdev(h['snr']))
            level[kind].append(statistics.median(h['snr']))
    out['windy_hours'], out['calm_hours'] = len(spread['windy']), len(spread['calm'])
    if spread['windy'] and spread['calm']:
        out['spread_windy_db'] = round(statistics.median(spread['windy']), 2)
        out['spread_calm_db'] = round(statistics.median(spread['calm']), 2)
        out['wind_drop_db'] = round(statistics.median(level['calm']) - statistics.median(level['windy']), 1)
    sw, sc = out.get('spread_windy_db'), out.get('spread_calm_db')
    out['wind_sensitive'] = bool(
        out['windy_hours'] >= MIN_WIND_HOURS and sw is not None
        and ((sw - sc >= WIND_SPREAD_DB and sw >= WIND_SPREAD_RATIO * max(sc, 0.3))
             or (out.get('wind_drop_db') or 0) >= RAIN_FADE_DB))
    # Hourly correlations with temperature and wind speed
    pts = [(statistics.fmean(h['snr']), h['obs']) for h in hours.values() if h['snr']]
    temp = [(s, o['temp_c']) for s, o in pts if o['temp_c'] is not None]
    wind = [(s, max(o['wind_kmh'] or 0, o['gust_kmh'] or 0)) for s, o in pts]
    if len(temp) >= MIN_HOURS:
        out['temp_r'] = _pearson([t for _, t in temp], [s for s, _ in temp])
        out['temp_range_c'] = round(max(t for _, t in temp) - min(t for _, t in temp), 1)
        cold = [s for s, t in temp if t <= 0]
        warm = [s for s, t in temp if t > 0]
        if cold and warm:
            out['freezing_drop_db'] = round(statistics.median(warm) - statistics.median(cold), 1)
    if len(wind) >= MIN_HOURS:
        out['wind_r'] = _pearson([w for _, w in wind], [s for s, _ in wind])
    out['temp_sensitive'] = bool(out.get('temp_r') is not None and abs(out['temp_r']) >= TEMP_R
                                 and out.get('temp_range_c', 0) >= TEMP_MIN_RANGE_C)
    return out


def _window(hours=None, start=None, end=None):
    end_dt = _ts(end) if end else datetime.now()
    start_dt = _ts(start) if start else end_dt - timedelta(hours=hours or 168)
    return start_dt.strftime(TS_FMT), end_dt.strftime(TS_FMT)


def _link_samples(conn, start, end, source=None, target=None):
    sql = ('SELECT timestamp, source_node, target_node, snr, quality FROM link_history '
           "WHERE sample_type = 'scan' AND UPPER(link_type) = 'RF' AND snr IS NOT NULL "
           'AND timestamp >= ? AND timestamp <= ?')
    args = [start, end]
    if source:
        sql += ' AND source_node = ?'
        args.append(source)
    if target:
        sql += ' AND target_node = ?'
        args.append(target)
    out = {}
    for r in conn.execute(sql + ' ORDER BY timestamp', args):
        out.setdefault((r['source_node'], r['target_node']), []).append((r['timestamp'], r['snr'], r['quality']))
    return out


def _verdicts(d):
    v = []
    if d.get('rain_fade'):
        v.append(f"rain fade: median SNR {d['rain_drop_db']:g} dB lower while wet "
                 f"({d['wet_samples']} wet samples over {d['wet_hours']} h)")
    if d.get('wind_sensitive'):
        v.append(f"wind: SNR spread {d['spread_windy_db']:g} dB in windy hours vs {d['spread_calm_db']:g} dB calm"
                 + (f", median {d['wind_drop_db']:g} dB lower" if (d.get('wind_drop_db') or 0) >= 1 else '')
                 + ' (antenna or mount moving?)')
    if d.get('temp_sensitive'):
        v.append(f"temperature: SNR {'rises' if d['temp_r'] > 0 else 'falls'} with temperature "
                 f"(r = {d['temp_r']:g} over {d['temp_range_c']:g} °C)")
    if (d.get('freezing_drop_db') or 0) >= RAIN_FADE_DB:
        v.append(f"below freezing: median SNR {d['freezing_drop_db']:g} dB lower (ice?)")
    return v


def correlation(source, target=None, hours=168, start=None, end=None):
    """Weather vs SNR for a link (both directions) or, without target, every
    RF tracker of one radio (its view of each neighbour)."""
    source = (source or '').lower()
    target = (target or '').lower() or None
    start, end = _window(hours, start, end)
    obs = observations(start, end)
    match = _matcher(obs)
    with database.get_connection() as conn:
        if target:
            series = {}
            series.update(_link_samples(conn, start, end, source, target))
            series.update(_link_samples(conn, start, end, target, source))
        else:
            series = _link_samples(conn, start, end, source)
    directions = []
    for (a, b), samples in sorted(series.items()):
        d = _direction_stats(samples, match)
        d.update(source=a, target=b, verdicts=_verdicts(d))
        directions.append(d)
    return {'start': start, 'end': end, 'station': station(), 'observations': len(obs),
            'wet_observations': sum(1 for o in obs if o['wet']),
            'windy_observations': sum(1 for o in obs if _windy(o) == 'windy'),
            'temp_range_c': (round(max(o['temp_c'] for o in obs if o['temp_c'] is not None)
                                   - min(o['temp_c'] for o in obs if o['temp_c'] is not None), 1)
                             if any(o['temp_c'] is not None for o in obs) else None),
            'source': source, 'target': target, 'directions': directions}


def summary(hours=168, start=None, end=None, limit=25):
    """Every RF link direction ranked by rain drop, wind sensitivity and
    temperature correlation over the window."""
    start, end = _window(hours, start, end)
    obs = observations(start, end)
    match = _matcher(obs)
    with database.get_connection() as conn:
        series = _link_samples(conn, start, end)
    rows = []
    for (a, b), samples in series.items():
        if len(samples) < 12:
            continue
        d = _direction_stats(samples, match)
        if not d['samples']:
            continue
        d.update(source=a, target=b, verdicts=_verdicts(d))
        rows.append(d)
    rain = sorted((r for r in rows if r.get('rain_drop_db') is not None and r['wet_samples'] >= 3),
                  key=lambda r: -r['rain_drop_db'])[:limit]
    wind = sorted((r for r in rows if r.get('spread_windy_db') is not None),
                  key=lambda r: -((r['spread_windy_db'] - r['spread_calm_db']) + max(0, r.get('wind_drop_db') or 0)))[:limit]
    temp = sorted((r for r in rows if r.get('temp_r') is not None),
                  key=lambda r: -abs(r['temp_r']))[:limit]
    flagged = [r for r in rows if r['verdicts']]
    notes = []
    if not obs:
        notes.append('No weather observations in this window (offline, disabled, or not fetched yet).')
    else:
        if not any(o['wet'] for o in obs):
            notes.append('No precipitation in this window, so rain fade cannot be judged yet.')
        if not any(_windy(o) == 'windy' for o in obs):
            notes.append(f'No windy hours (gust or wind >= {WIND_HIGH_KMH:g} km/h) in this window.')
    return {'start': start, 'end': end, 'station': station(), 'observations': len(obs),
            'wet_observations': sum(1 for o in obs if o['wet']),
            'windy_observations': sum(1 for o in obs if _windy(o) == 'windy'),
            'directions': len(rows), 'flagged': sorted(flagged, key=lambda r: -len(r['verdicts'])),
            'rain': rain, 'wind': wind, 'temperature': temp, 'notes': notes,
            'thresholds': {'rain_fade_db': RAIN_FADE_DB, 'wind_high_kmh': WIND_HIGH_KMH,
                           'wind_calm_kmh': WIND_CALM_KMH, 'temp_r': TEMP_R}}

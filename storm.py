"""
Storm warning (TODO #54): the NWS hourly forecast for the mesh centre plus
each link's measured rain fade (weather.summary) -> "heavy rain expected at
18:00 (80%); these 3 links lost 6+ dB in the last rain".

`fetch()` (hourly job when WEATHER_ENABLED): /points/<mesh centre> ->
`forecastHourly` (URL cached in weather_meta), the next 48 periods into
SQLite `weather_forecast` (start, end, precipitation probability, wording,
temperature, wind). `check()` after each fetch: the periods within
STORM_HOURS_AHEAD with precipitation probability >= STORM_POP_PCT or storm
wording (thunder, heavy rain, freezing rain, sleet, snow, hail) make a
warning; the links at risk are directions whose median SNR fell
STORM_FADE_DB+ while wet over the last STORM_FADE_DAYS, plus wind-sensitive
ones when gusts >= weather.WIND_HIGH_KMH are forecast. `storm_warning`
(warning) when a warning starts, `storm_warning_cleared` when the forecast
drops it. Failures (offline) are recorded, never raised.
"""

import json
import logging
import re
from datetime import datetime, timedelta

import config
import database
import weather

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
STORM_WORDS = re.compile(r'thunder|heavy|freezing|sleet|snow|hail|storm', re.I)

_tables_ready = False
_cache = {'summary': None, 'at': None}


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    weather.ensure_tables()
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS weather_forecast (
            start_time DATETIME PRIMARY KEY, end_time DATETIME, fetched_at DATETIME,
            pop_pct REAL, short_forecast TEXT, temp_c REAL, wind_kmh REAL, gust_kmh REAL)''')
    _tables_ready = True


def _kmh(text):
    """'10 mph' / '5 to 15 mph' / '20 km/h' -> the largest number in km/h."""
    nums = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', str(text or ''))]
    if not nums:
        return None
    v = max(nums)
    return round(v if 'km' in str(text) else v * 1.609, 1)


def _local(iso):
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime(TS_FMT)


def parse_periods(doc, now=None):
    out = []
    for p in (doc.get('properties') or {}).get('periods') or []:
        try:
            pop = (p.get('probabilityOfPrecipitation') or {}).get('value')
            temp = p.get('temperature')
            if temp is not None and (p.get('temperatureUnit') or 'F') == 'F':
                temp = round((temp - 32) * 5 / 9, 1)
            out.append({'start_time': _local(p['startTime']), 'end_time': _local(p['endTime']),
                        'pop_pct': pop, 'short_forecast': p.get('shortForecast'), 'temp_c': temp,
                        'wind_kmh': _kmh(p.get('windSpeed')), 'gust_kmh': _kmh(p.get('windGust'))})
        except (KeyError, ValueError, TypeError):
            continue
    return out[:48]


def fetch(get=None):
    """Fetch and store the hourly forecast. Returns the number of periods."""
    ensure_tables()
    get = get or weather._get
    url = weather._meta('forecast_url')
    if not url:
        centre = weather.mesh_centre()
        if not centre:
            raise RuntimeError('no located nodes for the forecast point')
        url = get(f'{weather.API}/points/{centre[0]},{centre[1]}')['properties']['forecastHourly']
        weather._set_meta('forecast_url', url)
    periods = parse_periods(get(url))
    now = datetime.now().strftime(TS_FMT)
    with database.get_connection() as conn:
        conn.execute('DELETE FROM weather_forecast')
        for p in periods:
            conn.execute('''INSERT OR REPLACE INTO weather_forecast VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                         (p['start_time'], p['end_time'], now, p['pop_pct'], p['short_forecast'], p['temp_c'],
                          p['wind_kmh'], p['gust_kmh']))
    weather._set_meta('forecast_fetch', json.dumps({'at': now, 'periods': len(periods), 'error': None}))
    return len(periods)


def forecast(hours=24, now=None):
    ensure_tables()
    now = now or datetime.now()
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM weather_forecast WHERE end_time > ? AND start_time < ? ORDER BY start_time',
            (now.strftime(TS_FMT), (now + timedelta(hours=hours)).strftime(TS_FMT)))]


def stormy(p):
    return (p.get('pop_pct') or 0) >= config.STORM_POP_PCT or bool(STORM_WORDS.search(p.get('short_forecast') or ''))


def _summary(now):
    if _cache['summary'] is None or not _cache['at'] or (now - _cache['at']).total_seconds() > 6 * 3600:
        _cache['summary'] = weather.summary(hours=24 * config.STORM_FADE_DAYS, limit=200)
        _cache['at'] = now
    return _cache['summary']


def assess(now=None, summary=None):
    """{warning, periods (stormy, next STORM_HOURS_AHEAD), first, links, wind_links, text}."""
    now = now or datetime.now()
    periods = forecast(config.STORM_HOURS_AHEAD, now)
    bad = [p for p in periods if stormy(p)]
    windy = [p for p in periods if max(p.get('gust_kmh') or 0, p.get('wind_kmh') or 0) >= weather.WIND_HIGH_KMH]
    out = {'generated_at': now.strftime(TS_FMT), 'hours_ahead': config.STORM_HOURS_AHEAD,
           'periods': periods, 'stormy': bad, 'windy': windy, 'links': [], 'wind_links': [],
           'warning': False, 'text': None,
           'fetch': json.loads(weather._meta('forecast_fetch', '{}') or '{}')}
    if not bad and not windy:
        return out
    s = summary if summary is not None else _summary(now)
    if bad:
        out['links'] = [{'source': r['source'], 'target': r['target'], 'rain_drop_db': r['rain_drop_db'],
                         'wet_samples': r.get('wet_samples')}
                        for r in s.get('rain') or [] if (r.get('rain_drop_db') or 0) >= config.STORM_FADE_DB]
    if windy:
        out['wind_links'] = [{'source': r['source'], 'target': r['target'],
                              'spread_windy_db': r.get('spread_windy_db'), 'spread_calm_db': r.get('spread_calm_db')}
                             for r in s.get('flagged') or [] if any('wind' in v.lower() for v in r['verdicts'])]
    first = (bad or windy)[0]
    what = (first.get('short_forecast') or 'rain').lower()
    when = first['start_time'][11:16]
    pop = f" ({first['pop_pct']:g}%)" if first.get('pop_pct') is not None else ''
    if out['links'] or out['wind_links']:
        out['warning'] = True
        parts = []
        if out['links']:
            worst = sorted(out['links'], key=lambda l: -l['rain_drop_db'])
            parts.append(f"{len(worst)} link(s) lost {config.STORM_FADE_DB:g}+ dB in past rain: " + ', '.join(
                f"{l['source']} hearing {l['target']} {l['rain_drop_db']:g} dB" for l in worst[:4]))
        if out['wind_links']:
            parts.append(f"{len(out['wind_links'])} wind-sensitive link(s)")
        out['text'] = f"{first.get('short_forecast') or 'Rain'} expected at {when}{pop}; " + '; '.join(parts)
    else:
        out['text'] = f"{what.capitalize()} expected at {when}{pop}; no link has shown rain fade or wind sensitivity yet."
    return out


def check(now=None):
    """Log storm_warning / storm_warning_cleared on transitions."""
    a = assess(now)
    was = weather._meta('storm_warning') == '1'
    events = []
    if a['warning'] and not was:
        database.log_event('storm_warning', None, a['text'], 'warning')
        events.append({'event_type': 'storm_warning', 'details': a['text']})
    elif was and not a['warning']:
        database.log_event('storm_warning_cleared', None, 'Forecast no longer shows rain or wind that hurt links', 'info')
        events.append({'event_type': 'storm_warning_cleared'})
    weather._set_meta('storm_warning', '1' if a['warning'] else '0')
    return events


def poll():
    if not (config.WEATHER_ENABLED and config.STORM_WARNING_ENABLED):
        return
    try:
        fetch()
        check()
    except Exception as exc:
        logger.warning('Storm forecast error: %s', exc)
        try:
            weather._set_meta('forecast_fetch', json.dumps({'at': datetime.now().strftime(TS_FMT), 'error': str(exc)[:200]}))
        except Exception:
            pass


def install(scheduler):
    if not (config.WEATHER_ENABLED and config.STORM_WARNING_ENABLED):
        return
    scheduler.add_job(poll, 'interval', minutes=60, id='storm_forecast', replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=1))

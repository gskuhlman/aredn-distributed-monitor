"""Local time labels. Every time the monitor stores and shows is this PC's
local time (Central for the Cedar Rapids install); CouchDB observation docs and
weather's `ts_utc` are the only UTC values. `TIME_ZONE_NAME` names the zone and
`TIME_ZONE_ABBRS` its standard / daylight abbreviations.
"""

import time
from datetime import datetime

import config


def abbr(dt=None):
    """CST or CDT (the zone's abbreviation in force at `dt`, default now)."""
    std, dst = config.TIME_ZONE_ABBRS
    if dt is None:
        return dst if time.localtime().tm_isdst > 0 else std
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S' if len(dt) >= 19 else '%Y-%m-%d %H:%M')
        except ValueError:
            return std
    try:
        return dst if time.localtime(dt.timestamp()).tm_isdst > 0 else std
    except (OverflowError, OSError, ValueError):
        return std


def label(dt=None):
    """'Central time (CST)'."""
    return f'{config.TIME_ZONE_NAME} time ({abbr(dt)})'


def stamp(value, seconds=False):
    """'2026-11-10 13:20 CST' from a stored local timestamp string."""
    if not value:
        return value
    text = str(value)[:19 if seconds else 16]
    return f'{text} {abbr(str(value))}'


def template_globals():
    """For Jinja: tz_label ('Central time (CST)'), tz_abbr and tz_name."""
    return {'tz_label': label(), 'tz_abbr': abbr(), 'tz_name': config.TIME_ZONE_NAME}

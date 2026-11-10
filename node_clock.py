"""Node clock and time zone (hygiene "Central time").

sysinfo.json carries no time zone, but the AREDN status page's clock widget
does: `<div class="t">1:49 pm</div><div class="s">cst (ntp)</div>` = the node's
local time, its zone abbreviation and the time source. `check_all()` (own job
every `NODE_CLOCK_HOURS`, gated by gateway.py) reads `/a/status` of every
active node and compares the shown time with this PC's local (Central) clock:
within `NODE_CLOCK_MAX_OFFSET_MINUTES` = on Central time. A node whose page
can't be read falls back to the time zone in its newest SSH `uci` snapshot
(`system.@system[0].timezone`, Central = `CST6CDT...`). SQLite `node_clock`.
Nodes left on AREDN's default (UTC) log and show times 5-6 hours off.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

import config
import database
import gateway

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
CENTRAL_ZONES = {'cst', 'cdt', 'central', 'america/chicago'}
CENTRAL_POSIX = re.compile(r'^\s*CST6CDT', re.I)
CLOCK_RE = re.compile(r'<div class="t">\s*([^<]+?)\s*</div>\s*<div class="s">\s*([^<]*?)\s*</div>', re.I)
TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?$', re.I)
SOURCE_RE = re.compile(r'^(.*?)\s*\(([^)]*)\)\s*$')
_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS node_clock (
            node_name TEXT PRIMARY KEY, checked_at DATETIME, node_time TEXT, zone TEXT, source TEXT,
            offset_minutes INTEGER, central INTEGER, via TEXT, error TEXT)''')
    _tables_ready = True


def parse_status(text, now=None):
    """{node_time, zone, source, offset_minutes} from an AREDN status page, or None."""
    now = now or datetime.now()
    for t, s in CLOCK_RE.findall(text or ''):
        m = TIME_RE.match(t.strip())
        if not m:
            continue
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or '').lower().replace('.', '')
        if ampm:
            hour = hour % 12 + (12 if ampm == 'pm' else 0)
        if hour > 23 or minute > 59:
            continue
        zone, source = s.strip(), None
        sm = SOURCE_RE.match(zone)
        if sm:
            zone, source = sm.group(1).strip(), sm.group(2).strip()
        diff = (hour * 60 + minute) - (now.hour * 60 + now.minute)
        diff = (diff + 720) % 1440 - 720          # -720 .. 719: the clock is a time of day only
        return {'node_time': t.strip(), 'zone': zone.lower() or None, 'source': source,
                'offset_minutes': diff}
    return None


def judge(parsed):
    """True = on Central time; the shown time decides, the label only when no time."""
    if parsed is None:
        return None
    if parsed.get('offset_minutes') is not None:
        return abs(parsed['offset_minutes']) <= config.NODE_CLOCK_MAX_OFFSET_MINUTES
    return (parsed.get('zone') or '') in CENTRAL_ZONES


def from_uci(text):
    """{zone, central} from a `uci show` snapshot, or None."""
    m = re.search(r"system\.@system\[0\]\.zonename='([^']*)'", text or '')
    t = re.search(r"system\.@system\[0\]\.timezone='([^']*)'", text or '')
    if not m and not t:
        return None
    zone = (m.group(1) if m else '') or (t.group(1) if t else '')
    central = bool(t and CENTRAL_POSIX.match(t.group(1))) or zone.lower() in CENTRAL_ZONES
    return {'zone': zone or None, 'central': central}


def fetch(name, timeout=10):
    try:
        r = requests.get(f'http://{name.lower()}.local.mesh/a/status', timeout=timeout)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}'
        parsed = parse_status(r.text)
        return parsed, None if parsed else 'no clock on the status page'
    except requests.RequestException as exc:
        return None, type(exc).__name__


def _uci_zones(conn):
    out = {}
    for r in conn.execute("SELECT node_name, text FROM ssh_snapshots s WHERE kind = 'uci' AND timestamp = "
                          "(SELECT MAX(timestamp) FROM ssh_snapshots t WHERE t.node_name = s.node_name "
                          "AND t.kind = 'uci')"):
        z = from_uci(r['text'])
        if z:
            out[r['node_name']] = z
    return out


def check_all(names=None, fetcher=fetch, now=None):
    """Read every active node's clock. Returns {'checked', 'central', 'off', 'aborted'}."""
    ensure_tables()
    if not gateway.is_reachable():
        return {'aborted': 'starting node unreachable', 'checked': 0}
    with database.get_connection() as conn:
        if names is None:
            names = [r[0] for r in conn.execute("SELECT name FROM nodes WHERE is_active = 1 AND name NOT LIKE 'mac-%'")]
        try:
            uci = _uci_zones(conn)
        except Exception:
            uci = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(names, pool.map(fetcher, names)))
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    rows = []
    for name, (parsed, error) in results.items():
        if parsed:
            rows.append((name, now_s, parsed['node_time'], parsed['zone'], parsed['source'],
                         parsed['offset_minutes'], int(judge(parsed)), 'status', None))
        elif name in uci:
            rows.append((name, now_s, None, uci[name]['zone'], None, None, int(uci[name]['central']), 'uci', error))
        else:
            rows.append((name, now_s, None, None, None, None, None, None, error))
    with database.get_connection() as conn:
        # A failed read keeps the last good answer (just records the error).
        for r in rows:
            if r[6] is None:
                cur = conn.execute('UPDATE node_clock SET error = ? WHERE node_name = ?', (r[8], r[0]))
                if cur.rowcount:
                    continue
            conn.execute('INSERT OR REPLACE INTO node_clock VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', r)
    good = [r for r in rows if r[6] is not None]
    return {'aborted': None, 'checked': len(good), 'central': sum(1 for r in good if r[6]),
            'off': sorted(r[0] for r in good if not r[6]), 'failed': len(rows) - len(good)}


def states():
    """{node: row} for the hygiene check."""
    ensure_tables()
    with database.get_connection() as conn:
        return {r['node_name']: dict(r) for r in conn.execute('SELECT * FROM node_clock')}


def describe(row):
    """Fix text for a node not on Central time."""
    off = row.get('offset_minutes')
    zone = row.get('zone')
    if off is not None:
        hours = abs(off) / 60
        when = f"{hours:.0f} h" if abs(off) >= 90 else f"{abs(off)} min"
        ahead = 'ahead of' if off > 0 else 'behind'
        likely = ' (looks like UTC, the AREDN default)' if abs(abs(off) - 300) <= 15 or abs(abs(off) - 360) <= 15 else ''
        clock = f"its clock shows {row.get('node_time')}" + (f" {zone}" if zone else '') + f", {when} {ahead} Central{likely}"
        if row.get('source') and row['source'].lower() in ('none', 'local'):
            clock += '; it has no time source either (set an NTP server)'
    else:
        clock = f"its time zone is {zone or 'not set'}"
    return (f"Set the node's time zone to Central (America/Chicago): {clock}. "
            "Click the clock on the node's status page to change it, so its logs and times match the club's.")


def install(scheduler, job):
    if not config.NODE_CLOCK_ENABLED:
        return
    scheduler.add_job(job, 'interval', hours=config.NODE_CLOCK_HOURS, id='node_clock', replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=6))

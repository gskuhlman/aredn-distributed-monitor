"""
WiFi survey at the sites (Channel Map "WiFi survey", channel planner noise).

Records the non-mesh access points each radio can hear, per channel, and
feeds them to the channel planner: a group whose radios (or radios on their
mast) have survey data from the last WIFI_SURVEY_MAX_AGE_DAYS is costed by
the access points actually heard on each candidate channel instead of the
fixed "busy WiFi" guesses (DFS, UNII-3, UNII-4; TDWR and C-V2X stay).

Getting a scan:
  scan_node(name)  asks the node for its WiFi scan page (WIFI_SCAN_PATHS,
                   tried in order) and parses it. A scan takes the radio off
                   its channel for a moment, so it only runs on request, or
                   nightly at WIFI_SURVEY_HOUR for infrastructure radios when
                   WIFI_SURVEY_ENABLED, plus radios an operator queued for
                   tonight (`wifi_survey_queue`, Spectrum scan reminders).
                   Which page a firmware offers varies;
                   when none answers, import the text instead;
  import_text()    parses pasted `iw dev wlan0 scan` output or the text of a
                   node's scan page (tables with signal / channel or
                   frequency / SSID / BSSID columns).
Mesh beacons (the SSIDs the mesh radios report) are marked `is_mesh` and
ignored by the planner. SQLite `wifi_survey` (one row per AP per scan).
"""

import logging
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser

import requests

import config
import database
import infra_audit as ia

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
WIFI_SCAN_PATHS = ('/cgi-bin/scan', '/a/scan')
AP_WIDTH_MHZ = 20.0          # assume a 20 MHz primary channel per AP
AP_COST = 12.0               # per AP fully inside a candidate channel, at a strong signal
SITE_METERS = 150

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS wifi_survey (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                taken_at DATETIME NOT NULL,
                source TEXT,
                bssid TEXT,
                ssid TEXT,
                frequency REAL,
                channel INTEGER,
                signal REAL,
                is_mesh INTEGER NOT NULL DEFAULT 0
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wifi_survey_node ON wifi_survey(node_name, taken_at)')
        conn.execute('''CREATE TABLE IF NOT EXISTS wifi_survey_queue (
                            node_name TEXT PRIMARY KEY, requested_at DATETIME, reason TEXT)''')
        cols = {r[1] for r in conn.execute('PRAGMA table_info(wifi_survey)')}
        for col in ('low_mhz', 'high_mhz'):        # occupied span, when the scan says (iw)
            if col not in cols:
                conn.execute(f'ALTER TABLE wifi_survey ADD COLUMN {col} REAL')
    _tables_ready = True


def channel_to_freq(ch):
    ch = int(ch)
    if 1 <= ch <= 14:
        return 2484.0 if ch == 14 else 2407.0 + 5 * ch
    if ch >= 32:
        return 5000.0 + 5 * ch
    if ch < 0:   # AREDN 2.4 GHz negative channels
        return 2407.0 + 5 * ch
    return None


# ------------------------------------------------------------- parsers ---

def _iw_span(ap):
    """(low, high) MHz an AP occupies from its HT/VHT operation lines, or
    (None, None) when the scan didn't say (then AP_WIDTH_MHZ is assumed)."""
    f = ap.get('frequency')
    if not f:
        return None, None
    width, seg1, seg2 = ap.pop('_vht_width', None), ap.pop('_seg1', None), ap.pop('_seg2', None)
    offset = ap.pop('_ht_offset', None)
    if width and seg1:
        mhz, centre = 80, seg1
        if width == 2:
            mhz = 160
        elif width == 1 and seg2 and abs(seg2 - seg1) == 8:     # 160 MHz signalled the newer way
            mhz, centre = 160, seg2
        c = 5000 + 5 * centre
        if c - mhz / 2 < f < c + mhz / 2:
            return c - mhz / 2, c + mhz / 2
        # Some APs advertise segments that don't contain their own primary
        # channel (seen: primary 132 with "segment 1: 58"); trust HT instead.
    if offset == 'above':
        return f - 10, f + 30
    if offset == 'below':
        return f - 30, f + 10
    if offset:
        return f - 10, f + 10
    return None, None


def parse_iw(text):
    """`iw dev <if> scan` output -> [{bssid, ssid, frequency, signal, low, high}].
    Only the BSS / freq / signal / SSID and HT/VHT operation lines are read,
    so the full output (thousands of lines) or a grep of those lines works."""
    out, cur = [], None
    for line in text.splitlines():
        s = line.strip().lstrip('* ').strip()
        m = re.match(r'^BSS ([0-9a-f:]{17})', s, re.I)
        if m:
            cur = {'bssid': m.group(1).lower(), 'ssid': None, 'frequency': None, 'signal': None}
            out.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r'secondary channel offset:\s*(above|below|no secondary)', s)
        if m:
            cur['_ht_offset'] = m.group(1)
            continue
        m = re.match(r'channel width:\s*(\d+)', s)
        if m:
            cur['_vht_width'] = int(m.group(1))
            continue
        m = re.match(r'center freq segment ([12]):\s*(\d+)', s)
        if m:
            cur['_seg' + m.group(1)] = int(m.group(2))
            continue
        if s.startswith('freq:'):
            try:
                cur['frequency'] = float(s.split(':', 1)[1].split()[0])
            except (ValueError, IndexError):
                pass
        elif s.startswith('signal:'):
            m = re.search(r'(-?\d+(?:\.\d+)?)', s)
            cur['signal'] = float(m.group(1)) if m else None
        elif s.startswith('SSID:'):
            cur['ssid'] = s.split(':', 1)[1].strip()
    for ap in out:
        ap['low'], ap['high'] = _iw_span(ap)
    return [x for x in out if x['frequency']]


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._row is not None and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _parse_rows(rows):
    """Header-driven table rows (scan page) -> APs."""
    out, header = [], None
    for row in rows:
        low = [c.lower() for c in row]
        if any('ssid' in c for c in low) and any(k in ' '.join(low) for k in ('chan', 'freq', 'sig')):
            header = low
            continue
        if not header or len(row) < 2:
            continue
        ap = {'bssid': None, 'ssid': None, 'frequency': None, 'signal': None}
        for h, v in zip(header, row):
            if 'ssid' in h and 'b' not in h.replace('ssid', ''):
                ap['ssid'] = v or None
            elif 'bssid' in h or 'mac' in h:
                m = re.search(r'([0-9a-f]{2}(?::[0-9a-f]{2}){5})', v, re.I)
                ap['bssid'] = m.group(1).lower() if m else None
            elif 'freq' in h:
                m = re.search(r'(\d{4})', v)
                ap['frequency'] = float(m.group(1)) if m else ap['frequency']
            elif 'chan' in h:
                m = re.search(r'(\d{4})', v)          # "149 (5745)" or "5745"
                if m:
                    ap['frequency'] = float(m.group(1))
                else:
                    m = re.search(r'(-?\d+)', v)
                    if m and not ap['frequency']:
                        ap['frequency'] = channel_to_freq(m.group(1))
            elif 'sig' in h or 'rssi' in h:
                m = re.search(r'(-\d+)', v)
                ap['signal'] = float(m.group(1)) if m else None
        if ap['frequency']:
            out.append(ap)
    return out


def parse_text(text):
    """Best effort: iw output, an HTML scan page, or tab/space-separated rows."""
    text = text or ''
    if re.search(r'^\s*BSS [0-9a-f:]{17}', text, re.I | re.M):
        return parse_iw(text)
    if '<tr' in text.lower():
        p = _Tables()
        p.feed(text)
        return _parse_rows(p.rows)
    rows = [re.split(r'\t|\s{2,}', l.strip()) for l in text.splitlines() if l.strip()]
    return _parse_rows(rows)


# --------------------------------------------------------------- store ---

def _mesh_ssids(conn):
    return {(r[0] or '').lower() for r in conn.execute('SELECT DISTINCT rf_ssid FROM nodes WHERE rf_ssid IS NOT NULL')}


def store(node, aps, source):
    ensure_tables()
    now = datetime.now().strftime(TS_FMT)
    with database.get_connection() as conn:
        if not conn.execute('SELECT 1 FROM nodes WHERE name = ?', (node,)).fetchone():
            raise ValueError(f'unknown node: {node}')
        mesh = _mesh_ssids(conn)
        rows = []
        for ap in aps:
            f = ap.get('frequency')
            ssid = ap.get('ssid')
            is_mesh = bool(ssid and (ssid.lower() in mesh or ssid.lower().startswith('aredn')))
            rows.append((node, now, source, ap.get('bssid'), ssid, f,
                         ia.freq_to_channel(f) if f else None, ap.get('signal'), int(is_mesh),
                         ap.get('low'), ap.get('high')))
        conn.executemany('''INSERT INTO wifi_survey (node_name, taken_at, source, bssid, ssid, frequency, channel,
                                signal, is_mesh, low_mhz, high_mhz) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', rows)
    return {'node': node, 'taken_at': now, 'source': source, 'aps': len(rows),
            'non_mesh': sum(1 for r in rows if not r[8])}


def import_text(node, text):
    aps = parse_text(text)
    if not aps:
        raise ValueError('no access points found in the text (expected iw scan output or a scan table)')
    return store(node, aps, 'import')


def scan_node(node):
    """Ask the node for a WiFi scan (on request only: briefly interrupts it)."""
    import gateway
    if not gateway.is_reachable():
        raise RuntimeError(gateway.unreachable_message())
    with database.get_connection() as conn:
        r = conn.execute('SELECT ip FROM nodes WHERE name = ?', (node,)).fetchone()
    host = f'{node}.local.mesh'
    errors = []
    for path in WIFI_SCAN_PATHS:
        for h in [host] + ([r['ip']] if r and r['ip'] else []):
            try:
                resp = requests.get(f'http://{h}{path}', timeout=30)
            except requests.RequestException as exc:
                errors.append(f'{path}: {type(exc).__name__}')
                continue
            if resp.status_code != 200:
                errors.append(f'{path}: HTTP {resp.status_code}')
                break
            aps = parse_text(resp.text)
            if aps:
                return store(node, aps, f'scan {path}')
            errors.append(f'{path}: no scan table in the page')
            break
    raise RuntimeError('No WiFi scan page answered (' + '; '.join(errors)
                       + '). Paste the scan text with Import instead.')


# ------------------------------------------------------------ queries ---

def latest(conn=None, max_age_days=None, nodes=None, at=None):
    """Newest scan per node within max_age_days (of `at`, default now):
    {node: [ap rows]}."""
    if conn is None:
        ensure_tables()
        with database.get_connection() as c:
            return latest(c, max_age_days, nodes, at)
    ensure_tables()
    at = at or datetime.now()
    since = (at - timedelta(days=max_age_days or config.WIFI_SURVEY_MAX_AGE_DAYS)).strftime(TS_FMT)
    newest = {r[0]: r[1] for r in conn.execute(
        'SELECT node_name, MAX(taken_at) FROM wifi_survey WHERE taken_at >= ? AND taken_at <= ? GROUP BY node_name',
        (since, at.strftime(TS_FMT)))}
    out = {}
    for n, at in newest.items():
        if nodes is not None and n not in nodes:
            continue
        out[n] = [dict(r) for r in conn.execute(
            'SELECT * FROM wifi_survey WHERE node_name = ? AND taken_at = ? ORDER BY signal DESC', (n, at))]
    return out


def previous(conn, node, before):
    """The scan of `node` just before `before` (taken_at), or []."""
    ensure_tables()
    r = conn.execute('SELECT MAX(taken_at) FROM wifi_survey WHERE node_name = ? AND taken_at < ?',
                     (node, before)).fetchone()
    if not r or not r[0]:
        return []
    return [dict(x) for x in conn.execute('SELECT * FROM wifi_survey WHERE node_name = ? AND taken_at = ?',
                                          (node, r[0]))]


def last_scans(conn):
    """{node: newest taken_at} over all time (scan reminders)."""
    ensure_tables()
    return {r[0]: r[1] for r in conn.execute('SELECT node_name, MAX(taken_at) FROM wifi_survey GROUP BY node_name')}


def scan_times(conn, nodes=None):
    """{node: [taken_at, ...]} newest first (Spectrum history picker)."""
    ensure_tables()
    out = {}
    for r in conn.execute('SELECT DISTINCT node_name, taken_at FROM wifi_survey ORDER BY taken_at DESC'):
        if nodes is None or r[0] in nodes:
            out.setdefault(r[0], []).append(r[1])
    return out


# ------------------------------------------------- tonight's scan queue ---

def queue_tonight(radios, reason=None):
    """Ask for these radios to be scanned at the next WIFI_SURVEY_HOUR run
    (an operator request; runs even when the nightly survey is off)."""
    ensure_tables()
    now = datetime.now().strftime(TS_FMT)
    with database.get_connection() as conn:
        known = {r[0] for r in conn.execute('SELECT name FROM nodes')}
        names = [n.strip().lower() for n in radios if n and n.strip().lower() in known]
        conn.executemany('INSERT OR REPLACE INTO wifi_survey_queue (node_name, requested_at, reason) VALUES (?, ?, ?)',
                         [(n, now, reason) for n in names])
    return names


def unqueue(radios=None):
    ensure_tables()
    with database.get_connection() as conn:
        if radios is None:
            return conn.execute('DELETE FROM wifi_survey_queue').rowcount
        return sum(conn.execute('DELETE FROM wifi_survey_queue WHERE node_name = ?', (n.lower(),)).rowcount
                   for n in radios)


def queued():
    ensure_tables()
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM wifi_survey_queue ORDER BY node_name')]


def aps_for(radio_names, by_name):
    """Non-mesh APs heard by these radios or by radios on their masts."""
    try:
        scans = latest()
    except Exception as exc:
        logger.warning('wifi survey read failed: %s', exc)
        return []
    if not scans:
        return []
    import link_diagnostics as ld
    want = set(radio_names)
    for n, r in by_name.items():
        if n in scans and r.get('lat') is not None and any(
                by_name.get(m, {}).get('lat') is not None and
                (ld._haversine_m(r['lat'], r['lon'], by_name[m]['lat'], by_name[m]['lon']) or 1e9) <= SITE_METERS
                for m in radio_names):
            want.add(n)
    aps = {}
    for n in want:
        for ap in scans.get(n, []):
            if ap['is_mesh'] or not ap['frequency']:
                continue
            key = ap['bssid'] or (ap['ssid'], ap['frequency'])
            if key not in aps or (ap['signal'] or -200) > (aps[key]['signal'] or -200):
                aps[key] = dict(ap, low=ap.get('low_mhz') or ap['frequency'] - AP_WIDTH_MHZ / 2,
                                high=ap.get('high_mhz') or ap['frequency'] + AP_WIDTH_MHZ / 2)
    return list(aps.values())


def ap_cost(lo, hi, aps, members):
    """(cost, overlapping APs) of a channel span from surveyed APs."""
    cost, hits = 0.0, []
    for ap in aps:
        overlap = min(hi, ap['high']) - max(lo, ap['low'])
        if overlap <= 0:
            continue
        sig = ap['signal'] if ap['signal'] is not None else -80.0
        strength = min(1.5, max(0.1, (sig + 95.0) / 30.0))    # -95 dBm ~0, -65 dBm 1, louder up to 1.5
        cost += AP_COST * strength * overlap / (hi - lo) * members
        hits.append(ap)
    return cost, hits


def report():
    ensure_tables()
    scans = latest(max_age_days=90)
    infra = database.get_infrastructure_nodes()
    nodes = []
    for n, aps in sorted(scans.items()):
        non_mesh = [a for a in aps if not a['is_mesh']]
        by_channel = {}
        for a in non_mesh:
            by_channel.setdefault(a['channel'] or a['frequency'], []).append(a)
        nodes.append({'node': n, 'site': infra.get(n), 'taken_at': aps[0]['taken_at'] if aps else None,
                      'source': aps[0]['source'] if aps else None, 'aps': aps, 'non_mesh': len(non_mesh),
                      'by_channel': {str(k): len(v) for k, v in sorted(by_channel.items(), key=lambda x: float(x[0]))}})
    return {'generated_at': datetime.now().strftime(TS_FMT), 'nodes': nodes, 'scan_paths': list(WIFI_SCAN_PATHS),
            'max_age_days': config.WIFI_SURVEY_MAX_AGE_DAYS, 'scheduled': config.WIFI_SURVEY_ENABLED,
            'queued': queued(), 'hour': config.WIFI_SURVEY_HOUR}


def nightly_job():
    """Infrastructure radios when WIFI_SURVEY_ENABLED, plus every radio queued
    for tonight; queued radios leave the queue once tried."""
    names = set(r['node_name'] for r in queued())
    if config.WIFI_SURVEY_ENABLED:
        names |= set(database.get_infrastructure_nodes())
    done = []
    for n in sorted(names):
        try:
            scan_node(n)
            done.append(n)
        except Exception as exc:
            logger.info('wifi survey of %s skipped: %s', n, exc)
    unqueue(sorted(names))
    return done


def install(scheduler):
    # Always scheduled: with the survey off it only scans radios an operator
    # queued for tonight (and does nothing when the queue is empty).
    scheduler.add_job(nightly_job, 'cron', hour=config.WIFI_SURVEY_HOUR, minute=15, id='wifi_survey',
                      replace_existing=True)

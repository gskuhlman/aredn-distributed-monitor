"""
Phone connectivity (VOIP tab "Phone Connectivity"): a list of the phones on
the PBX with their IP and the AREDN node each one hangs off, pinged from this
PC after every committed scan.

Where the list comes from (merged by IP; operator edits are kept):
  service  advertised phone/PBX services (database._voip_service_type); the
           phone's IP comes from the link path or the service name ("Direct
           IP: 10.1.2.3"), else the link's host in the mesh host list (not the
           node itself). "Phone book" pages are skipped. Services without an
           IP are listed as `unresolved` (not pinged) for the operator to add;

  host     the mesh host list every node reports (sysinfo `hosts`, stored in
           SQLite `mesh_hosts` by the scanner) whose name looks like a phone
           or PBX (PHONE_NAME_RE);
  pbx      pasted from the PBX: `pjsip show contacts`, `sip show peers`, or
           "extension,name,ip" lines (parse_pbx_text);
  manual   added on the VOIP tab.
Auto entries (service/host) the scans stop reporting are dropped after
AUTO_FORGET_DAYS; removing one hides it (`ignored`) instead of deleting it.

Attached node: set by hand, else the node whose LAN holds the IP. AREDN LAN
subnets are /29, /28, /27 or /26 with the node's br-lan address (nodes.ip)
first, so the node with the smallest such block containing the IP wins; an IP
that is a node's own address belongs to that node.

Each run pings every enabled phone PHONE_PING_COUNT times (concurrently),
gated by gateway.py: skipped when the starting node is unreachable, and the
whole run is discarded when a failure turns out to be ours. Results go to
SQLite `phone_pings` (kept HISTORY_RETENTION_HOURS) and the per-phone state
in `phones`. Events on transitions: `phone_unreachable` (warning) after
PHONE_PING_FAILS failed runs in a row while the attached node answers the
scanner (a down node is the scanner's alarm; with no attached node the phone
alarms on its own), `phone_restored` (info). node_name = attached node, else
the phone's name.
"""

import ipaddress
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import config
import database
import gateway

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
AUTO_FORGET_DAYS = 7
LAN_BLOCKS = (8, 16, 32, 64)
PHONE_NAME_RE = re.compile(
    r'phone|ext\.?-?\d|\bext\b|sip|voip|yealink|grandstream|\bgxp|\bgrp\d|polycom|cisco-?\d|\bspa-?\d|'
    r'\bata\b|handset|linksys-?pap|obihai|\bobi\d|snom|fanvil|\bdect\b', re.I)
PBX_NAME_RE = re.compile(r'pbx|asterisk|3cx|freeswitch|hamshack|\bhhus\b|phone\s*server', re.I)
PHONEBOOK_RE = re.compile(r'phone\s*book|directory', re.I)
EXT_RE = re.compile(r'(?:ext\.?\s*:?\s*-?\s*|\bx)(\d{3,6})\b|^(\d{3,6})\s*-', re.I)
IP_RE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')

_tables_ready = False
_hosts_seen = {}  # ip -> name, filled by the scanner between saves


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS mesh_hosts (
                ip TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                first_seen DATETIME,
                last_seen DATETIME
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS phones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL UNIQUE,
                name TEXT,
                extension TEXT,
                kind TEXT NOT NULL DEFAULT 'phone',
                node_name TEXT,
                source TEXT NOT NULL,
                note TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                ignored INTEGER NOT NULL DEFAULT 0,
                added DATETIME,
                last_detected DATETIME,
                ok INTEGER,
                fail_count INTEGER NOT NULL DEFAULT 0,
                alarm INTEGER NOT NULL DEFAULT 0,
                since DATETIME,
                last_check DATETIME,
                last_ok DATETIME,
                last_avg REAL,
                last_jitter REAL,
                last_loss REAL,
                checks INTEGER NOT NULL DEFAULT 0,
                oks INTEGER NOT NULL DEFAULT 0,
                edited INTEGER NOT NULL DEFAULT 0
            )
        ''')
        if 'edited' not in {r[1] for r in conn.execute('PRAGMA table_info(phones)')}:
            conn.execute('ALTER TABLE phones ADD COLUMN edited INTEGER NOT NULL DEFAULT 0')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS phone_pings (
                timestamp DATETIME NOT NULL,
                phone_id INTEGER NOT NULL,
                ok INTEGER,
                avg_ms REAL,
                jitter_ms REAL,
                loss REAL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_phone_pings ON phone_pings (phone_id, timestamp)')
    _tables_ready = True


# --- mesh host list ---------------------------------------------------------

def note_hosts(hosts):
    """Called by the scanner for each node's sysinfo `hosts` (name, ip) list."""
    for h in hosts or []:
        ip, name = (h or {}).get('ip'), (h or {}).get('name')
        if ip and name and IP_RE.fullmatch(str(ip)):
            _hosts_seen[str(ip)] = str(name)


def save_hosts(now=None):
    """Store the hosts noted since the last save (after a committed scan)."""
    if not _hosts_seen:
        return 0
    ensure_tables()
    now_s = (now or datetime.now()).strftime(TS_FMT)
    rows = list(_hosts_seen.items())
    _hosts_seen.clear()
    with database.get_connection() as conn:
        conn.executemany('''INSERT INTO mesh_hosts (ip, name, first_seen, last_seen) VALUES (?, ?, ?, ?)
                            ON CONFLICT(ip) DO UPDATE SET name = excluded.name, last_seen = excluded.last_seen''',
                         [(ip, name, now_s, now_s) for ip, name in rows])
    return len(rows)


def _short(name):
    n = str(name or '').strip().lower()
    for suffix in ('.local.mesh', '.local'):
        if n.endswith(suffix):
            n = n[:-len(suffix)]
    return n


# --- attached node ----------------------------------------------------------

def _ip_int(ip):
    try:
        return int(ipaddress.IPv4Address(str(ip)))
    except (ipaddress.AddressValueError, ValueError):
        return None


def owner_node(ip, node_ips):
    """The node whose LAN holds `ip`. node_ips: {name: br-lan ip}."""
    v = _ip_int(ip)
    if v is None:
        return None
    best = None
    for name, nip in node_ips.items():
        n = _ip_int(nip)
        if n is None:
            continue
        if n == v:
            return name
        net = n - 1
        for size in LAN_BLOCKS:
            if net % size == 0 and n < v < net + size - 1:
                if best is None or size < best[0]:
                    best = (size, name)
                break
    return best[1] if best else None


# --- building the list ------------------------------------------------------

def _kind_for_name(name):
    if PBX_NAME_RE.search(name or ''):
        return 'pbx'
    if PHONE_NAME_RE.search(name or ''):
        return 'phone'
    return None


def _extension(text):
    m = EXT_RE.search(text or '')
    return next((g for g in m.groups() if g), None) if m else None


def _clean(name):
    return re.sub(r'\s*\[(phone|server|pbx|voip)\]\s*', ' ', name or '', flags=re.I).strip()


def detect(conn):
    """Auto-detected phones: ([{ip, name, extension, kind, source}], [unresolved]).

    A service's phone IP is, in order: an IP in its link (sip://node:5060/10.1.2.3),
    an IP in its name ("Direct IP: 10.1.2.3"), or its link host from the mesh host
    list when that is not the node itself. Services with none of these
    ("x1000 [phone]") are returned as unresolved (not pinged) unless their node
    already has a resolved phone (the label then describes that phone).
    """
    node_rows = [dict(r) for r in conn.execute('SELECT name, ip FROM nodes')]
    node_names = {r['name'].lower() for r in node_rows}
    node_ips = {r['ip'] for r in node_rows if r['ip']}
    hosts = {r['ip']: r['name'] for r in conn.execute('SELECT ip, name FROM mesh_hosts')}
    by_name = {_short(n): ip for ip, n in hosts.items()}
    found, unresolved = {}, []
    for e in database.get_voip_endpoints():
        name, link = e.get('device') or '', (e.get('link') or '').strip()
        if PHONEBOOK_RE.search(name) or PHONEBOOK_RE.search(link):
            continue
        ip = None
        m = re.match(r'^(?:[a-z]+://)?([^/:?]+)(.*)$', link, re.I)
        host, rest = (m.group(1), m.group(2)) if m else (None, '')
        for text in (rest, name):
            hit = IP_RE.search(text or '')
            if hit and _ip_int(hit.group(1)) is not None:
                ip = hit.group(1)
                break
        if not ip and host:
            ip = host if IP_RE.fullmatch(host) else by_name.get(_short(host))
            if ip in node_ips:
                ip = None
        kind = 'pbx' if e['type'] == 'pbx' or PBX_NAME_RE.search(f'{name} {host or ""}') else 'phone'
        entry = {'ip': ip, 'name': _clean(name), 'extension': _extension(name), 'kind': kind,
                 'source': 'service', 'node': e['node']}
        if ip:
            found.setdefault(ip, entry)
        else:
            unresolved.append(entry)
    # The host list covers the whole connected mesh (every tunnelled region), so a
    # host only counts when it sits on the LAN of a node this monitor scans.
    node_ip = {r['name']: r['ip'] for r in node_rows if r['ip']}
    for ip, name in hosts.items():
        short = _short(name)
        if ip in found or ip in node_ips or short in node_names or short.split('.')[0] in ('dtdlink', 'mid1', 'mid2', 'lan'):
            continue
        kind = _kind_for_name(name)
        if kind and not PHONEBOOK_RE.search(name) and owner_node(ip, node_ip):
            found[ip] = {'ip': ip, 'name': name, 'extension': _extension(name), 'kind': kind, 'source': 'host'}
    with_phone = {owner_node(ip, node_ip) for ip in found}
    unresolved = [u for u in unresolved if u['node'] not in with_phone]
    return list(found.values()), unresolved


def sync(now=None):
    """Merge auto-detected phones into the `phones` table."""
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    forget = (now - timedelta(days=AUTO_FORGET_DAYS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        found, _ = detect(conn)
        existing = {r['ip']: dict(r) for r in conn.execute('SELECT id, ip, source FROM phones')}
        for f in found:
            if f['ip'] in existing:
                conn.execute('''UPDATE phones SET last_detected = ?,
                                name = CASE WHEN edited = 1 OR source NOT IN ('service', 'host') THEN name ELSE ? END,
                                kind = CASE WHEN edited = 1 OR source NOT IN ('service', 'host') THEN kind ELSE ? END,
                                extension = CASE WHEN edited = 1 THEN extension ELSE COALESCE(extension, ?) END
                                WHERE ip = ?''',
                             (now_s, f['name'], f['kind'], f['extension'], f['ip']))
            else:
                conn.execute('''INSERT INTO phones (ip, name, extension, kind, source, added, last_detected)
                                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                             (f['ip'], f['name'], f['extension'], f['kind'], f['source'], now_s, now_s))
        # With a fresh host list, auto entries no longer detected go at once (unless
        # the operator set a node or note on them); otherwise after AUTO_FORGET_DAYS.
        fresh = conn.execute('SELECT MAX(last_seen) FROM mesh_hosts').fetchone()[0]
        if fresh and fresh >= (now - timedelta(hours=1)).strftime(TS_FMT):
            stale = [r['id'] for r in conn.execute(
                """SELECT id FROM phones WHERE source IN ('service', 'host') AND last_detected < ?
                   AND (last_detected < ? OR (node_name IS NULL AND note IS NULL))""", (now_s, forget))]
        else:
            stale = [r['id'] for r in conn.execute(
                "SELECT id FROM phones WHERE source IN ('service', 'host') AND last_detected < ?", (forget,))]
        for pid in stale:
            conn.execute('DELETE FROM phone_pings WHERE phone_id = ?', (pid,))
            conn.execute('DELETE FROM phones WHERE id = ?', (pid,))
    return len(found)


def parse_pbx_text(text):
    """Phones from pasted PBX output: [{ip, extension, name}].

    Understands `pjsip show contacts` (Contact: 1200/sip:1200@10.1.2.3:5060),
    `sip show peers` (1200/1200  10.1.2.3 ...), `pjsip show endpoints`
    contact lines, and comma/tab separated "extension,name,ip" lines.
    """
    out = {}
    for line in (text or '').splitlines():
        m = IP_RE.search(line)
        if not m or _ip_int(m.group(1)) is None:
            continue
        ip = m.group(1)
        if ip.startswith('127.') or ip == '0.0.0.0':
            continue
        ext = name = None
        sip = re.search(r'sips?:([^@\s;]+)@', line)
        if ',' in line or '\t' in line:
            fields = [f.strip().strip('"') for f in re.split(r'[,\t]', line)]
            for f in fields:
                if not f or f == ip or IP_RE.search(f):
                    continue
                if ext is None and re.fullmatch(r'\*?\d{2,8}', f):
                    ext = f
                elif name is None:
                    name = f
        elif sip:
            ext = sip.group(1)
        else:
            first = re.match(r'^\s*(?:Contact:\s*)?([\w.*-]+)', line)
            if first and first.group(1) != ip and not first.group(1).lower().startswith(('name', 'contact')):
                ext = first.group(1)
        if ext and '/' in ext:
            ext = ext.split('/')[0]
        out[ip] = {'ip': ip, 'extension': ext, 'name': name}
    return list(out.values())


def import_pbx(text):
    """Add/update phones from PBX text. Returns {'added', 'updated', 'phones'}."""
    ensure_tables()
    parsed = parse_pbx_text(text)
    now_s = datetime.now().strftime(TS_FMT)
    added = updated = 0
    with database.get_connection() as conn:
        for p in parsed:
            row = conn.execute('SELECT id FROM phones WHERE ip = ?', (p['ip'],)).fetchone()
            if row:
                conn.execute('''UPDATE phones SET extension = COALESCE(?, extension), name = COALESCE(?, name),
                                ignored = 0 WHERE id = ?''', (p['extension'], p['name'], row['id']))
                updated += 1
            else:
                conn.execute('''INSERT INTO phones (ip, name, extension, kind, source, added)
                                VALUES (?, ?, ?, 'phone', 'pbx', ?)''',
                             (p['ip'], p['name'] or (f"Ext {p['extension']}" if p['extension'] else None),
                              p['extension'], now_s))
                added += 1
    return {'added': added, 'updated': updated, 'phones': parsed}


def save_phone(data, phone_id=None):
    """Add (manual) or edit a phone. Returns its id; raises ValueError."""
    ensure_tables()
    fields = {}
    for key in ('name', 'extension', 'node_name', 'note', 'kind'):
        if key in data:
            v = (data.get(key) or '').strip() or None
            fields[key] = v.lower() if key == 'node_name' and v else v
    if fields.get('kind') and fields['kind'] not in ('phone', 'pbx'):
        raise ValueError('kind must be phone or pbx')
    for key in ('enabled', 'ignored'):
        if key in data:
            fields[key] = 1 if data.get(key) else 0
    with database.get_connection() as conn:
        if phone_id is None:
            ip = (data.get('ip') or '').strip()
            if _ip_int(ip) is None:
                raise ValueError('a valid IPv4 address is needed')
            row = conn.execute('SELECT id FROM phones WHERE ip = ?', (ip,)).fetchone()
            if row:
                phone_id = row['id']
                fields.setdefault('ignored', 0)
            else:
                cur = conn.execute("INSERT INTO phones (ip, kind, source, added) VALUES (?, 'phone', 'manual', ?)",
                                   (ip, datetime.now().strftime(TS_FMT)))
                phone_id = cur.lastrowid
        elif not conn.execute('SELECT 1 FROM phones WHERE id = ?', (phone_id,)).fetchone():
            raise KeyError(phone_id)
        if {'name', 'extension', 'kind'} & set(fields):
            fields['edited'] = 1  # detection no longer overwrites these
        if fields:
            conn.execute(f"UPDATE phones SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         [*fields.values(), phone_id])
    return phone_id


def delete_phone(phone_id):
    """Manual/PBX entries are deleted; auto-detected ones are hidden."""
    ensure_tables()
    with database.get_connection() as conn:
        row = conn.execute('SELECT source FROM phones WHERE id = ?', (phone_id,)).fetchone()
        if not row:
            return False
        if row['source'] in ('service', 'host'):
            conn.execute('UPDATE phones SET ignored = 1 WHERE id = ?', (phone_id,))
        else:
            conn.execute('DELETE FROM phone_pings WHERE phone_id = ?', (phone_id,))
            conn.execute('DELETE FROM phones WHERE id = ?', (phone_id,))
    return True


# --- pinging ----------------------------------------------------------------

def _node_up(node, now):
    seen = (node or {}).get('last_seen')
    if not (node or {}).get('is_active') or not seen:
        return False
    try:
        age = (now - datetime.strptime(seen[:19], TS_FMT)).total_seconds()
    except ValueError:
        return False
    return age <= max(2 * int(config.POLL_INTERVAL or 180), 600)


def _ping(ip):
    import rf_stats
    try:
        return rf_stats.ping_node(ip, count=config.PHONE_PING_COUNT, timeout=config.PHONE_PING_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Phone ping of %s failed: %s", ip, exc)
        return None


def _ping_many(ips):
    if not ips:
        return {}
    workers = max(1, min(16, len(ips)))
    try:  # see voip.ping_all_endpoints: GreenPool composes with eventlet's tpool
        import eventlet
        return dict(zip(ips, eventlet.GreenPool(workers).imap(_ping, ips)))
    except ImportError:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(ips, pool.map(_ping, ips)))


def _attached(phones, conn):
    node_rows = {r['name']: dict(r) for r in conn.execute('SELECT name, ip, is_active, last_seen FROM nodes')}
    node_ips = {n: r['ip'] for n, r in node_rows.items() if r['ip']}
    for p in phones:
        p['attached_node'] = p.get('node_name') or owner_node(p['ip'], node_ips)
        p['attached_by'] = 'set' if p.get('node_name') else 'lan' if p['attached_node'] else None
    return node_rows


def run(now=None, pinger=None):
    """Sync the list and ping every enabled phone once.

    Returns {'events', 'aborted', 'checked'}; never raises for one bad phone.
    """
    ensure_tables()
    now = now or datetime.now()
    if not config.PHONE_PING_ENABLED:
        return {'events': [], 'aborted': 'disabled', 'checked': 0}
    try:
        save_hosts(now)
        sync(now)
    except Exception as exc:
        logger.error("Phone list sync failed: %s", exc)
    if not gateway.is_reachable():
        return {'events': [], 'aborted': 'starting node unreachable', 'checked': 0}
    with database.get_connection() as conn:
        phones = [dict(r) for r in conn.execute('SELECT * FROM phones WHERE enabled = 1 AND ignored = 0')]
        nodes = _attached(phones, conn)
    if not phones:
        return {'events': [], 'aborted': None, 'checked': 0}
    results = (pinger or _ping_many)([p['ip'] for p in phones])
    failed = [p for p in phones if not _ok(results.get(p['ip']))]
    if failed and gateway.failure_is_ours():
        return {'events': [], 'aborted': 'starting node lost during the pings', 'checked': 0}
    now_s = now.strftime(TS_FMT)
    events, updates, samples = [], [], []
    for p in phones:
        res = results.get(p['ip']) or {}
        ok = _ok(res)
        avg, jitter, loss = res.get('avg'), res.get('jitter'), res.get('loss')
        if loss is None:
            loss = 0.0 if ok else 100.0
        node = p['attached_node']
        label = _label(p)
        fail_count, alarm, since = p['fail_count'], p['alarm'], p['since']
        checks, oks, last_ok = p['checks'] + 1, p['oks'], p['last_ok']
        if ok:
            oks += 1
            last_ok = now_s
            if alarm:
                events.append(('phone_restored', node or p['name'] or p['ip'],
                               f"Phone {label} answers ping again ({avg:.0f} ms)" if avg is not None
                               else f"Phone {label} answers ping again", 'info'))
            fail_count, alarm, since = 0, 0, None
        elif node and node in nodes and not _node_up(nodes[node], now):
            pass  # its node is down: the scanner reports that; keep the counters
        else:
            fail_count += 1
            if not alarm and fail_count >= config.PHONE_PING_FAILS:
                alarm, since = 1, now_s
                where = f"; node {node} is up" if node else ''
                events.append(('phone_unreachable', node or p['name'] or p['ip'],
                               f"Phone {label} not answering ping ({fail_count} scans in a row){where}", 'warning'))
        updates.append((int(ok), fail_count, alarm, since, now_s, last_ok, avg, jitter, loss, checks, oks, p['id']))
        samples.append((now_s, p['id'], int(ok), avg, jitter, loss))
    with database.get_connection() as conn:
        conn.executemany('''UPDATE phones SET ok = ?, fail_count = ?, alarm = ?, since = ?, last_check = ?,
                                last_ok = ?, last_avg = ?, last_jitter = ?, last_loss = ?, checks = ?, oks = ?
                            WHERE id = ?''', updates)
        conn.executemany('INSERT INTO phone_pings (timestamp, phone_id, ok, avg_ms, jitter_ms, loss) VALUES (?, ?, ?, ?, ?, ?)',
                         samples)
    out = []
    for kind, node, text, sev in events:
        database.log_event(kind, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': kind, 'node_name': node, 'details': text, 'severity': sev})
    return {'events': out, 'aborted': None, 'checked': len(phones)}


def ping_one(phone_id):
    """Ping one phone now (Ping button). Shown only: not stored, alarm counters untouched.

    Returns {ip, ok, avg, jitter, loss} or {aborted}; raises KeyError for an unknown id.
    """
    ensure_tables()
    with database.get_connection() as conn:
        row = conn.execute('SELECT ip FROM phones WHERE id = ?', (phone_id,)).fetchone()
    if not row:
        raise KeyError(phone_id)
    if not gateway.is_reachable():
        return {'ip': row['ip'], 'aborted': 'starting node unreachable'}
    res = _ping(row['ip']) or {}
    ok = _ok(res)
    if not ok and gateway.failure_is_ours():
        return {'ip': row['ip'], 'aborted': 'starting node lost during the ping'}
    return {'ip': row['ip'], 'ok': ok, 'avg': res.get('avg'), 'jitter': res.get('jitter'),
            'loss': res.get('loss') if res.get('loss') is not None else (0.0 if ok else 100.0)}


def _ok(res):
    return bool(res) and (res.get('loss') if res.get('loss') is not None else 100) < 100


def _label(p):
    parts = [p.get('name') or p['ip']]
    if p.get('extension'):
        parts.append(f"ext {p['extension']}")
    if p.get('name'):
        parts.append(p['ip'])
    return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]


# --- report -----------------------------------------------------------------

def report(hours=24, now=None):
    ensure_tables()
    now = now or datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        phones = [dict(r) for r in conn.execute('SELECT * FROM phones ORDER BY ignored, kind DESC, name, ip')]
        nodes = _attached(phones, conn)
        _, unresolved = detect(conn)
        names = {(p['name'] or '').lower() for p in phones}
        unresolved = [u for u in unresolved if u['name'].lower() not in names]
        host_names = {p['ip']: r['name'] for p in phones
                      for r in conn.execute('SELECT name FROM mesh_hosts WHERE ip = ?', (p['ip'],))}
        series = {}
        for r in conn.execute('SELECT phone_id, timestamp, ok, avg_ms, loss FROM phone_pings WHERE timestamp >= ? '
                              'ORDER BY timestamp', (since,)):
            series.setdefault(r['phone_id'], []).append({'t': r['timestamp'], 'ok': r['ok'], 'ms': r['avg_ms'],
                                                         'loss': r['loss']})
    counts = {}
    for p in phones:
        node = nodes.get(p['attached_node']) if p['attached_node'] else None
        p['node_known'] = node is not None
        p['node_up'] = _node_up(node, now) if node else None
        p['host_name'] = host_names.get(p['ip'])
        s = series.get(p['id'], [])
        p['series'] = s
        p['window_checks'] = len(s)
        p['window_pct'] = round(100.0 * sum(1 for x in s if x['ok']) / len(s), 1) if s else None
        ms = sorted(x['ms'] for x in s if x['ms'] is not None)
        p['window_median_ms'] = round(ms[len(ms) // 2], 1) if ms else None
        p['state'] = ('ignored' if p['ignored'] else 'off' if not p['enabled'] else
                      'alarm' if p['alarm'] else 'unchecked' if p['ok'] is None else
                      'up' if p['ok'] else 'node_down' if p['node_up'] is False else 'down')
        counts[p['state']] = counts.get(p['state'], 0) + 1
    last = max((p['last_check'] for p in phones if p['last_check']), default=None)
    return {'phones': phones, 'unresolved': unresolved, 'counts': counts, 'last_check': last, 'hours': hours,
            'enabled': config.PHONE_PING_ENABLED, 'ping_count': config.PHONE_PING_COUNT,
            'fails_to_alarm': config.PHONE_PING_FAILS}


def hosts(query='', limit=50):
    """Mesh hosts not yet on the list (for "add from mesh hosts"), with the attached node."""
    ensure_tables()
    q = f'%{(query or "").strip().lower()}%'
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            '''SELECT h.ip, h.name, h.last_seen FROM mesh_hosts h
               WHERE (lower(h.name) LIKE ? OR h.ip LIKE ?) AND h.ip NOT IN (SELECT ip FROM phones WHERE ignored = 0)
                 AND h.ip NOT IN (SELECT ip FROM nodes WHERE ip IS NOT NULL)
               ORDER BY h.name LIMIT ?''', (q, q, limit))]
        node_ips = {r['name']: r['ip'] for r in conn.execute('SELECT name, ip FROM nodes WHERE ip IS NOT NULL')}
    for r in rows:
        r['attached_node'] = owner_node(r['ip'], node_ips)
    return rows


def cleanup(now=None):
    ensure_tables()
    cutoff = ((now or datetime.now()) - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        conn.execute('DELETE FROM phone_pings WHERE timestamp < ?', (cutoff,))
        conn.execute('DELETE FROM mesh_hosts WHERE last_seen < ?', (cutoff,))

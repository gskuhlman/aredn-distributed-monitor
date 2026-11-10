"""
Incidents (TODO #51, #52, #86): one record per problem instead of a flood of
separate events, plus the operator's investigation notes on it.

Automatic incidents (`auto` = 1), opened from what the scanner stores:
  starting_node  `gateway_lost` opens it, `gateway_restored` resolves it
                 (event listener, so it works while scans are aborted).
  site_outage    every polled radio at an infrastructure site (2+ radios,
                 `INCIDENT_SITE_MIN_RADIOS`) failed its latest poll together.
                 When they answer again the uptime counters say why (#52):
                 half or more restarted during the outage = `power` ("all
                 radios rebooted together: power, not RF"), else `network`
                 (the site was cut off while its radios kept running).
  cutoff         `INCIDENT_CUTOFF_MIN_NODES`+ nodes went down in the same scan
                 and most of them sit behind one bridge link (spof.py, over
                 links seen within the last hour): root cause = that link.
  group          as many nodes down together with no single link to blame.
Nodes already covered by an open incident don't open another one. While an
incident is open, events for its member nodes are added to its timeline, and
notify.py suppresses their notifications (`covering()`), so the operator gets
one `incident_opened` notification and one `incident_resolved`.

Manual incidents (#86, also any auto one): symptoms, suspected cause, current
action, owner contacted, resolution, status open / monitoring / resolved /
closed, a timeline of notes and status changes, and links to change-log
entries and notes.

SQLite `incidents`, `incident_timeline`, `incident_links`. Local operator data,
never replicated.
"""

import json
import logging
from datetime import datetime, timedelta

import config
import database

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
STATUSES = ('open', 'monitoring', 'resolved', 'closed')
ACTIVE = ('open', 'monitoring')
TEXT_FIELDS = ('title', 'symptoms', 'suspected_cause', 'current_action', 'owner_contacted', 'resolution', 'owner')
KINDS = ('starting_node', 'site_outage', 'cutoff', 'group', 'manual')
REF_TYPES = ('change', 'note')
# Timeline event types attached to an open incident's member nodes.
MEMBER_EVENTS = ('node_offline', 'node_online', 'node_reboot', 'link_dropped', 'link_restored', 'node_degraded',
                 'one_way_link', 'one_way_link_cleared', 'service_down', 'service_restored', 'phone_unreachable',
                 'phone_restored', 'route_detour', 'route_restored')
MAX_TEXT = 4000

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open', severity TEXT DEFAULT 'warning',
            title TEXT NOT NULL, root_cause TEXT, cause TEXT,
            site TEXT, link_a TEXT, link_b TEXT, nodes TEXT,
            symptoms TEXT, suspected_cause TEXT, current_action TEXT, owner_contacted TEXT,
            resolution TEXT, owner TEXT,
            opened_at DATETIME NOT NULL, resolved_at DATETIME, closed_at DATETIME,
            updated_at DATETIME, last_event_id INTEGER DEFAULT 0)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS incident_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL, timestamp DATETIME NOT NULL,
            kind TEXT NOT NULL, event_id INTEGER, text TEXT, author TEXT)''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_incident_timeline ON incident_timeline(incident_id, timestamp)')
        conn.execute('''CREATE TABLE IF NOT EXISTS incident_links (
            incident_id INTEGER NOT NULL, ref_type TEXT NOT NULL, ref_id INTEGER NOT NULL,
            PRIMARY KEY (incident_id, ref_type, ref_id))''')
    _tables_ready = True


def _now(now=None):
    return (now or datetime.now()).strftime(TS_FMT)


def _ts(s):
    return datetime.strptime(s[:19], TS_FMT)


def _row(r, conn=None):
    d = dict(r)
    d['auto'] = bool(d.get('auto'))
    try:
        d['nodes'] = json.loads(d.get('nodes') or '[]')
    except ValueError:
        d['nodes'] = []
    return d


# ------------------------------------------------------------ records --

def create(data, now=None, conn=None):
    """A new incident (manual unless data['auto']). Returns it."""
    ensure_tables()
    data = data or {}
    title = str(data.get('title') or '').strip()[:300]
    if not title:
        raise ValueError('A title is required')
    kind = data.get('kind') or 'manual'
    if kind not in KINDS:
        raise ValueError('Unknown kind')
    status = data.get('status') or 'open'
    if status not in STATUSES:
        raise ValueError('Unknown status')
    ts = data.get('opened_at') or _now(now)
    nodes = sorted({str(n).strip().lower() for n in (data.get('nodes') or []) if str(n).strip()})
    vals = {'kind': kind, 'auto': 1 if data.get('auto') else 0, 'status': status,
            'severity': data.get('severity') or 'warning', 'title': title,
            'root_cause': data.get('root_cause'), 'cause': data.get('cause'), 'site': data.get('site'),
            'link_a': data.get('link_a'), 'link_b': data.get('link_b'), 'nodes': json.dumps(nodes),
            'opened_at': ts, 'updated_at': _now(now), 'last_event_id': data.get('last_event_id') or 0}
    for f in TEXT_FIELDS[1:]:
        if data.get(f):
            vals[f] = str(data[f])[:MAX_TEXT]

    def go(c):
        cur = c.execute(f"INSERT INTO incidents ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                        list(vals.values()))
        iid = cur.lastrowid
        c.execute('INSERT INTO incident_timeline (incident_id, timestamp, kind, text, author) VALUES (?, ?, ?, ?, ?)',
                  (iid, ts, 'status', 'Opened' + (f": {vals['root_cause']}" if vals.get('root_cause') else ''),
                   data.get('author') or ('monitor' if vals['auto'] else None)))
        return iid
    if conn is not None:
        return go(conn)
    with database.get_connection() as c:
        iid = go(c)
    return get(iid)


def get(iid):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM incidents WHERE id = ?', (int(iid),)).fetchone()
        if not r:
            return None
        d = _row(r)
        d['timeline'] = [dict(x) for x in conn.execute(
            'SELECT * FROM incident_timeline WHERE incident_id = ? ORDER BY timestamp, id', (d['id'],))]
        d['links'] = []
        for l in conn.execute('SELECT ref_type, ref_id FROM incident_links WHERE incident_id = ?', (d['id'],)):
            ref = {'ref_type': l['ref_type'], 'ref_id': l['ref_id']}
            try:
                if l['ref_type'] == 'change':
                    x = conn.execute('SELECT timestamp, node_name, description, category FROM change_log WHERE id = ?',
                                     (l['ref_id'],)).fetchone()
                else:
                    x = conn.execute('SELECT created_at AS timestamp, node_name, text AS description FROM notes '
                                     'WHERE id = ?', (l['ref_id'],)).fetchone()
                if x:
                    ref.update(dict(x))
            except Exception:
                pass
            d['links'].append(ref)
    return d


def list_incidents(status=None, days=30, limit=200):
    """Newest first. status: one status, 'active' (open + monitoring) or None."""
    ensure_tables()
    sql, args = 'SELECT * FROM incidents WHERE 1=1', []
    if status == 'active':
        sql += f" AND status IN ({', '.join('?' * len(ACTIVE))})"
        args += list(ACTIVE)
    elif status:
        sql += ' AND status = ?'
        args.append(status)
    if days and status != 'active':
        sql += ' AND (opened_at >= ? OR status IN (?, ?))'
        args += [(datetime.now() - timedelta(days=int(days))).strftime(TS_FMT), *ACTIVE]
    sql += ' ORDER BY opened_at DESC, id DESC LIMIT ?'
    args.append(int(limit))
    with database.get_connection() as conn:
        out = [_row(r) for r in conn.execute(sql, args)]
        for d in out:
            d['timeline_count'] = conn.execute('SELECT COUNT(*) FROM incident_timeline WHERE incident_id = ?',
                                               (d['id'],)).fetchone()[0]
    counts = {s: 0 for s in STATUSES}
    for d in out:
        counts[d['status']] = counts.get(d['status'], 0) + 1
    return {'incidents': out, 'counts': counts}


def update(iid, data, author=None, now=None):
    """Edit fields / status; each change adds a timeline line."""
    ensure_tables()
    data = data or {}
    cur = get(iid)
    if not cur:
        raise LookupError('Incident not found')
    sets, lines = {}, []
    for f in TEXT_FIELDS:
        if f in data and (data[f] or '') != (cur.get(f) or ''):
            v = str(data[f] or '').strip()[:MAX_TEXT]
            if f == 'title' and not v:
                raise ValueError('A title is required')
            sets[f] = v or None
            lines.append(f"{f.replace('_', ' ').capitalize()}: {v or '(cleared)'}")
    status = data.get('status')
    if status and status != cur['status']:
        if status not in STATUSES:
            raise ValueError('Unknown status')
        sets['status'] = status
        if status == 'resolved' and not cur.get('resolved_at'):
            sets['resolved_at'] = _now(now)
        if status == 'closed':
            sets['closed_at'] = _now(now)
            if not cur.get('resolved_at'):
                sets['resolved_at'] = _now(now)
        if status in ACTIVE:
            sets['closed_at'] = None
        lines.insert(0, f"Status {cur['status']} -> {status}")
    if not sets:
        return cur
    sets['updated_at'] = _now(now)
    with database.get_connection() as conn:
        conn.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     list(sets.values()) + [int(iid)])
        for line in lines:
            conn.execute('INSERT INTO incident_timeline (incident_id, timestamp, kind, text, author) '
                         'VALUES (?, ?, ?, ?, ?)', (int(iid), _now(now), 'status' if line.startswith('Status')
                                                    else 'edit', line, author))
    return get(iid)


def add_note(iid, text, author=None, now=None):
    ensure_tables()
    text = str(text or '').strip()[:MAX_TEXT]
    if not text:
        raise ValueError('Text is required')
    if not get(iid):
        raise LookupError('Incident not found')
    with database.get_connection() as conn:
        conn.execute('INSERT INTO incident_timeline (incident_id, timestamp, kind, text, author) VALUES (?, ?, ?, ?, ?)',
                     (int(iid), _now(now), 'note', text, author))
        conn.execute('UPDATE incidents SET updated_at = ? WHERE id = ?', (_now(now), int(iid)))
    return get(iid)


def link_ref(iid, ref_type, ref_id, remove=False):
    ensure_tables()
    if ref_type not in REF_TYPES:
        raise ValueError('ref_type is change or note')
    if not get(iid):
        raise LookupError('Incident not found')
    with database.get_connection() as conn:
        if remove:
            conn.execute('DELETE FROM incident_links WHERE incident_id = ? AND ref_type = ? AND ref_id = ?',
                         (int(iid), ref_type, int(ref_id)))
        else:
            conn.execute('INSERT OR IGNORE INTO incident_links (incident_id, ref_type, ref_id) VALUES (?, ?, ?)',
                         (int(iid), ref_type, int(ref_id)))
    return get(iid)


def delete(iid):
    ensure_tables()
    with database.get_connection() as conn:
        n = conn.execute('DELETE FROM incidents WHERE id = ?', (int(iid),)).rowcount
        conn.execute('DELETE FROM incident_timeline WHERE incident_id = ?', (int(iid),))
        conn.execute('DELETE FROM incident_links WHERE incident_id = ?', (int(iid),))
    return n > 0


# -------------------------------------------------- notification hook --

def covering(event):
    """The open auto incident whose members include the event's node (or its
    link peer), as 'incident #N: title', else None. Incident events
    themselves are never covered."""
    et = event.get('event_type') or ''
    if et.startswith('incident_'):
        return None
    try:
        ensure_tables()
        node = (event.get('node_name') or '').lower()
        import notify
        _, peer = notify.event_subject(event)
        with database.get_connection() as conn:
            rows = conn.execute("SELECT id, title, kind, nodes, opened_at FROM incidents WHERE auto = 1 AND status IN "
                                "('open', 'monitoring')").fetchall()
        for r in rows:
            if r['kind'] == 'starting_node' and et != 'gateway_lost':
                # Everything goes quiet while the collector is off the mesh.
                return f"incident #{r['id']}: {r['title']}"
            members = set(json.loads(r['nodes'] or '[]'))
            if (node and node in members) or (peer and peer in members):
                return f"incident #{r['id']}: {r['title']}"
    except Exception as exc:
        logger.debug('incident check failed: %s', exc)
    return None


# ------------------------------------------------------- detection --

def _open_auto(conn, kind, key_field=None, key=None):
    sql = "SELECT * FROM incidents WHERE auto = 1 AND kind = ? AND status IN ('open', 'monitoring')"
    args = [kind]
    if key_field:
        sql += f' AND {key_field} = ?'
        args.append(key)
    return [_row(r) for r in conn.execute(sql, args)]


def _log(event_type, node, details, severity):
    try:
        database.log_event(event_type, node, details, severity)
    except Exception as exc:
        logger.error('incident event failed: %s', exc)
    return {'event_type': event_type, 'node_name': node, 'details': details, 'severity': severity,
            'timestamp': _now()}


def on_event(event):
    """database.log_event listener: starting-node incidents."""
    et = event.get('event_type')
    if et not in ('gateway_lost', 'gateway_restored') or not config.INCIDENTS_ENABLED:
        return
    try:
        ensure_tables()
        if et == 'gateway_lost':
            with database.get_connection() as conn:
                if _open_auto(conn, 'starting_node'):
                    return
                iid = create({'kind': 'starting_node', 'auto': True, 'severity': 'critical',
                              'title': 'Starting node unreachable',
                              'root_cause': 'This collector lost its path onto the mesh (the starting node does not '
                                            'answer). Scans are aborted; nothing is judged down meanwhile.',
                              'cause': 'collector', 'opened_at': event.get('timestamp')}, conn=conn)
            _log('incident_opened', None, f'Incident #{iid}: Starting node unreachable', 'critical')
        else:
            with database.get_connection() as conn:
                open_ = _open_auto(conn, 'starting_node')
            for inc in open_:
                _resolve(inc['id'], 'Starting node answers again', event.get('timestamp'))
    except Exception as exc:
        logger.error('incident listener error: %s', exc)


def _resolve(iid, text, ts=None, cause=None, root=None):
    ts = ts or _now()
    with database.get_connection() as conn:
        inc = conn.execute('SELECT * FROM incidents WHERE id = ?', (iid,)).fetchone()
        sets = {'status': 'resolved', 'resolved_at': ts, 'updated_at': ts}
        if cause:
            sets['cause'] = cause
        if root:
            sets['root_cause'] = root
        conn.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     list(sets.values()) + [iid])
        conn.execute('INSERT INTO incident_timeline (incident_id, timestamp, kind, text, author) VALUES (?, ?, ?, ?, ?)',
                     (iid, ts, 'status', f'Resolved: {text}', 'monitor'))
    node = inc['link_a'] or (json.loads(inc['nodes'] or '[]') or [None])[0] if inc else None
    return _log('incident_resolved', node, f"Incident #{iid}: {inc['title'] if inc else ''}: {text}", 'info')


def poll_state(conn, now, hours=3):
    """{node: [(timestamp, reachable, uptime_seconds)] oldest first} of the
    scanner's own polls (not neighbour probes) in the last hours."""
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    out = {}
    for r in conn.execute('''SELECT node_name, timestamp, reachable, uptime_seconds FROM node_health_history
                             WHERE timestamp >= ? AND probe_via IS NULL ORDER BY timestamp''', (since,)):
        out.setdefault(r['node_name'], []).append((r['timestamp'], r['reachable'], r['uptime_seconds']))
    return out


def _down_sets(polls, now):
    """(down now, newly down this scan): latest poll failed (within 20 min);
    newly = the poll before it answered."""
    fresh = (now - timedelta(minutes=20)).strftime(TS_FMT)
    down, newly = set(), set()
    for node, ps in polls.items():
        t, ok, _ = ps[-1]
        if ok or t < fresh:
            continue
        down.add(node)
        if len(ps) >= 2 and ps[-2][1]:
            newly.add(node)
    return down, newly


def _rebooted_during(ps, since):
    """True / False / None: did the node restart after `since`, judged from
    the first answered poll after it (uptime shorter than the time since)."""
    for t, ok, up in ps:
        if t < since or not ok:
            continue
        if up is None:
            return None
        return up < (_ts(t) - _ts(since)).total_seconds() + 600
    return None


def after_scan(now=None, spof_report=None):
    """Open / update / resolve automatic incidents from the latest polls.
    Returns the events logged."""
    if not config.INCIDENTS_ENABLED:
        return []
    ensure_tables()
    now = now or datetime.now()
    events = []
    infra = database.get_infrastructure_nodes()
    sites = {}
    for n, s in infra.items():
        if s:
            sites.setdefault(s, []).append(n)
    with database.get_connection() as conn:
        polls = poll_state(conn, now)
        if _open_auto(conn, 'starting_node'):
            return []          # judged once the collector is back on the mesh
        down, newly = _down_sets(polls, now)
        open_auto = [_row(r) for r in conn.execute(
            "SELECT * FROM incidents WHERE auto = 1 AND status IN ('open', 'monitoring')")]
    covered = set()
    for inc in open_auto:
        covered |= set(inc['nodes'])

    # --- site outages (#52) ---
    for site, radios in sorted(sites.items()):
        polled = [r for r in radios if r in polls]
        if len(polled) < config.INCIDENT_SITE_MIN_RADIOS:
            continue
        open_site = [i for i in open_auto if i['kind'] == 'site_outage' and i['site'] == site]
        if all(r in down for r in polled):
            if open_site or not (set(polled) & newly):
                continue
            behind = sorted(newly - set(polled) - covered)
            title = f'Site {site}: all {len(polled)} radios unreachable'
            with database.get_connection() as conn:
                iid = create({'kind': 'site_outage', 'auto': True, 'severity': 'critical', 'site': site,
                              'title': title, 'nodes': polled + behind,
                              'root_cause': f'Every radio at {site} stopped answering in the same scan '
                                            f'({", ".join(polled)}). Power or the site\'s uplink; '
                                            'the uptime counters will tell when they come back.',
                              'opened_at': _now(now)}, conn=conn)
            covered |= set(polled) | set(behind)
            events.append(_log('incident_opened', polled[0], f'Incident #{iid}: {title}', 'critical'))
        elif open_site and all(r not in down for r in polled):
            inc = open_site[0]
            rebooted = [r for r in polled if _rebooted_during(polls.get(r, []), inc['opened_at'])]
            if len(rebooted) * 2 >= len(polled):
                cause, root = 'power', (f'Power at {site}: {len(rebooted)} of {len(polled)} radios restarted '
                                        'during the outage (uptime reset), so they lost power together, not RF.')
            else:
                cause, root = 'network', (f'{site} was cut off but its radios kept running (no uptime reset): '
                                          'the link or path to the site, not power.')
            events.append(_resolve(inc['id'], f'all radios at {site} answer again. {root}', _now(now), cause, root))
            covered -= set(inc['nodes'])

    # --- nodes cut off together (#51) ---
    fresh_down = sorted(newly - covered)
    if len(fresh_down) >= config.INCIDENT_CUTOFF_MIN_NODES:
        best = None
        try:
            rep = spof_report if spof_report is not None else __import__('spof').report(hours=1)
            for l in rep.get('links', []):
                behind = set(l.get('nodes_behind') or [])
                hit = behind & set(fresh_down)
                if len(hit) >= config.INCIDENT_CUTOFF_MIN_NODES and len(hit) * 10 >= len(behind) * 6:
                    if not best or len(hit) > best[1]:
                        best = (l, len(hit))
        except Exception as exc:
            logger.debug('spof for incidents failed: %s', exc)
        if best:
            l, n = best
            near = l['b'] if l['far_end'] == l['a'] else l['a']
            title = f"Link {near} - {l['far_end']} lost: {len(l['nodes_behind'])} node(s) cut off"
            members = sorted(set(l['nodes_behind']) | {l['far_end']})
            with database.get_connection() as conn:
                iid = create({'kind': 'cutoff', 'auto': True, 'title': title, 'link_a': l['far_end'],
                              'link_b': near, 'nodes': members,
                              'severity': 'critical' if l.get('behind_infra') else 'warning',
                              'root_cause': f"{n} nodes went down in the same scan and they sit behind the "
                                            f"{l['type']} link {near} - {l['far_end']}, the only path to them.",
                              'opened_at': _now(now)}, conn=conn)
            events.append(_log('incident_opened', l['far_end'], f'Incident #{iid}: {title}',
                               'critical' if l.get('behind_infra') else 'warning'))
        else:
            title = f'{len(fresh_down)} nodes unreachable together'
            with database.get_connection() as conn:
                iid = create({'kind': 'group', 'auto': True, 'title': title, 'nodes': fresh_down,
                              'root_cause': 'They stopped answering in the same scan; no single link explains it.',
                              'opened_at': _now(now)}, conn=conn)
            events.append(_log('incident_opened', fresh_down[0], f'Incident #{iid}: {title}', 'warning'))

    # --- cut-off / group incidents resolve when most members answer again ---
    with database.get_connection() as conn:
        open_auto = [_row(r) for r in conn.execute(
            "SELECT * FROM incidents WHERE auto = 1 AND status IN ('open', 'monitoring') AND kind IN ('cutoff', 'group')")]
    for inc in open_auto:
        members = [n for n in inc['nodes'] if n in polls]
        back = [n for n in members if n not in down]
        if members and len(back) * 10 >= len(members) * 8:
            events.append(_resolve(inc['id'], f'{len(back)} of {len(members)} nodes answer again', _now(now)))

    attach_events()
    return events


def attach_events():
    """Add member nodes' events to each active auto incident's timeline."""
    ensure_tables()
    with database.get_connection() as conn:
        for r in conn.execute("SELECT * FROM incidents WHERE status IN ('open', 'monitoring') OR "
                              "(status = 'resolved' AND resolved_at >= ?)",
                              ((datetime.now() - timedelta(hours=1)).strftime(TS_FMT),)).fetchall():
            inc = _row(r)
            if not inc['nodes']:
                continue
            ph = ', '.join('?' * len(inc['nodes']))
            types = ', '.join('?' * len(MEMBER_EVENTS))
            rows = conn.execute(f'''SELECT id, timestamp, event_type, node_name, details FROM events
                                    WHERE id > ? AND timestamp >= ? AND node_name IN ({ph})
                                      AND event_type IN ({types}) ORDER BY id LIMIT 500''',
                                [inc['last_event_id'] or 0, inc['opened_at'], *inc['nodes'], *MEMBER_EVENTS]).fetchall()
            for e in rows:
                conn.execute('INSERT INTO incident_timeline (incident_id, timestamp, kind, event_id, text) '
                             'VALUES (?, ?, ?, ?, ?)',
                             (inc['id'], e['timestamp'], 'event', e['id'],
                              f"{e['event_type'].replace('_', ' ')}: {e['node_name']}"
                              + (f" - {e['details']}" if e['details'] else '')))
            if rows:
                conn.execute('UPDATE incidents SET last_event_id = ? WHERE id = ?', (rows[-1]['id'], inc['id']))


def site_outages(start, end):
    """Auto site-outage incidents opened in [start, end) (weekly report)."""
    ensure_tables()
    with database.get_connection() as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM incidents WHERE kind = 'site_outage' AND opened_at >= ? AND opened_at < ? ORDER BY opened_at",
            (start, end))]


def install():
    database.add_event_listener(on_event)

"""
Channel-plan rollout tracker (Changes tab "Rollouts", Channel Map planner
"Start rollout…").

A rollout is a planner plan (or "name freq width" / "name off" what-if lines)
turned into an ordered checklist, one step per radio:
  clients (non-infrastructure radios) first, then sectors (infrastructure
  radios with non-infrastructure RF peers), then backbones (infrastructure
  radios whose RF peers are all infrastructure) last, so the radios that are
  reached through a link are moved before the radio at the near end of it.
Each step records the radio's RF peers when the rollout was created.

After every committed scan `check()` reads each pending radio's stored
frequency/width (nodes table, from sysinfo):
  pending  -> changed   the radio reports the planned channel (or its radio
                        off for an "off" step);
  pending  -> problem   the radio reports a channel that is neither the old
                        nor the planned one;
  changed  -> verified  every recorded peer that is not itself still waiting
                        in this rollout has a current RF link with it again;
  changed  -> problem   a peer link hasn't come back ROLLOUT_LINK_GRACE_MINUTES
                        after the change (back to verified once it does).
Events (transitions only): `rollout_step_changed` (info),
`rollout_link_missing` (warning), `rollout_complete` (info).

Nothing here changes a radio: it only watches what the scanner sees.
"""

import json
from datetime import datetime, timedelta

import channel_map
import config
import database
import infra_audit as ia

TS_FMT = '%Y-%m-%d %H:%M:%S'
ROLE_ORDER = {'client': 0, 'sector': 1, 'backbone': 2}
STEP_STATES = ('pending', 'changed', 'verified', 'problem', 'skipped')
FREQ_TOLERANCE_MHZ = 1.0

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS rollouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                plan_text TEXT,
                note TEXT,
                author TEXT,
                created_at DATETIME NOT NULL,
                finished_at DATETIME
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS rollout_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollout_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                node_name TEXT NOT NULL,
                role TEXT,
                site TEXT,
                old_frequency REAL,
                old_chanbw REAL,
                new_frequency REAL,
                new_chanbw REAL,
                turn_off INTEGER NOT NULL DEFAULT 0,
                peers TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                detail TEXT,
                missing TEXT,
                changed_at DATETIME,
                verified_at DATETIME,
                updated_at DATETIME
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_rollout_steps ON rollout_steps(rollout_id, seq)')
    _tables_ready = True


def _now():
    return datetime.now()


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _rf_peers(conn, name):
    """Current RF peers of a radio (either direction, not removed/dropped)."""
    rows = conn.execute('''
        SELECT source_node, target_node FROM links
        WHERE UPPER(link_type) = 'RF' AND status = 'good' AND ? IN (source_node, target_node)
    ''', (name,)).fetchall()
    return sorted({r[1] if r[0] == name else r[0] for r in rows} - {name})


def _cabled(conn, name):
    rows = conn.execute('''
        SELECT source_node, target_node FROM links
        WHERE UPPER(link_type) = 'DTD' AND status != 'removed' AND ? IN (source_node, target_node)
    ''', (name,)).fetchall()
    return {r[1] if r[0] == name else r[0] for r in rows}


def _role(name, peers, infra):
    if name not in infra:
        return 'client'
    return 'sector' if any(p not in infra for p in peers) else 'backbone'


def create(plan, name=None, note=None, author=None):
    """plan: what-if text, {name: spec} dict, or a list of planner radios
    ({name, new: {frequency, chanbw, off}} or {name, frequency, chanbw, off})."""
    ensure_tables()
    if isinstance(plan, list):
        spec = {}
        for r in plan:
            new = r.get('new') or r
            if not r.get('name') or (r.get('changed') is False):
                continue
            spec[r['name']] = ({'off': True} if (new.get('off') or r.get('off'))
                               else {'frequency': new.get('frequency'), 'chanbw': new.get('chanbw')})
        overrides = channel_map.parse_overrides(spec)
    else:
        overrides = channel_map.parse_overrides(plan)
    if not overrides:
        raise ValueError('no radios in the plan (expected "name freq width" or "name off" lines)')
    infra = database.get_infrastructure_nodes()
    now = _now().strftime(TS_FMT)
    with database.get_connection() as conn:
        nodes = {r['name'].lower(): dict(r) for r in conn.execute(
            'SELECT name, rf_frequency, rf_chanbw, rf_status FROM nodes')}
        steps = []
        for key, spec in overrides.items():
            node = nodes.get(key)
            if not node:
                raise ValueError(f'unknown radio: {key}')
            name_ = node['name']
            peers = [p for p in _rf_peers(conn, name_) if p not in _cabled(conn, name_)]
            steps.append({
                'node_name': name_, 'role': _role(name_, peers, infra),
                'site': infra.get(name_) or '',
                'old_frequency': _num(node['rf_frequency']), 'old_chanbw': _num(node['rf_chanbw']),
                'new_frequency': spec.get('frequency'), 'new_chanbw': spec.get('chanbw'),
                'turn_off': 1 if spec.get('off') else 0, 'peers': peers,
            })
        # Clients before the sector that serves them, backbones last; turned-off
        # radios first (nothing depends on them).
        steps.sort(key=lambda s: (0 if s['turn_off'] else 1, ROLE_ORDER.get(s['role'], 9), s['site'], s['node_name']))
        lines = [f"{s['node_name']} off" if s['turn_off'] else
                 f"{s['node_name']} {s['new_frequency']:g} {s['new_chanbw']:g}" if s['new_chanbw'] else
                 f"{s['node_name']} {s['new_frequency']:g}" for s in steps]
        cur = conn.execute('''INSERT INTO rollouts (name, status, plan_text, note, author, created_at)
                              VALUES (?, 'active', ?, ?, ?, ?)''',
                           ((name or f'Channel plan {now[:16]}')[:200], '\n'.join(lines),
                            (note or '')[:1000] or None, (author or '')[:100] or None, now))
        rid = cur.lastrowid
        for i, s in enumerate(steps, 1):
            conn.execute('''INSERT INTO rollout_steps (rollout_id, seq, node_name, role, site, old_frequency,
                                old_chanbw, new_frequency, new_chanbw, turn_off, peers, status, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
                         (rid, i, s['node_name'], s['role'], s['site'], s['old_frequency'], s['old_chanbw'],
                          s['new_frequency'], s['new_chanbw'], s['turn_off'], json.dumps(s['peers']), now))
    database.log_event('rollout_created', None,
                       f"Rollout #{rid}: {len(steps)} radio(s) — " + ', '.join(lines)[:400], 'info')
    return get(rid)


def _step_row(r):
    d = dict(r)
    d['peers'] = json.loads(d.get('peers') or '[]')
    d['missing'] = json.loads(d.get('missing') or '[]')
    d['turn_off'] = bool(d['turn_off'])
    for k in ('old', 'new'):
        f = d.get(f'{k}_frequency')
        d[f'{k}_channel'] = ia.freq_to_channel(f) if f else None
    return d


def _progress(steps):
    c = {s: 0 for s in STEP_STATES}
    for s in steps:
        c[s['status']] = c.get(s['status'], 0) + 1
    c['total'] = len(steps)
    c['done'] = c['verified'] + c['skipped']
    return c


def get(rollout_id):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM rollouts WHERE id = ?', (rollout_id,)).fetchone()
        if not r:
            return None
        steps = [_step_row(s) for s in conn.execute(
            'SELECT * FROM rollout_steps WHERE rollout_id = ? ORDER BY seq', (rollout_id,))]
    out = dict(r)
    out['steps'] = steps
    out['progress'] = _progress(steps)
    # The next step to do: the first one still pending.
    out['next_step'] = next((s['id'] for s in steps if s['status'] == 'pending'), None)
    return out


def list_rollouts(include_finished=True, limit=20):
    ensure_tables()
    with database.get_connection() as conn:
        ids = [r[0] for r in conn.execute(
            'SELECT id FROM rollouts ' + ('' if include_finished else "WHERE status = 'active' ")
            + 'ORDER BY (status = \'active\') DESC, id DESC LIMIT ?', (limit,))]
    return [get(i) for i in ids]


def set_status(rollout_id, status):
    ensure_tables()
    if status not in ('active', 'done', 'cancelled'):
        raise ValueError('status must be active, done or cancelled')
    with database.get_connection() as conn:
        n = conn.execute('UPDATE rollouts SET status = ?, finished_at = ? WHERE id = ?',
                         (status, None if status == 'active' else _now().strftime(TS_FMT), rollout_id)).rowcount
    return n > 0


def set_step(rollout_id, step_id, status, detail=None):
    """Operator override: skip a step, put it back to pending, or mark it
    changed by hand (when the scanner can't read the radio)."""
    ensure_tables()
    if status not in ('pending', 'skipped', 'changed'):
        raise ValueError('status must be pending, skipped or changed')
    now = _now().strftime(TS_FMT)
    with database.get_connection() as conn:
        n = conn.execute('''UPDATE rollout_steps SET status = ?, detail = ?, missing = NULL,
                                changed_at = CASE WHEN ? = 'changed' THEN ? ELSE NULL END,
                                verified_at = NULL, updated_at = ?
                            WHERE id = ? AND rollout_id = ?''',
                         (status, (detail or ('marked by hand' if status != 'pending' else None)),
                          status, now, now, step_id, rollout_id)).rowcount
    return n > 0


def delete(rollout_id):
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('DELETE FROM rollout_steps WHERE rollout_id = ?', (rollout_id,))
        return conn.execute('DELETE FROM rollouts WHERE id = ?', (rollout_id,)).rowcount > 0


def _matches(node, step):
    if step['turn_off']:
        return str(node.get('rf_status') or '').lower() == 'off' or not _num(node.get('rf_frequency'))
    f, w = _num(node.get('rf_frequency')), _num(node.get('rf_chanbw'))
    if f is None or step['new_frequency'] is None or abs(f - step['new_frequency']) > FREQ_TOLERANCE_MHZ:
        return False
    return w is None or step['new_chanbw'] is None or abs(w - step['new_chanbw']) < 0.5


def _on_old(node, step):
    f = _num(node.get('rf_frequency'))
    return f is None or step['old_frequency'] is None or abs(f - step['old_frequency']) <= FREQ_TOLERANCE_MHZ


def _link_back(conn, a, b, since):
    row = conn.execute('''
        SELECT 1 FROM links WHERE UPPER(link_type) = 'RF' AND status = 'good' AND last_seen >= ?
          AND ((source_node = ? AND target_node = ?) OR (source_node = ? AND target_node = ?)) LIMIT 1
    ''', (since, a, b, b, a)).fetchone()
    return row is not None


def check(now=None):
    """Advance every active rollout from what the last scan stored. Returns
    the events logged (dicts, for Socket.IO)."""
    ensure_tables()
    now = now or _now()
    now_s = now.strftime(TS_FMT)
    grace = timedelta(minutes=config.ROLLOUT_LINK_GRACE_MINUTES)
    events = []

    def log(kind, node, text, severity='info'):
        database.log_event(kind, node, text, severity)
        events.append({'timestamp': now_s, 'event_type': kind, 'node_name': node,
                       'details': text, 'severity': severity})

    with database.get_connection() as conn:
        nodes = {r['name']: dict(r) for r in conn.execute(
            'SELECT name, rf_frequency, rf_chanbw, rf_status FROM nodes')}
        active = [dict(r) for r in conn.execute("SELECT * FROM rollouts WHERE status = 'active'")]
        updates = []
        finished = []
        for ro in active:
            steps = [_step_row(s) for s in conn.execute(
                'SELECT * FROM rollout_steps WHERE rollout_id = ? ORDER BY seq', (ro['id'],))]
            waiting = {s['node_name'] for s in steps if s['status'] == 'pending'}
            off = {s['node_name'] for s in steps if s['turn_off']}
            for s in steps:
                node = nodes.get(s['node_name']) or {}
                status, detail, missing = s['status'], s.get('detail'), s['missing']
                changed_at, verified_at = s.get('changed_at'), s.get('verified_at')
                if status == 'skipped':
                    continue
                if status == 'pending' or (status == 'problem' and not changed_at):
                    if _matches(node, s):
                        status, changed_at, detail = 'changed', now_s, None
                        waiting.discard(s['node_name'])
                        target = 'radio off' if s['turn_off'] else f"{s['new_frequency']:g}/{(s['new_chanbw'] or 0):g} MHz"
                        log('rollout_step_changed', s['node_name'],
                            f"Rollout #{ro['id']} step {s['seq']}: {s['node_name']} now on {target}")
                    elif not _on_old(node, s):
                        if status != 'problem':
                            log('rollout_link_missing', s['node_name'],
                                f"Rollout #{ro['id']} step {s['seq']}: {s['node_name']} reports "
                                f"{node.get('rf_frequency')}/{node.get('rf_chanbw')} MHz, the plan says "
                                f"{s['new_frequency']}/{s['new_chanbw']}", 'warning')
                        status = 'problem'
                        detail = (f"reports {node.get('rf_frequency')}/{node.get('rf_chanbw')} MHz, "
                                  f"neither the old nor the planned channel")
                    else:
                        status, detail = 'pending', None
                if status in ('changed', 'verified', 'problem') and changed_at:
                    if s['turn_off']:
                        status, missing = 'verified', []
                        verified_at = verified_at or now_s
                    else:
                        expect = [p for p in s['peers'] if p not in waiting and p not in off]
                        missing = [p for p in expect if not _link_back(conn, s['node_name'], p, changed_at)]
                        late = now - datetime.strptime(changed_at, TS_FMT) >= grace
                        if not missing:
                            status, detail = 'verified', None
                            verified_at = verified_at or now_s
                        elif late:
                            if status != 'problem':
                                log('rollout_link_missing', s['node_name'],
                                    f"Rollout #{ro['id']} step {s['seq']}: RF link(s) from {s['node_name']} to "
                                    + ', '.join(missing) + f" not back {config.ROLLOUT_LINK_GRACE_MINUTES} min "
                                    "after the change", 'warning')
                            status, verified_at = 'problem', None
                            detail = 'not back: ' + ', '.join(missing)
                        else:
                            status, verified_at = 'changed', None
                            detail = 'waiting for: ' + ', '.join(missing)
                if (status, detail, missing, changed_at, verified_at) != (
                        s['status'], s.get('detail'), s['missing'], s.get('changed_at'), s.get('verified_at')):
                    updates.append((status, detail, json.dumps(missing or []), changed_at, verified_at,
                                    now_s, s['id']))
                s['status'] = status
            if steps and all(s['status'] in ('verified', 'skipped') for s in steps):
                finished.append(ro)
        for u in updates:
            conn.execute('''UPDATE rollout_steps SET status = ?, detail = ?, missing = ?, changed_at = ?,
                                verified_at = ?, updated_at = ? WHERE id = ?''', u)
        for ro in finished:
            conn.execute("UPDATE rollouts SET status = 'done', finished_at = ? WHERE id = ?", (now_s, ro['id']))
    for ro in finished:
        log('rollout_complete', None, f"Rollout #{ro['id']} ({ro['name']}) complete: every radio is on its "
            "planned channel and its RF links are back")
    return events

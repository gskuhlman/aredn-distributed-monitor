"""
Firmware upgrade planner (TODO #58; Setup page "Firmware Upgrades").

An upgrade reboots the node (a few minutes off the air), and every node whose
only way to the starting node runs through it goes dark with it. So the far
ends go first: a node is upgraded only after every node behind it.

`plan(target, names)`: over the current links and scan scope (`spof._graph`,
tunnels included: they stay up while a radio reboots) each node's dependents
are the nodes the starting node can no longer reach while it is down (a
brute-force BFS per node, like spof.py). A node's dependents always come
first; nodes in the same **wave** don't depend on each other and can be
upgraded together. Wave = 1 + the highest wave among its dependents in the
plan. The starting node, when it is in the plan, is last (the monitor loses
the mesh while it reboots). Nodes the last scan didn't reach go at the end.
Default target: the newest release on the mesh; default nodes: every polled
node in scope on an older release (nightlies and unknown versions are listed
as left out unless named).

A plan becomes a tracked rollout (`create`), ticked off after every scan by
`check()`: pending -> upgraded when the node reports the target version (or a
newer release); pending -> problem when it reports a version that is neither
the old one nor the target. A node upgraded while a node behind it in the
same rollout is still pending is noted (`firmware_out_of_order`). The rollout
is done when every step is upgraded or skipped. Events:
`firmware_step_upgraded`, `firmware_out_of_order` (info), `firmware_rollout_complete`.

Nothing here touches a node: it only watches what the scanner reads.
"""

import json
from datetime import datetime

import database
import drift
import spof

TS_FMT = '%Y-%m-%d %H:%M:%S'
STEP_STATES = ('pending', 'upgraded', 'problem', 'skipped')
CUTS_SHOWN = 30

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS firmware_rollouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                note TEXT,
                author TEXT,
                created_at DATETIME NOT NULL,
                finished_at DATETIME
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS firmware_rollout_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollout_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                wave INTEGER,
                node_name TEXT NOT NULL,
                old_version TEXT,
                seen_version TEXT,
                hops INTEGER,
                behind TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                detail TEXT,
                upgraded_at DATETIME,
                updated_at DATETIME
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_fw_steps ON firmware_rollout_steps(rollout_id, seq)')
    _tables_ready = True


# ------------------------------------------------------------- versions ---

def _release(version):
    p = drift.parse_firmware(version)
    return p[1] if p[0] == 'release' else None


def is_done(version, target):
    """The node runs the target, or a newer release than a release target."""
    if not version:
        return False
    if version.strip() == (target or '').strip():
        return True
    v, t = _release(version), _release(target)
    return bool(v and t and v >= t)


def needs(version, target):
    """An older release than a release target (nightlies / unknown: None = ask)."""
    v, t = _release(version), _release(target)
    if v is None or t is None:
        return None if not is_done(version, target) else False
    return v < t


def versions_seen():
    """[{version, count, release}] newest release first, then the rest."""
    with database.get_connection() as conn:
        rows = conn.execute('''SELECT firmware_version, COUNT(*) FROM nodes
                               WHERE is_active = 1 AND firmware_version IS NOT NULL AND firmware_version != ''
                               GROUP BY firmware_version''').fetchall()
    out = [{'version': v, 'count': n, 'release': _release(v) is not None} for v, n in rows]
    out.sort(key=lambda r: (not r['release'], [-x for x in (_release(r['version']) or ())], r['version']))
    return out


def latest_release():
    rel = [r['version'] for r in versions_seen() if r['release']]
    return rel[0] if rel else None


# ----------------------------------------------------------------- plan ---

def _hops(seed, adj):
    dist = {seed: 0}
    frontier = [seed]
    while frontier:
        nxt = []
        for n in frontier:
            for p in adj.get(n, ()):
                if p not in dist:
                    dist[p] = dist[n] + 1
                    nxt.append(p)
        frontier = nxt
    return dist


def plan(target=None, names=None, infrastructure_only=False):
    """The ordered plan (not stored). names: nodes to upgrade (default: every
    polled node in scope on an older release)."""
    target = (target or latest_release() or '').strip()
    if not target:
        raise ValueError('no firmware version to upgrade to: none reported yet')
    seed, scope, adj, _pairs, polled, by_name = spof._graph(exclude_tunnels=False)
    infra = set(database.get_infrastructure_nodes() or [])
    left_out, already = [], []
    if names:
        wanted = []
        for n in dict.fromkeys(x.strip() for x in names if x and x.strip()):
            node = by_name.get(n) or next((v for k, v in by_name.items() if k.lower() == n.lower()), None)
            if not node:
                left_out.append({'node': n, 'reason': 'not a known node'})
            elif is_done(node.get('firmware_version'), target):
                already.append(node['name'])
            else:
                wanted.append(node['name'])
    else:
        wanted = []
        for n in sorted(polled):
            node = by_name.get(n) or {}
            if infrastructure_only and n not in infra:
                continue
            v = node.get('firmware_version')
            need = needs(v, target)
            if need:
                wanted.append(n)
            elif need is None and not is_done(v, target):
                left_out.append({'node': n, 'version': v,
                                 'reason': 'nightly build' if drift.parse_firmware(v)[0] == 'nightly'
                                 else 'version not reported'})
            else:
                already.append(n)

    reach_all = spof._reach(seed, adj) if seed else set()
    hops = _hops(seed, adj) if seed else {}
    plan_set = set(wanted)
    behind = {}
    for n in wanted:
        if seed and n in reach_all:
            behind[n] = reach_all - spof._reach(seed, adj, skip_node=n) - {n} if n != seed else reach_all - {n}
    reached = [n for n in wanted if n in behind]
    unreached = sorted(n for n in wanted if n not in behind)

    wave = {}
    for n in sorted(reached, key=lambda x: len(behind[x])):
        before = [wave[d] for d in behind[n] & plan_set if d in wave]
        wave[n] = 1 + max(before, default=0)

    def row(n, w):
        node = by_name.get(n) or {}
        cut = sorted(d for d in behind.get(n, ()) if d in polled)
        return {'node': n, 'wave': w, 'version': node.get('firmware_version'),
                'hops': hops.get(n), 'is_infrastructure': n in infra, 'model': node.get('model'),
                'is_starting_node': n == seed,
                'behind_count': len(cut), 'behind': cut[:CUTS_SHOWN],
                'behind_in_plan': sorted(d for d in behind.get(n, ()) if d in plan_set)}

    steps = [row(n, wave[n]) for n in sorted(reached, key=lambda x: (wave[x], -(hops.get(x) or 0), x))]
    last = (max(wave.values()) if wave else 0) + 1
    steps += [dict(row(n, None), unreached=True) for n in unreached]
    for i, s in enumerate(steps, 1):
        s['seq'] = i
    waves = {}
    for s in steps:
        if s['wave']:
            waves.setdefault(s['wave'], []).append(s['node'])
    return {'target': target, 'starting_node': seed, 'steps': steps,
            'waves': [{'wave': w, 'nodes': ns} for w, ns in sorted(waves.items())],
            'unreached_wave': last if unreached else None,
            'already': sorted(already), 'left_out': left_out,
            'versions': versions_seen()}


# ------------------------------------------------------------- rollouts ---

def _now():
    return datetime.now()


def create(target=None, names=None, name=None, note=None, author=None, infrastructure_only=False):
    p = plan(target, names, infrastructure_only)
    if not p['steps']:
        raise ValueError('nothing to upgrade: every chosen node already runs ' + p['target'])
    ensure_tables()
    now = _now().strftime(TS_FMT)
    with database.get_connection() as conn:
        rid = conn.execute('''INSERT INTO firmware_rollouts (name, target, note, author, created_at)
                              VALUES (?, ?, ?, ?, ?)''',
                           ((name or f"Upgrade to {p['target']}")[:120], p['target'],
                            (note or '')[:500] or None, (author or '')[:60] or None, now)).lastrowid
        for s in p['steps']:
            conn.execute('''INSERT INTO firmware_rollout_steps
                                (rollout_id, seq, wave, node_name, old_version, hops, behind, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                         (rid, s['seq'], s['wave'], s['node'], s['version'], s['hops'],
                          json.dumps(s['behind_in_plan']), now))
    return get(rid)


def _step(r):
    d = dict(r)
    d['behind'] = json.loads(d.get('behind') or '[]')
    return d


def _progress(steps):
    c = {s: 0 for s in STEP_STATES}
    for s in steps:
        c[s['status']] = c.get(s['status'], 0) + 1
    c['total'] = len(steps)
    c['done'] = c['upgraded'] + c['skipped']
    return c


def get(rollout_id):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM firmware_rollouts WHERE id = ?', (rollout_id,)).fetchone()
        if not r:
            return None
        steps = [_step(s) for s in conn.execute(
            'SELECT * FROM firmware_rollout_steps WHERE rollout_id = ? ORDER BY seq', (rollout_id,))]
        versions = {row[0]: row[1] for row in conn.execute('SELECT name, firmware_version FROM nodes')}
    for s in steps:
        s['version_now'] = versions.get(s['node_name'])
    out = dict(r)
    out['steps'] = steps
    out['progress'] = _progress(steps)
    pending = [s for s in steps if s['status'] in ('pending', 'problem')]
    nxt = min((s['wave'] or 10 ** 6 for s in pending), default=None)
    out['next_wave'] = None if nxt is None or nxt == 10 ** 6 else nxt
    out['next_nodes'] = [s['node_name'] for s in pending if (s['wave'] or 10 ** 6) == nxt] if pending else []
    return out


def list_rollouts(include_finished=True, limit=20):
    ensure_tables()
    with database.get_connection() as conn:
        ids = [r[0] for r in conn.execute(
            'SELECT id FROM firmware_rollouts ' + ('' if include_finished else "WHERE status = 'active' ")
            + "ORDER BY (status = 'active') DESC, id DESC LIMIT ?", (limit,))]
    return [get(i) for i in ids]


def set_status(rollout_id, status):
    ensure_tables()
    if status not in ('active', 'done', 'cancelled'):
        raise ValueError('status must be active, done or cancelled')
    with database.get_connection() as conn:
        return conn.execute('UPDATE firmware_rollouts SET status = ?, finished_at = ? WHERE id = ?',
                            (status, None if status == 'active' else _now().strftime(TS_FMT),
                             rollout_id)).rowcount > 0


def set_step(rollout_id, step_id, status, detail=None):
    """Operator override: skip, back to pending, or upgraded by hand."""
    ensure_tables()
    if status not in ('pending', 'skipped', 'upgraded'):
        raise ValueError('status must be pending, skipped or upgraded')
    now = _now().strftime(TS_FMT)
    with database.get_connection() as conn:
        return conn.execute('''UPDATE firmware_rollout_steps SET status = ?, detail = ?, updated_at = ?,
                                   upgraded_at = CASE WHEN ? = 'upgraded' THEN ? ELSE NULL END
                               WHERE id = ? AND rollout_id = ?''',
                            (status, detail or ('marked by hand' if status != 'pending' else None), now,
                             status, now, step_id, rollout_id)).rowcount > 0


def delete(rollout_id):
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('DELETE FROM firmware_rollout_steps WHERE rollout_id = ?', (rollout_id,))
        return conn.execute('DELETE FROM firmware_rollouts WHERE id = ?', (rollout_id,)).rowcount > 0


def check(now=None):
    """Tick off steps from the firmware versions the last scan stored.
    Returns the events logged (for Socket.IO)."""
    ensure_tables()
    now_s = (now or _now()).strftime(TS_FMT)
    events = []

    def log(kind, node, text, severity='info'):
        database.log_event(kind, node, text, severity)
        events.append({'timestamp': now_s, 'event_type': kind, 'node_name': node,
                       'details': text, 'severity': severity})

    finished = []
    with database.get_connection() as conn:
        versions = {r[0]: r[1] for r in conn.execute('SELECT name, firmware_version FROM nodes')}
        for ro in [dict(r) for r in conn.execute("SELECT * FROM firmware_rollouts WHERE status = 'active'")]:
            steps = [_step(s) for s in conn.execute(
                'SELECT * FROM firmware_rollout_steps WHERE rollout_id = ? ORDER BY seq', (ro['id'],))]
            pending = {s['node_name'] for s in steps if s['status'] in ('pending', 'problem')}
            for s in steps:
                if s['status'] not in ('pending', 'problem'):
                    continue
                v = versions.get(s['node_name'])
                status, detail = s['status'], s.get('detail')
                if is_done(v, ro['target']):
                    status, detail = 'upgraded', None
                    pending.discard(s['node_name'])
                    log('firmware_step_upgraded', s['node_name'],
                        f"Firmware rollout #{ro['id']} step {s['seq']}: {s['node_name']} now runs {v}")
                    early = sorted(set(s['behind']) & pending)
                    if early:
                        detail = 'upgraded before ' + ', '.join(early) + ' (behind it)'
                        log('firmware_out_of_order', s['node_name'],
                            f"Firmware rollout #{ro['id']}: {s['node_name']} was upgraded before "
                            + ', '.join(early) + ', which reach the mesh through it')
                elif v and s.get('old_version') and v != s['old_version']:
                    status, detail = 'problem', f"now reports {v}: neither {s['old_version']} nor {ro['target']}"
                elif status == 'problem':
                    status, detail = 'pending', None
                if (status, detail, v) != (s['status'], s.get('detail'), s.get('seen_version')):
                    conn.execute('''UPDATE firmware_rollout_steps SET status = ?, detail = ?, seen_version = ?,
                                        upgraded_at = CASE WHEN ? = 'upgraded' THEN ? ELSE upgraded_at END,
                                        updated_at = ? WHERE id = ?''',
                                 (status, detail, v, status, now_s, now_s, s['id']))
                s['status'] = status
            if steps and all(s['status'] in ('upgraded', 'skipped') for s in steps):
                conn.execute("UPDATE firmware_rollouts SET status = 'done', finished_at = ? WHERE id = ?",
                             (now_s, ro['id']))
                finished.append(ro)
    for ro in finished:
        log('firmware_rollout_complete', None,
            f"Firmware rollout #{ro['id']} ({ro['name']}) complete: every node runs {ro['target']}")
    return events

"""
Cleanup of stale and junk node entries (TODO #24).

Lists nodes that only add noise to scans and reports, each with a reason and
a suggested action, and applies the operator's explicit choices:

- fake_name: a bare number / IP fragment such as "10" (failed polls of the
  starting node's IP recorded by older scanner code). Delete.
- placeholder: a ``mac-…`` name a neighbor reported without a hostname.
  Exclude (a delete comes back while the neighbor still lists it).
- legacy_tunnel_only, pings_no_http, never_resolves: unreachable from here
  for a known reason (unreachable.py). Exclude.
- stale: no answer in over ``stale_days``. Exclude while a neighbor still
  lists it and the scanner keeps polling it; otherwise delete the record.
- excluded: already an 'exclude' scan boundary (offered for un-exclude).

Never suggested, and refused by the actions: infrastructure, Permanent/Event
tagged and selected nodes, and the starting node. An exclude that would cut
healthy nodes off from the starting node (they are only reached through it,
e.g. kc0wkp-ar750 for its WireGuard clients) is refused unless forced.

Analysis is read-only; only exclude(), unexclude() and delete() write.
"""

import logging
import re
from datetime import datetime, timedelta

import database
import unreachable

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
STALE_DAYS = 30
WINDOW_HOURS = 72   # failed-poll window handed to the unreachable classifier

REASONS = {
    'fake_name': ('Fake name', 'delete',
                  'A bare number or IP fragment, not a node. Older scanner code recorded failed polls of '
                  'the starting node\'s IP under the first octet.'),
    'placeholder': ('MAC-only placeholder', 'exclude',
                    'A neighbor lists this radio only by MAC address; it can never be polled. Deleting it '
                    'does not help while the neighbor still lists it, so exclude it.'),
    'legacy_tunnel_only': ('Legacy tunnel only', 'exclude',
                           'Only reported over old-style (vtun) tunnel links, whose routes do not reach this '
                           'collector. Every scan waits for it to time out.'),
    'pings_no_http': ('Up, no HTTP from here', 'exclude',
                      'Mesh probes reach it but the scanner\'s sysinfo request never does.'),
    'never_resolves': ('Name does not resolve', 'exclude',
                       'Polls fail at name resolution (DNS) before reaching the node.'),
    'stale': ('Stale', 'delete',
              'No answer in a long time.'),
    'excluded': ('Already excluded', 'unexclude',
                 'Excluded from scanning. Un-exclude it if it should be polled again.'),
}
# Classifier causes that become cleanup reasons (others are real problems).
CAUSE_REASONS = {'placeholder', 'legacy_tunnel_only', 'pings_no_http', 'never_resolves', 'stale'}
PRIORITY = ['fake_name', 'placeholder', 'legacy_tunnel_only', 'never_resolves', 'pings_no_http',
            'stale', 'excluded']

FAKE_NAME = re.compile(r'[\d.]+')


def is_fake_name(name):
    return bool(FAKE_NAME.fullmatch(name or ''))


def starting_node_name():
    """Name of the configured starting node (its IP looked up, else depth 0)."""
    import scanner
    host = scanner.url_host(scanner.get_starting_node_url())
    name = database.get_node_name_by_ip(host) if is_fake_name(host) else host.split('.')[0]
    with database.get_connection() as conn:
        if name and conn.execute('SELECT 1 FROM nodes WHERE name = ?', (name,)).fetchone():
            return name
        # localnode.local.mesh and the like: the node found at depth 0.
        row = conn.execute('SELECT name FROM nodes WHERE scan_depth = 0 LIMIT 1').fetchone()
        return row['name'] if row else name


def _protections():
    """{name: [why]} for nodes the cleanup must never touch."""
    out = {}

    def add(names, why):
        for n in names:
            out.setdefault(n, []).append(why)

    add(database.get_infrastructure_node_names(), 'infrastructure')
    tags = database.get_tagged_node_names()
    add(tags.get('permanent', []), 'Permanent')
    add(tags.get('event', []), 'Event')
    add(database.get_selected_node_names(), 'selected')
    seed = starting_node_name()
    if seed:
        add([seed], 'starting node')
    return out


def _graph(conn):
    """Current discovery graph: polled node -> neighbors it reports."""
    graph = {}
    for r in conn.execute("SELECT source_node, target_node FROM links WHERE status = 'good'"):
        graph.setdefault(r['source_node'], set()).add(r['target_node'])
    return graph


def _reach(graph, seed, skip):
    seen, todo = {seed}, [seed]
    while todo:
        for nxt in graph.get(todo.pop(), ()):
            if nxt not in seen and nxt not in skip:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def cut_off_by(names, conn=None):
    """{name: [healthy nodes only reached through it]} if each were excluded
    (on top of the existing exclude boundaries)."""
    if conn is None:
        with database.get_connection() as c:
            return cut_off_by(names, c)
    seed = starting_node_name()
    if not seed:
        return {n: [] for n in names}
    graph = _graph(conn)
    excluded = {n for n, m in database.get_scan_boundaries().items() if m == 'exclude'}
    healthy = {r['name'] for r in conn.execute('SELECT name FROM nodes WHERE is_active = 1')}
    base = _reach(graph, seed, excluded) & healthy
    return {n: sorted((base - _reach(graph, seed, excluded | {n})) - {n}) for n in names}


def candidates(stale_days=STALE_DAYS, hours=WINDOW_HOURS):
    """Cleanup candidates grouped by reason, with the facts to decide on each."""
    end = datetime.now()
    start = end - timedelta(hours=hours)
    stale_cutoff = (end - timedelta(days=stale_days)).strftime(TS_FMT)
    causes = unreachable.classify(start.strftime(TS_FMT), end.strftime(TS_FMT))
    boundaries = database.get_scan_boundaries()

    found = {}   # name -> (reason, evidence)

    def offer(name, reason, evidence):
        if name not in found or PRIORITY.index(reason) < PRIORITY.index(found[name][0]):
            found[name] = (reason, evidence)

    with database.get_connection() as conn:
        nodes = {r['name']: dict(r) for r in conn.execute(
            'SELECT name, ip, last_seen, is_active FROM nodes')}
        health = {r['node_name']: dict(r) for r in conn.execute('''
            SELECT node_name, COUNT(*) AS rows_, MAX(CASE WHEN reachable = 1 AND probe_via IS NULL
                   THEN timestamp END) AS last_ok, MAX(timestamp) AS last_poll
            FROM node_health_history GROUP BY node_name''')}
        reporters = {}
        link_counts = {}
        for r in conn.execute('''SELECT source_node, target_node, link_type FROM links
                                 WHERE status != 'removed' '''):
            reporters.setdefault(r['target_node'], set()).add(f"{r['source_node']} ({r['link_type']})")
            for n in (r['source_node'], r['target_node']):
                link_counts[n] = link_counts.get(n, 0) + 1

        for name in set(nodes) | set(health):
            if is_fake_name(name):
                offer(name, 'fake_name', [f'"{name}" is not a node name'])
        for name, res in causes.items():
            if res['cause'] in CAUSE_REASONS:
                offer(name, res['cause'], res['evidence'])
        for name, n in nodes.items():
            if n['last_seen'] and n['last_seen'] < stale_cutoff and not n['is_active']:
                offer(name, 'stale', [f'last seen {n["last_seen"]}'])
        for name, mode in boundaries.items():
            if mode == 'exclude':
                offer(name, 'excluded', ['scan boundary: exclude'])

        protected = _protections()
        cut = cut_off_by([n for n, (r, _) in found.items() if REASONS[r][1] == 'exclude'
                          and n not in protected], conn)

    rows = []
    recent_poll = (end - timedelta(hours=hours)).strftime(TS_FMT)
    for name, (reason, evidence) in found.items():
        label, action, detail = REASONS[reason]
        node = nodes.get(name, {})
        h = health.get(name, {})
        reps = sorted(reporters.get(name, ()))
        # A stale node the scanner still polls (a neighbor lists it) comes
        # back after a delete; exclude it instead.
        if reason == 'stale' and reps and (h.get('last_poll') or '') >= recent_poll:
            action = 'exclude'
        why_protected = protected.get(name, [])
        cuts = cut.get(name, [])
        if why_protected:
            action = 'keep'
        elif action == 'exclude' and boundaries.get(name) == 'exclude':
            reason, evidence = 'excluded', evidence + ['scan boundary: exclude']
            label, action, detail = REASONS[reason]
        elif action == 'exclude' and cuts:
            action = 'review'
        rows.append({
            'name': name,
            'reason': reason,
            'reason_label': label,
            'detail': detail,
            'evidence': evidence,
            'suggested_action': action,
            'in_nodes': bool(node),
            'is_active': bool(node.get('is_active')),
            'ip': node.get('ip'),
            'last_seen': node.get('last_seen'),
            'last_answered': h.get('last_ok') or (node.get('last_seen') if node else None),
            'last_poll': h.get('last_poll'),
            'health_rows': h.get('rows_', 0),
            'reached_through': reps,
            'link_count': link_counts.get(name, 0),
            'scan_boundary': boundaries.get(name),
            'protected': why_protected,
            'cuts_off': cuts,
        })
    order = {r: i for i, r in enumerate(PRIORITY)}
    rows.sort(key=lambda r: (order[r['reason']], r['name']))

    groups = []
    for reason in PRIORITY:
        members = [r for r in rows if r['reason'] == reason]
        if members:
            label, action, detail = REASONS[reason]
            groups.append({'reason': reason, 'label': label, 'action': action, 'detail': detail,
                           'nodes': members})
    return {
        'generated_at': end.strftime(TS_FMT),
        'stale_days': stale_days,
        'window_hours': hours,
        'starting_node': starting_node_name(),
        'counts': {g['reason']: len(g['nodes']) for g in groups},
        'groups': groups,
    }


def _clean_names(names):
    out = []
    for n in names or []:
        n = str(n or '').strip().lower()
        if n and n not in out:
            out.append(n)
    return out


def exclude(names, force=False):
    """Make each name an 'exclude' scan boundary. Protected nodes are always
    refused; nodes that would cut healthy nodes off are refused unless forced."""
    names = _clean_names(names)
    protected = _protections()
    cuts = cut_off_by([n for n in names if n not in protected])
    done, refused = [], {}
    for name in names:
        if name in protected:
            refused[name] = 'protected: ' + ', '.join(protected[name])
        elif cuts.get(name) and not force:
            refused[name] = f'would cut off {len(cuts[name])} healthy node(s): ' + ', '.join(cuts[name][:8])
        else:
            database.set_scan_boundary(name, 'exclude')
            done.append(name)
    if done:
        logger.info('Cleanup: excluded %s', ', '.join(done))
    return {'excluded': done, 'refused': refused}


def unexclude(names):
    """Remove the 'exclude' scan boundary (other boundary modes are left alone)."""
    boundaries = database.get_scan_boundaries()
    done = []
    for name in _clean_names(names):
        if boundaries.get(name) == 'exclude':
            database.set_scan_boundary(name, None)
            done.append(name)
    if done:
        logger.info('Cleanup: un-excluded %s', ', '.join(done))
    return {'unexcluded': done}


def delete(names, force=False):
    """Delete node records (database.delete_node: node, links, services, link
    history) plus the node's health history, so junk stops showing in reports.
    Protected nodes are refused; active nodes are refused unless forced (the
    next scan would only re-create them)."""
    names = _clean_names(names)
    protected = _protections()
    with database.get_connection() as conn:
        active = {r['name'] for r in conn.execute('SELECT name FROM nodes WHERE is_active = 1')}
    done, refused, health_rows = [], {}, 0
    for name in names:
        if name in protected:
            refused[name] = 'protected: ' + ', '.join(protected[name])
            continue
        if name in active and not force:
            refused[name] = 'active: the next scan would re-create it'
            continue
        database.delete_node(name)
        with database.get_connection() as conn:
            health_rows += conn.execute('DELETE FROM node_health_history WHERE node_name = ?',
                                        (name,)).rowcount
        done.append(name)
    if done:
        logger.info('Cleanup: deleted %s (%d health rows)', ', '.join(done), health_rows)
    return {'deleted': done, 'refused': refused, 'health_rows_deleted': health_rows}

"""
Distributed node status (Admin page "Collectors & status", /api/status/distributed).

Derives each node's status from append-only observations and collector
heartbeats, following the rules in CLAUDE.md. Nothing is ever written: no
shared `node:<name>` documents.

`derive()` is pure (tests feed it synthetic collectors). A collector is
healthy when its newest heartbeat is within HEALTHY_SECONDS and that cycle
wasn't aborted (it lost its starting node: its silence is no evidence). Per
node, each healthy collector's observations in the last RECENT_SECONDS say
whether it sees the node (any reachable poll) or not (only failed polls):

  UP                   seen by every healthy collector that tried it
  LOCALLY_UNREACHABLE  seen by another collector, not by the local one
  PARTITIONED          healthy collectors (other than only the local one)
                       disagree
  DOWN_CONFIRMED       nobody sees it and at least DOWN_MIN_COLLECTORS healthy
                       collectors each failed DOWN_MIN_FAILS polls recently
  STALE                nobody sees it, but the evidence is too thin to call it
                       down (one collector, or few failures)
  UNKNOWN              no observations in the window at all

Never DOWN_CONFIRMED from the local collector alone.

Sources: CouchDB (`node_observation`, `collector_heartbeat`) when COUCH_URL is
set; otherwise the local SQLite history (node_health_history scanner polls as
this collector's observations, collector_health / the last scan as its
heartbeat), which can only say UP / STALE / UNKNOWN.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
import database

TS_FMT = '%Y-%m-%d %H:%M:%S'
STATES = ('UP', 'LOCALLY_UNREACHABLE', 'PARTITIONED', 'STALE', 'DOWN_CONFIRMED', 'UNKNOWN')


def _parse(ts):
    """ISO 'Z' timestamps (CouchDB) or naive local ones (SQLite) -> aware UTC."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.astimezone(timezone.utc)
    if not ts:
        return None
    s = str(ts).strip()
    try:
        if s.endswith('Z') or '+' in s[10:]:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)
        return datetime.strptime(s[:19].replace('T', ' '), TS_FMT).astimezone(timezone.utc)
    except ValueError:
        return None


def healthy_collectors(heartbeats, now, healthy_seconds):
    """{collector_id: newest heartbeat} for collectors that count."""
    newest = {}
    for h in heartbeats:
        at = _parse(h.get('observed_at'))
        cid = h.get('collector_id')
        if not cid or not at:
            continue
        if cid not in newest or at > newest[cid]['at']:
            newest[cid] = {'at': at, 'aborted': bool(h.get('scan_aborted')), 'site': h.get('collector_site')}
    return {cid: h for cid, h in newest.items()
            if not h['aborted'] and (now - h['at']).total_seconds() <= healthy_seconds}, newest


def derive(observations, heartbeats, now=None, local_collector=None, recent_seconds=None,
           healthy_seconds=None, down_min_collectors=None, down_min_fails=None):
    now = now or datetime.now(timezone.utc)
    recent_seconds = recent_seconds or config.DIST_RECENT_SECONDS
    healthy_seconds = healthy_seconds or config.DIST_HEALTHY_SECONDS
    down_min_collectors = down_min_collectors or config.DIST_DOWN_MIN_COLLECTORS
    down_min_fails = down_min_fails or config.DIST_DOWN_MIN_FAILS
    local_collector = local_collector or config.COLLECTOR_ID
    healthy, all_collectors = healthy_collectors(heartbeats, now, healthy_seconds)

    per = defaultdict(lambda: defaultdict(lambda: {'ok': 0, 'fail': 0, 'last_ok': None, 'last': None}))
    for o in observations:
        node, cid, at = o.get('target_node'), o.get('collector_id'), _parse(o.get('observed_at'))
        if not node or not cid or not at or (now - at).total_seconds() > recent_seconds:
            continue
        c = per[node][cid]
        if o.get('reachable'):
            c['ok'] += 1
            c['last_ok'] = max(c['last_ok'] or at, at)
        else:
            c['fail'] += 1
        c['last'] = max(c['last'] or at, at)

    nodes = []
    for node in sorted(per):
        by = per[node]
        seen = sorted(cid for cid, c in by.items() if cid in healthy and c['ok'])
        unseen = sorted(cid for cid, c in by.items() if cid in healthy and not c['ok'] and c['fail'])
        ignored = sorted(cid for cid in by if cid not in healthy)
        if seen and not unseen:
            state, why = 'UP', f"seen by {', '.join(seen)}"
        elif seen and unseen == [local_collector]:
            state, why = 'LOCALLY_UNREACHABLE', f"seen by {', '.join(seen)}, not by this collector"
        elif seen:
            state, why = 'PARTITIONED', f"seen by {', '.join(seen)}; not by {', '.join(unseen)}"
        else:
            strong = [cid for cid in unseen if by[cid]['fail'] >= down_min_fails]
            if len(strong) >= down_min_collectors:
                state, why = 'DOWN_CONFIRMED', f"{len(strong)} healthy collectors failed {down_min_fails}+ polls each"
            elif unseen or ignored:
                state = 'STALE'
                why = (f"only {len(strong)} collector(s) with {down_min_fails}+ failed polls; "
                       f"{down_min_collectors} needed to confirm down" if unseen else
                       'only observations from collectors that are not healthy now')
            else:
                state, why = 'UNKNOWN', 'no useful observations'
        last_ok = max((c['last_ok'] for c in by.values() if c['last_ok']), default=None)
        nodes.append({'node': node, 'status': state, 'why': why, 'seen_by': seen, 'not_seen_by': unseen,
                      'ignored_collectors': ignored,
                      'last_seen': last_ok.isoformat().replace('+00:00', 'Z') if last_ok else None,
                      'collectors': {cid: {'ok': c['ok'], 'fail': c['fail']} for cid, c in by.items()}})
    counts = {s: 0 for s in STATES}
    for n in nodes:
        counts[n['status']] += 1
    collectors = [{'collector_id': cid, 'site': h['site'], 'last_heartbeat': h['at'].isoformat().replace('+00:00', 'Z'),
                   'aborted': h['aborted'], 'healthy': cid in healthy, 'local': cid == local_collector}
                  for cid, h in sorted(all_collectors.items())]
    return {'generated_at': now.isoformat().replace('+00:00', 'Z'), 'nodes': nodes, 'counts': counts,
            'collectors': collectors, 'healthy_collectors': len(healthy),
            'rules': {'recent_seconds': recent_seconds, 'healthy_seconds': healthy_seconds,
                      'down_min_collectors': down_min_collectors, 'down_min_fails': down_min_fails}}


def _couch_inputs(client, now, recent_seconds, healthy_seconds):
    since_obs = (now - timedelta(seconds=recent_seconds)).isoformat().replace('+00:00', 'Z')
    since_hb = (now - timedelta(seconds=max(healthy_seconds, recent_seconds) * 4)).isoformat().replace('+00:00', 'Z')
    obs = client.find(selector={'type': 'node_observation', 'observed_at': {'$gte': since_obs}},
                      fields=['target_node', 'collector_id', 'observed_at', 'reachable'], limit=50000)
    hbs = client.find(selector={'type': 'collector_heartbeat', 'observed_at': {'$gte': since_hb}},
                      fields=['collector_id', 'collector_site', 'observed_at', 'scan_aborted'], limit=5000)
    return obs, hbs


def _local_inputs(now, recent_seconds):
    since = (now - timedelta(seconds=recent_seconds)).astimezone().strftime(TS_FMT)
    cid = config.COLLECTOR_ID
    with database.get_connection() as conn:
        obs = [{'target_node': r[0], 'collector_id': cid, 'observed_at': r[1], 'reachable': bool(r[2])}
               for r in conn.execute('''SELECT node_name, timestamp, reachable FROM node_health_history
                                        WHERE timestamp >= ? AND (probe_via IS NULL OR probe_via = '')''', (since,))]
        hbs = []
        try:
            hbs = [{'collector_id': cid, 'collector_site': config.COLLECTOR_SITE, 'observed_at': r[0],
                    'scan_aborted': bool(r[1])}
                   for r in conn.execute("SELECT timestamp, scan_aborted FROM collector_health WHERE kind = 'scan' "
                                         'ORDER BY id DESC LIMIT 5')]
        except Exception:
            pass
    return obs, hbs


def status(now=None):
    now = now or datetime.now(timezone.utc)
    import couch_client
    client = couch_client.configured_client()
    source = 'couchdb' if client else 'local'
    error = None
    if client:
        try:
            obs, hbs = _couch_inputs(client, now, config.DIST_RECENT_SECONDS, config.DIST_HEALTHY_SECONDS)
        except Exception as exc:
            error = f'CouchDB: {exc}'
            source = 'local'
            obs, hbs = _local_inputs(now, config.DIST_RECENT_SECONDS)
    else:
        obs, hbs = _local_inputs(now, config.DIST_RECENT_SECONDS)
    out = derive(obs, hbs, now=now)
    out['source'] = source
    out['error'] = error
    out['local_collector'] = config.COLLECTOR_ID
    if source == 'local':
        out['note'] = ('Only this collector: nodes can be UP, STALE or UNKNOWN. Run a second collector '
                       '(another PC with its own COLLECTOR_ID and the same COUCH_URL) for '
                       'LOCALLY_UNREACHABLE, PARTITIONED and DOWN_CONFIRMED.')
    return out

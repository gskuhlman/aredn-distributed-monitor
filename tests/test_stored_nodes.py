"""Stored (retired) nodes, TODO #83: kept, not counted as down, no alerts."""

from datetime import datetime, timedelta

import database
import notify
import unreachable
import uptime

TS = '%Y-%m-%d %H:%M:%S'


def _polls(mesh, name, ok):
    t0 = datetime.now() - timedelta(hours=2)
    for i in range(12):
        mesh.sql('INSERT INTO node_health_history (timestamp, node_name, reachable, uptime_seconds) VALUES (?, ?, ?, ?)',
                 ((t0 + timedelta(minutes=5 * i)).strftime(TS), name, 1 if ok else 0, 86400 + 300 * i if ok else None))


def test_stored_node_not_down_not_alerted_not_pruned(mesh):
    old = (datetime.now() - timedelta(days=200)).strftime(TS)
    mesh.node('k1aa-shelf', is_active=0, last_seen=old)
    mesh.node('k1bb-gone', is_active=0, last_seen=old)
    mesh.node('k1cc-down', is_active=0)
    _polls(mesh, 'k1aa-shelf', ok=False)
    _polls(mesh, 'k1cc-down', ok=False)
    flags = database.set_node_tag('K1AA-shelf', 'stored', True)
    assert flags['is_stored'] and not flags['is_permanent']
    assert database.stored_node_names() == {'k1aa-shelf'}

    start = (datetime.now() - timedelta(hours=3)).strftime(TS)
    end = datetime.now().strftime(TS)
    assert set(uptime.node_uptime(start, end)) == {'k1cc-down'}
    assert set(unreachable.classify(start, end)) == {'k1cc-down'}

    assert notify.is_suppressed({'event_type': 'node_offline', 'node_name': 'k1aa-shelf',
                                 'timestamp': end}) == 'k1aa-shelf is stored (retired)'

    assert database.prune_old_nodes(90) == 1        # k1bb-gone only
    names = {r[0] for r in mesh.sql('SELECT name FROM nodes')}
    assert 'k1aa-shelf' in names and 'k1bb-gone' not in names

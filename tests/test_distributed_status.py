"""Distributed status rules (CLAUDE.md) with synthetic collectors."""

from datetime import datetime, timedelta, timezone

import distributed_status as ds

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace('+00:00', 'Z')


def hb(cid, minutes_ago=1, aborted=False):
    return {'collector_id': cid, 'observed_at': iso(minutes_ago), 'scan_aborted': aborted}


def obs(cid, node, ok, minutes_ago=2):
    return {'collector_id': cid, 'target_node': node, 'observed_at': iso(minutes_ago), 'reachable': ok}


def status(observations, heartbeats, local='a'):
    r = ds.derive(observations, heartbeats, now=NOW, local_collector=local, recent_seconds=900,
                  healthy_seconds=900, down_min_collectors=2, down_min_fails=2)
    return {n['node']: n['status'] for n in r['nodes']}


def test_up_locally_unreachable_partitioned():
    hbs = [hb('a'), hb('b'), hb('c')]
    o = [obs('a', 'n1', True), obs('b', 'n1', True),
         obs('a', 'n2', False), obs('b', 'n2', True),
         obs('b', 'n3', False), obs('c', 'n3', True), obs('a', 'n3', True)]
    s = status(o, hbs)
    assert s['n1'] == 'UP'
    assert s['n2'] == 'LOCALLY_UNREACHABLE'
    assert s['n3'] == 'PARTITIONED'


def test_down_needs_two_healthy_collectors():
    hbs = [hb('a'), hb('b')]
    two = [obs('a', 'n', False, 2), obs('a', 'n', False, 5), obs('b', 'n', False, 3), obs('b', 'n', False, 6)]
    assert status(two, hbs)['n'] == 'DOWN_CONFIRMED'
    one = [obs('a', 'n', False, 2), obs('a', 'n', False, 5)]
    assert status(one, hbs)['n'] == 'STALE'      # never down from the local collector alone


def test_unhealthy_collectors_dont_count():
    # b lost its starting node (aborted) and c's heartbeat is old: only a counts.
    hbs = [hb('a'), hb('b', aborted=True), hb('c', minutes_ago=60)]
    o = [obs('a', 'n', False, 2), obs('a', 'n', False, 4),
         obs('b', 'n', False, 2), obs('b', 'n', False, 4), obs('c', 'n', False, 2), obs('c', 'n', False, 3)]
    assert status(o, hbs)['n'] == 'STALE'


def test_old_observations_are_unknown_or_absent():
    hbs = [hb('a')]
    o = [obs('a', 'n', True, minutes_ago=120)]
    assert 'n' not in status(o, hbs)

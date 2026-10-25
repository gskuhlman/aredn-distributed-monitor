"""Rollout tracker: order (clients, sectors, backbones), steps tick off when
the radio reports the planned channel, and a missing link becomes a problem
after the grace period."""

from datetime import datetime, timedelta

import config
import rollout


def setup_mesh(mesh):
    mesh.node('w0aa-col-sec', rf_frequency='5805', rf_chanbw=20, rf_status='on')
    mesh.node('w0aa-col-bb', rf_frequency='5745', rf_chanbw=20, rf_status='on')
    mesh.node('w0aa-mmc-bb', rf_frequency='5745', rf_chanbw=20, rf_status='on')
    mesh.node('k1aa-home', rf_frequency='5805', rf_chanbw=20, rf_status='on')
    mesh.infra('w0aa-col-sec', 'COL')
    mesh.infra('w0aa-col-bb', 'COL')
    mesh.infra('w0aa-mmc-bb', 'MMC')
    mesh.link('k1aa-home', 'w0aa-col-sec', 'RF')
    mesh.link('w0aa-col-bb', 'w0aa-mmc-bb', 'RF')


def test_order_clients_sectors_backbones(mesh):
    setup_mesh(mesh)
    r = rollout.create("w0aa-col-bb 5700 20\nw0aa-col-sec 5865 20\nk1aa-home 5865 20\nw0aa-mmc-bb 5700 20")
    roles = [s['role'] for s in r['steps']]
    assert roles == ['client', 'sector', 'backbone', 'backbone']
    assert r['steps'][0]['node_name'] == 'k1aa-home'
    assert r['progress']['total'] == 4 and r['next_step'] == r['steps'][0]['id']


def test_step_changes_then_verifies(mesh):
    setup_mesh(mesh)
    r = rollout.create("k1aa-home 5865 20\nw0aa-col-sec 5865 20")
    # Client moved: changed, waiting for its sector (still pending) -> not a problem.
    mesh.sql("UPDATE nodes SET rf_frequency = '5865' WHERE name = 'k1aa-home'")
    rollout.check()
    steps = {s['node_name']: s for s in rollout.get(r['id'])['steps']}
    assert steps['k1aa-home']['status'] == 'verified'   # its only peer is still waiting, so nothing is expected yet
    assert steps['w0aa-col-sec']['status'] == 'pending'
    # Sector moved and the link is back (fresh last_seen).
    mesh.sql("UPDATE nodes SET rf_frequency = '5865' WHERE name = 'w0aa-col-sec'")
    now = datetime.now() + timedelta(seconds=1)
    mesh.sql("UPDATE links SET last_seen = ?", (now.strftime('%Y-%m-%d %H:%M:%S'),))
    events = rollout.check(now=now)
    got = rollout.get(r['id'])
    assert all(s['status'] == 'verified' for s in got['steps'])
    assert got['status'] == 'done'
    assert any(e['event_type'] == 'rollout_complete' for e in events)


def test_missing_link_becomes_problem(mesh):
    setup_mesh(mesh)
    r = rollout.create("w0aa-col-bb 5700 20")
    mesh.sql("UPDATE nodes SET rf_frequency = '5700' WHERE name = 'w0aa-col-bb'")
    t0 = datetime.now() + timedelta(seconds=1)
    rollout.check(now=t0)
    assert rollout.get(r['id'])['steps'][0]['status'] == 'changed'
    late = t0 + timedelta(minutes=config.ROLLOUT_LINK_GRACE_MINUTES + 1)
    events = rollout.check(now=late)
    s = rollout.get(r['id'])['steps'][0]
    assert s['status'] == 'problem' and s['missing'] == ['w0aa-mmc-bb']
    assert any(e['event_type'] == 'rollout_link_missing' for e in events)


def test_wrong_channel_is_a_problem(mesh):
    setup_mesh(mesh)
    r = rollout.create("k1aa-home 5865 20")
    mesh.sql("UPDATE nodes SET rf_frequency = '5900' WHERE name = 'k1aa-home'")
    rollout.check()
    assert rollout.get(r['id'])['steps'][0]['status'] == 'problem'

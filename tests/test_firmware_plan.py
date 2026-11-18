"""Firmware upgrade planner (firmware_plan.py): far ends first, waves, and
tracking from the versions the scanner reads."""

import database
import firmware_plan

OLD, NEW = '4.26.1.0', '4.26.10.0'


def setup_mesh(mesh, monkeypatch):
    monkeypatch.setattr(database, 'log_event', lambda *a, **k: None)
    mesh.node('w0aa-hub', scan_depth=0, firmware_version=NEW)
    mesh.node('w0aa-sec', scan_depth=1, firmware_version=OLD)
    mesh.node('k1aa-home', scan_depth=2, firmware_version=OLD)
    mesh.node('k1bb-home', scan_depth=2, firmware_version=OLD)
    mesh.node('k1cc-home', scan_depth=1, firmware_version=OLD)
    mesh.node('k1dd-dev', scan_depth=1, firmware_version='20261002-64d86b12')
    mesh.infra('w0aa-sec', 'COL')
    mesh.link('w0aa-hub', 'w0aa-sec', 'DTD')
    mesh.link('w0aa-sec', 'k1aa-home')
    mesh.link('w0aa-sec', 'k1bb-home')
    mesh.link('w0aa-hub', 'k1cc-home')
    mesh.link('w0aa-hub', 'k1dd-dev')


def test_far_ends_first_in_waves(mesh, monkeypatch):
    setup_mesh(mesh, monkeypatch)
    p = firmware_plan.plan()
    assert p['target'] == NEW and p['starting_node'] == 'w0aa-hub'
    order = [s['node'] for s in p['steps']]
    assert order[-1] == 'w0aa-sec'
    assert {w['wave']: sorted(w['nodes']) for w in p['waves']} == {
        1: ['k1aa-home', 'k1bb-home', 'k1cc-home'], 2: ['w0aa-sec']}
    sec = p['steps'][-1]
    assert sec['behind'] == ['k1aa-home', 'k1bb-home'] and sec['behind_count'] == 2
    assert p['left_out'][0]['node'] == 'k1dd-dev' and 'nightly' in p['left_out'][0]['reason']
    assert p['already'] == ['w0aa-hub']
    assert firmware_plan.plan(infrastructure_only=True)['waves'] == [{'wave': 1, 'nodes': ['w0aa-sec']}]


def test_named_nodes_and_versions():
    assert firmware_plan.is_done('4.26.10.0', '4.26.10.0')
    assert firmware_plan.is_done('4.27.1.0', '4.26.10.0')
    assert not firmware_plan.is_done('4.26.7.0', '4.26.10.0')
    assert firmware_plan.needs('4.26.7.0', '4.26.10.0') is True
    assert firmware_plan.needs('20261002-64d86b12', '4.26.10.0') is None


def test_tracking_ticks_off_and_flags_out_of_order(mesh, monkeypatch):
    setup_mesh(mesh, monkeypatch)
    r = firmware_plan.create(names=['w0aa-sec', 'k1aa-home', 'k1bb-home'])
    assert [s['node_name'] for s in r['steps']][-1] == 'w0aa-sec'
    assert r['next_wave'] == 1 and sorted(r['next_nodes']) == ['k1aa-home', 'k1bb-home']
    # The sector is upgraded while both its clients still wait: noted.
    mesh.sql("UPDATE nodes SET firmware_version = ? WHERE name = 'w0aa-sec'", (NEW,))
    kinds = [e['event_type'] for e in firmware_plan.check()]
    assert kinds == ['firmware_step_upgraded', 'firmware_out_of_order']
    steps = {s['node_name']: s for s in firmware_plan.get(r['id'])['steps']}
    assert steps['w0aa-sec']['status'] == 'upgraded' and 'k1aa-home' in steps['w0aa-sec']['detail']
    # A version that is neither old nor target is a problem; skipping finishes the rollout.
    mesh.sql("UPDATE nodes SET firmware_version = '4.26.7.0' WHERE name = 'k1aa-home'")
    firmware_plan.check()
    steps = {s['node_name']: s for s in firmware_plan.get(r['id'])['steps']}
    assert steps['k1aa-home']['status'] == 'problem'
    firmware_plan.set_step(r['id'], steps['k1aa-home']['id'], 'skipped')
    mesh.sql("UPDATE nodes SET firmware_version = ? WHERE name = 'k1bb-home'", (NEW,))
    kinds = [e['event_type'] for e in firmware_plan.check()]
    assert kinds == ['firmware_step_upgraded', 'firmware_rollout_complete']
    assert firmware_plan.get(r['id'])['status'] == 'done'

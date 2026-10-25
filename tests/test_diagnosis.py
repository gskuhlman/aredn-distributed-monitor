"""Pair diagnosis for a DtD-wired pair whose one end has its radio off, and
the ghost tracker check that keeps such trackers out of the RF checks."""

import ghost_trackers
import link_diagnostics


def wired_pair(mesh):
    mesh.node('k9zz-home-beam', lat=42.0, lon=-91.6, rf_frequency='5910', rf_chanbw=20, rf_status='on')
    mesh.node('k9zz-home-node1', lat=42.0, lon=-91.6, rf_frequency='', rf_status='off')
    mesh.node('w0aa-col-rkt-s', lat=42.02, lon=-91.6, rf_frequency='5910', rf_chanbw=20, rf_status='on')
    mesh.link('k9zz-home-beam', 'k9zz-home-node1', 'DtD', quality=100, snr=None)
    mesh.link('k9zz-home-beam', 'w0aa-col-rkt-s', 'RF', quality=95, snr=35)


def test_wired_pair_verdict_is_ok(mesh):
    wired_pair(mesh)
    d = link_diagnostics.diagnose_pair('k9zz-home-beam', 'k9zz-home-node1')
    v = d['verdict']
    assert v.get('wired'), v
    assert v['severity'] in ('ok', 'warning')
    assert 'fine' in v['text']


def test_radio_off_tracker_is_a_ghost(mesh):
    wired_pair(mesh)
    # A leftover RF tracker at another radio for the node whose radio is off.
    mesh.node('w0bb-other', lat=42.05, lon=-91.6, rf_frequency='5910', rf_chanbw=20, rf_status='on')
    mesh.link('w0bb-other', 'k9zz-home-node1', 'RF', both=False, quality=40, snr=12)
    ghosts = ghost_trackers.find()
    kinds = {(g['source'], g['target']): g['kinds'] for g in ghosts}
    assert 'radio_off' in kinds[('w0bb-other', 'k9zz-home-node1')]
    assert ('w0bb-other', 'k9zz-home-node1') in ghost_trackers.excluded_pairs()
    assert ('k9zz-home-beam', 'w0aa-col-rkt-s') not in ghost_trackers.excluded_pairs()


def test_channel_mismatch_and_mac_conflict(mesh):
    mesh.node('a1-x', lat=42.0, lon=-91.6, rf_frequency='5805', rf_chanbw=10, rf_status='on')
    mesh.node('a1-y', lat=42.01, lon=-91.6, rf_frequency='5905', rf_chanbw=10, rf_status='on')
    mesh.node('a1-z', lat=42.01, lon=-91.6, rf_frequency='5805', rf_chanbw=10, rf_status='on')
    mesh.link('a1-x', 'a1-y', 'RF', both=False, mac_address='aa:bb:cc:00:00:01')
    mesh.link('a1-z', 'a1-x', 'RF', both=False, mac_address='aa:bb:cc:00:00:02')
    mesh.link('a1-y', 'a1-z', 'RF', both=False, mac_address='aa:bb:cc:00:00:02')
    kinds = {(g['source'], g['target']): g['kinds'] for g in ghost_trackers.find()}
    assert 'channel_mismatch' in kinds[('a1-x', 'a1-y')]
    assert 'mac_conflict' in kinds[('a1-z', 'a1-x')]
    # Reported, not excluded: a person has to look.
    assert not ghost_trackers.excluded_pairs()

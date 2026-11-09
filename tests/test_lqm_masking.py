"""AREDN LQM shows ping quality alone once retries reach the frames sent
(tx_quality 0 counts as no data), so a 100% quality can hide the worst
transmit state. Link Health and the pair diagnosis flag it."""

import json

from conftest import ts
import link_diagnostics
import link_health

A, B = 'w0aa-col-sq-2mmc', 'w0aa-mmc-sq-2col'


def test_masked_sample_counts_at_its_real_quality():
    masked = {'quality': 100, 'tx_quality': 0.0, 'ping_quality': 100}
    assert link_health.lqm_masked(masked)
    assert link_health.effective_quality(masked) == 50
    normal = {'quality': 58, 'tx_quality': 15.3, 'ping_quality': 100}
    assert not link_health.lqm_masked(normal)
    assert link_health.effective_quality(normal) == 58
    # No transmit data at all is not masking.
    assert not link_health.lqm_masked({'quality': 100, 'tx_quality': None, 'ping_quality': 100})


def test_analyze_masking_rates_the_worst_direction():
    rows = ([{'source_node': A, 'target_node': B, 'quality': 100, 'tx_quality': 0.0, 'ping_quality': 100}] * 6
            + [{'source_node': B, 'target_node': A, 'quality': 100 if i % 2 else 57,
                'tx_quality': 0.0 if i % 2 else 14.0, 'ping_quality': 100} for i in range(6)])
    m = link_health.analyze_lqm_masking(rows)
    assert m['rating'] == 'poor' and m['masked_pct'] == 100.0
    assert (m['source'], m['target']) == (A, B)
    by = {(d['source'], d['target']): d for d in m['directions']}
    assert by[(A, B)]['reported_quality_avg'] == 100 and by[(A, B)]['quality_avg'] == 50
    assert by[(B, A)]['masked_pct'] == 50.0
    assert 'ping quality alone' in m['message']
    healthy = [{'source_node': A, 'target_node': B, 'quality': 95, 'tx_quality': 90.0, 'ping_quality': 100}] * 5
    assert link_health.analyze_lqm_masking(healthy)['rating'] == 'good'
    assert link_health.analyze_lqm_masking(healthy[:2])['rating'] == 'unknown'


def test_stability_and_grade_use_effective_quality():
    rows = [{'quality': 100, 'tx_quality': 0.0, 'ping_quality': 100, 'snr': 29}] * 10
    s = link_health.compute_rf_stability(rows)
    assert s['quality_avg'] == 50
    poor = link_health.compute_overall_grade({'lqm_tx': {'rating': 'poor'}})
    assert poor['rating'] == 'poor'


def test_pair_diagnosis_flags_hidden_retries(mesh):
    mesh.node(A, lat=42.0, lon=-91.6, rf_frequency='5660', rf_chanbw=40, rf_status='on')
    mesh.node(B, lat=42.05, lon=-91.62, rf_frequency='5660', rf_chanbw=40, rf_status='on')
    tracker = {'quality': 100, 'tx_quality': 0, 'ping_quality': 100, 'lq': 100, 'snr': 29}
    mesh.link(A, B, 'RF', both=False, quality=100, snr=29, raw_tracker=json.dumps(tracker))
    mesh.link(B, A, 'RF', both=False, quality=95, snr=29,
              raw_tracker=json.dumps({'quality': 95, 'tx_quality': 90, 'ping_quality': 100, 'lq': 100, 'snr': 29}))
    for i in range(6):
        mesh.sql('''INSERT INTO link_history (timestamp, source_node, target_node, link_type, quality, snr,
                    tx_quality, ping_quality, sample_type) VALUES (?, ?, ?, 'RF', 100, 29, 0, 100, 'scan')''',
                 (ts(10 * i + 5), A, B))
    d = link_diagnostics.diagnose_pair(A, B)
    hits = [x for x in d['findings'] if x['category'] == 'quality' and 'hides heavy retries' in x['title']]
    assert len(hits) == 1, [x['title'] for x in d['findings']]
    assert hits[0]['title'].startswith(A) and hits[0]['severity'] == 'warning'
    assert any('100% of' in e for e in hits[0]['evidence'])

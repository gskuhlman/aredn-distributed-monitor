"""Links far below their potential: MCS estimated from the rate (802.11ac
radios report none), the LQM fallback where Prometheus doesn't reach, and the
hourly watch that raises and clears `link_underperforming`."""

from datetime import datetime, timedelta

import database
import link_potential as lp
import mesh_diagnostics
import prom_watch

TS = '%Y-%m-%d %H:%M:%S'


def test_mcs_from_rate_handles_vht_and_ambiguous_rates():
    assert lp.mcs_from_rate(54, 40) == [(1, 3), (2, 1)]
    assert lp.mcs_from_rate(300, 40) == [(2, 7)]          # short guard interval
    assert lp.mcs_from_rate(24, 20, nearest=False) == []  # legacy 802.11a rate
    assert lp.supported_mcs(31) == 9 and lp.supported_mcs(28) == 7


def _dishes(mesh, rate, snr=31, hours=24, start=0):
    # Two infrastructure dishes 7 km apart, gains set; scan samples every 30 min.
    for name, lat in (('w0aa-mmc-dish', 42.000), ('w0aa-eoc-dish', 42.063)):
        mesh.node(name, lat=lat, lon=-91.600, rf_frequency='5825', rf_chanbw=40, rf_status='on',
                  antenna_gain=25, model='Ubiquiti PowerBeam 5AC 400')
    mesh.infra('w0aa-mmc-dish', 'MMC')
    mesh.infra('w0aa-eoc-dish', 'EOC')
    mesh.link('w0aa-mmc-dish', 'w0aa-eoc-dish', snr=snr)
    now = datetime.now()
    with database.get_connection() as conn:
        for i in range(start, start + hours * 2):
            t = (now - timedelta(minutes=30 * i)).strftime(TS)
            for a, b in (('w0aa-mmc-dish', 'w0aa-eoc-dish'), ('w0aa-eoc-dish', 'w0aa-mmc-dish')):
                conn.execute('''INSERT INTO link_history (timestamp, source_node, target_node, link_type, quality,
                                snr, signal, tx_bitrate, rx_bitrate) VALUES (?, ?, ?, 'RF', 100, ?, ?, ?, ?)''',
                             (t, a, b, snr, snr - 98, rate, rate))


def test_single_stream_link_is_found_without_prometheus(mesh):
    _dishes(mesh, rate=54)
    with database.get_connection() as conn:
        low = prom_watch.low_mcs_for_snr(conn, datetime.now())
    m = next(x for x in low if x['node'] == 'w0aa-mmc-dish')
    assert m['source'] == 'lqm' and m['basis'] == 'rate' and (1, 3) in m['fits'] and m['supports'] == 9
    assert 'MCS 3 on 1 stream or MCS 1 on 2 streams' in prom_watch.describe_low_mcs(m)
    report = mesh_diagnostics.run_health_check(24)
    cats = {f['category'] for f in report['findings']}
    assert 'mcs' in cats
    mcs = next(f for f in report['findings'] if f['category'] == 'mcs')
    assert 'one polarisation' in mcs['detail']


def test_underperforming_link_raises_then_clears(mesh):
    _dishes(mesh, rate=54)
    under = lp.underperforming()
    assert {(p['from'], p['to']) for p in under} == {('w0aa-mmc-dish', 'w0aa-eoc-dish'),
                                                      ('w0aa-eoc-dish', 'w0aa-mmc-dish')}
    assert any('20% of the 270 Mbps' in r for r in under[0]['reasons'])
    ev, = lp.watch()
    assert ev['event_type'] == lp.EVENT_UNDER and ev['severity'] == 'warning'
    assert ev['details'].startswith('RF link to w0aa-mmc-dish: far below its potential')
    assert lp.watch() == []   # transitions only
    # Realigned: the last few hours are back at full rate and SNR.
    with database.get_connection() as conn:
        conn.execute("UPDATE link_history SET tx_bitrate = 300, snr = 44, signal = -54 WHERE timestamp >= ?",
                     ((datetime.now() - timedelta(hours=3, minutes=10)).strftime(TS),))
    assert lp.underperforming() == []
    ev, = lp.watch()
    assert ev['event_type'] == lp.EVENT_UNDER_CLEARED and lp.active() == []


def test_full_rate_link_is_not_flagged(mesh):
    _dishes(mesh, rate=300, snr=44)
    with database.get_connection() as conn:
        assert prom_watch.low_mcs_for_snr(conn, datetime.now()) == []
    assert lp.underperforming() == [] and lp.watch() == []

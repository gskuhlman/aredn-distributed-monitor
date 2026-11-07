"""Spectrum by location: our radios, guard, WiFi from a survey, mesh radios
at other sites in range, free spans."""

import spectrum_view
import wifi_survey

IW = """BSS aa:bb:cc:00:00:01(on wlan0)
        freq: 5745.0
        signal: -60.00 dBm
        SSID: Home
        VHT operation:
                 * channel width: 1 (80 MHz)
                 * center freq segment 1: 155
"""


def test_site_row_marks_ours_guard_wifi_mesh_and_free(mesh):
    mesh.node('w0aa-col-rkt-n', lat=42.0, lon=-91.6, rf_frequency='5900', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc')
    mesh.infra('w0aa-col-rkt-n', 'COL')
    mesh.node('k9zz-far-omni', lat=42.05, lon=-91.6, rf_frequency='5680', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc')
    wifi_survey.import_text('w0aa-col-rkt-n', IW)

    r = spectrum_view.build(band=(5640, 5930))
    row, = r['rows']
    status = {c['low']: c['status'] for c in row['cells']}
    assert status[5890] == 'ours' and status[5905] == 'ours'
    assert status[5880] == 'guard' and status[5915] == 'guard'
    assert status[5750] == 'busy'                     # 80 MHz AP at -60 dBm, 5735-5815
    assert status[5670] == 'busy'                     # two omnis 5.6 km apart hear each other
    assert {'low': 5815.0, 'high': 5880.0, 'mhz': 65.0} in row['free']
    scan, = row['scans']
    assert scan['radio'] == 'w0aa-col-rkt-n' and len(scan['aps']) == 1


def test_unscanned_site_is_unsurveyed_not_free(mesh):
    mesh.node('w0aa-col-rkt-n', lat=42.0, lon=-91.6, rf_frequency='5900', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc')
    mesh.infra('w0aa-col-rkt-n', 'COL')
    row, = spectrum_view.build(band=(5640, 5930))['rows']
    assert {c['status'] for c in row['cells']} == {'ours', 'guard', 'unsurveyed'}
    assert row['free'] == []


def test_scans_pool_across_radios_at_the_same_coordinates(mesh):
    # Sectors on one mast often share exact coordinates (0 m apart): one
    # sector's scan must count for its neighbours (planner WiFi cost).
    import channel_map as cm
    for name in ('w0aa-col-rkt-n', 'w0aa-col-rkt-s'):
        mesh.node(name, lat=42.0, lon=-91.6, rf_frequency='5900', rf_chanbw=20, rf_status='on', rf_mode='adhoc')
        mesh.infra(name, 'COL')
    wifi_survey.import_text('w0aa-col-rkt-n', IW)
    by_name = {r['name']: r for r in cm.build_map()['radios']}
    assert [a['bssid'] for a in wifi_survey.aps_for(['w0aa-col-rkt-s'], by_name)] == ['aa:bb:cc:00:00:01']


def test_scan_detail_pools_a_site_and_grades_slots(mesh):
    for name, f in (('w0aa-col-rkt-n', '5900'), ('w0aa-col-rkt-s', '5860')):
        mesh.node(name, lat=42.0, lon=-91.6, rf_frequency=f, rf_chanbw=20, rf_status='on', rf_mode='adhoc')
        mesh.infra(name, 'COL')
    wifi_survey.import_text('w0aa-col-rkt-n', IW)
    d = wifi_survey.detail(site='col', low=5640, high=5930)
    assert d['site'] == 'COL' and [s['node'] for s in d['scans']] == ['w0aa-col-rkt-n']
    ap, = d['aps']
    assert (ap['low'], ap['high'], ap['width_known'], ap['heard_by']) == (5735, 5815, True, 'w0aa-col-rkt-n')
    level = {s['low']: s['level'] for s in d['slots']}
    assert level[5750.0] == 'light' and level[5850.0] == 'free'   # one AP at -60 dBm
    assert {o['name']: o['scanned'] for o in d['ours']} == {'w0aa-col-rkt-n': True, 'w0aa-col-rkt-s': False}
    one = wifi_survey.detail(node='w0aa-col-rkt-n', low=5640, high=5930)
    assert one['site'] is None and len(one['aps']) == 1

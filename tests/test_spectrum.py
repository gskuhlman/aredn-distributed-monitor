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

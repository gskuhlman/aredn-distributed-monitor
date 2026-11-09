"""Sector Airtime: which radios count as sectors."""

import airtime


def radio(mesh, name, lat, lon, **f):
    row = dict(lat=lat, lon=lon, rf_frequency='5900', rf_chanbw=20, rf_status='on', rf_mode='adhoc',
               model='Ubiquiti Rocket 5AC Lite')
    row.update(f)
    return mesh.node(name, **row)


def test_infrastructure_sector_with_one_client_is_a_sector(mesh):
    radio(mesh, 'w0aa-col-rkt-e', 42.0, -91.6, antenna_azimuth=90, antenna_beamwidth=120)
    mesh.infra('w0aa-col-rkt-e', 'COL')
    radio(mesh, 'w0aa-col-sq-2mmc', 42.0, -91.6, rf_frequency='5660', rf_chanbw=40,
          antenna_azimuth=185, antenna_beamwidth=23)
    mesh.infra('w0aa-col-sq-2mmc', 'COL')
    radio(mesh, 'w0aa-mmc-sq-2col', 41.9, -91.62, rf_frequency='5660', rf_chanbw=40,
          antenna_azimuth=5, antenna_beamwidth=23)
    mesh.infra('w0aa-mmc-sq-2col', 'MMC')
    radio(mesh, 'k0bb-qth', 42.0, -91.5, antenna_azimuth=270, antenna_beamwidth=20)
    mesh.link('w0aa-col-rkt-e', 'k0bb-qth')
    mesh.link('w0aa-col-sq-2mmc', 'w0aa-mmc-sq-2col')
    names = [s['name'] for s in airtime.run_report('all')['sectors']]
    # The sector antenna counts; the point-to-point dishes and the lone client do not.
    assert names == ['w0aa-col-rkt-e'], names

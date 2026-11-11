"""Club status page fragment (public_status.py)."""

import phones
import public_status


def test_club_status_infra_clients_and_phones(mesh):
    mesh.node('w0gq-col-rkt-n', rf_frequency='5890', rf_channel='178', rf_chanbw='20', rf_mode='adhoc',
              uptime_seconds=90061, model='Rocket', firmware_version='4.26.10.0')
    mesh.node('w0gq-col-ac3')
    mesh.node('kf0abc-qth', rf_frequency='5890', rf_chanbw='20', rf_mode='adhoc', is_active=0)
    mesh.node('kf0xyz-far', rf_frequency='5890', rf_chanbw='20')
    mesh.infra('w0gq-col-rkt-n', 'COL')
    mesh.infra('w0gq-col-ac3', 'COL')
    mesh.link('w0gq-col-rkt-n', 'kf0abc-qth', snr=27)
    mesh.link('w0gq-col-rkt-n', 'kf0xyz-far', status='removed')
    mesh.link('w0gq-col-rkt-n', 'w0gq-col-ac3', link_type='DtD')
    phones.ensure_tables()
    mesh.sql("INSERT INTO phones (ip, name, extension, kind, source, added, enabled, ignored, ok) "
             "VALUES ('10.1.2.3', '1150 - Direct IP : 10.1.2.3', '1150', 'phone', 'manual', '2026-01-01 00:00:00', 1, 0, 1)")

    d = public_status.build(30)
    site = d['sites'][0]
    assert site['code'] == 'COL' and [r['name'] for r in site['radios']] == ['w0gq-col-ac3', 'w0gq-col-rkt-n']
    rkt = site['radios'][1]
    assert (rkt['frequency'], rkt['width'], rkt['mode'], rkt['rf_clients'], rkt['uptime']) == (5890, 20, 'Mesh', 1, '1 d 1 h')
    assert [c['name'] for c in d['clients']] == ['kf0abc-qth']    # removed link and DtD left out
    assert d['clients'][0]['status'] == 'down' and d['clients'][0]['links'][0]['snr'] == 27
    assert d['phones'][0]['ip'] is None and '10.1.2.3' not in d['phones'][0]['name']

    frag = public_status.render(d)
    assert '<script' not in frag and '<style' not in frag
    assert 'w0gq-col-rkt-n' in frag and '5890 MHz' in frag and '10.1.2.3' not in frag
    assert '10.1.2.3' in public_status.render(public_status.build(30, include_ips=True))


def test_club_status_escapes_text(mesh):
    mesh.node('w0gq-x<b>', antenna_desc='<script>alert(1)</script>')
    mesh.infra('w0gq-x<b>', 'X')
    frag = public_status.render(public_status.build(7))
    assert '<script>' not in frag and '&lt;script&gt;' in frag


def test_setup_checklist_is_not_collapsible(mesh):
    """The page is pasted into emails, where <details> sections lose their contents."""
    mesh.node('w0gq-col-rkt-n')
    mesh.infra('w0gq-col-rkt-n', 'COL')
    d = public_status.build(7)
    radio = d['sites'][0]['radios'][0]
    radio['fixes'], radio['hygiene'] = ['Set the antenna azimuth'], 60
    frag = public_status.render(d, hygiene_details=True)
    assert 'Node setup checklist' in frag and 'Set the antenna azimuth' in frag
    assert '<details' not in frag and '<summary' not in frag

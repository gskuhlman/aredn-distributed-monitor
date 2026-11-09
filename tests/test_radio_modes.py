"""PtP / PtMP / Station radios and the planner: the dish rule graded by aim,
protected radios, stations that follow their access point by SSID, rollout
instructions, and no remote WiFi scan outside Mesh mode."""

import pytest

import channel_planner as cp


def _geo(a_sees, b_sees, dish=True):
    return {'d': 13000, 'colocated': False, 'a_sees': a_sees, 'b_sees': b_sees, 'back_to_back': False,
            'infra_watch': True, 'dish_watch': dish}


def test_dish_rule_is_graded_by_aim():
    # Aimed at each other with a dish: all but forbidden.
    for rel in ('overlap', 'adjacent', 'same'):
        assert cp._pair_cost(rel, _geo(True, True)) == cp.DISH_FACING_COST
    # Only the dish aimed at the other (its side/back lobe): a warning.
    one_way = cp._pair_cost('overlap', _geo(False, True))
    assert cp._severity_of(one_way) == 'warning'
    assert one_way < cp.SEVERITY_COST['critical']
    # Two sectors aimed at each other keep the old critical guard band.
    assert cp._pair_cost('overlap', _geo(True, True, dish=False)) == cp.INFRA_COST['overlap']


def _two_sites(mesh):
    # A protected omni (PtMP, 5905) and a sector 6 km away aimed at it on an overlapping channel.
    mesh.node('w0aa-mmc-omni', lat=42.000, lon=-91.600, rf_frequency='5905', rf_chanbw=20, rf_status='on',
              rf_mode='ptmp', rf_ssid='AREDN-181-20-v3', antenna_beamwidth=360)
    mesh.infra('w0aa-mmc-omni', 'MMC')
    mesh.node('w0aa-col-s', lat=42.055, lon=-91.600, rf_frequency='5910', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc', rf_ssid='AREDN-20-v3', antenna_azimuth=180, antenna_beamwidth=120)
    mesh.infra('w0aa-col-s', 'COL')


def test_protected_radio_keeps_its_channel_and_pushes_neighbours_off(mesh):
    _two_sites(mesh)
    r = cp.plan(site='ALL', top=1, protected=['w0aa-mmc-omni'])
    assert r['protected'] == ['w0aa-mmc-omni']
    new = {x['name']: x['new'] for x in r['plans'][0]['radios']}
    assert new['w0aa-mmc-omni']['frequency'] == 5905
    lo, hi = new['w0aa-col-s']['low'], new['w0aa-col-s']['high']
    assert hi <= 5895 or lo >= 5915, new['w0aa-col-s']
    # The current plan (overlapping) is flagged against the protected radio.
    rules = {i['rule'] for g in r['current']['groups'] for i in g['issues']}
    assert 'protected' in rules


def test_saved_protected_list_is_the_default(mesh):
    _two_sites(mesh)
    assert cp.set_protected('W0AA-MMC-OMNI, ') == ['w0aa-mmc-omni']
    r = cp.plan(site='ALL', top=1)
    assert r['protected'] == ['w0aa-mmc-omni']
    assert cp.plan(site='ALL', top=1, protected=[])['protected'] == []


def test_station_follows_its_access_point_by_ssid(mesh):
    _two_sites(mesh)
    # An event station with no current link, configured for the omni's SSID.
    mesh.node('k1aa-race-cam', lat=42.010, lon=-91.610, rf_frequency='5905', rf_chanbw=20, rf_status='on',
              rf_mode='sta', rf_ssid='AREDN-181-20-v3', antenna_azimuth=45, antenna_beamwidth=20)
    r = cp.plan(site='MMC', top=1, band=[5650, 5880], protected=[])
    groups = [g for g in r['groups'] if 'w0aa-mmc-omni' in g['members']]
    assert groups and 'k1aa-race-cam' in groups[0]['members']
    assert groups[0]['ssid_stations'] == {'k1aa-race-cam': 'w0aa-mmc-omni'}
    rows = {x['name']: x for x in r['plans'][0]['radios']}
    omni = rows['w0aa-mmc-omni']
    assert omni['changed'] and omni['old']['ssid'] == 'AREDN-181-20-v3'
    assert omni['new']['ssid'] == cp.new_ssid('AREDN-181-20-v3', omni['new']['frequency'], 20)
    assert rows['k1aa-race-cam']['new']['frequency'] == omni['new']['frequency']


def test_same_channel_says_whether_radios_would_link():
    mesh_a, mesh_b = {'mode': 'adhoc'}, {'mode': 'adhoc'}
    assert cp._link_kind(mesh_a, mesh_b) == 'mesh'
    assert cp._link_kind(mesh_a, {'mode': 'ptmp'}) == 'none'
    assert cp._link_kind({'mode': 'sta'}, {'mode': 'ptp'}) == 'none'
    assert cp.new_ssid('AREDN-165-40-v3', 5835, 40) == 'AREDN-167-40-v3'
    assert cp.new_ssid('AREDN-20-v3', 5835, 20) is None


def test_rollout_puts_station_first_with_instructions(mesh):
    import rollout
    mesh.node('w0aa-mmc-dish', rf_frequency='5825', rf_chanbw=40, rf_status='on', rf_mode='ptp',
              rf_ssid='AREDN-165-40-v3')
    mesh.node('w0aa-eoc-dish', rf_frequency='5825', rf_chanbw=40, rf_status='on', rf_mode='sta',
              rf_ssid='AREDN-165-40-v3')
    mesh.infra('w0aa-mmc-dish', 'MMC')
    mesh.infra('w0aa-eoc-dish', 'EOC')
    mesh.link('w0aa-mmc-dish', 'w0aa-eoc-dish', 'RF')
    r = rollout.create('w0aa-mmc-dish 5835 40\nw0aa-eoc-dish 5835 40')
    steps = r['steps']
    assert [s['node_name'] for s in steps] == ['w0aa-eoc-dish', 'w0aa-mmc-dish']
    assert 'add channel 167' in steps[0]['instructions'] and 'w0aa-mmc-dish' in steps[0]['instructions']
    assert 'AREDN-165-40-v3 → AREDN-167-40-v3' in steps[1]['instructions']
    assert 'w0aa-eoc-dish' in steps[1]['instructions']


def test_no_remote_scan_outside_mesh_mode(mesh, monkeypatch):
    import wifi_survey
    mesh.node('w0aa-mmc-omni', rf_frequency='5905', rf_chanbw=20, rf_status='on', rf_mode='ptmp')
    mesh.node('w0aa-col-s', rf_frequency='5910', rf_chanbw=20, rf_status='on', rf_mode='adhoc')
    with pytest.raises(wifi_survey.ScanModeError):
        wifi_survey.scan_node('w0aa-mmc-omni')
    assert wifi_survey.queue_tonight(['w0aa-mmc-omni', 'w0aa-col-s']) == ['w0aa-col-s']
    tried = []
    monkeypatch.setattr(wifi_survey, 'scan_node', lambda n: tried.append(n))
    monkeypatch.setattr(wifi_survey.config, 'WIFI_SURVEY_ENABLED', False)
    wifi_survey.queue_tonight(['w0aa-col-s'])
    with wifi_survey.database.get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO wifi_survey_queue (node_name, requested_at) VALUES ('w0aa-mmc-omni', '2026-01-01')")
    wifi_survey.nightly_job()
    assert tried == ['w0aa-col-s']

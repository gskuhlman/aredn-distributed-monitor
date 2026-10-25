"""Channel planner: same data -> same plan whatever the Python hash seed
(the bug fixed in c436223), and a plan that separates colocated sectors."""

import json
import os
import subprocess
import sys

from conftest import ROOT

SITE = [
    # name, lat, lon, freq, width, azimuth, beamwidth
    ('t0st-tst-n', 42.0000, -91.6000, 5805, 20, 0, 90),
    ('t0st-tst-e', 42.0001, -91.6000, 5805, 20, 90, 90),
    ('t0st-tst-s', 42.0000, -91.6001, 5805, 20, 180, 90),
    ('t0st-tst-w', 42.0001, -91.6001, 5805, 20, 270, 90),
]
CLIENTS = [
    ('k1aa-home', 42.0300, -91.6000, 't0st-tst-n'),
    ('k1bb-home', 42.0000, -91.5600, 't0st-tst-e'),
    ('k1cc-home', 41.9700, -91.6000, 't0st-tst-s'),
    ('k1dd-home', 42.0000, -91.6400, 't0st-tst-w'),
]


def build(mesh):
    for name, lat, lon, f, w, az, bw in SITE:
        mesh.node(name, lat=lat, lon=lon, rf_frequency=str(f), rf_chanbw=w, rf_status='on', rf_mode='adhoc',
                  antenna_azimuth=az, antenna_beamwidth=bw, model='Ubiquiti Rocket 5AC Lite')
        mesh.infra(name, 'TST')
    for name, lat, lon, sector in CLIENTS:
        mesh.node(name, lat=lat, lon=lon, rf_frequency='5805', rf_chanbw=20, rf_status='on', rf_mode='adhoc',
                  antenna_beamwidth=20, antenna_azimuth=0)
        mesh.link(name, sector, 'RF', quality=95, snr=30)


PLAN_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import channel_planner
r = channel_planner.plan(site='TST', separate_sectors=True, top=1)
p = r['plans'][0]
print(json.dumps({'cost': p['cost'], 'radios': sorted((x['name'], x['new'].get('frequency'), x['new'].get('chanbw'))
                                                     for x in p['radios'])}))
"""


def _run(path, seed):
    env = dict(os.environ, DATABASE_PATH=path, PYTHONHASHSEED=str(seed), COUCH_URL='', PYTHONIOENCODING='utf-8')
    out = subprocess.run([sys.executable, '-c', PLAN_SCRIPT, ROOT], env=env, capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_plan_is_deterministic_across_hash_seeds(mesh):
    build(mesh)
    plans = [_run(mesh.path, seed) for seed in (0, 1, 12345)]
    assert plans[0] == plans[1] == plans[2]


def test_separate_sectors_gives_each_sector_its_own_channel(mesh):
    build(mesh)
    import channel_planner
    r = channel_planner.plan(site='TST', separate_sectors=True, top=1)
    p = r['plans'][0]
    chans = {x['name']: x['new'].get('frequency') for x in p['radios'] if x['name'].startswith('t0st-tst-')}
    assert len(set(chans.values())) == 4, chans
    # Each client follows its sector (it would lose its link otherwise).
    new = {x['name']: x['new'].get('frequency') for x in p['radios']}
    for name, _, _, sector in CLIENTS:
        assert new.get(name, chans[sector]) == chans[sector]

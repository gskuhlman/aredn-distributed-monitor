"""The TDWR-in-range setting: off by default (Cedar Rapids), on turns
5600-5650 into radar and penalises 5650-5660 in the planner's noise ranges."""

import channel_planner as cp


def test_tdwr_setting_switches_noise_ranges(mesh):
    cp._tdwr_cache[:] = [0.0, None]
    try:
        assert not cp.tdwr_nearby()
        ranges = cp.noise_ranges()
        assert 'below the 5 GHz ham band' in ranges[0]['label']
        assert not any(r['low'] == 5650 for r in ranges)
        cp.set_tdwr_nearby(True)
        ranges = cp.noise_ranges()
        assert ranges[0]['label'].startswith('TDWR weather radar')
        assert any(r['low'] == 5650 and r['high'] == 5660 for r in ranges)
        cp._tdwr_cache[:] = [0.0, None]   # read back from the settings table
        assert cp.tdwr_nearby()
    finally:
        cp.set_tdwr_nearby(False)
        cp._tdwr_cache[:] = [0.0, None]

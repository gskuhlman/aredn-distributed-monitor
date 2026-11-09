"""SSH radio health (ssh_health.py): the extra sections of one collection
(system, Ethernet, iw dev info, ath10k fw_stats, coverage class, uci
settings) and the alarms built on them, plus their use by the drift report
and channel planner. A fake ssh; never runs ssh."""

import time
from datetime import datetime, timedelta

import pytest

import ssh_collect as sc
import ssh_health

M = sc.MARK

STATION = """Station 24:5a:4c:11:22:33 (on wlan0)
\tinactive time:\t10 ms
\trx packets:\t{rx}
\ttx packets:\t{tx}
\ttx retries:\t{retries}
\ttx failed:\t4
\tsignal:  \t-64 [-64, -65] dBm
\tsignal avg:\t-64 [-64, -65] dBm
\ttx bitrate:\t180.0 MBit/s VHT-MCS 4 40MHz VHT-NSS 2
\trx bitrate:\t180.0 MBit/s VHT-MCS 4 40MHz VHT-NSS 2
\tconnected time:\t{connected} seconds
\tbeacon loss:\t3
\trx drop misc:\t12
\tavg ack signal:\t-83 dBm
\texpected throughput:\t80.0Mbps
"""
SURVEY = """Survey data from wlan0
\tfrequency:\t\t\t5660 MHz [in use]
\tnoise:\t\t\t\t-103 dBm
\tchannel active time:\t\t{active} ms
\tchannel busy time:\t\t{busy} ms
\tchannel receive time:\t\t{rx} ms
\tchannel transmit time:\t\t{tx} ms
"""
INFO = """Interface wlan0
\tifindex 9
\twdev 0x1
\taddr 24:5a:4c:aa:bb:cc
\ttype managed
\twiphy 0
\tchannel 132 (5660 MHz), width: 40 MHz, center1: 5670 MHz
\ttxpower 23.00 dBm
"""
FW = """             ath10k PDEV stats
             =================
          Channel noise floor        -103
             Channel TX power          46
               PHY error count          7
                 RTS bad count       1000
      MPDUs delivered to HTT     {rx_ok}
   MPDU errors (FCS, MIC, ENC)    {rx_err}
                   MPDU queued     {txq}
          MPDUs receive no ACK     {noack}
"""
PHY = """Wiphy phy0
\tCoverage class: 13 (up to 5850m)
\tAvailable Antennas: TX 0x3 RX 0x3
\tConfigured Antennas: TX 0x3 RX 0x3
"""
UCI = """wireless.radio0=wifi-device
wireless.radio0.channel='132'
wireless.radio0.chanbw='40'
wireless.radio0.country='HX'
wireless.radio0.distance='8047'
wireless.radio0.txpower='25'
wireless.@wifi-iface[0]=wifi-iface
wireless.@wifi-iface[0].device='radio0'
wireless.@wifi-iface[0].network='wifi'
wireless.@wifi-iface[0].mode='sta'
wireless.@wifi-iface[0].key='secret'
aredn.@lqm[0].min_snr='15'"""


def output(i, retries_per_pkt=0.1, connected=None, busy=(20, 10, 5), clock=0, carrier=1, crc=0,
           mem_avail=100000, rx_err_pct=2, slow=False):
    """Run i of a collection every 15 minutes: counters grow by 1000 s worth."""
    t = time_of(i)
    tx = 1000000 * (i + 1)
    active = 900000 * (i + 1)
    b, rx, txt = (int(active * p / 100) for p in busy)
    rx_total = 100000 * (i + 1)
    out = (f"{M} sys\nepoch {int(time.time()) + clock}\nload 0.50 0.40 0.30 1/80 999\nuptime {86400 + 900 * i}.5 1.0\n"
           f"cores 4\nMemTotal:  250000 kB\nMemAvailable: {mem_avail} kB\n"
           f"{M} eth\neth0 1000 full {carrier} {2 + (0 if carrier else i)} 0 {crc * i} 0\n"
           f"{M} station wlan0\n" + STATION.format(rx=rx_total, tx=tx, retries=int(tx * retries_per_pkt),
                                                   connected=connected(i) if connected else 3600 + 900 * i)
           + f"{M} survey wlan0\n" + SURVEY.format(active=active, busy=b, rx=rx, tx=txt)
           + f"{M} info wlan0\n{INFO}"
           + f"{M} fw_stats /sys/kernel/debug/ieee80211/phy0/ath10k/fw_stats\n"
           + FW.format(rx_ok=int(rx_total * (100 - rx_err_pct) / 100), rx_err=int(rx_total * rx_err_pct / 100),
                       txq=tx, noack=tx // 2))
    if slow:
        out += f"{M} phy\n{PHY}{M} uci\n{UCI}\n"
    return out + f"{M} end\n"


T0 = datetime.now().replace(microsecond=0) - timedelta(minutes=65)


def time_of(i):
    return T0 + timedelta(minutes=15 * i)


@pytest.fixture
def ssh(mesh, monkeypatch, tmp_path):
    key = tmp_path / 'key'
    key.write_text('x')
    (tmp_path / 'key.pub').write_text('ssh-ed25519 AAAA test')
    monkeypatch.setattr(sc.config, 'SSH_KEY_PATH', str(key))
    monkeypatch.setattr(sc.config, 'SSH_KNOWN_HOSTS', str(tmp_path / 'known_hosts'))
    import gateway
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: False)

    def fake(node, script):
        return 0, fake.out(fake.i), ''
    fake.i, fake.out = 0, output
    monkeypatch.setattr(sc, '_run_ssh', fake)
    mesh.node('w0aa-col-sq-2mmc')
    mesh.node('w0aa-mmc-sq-2col')
    mesh.infra('w0aa-col-sq-2mmc', 'COL')
    mesh.link('w0aa-col-sq-2mmc', 'w0aa-mmc-sq-2col', mac_address='26:5a:4c:11:22:33', distance=6171, snr=29)

    def run(n, out=lambda i: output(i, slow=i == 0), slow_first=True):
        events = []
        fake.out = out
        for i in range(n):
            fake.i = i
            sc.collect_node('w0aa-col-sq-2mmc', slow=slow_first and i == 0, now=time_of(i))
            events += sc.evaluate(time_of(i))
        return events
    return run


def kinds(events):
    return sorted({e['event_type'] for e in events})


def test_parsers():
    s = sc.parse_sys("epoch 1700000000\nload 0.5 0.4 0.3 1/80 9\nuptime 123.4 5\ncores 2\nMemTotal: 1000 kB\nMemAvailable: 400 kB")
    assert s == {'epoch': 1700000000, 'load1': 0.5, 'load5': 0.4, 'load15': 0.3, 'uptime_s': 123.4, 'cores': 2,
                 'mem_total_kb': 1000, 'mem_avail_kb': 400}
    e, = sc.parse_eth('eth0 100 half 1 7 0 3 - \nbogus line')
    assert e == {'iface': 'eth0', 'speed': 100, 'duplex': 'half', 'carrier': 1, 'carrier_changes': 7,
                 'rx_errors': 0, 'rx_crc_errors': 3, 'tx_errors': None}
    assert sc.parse_eth('eth1 -1 unknown 0 2 0 0 0')[0]['speed'] is None
    i = sc.parse_iw_info(INFO, 'wlan0')
    assert (i['wiphy'], i['type'], i['frequency'], i['width'], i['center1'], i['txpower_dbm']) == (0, 'managed', 5660, 40, 5670, 23.0)
    f = sc.parse_fw_stats(FW.format(rx_ok=900, rx_err=100, txq=50, noack=20))
    assert f['rx_mpdus'] == 900 and f['rx_mpdu_errors'] == 100 and f['mpdu_no_ack'] == 20 and f['txpower_half_dbm'] == 46
    assert f['ppdu_noack'] is None
    assert sc.parse_coverage(PHY) == {0: (13, 5850)}
    u = sc.parse_uci_settings(UCI)
    assert u['mode'] == 'sta' and u['distance_m'] == 8047 and u['txpower_set'] == 25 and u['lqm'] == {'min_snr': '15'}
    assert sc.parse_uci_settings(UCI.replace("network='wifi'", "network='wifi0'"))['channel'] == '132'   # Mesh mode
    assert sc.parse_uci_settings("wireless.radio0.channel='132'") is None
    st, = sc.parse_station_dump(STATION.format(rx=1, tx=2, retries=3, connected=99), 'wlan0')
    assert st['connected_s'] == 99 and st['beacon_loss'] == 3 and st['rx_drop_misc'] == 12 and st['ack_signal'] == -83


def test_quiet_link_raises_nothing(ssh):
    assert ssh(4) == []   # ACK window a little short (AREDN rounds down) but few retries: no alarm
    d = sc.node_details('w0aa-col-sq-2mmc')
    assert d['system']['clock_offset_s'] is not None and abs(d['system']['clock_offset_s']) < 5
    assert d['system']['mem_avail_pct'] == 40.0
    radio, = d['radios']
    assert radio['txpower_dbm'] == 23.0 and radio['frequency'] == 5660
    assert d['settings']['coverage_m'] == 5850 and d['settings']['longest_link']['peer'] == 'w0aa-mmc-sq-2col'
    assert 'secret' not in str(d['settings'])
    rates = d['station_rates']['w0aa-mmc-sq-2col']
    assert rates['retry_ratio'] == 0.1 and rates['reconnects_24h'] == 0
    assert d['fw']['phy0']['last']['no_ack_pct'] == 50.0 and d['fw']['phy0']['last']['rx_error_pct'] == 2.0
    assert d['busy_now']['others_pct'] == 5.0
    assert ssh_health.tx_power('w0aa-col-sq-2mmc') == 23.0


def test_retries_raise_and_escalate_the_ack_window(ssh):
    events = ssh(4, out=lambda i: output(i, retries_per_pkt=0.9, slow=i == 0))
    assert kinds(events) == ['radio_distance_short', 'radio_retries_high']
    ev = next(e for e in events if e['event_type'] == 'radio_retries_high')
    assert ev['details'].startswith('RF link to w0aa-mmc-sq-2col:') and '0.90 times' in ev['details']
    assert 'at 29 dB SNR' in ev['details'] and '50 missed ACKs per 100 frames' in ev['details']
    cov = next(e for e in events if e['event_type'] == 'radio_distance_short')
    assert 'coverage class 13, up to 5850 m' in cov['details'] and '6171 m' in cov['details']
    sev = {a['kind']: a['severity'] for a in sc.active_alarms()}
    assert sev['retries'] == 'warning' and sev['coverage'] == 'warning'
    with __import__('database').get_connection() as conn:
        bad, _ = ssh_health.coverage_conditions(conn, time_of(3))
    assert bad[('w0aa-col-sq-2mmc', 'coverage')][1] == 'warning'


def test_reconnects_interference_rx_errors_ethernet_clock_memory(ssh):
    events = ssh(5, out=lambda i: output(i, connected=lambda j: 5000 - 1000 * j,
                                         busy=(45, 10, 5), clock=600, crc=5, mem_avail=10000, rx_err_pct=30,
                                         slow=i == 0))
    assert kinds(events) == ['ethernet_problem', 'node_clock_skew', 'radio_interference',
                             'radio_reconnects', 'radio_rx_errors', 'resource_alarm']
    text = {e['event_type']: e['details'] for e in events}
    assert '30% of the airtime on 5660 MHz' in text['radio_interference']
    assert 'reconnected' in text['radio_reconnects']
    assert 'CRC errors +5' in text['ethernet_problem']
    assert ' s ahead of this PC' in text['node_clock_skew']   # ~600 s (epoch is whole seconds)
    assert '30% of the frames' in text['radio_rx_errors']


def test_reboot_is_not_a_reconnect(ssh):
    rows = [{'node_name': 'n', 'timestamp': 'a', 'connected_s': 5000}, {'node_name': 'n', 'timestamp': 'b', 'connected_s': 10}]
    assert ssh_health.reconnects(rows, {('n', 'a'): 9000, ('n', 'b'): 20}) == []
    assert ssh_health.reconnects(rows, {('n', 'a'): 9000, ('n', 'b'): 9900}) == ['b']


def test_drift_and_planner_use_ssh_settings(ssh, monkeypatch):
    ssh(3)
    import drift
    findings = []
    ctx = {'radios': {'w0aa-col-sq-2mmc': {}}, 'sites': []}
    rows = drift._check_ssh_settings(ctx, findings)
    row, = rows
    assert row['txpower_set'] == 25 and row['txpower_dbm'] == 23 and row['coverage_m'] == 5850
    titles = ' | '.join(f['title'] for f in findings)
    assert 'ACK window shorter' in titles and 'TX power set to 25 dBm, radio uses 23 dBm' in titles
    assert 'distance setting' not in titles   # 8047 m > 6171 m
    busy = ssh_health.busy_samples(24)
    assert busy['w0aa-col-sq-2mmc']['others_pct'] == 5.0
    import channel_planner as cp
    g = {'members': ['a'], 'busy_refs': [{'name': 'a', 'frequency': 5670, 'chanbw': 40, 'low': 5650, 'high': 5690,
                                          'others_pct': 20.0}]}
    cost, ref = cp._measured_busy(g, (5670, 40))
    assert cost == cp.BUSY_PCT_COST * 20 and ref['overlap_pct'] == 100
    assert cp._measured_busy(g, (5800, 20)) == (0, None)


def test_settings_backfilled_from_stored_snapshots(ssh, mesh):
    ssh(1)
    mesh.sql('DELETE FROM ssh_settings')   # as if collected before settings were read
    ssh(1, out=lambda i: output(i), slow_first=False)
    s, = ssh_health.settings('w0aa-col-sq-2mmc')
    assert s['distance_m'] == 8047 and s['coverage_m'] == 5850 and s['lqm'] == {'min_snr': '15'}


def test_rx_errors_count_frames_delivered_to_the_stack(mesh):
    import database
    sc.ensure_tables()
    now = datetime.now()
    with database.get_connection() as conn:
        for i, (htt, stack, err) in enumerate(((1000, 10000, 4000), (1100, 20000, 8000), (1200, 30000, 12000))):
            t = (now - timedelta(minutes=30 - 15 * i)).strftime(sc.TS_FMT)
            conn.execute("INSERT INTO ssh_fw_samples (node_name, timestamp, phy, rx_mpdus, rx_mpdus_stack, "
                         "rx_mpdu_errors, tx_mpdus, mpdu_no_ack) VALUES ('ap', ?, 'phy0', ?, ?, ?, 0, 0)",
                         (t, htt, stack, err))
        pts = ssh_health.fw_rates(conn, 'ap', (now - timedelta(hours=1)).strftime(sc.TS_FMT))['phy0']
    assert [p['rx_error_pct'] for p in pts] == [28.4, 28.4]   # 4000 of 14100, not 4000 of 4100


def test_too_few_samples_keep_an_alarm(mesh):
    import database
    sc.ensure_tables()
    now = datetime.now()
    with database.get_connection() as conn:
        conn.execute("INSERT INTO ssh_fw_samples (node_name, timestamp, phy, rx_mpdus, rx_mpdu_errors, tx_mpdus, "
                     "mpdu_no_ack) VALUES ('ap', ?, 'phy0', 1000, 9000, 0, 0)", (now.strftime(sc.TS_FMT),))
        bad, evaluated = ssh_health.rx_error_conditions(conn, now)
    assert bad == {} and evaluated == set()   # one sample: not judged, so an active alarm isn't cleared

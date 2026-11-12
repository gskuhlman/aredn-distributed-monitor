"""SSH radio details: parsing `iw` output, uci redaction, one collection with
a fake ssh, failure handling, chain / stream alarms, channel busy and log
findings. Never runs ssh."""

import json
from datetime import datetime, timedelta

import pytest

import database
import ssh_collect as sc

STATION = """Station 24:5a:4c:11:22:33 (on wlan0)
\tinactive time:\t10 ms
\trx packets:\t1000
\ttx packets:\t900
\ttx retries:\t120
\ttx failed:\t4
\tsignal:  \t-68 [-66, -79] dBm
\tsignal avg:\t-67 [-65, -78] dBm
\ttx bitrate:\t54.0 MBit/s VHT-MCS 3 40MHz VHT-NSS 1
\trx bitrate:\t130.0 MBit/s MCS 15
\texpected throughput:\t40.0Mbps
"""
SURVEY = """Survey data from wlan0
\tfrequency:\t\t\t5805 MHz
\tnoise:\t\t\t\t-92 dBm
Survey data from wlan0
\tfrequency:\t\t\t5825 MHz [in use]
\tnoise:\t\t\t\t-96 dBm
\tchannel active time:\t\t{active} ms
\tchannel busy time:\t\t{busy} ms
\tchannel receive time:\t\t{rx} ms
\tchannel transmit time:\t\t{tx} ms
"""
UCI = """wireless.radio0=wifi-device
wireless.@wifi-iface[1].ssid='MyLan'
wireless.@wifi-iface[1].key='hunter22'
vtun.@server[0].passwd='tunnelpw'
wireguard.wg0.private_key='abc='
aredn.@location[0].lat='42.0'"""


def _logline(t, text):
    return f"{t.strftime('%a %b')} {t.day:2d} {t.strftime('%H:%M:%S %Y')} kern.err kernel: {text}"


def _output(active=100000, busy=30000, station=STATION, slow=False):
    out = f"{sc.MARK} station wlan0\n{station}{sc.MARK} survey wlan0\n" + SURVEY.format(
        active=active, busy=busy, rx=busy - 5000, tx=5000)
    if slow:
        now = datetime.now()
        out += (f"{sc.MARK} phy\nWiphy phy0\n{sc.MARK} dmesg\n[ 1.0] booted\n{sc.MARK} logread\n"
                + _logline(now - timedelta(hours=1), 'ath10k_pci 0000:01:00.0: firmware crashed!') + '\n'
                + _logline(now - timedelta(days=3), 'ath10k_pci 0000:01:00.0: firmware crashed!') + '\n'
                + f"{sc.MARK} uci\n{UCI}\n")
    return out + f"{sc.MARK} end\n"


def test_parse_bitrates():
    assert sc.parse_bitrate('54.0 MBit/s VHT-MCS 3 40MHz VHT-NSS 1') == {
        'rate': 54.0, 'mcs': 3, 'nss': 1, 'width': 40, 'kind': 'VHT'}
    assert sc.parse_bitrate('130.0 MBit/s MCS 15') == {'rate': 130.0, 'mcs': 7, 'nss': 2, 'width': 20, 'kind': 'HT'}
    assert sc.parse_bitrate('300.0 MBit/s VHT-MCS 7 40MHz short GI VHT-NSS 2')['nss'] == 2
    assert sc.parse_bitrate('24.0 MBit/s')['kind'] == 'legacy'


def test_parse_station_and_survey():
    s, = sc.parse_station_dump(STATION, 'wlan0')
    assert s['mac'] == '24:5a:4c:11:22:33' and s['signal'] == -68 and s['chains'] == [-66, -79]
    assert s['chains_avg'] == [-65, -78] and s['tx_nss'] == 1 and s['tx_retries'] == 120 and s['expected_mbps'] == 40
    v, = sc.parse_survey_dump(SURVEY.format(active=10, busy=5, rx=3, tx=1), 'wlan0')
    assert v['frequency'] == 5825 and v['noise'] == -96 and v['busy_ms'] == 5


def test_uci_secrets_are_removed():
    red = sc.redact_uci(UCI)
    for secret in ('hunter22', 'tunnelpw', 'abc='):
        assert secret not in red
    assert "ssid='MyLan'" in red and "lat='42.0'" in red and 'wireless.radio0=wifi-device' in red


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
    calls = []

    def fake(node, script):
        calls.append(script)
        return fake.result
    fake.result = (0, _output(), '')
    monkeypatch.setattr(sc, '_run_ssh', fake)
    mesh.node('w0aa-eoc-dish')
    mesh.node('w0aa-mmc-dish')
    mesh.infra('w0aa-eoc-dish', 'EOC')
    mesh.link('w0aa-eoc-dish', 'w0aa-mmc-dish', both=False, mac_address='26:5a:4c:11:22:33')
    fake.calls = calls
    return fake


def test_collect_stores_samples_and_redacted_snapshots(ssh):
    ssh.result = (0, _output(slow=True), '')
    r = sc.collect_node('w0aa-eoc-dish', slow=True)
    assert r == {'node': 'w0aa-eoc-dish', 'ok': True, 'stations': 1, 'surveys': 1, 'snapshots': 4}
    assert 'iw dev "$d" station dump' in ssh.calls[0] and 'uci show' in ssh.calls[0] and 'scan' not in ssh.calls[0]
    d = sc.node_details('w0aa-eoc-dish')
    assert d['stations'][0]['peer'] == 'w0aa-mmc-dish'   # matched on the last five MAC octets
    uci = next(s for s in d['snapshots'] if s['kind'] == 'uci')
    assert 'hunter22' not in sc.snapshot(uci['id'])['text']
    found, = sc.log_findings(24, {'w0aa-eoc-dish'})
    assert found['label'] == 'radio firmware crash' and found['count'] == 1   # the 3-day-old line is left out
    assert sc.status()['nodes'][0]['last_slow']


def test_auth_failure_is_recorded_and_backed_off(ssh):
    ssh.result = (255, '', 'root@w0aa-eoc-dish.local.mesh: Permission denied (publickey).')
    r = sc.collect_node('w0aa-eoc-dish')
    assert r['error_kind'] == 'auth' and 'public key' in r['hint']
    assert sc.collect_node('w0aa-eoc-dish')['skipped']            # not every 15 minutes
    assert not sc.collect_node('w0aa-eoc-dish', manual=True).get('skipped')
    assert sc.classify_error('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!') == 'host_key'


def test_chain_alarm_raises_and_clears(ssh):
    t0 = datetime.now() - timedelta(minutes=60)
    events = []
    for i in range(4):   # 13 dB between chains, one stream; LQM SNR 30 dB
        ssh.result = (0, _output(active=100000 * (i + 1), busy=30000 * (i + 1)), '')
        sc.collect_node('w0aa-eoc-dish', slow=False, now=t0 + timedelta(minutes=15 * i))
        events += sc.evaluate(t0 + timedelta(minutes=15 * i))
    ev, = events
    assert ev['event_type'] == sc.EVENT_ALARM and ev['details'].startswith('RF link to w0aa-mmc-dish:')
    assert '13 dB louder on one chain' in ev['details'] and 'one stream' in ev['details']
    assert 'at 30 dB SNR' in ev['details']   # the link's LQM SNR, not signal - survey noise (28)
    good = STATION.replace('[-65, -78]', '[-55, -57]').replace('[-66, -79]', '[-56, -58]').replace(
        'VHT-MCS 3 40MHz VHT-NSS 1', 'VHT-MCS 7 40MHz VHT-NSS 2').replace('-68', '-56').replace('-67 ', '-55 ')
    events = []
    for i in range(4, 9):
        ssh.result = (0, _output(active=100000 * (i + 1), station=good), '')
        sc.collect_node('w0aa-eoc-dish', slow=False, now=t0 + timedelta(minutes=15 * i))
        events += sc.evaluate(t0 + timedelta(minutes=15 * i))
    assert [e['event_type'] for e in events] == [sc.EVENT_CLEARED] and sc.active_alarms() == []


def test_channel_busy_from_counter_deltas(ssh):
    now = datetime.now()
    for i, (active, busy) in enumerate(((100000, 30000), (200000, 70000), (50000, 10000))):   # then a reset
        ssh.result = (0, _output(active=active, busy=busy), '')
        sc.collect_node('w0aa-eoc-dish', slow=False, now=now - timedelta(minutes=30 - 10 * i))
    b = sc.channel_busy('w0aa-eoc-dish')
    assert b['busy_pct'] == 40.0 and b['frequency'] == 5825


def test_run_needs_a_key(mesh, monkeypatch, tmp_path):
    monkeypatch.setattr(sc.config, 'SSH_KEY_PATH', str(tmp_path / 'none'))
    assert 'No SSH key' in sc.run()['error'] and not sc.enabled()


def test_ath10k_nonsense_chains_and_routine_watchdog_lines():
    # Real output from a PowerBeam 5AC 400 on AREDN 4.26.
    s, = sc.parse_station_dump(STATION.replace('-68 [-66, -79]', '-56 [90, 83]').replace('-67 [-65, -78]', '-54 [-50, 50]'), 'wlan0')
    assert s['chains'] == [None, None] and s['chains_avg'] == [-50, None] and s['signal'] == -56
    assert sc._signal('-54 [-27, -55] dBm') == (-54, [None, -55])   # far louder than the total: nonsense
    routine = ('init: - watchdog -', 'daemon.notice manager.hw_watchdog[2062]: Starting task',
               'daemon.notice manager.wireguard_watchdog[2062]: Starting task')
    assert not any(p.search(line) for line in routine for _, _, p in sc.LOG_PATTERNS)
    assert any(p.search('watchdog: BUG: soft lockup - CPU#0 stuck') for _, _, p in sc.LOG_PATTERNS)


def test_extra_ath10k_chains_are_dropped(ssh):
    # SXTsq 5 ac: a 2x2 radio whose station dump lists four chains.
    four = STATION.replace('-68 [-66, -79]', '-64 [-64, -65, -79, -79]').replace('-67 [-65, -78]', '-64 [-63, -64, -78, -78]')
    ssh.result = (0, _output(station=four), '')
    sc.collect_node('w0aa-eoc-dish', slow=False)
    s, = sc.node_details('w0aa-eoc-dish')['stations']
    assert s['chains_avg'] == [-63, -64]   # no iw phy yet: two chains
    assert sc.rx_chains('Available Antennas: TX 0x3 RX 0x3\n\tConfigured Antennas: TX 0x1 RX 0x1') == 1
    assert sc.rx_chains('Wiphy phy0') is None


def test_run_stops_between_nodes_on_shutdown(monkeypatch):
    """After stop() (SIGTERM), the session in progress finishes and no further
    node is started."""
    import gateway
    import ssh_collect as sc
    monkeypatch.setattr(sc, 'ensure_tables', lambda: None)
    monkeypatch.setattr(sc, 'key_present', lambda: True)
    monkeypatch.setattr(gateway, 'is_reachable', lambda: True)
    monkeypatch.setattr(sc, 'evaluate', lambda now: [])
    monkeypatch.setattr(sc, '_prune', lambda now: None)
    monkeypatch.setattr(sc, '_stopping', sc.threading.Event())
    seen = []

    def collect(node, **kw):
        seen.append(node)
        assert sc.busy()
        if node == 'b':
            sc.stop()
        return {'node': node, 'ok': True}
    monkeypatch.setattr(sc, 'collect_node', collect)
    result = sc.run(['a', 'b', 'c'])
    assert seen == ['a', 'b'] and result['stopped'] and not sc.busy()


def test_logread_every_15_minutes_at_most(mesh, monkeypatch):
    assert 'logread' in sc.node_script(True) and 'logread' not in sc.node_script(False)
    assert 'logread' in sc.node_script(False, logread=True)
    now = datetime(2026, 11, 12, 14, 0, 0)
    at = lambda m: (now - timedelta(minutes=m)).strftime(sc.TS_FMT)
    assert sc.logread_due({'logread_at': None}, now)
    assert not sc.logread_due({'logread_at': at(10)}, now) and sc.logread_due({'logread_at': at(15)}, now)
    monkeypatch.setattr(sc.config, 'SYSLOG_PULL_MINUTES', 5)     # never under 15
    assert not sc.logread_due({'logread_at': at(10)}, now)
    monkeypatch.setattr(sc.config, 'SYSLOG_PULL_MINUTES', 60)
    assert not sc.logread_due({'logread_at': at(30)}, now)


def test_pull_logs_on_request(mesh, monkeypatch, tmp_path):
    key = tmp_path / 'k'
    key.write_text('x')
    monkeypatch.setattr(sc.config, 'SSH_KEY_PATH', str(key))
    monkeypatch.setattr(sc, 'targets', lambda: ['w0aa-x'])
    now = datetime.now()
    calls = []

    def fake(node, script, args=None):
        calls.append(script)
        return 0, f"{sc.MARK} logread\n{_logline(now, 'hello from the node')}\n{sc.MARK} end\n", ''
    monkeypatch.setattr(sc, '_run_ssh', fake)
    assert sc.pull_logs('w0aa-y')['error_kind'] == 'not_target' and not calls
    r = sc.pull_logs('w0aa-x', now=now)
    assert r['ok'] and r['added'] == 1 and 'logread' in calls[0] and 'station' not in calls[0]
    assert sc.pull_logs('w0aa-x', now=now + timedelta(seconds=20))['recent'] and len(calls) == 1
    assert sc.pull_logs('w0aa-x', now=now + timedelta(seconds=90))['ok'] and len(calls) == 2
    with database.get_connection() as conn:
        assert conn.execute("SELECT logread_at FROM ssh_status WHERE node_name = 'w0aa-x'").fetchone()[0]

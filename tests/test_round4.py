"""Round 4 (TODO #63-74): Prometheus samples and alarms, per-link alerts,
measured traffic, link potential, spectrum history / scan reminders, embeds.
Prometheus is faked: nothing leaves the machine."""

from datetime import datetime, timedelta

import pytest

import capacity
import config
import embeds
import link_alerts
import link_potential
import mesh_diagnostics
import prom_source
import prom_watch
import spectrum_view
import wifi_survey

TS = '%Y-%m-%d %H:%M:%S'
INST = ('10.0.0.1:8080', 'aredn')


def _r(value, **labels):
    m = {'instance': INST[0], 'job': INST[1]}
    m.update(labels)
    return {'metric': m, 'value': value}


FAKE = {
    'node_memory_MemTotal_bytes': [_r(128e6)],
    'node_memory_MemAvailable_bytes': [_r(64e6)],
    'node_babel_route_total': [_r(500)],
    'node_babel_neighbor_total': [_r(6)],
    'node_wifi_station_transmit_mcs': [_r(2, mac='D0:EA:11:00:00:01', device='wlan0')],
    'node_wifi_station_transmit_rate_bits_per_second': [_r(45e6, mac='d0:ea:11:00:00:01', device='wlan0')],
    'node_lqm_tracker_snr': [_r(28, hostname='W0AA-MMC-SQ', mac='d0:ea:11:00:00:01', type='RF')],
    'node_lqm_tracker_ping_quality': [_r(100, hostname='w0aa-mmc-sq', mac='d0:ea:11:00:00:01', type='RF')],
}


@pytest.fixture
def prom(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PROMETHEUS_URL', 'http://prom.test:9090')
    monkeypatch.setattr(prom_source, '_instances', {'at': 0, 'map': {}, 'info': {}})

    def query(expr):
        if expr == 'node_aredn_info':
            return [_r(1, node='W0AA-COL-SQ'), {'metric': {'instance': INST[0], 'job': 'other', 'node': 'W0AA-COL-SQ'},
                                               'value': 1}]
        if expr == 'node_uname_info':
            return []
        if 'rate(node_network_transmit_bytes_total' in expr and expr.startswith('8 *'):
            return [_r(4e6, device='wlan0'), _r(1e3, device='lo'), _r(2e5, device='wg0'), _r(1e5, device='wg1')]
        return FAKE.get(expr, [])
    monkeypatch.setattr(prom_source, 'query', query)
    return mesh


def _store_series(node, now, n, **fields):
    """n node samples 10 min apart ending at now."""
    prom_source.ensure_tables()
    rows = []
    for i in range(n):
        ts = (now - timedelta(minutes=10 * (n - 1 - i))).strftime(TS)
        f = {k: (v(i) if callable(v) else v) for k, v in fields.items()}
        rows.append({node: {'node': f, 'ifaces': {}, 'stations': {}, 'lqm': {}}})
    for i, r in enumerate(rows):
        prom_source.store(r, now - timedelta(minutes=10 * (n - 1 - i)))


def test_collect_maps_instances_stations_and_interfaces(prom):
    nodes = prom_source.collect()
    assert list(nodes) == ['w0aa-col-sq']                       # one job per instance, lower-cased
    n = nodes['w0aa-col-sq']
    assert n['node']['mem_used_pct'] == 50.0
    sta, = n['stations'].values()
    assert sta['peer'] == 'w0aa-mmc-sq' and sta['tx_mcs'] == 2   # named from the LQM tracker's MAC
    assert n['ifaces']['wlan0']['kind'] == 'rf' and 'lo' not in n['ifaces']
    prom_source.store(nodes)
    with prom_source.database.get_connection() as conn:
        ifaces = {r['iface']: r['tx_bps'] for r in conn.execute('SELECT * FROM prom_iface_samples')}
    assert ifaces == {'wlan0': 4e6, 'tunnels': pytest.approx(3e5)}  # tunnels summed into one row
    assert prom_source.latest_nodes()['w0aa-col-sq']['routes'] == 500


def test_node_details_reports_unreachable_prometheus(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PROMETHEUS_URL', 'http://prom.test:9090')
    monkeypatch.setattr(prom_source, '_detail_cache', {})
    monkeypatch.setattr(prom_source, '_instances', {'at': 0, 'map': {}, 'info': {}})

    def boom(expr):
        raise prom_source.PromError('Prometheus unreachable (ConnectTimeout)')
    monkeypatch.setattr(prom_source, 'query', boom)
    d = prom_source.node_details('w0aa-col-sq')
    assert d['available'] is False and 'unreachable' in d['error']


def test_resource_conditions_and_alarm_transitions(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PROMETHEUS_URL', 'http://prom.test:9090')
    mesh.node('w0aa-col-sq')
    now = datetime.now().replace(microsecond=0)
    _store_series('w0aa-col-sq', now, 3, mem_total=128e6, mem_avail=8e6, flash_size=9e6, flash_avail=0.5e6,
                  cpu_busy=97, routes=500, neighbours=5)
    latest = prom_source.latest_nodes()['w0aa-col-sq']
    cond = prom_watch.node_conditions(latest, prom_source.node_history('w0aa-col-sq'), now)
    assert set(cond) == {'mem_low', 'flash_low', 'cpu_busy'}
    assert cond['mem_low'][0] == 'warning'                          # 6.25%: below 10, above 5
    first = prom_watch.evaluate(now=now)
    assert first == []                                              # needs two snapshots in a row
    second = prom_watch.evaluate(now=now + timedelta(minutes=10))
    assert sorted(e['details'].split(':')[0] for e in second) == ['CPU pegged', 'Flash nearly full', 'Low memory']
    assert {a['kind'] for a in prom_watch.active('w0aa-col-sq')} == {'mem_low', 'flash_low', 'cpu_busy'}


def test_route_drop_against_24h_median(mesh):
    now = datetime.now().replace(microsecond=0)
    hist = [{'timestamp': (now - timedelta(hours=h)).strftime(TS), 'routes': 800} for h in range(20, 1, -1)]
    latest = {'timestamp': now.strftime(TS), 'routes': 300, 'neighbours': 4}
    cond = prom_watch.node_conditions(latest, hist + [latest], now)
    assert 'route_drop' in cond and '62% fewer' in cond['route_drop'][1]


def test_leak_trend_projects_days_to_low(mesh):
    now = datetime.now().replace(microsecond=0)
    samples = [{'timestamp': (now - timedelta(hours=4 * i)).strftime(TS), 'mem_total': 128 * 1048576,
                'mem_avail': (40 - 2 * (24 - i) / 6) * 1048576, 'boot_time': 1000.0} for i in range(24, -1, -1)]
    t = prom_watch.leak_trend(samples, now)
    assert t['mb_per_day'] == pytest.approx(-2.0, abs=0.05) and t['r2'] > 0.99
    assert t['days_to_low'] == pytest.approx((32 - 12.8) / 2, abs=0.2)


def test_mcs_drop_with_steady_snr_and_low_mcs_for_snr(mesh):
    prom_source.ensure_tables()
    now = datetime.now().replace(microsecond=0)
    with prom_source.database.get_connection() as conn:
        for i in range(30):
            t = (now - timedelta(minutes=10 * i)).strftime(TS)
            recent = i < 3
            conn.execute('INSERT INTO prom_station_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         ('a', t, 'm1', 'b', 2 if recent else 12, None, (20e6 if recent else 78e6), None, -60, 1, 1))
            conn.execute('INSERT INTO prom_lqm_samples VALUES (?,?,?,?,?,?,?,?,?)',
                         ('a', t, 'b', 'RF', 100, 2, 28 if recent else 29, 27, 90))
        cond = prom_watch.station_conditions(now, conn)
        assert ('a', 'b') in cond and 'while SNR held' in cond[('a', 'b')][1]
        low = prom_watch.low_mcs_for_snr(conn, now, hours=0.4)
    assert low and low[0]['mcs'] == 2 and low[0]['supports'] == 7   # 28 dB supports MCS 7 per stream


def test_link_alert_raises_and_clears(mesh):
    mesh.node('sector')
    mesh.node('client')
    mesh.link('sector', 'client', snr=12, quality=90)
    rid = link_alerts.save_rule({'source': 'sector', 'target': 'client', 'min_snr': 15})
    assert link_alerts.evaluate() == []
    ev, = link_alerts.evaluate()
    assert ev['event_type'] == 'link_alert' and ev['details'].startswith('RF link to client: SNR 12 dB below 15')
    mesh.sql("UPDATE links SET snr = 25 WHERE source_node = 'sector'")
    assert link_alerts.evaluate() == []
    ev, = link_alerts.evaluate()
    assert ev['event_type'] == 'link_alert_cleared'
    rule, = link_alerts.list_rules()
    assert rule['id'] == rid and rule['active'] is False and rule['values']['snr'] == 25


def test_link_alert_missing_link_and_validation(mesh):
    mesh.node('sector')
    with pytest.raises(ValueError):
        link_alerts.save_rule({'source': 'sector', 'target': 'sector'})
    link_alerts.save_rule({'source': 'sector', 'target': 'gone', 'min_snr': 10})
    link_alerts.evaluate()
    ev, = link_alerts.evaluate()
    assert 'link not reported' in ev['details']


def test_measured_traffic_split_by_station_packets(mesh):
    prom_source.ensure_tables()
    capacity.ensure_tables()
    t = datetime.now().strftime(TS)
    with prom_source.database.get_connection() as conn:
        conn.execute('INSERT INTO prom_iface_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('ap', t, 'wlan0', 'rf', 1e6, 8e6, 10, 100, 0, 0, 0, 0))
        for mac, peer, pps in (('m1', 'c1', 75), ('m2', 'c2', 25)):
            conn.execute('INSERT INTO prom_station_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         ('ap', t, mac, peer, 7, 7, 65e6, 65e6, -60, pps, 1))
        med, p90, pps = capacity._measured(conn, 'ap', 'c1', (datetime.now() - timedelta(hours=1)).strftime(TS))
    assert med == 6.0 and pps == 75                               # 75% of 8 Mbps
    busy, = capacity.busiest_radios()
    assert busy['node'] == 'ap' and busy['total_mbps'] == 9.0


def test_link_potential_rates_and_report(mesh):
    assert link_potential.rate_for_snr(28, 20) == 130.0           # MCS 7 x 2 streams
    assert link_potential.rate_for_snr(10, 40) == pytest.approx(81.0, abs=0.1)
    assert link_potential.rate_for_snr(1) == 0.0
    mesh.node('a', lat=42.0, lon=-91.6, rf_frequency='5800', rf_chanbw=20, antenna_gain=23)
    mesh.node('b', lat=42.05, lon=-91.6, rf_frequency='5800', rf_chanbw=20, antenna_gain=23)
    mesh.link('a', 'b', snr=10)
    r = link_potential.report()
    assert len(r['points']) == 2 and r['curve']
    p = r['points'][0]
    assert p['distance_km'] == pytest.approx(5.56, abs=0.05) and p['predicted_snr'] > 30


def test_spectrum_changes_reminders_and_history(mesh):
    mesh.node('w0aa-col-rkt-n', lat=42.0, lon=-91.6, rf_frequency='5900', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc')
    mesh.infra('w0aa-col-rkt-n', 'COL')
    mesh.node('w0aa-col-rkt-s', lat=42.0, lon=-91.6, rf_frequency='5775', rf_chanbw=20, rf_status='on',
              rf_mode='adhoc')
    mesh.infra('w0aa-col-rkt-s', 'COL')
    old = (datetime.now() - timedelta(days=3)).strftime(TS)
    wifi_survey.ensure_tables()
    with wifi_survey.database.get_connection() as conn:
        conn.execute("INSERT INTO wifi_survey (node_name, taken_at, bssid, ssid, frequency, signal, is_mesh) "
                     "VALUES ('w0aa-col-rkt-n', ?, 'aa:01', 'Old', 5745, -60, 0)", (old,))
    wifi_survey.import_text('w0aa-col-rkt-n', 'BSS aa:bb:cc:00:00:02(on wlan0)\n\tfreq: 5825.0\n'
                                              '\tsignal: -55.00 dBm\n\tSSID: New\n')
    r = spectrum_view.build(band=(5640, 5930))
    scan, = r['rows'][0]['scans']
    ch = scan['changes']
    assert [a['ssid'] for a in ch['new']] == ['New'] and [a['ssid'] for a in ch['gone']] == ['Old']
    cells = {c['low']: x for c, x in zip(scan['cells'], ch['cells'])}
    assert cells[5820] == 'new' and cells[5740] == 'gone'
    rem, = r['reminders']                                          # rkt-s was never scanned
    assert rem['radio'] == 'w0aa-col-rkt-s' and rem['last_scan'] is None
    assert wifi_survey.queue_tonight(['w0aa-col-rkt-s', 'nobody']) == ['w0aa-col-rkt-s']
    assert spectrum_view.build(band=(5640, 5930))['reminders'][0]['queued'] is True
    # As of 2 days ago: only the old survey, and rkt-s back on its earlier channel.
    mesh.sql("INSERT INTO events (timestamp, event_type, node_name, details, severity) VALUES "
             "(?, 'frequency_change', 'w0aa-col-rkt-s', 'Frequency changed: 5745 MHz -> 5775 MHz', 'warning')",
             ((datetime.now() - timedelta(days=1)).strftime(TS),))
    h = spectrum_view.build(band=(5640, 5930), at=datetime.now() - timedelta(days=2))
    assert h['history_channels'] == {'w0aa-col-rkt-s': 5745.0}
    row = h['rows'][0]
    assert {o['name']: o['frequency'] for o in row['ours']}['w0aa-col-rkt-s'] == 5745.0
    assert [a['ssid'] for a in row['scans'][0]['aps']] == ['Old'] and h['reminders'] == []


def test_embeds_node_card_and_badge(mesh):
    mesh.node('w0aa-home', uptime_seconds=3 * 86400, model='hAP', firmware_version='4.26.7.0', rf_frequency='5805',
              rf_chanbw='20')
    mesh.node('w0aa-peer')
    mesh.link('w0aa-home', 'w0aa-peer', snr=31, quality=97)
    card = embeds.node_card('W0AA-HOME')
    assert card['state'] == 'up' and card['uptime'] == '3d' and card['rf'][0]['target_node'] == 'w0aa-peer'
    html = embeds.render(card, 'http://monitor/', refresh=60, theme='dark')
    assert 'http-equiv="refresh" content="60"' in html and 'w0aa-peer' in html and '<script' not in html
    assert '5805 MHz / 20 MHz' in html
    assert 'up 3d' in embeds.node_badge('w0aa-home') and embeds.node_card('nope') is None


def test_mesh_health_lists_resource_findings(mesh, monkeypatch):
    monkeypatch.setattr(config, 'PROMETHEUS_URL', 'http://prom.test:9090')
    mesh.node('w0aa-col-sq', scan_depth=0)
    now = datetime.now().replace(microsecond=0)
    _store_series('w0aa-col-sq', now, 2, mem_total=128e6, mem_avail=4e6)
    report = mesh_diagnostics.run_health_check(hours=24)
    res = [f for f in report['findings'] if f['category'] == 'resources']
    assert res and res[0]['severity'] == 'critical' and 'Low memory' in res[0]['title']

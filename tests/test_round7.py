"""Round 7: incidents (site outage power vs network, starting node), suppressions,
alert acknowledgement / escalation, audit redaction, syslog parsing and storage,
degradation trends, tunnel uptime, tape slots, call-quality grades, boards."""

from datetime import date, datetime, timedelta

import pytest
from flask import Flask, jsonify

import acks
import audit
import boards
import call_quality
import config
import degradation
import incidents
import suppressions
import syslog_rx
import tape
import tunnels

TS = '%Y-%m-%d %H:%M:%S'


def poll(mesh, node, t, ok, uptime=None):
    mesh.sql('INSERT INTO node_health_history (node_name, timestamp, reachable, uptime_seconds) VALUES (?, ?, ?, ?)',
             (node, t.strftime(TS), 1 if ok else 0, uptime))


# ------------------------------------------------------------ incidents --

def _site(mesh, now, rebooted):
    for n in ('w0gq-eoc-a', 'w0gq-eoc-b'):
        mesh.node(n)
        mesh.infra(n, 'EOC')
        poll(mesh, n, now - timedelta(minutes=20), True, 900000)
        poll(mesh, n, now - timedelta(minutes=10), False)
    assert incidents.after_scan(now - timedelta(minutes=9), spof_report={'links': []})
    opened = incidents.list_incidents(status='active')['incidents']
    assert len(opened) == 1 and opened[0]['kind'] == 'site_outage' and opened[0]['site'] == 'EOC'
    for n in ('w0gq-eoc-a', 'w0gq-eoc-b'):
        poll(mesh, n, now, True, 120 if rebooted else 900000 + 1200)
    incidents.after_scan(now, spof_report={'links': []})
    return incidents.get(opened[0]['id'])


def test_site_outage_power(mesh):
    inc = _site(mesh, datetime.now(), rebooted=True)
    assert inc['status'] == 'resolved' and inc['cause'] == 'power'


def test_site_outage_network(mesh):
    inc = _site(mesh, datetime.now(), rebooted=False)
    assert inc['status'] == 'resolved' and inc['cause'] == 'network'


def test_starting_node_incident_covers_other_events(mesh):
    incidents.on_event({'event_type': 'gateway_lost', 'timestamp': datetime.now().strftime(TS)})
    assert 'Starting node' in incidents.covering({'event_type': 'node_offline', 'node_name': 'x'})
    assert incidents.covering({'event_type': 'incident_opened', 'node_name': None}) is None
    incidents.on_event({'event_type': 'gateway_restored', 'timestamp': datetime.now().strftime(TS)})
    assert incidents.list_incidents(status='active')['incidents'] == []


def test_manual_incident_record(mesh):
    inc = incidents.create({'title': 'COL S one-way', 'nodes': ['W0GQ-COL-RKT-S']})
    assert inc['nodes'] == ['w0gq-col-rkt-s'] and inc['status'] == 'open'
    inc = incidents.update(inc['id'], {'status': 'monitoring', 'suspected_cause': 'kf9mt location'}, 'op')
    assert inc['status'] == 'monitoring'
    assert any('Status open -> monitoring' in t['text'] for t in inc['timeline'])
    with pytest.raises(ValueError):
        incidents.create({'title': ''})


# --------------------------------------------------------- suppressions --

def test_suppression_needs_reason_and_future_date(mesh):
    future = (date.today() + timedelta(days=10)).isoformat()
    with pytest.raises(ValueError):
        suppressions.create({'node_name': 'a', 'reason': '', 'review_date': future})
    with pytest.raises(ValueError):
        suppressions.create({'node_name': 'a', 'reason': 'x', 'review_date': '2020-01-01'})
    s = suppressions.create({'node_name': 'a', 'reason': 'known flaky', 'review_date': future,
                             'event_types': ['node_offline']})
    assert suppressions.covering({'event_type': 'node_offline', 'node_name': 'a'})
    assert suppressions.covering({'event_type': 'node_reboot', 'node_name': 'a'}) is None
    suppressions.end(s['id'])
    assert suppressions.covering({'event_type': 'node_offline', 'node_name': 'a'}) is None


def test_suppression_overdue(mesh):
    s = suppressions.create({'node_name': 'a', 'reason': 'r', 'review_date': (date.today() + timedelta(days=1)).isoformat()})
    mesh.sql('UPDATE suppressions SET review_date = ? WHERE id = ?', ((date.today() - timedelta(days=1)).isoformat(), s['id']))
    assert suppressions.list_all()['overdue'] == 1


# ----------------------------------------------------------------- acks --

def test_ack_repeat_and_escalate(mesh):
    alert = {'rule': 'r', 'title': 't', 'line': 'node down', 'events': [{'severity': 'critical', 'event_type': 'node_offline'}]}
    token = acks.register(alert, ['discord'])
    assert acks.register(alert, ['discord']) == token          # once per alert
    cfg = {'escalation': {'enabled': True, 'min_severity': 'critical', 'repeat_minutes': 30, 'max_repeats': 2,
                          'escalate_minutes': 60, 'channels': ['email']}}
    now = datetime.now()
    assert acks.due(cfg, now + timedelta(minutes=10)) == []
    assert [a for _, a in acks.due(cfg, now + timedelta(minutes=31))] == ['repeat']
    assert [a for _, a in acks.due(cfg, now + timedelta(minutes=61))] == ['escalate']
    row = acks.acknowledge(token=token, by='KD0X')
    assert row['acked'] and 'token' not in row
    assert acks.due(cfg, now + timedelta(minutes=61)) == []
    assert acks.acknowledge(token='nope') is None


# ---------------------------------------------------------------- audit --

def test_audit_redacts_and_skips_login(mesh):
    assert audit.redact({'password': 'x', 'nested': {'webhook_url': 'y', 'name': 'ok'}}) == \
        {'password': '********', 'nested': {'webhook_url': '********', 'name': 'ok'}}
    app = Flask(__name__)
    audit.install(app)

    @app.route('/api/thing', methods=['POST'])
    def thing():
        return jsonify({'ok': True})

    @app.route('/api/auth/login', methods=['POST'])
    def login():
        return jsonify({'ok': True})

    c = app.test_client()
    c.post('/api/thing', json={'token': 'secret', 'note': 'hi'})
    c.post('/api/auth/login', json={'password': 'hunter2'})
    c.get('/api/thing')
    rows = audit.list_entries()
    assert [r['path'] for r in rows] == ['/api/auth/login', '/api/thing']
    assert 'secret' not in rows[1]['body'] and 'hi' in rows[1]['body']
    assert 'hunter2' not in (rows[0]['body'] or '')


# --------------------------------------------------------------- syslog --

def test_syslog_parse_formats():
    now = datetime(2026, 11, 9, 22, 0)
    m = syslog_rx.parse(b'<30>Nov  9 21:58:22 w0gq-col-rkt-s netifd[123]: Interface wan is up', '10.1.1.1', now)
    assert (m['host'], m['program'], m['pid'], m['severity'], m['facility']) == ('w0gq-col-rkt-s', 'netifd', 123, 6, 'daemon')
    assert m['node_time'] == '2026-11-09 21:58:22'
    m = syslog_rx.parse(b'<11>1 2026-11-09T21:58:22Z node app 7 - - boom', None, now)
    assert (m['program'], m['severity'], m['message']) == ('app', 3, 'boom')
    assert syslog_rx.parse(b'not syslog') is None
    # A December message read in January belongs to last year.
    m = syslog_rx.parse(b'<14>Dec 31 23:59:00 n p: x', None, datetime(2027, 1, 1, 0, 1))
    assert m['node_time'].startswith('2026-12-31')


def test_syslog_store_dedupe_collapse_chips(mesh):
    text = '\n'.join([
        'Sun Nov  8 08:20:36 2026 authpriv.info dropbear[3114]: Child connection from 10.42.92.116:65171',
        'Sun Nov  8 08:35:36 2026 authpriv.info dropbear[3200]: Child connection from 10.42.92.116:65999',
        'Sun Nov  8 08:36:00 2026 kern.err kernel: ath10k_pci 0000:00:00.0: failed to request fw stats: -145',
        'garbage line'])
    assert syslog_rx.ingest_logread('w0gq-col-rkt-s', text) == 3
    assert syslog_rx.ingest_logread('w0gq-col-rkt-s', text) == 0       # re-import adds nothing
    r = syslog_rx.query(node='w0gq-col-rkt-s', collapse=True)
    assert r['total'] == 3 and len(r['messages']) == 2
    assert next(m for m in r['messages'] if m['program'] == 'dropbear')['count'] == 2
    errs = syslog_rx.query(max_severity=3)
    assert [m['program'] for m in errs['messages']] == ['kernel']
    assert set(errs['messages'][0]['chips']) >= {'kernel', 'radio'}
    assert [m['program'] for m in syslog_rx.query(chips=['auth'])['messages']] == ['dropbear', 'dropbear']
    assert 'dropbear' in syslog_rx.csv_text(node='w0gq-col-rkt-s')


def test_syslog_node_from_ip(mesh):
    mesh.node('kd0x-node', ip='10.5.5.5')
    msg = syslog_rx.parse(b'<13>kernel: hello', '10.5.5.5')
    syslog_rx.store([msg])
    assert syslog_rx.query(node='kd0x-node')['total'] == 1


# ------------------------------------------------------------ degradation --

def test_degradation_flags_steady_slide_only():
    days = [(i, f'2026-10-{i + 1:02d}', 40 - 0.2 * i) for i in range(20)]   # -6 dB a month
    t = degradation.trend(days)
    assert t['slope_month'] == pytest.approx(-6, abs=0.1)
    assert degradation.judge(t, None)
    flat = degradation.trend([(i, f'2026-10-{i + 1:02d}', 40 + (i % 2)) for i in range(20)])
    assert degradation.judge(flat, None) == []


# -------------------------------------------------------------- tunnels --

def test_tunnel_uptime_and_health():
    start, end = '2026-11-09 00:00:00', '2026-11-09 10:00:00'
    states = [('2026-11-09 02:00:00', 'down'), ('2026-11-09 04:00:00', 'up')]
    assert tunnels.uptime_pct(states, start, end, initial='up') == pytest.approx(80, abs=0.1)
    assert tunnels.health_pct(100, 0, 20) == 100
    assert tunnels.health_pct(50, None, None) == 50


# ----------------------------------------------------------------- tape --

def test_tape_slots():
    start = '2026-11-09 00:00:00'
    rows = [('a', '2026-11-09 00:01:00', 1, 0), ('a', '2026-11-09 00:06:00', 0, 0), ('a', '2026-11-09 00:07:00', 1, 1)]
    d = tape.build(rows, start, 3, 5)['a']
    assert ''.join(d['tape']) == 'hm-'        # missed beats degraded in the same slot
    assert (d['heard'], d['degraded'], d['missed']) == (1, 1, 1)
    assert tape.callsign('kd0cgn-prox') == 'KD0CGN' and tape.callsign('mac-0200') == 'OTHER'


# --------------------------------------------------------- call quality --

def test_call_quality_grades():
    assert call_quality.grade(10, 4.2) == 'ok'
    assert call_quality.grade(config.CALL_JITTER_WARN_MS + 1, 4.2) == 'warning'
    assert call_quality.grade(5, config.CALL_MOS_CRIT) == 'critical'
    assert call_quality.grade(None, None) is None


# --------------------------------------------------------------- boards --

def test_agency_board_default_sites(mesh):
    mesh.node('w0gq-eoc-a')
    mesh.node('w0gq-eoc-b', is_active=0)
    mesh.infra('w0gq-eoc-a', 'EOC')
    mesh.infra('w0gq-eoc-b', 'EOC')
    tile = boards.agencies()['agencies'][0]
    assert (tile['site'], tile['nodes_up'], tile['nodes_total'], tile['state']) == ('EOC', 1, 2, 'degraded')
    boards.save_config([{'name': 'Hospital', 'nodes': 'w0gq-eoc-a'}])
    tile = boards.agencies()['agencies'][0]
    assert (tile['name'], tile['state'], tile['members']) == ('Hospital', 'ok', ['w0gq-eoc-a'])


def test_ticker_lists_warnings_and_incidents(mesh):
    import database
    database.log_event('node_offline', 'a', 'a stopped answering', 'warning')
    database.log_event('node_discovered', 'b', 'new', 'info')
    incidents.create({'title': 'Investigating'})
    items = boards.ticker()['items']
    assert items[0]['kind'] == 'incident'
    assert [i['node'] for i in items if i['kind'] == 'event'] == ['a']

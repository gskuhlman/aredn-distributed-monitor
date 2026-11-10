"""History rollup (rollup.py): hourly aggregates and the raw cleanup."""

from datetime import datetime, timedelta

import config
import rollup

TS = '%Y-%m-%d %H:%M:%S'


def add(mesh, t, snr, lq=100, tx_packets=None, src='a', tgt='b', sample_type='scan'):
    mesh.sql('''INSERT INTO link_history (timestamp, source_node, target_node, link_type, snr, lq, quality,
                tx_packets, routable, sample_type) VALUES (?, ?, ?, 'RF', ?, ?, 90, ?, 1, ?)''',
             (t.strftime(TS), src, tgt, snr, lq, tx_packets, sample_type))


def test_quantile():
    assert rollup.quantile([None, 1, 2, 3, 4], 0.5) == 2.5
    assert rollup.quantile([5], 0.1) == 5
    assert rollup.quantile([None], 0.5) is None


def test_rollup_and_cleanup(mesh, monkeypatch):
    rollup._tables_ready = False
    now = datetime(2026, 11, 9, 12, 30)
    old = now - timedelta(days=40)
    for i, snr in enumerate((20, 22, 24, 26)):
        add(mesh, old.replace(minute=0) + timedelta(minutes=10 * i), snr, tx_packets=1000 + 100 * i)
    add(mesh, old.replace(minute=0) + timedelta(minutes=50), 99, sample_type='incident')
    for i in range(3):
        add(mesh, now.replace(minute=0) - timedelta(hours=1) + timedelta(minutes=15 * i), 30)
    add(mesh, now.replace(minute=5), 31)    # current hour: not complete, not rolled up
    r = rollup.run(now)
    assert r['rows'] == 2
    rows = mesh.sql('SELECT * FROM link_history_hourly ORDER BY hour')
    first = dict(rows[0])
    assert first['samples'] == 4 and first['snr_med'] == 23 and first['snr_p10'] == 20.6
    assert first['tx_packets_delta'] == 300 and first['routable_pct'] == 100
    assert dict(rows[1])['samples'] == 3
    # a second run does nothing new
    assert rollup.run(now)['rows'] == 0
    monkeypatch.setattr(config, 'LINK_HISTORY_RAW_DAYS', 30)
    monkeypatch.setattr(config, 'ROLLUP_RETENTION_DAYS', 0)
    c = rollup.cleanup(now)
    assert c['raw_deleted'] == 5 and c['hourly_deleted'] == 0
    assert mesh.sql('SELECT COUNT(*) FROM link_history')[0][0] == 4
    assert len(rollup.hourly('a', 'b', (now - timedelta(days=60)).strftime(TS), now.strftime(TS))) == 2


def test_cleanup_never_drops_unrolled_hours(mesh, monkeypatch):
    rollup._tables_ready = False
    now = datetime(2026, 11, 9, 12, 30)
    add(mesh, now - timedelta(days=40), 20)
    rollup.ensure_tables()
    with rollup.database.get_connection() as conn:   # pretend the rollup stopped 45 days ago
        rollup._meta(conn, 'rolled_until', (now - timedelta(days=45)).strftime(TS))
    monkeypatch.setattr(rollup, 'run', lambda now=None: None)
    assert rollup.cleanup(now)['raw_deleted'] == 0

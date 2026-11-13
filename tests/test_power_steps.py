"""One-way signal steps as likely TX power changes (power_steps.py)."""

from datetime import datetime, timedelta

import change_log
import database
import power_steps

NOW = datetime(2026, 11, 13, 12, 20)


def _fill(conn, rx, tx, start, hours, level_fn):
    for h in range(hours):
        t = start + timedelta(hours=h)
        for m in (5, 20, 35, 50):
            ts = t + timedelta(minutes=m)
            conn.execute("""INSERT INTO link_history (timestamp, source_node, target_node, link_type, quality, signal)
                            VALUES (?, ?, ?, 'RF', 100, ?)""",
                         (ts.strftime('%Y-%m-%d %H:%M:%S'), rx, tx, str(level_fn(ts))))


def test_one_way_step_logged_as_power_change(mesh):
    start = NOW - timedelta(hours=30)
    step_at = datetime(2026, 11, 13, 7, 40)
    with database.get_connection() as conn:
        _fill(conn, 'w0aa-col', 'w0aa-far', start, 31, lambda t: -62.5 if t < step_at else -58.8)
        _fill(conn, 'w0aa-far', 'w0aa-col', start, 31, lambda t: -60.3)
        _fill(conn, 'w0aa-a', 'w0aa-b', start, 31, lambda t: -70 + (t.hour % 2) * 0.5)   # steady pair
    assert power_steps.detect(NOW) == 1
    (r,) = change_log.list_changes()
    assert (r['node_name'], r['peer_node'], r['category'], r['origin']) == ('w0aa-far', 'w0aa-col', 'power', 'scan')
    assert r['timestamp'] == '2026-11-13 07:30:00'
    assert '3.7 dB stronger' in r['description'] and 'between 07:00 and 08:00' in r['description']
    assert power_steps.detect(NOW) == 0                       # once
    assert power_steps.detect(NOW + timedelta(hours=2)) == 0  # not again a little later


def test_step_both_ways_is_not_a_power_change(mesh):
    start = NOW - timedelta(hours=30)
    step_at = datetime(2026, 11, 13, 7, 0)
    with database.get_connection() as conn:
        _fill(conn, 'w0aa-col', 'w0aa-far', start, 31, lambda t: -62.5 if t < step_at else -58.8)
        _fill(conn, 'w0aa-far', 'w0aa-col', start, 31, lambda t: -60.3 if t < step_at else -56.5)
    assert power_steps.detect(NOW) == 0


def test_noisy_signal_is_not_a_step(mesh):
    start = NOW - timedelta(hours=30)
    with database.get_connection() as conn:
        _fill(conn, 'w0aa-col', 'w0aa-far', start, 31, lambda t: -62 + (t.hour % 3) * 2.5)
    assert power_steps.detect(NOW) == 0

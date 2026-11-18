"""Long-term monthly rollups (monthly.py, TODO #83)."""

from datetime import datetime, timedelta

import monthly

TS = '%Y-%m-%d %H:%M:%S'


def test_days_then_months_kept(mesh):
    now = datetime(2026, 11, 3, 8, 0)
    # Polls every 30 min from 29 Oct to 2 Nov; k1bb down for 6 hours on 30 Oct.
    t = datetime(2026, 10, 29, 0, 0)
    while t < datetime(2026, 11, 3):
        for name in ('k1aa-home', 'k1bb-home'):
            down = name == 'k1bb-home' and datetime(2026, 10, 30, 6) <= t < datetime(2026, 10, 30, 12)
            mesh.sql('INSERT INTO node_health_history (timestamp, node_name, reachable, uptime_seconds) VALUES (?, ?, ?, ?)',
                     (t.strftime(TS), name, 0 if down else 1,
                      None if down else int((t - datetime(2026, 10, 1)).total_seconds())))
        t += timedelta(minutes=30)

    r = monthly.run(now)
    assert r['days'] == 4 and r['months'] == 1          # 30 Oct .. 2 Nov judged; October stored
    rows = {x[0]: x for x in mesh.sql('SELECT node_name, uptime_pct, days FROM monthly_nodes WHERE month = ?',
                                       ('2026-10',))}
    assert rows['k1aa-home'][1] == 100 and rows['k1aa-home'][2] == 2
    assert rows['k1bb-home'][1] < 90
    assert monthly.run(now) == {'days': 0, 'months': 0}   # nothing twice

    rep = monthly.report(now=now)
    assert rep['months'] == ['2026-10', '2026-11']
    assert rep['summary'][-1]['partial'] and not rep['summary'][0]['partial']
    assert rep['nodes']['k1aa-home'] == {'2026-10': 100.0, '2026-11': 100.0}

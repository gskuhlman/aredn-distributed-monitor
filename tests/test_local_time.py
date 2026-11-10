"""Local (Central) time: node clock check, hygiene item, labels, UTC migration."""

from datetime import datetime

import change_log
import database
import gateway
import hygiene
import local_time
import node_clock

STATUS = '''<div class="ctrl" hx-get="status/e/time" hx-target="#ctrl-modal">
    <div class="t">{t}</div>
    <div class="s">{s}</div>
</div>'''
NOW = datetime(2026, 11, 10, 13, 49, 30)


def test_parse_status_clock_formats():
    p = node_clock.parse_status(STATUS.format(t='1:49 pm', s='cst (ntp)'), NOW)
    assert (p['zone'], p['source'], p['offset_minutes']) == ('cst', 'ntp', 0)
    assert node_clock.judge(p) is True
    assert node_clock.parse_status(STATUS.format(t='13:52', s='cst (ntp)'), NOW)['offset_minutes'] == 3
    utc = node_clock.parse_status(STATUS.format(t='7:49 pm', s='utc (ntp)'), NOW)
    assert utc['offset_minutes'] == 360 and node_clock.judge(utc) is False
    assert 'UTC' in node_clock.describe(utc)
    # "time" label but the clock is right: on Central time
    assert node_clock.judge(node_clock.parse_status(STATUS.format(t='1:50 pm', s='time (ntp)'), NOW)) is True
    # around midnight the offset wraps instead of reading as a day off
    late = datetime(2026, 11, 10, 23, 58)
    assert node_clock.parse_status(STATUS.format(t='12:01 am', s='cst (ntp)'), late)['offset_minutes'] == 3
    assert node_clock.parse_status('<html>no clock</html>', NOW) is None


def test_from_uci():
    assert node_clock.from_uci("system.@system[0].timezone='CST6CDT,M3.2.0,M11.1.0'")['central'] is True
    assert node_clock.from_uci("system.@system[0].timezone='UTC'")['central'] is False
    assert node_clock.from_uci('nothing here') is None


def test_check_all_and_hygiene(mesh, monkeypatch):
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: True)
    mesh.node('kf0abc-qth', lat=42.0, lon=-91.6)
    mesh.node('kf0utc-qth', lat=42.0, lon=-91.6)
    mesh.node('kf0gone-qth', lat=42.0, lon=-91.6)
    now = datetime.now()
    utc_time = (now.hour + 6) % 24
    pages = {'kf0abc-qth': STATUS.format(t=now.strftime('%H:%M'), s='cst (ntp)'),
             'kf0utc-qth': STATUS.format(t=f'{utc_time}:{now.minute:02d}', s='utc (ntp)')}

    def fetcher(name):
        if name not in pages:
            return None, 'ConnectTimeout'
        return node_clock.parse_status(pages[name]), None

    r = node_clock.check_all(fetcher=fetcher)
    assert (r['checked'], r['central'], r['off'], r['failed']) == (2, 1, ['kf0utc-qth'], 1)
    rows = {n['name']: n for n in hygiene.report()['nodes']}
    keys = lambda n: {c['key']: c['ok'] for c in rows[n]['checks']}
    assert keys('kf0abc-qth')['timezone'] is True
    assert keys('kf0utc-qth')['timezone'] is False
    assert any('Central' in f for f in rows['kf0utc-qth']['fixes'])
    assert 'timezone' not in keys('kf0gone-qth')     # never read: not judged


def test_check_all_skipped_when_starting_node_down(mesh, monkeypatch):
    monkeypatch.setattr(gateway, 'is_reachable', lambda *a, **k: False)
    assert node_clock.check_all(fetcher=lambda n: (None, 'x'))['aborted']


def test_change_log_created_at_is_local(mesh):
    c = change_log.add_change('kf0abc-qth', 'moved antenna', category='antenna')
    created = datetime.strptime(c['created_at'][:19], '%Y-%m-%d %H:%M:%S')
    assert abs((created - datetime.now()).total_seconds()) < 120


def test_utc_to_local_and_labels():
    from datetime import timezone
    utc = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)
    assert database._utc_to_local('2026-07-01 18:00:00') == utc.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    assert database._utc_to_local('junk') is None
    assert local_time.abbr('2026-01-15 12:00:00') in ('CST', 'CDT')
    assert local_time.label().startswith('Central time (')
    assert local_time.stamp('2026-11-10 13:20:44').startswith('2026-11-10 13:20 ')

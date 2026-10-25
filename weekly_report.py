"""
Weekly report (Reports tab "Weekly Report").

A saved summary of one week (default: the 7 days before it is built):
  - headline: starting-node availability and outages, really-down /
    intermittent nodes (unreachable.py), warning events, maintenance windows;
  - worst links and nodes by health score (health_score.py), with the
    biggest drop;
  - new problems: warning/critical events of a (type, node) not seen in the
    week before, and nodes really down or intermittent this week but not last;
  - infrastructure: Infrastructure Review findings and the RF link verdicts
    that aren't OK (infra_audit.py, stored settings, no live refresh);
  - changes from the change log;
  - capacity flags and backbone capacity (capacity.py), single points of
    failure with the internet down (spof.py), weather-flagged links.
Each section is built on its own, so one failing leaves a note instead of
losing the report.

Stored in SQLite `weekly_reports` (JSON + plain text). The scheduled job runs
WEEKLY_REPORT_DAY at WEEKLY_REPORT_TIME and sends the text on the channels in
the `weekly_report_channels` setting (falls back to WEEKLY_REPORT_CHANNELS),
through notify.send, so channel secrets stay in notify's local config.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import config
import database

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
PROBLEM_SEVERITIES = ('warning', 'critical')
# Frequent, mostly routine events: counted, not listed one by one.
ROUTINE_EVENTS = ('link_dropped', 'node_degraded', 'link_restored')
CHAT_LIMIT = 1800
_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS weekly_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                trigger TEXT,
                summary TEXT,
                text TEXT,
                sent TEXT
            )
        ''')
    _tables_ready = True


def get_channels():
    raw = database.get_setting('weekly_report_channels', None)
    if raw is None:
        return list(config.WEEKLY_REPORT_CHANNELS)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = [c.strip() for c in str(raw).split(',')]
    return [c for c in value if c in ('email', 'discord', 'ntfy', 'webhook')]


def set_channels(channels):
    clean = [c for c in (channels or []) if c in ('email', 'discord', 'ntfy', 'webhook')]
    database.set_setting('weekly_report_channels', json.dumps(clean))
    return clean


def _fmt(dt):
    return dt.strftime(TS_FMT)


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M')


def _section(out, key, fn):
    try:
        out[key] = fn()
    except Exception as exc:
        logger.exception('weekly report section %s failed', key)
        out[key] = {'error': f'{type(exc).__name__}: {exc}'}


def _headline(start, end):
    import collector_health
    import maintenance
    import unreachable
    ch = collector_health.report(_fmt(start), _fmt(end))['summary']
    un = unreachable.summarize(unreachable.classify(_fmt(start), _fmt(end)))
    with database.get_connection() as conn:
        warn = conn.execute(f'''SELECT COUNT(*) FROM events WHERE timestamp >= ? AND timestamp < ?
                                AND severity IN ({','.join('?' * len(PROBLEM_SEVERITIES))})''',
                            (_fmt(start), _fmt(end)) + PROBLEM_SEVERITIES).fetchone()[0]
        reboots = conn.execute('''SELECT COUNT(*) FROM events WHERE timestamp >= ? AND timestamp < ?
                                  AND event_type = 'node_reboot' ''', (_fmt(start), _fmt(end))).fetchone()[0]
        polled = conn.execute('''SELECT COUNT(DISTINCT node_name) FROM node_health_history
                                 WHERE timestamp >= ? AND timestamp < ? AND probe_via IS NULL AND reachable = 1''',
                              (_fmt(start), _fmt(end))).fetchone()[0]
    return {'starting_node_availability_pct': ch.get('availability_pct'), 'starting_node_outages': ch.get('outages'),
            'starting_node_outage_minutes': ch.get('outage_minutes'), 'lost_scans': ch.get('lost_scans'),
            'nodes_answering': polled, 'really_down': un['really_down_nodes'], 'intermittent': un['intermittent_nodes'],
            'explained_unreachable': un['explained_nodes'], 'warning_events': warn, 'reboots': reboots,
            'maintenance_windows': len(maintenance.windows_between(start, end))}


def _worst(start, end, kind, n=8):
    import health_score
    r = health_score.scores(_iso(start), _iso(end), kind=kind, limit=100000)
    items = [i for i in r['items'] if i['avg'] is not None and not i.get('never_ok')]
    items.sort(key=lambda i: (i['avg'], i['min'] if i['min'] is not None else 101))
    out = []
    for i in items[:n]:
        out.append({'name': i.get('name') or f"{i['a']} ↔ {i['b']}", 'avg': i['avg'], 'min': i['min'],
                    'now': i['now'], 'infrastructure': i['infrastructure'],
                    'drop': {'points': i['drop']['points'], 'at': i['drop']['at']} if i['drop'] else None})
    return out


def _new_problems(start, end):
    import unreachable
    prev = start - (end - start)
    with database.get_connection() as conn:
        rows = conn.execute(f'''SELECT event_type, node_name, severity, MIN(timestamp), COUNT(*), MAX(details)
                                FROM events WHERE timestamp >= ? AND timestamp < ?
                                AND severity IN ({','.join('?' * len(PROBLEM_SEVERITIES))})
                                GROUP BY event_type, node_name''',
                            (_fmt(start), _fmt(end)) + PROBLEM_SEVERITIES).fetchall()
        before = {(r[0], r[1]) for r in conn.execute(f'''SELECT DISTINCT event_type, node_name FROM events
                                WHERE timestamp >= ? AND timestamp < ?
                                AND severity IN ({','.join('?' * len(PROBLEM_SEVERITIES))})''',
                            (_fmt(prev), _fmt(start)) + PROBLEM_SEVERITIES)}
        oldest = conn.execute('SELECT MIN(timestamp) FROM events').fetchone()[0]
    # No history before this week: nothing is "new", say so instead.
    baseline = bool(oldest and oldest < _fmt(start - timedelta(days=1)))
    new = [{'type': r[0], 'node': r[1], 'severity': r[2], 'first': r[3], 'count': r[4], 'detail': r[5]}
           for r in rows if (r[0], r[1]) not in before]
    routine = defaultdict(int)
    for e in new:
        if e['type'] in ROUTINE_EVENTS:
            routine[e['type']] += 1
    events = [e for e in new if e['type'] not in ROUTINE_EVENTS]
    rank = {'critical': 0, 'warning': 1}
    events.sort(key=lambda e: (rank.get(e['severity'], 2), -e['count'], e['first']))
    now_bad = {n: r for n, r in unreachable.classify(_fmt(start), _fmt(end)).items()
               if r['cause'] in ('really_down', 'intermittent')}
    was_bad = {n for n, r in unreachable.classify(_fmt(prev), _fmt(start)).items()
               if r['cause'] in ('really_down', 'intermittent')}
    nodes = [{'name': n, 'cause': r['cause_label'], 'failed_polls': r['failed_polls']}
             for n, r in sorted(now_bad.items()) if n not in was_bad]
    return {'events': events[:25], 'events_total': len(events), 'nodes': nodes,
            'routine': {t: n for t, n in routine.items()}, 'baseline': baseline}


def _infrastructure():
    import infra_audit
    r = infra_audit.run_review(refresh=False)
    if r.get('error'):
        return {'error': r['error']}
    bad = [l for l in r['links'] if l.get('severity') not in ('ok', 'info')]
    return {'counts': r['counts'],
            'findings': [{'severity': f['severity'], 'title': f['title']} for f in r['findings']
                         if f['severity'] in ('critical', 'warning')][:12],
            'links': [{'a': l['a'], 'b': l['b'], 'severity': l['severity'], 'verdict': l['verdict']} for l in bad][:12],
            'links_total': len(r['links'])}


def _changes(start, end):
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute('''
            SELECT timestamp, node_name, peer_node, category, description, source FROM change_log
            WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp''', (_fmt(start), _fmt(end)))]


def _verdicts(start, end):
    import change_verdict
    return [{'change_id': v['change_id'], 'node': v['node_name'], 'time': v['change_time'], 'verdict': v['verdict'],
             'text': v['text']}
            for v in change_verdict.list_verdicts(days=max(1, (datetime.now() - start).days + 1))
            if start.strftime(TS_FMT) <= (v['created_at'] or '') < end.strftime(TS_FMT)
            and v['verdict'] not in ('duplicate', 'n/a')]


def _capacity():
    import capacity
    r = capacity.report(hours=24 * 7)
    return {'flags': [{'link': f"{l['source']} → {l['target']}", 'flag': l['flag']} for l in r['links'] if l['flag']],
            'backbone': [{'link': f"{l['source']} → {l['target']}", 'capacity': l['capacity'],
                          'basis': l['capacity_basis'], 'phy': l['phy_mbps'], 'spare': l['spare_mbps']}
                         for l in r['links'] if l['backbone']]}


def _spof():
    import spof
    r = spof.report(exclude_tunnels=True)
    return [{'link': f"{l['a']} ↔ {l['b']}", 'type': l['type'], 'quality': l['quality'], 'behind': l['behind'],
             'behind_infra': l['behind_infra'], 'risk': l['risk']} for l in r.get('links', [])[:5]]


def _weather(start, end):
    import weather
    r = weather.summary(start=_fmt(start), end=_fmt(end))
    return [{'link': f"{x['source']} → {x['target']}", 'verdicts': x['verdicts']} for x in r.get('flagged', [])][:8]


def build(end=None, days=7):
    end = end or datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    out = {'start': _fmt(start), 'end': _fmt(end), 'generated_at': _fmt(datetime.now())}
    _section(out, 'headline', lambda: _headline(start, end))
    _section(out, 'worst_links', lambda: _worst(start, end, 'link'))
    _section(out, 'worst_nodes', lambda: _worst(start, end, 'node'))
    _section(out, 'new_problems', lambda: _new_problems(start, end))
    _section(out, 'infrastructure', _infrastructure)
    _section(out, 'changes', lambda: _changes(start, end))
    _section(out, 'change_verdicts', lambda: _verdicts(start, end))
    _section(out, 'capacity', _capacity)
    _section(out, 'spof', _spof)
    _section(out, 'weather', lambda: _weather(start, end))
    return out


def _err(section):
    return isinstance(section, dict) and section.get('error')


def render_text(r, short=False):
    """Plain text for email/webhook (short=True: chat-sized summary)."""
    lines = [f"AREDN weekly report {r['start'][:10]} – {r['end'][:10]}"]
    h = r.get('headline') or {}
    if not _err(h):
        lines.append(f"Starting node: {h['starting_node_availability_pct'] if h['starting_node_availability_pct'] is not None else '?'}% available, "
                     f"{h['starting_node_outages']} outage(s), {h['lost_scans']} lost scan(s). "
                     f"Nodes answering: {h['nodes_answering']}; really down {h['really_down']}, intermittent {h['intermittent']}. "
                     f"Warnings: {h['warning_events']}, reboots: {h['reboots']}, maintenance windows: {h['maintenance_windows']}.")

    def block(title, items, fmt, limit):
        if _err(items):
            lines.append(f'\n{title}: unavailable ({items["error"]})')
            return
        if not items:
            return
        lines.append(f'\n{title}:')
        for i in items[:limit]:
            lines.append('  - ' + fmt(i))

    lim = 4 if short else 10
    block('Worst links', r.get('worst_links'), lambda i: f"{i['name']}: avg {i['avg']}, min {i['min']}"
          + (f", dropped {i['drop']['points']} at {i['drop']['at'][5:16]}" if i['drop'] else ''), lim)
    block('Worst nodes', r.get('worst_nodes'), lambda i: f"{i['name']}: avg {i['avg']}, min {i['min']}", lim)
    np_ = r.get('new_problems') or {}
    if _err(np_):
        lines.append(f"\nNew problems: unavailable ({np_['error']})")
    else:
        if not np_.get('baseline'):
            lines.append("\nNew problems: no history before this week, so everything below is this week's, not new.")
        if np_.get('routine'):
            lines.append('Routine: ' + ', '.join(f"{t.replace('_', ' ')} on {n} node(s)" for t, n in np_['routine'].items()) + '.')
        block(f"New problems ({np_.get('events_total', 0)} new event kind(s))", np_.get('events'),
              lambda e: f"{e['severity']}: {e['type'].replace('_', ' ')} on {e['node']} ×{e['count']} (first {e['first'][5:16]})",
              lim)
        block('Newly unreachable', np_.get('nodes'), lambda n: f"{n['name']}: {n['cause']} ({n['failed_polls']} failed polls)", lim)
    inf = r.get('infrastructure') or {}
    if _err(inf):
        lines.append(f"\nInfrastructure: unavailable ({inf['error']})")
    elif inf:
        c = inf.get('counts', {})
        lines.append(f"\nInfrastructure review: {c.get('critical', 0)} critical, {c.get('warning', 0)} warning finding(s); "
                     f"{len(inf.get('links', []))} of {inf.get('links_total', 0)} infrastructure RF link(s) not OK.")
        block('Infrastructure findings', inf.get('findings'), lambda f: f"{f['severity']}: {f['title']}", lim)
        block('Infrastructure links', inf.get('links'), lambda l: f"{l['a']} ↔ {l['b']}: {l['verdict']}", lim)
    block('Changes', r.get('changes'), lambda c: f"{c['timestamp'][5:16]} {c['node_name']}: {c['category']} – {c['description'] or ''}", lim)
    block('Before/after verdicts', r.get('change_verdicts'), lambda v: f"{v['verdict']}: {v['text']}", lim)
    cap = r.get('capacity') or {}
    if not _err(cap):
        block('Capacity drops', cap.get('flags'), lambda f: f"{f['link']}: {f['flag']}", lim)
        if not short:
            block('Backbone capacity', cap.get('backbone'), lambda b: f"{b['link']}: {b['capacity'] if b['capacity'] is not None else '?'} Mbps"
                  + (f" (PHY {b['phy']})" if b['phy'] else '') + (f", spare {b['spare']}" if b['spare'] is not None else ''), lim)
    block('Single points of failure with the internet down', r.get('spof'),
          lambda s: f"{s['link']} ({s['type']}{', quality ' + str(s['quality']) + '%' if s['quality'] is not None else ''}): "
                    f"{s['behind']} node(s) behind" + (f", {s['behind_infra']} infrastructure" if s['behind_infra'] else ''),
          3 if short else 5)
    if not short:
        block('Weather-flagged links', r.get('weather'), lambda w: f"{w['link']}: {'; '.join(w['verdicts'])}", lim)
    text = '\n'.join(lines)
    if short and len(text) > CHAT_LIMIT:
        text = text[:CHAT_LIMIT - 20] + '\n… (full report in the app)'
    return text


def save(r, trigger='manual', sent=None):
    ensure_tables()
    with database.get_connection() as conn:
        cur = conn.execute('''INSERT INTO weekly_reports (created_at, start, end, trigger, summary, text, sent)
                              VALUES (?, ?, ?, ?, ?, ?, ?)''',
                           (r['generated_at'], r['start'], r['end'], trigger, json.dumps(r), render_text(r),
                            json.dumps(sent or {})))
        return cur.lastrowid


def _update_sent(report_id, sent):
    with database.get_connection() as conn:
        conn.execute('UPDATE weekly_reports SET sent = ? WHERE id = ?', (json.dumps(sent), report_id))


def send(report_id, channels=None, send_fn=None):
    """Send a saved report on channels (default: the configured ones).
    Returns {channel: 'sent' | error}."""
    import notify
    r = get(report_id)
    if not r:
        return None
    channels = channels if channels is not None else get_channels()
    title = f"AREDN weekly report {r['start'][:10]} – {r['end'][:10]}"
    sent = dict(r.get('sent') or {})
    for ch in channels:
        body = render_text(r['summary'], short=ch in ('discord', 'ntfy'))
        try:
            (send_fn or notify.send)(ch, title, body)
            sent[ch] = 'sent'
            notify.log_notification('weekly_report', ch, [], title, 'sent')
        except Exception as exc:
            sent[ch] = str(exc)
            notify.log_notification('weekly_report', ch, [], title, 'failed', str(exc))
    _update_sent(report_id, sent)
    return sent


def generate(trigger='manual', send_now=False, end=None, send_fn=None):
    r = build(end=end)
    report_id = save(r, trigger=trigger)
    sent = send(report_id, send_fn=send_fn) if send_now else {}
    return {'id': report_id, 'sent': sent}


def list_reports(limit=20):
    ensure_tables()
    with database.get_connection() as conn:
        return [dict(r, sent=json.loads(r['sent'] or '{}')) for r in conn.execute(
            'SELECT id, created_at, start, end, trigger, sent FROM weekly_reports ORDER BY id DESC LIMIT ?',
            (limit,))]


def get(report_id):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM weekly_reports WHERE id = ?', (report_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    r['summary'] = json.loads(r['summary'] or '{}')
    r['sent'] = json.loads(r['sent'] or '{}')
    return r


def delete(report_id):
    ensure_tables()
    with database.get_connection() as conn:
        return conn.execute('DELETE FROM weekly_reports WHERE id = ?', (report_id,)).rowcount > 0


def install(scheduler, job):
    if not config.WEEKLY_REPORT_ENABLED:
        return
    day = config.WEEKLY_REPORT_DAY if config.WEEKLY_REPORT_DAY in DAYS else 'mon'
    try:
        h, m = (int(x) for x in config.WEEKLY_REPORT_TIME.split(':')[:2])
    except ValueError:
        h, m = 7, 0
    scheduler.add_job(job, 'cron', day_of_week=day, hour=h, minute=m, id='weekly_report', replace_existing=True)
    logger.info('Weekly report scheduled %s %02d:%02d', day, h, m)

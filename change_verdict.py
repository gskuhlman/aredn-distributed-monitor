"""
Automatic before/after verdict for radio changes (Changes tab, weekly
report, notifications).

Every hour (own APScheduler job) each change-log entry in a radio category
(channel, power, antenna) that is at least CHANGE_VERDICT_HOURS old and has no
verdict yet is compared with `change_log.compare()` over CHANGE_VERDICT_HOURS
each side. Per RF link the comparison already says better / worse / same; the
verdict here sums those up ("2 links better, 1 worse: to X SNR -6 dB") and
logs one `change_verdict` event (warning when more links got worse than
better, else info), which notifications and the weekly report pick up.

An automatic entry (from a `frequency_change` event) and a manual or
planner entry for the same radio within DUPLICATE_MINUTES are one change:
only the first gets a verdict, the other points at it.
Entries older than CHANGE_VERDICT_MAX_AGE_DAYS are never backfilled, and only
changes that became due within NOTIFY_GRACE_HOURS raise an event (a first run
doesn't send a week of verdicts at once).

SQLite `change_verdicts` (one row per change_log id).
"""

import json
import logging
from datetime import datetime, timedelta

import config
import database
import change_log

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
CATEGORIES = ('channel', 'power', 'antenna')
DUPLICATE_MINUTES = 120
NOTIFY_GRACE_HOURS = 3

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS change_verdicts (
                change_id INTEGER PRIMARY KEY,
                node_name TEXT,
                change_time DATETIME,
                verdict TEXT,
                better INTEGER,
                worse INTEGER,
                same INTEGER,
                text TEXT,
                detail TEXT,
                duplicate_of INTEGER,
                created_at DATETIME NOT NULL
            )
        ''')
    _tables_ready = True


def _metric_note(link):
    """Biggest moves on one link: 'SNR -6 dB, LQ -20'."""
    notes = []
    for d in link['directions']:
        for m in d['metrics']:
            if m['verdict'] in ('better', 'worse') and m['delta'] is not None and m['key'] in change_log.KEY_METRICS:
                notes.append((abs(m['delta']), f"{m['label']} {m['delta']:+g} ({d['source']}→{d['target']})"))
    notes.sort(reverse=True)
    return ', '.join(n for _, n in notes[:2])


def summarize(result, change):
    """(verdict, text, detail) from a change_log.compare() result."""
    links = result['links']
    s = result['summary']
    what = f"{change['category']} change on {change['node_name']}"
    if change.get('description'):
        what += f" ({change['description'][:120]})"
    if not links:
        return 'n/a', f"{what}: no RF link data on either side of the change.", []
    worse = [l for l in links if l['verdict'] == 'worse']
    better = [l for l in links if l['verdict'] == 'better']
    if not (worse or better or s['same']):
        return 'n/a', f"{what}: no comparable RF data before and after the change.", []
    verdict = ('worse' if len(worse) > len(better) else 'better' if len(better) > len(worse)
               else 'mixed' if worse else 'same')
    parts = [f"{s['better']} link(s) better, {s['worse']} worse, {s['same']} same"
             + (f", {s['n/a']} without data" if s['n/a'] else '')]
    for l in worse[:3]:
        parts.append(f"worse to {l['peer']}" + (f": {_metric_note(l)}" if _metric_note(l) else ''))
    for l in better[:2]:
        parts.append(f"better to {l['peer']}" + (f": {_metric_note(l)}" if _metric_note(l) else ''))
    reach = result.get('reachability', {}).get(change['node_name']) or {}
    detail = [{'peer': l['peer'], 'verdict': l['verdict'], 'note': _metric_note(l)} for l in links]
    return verdict, f"{result['window_hours']} h after the {what}: " + '; '.join(parts) + '.', detail


def _candidates(conn, now):
    hours = config.CHANGE_VERDICT_HOURS
    newest = (now - timedelta(hours=hours)).strftime(TS_FMT)
    oldest = (now - timedelta(days=config.CHANGE_VERDICT_MAX_AGE_DAYS)).strftime(TS_FMT)
    return [dict(r) for r in conn.execute(f'''
        SELECT c.* FROM change_log c LEFT JOIN change_verdicts v ON v.change_id = c.id
        WHERE v.change_id IS NULL AND c.timestamp <= ? AND c.timestamp >= ?
          AND c.category IN ({','.join('?' * len(CATEGORIES))})
        ORDER BY c.timestamp, c.id
    ''', (newest, oldest, *CATEGORIES))]


def run(now=None, notify_events=True):
    """Give every due change a verdict. Returns the verdict rows written."""
    ensure_tables()
    now = now or datetime.now()
    written = []
    with database.get_connection() as conn:
        due = _candidates(conn, now)
        done = [dict(r) for r in conn.execute(
            'SELECT change_id, node_name, change_time FROM change_verdicts WHERE duplicate_of IS NULL')]
    for change in due:
        at = datetime.strptime(change['timestamp'][:19], TS_FMT)
        dup = next((d for d in done if d['node_name'] == change['node_name'] and d['change_time']
                    and abs((datetime.strptime(d['change_time'][:19], TS_FMT) - at).total_seconds())
                    <= DUPLICATE_MINUTES * 60), None)
        row = {'change_id': change['id'], 'node_name': change['node_name'], 'change_time': change['timestamp'],
               'created_at': now.strftime(TS_FMT), 'duplicate_of': None}
        if dup:
            row.update(verdict='duplicate', better=0, worse=0, same=0, duplicate_of=dup['change_id'],
                       text=f"Same change as #{dup['change_id']}; see its verdict.", detail='[]')
        else:
            try:
                result = change_log.compare(change['node_name'], change['timestamp'],
                                            config.CHANGE_VERDICT_HOURS, change.get('peer_node'))
            except Exception as exc:
                logger.warning('change verdict %s failed: %s', change['id'], exc)
                continue
            verdict, text, detail = summarize(result, change)
            row.update(verdict=verdict, better=result['summary']['better'], worse=result['summary']['worse'],
                       same=result['summary']['same'], text=text, detail=json.dumps(detail))
            done.append({'change_id': change['id'], 'node_name': change['node_name'],
                         'change_time': change['timestamp']})
        with database.get_connection() as conn:
            conn.execute('''INSERT OR REPLACE INTO change_verdicts (change_id, node_name, change_time, verdict,
                                better, worse, same, text, detail, duplicate_of, created_at)
                            VALUES (:change_id, :node_name, :change_time, :verdict, :better, :worse, :same,
                                    :text, :detail, :duplicate_of, :created_at)''', row)
        written.append(row)
        # Only changes that just became due notify; older ones (first run,
        # or after downtime) get their verdict quietly.
        fresh = at >= now - timedelta(hours=config.CHANGE_VERDICT_HOURS + NOTIFY_GRACE_HOURS)
        if notify_events and fresh and row['verdict'] not in ('duplicate', 'n/a'):
            database.log_event('change_verdict', change['node_name'], row['text'],
                               'warning' if row['verdict'] == 'worse' else 'info')
    return written


def list_verdicts(days=14, change_ids=None):
    ensure_tables()
    with database.get_connection() as conn:
        if change_ids:
            ids = [int(i) for i in change_ids][:500]
            rows = conn.execute(f'SELECT * FROM change_verdicts WHERE change_id IN ({",".join("?" * len(ids))})', ids)
        else:
            since = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
            rows = conn.execute('SELECT * FROM change_verdicts WHERE change_time >= ? ORDER BY change_time DESC',
                                (since,))
        out = []
        for r in rows:
            d = dict(r)
            d['detail'] = json.loads(d.get('detail') or '[]')
            out.append(d)
    return out


def install(scheduler):
    if not config.CHANGE_VERDICT_ENABLED:
        return
    scheduler.add_job(lambda: run(), 'interval', minutes=60, id='change_verdicts', replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=3))

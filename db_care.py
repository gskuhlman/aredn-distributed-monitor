"""
Database care (Admin page): what the SQLite file holds, how fast it grows,
the retention settings that bound it, VACUUM, and backups.

  report()   file / WAL size, free pages, per table rows and an estimated size
             (average stored bytes of a sample of rows x rows; this SQLite has
             no dbstat), rows added in the last day and per day over 7 days
             for tables with a timestamp, and the retention settings;
  vacuum()   VACUUM (rewrites the file to drop free pages); refused while a
             scan is running, since it locks the database for its duration;
  backup()   a consistent copy through the SQLite backup API into BACKUP_DIR
             as aredn_monitor-YYYYmmdd-HHMMSS.db, checked with
             PRAGMA quick_check, keeping the newest BACKUP_KEEP copies (only
             files matching that name are ever deleted). Nightly at
             BACKUP_TIME when BACKUP_ENABLED.
"""

import glob
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

import config
import database

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
BACKUP_RE = re.compile(r'^aredn_monitor-(\d{8}-\d{6})\.db$')
SAMPLE_ROWS = 2000
TIME_COLUMNS = ('timestamp', 'created_at', 'last_seen', 'observed_at', 'time')

_lock = threading.Lock()
_state = {'running': None, 'last': None}


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def backup_dir():
    d = config.BACKUP_DIR
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(os.path.abspath(database.get_db_path())), d)
    return d


def _table_info(conn, table, now):
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    est = None
    if rows and cols:
        expr = ' + '.join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 1)' for c in cols)
        avg = conn.execute(f'SELECT AVG({expr}) FROM (SELECT * FROM "{table}" ORDER BY ROWID DESC LIMIT ?)',
                           (SAMPLE_ROWS,)).fetchone()[0] or 0
        est = int(rows * (avg + 8 + 2 * len(cols)))   # + rowid and record header, roughly
    tcol = next((c for c in TIME_COLUMNS if c in cols), None)
    day = week = None
    if tcol and rows:
        day = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{tcol}" >= ?',
                           ((now - timedelta(days=1)).strftime(TS_FMT),)).fetchone()[0]
        week = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{tcol}" >= ?',
                            ((now - timedelta(days=7)).strftime(TS_FMT),)).fetchone()[0]
        oldest = conn.execute(f'SELECT MIN("{tcol}") FROM "{table}"').fetchone()[0]
    else:
        oldest = None
    return {'table': table, 'rows': rows, 'est_bytes': est, 'time_column': tcol,
            'rows_last_day': day, 'rows_per_day_7d': round(week / 7) if week is not None else None,
            'oldest': oldest}


def report():
    path = database.get_db_path()
    now = datetime.now()
    with database.get_connection() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        info = [_table_info(conn, t, now) for t in tables]
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        free = conn.execute('PRAGMA freelist_count').fetchone()[0]
        journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
        settings = {r[0]: r[1] for r in conn.execute('SELECT key, value FROM settings')}
    info.sort(key=lambda t: -(t['est_bytes'] or 0))
    total_est = sum(t['est_bytes'] or 0 for t in info) or 1
    for t in info:
        t['est_pct'] = round(100.0 * (t['est_bytes'] or 0) / total_est, 1)
        t['bytes_per_day'] = (int((t['est_bytes'] or 0) / t['rows'] * t['rows_per_day_7d'])
                              if t['rows'] and t['rows_per_day_7d'] else None)
    retention = [
        {'key': 'HISTORY_RETENTION_HOURS', 'value': config.HISTORY_RETENTION_HOURS, 'unit': 'hours',
         'applies': 'link_history, weather observations'},
        {'key': 'RAW_TRACKER_RETENTION_HOURS', 'value': config.RAW_TRACKER_RETENTION_HOURS, 'unit': 'hours',
         'applies': 'raw LQM tracker JSON in link_history'},
        {'key': 'NODE_HEALTH_RETENTION_HOURS', 'value': config.NODE_HEALTH_RETENTION_HOURS, 'unit': 'hours',
         'applies': 'node_health_history, collector_health'},
        {'key': 'LINK_STATE_LOG_RETENTION_DAYS', 'value': config.LINK_STATE_LOG_RETENTION_DAYS, 'unit': 'days',
         'applies': 'link_state_log'},
        {'key': 'CONNECTIVITY_LOG_RETENTION_DAYS', 'value': config.CONNECTIVITY_LOG_RETENTION_DAYS, 'unit': 'days',
         'applies': 'events log file, notifications_log'},
        {'key': 'ROUTE_HISTORY_RETENTION_DAYS', 'value': config.ROUTE_HISTORY_RETENTION_DAYS, 'unit': 'days',
         'applies': 'route_history'},
        {'key': 'database_retention_days (setting)', 'value': settings.get('database_retention_days'),
         'unit': 'days', 'applies': 'inactive node records'},
    ]
    return {'generated_at': now.strftime(TS_FMT), 'path': path, 'file_bytes': _size(path),
            'wal_bytes': _size(path + '-wal'), 'page_size': page_size, 'pages': pages, 'free_pages': free,
            'free_bytes': free * page_size, 'journal_mode': journal, 'tables': info, 'retention': retention,
            'backups': list_backups(), 'backup': {'enabled': config.BACKUP_ENABLED, 'dir': backup_dir(),
                                                  'keep': config.BACKUP_KEEP, 'time': config.BACKUP_TIME},
            'task': dict(_state)}


def list_backups():
    d = backup_dir()
    out = []
    for p in sorted(glob.glob(os.path.join(d, 'aredn_monitor-*.db')), reverse=True):
        m = BACKUP_RE.match(os.path.basename(p))
        if m:
            out.append({'file': os.path.basename(p), 'bytes': _size(p),
                        'taken_at': datetime.strptime(m.group(1), '%Y%m%d-%H%M%S').strftime(TS_FMT)})
    return out


def _run(kind, fn):
    if not _lock.acquire(blocking=False):
        raise RuntimeError(f"{_state['running'] or 'another task'} is already running")
    _state['running'] = kind
    started = datetime.now()
    try:
        result = fn()
        _state['last'] = {'task': kind, 'ok': True, 'at': started.strftime(TS_FMT),
                          'seconds': round((datetime.now() - started).total_seconds(), 1), **(result or {})}
        return _state['last']
    except Exception as exc:
        _state['last'] = {'task': kind, 'ok': False, 'at': started.strftime(TS_FMT), 'error': str(exc)}
        raise
    finally:
        _state['running'] = None
        _lock.release()


def backup(trigger='manual'):
    def work():
        d = backup_dir()
        os.makedirs(d, exist_ok=True)
        name = f"aredn_monitor-{datetime.now():%Y%m%d-%H%M%S}.db"
        dest_path = os.path.join(d, name)
        src = sqlite3.connect(database.get_db_path(), timeout=30)
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst, pages=2048, sleep=0.05)   # in steps, so scans keep writing
            check = dst.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            dst.close()
            src.close()
        if check != 'ok':
            raise RuntimeError(f'backup {name} failed quick_check: {check}')
        removed = []
        for b in list_backups()[max(1, config.BACKUP_KEEP):]:
            try:
                os.remove(os.path.join(d, b['file']))
                removed.append(b['file'])
            except OSError as exc:
                logger.warning('could not remove old backup %s: %s', b['file'], exc)
        database.log_event('database_backup', None, f'Database backup {name} ({trigger}); '
                           f'{len(removed)} old copy(ies) removed', 'info')
        return {'file': name, 'bytes': _size(dest_path), 'removed': removed}
    return _run('backup', work)


def vacuum(scan_running=False):
    if scan_running:
        raise RuntimeError('a scan is running; try again when it finishes')

    def work():
        before = _size(database.get_db_path())
        conn = sqlite3.connect(database.get_db_path(), timeout=60, isolation_level=None)
        try:
            conn.execute('VACUUM')
            conn.execute('PRAGMA optimize')
        finally:
            conn.close()
        after = _size(database.get_db_path())
        database.log_event('database_vacuum', None, f'VACUUM: {before} -> {after} bytes', 'info')
        return {'before_bytes': before, 'after_bytes': after}
    return _run('vacuum', work)


def install(scheduler):
    if not config.BACKUP_ENABLED:
        return
    try:
        hh, mm = [int(x) for x in str(config.BACKUP_TIME).split(':')[:2]]
    except ValueError:
        hh, mm = 3, 30

    def job():
        try:
            backup('nightly')
        except Exception as exc:
            logger.error('nightly database backup failed: %s', exc)
            database.log_event('database_backup_failed', None, f'Nightly backup failed: {exc}', 'warning')
    scheduler.add_job(job, 'cron', hour=hh, minute=mm, id='db_backup', replace_existing=True)

"""
Test fixtures: every test gets its own small SQLite database (database.init_db
on a temp file) and helpers to add nodes and links. Nothing touches the live
aredn_monitor.db, the network or CouchDB.

Run: python -m pytest
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault('COUCH_URL', '')
os.environ.setdefault('ELEVATION_ENABLED', '0')
os.environ.setdefault('WEATHER_ENABLED', '0')

import config  # noqa: E402
import database  # noqa: E402

TS_FMT = '%Y-%m-%d %H:%M:%S'
# Modules that create their tables once per process: reset per test database.
TABLE_FLAGS = ('notes', 'rollout', 'change_verdict', 'service_check', 'welcome', 'wifi_survey', 'auth',
               'route_watch', 'weekly_report', 'capacity', 'link_watch', 'collector_health', 'link_budget',
               'maintenance', 'notify', 'weather')


def ts(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime(TS_FMT)


class MeshBuilder:
    def __init__(self, path):
        self.path = path

    def node(self, name, **f):
        row = {'name': name, 'is_active': 1, 'last_seen': ts(), 'first_seen': ts(60 * 24 * 30)}
        row.update(f)
        cols = ', '.join(row)
        with database.get_connection() as conn:
            conn.execute(f'INSERT OR REPLACE INTO nodes ({cols}) VALUES ({", ".join("?" * len(row))})',
                         list(row.values()))
        return name

    def link(self, a, b, link_type='RF', both=True, **f):
        for src, tgt in ((a, b), (b, a)) if both else ((a, b),):
            row = {'source_node': src, 'target_node': tgt, 'link_type': link_type, 'status': 'good',
                   'quality': 100, 'snr': 30, 'first_seen': ts(60 * 24), 'last_seen': ts()}
            row.update(f)
            cols = ', '.join(row)
            with database.get_connection() as conn:
                conn.execute(f'INSERT OR REPLACE INTO links ({cols}) VALUES ({", ".join("?" * len(row))})',
                             list(row.values()))

    def infra(self, name, site):
        with database.get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO infrastructure_nodes (node_name, site) VALUES (?, ?)', (name, site))

    def sql(self, query, params=()):
        with database.get_connection() as conn:
            return conn.execute(query, params).fetchall()


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    path = str(tmp_path / 'test.db')
    monkeypatch.setattr(config, 'DATABASE_PATH', path)
    import importlib
    for mod in TABLE_FLAGS:
        try:
            m = importlib.import_module(mod)
        except Exception:
            continue
        for attr in ('_tables_ready', '_schema_ready'):
            if hasattr(m, attr):
                monkeypatch.setattr(m, attr, False)
    database.init_db()
    return MeshBuilder(path)

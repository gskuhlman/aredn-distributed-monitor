"""
Small CouchDB client for append-only monitoring observations.
"""

import logging

import requests

import config


logger = logging.getLogger(__name__)


class CouchDBClient:
    def __init__(self, base_url=None, database=None, timeout=10):
        self.base_url = (base_url or config.COUCH_URL).rstrip('/')
        self.database = database or config.COUCH_DB
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def db_url(self):
        return f"{self.base_url}/{self.database}"

    def is_configured(self):
        return bool(self.base_url)

    def health(self):
        response = self.session.get(self.base_url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def ensure_database(self, database=None):
        db_name = database or self.database
        response = self.session.put(f"{self.base_url}/{db_name}", timeout=self.timeout)
        if response.status_code in (201, 202, 412):
            return True
        response.raise_for_status()
        return True

    def ensure_monitor_indexes(self):
        indexes = [
            ('idx_type_observed', ['type', 'observed_at']),
            ('idx_node_observed', ['type', 'target_node', 'observed_at']),
            ('idx_collector_observed', ['type', 'collector_id', 'observed_at']),
            ('idx_link_observed', ['type', 'link_key', 'observed_at']),
            ('idx_heartbeat_collector', ['type', 'collector_id', 'observed_at']),
        ]
        for name, fields in indexes:
            payload = {
                'index': {'fields': fields},
                'name': name,
                'type': 'json'
            }
            response = self.session.post(
                f"{self.db_url}/_index",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()

    def bootstrap(self):
        self.health()
        self.ensure_database(self.database)
        if config.LOCAL_CONFIG_DB:
            self.ensure_database(config.LOCAL_CONFIG_DB)
        self.ensure_database('_replicator')
        self.ensure_monitor_indexes()

    def bulk_docs(self, docs):
        if not docs:
            return {'ok': 0, 'conflict': 0, 'errors': []}

        response = self.session.post(
            f"{self.db_url}/_bulk_docs",
            json={'docs': docs},
            timeout=self.timeout
        )
        response.raise_for_status()

        summary = {'ok': 0, 'conflict': 0, 'errors': []}
        for row in response.json():
            if row.get('ok'):
                summary['ok'] += 1
            elif row.get('error') == 'conflict':
                summary['conflict'] += 1
            else:
                summary['errors'].append(row)

        if summary['errors']:
            logger.warning("CouchDB bulk write had errors: %s", summary['errors'])
        if summary['conflict']:
            logger.info("CouchDB bulk write skipped %s duplicate docs", summary['conflict'])
        return summary

    def find(self, selector, fields=None, sort=None, limit=100):
        payload = {
            'selector': selector,
            'limit': limit
        }
        if fields:
            payload['fields'] = fields
        if sort:
            payload['sort'] = sort

        response = self.session.post(
            f"{self.db_url}/_find",
            json=payload,
            timeout=self.timeout
        )
        response.raise_for_status()
        return response.json().get('docs', [])


def configured_client():
    if not config.COUCH_URL:
        return None
    return CouchDBClient()


def bootstrap():
    client = configured_client()
    if not client:
        raise RuntimeError('COUCH_URL is not configured')
    client.bootstrap()
    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bootstrap()
    print('CouchDB bootstrap complete')

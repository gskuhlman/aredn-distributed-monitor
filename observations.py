"""
Append-only observation document helpers for distributed AREDN monitoring.

These helpers intentionally do not write to storage. They translate the current
scanner/sysinfo data into deterministic CouchDB documents that can be retried
without creating duplicates.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urlparse


SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_z(value):
    if isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def compact_ts(value):
    if isinstance(value, str):
        normalized = value.replace('-', '').replace(':', '')
        return normalized.replace('T', 'T').replace('Z', 'Z')
    return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def slugify(value):
    text = str(value or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    return text or 'unknown'


def target_from_url(url):
    parsed = urlparse(url if '://' in url else f'http://{url}')
    host = parsed.hostname or url
    return host.split('.')[0].lower()


def poll_cycle_id(collector_id, observed_at):
    return f"{collector_id}:{compact_ts(observed_at)}"


def node_observation_id(collector_id, observed_at, target_node):
    return f"obs:node:{slugify(collector_id)}:{compact_ts(observed_at)}:{slugify(target_node)}"


def link_observation_id(collector_id, observed_at, source_node, neighbor_node):
    return (
        f"obs:link:{slugify(collector_id)}:{compact_ts(observed_at)}:"
        f"{slugify(source_node)}:{slugify(neighbor_node)}"
    )


def service_observation_id(collector_id, observed_at, target_node, service_name):
    return (
        f"obs:service:{slugify(collector_id)}:{compact_ts(observed_at)}:"
        f"{slugify(target_node)}:{slugify(service_name)}"
    )


def heartbeat_id(collector_id, observed_at):
    return f"heartbeat:{slugify(collector_id)}:{compact_ts(observed_at)}"


def _node_ip(data):
    for iface in data.get('interfaces', []):
        if iface.get('name') == 'br-lan' and iface.get('ip'):
            return iface['ip']
    return None


def build_node_observation(data, collector_id, collector_site, observed_at,
                           response_ms=None, errors=None):
    node_name = (data.get('node') or 'unknown').lower()
    node_details = data.get('node_details', {})
    trackers = data.get('lqm', {}).get('info', {}).get('trackers', {})
    if isinstance(trackers, list):
        link_count = len(trackers)
    elif isinstance(trackers, dict):
        link_count = len(trackers)
    else:
        link_count = 0

    return {
        '_id': node_observation_id(collector_id, observed_at, node_name),
        'type': 'node_observation',
        'schema_version': SCHEMA_VERSION,
        'collector_id': collector_id,
        'collector_site': collector_site,
        'observed_at': iso_z(observed_at),
        'poll_cycle_id': poll_cycle_id(collector_id, observed_at),
        'target_node': node_name,
        'target_ip': _node_ip(data),
        'reachable': True,
        'api_ok': True,
        'response_ms': response_ms,
        'services_seen': len(data.get('services_local', []) or []),
        'hosts_seen': len(data.get('hosts', []) or []),
        'links_seen': link_count,
        'firmware': node_details.get('firmware_version'),
        'lat': data.get('lat') or None,
        'lon': data.get('lon') or None,
        'raw_summary': {
            'source': 'aredn_sysinfo',
            'flags': ['hosts', 'services', 'services_local', 'lqm']
        },
        'errors': errors or []
    }


def build_failed_node_observation(target, collector_id, collector_site, observed_at,
                                  message, kind='request_failed'):
    target_node = slugify(target)
    return {
        '_id': node_observation_id(collector_id, observed_at, target_node),
        'type': 'node_observation',
        'schema_version': SCHEMA_VERSION,
        'collector_id': collector_id,
        'collector_site': collector_site,
        'observed_at': iso_z(observed_at),
        'poll_cycle_id': poll_cycle_id(collector_id, observed_at),
        'target_node': target_node,
        'target_ip': None,
        'reachable': False,
        'api_ok': False,
        'response_ms': None,
        'services_seen': 0,
        'hosts_seen': 0,
        'links_seen': 0,
        'errors': [{'kind': kind, 'message': message}]
    }


def iter_link_observations(data, source_node, collector_id, collector_site, observed_at):
    trackers = data.get('lqm', {}).get('info', {}).get('trackers', {})
    if isinstance(trackers, list):
        trackers = {str(index): tracker for index, tracker in enumerate(trackers)}
    if not isinstance(trackers, dict):
        return

    for tracker in trackers.values():
        neighbor = (tracker.get('hostname') or '').lower()
        if not neighbor:
            continue

        quality = tracker.get('quality')
        try:
            quality_value = float(quality)
            lq = quality_value / 100 if quality_value > 1 else quality_value
        except (TypeError, ValueError):
            lq = None

        pair = sorted([slugify(source_node), slugify(neighbor)])
        yield {
            '_id': link_observation_id(collector_id, observed_at, source_node, neighbor),
            'type': 'link_observation',
            'schema_version': SCHEMA_VERSION,
            'collector_id': collector_id,
            'collector_site': collector_site,
            'observed_at': iso_z(observed_at),
            'poll_cycle_id': poll_cycle_id(collector_id, observed_at),
            'source_node': source_node,
            'neighbor_node': neighbor,
            'link_key': f"{pair[0]}--{pair[1]}",
            'link_type': tracker.get('type'),
            'lq': lq,
            'nlq': None,
            'signal': tracker.get('signal'),
            'noise': tracker.get('noise'),
            'snr': tracker.get('snr'),
            'tx_rate': tracker.get('tx_rate'),
            'rx_rate': tracker.get('rx_rate'),
            'raw': {}
        }


def iter_service_observations(data, target_node, collector_id, collector_site, observed_at):
    for service in data.get('services_local', []) or []:
        name = service.get('name') or 'unknown'
        yield {
            '_id': service_observation_id(collector_id, observed_at, target_node, name),
            'type': 'service_observation',
            'schema_version': SCHEMA_VERSION,
            'collector_id': collector_id,
            'collector_site': collector_site,
            'observed_at': iso_z(observed_at),
            'poll_cycle_id': poll_cycle_id(collector_id, observed_at),
            'target_node': target_node,
            'service_name': name,
            'service_url': service.get('link'),
            'service_category': service.get('protocol'),
            'seen': True,
            'raw': {}
        }


def build_heartbeat(collector_id, collector_site, observed_at, collector_version,
                    node_count_attempted, node_count_reachable, duration_ms,
                    errors=None):
    failed = max(0, node_count_attempted - node_count_reachable)
    return {
        '_id': heartbeat_id(collector_id, observed_at),
        'type': 'collector_heartbeat',
        'schema_version': SCHEMA_VERSION,
        'collector_id': collector_id,
        'collector_site': collector_site,
        'observed_at': iso_z(observed_at),
        'poll_cycle_id': poll_cycle_id(collector_id, observed_at),
        'collector_version': collector_version,
        'node_count_attempted': node_count_attempted,
        'node_count_reachable': node_count_reachable,
        'node_count_failed': failed,
        'duration_ms': duration_ms,
        'errors': errors or []
    }

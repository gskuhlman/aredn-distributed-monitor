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


def tracker_mac_address(mac_key, tracker):
    for key in ('mac', 'macaddr', 'mac_address', 'neighbor_mac'):
        value = tracker.get(key)
        if value:
            return str(value).lower()
    return str(mac_key).lower() if mac_key and ':' in str(mac_key) else None


def tracker_routability_status(tracker):
    if tracker.get('routable') is True:
        return 'routable'
    if tracker.get('routable') is False:
        return 'not_routable'
    return 'unknown'


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tracker_rev_snr(tracker):
    """Neighbor's view of our signal, if the firmware reports it.

    Asymmetric links (we hear them well, they hear us poorly) are a primary
    flapping cause, so this is worth promoting out of the raw tracker.
    """
    for key in ('rev_snr', 'revsnr', 'neighbor_snr', 'remote_snr'):
        value = tracker.get(key)
        if value is not None:
            return value
    return None


def tracker_block_info(tracker):
    """Extract AREDN LQM block state and reason from a tracker entry.

    LQM actively blocks/unblocks marginal links; the reason (signal, distance,
    dtd, dup, quality, user, ...) is the single most useful flap diagnostic.
    Firmware shapes vary, so we read defensively: a ``blocks`` dict of
    reason -> bool, and/or a top-level ``blocked`` flag.
    """
    reasons = []
    blocks = tracker.get('blocks')
    if isinstance(blocks, dict):
        reasons = sorted(str(k) for k, v in blocks.items() if v)
    elif isinstance(blocks, list):
        reasons = sorted(str(item) for item in blocks)

    blocked = tracker.get('blocked')
    if blocked is None:
        blocked = bool(reasons) if blocks is not None else None
    else:
        blocked = bool(blocked)

    # Some firmware exposes a single reason string instead of a dict.
    if not reasons:
        single = tracker.get('blocked_reason') or tracker.get('block_reason')
        if single:
            reasons = [str(single)]

    return {
        'blocked': blocked,
        'blocked_reason': ','.join(reasons) if reasons else None,
        'pending': tracker.get('pending'),
        'lastseen': tracker.get('lastseen'),
    }


def node_health_fields(data):
    """Pull node-health signals from sysinfo.json (present even in bare sysinfo).

    Reboots, CPU load spikes, and memory pressure all surface here and explain
    "degraded"/flapping behavior that link metrics alone cannot.
    """
    sysinfo = data.get('sysinfo', {}) or {}
    loads = sysinfo.get('loads') or []
    load1 = load5 = load15 = None
    if isinstance(loads, (list, tuple)):
        if len(loads) > 0:
            load1 = _to_float(loads[0])
        if len(loads) > 1:
            load5 = _to_float(loads[1])
        if len(loads) > 2:
            load15 = _to_float(loads[2])

    uptime_raw = sysinfo.get('uptime')
    uptime_seconds = _to_float(uptime_raw)
    if uptime_seconds is None and isinstance(uptime_raw, str):
        uptime_seconds = _parse_uptime_seconds(uptime_raw)

    memory = data.get('memory', {}) or sysinfo.get('memory', {}) or {}
    mem_free = _to_int(memory.get('freeram') or memory.get('free') or memory.get('memfree'))
    mem_total = _to_int(memory.get('totalram') or memory.get('total') or memory.get('memtotal'))

    meshrf = data.get('meshrf', {}) or {}
    channel_busy = _to_float(
        meshrf.get('channel_busy')
        or meshrf.get('channelbusy')
        or meshrf.get('busy')
    )

    return {
        'uptime': uptime_raw if isinstance(uptime_raw, str) else None,
        'uptime_seconds': int(uptime_seconds) if uptime_seconds is not None else None,
        'load1': load1,
        'load5': load5,
        'load15': load15,
        'mem_free': mem_free,
        'mem_total': mem_total,
        'channel_busy': channel_busy,
    }


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_uptime_seconds(text):
    """Parse AREDN's human uptime string ('1 days, 2:03:04' / '02:03:04')."""
    if not text:
        return None
    total = 0
    days_match = re.search(r'(\d+)\s*day', text)
    if days_match:
        total += int(days_match.group(1)) * 86400
    hms = re.search(r'(\d+):(\d+):(\d+)', text)
    if hms:
        total += int(hms.group(1)) * 3600 + int(hms.group(2)) * 60 + int(hms.group(3))
    return total or None


def build_node_observation(data, collector_id, collector_site, observed_at,
                           response_ms=None, errors=None):
    node_name = (data.get('node') or 'unknown').lower()
    node_details = data.get('node_details', {})
    health = node_health_fields(data)
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
        'uptime': health['uptime'],
        'uptime_seconds': health['uptime_seconds'],
        'load1': health['load1'],
        'load5': health['load5'],
        'load15': health['load15'],
        'mem_free': health['mem_free'],
        'mem_total': health['mem_total'],
        'channel_busy': health['channel_busy'],
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


def iter_link_observations(data, source_node, collector_id, collector_site, observed_at,
                           include_raw=False):
    trackers = data.get('lqm', {}).get('info', {}).get('trackers', {})
    if isinstance(trackers, list):
        trackers = {str(index): tracker for index, tracker in enumerate(trackers)}
    if not isinstance(trackers, dict):
        return

    for mac_key, tracker in trackers.items():
        mac_address = tracker_mac_address(mac_key, tracker)
        hostname = (tracker.get('hostname') or '').lower()
        neighbor = hostname or mac_address or 'unknown'
        identity_status = 'lqm_only' if hostname else ('mac_only' if mac_address else 'unknown')

        quality = tracker.get('quality')
        try:
            quality_value = float(quality)
            lq = quality_value / 100 if quality_value > 1 else quality_value
        except (TypeError, ValueError):
            lq = None

        pair = sorted([slugify(source_node), slugify(neighbor)])
        block_info = tracker_block_info(tracker)
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
            'neighbor_mac': mac_address,
            'canonical_ip': tracker.get('canonical_ip'),
            'identity_status': identity_status,
            'routability_status': tracker_routability_status(tracker),
            'link_key': f"{pair[0]}--{pair[1]}",
            'link_type': tracker.get('type'),
            'lq': lq,
            'nlq': None,
            'signal': tracker.get('signal'),
            'noise': tracker.get('noise'),
            'snr': tracker.get('snr'),
            'rev_snr': tracker_rev_snr(tracker),
            'tx_rate': tracker.get('tx_rate'),
            'rx_rate': tracker.get('rx_rate'),
            'blocked': block_info['blocked'],
            'blocked_reason': block_info['blocked_reason'],
            'pending': block_info['pending'],
            'lastseen': block_info['lastseen'],
            'raw': dict(tracker) if include_raw else {}
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

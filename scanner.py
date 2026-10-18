"""
Network Scanner for AREDN Network Monitor
Handles node discovery and polling
"""

import json
import requests
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import monotonic
from urllib.parse import urlparse
import config
import database
import couch_client
import gateway
import observations

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Nodes whose last poll needed the bare-sysinfo fallback, used to emit
# degraded/recovered events only on transitions instead of every scan.
_degraded_nodes = set()

# Whether the last scan lost the starting node, so gateway lost/restored events
# are logged only on transitions instead of every aborted scan.
_gateway_lost = False


def build_sysinfo_url(ip_or_hostname, full=True):
    """Build the sysinfo.json URL for a node.

    The full URL asks the node to collect LQM, hosts, and service data, which
    can take far longer than the bare sysinfo.json on a struggling node.
    """
    # Remove any existing path/scheme
    host = ip_or_hostname.replace('http://', '').replace('https://', '').split('/')[0]
    if not full:
        return f"http://{host}/cgi-bin/sysinfo.json"
    return f"http://{host}/cgi-bin/sysinfo.json?lqm=1&hosts=1&services=1&services_local=1"


# Why the last fetch of each URL failed (see fetch_fail_reason); read by the
# scan commit to record the cause with the failed health sample.
_fetch_failures = {}


def fetch_fail_reason(exc):
    """Short failure cause for a requests exception (unreachable classifier)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return 'connect_timeout'
    if isinstance(exc, requests.exceptions.Timeout):
        return 'timeout'
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(exc.response, 'status_code', None)
        return f'http_{code}' if code else 'http_error'
    text = str(exc).lower()
    if any(s in text for s in ('name resolution', 'getaddrinfo', 'name or service not known',
                               'nodename nor servname', 'failed to resolve')):
        return 'dns'
    if 'refused' in text or '10061' in text:
        return 'refused'
    if any(s in text for s in ('unreachable', 'no route', '10065', '10051')):
        return 'no_route'
    if 'timed out' in text or '10060' in text:
        return 'connect_timeout'
    return 'connection_error'


def fetch_node_info(url):
    """Fetch sysinfo.json from a node"""
    try:
        response = requests.get(url, timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        _fetch_failures.pop(url, None)
        return data
    except requests.exceptions.Timeout as e:
        logger.warning(f"Timeout fetching {url}")
        _fetch_failures[url] = fetch_fail_reason(e)
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error fetching {url}: {e}")
        _fetch_failures[url] = fetch_fail_reason(e)
        return None
    except ValueError as e:
        logger.warning(f"Invalid JSON from {url}: {e}")
        _fetch_failures[url] = 'bad_json'
        return None


def fetch_node_with_fallback(url):
    """Fetch full sysinfo, falling back to bare sysinfo when the full request
    fails/times out. Returns (data, degraded, response_ms). Pure I/O — safe to
    run concurrently across nodes; no DB access here.
    """
    started = monotonic()
    data = fetch_node_info(url)
    degraded = False
    if not data:
        # The full sysinfo (lqm+hosts+services) can hang on a struggling node; a
        # bare sysinfo answering means the node is up but degraded, not down.
        data = fetch_node_info(build_sysinfo_url(url, full=False))
        degraded = data is not None
        bare_reason = _fetch_failures.pop(build_sysinfo_url(url, full=False), None)
        if not degraded and bare_reason and url not in _fetch_failures:
            _fetch_failures[url] = bare_reason
        if degraded:
            logger.warning(f"Full sysinfo failed, bare sysinfo succeeded (degraded): {url}")
    return data, degraded, int((monotonic() - started) * 1000)


def is_supernode(data):
    """Check if a node is a supernode"""
    if not data:
        return False

    node_details = data.get('node_details', {})

    # Check for supernode flag in node_details
    if node_details.get('supernode'):
        return True

    # Check for 'supernode' in node name or description
    node_name = data.get('node', '').lower()
    description = node_details.get('description', '').lower()

    if 'supernode' in node_name or 'supernode' in description:
        return True

    return False


def process_node_data(data, refresh_services=True):
    """Process and save node data from sysinfo.json response.

    refresh_services=False preserves stored services when processing a bare
    sysinfo response that carries no services_local data.
    """
    if not data:
        return None, []

    node_name = data.get('node', '').lower()
    if not node_name:
        return None, []

    events = []

    # Extract node details
    node_details = data.get('node_details', {})
    description = node_details.get('description', '')
    model = node_details.get('model', '')
    firmware = node_details.get('firmware_version', '')

    # Check if supernode
    supernode = is_supernode(data)

    lat = None
    lon = None
    try:
        lat = float(data.get('lat', 0)) or None
        lon = float(data.get('lon', 0)) or None
    except (ValueError, TypeError):
        pass

    # Extract RF information
    meshrf = data.get('meshrf', {})
    rf_frequency = meshrf.get('freq', '')
    rf_channel = meshrf.get('channel', '')

    # Find the main IP from interfaces
    ip = None
    for iface in data.get('interfaces', []):
        if iface.get('name') == 'br-lan' and iface.get('ip'):
            ip = iface['ip']
            break

    # Check if node exists and track changes
    existing_node = database.get_node(node_name)

    if existing_node is None:
        if should_announce_new_node(node_name):
            events.append({
                'type': database.EVENT_NODE_DISCOVERED,
                'node': node_name,
                'details': f"Model: {model}, IP: {ip}",
                'severity': 'info'
            })
            logger.info(f"New node discovered: {node_name}")
        else:
            logger.info(f"Node rediscovered within new-node window: {node_name}")
    else:
        # Check if node was previously inactive (came back online)
        if not existing_node.get('is_active'):
            events.append({
                'type': database.EVENT_NODE_ONLINE,
                'node': node_name,
                'details': f"Node back online",
                'severity': 'info'
            })
            logger.info(f"Node back online: {node_name}")

        # Check for frequency change (only log if change is >= 1 MHz)
        old_freq = existing_node.get('rf_frequency', '')
        if old_freq and rf_frequency and old_freq != rf_frequency:
            try:
                old_freq_val = float(old_freq)
                new_freq_val = float(rf_frequency)
                freq_diff = abs(new_freq_val - old_freq_val)
                if freq_diff >= 1.0:  # Only log changes >= 1 MHz
                    events.append({
                        'type': database.EVENT_FREQUENCY_CHANGE,
                        'node': node_name,
                        'details': f"Frequency changed: {old_freq} MHz -> {rf_frequency} MHz",
                        'severity': 'warning'
                    })
                    logger.info(f"Frequency change on {node_name}: {old_freq} -> {rf_frequency}")
            except (ValueError, TypeError):
                pass  # Ignore if frequencies aren't valid numbers

    # Upsert the node
    database.upsert_node(
        name=node_name,
        ip=ip,
        description=description,
        model=model,
        firmware_version=firmware,
        lat=lat,
        lon=lon,
        rf_frequency=rf_frequency,
        rf_channel=rf_channel,
        is_supernode=supernode
    )

    # Radio settings (mode, SSID, width, antenna) for audits and diagnostics.
    labels = {'rf_mode': 'mode', 'rf_ssid': 'SSID', 'rf_chanbw': 'channel width'}
    changes = database.set_node_radio_config(node_name, database.radio_config_from_sysinfo(data))
    for col, (old, new) in changes.items():
        events.append({
            'type': database.EVENT_RADIO_CONFIG_CHANGE,
            'node': node_name,
            'details': f"Radio {labels[col]} changed: {old} -> {new}",
            'severity': 'warning'
        })

    # Process services_local (services provided by this node)
    if refresh_services:
        database.clear_node_services(node_name)
        for service in data.get('services_local', []):
            database.upsert_service(
                node_name=node_name,
                name=service.get('name', ''),
                protocol=service.get('protocol', 'tcp'),
                link=service.get('link', ''),
                ip=ip
            )

    return node_name, events


def tracker_is_connected(tracker):
    """Return False when LQM tracker data explicitly reports a disconnected link."""
    disconnected_values = {
        'bad', 'dead', 'disconnected', 'down', 'dropped', 'failed',
        'false', 'inactive', 'lost', 'no', 'offline', 'unreachable'
    }
    connected_values = {
        'active', 'connected', 'good', 'ok', 'online', 'reachable',
        'true', 'up', 'yes'
    }

    for key in ('connected', 'active', 'up', 'online', 'reachable', 'link_up'):
        if key in tracker:
            value = tracker.get(key)
            if isinstance(value, bool):
                return value
            if value is not None and str(value).strip().lower() in disconnected_values:
                return False

    for key in ('status', 'state', 'link_status', 'connection', 'link_state'):
        value = tracker.get(key)
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in disconnected_values:
            return False
        if normalized in connected_values:
            return True

    return True


def tracker_mac_address(mac_key, tracker):
    """Return the best MAC address value from a tracker entry."""
    for key in ('mac', 'macaddr', 'mac_address', 'neighbor_mac'):
        value = tracker.get(key)
        if value:
            return str(value).lower()
    return str(mac_key).lower() if mac_key and ':' in str(mac_key) else None


def tracker_routability_status(tracker):
    """Classify whether LQM data says this neighbor is routable."""
    if tracker.get('routable') is True:
        return 'routable'
    if tracker.get('routable') is False:
        return 'not_routable'
    return 'unknown'


def tracker_identity_status(hostname, mac_address=None):
    """Classify identity quality for a neighbor observed through LQM."""
    if hostname:
        return 'named'
    return 'mac_only' if mac_address else 'lqm_only'


def tracker_lqm_status_message(hostname, canonical_ip, routability_status):
    """Human-facing diagnostic summary for an LQM neighbor."""
    if not hostname:
        return 'MAC-only neighbor'
    if canonical_ip and ':' in str(canonical_ip):
        return 'IPv6 link-local only'
    if routability_status == 'not_routable':
        return 'Seen by LQM, not currently routable'
    if not canonical_ip:
        return 'Awaiting host/IP mapping'
    if routability_status == 'unknown':
        return 'LQM-only neighbor'
    return None


def lqm_trackers(data):
    """Return the node's LQM trackers as a dict, or None if malformed.

    Some firmware reports trackers as a list; it is keyed by index then.
    """
    trackers = data.get('lqm', {}).get('info', {}).get('trackers', {})
    if isinstance(trackers, list):
        return {str(i): t for i, t in enumerate(trackers)}
    if not isinstance(trackers, dict):
        logger.warning(f"Unexpected trackers type: {type(trackers)}")
        return None
    return trackers


def tracker_hostnames(mac, tracker):
    """Return (tracker_hostname, hostname, mac_address) for a tracker.

    hostname falls back to a mac-derived name when the tracker has none.
    """
    mac_address = tracker_mac_address(mac, tracker)
    tracker_hostname = tracker.get('hostname', '').lower()
    hostname = tracker_hostname
    if not hostname and mac_address:
        hostname = f"mac-{mac_address.replace(':', '').replace('-', '')}"
    return tracker_hostname, hostname, mac_address


def is_tunnel_link_type(link_type):
    return link_type.upper() in ('WIREGUARD', 'TUN', 'TUNNEL', 'VTUN', 'WG')


def get_show_tunnels():
    """Whether tunnel links are saved (from settings or config)."""
    return database.get_setting('show_tunnels', 'false').lower() == 'true' or config.SHOW_TUNNELS


def discover_neighbors(data, show_tunnels):
    """Return the routable neighbors to queue for discovery from one node's
    sysinfo. Pure (no DB writes), so a scan can walk the mesh before deciding
    whether its results are trustworthy enough to commit.
    """
    trackers = lqm_trackers(data) if data else None
    if not trackers:
        return []

    discovered_nodes = []
    for mac, tracker in trackers.items():
        _, hostname, _ = tracker_hostnames(mac, tracker)
        if not hostname:
            continue
        # A saved link type reported disconnected is not traversed.
        if (not is_tunnel_link_type(tracker.get('type', '')) or show_tunnels)                 and not tracker_is_connected(tracker):
            continue
        # ALWAYS add routable nodes to discovery queue (regardless of link type)
        # This ensures we discover all nodes even if connected only via tunnels.
        # Fall back to the tracker 'ip' field when canonical_ip is null —
        # Babel firmware leaves canonical_ip empty for OLSR DTD neighbors but
        # populates 'ip' from its own discovery.
        discover_ip = tracker.get('canonical_ip') or tracker.get('ip')
        if tracker.get('routable') and discover_ip:
            discovered_nodes.append({
                'hostname': hostname,
                'ip': discover_ip,
                'url': build_sysinfo_url(discover_ip),
                'link_type': tracker.get('type')
            })
    return discovered_nodes


def process_links(data, source_node):
    """Process LQM tracker data to extract links and discover connected nodes"""
    if not data or not source_node:
        return [], []

    trackers = lqm_trackers(data)
    events = []
    if trackers is None:
        return [], []

    show_tunnels = get_show_tunnels()
    current_targets = set()

    for mac, tracker in trackers.items():
        link_type = tracker.get('type', '')
        canonical_ip = tracker.get('canonical_ip')
        tracker_hostname, hostname, mac_address = tracker_hostnames(mac, tracker)

        if not hostname:
            continue

        identity_status = tracker_identity_status(tracker_hostname, mac_address)
        routability_status = tracker_routability_status(tracker)
        lqm_status_message = tracker_lqm_status_message(tracker_hostname, canonical_ip, routability_status)
        quality = tracker.get('quality', 0)
        snr = tracker.get('snr')
        distance = tracker.get('distance')

        # Ensure quality is an integer
        try:
            quality = int(quality)
        except (ValueError, TypeError):
            quality = 0

        # Always save RF and DTD links, optionally save tunnel links
        if not is_tunnel_link_type(link_type) or show_tunnels:
            if not tracker_is_connected(tracker):
                existing_link = database.get_link(source_node, hostname)
                if existing_link and existing_link.get('status') not in ('dropped', 'removed'):
                    changed = database.mark_link_dropped(source_node, hostname)
                    if changed:
                        events.append({
                            'type': database.EVENT_LINK_DROPPED,
                            'node': source_node,
                            'details': f"{link_type} link to {hostname} dropped (reported disconnected)",
                            'severity': 'warning'
                        })
                        database.log_link_state(
                            source_node, hostname, 'down', link_type=link_type,
                            quality=quality, snr=snr,
                            rev_snr=observations.tracker_rev_snr(tracker),
                            detail='reported disconnected', origin='node_reported'
                        )
                        logger.info(f"Link reported disconnected: {source_node} -> {hostname}")
                continue

            current_targets.add(hostname)

            # Check if this is a new link or restored link
            existing_link = database.get_link(source_node, hostname)

            rev_snr = observations.tracker_rev_snr(tracker)
            block_info = observations.tracker_block_info(tracker)
            raw_tracker = json.dumps(tracker, default=str) if config.STORE_RAW_TRACKER else None

            if existing_link is None:
                # New link
                events.append({
                    'type': database.EVENT_LINK_NEW,
                    'node': source_node,
                    'details': f"New {link_type} link to {hostname} (Q:{quality}%)",
                    'severity': 'info'
                })
                database.log_link_state(
                    source_node, hostname, 'up', link_type=link_type,
                    quality=quality, snr=snr, rev_snr=rev_snr,
                    blocked_reason=block_info['blocked_reason'], detail='new link'
                )
                logger.info(f"New link: {source_node} <-> {hostname} ({link_type})")
            elif existing_link.get('status') in ('dropped', 'removed'):
                # Link restored
                events.append({
                    'type': database.EVENT_LINK_RESTORED,
                    'node': source_node,
                    'details': f"{link_type} link to {hostname} restored (Q:{quality}%)",
                    'severity': 'info'
                })
                database.log_link_state(
                    source_node, hostname, 'up', link_type=link_type,
                    quality=quality, snr=snr, rev_snr=rev_snr,
                    blocked_reason=block_info['blocked_reason'], detail='restored'
                )
                logger.info(f"Link restored: {source_node} <-> {hostname}")

            # LQM block-state transitions are a primary flap cause: emit an event
            # and log the transition only when the state actually changes.
            new_blocked = block_info['blocked']
            if new_blocked is not None and existing_link is not None:
                prev_blocked = existing_link.get('blocked')
                prev_blocked = None if prev_blocked is None else bool(prev_blocked)
                if prev_blocked != new_blocked:
                    if new_blocked:
                        events.append({
                            'type': database.EVENT_LINK_BLOCKED,
                            'node': source_node,
                            'details': f"LQM blocked link to {hostname} "
                                       f"({block_info['blocked_reason'] or 'unspecified'})",
                            'severity': 'warning'
                        })
                        database.log_link_state(
                            source_node, hostname, 'blocked', link_type=link_type,
                            quality=quality, snr=snr, rev_snr=rev_snr,
                            blocked_reason=block_info['blocked_reason']
                        )
                        logger.info(f"LQM blocked: {source_node} -> {hostname} ({block_info['blocked_reason']})")
                    else:
                        events.append({
                            'type': database.EVENT_LINK_UNBLOCKED,
                            'node': source_node,
                            'details': f"LQM unblocked link to {hostname}",
                            'severity': 'info'
                        })
                        database.log_link_state(
                            source_node, hostname, 'unblocked', link_type=link_type,
                            quality=quality, snr=snr, rev_snr=rev_snr
                        )

            database.upsert_link(
                source_node=source_node,
                target_node=hostname,
                link_type=link_type,
                quality=quality,
                snr=snr,
                distance=distance,
                mac_address=mac_address,
                canonical_ip=canonical_ip,
                identity_status=identity_status,
                routability_status=routability_status,
                lqm_status_message=lqm_status_message,
                signal=tracker.get('signal'),
                noise=tracker.get('noise'),
                tx_rate=tracker.get('tx_rate'),
                rx_rate=tracker.get('rx_rate'),
                rev_snr=rev_snr,
                blocked=new_blocked,
                blocked_reason=block_info['blocked_reason'],
                lqm_pending=str(block_info['pending']) if block_info['pending'] is not None else None,
                raw_tracker=raw_tracker
            )

    missing_links = database.get_missing_source_links(source_node, current_targets)
    dropped_count = database.mark_missing_source_links_dropped(source_node, current_targets)
    for link in missing_links:
        events.append({
            'type': database.EVENT_LINK_DROPPED,
            'node': link['source'],
            'details': f"{link['type']} link to {link['target']} dropped",
            'severity': 'warning'
        })
        database.log_link_state(
            link['source'], link['target'], 'down',
            link_type=link['type'], detail='absent from scan', origin='node_reported'
        )

    if dropped_count > 0:
        logger.info(f"Marked {dropped_count} missing links from {source_node} as dropped")

    return discover_neighbors(data, show_tunnels), events


def normalize_start_url(url):
    """Ensure the URL has the proper sysinfo.json path"""
    if not url:
        return config.STARTING_NODE

    # If it doesn't contain the sysinfo.json path, add it
    if '/cgi-bin/sysinfo.json' not in url:
        return build_sysinfo_url(url)

    return url


def url_host(url):
    """Lowercased host of a node URL (scheme optional)."""
    if '://' not in url:
        url = f"http://{url}"
    return (urlparse(url).hostname or '').lower()


def gateway_reachable(gateway_url):
    """Probe the starting node with a bare sysinfo request (cheap, and answers
    even when the node is too loaded for the full LQM/hosts/services one)."""
    return gateway.probe(url_host(gateway_url))


def discover_network(start_url=None, max_depth=None, record_depth=True, gateway_url=None):
    """
    Discover the network starting from a node.
    Uses BFS traversal to find all connected nodes.
    Returns a dictionary with scan results.

    The scan runs in two phases: every node is fetched first (no DB writes),
    then the results are committed. The configured starting node is the
    collector's gateway into the mesh; if it is unreachable at the start, or
    stops answering at any point during the scan, the scan is aborted and every
    result is discarded. Failures in that case say nothing about the mesh
    nodes themselves -- only that this collector is off the network.

    Args:
        start_url: Starting node URL
        max_depth: Maximum hops from starting node (default from settings/config)
        gateway_url: Node whose reachability gates the scan (default: start_url)
    """
    if start_url is None:
        # Check for override in settings
        start_url = get_starting_node_url()
    if gateway_url is None:
        gateway_url = start_url

    if max_depth is None:
        # Get from settings or config
        max_depth = int(database.get_setting('max_depth', config.MAX_DEPTH))

    # Normalize the URL to ensure it has the proper path
    start_url = normalize_start_url(start_url)
    gateway_url = normalize_start_url(gateway_url)
    gateway_host = url_host(gateway_url)

    logger.info(f"Starting network discovery from {start_url} (max depth: {max_depth})")

    visited_urls = set()
    visited_nodes = set()
    # Queue contains tuples of (url, depth, target_hint). target_hint is the
    # neighbor-reported LQM hostname for a discovered node, so a FAILED poll can
    # be recorded under the real node name instead of the URL/IP-derived slug.
    queue = [(start_url, 0, None)]
    nodes_found = 0
    links_found = 0
    errors = []
    max_depth_reached = 0
    all_events = []
    starting_node_error = None  # Set when the scan is aborted (gateway lost)
    observed_at = observations.utc_now()
    observation_docs = []
    attempted_nodes = 0
    reachable_nodes = 0
    # Operator-defined scan boundaries keep a local-event scan from wandering
    # into remote meshes. 'exclude' nodes are never polled (so anything only
    # reachable through them is never reached); 'stop' nodes are polled but not
    # traversed past. The seed is always polled and traversed.
    boundaries = database.get_scan_boundaries()
    boundary_skipped = set()
    boundary_stopped = set()
    # Every node name polled or reported as a neighbor this scan. Used to tell
    # "left scan scope" apart from "went offline inside the scanned area".
    seen_names = set()
    show_tunnels = get_show_tunnels()
    # (item, fetch result) pairs in BFS order, committed only after the scan.
    fetched_results = []
    # url -> (reporting neighbor, its LQM link type), recorded with failed polls.
    poll_via = {}

    def process_result(url, depth, target_hint, data, degraded, response_ms):
        """Commit one fetched node (all DB writes happen here, serially, and
        only once the whole scan is known to be trustworthy)."""
        nonlocal attempted_nodes, reachable_nodes, nodes_found, links_found
        nonlocal max_depth_reached
        attempted_nodes += 1
        logger.info(f"Scanned (depth {depth}): {url}")

        if not data:
            error_msg = f"Failed to fetch: {url}"
            errors.append(error_msg)
            # Prefer the neighbor-reported hostname so failures are keyed to the
            # real node name; fall back to the URL slug for the seed node. Note
            # target_from_url yields a useless "10" for bare IP URLs, which is
            # exactly the case the hint fixes.
            failed_target = (target_hint or '').lower() or observations.target_from_url(url)
            observation_docs.append(
                observations.build_failed_node_observation(
                    target=failed_target,
                    collector_id=config.COLLECTOR_ID,
                    collector_site=config.COLLECTOR_SITE,
                    observed_at=observed_at,
                    message=error_msg
                )
            )
            # Record an unreachable health sample so the scanner-to-node downtime
            # is visible in the time series under the real node name.
            if config.NODE_HEALTH_ENABLED:
                reporter, via_type = poll_via.get(url.lower(), (None, None))
                database.update_node_health(
                    failed_target, reachable=False, response_ms=response_ms,
                    fail_reason=_fetch_failures.pop(url, None),
                    reported_by=reporter, via_link_type=via_type
                )
            return

        # Process the node
        node_name, node_events = process_node_data(data, refresh_services=not degraded)
        all_events.extend(node_events)

        if not node_name:
            errors.append(f"Invalid node data from: {url}")
            return

        # Record discovery depth (hops from the seed) on full scans only; a
        # targeted single-node scan starts at depth 0 and must not overwrite it.
        if record_depth:
            database.set_node_scan_depth(node_name, depth)

        # Emit degraded/recovered events only on transitions
        if degraded:
            if node_name not in _degraded_nodes:
                _degraded_nodes.add(node_name)
                all_events.append({
                    'type': database.EVENT_NODE_DEGRADED,
                    'node': node_name,
                    'details': "Full sysinfo timed out but node answers bare sysinfo (up but degraded)",
                    'severity': 'warning'
                })
        elif node_name in _degraded_nodes:
            _degraded_nodes.discard(node_name)
            all_events.append({
                'type': database.EVENT_NODE_DEGRADED,
                'node': node_name,
                'details': "Full sysinfo responding again",
                'severity': 'info'
            })

        reachable_nodes += 1

        # Capture node health (uptime/load/memory) and detect reboots. Works on
        # degraded bare-sysinfo responses too, which still carry the sysinfo
        # block -- a load/memory spike is often what caused the degradation.
        if config.NODE_HEALTH_ENABLED:
            health = observations.node_health_fields(data)
            reboot = database.update_node_health(
                node_name, health, reachable=True, degraded=degraded,
                response_ms=response_ms
            )
            if reboot:
                all_events.append({
                    'type': database.EVENT_NODE_REBOOT,
                    'node': node_name,
                    'details': "Node rebooted (uptime reset to "
                               f"{reboot['uptime_seconds']}s from {reboot['prior_uptime_seconds']}s)",
                    'severity': 'warning'
                })
                logger.info(f"Reboot detected on {node_name}")

        observation_docs.append(
            observations.build_node_observation(
                data=data,
                collector_id=config.COLLECTOR_ID,
                collector_site=config.COLLECTOR_SITE,
                observed_at=observed_at,
                response_ms=response_ms,
                errors=[{
                    'kind': 'degraded',
                    'message': 'full sysinfo failed; bare sysinfo fallback succeeded'
                }] if degraded else None
            )
        )
        observation_docs.extend(
            observations.iter_link_observations(
                data=data,
                source_node=node_name,
                collector_id=config.COLLECTOR_ID,
                collector_site=config.COLLECTOR_SITE,
                observed_at=observed_at,
                include_raw=config.STORE_RAW_TRACKER
            )
        )
        observation_docs.extend(
            observations.iter_service_observations(
                data=data,
                target_node=node_name,
                collector_id=config.COLLECTOR_ID,
                collector_site=config.COLLECTOR_SITE,
                observed_at=observed_at
            )
        )

        if node_name not in visited_nodes:
            visited_nodes.add(node_name)
            nodes_found += 1
            max_depth_reached = max(max_depth_reached, depth)

        # Process links. A degraded (bare sysinfo) response has no LQM data,
        # so it says nothing about links: skip link processing rather than
        # mark every link from this node dropped.
        if not degraded:
            discovered, link_events = process_links(data, node_name)
            all_events.extend(link_events)
            links_found += len(discovered)

    def plan_next(depth, data, degraded):
        """Return the next-level (url, depth, hint) tuples to queue for one
        fetched node. No DB writes, so traversal can run ahead of the commit."""
        node_name = (data or {}).get('node', '').lower()
        if not node_name:
            return []
        seen_names.add(node_name)

        # Check if this is a supernode - if so, don't traverse beyond it
        supernode = is_supernode(data)
        if supernode:
            logger.info(f"Found supernode: {node_name} - not traversing beyond")

        # An operator boundary also stops traversal. An 'exclude' node is
        # normally never queued, but one can still be reached by IP before its
        # name is known; treat that like 'stop' so nothing beyond it is scanned.
        at_boundary = depth > 0 and node_name in boundaries
        if at_boundary:
            boundary_stopped.add(node_name)
            logger.info(f"Scan boundary ({boundaries[node_name]}) at {node_name} - not traversing beyond")

        # A degraded (bare sysinfo) response has no LQM data, so it has no
        # neighbors to traverse.
        discovered = [] if degraded else discover_neighbors(data, show_tunnels)

        # Queue next-level nodes (unless at max depth or past a supernode).
        next_nodes = []
        for node_info in discovered:
            if node_info.get('hostname'):
                seen_names.add(node_info['hostname'])
            poll_via.setdefault(node_info['url'].lower(), (node_name, node_info.get('link_type')))
        if depth < max_depth and not supernode and not at_boundary:
            for node_info in discovered:
                node_url = node_info['url']
                hostname = node_info.get('hostname')
                if hostname and boundaries.get(hostname) == 'exclude':
                    boundary_skipped.add(hostname)
                    continue
                if node_url.lower() not in visited_urls:
                    next_nodes.append((node_url, depth + 1, hostname))
        return next_nodes

    # BFS by depth "waves": fetch every node in the current wave concurrently
    # (I/O-bound), then process the results serially. Serial polling of a large
    # mesh made the scan cycle far longer than LINK_TIMEOUT, which marked healthy
    # nodes unreachable simply because the scan couldn't revisit them in time.
    while queue:
        wave = []
        for item in queue:
            normalized = item[0].lower()
            if normalized in visited_urls:
                continue
            visited_urls.add(normalized)
            wave.append(item)
        queue = []
        if not wave:
            break

        logger.info(f"Scanning wave of {len(wave)} node(s) (concurrency {config.SCAN_CONCURRENCY})")
        workers = max(1, min(config.SCAN_CONCURRENCY, len(wave)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = list(pool.map(
                lambda it: (it, fetch_node_with_fallback(it[0])), wave
            ))

        # Is the gateway still answering? If it was polled in this wave its own
        # result answers that; otherwise probe it. Losing it means every
        # failure in this scan may be ours, not the mesh's: abort.
        gateway_ok = None
        for (url, _, _), (data, _, _) in fetched:
            if url_host(url) == gateway_host:
                gateway_ok = bool(gateway_ok) or data is not None
        if gateway_ok is None:
            gateway_ok = gateway_reachable(gateway_url)
        else:
            gateway.record(gateway_ok)
        if not gateway_ok:
            starting_node_error = (
                f"Starting node {gateway_host} unreachable -- scan aborted, results "
                "discarded (this collector appears to be off the AREDN network)"
            )
            logger.error(starting_node_error)
            break

        for item, fetch_result in fetched:
            fetched_results.append((item, fetch_result))
            queue.extend(plan_next(item[1], fetch_result[0], fetch_result[1]))

    if starting_node_error:
        return {
            'aborted': True,
            'nodes_found': 0,
            'links_found': 0,
            'nodes_visited': 0,
            'max_depth_reached': 0,
            'errors': [starting_node_error],
            'events': [],
            'timestamp': datetime.now().isoformat(),
            'starting_node_error': starting_node_error,
            'observed_at': observations.iso_z(observed_at),
            'observation_docs': [],
            'node_count_attempted': 0,
            'node_count_reachable': 0,
            'boundaries_active': bool(boundaries),
            'boundary_skipped': [],
            'boundary_stopped': [],
            'seen_names': []
        }

    # The gateway stayed reachable throughout, so the results reflect the mesh.
    for (url, depth, target_hint), (data, degraded, response_ms) in fetched_results:
        process_result(url, depth, target_hint, data, degraded, response_ms)

    logger.info(f"Discovery complete: {nodes_found} nodes, {links_found} links, max depth reached: {max_depth_reached}")

    return {
        'aborted': False,
        'nodes_found': nodes_found,
        'links_found': links_found,
        'nodes_visited': len(visited_nodes),
        'max_depth_reached': max_depth_reached,
        'errors': errors,
        'events': all_events,
        'timestamp': datetime.now().isoformat(),
        'starting_node_error': None,
        'observed_at': observations.iso_z(observed_at),
        'observation_docs': observation_docs,
        'node_count_attempted': attempted_nodes,
        'node_count_reachable': reachable_nodes,
        'boundaries_active': bool(boundaries),
        'boundary_skipped': sorted(boundary_skipped),
        'boundary_stopped': sorted(boundary_stopped),
        'seen_names': sorted(seen_names)
    }


def write_observations(result, duration_ms):
    """Write append-only observation docs to CouchDB when configured."""
    client = couch_client.configured_client()
    if not client:
        return None

    observed_at = result.get('observed_at') or observations.iso_z(observations.utc_now())
    docs_by_id = {
        doc['_id']: doc
        for doc in result.get('observation_docs', [])
        if doc.get('_id')
    }
    heartbeat = (
        observations.build_heartbeat(
            collector_id=config.COLLECTOR_ID,
            collector_site=config.COLLECTOR_SITE,
            observed_at=observed_at,
            collector_version=config.COLLECTOR_VERSION,
            node_count_attempted=result.get('node_count_attempted', 0),
            node_count_reachable=result.get('node_count_reachable', 0),
            duration_ms=duration_ms,
            errors=result.get('errors', []),
            scan_aborted=result.get('aborted', False)
        )
    )
    docs_by_id[heartbeat['_id']] = heartbeat
    docs = list(docs_by_id.values())

    try:
        summary = client.bulk_docs(docs)
        logger.info("Wrote CouchDB observations: %s", summary)
        return summary
    except Exception as exc:
        logger.warning("Failed to write CouchDB observations: %s", exc)
        return {'ok': 0, 'conflict': 0, 'errors': [{'error': str(exc)}]}


def get_link_remove_after_seconds():
    """Get dropped-link retention from settings, defaulting to config."""
    raw_value = database.get_setting('link_remove_after_minutes', str(config.LINK_REMOVE_AFTER // 60))
    try:
        minutes = int(raw_value)
    except (TypeError, ValueError):
        minutes = config.LINK_REMOVE_AFTER // 60
    minutes = max(1, min(10080, minutes))
    return minutes * 60


def get_new_node_days():
    """Get the window used to suppress repeat new-node announcements."""
    raw_value = database.get_setting('new_node_days', str(config.NEW_NODE_DAYS))
    try:
        days = int(raw_value)
    except (TypeError, ValueError):
        days = config.NEW_NODE_DAYS
    return max(1, min(3650, days))


def get_database_retention_days():
    """Get how long inactive nodes remain in the local SQLite database."""
    raw_value = database.get_setting('database_retention_days', str(config.DATABASE_RETENTION_DAYS))
    try:
        days = int(raw_value)
    except (TypeError, ValueError):
        days = config.DATABASE_RETENTION_DAYS
    return max(1, min(3650, days))


def should_announce_new_node(node_name):
    """Only announce a node as new if it has no recent retained node events."""
    return not database.has_recent_node_event(node_name, get_new_node_days())


def prune_database_retention():
    """Remove nodes and related local data older than the database retention window."""
    retention_days = get_database_retention_days()
    removed = database.prune_old_nodes(retention_days)
    if removed > 0:
        logger.info(f"Pruned {removed} nodes older than {retention_days} days from the database")
    return removed


def update_link_statuses():
    """Update link statuses based on timeouts, return details of changes"""
    # Get links that will be dropped before marking them
    dropped_links = database.get_links_to_drop(config.LINK_TIMEOUT)
    remove_after_seconds = get_link_remove_after_seconds()
    removed_links = database.get_links_to_remove(remove_after_seconds)
    events = []

    # Log events for dropped links
    # Allow one extra poll interval of grace so a single missed scan does not
    # immediately count as "scanner lost the node".
    reachable_grace = config.LINK_TIMEOUT + config.POLL_INTERVAL
    for link in dropped_links:
        events.append({
            'type': database.EVENT_LINK_DROPPED,
            'node': link['source'],
            'details': f"{link['type']} link to {link['target']} dropped",
            'severity': 'warning'
        })
        # A timeout is only a peer flap if the source node was actually reachable.
        # If the scanner lost its path to the source node, this is a
        # scanner-to-node problem, not the peer link flapping.
        if database.was_reachable_within(link['source'], reachable_grace):
            database.log_link_state(
                link['source'], link['target'], 'down',
                link_type=link['type'], detail='link timeout',
                origin='scanner_inferred'
            )
        else:
            database.log_link_state(
                link['source'], link['target'], 'scanner_unreachable',
                link_type=link['type'], detail='source unreachable (scanner lost node)',
                origin='scanner_inferred'
            )

    dropped = database.mark_stale_links_dropped(config.LINK_TIMEOUT)
    removed = database.remove_old_dropped_links(remove_after_seconds)

    for link in removed_links:
        events.append({
            'type': database.EVENT_LINK_REMOVED,
            'node': link['source'],
            'details': f"{link['type']} link to {link['target']} removed after being dropped",
            'severity': 'info'
        })

    if dropped > 0:
        logger.info(f"Marked {dropped} links as dropped")
    if removed > 0:
        logger.info(f"Removed {removed} old dropped links")

    return {
        'dropped': dropped,
        'removed': removed,
        'dropped_links': dropped_links,
        'removed_links': removed_links,
        'events': events
    }


def update_node_statuses(seen_names=None, boundaries_active=False):
    """Update node statuses based on timeouts, return details of changes.

    When scan boundaries are configured, a node that nobody in this scan polled
    or reported as a neighbor has most likely just fallen outside the scan
    scope. It still goes inactive locally, but is logged as info rather than an
    outage warning so setting a boundary doesn't flood the log with "offline".
    """
    # Get nodes that will be marked inactive before marking them
    inactive_nodes = database.get_nodes_to_mark_inactive(config.LINK_TIMEOUT)
    events = []
    seen = set(seen_names or [])

    # Log events for nodes going offline
    for node in inactive_nodes:
        if boundaries_active and node['name'] not in seen:
            events.append({
                'type': database.EVENT_NODE_OFFLINE,
                'node': node['name'],
                'details': "Node left scan scope (not seen by any scanned node; beyond a scan boundary or gone)",
                'severity': 'info'
            })
            continue
        events.append({
            'type': database.EVENT_NODE_OFFLINE,
            'node': node['name'],
            'details': f"Node offline (IP: {node.get('ip', 'unknown')})",
            'severity': 'warning'
        })

    count = database.mark_stale_nodes_inactive(config.LINK_TIMEOUT)

    if count > 0:
        logger.info(f"Marked {count} nodes as inactive (stale)")

    # Also mark orphan nodes (nodes with no active links) as inactive
    orphan_nodes = database.get_orphan_nodes()
    for node in orphan_nodes:
        events.append({
            'type': database.EVENT_NODE_OFFLINE,
            'node': node['name'],
            'details': f"Node orphaned - no active links (IP: {node.get('ip', 'unknown')})",
            'severity': 'warning'
        })

    orphan_count = database.mark_orphan_nodes_inactive()

    if orphan_count > 0:
        logger.info(f"Marked {orphan_count} orphan nodes as inactive")

    return {
        'marked_inactive': count + orphan_count,
        'inactive_nodes': inactive_nodes + orphan_nodes,
        'events': events
    }


def run_scan():
    """
    Run a complete network scan.
    This is the main entry point for scheduled scans.
    """
    logger.info("Starting scheduled scan...")

    scan_started = monotonic()

    # Discover network
    result = discover_network()

    gateway_events = gateway_transition_events(result)
    if result.get('aborted'):
        # Lost the starting node: nothing in this scan reflects the mesh. Skip
        # the timeout-based link/node aging and retention pruning too, so an
        # outage on our side doesn't age healthy nodes into "offline".
        log_scan_events(gateway_events)
        result['events'] = gateway_events
        result['dropped_links'] = []
        result['inactive_nodes'] = []
        return finish_scan_result(result, scan_started)

    # Collect all events
    all_events = gateway_events + result.get('events', [])

    # Update link statuses
    link_status = update_link_statuses()
    result['dropped'] = link_status['dropped']
    result['removed'] = link_status['removed']
    result['dropped_links'] = link_status['dropped_links']
    all_events.extend(link_status.get('events', []))

    # Update node statuses
    node_status = update_node_statuses(
        seen_names=result.get('seen_names'),
        boundaries_active=result.get('boundaries_active', False)
    )
    result['inactive_nodes'] = node_status['inactive_nodes']
    all_events.extend(node_status.get('events', []))

    # Save all events to database
    log_scan_events(all_events)

    database.clear_old_events(config.CONNECTIVITY_LOG_RETENTION_DAYS)
    database.trim_connectivity_log(config.CONNECTIVITY_LOG_RETENTION_DAYS)
    result['nodes_pruned'] = prune_database_retention()

    result['events'] = all_events
    finish_scan_result(result, scan_started)

    logger.info(f"Scan complete: {result['nodes_found']} nodes, {result['links_found']} links, {len(all_events)} events")
    return result


def gateway_transition_events(result):
    """Events for losing/regaining the starting node, on transitions only."""
    global _gateway_lost
    if result.get('aborted'):
        if _gateway_lost:
            return []
        _gateway_lost = True
        return [{
            'type': database.EVENT_GATEWAY_LOST,
            'node': None,
            'details': f"{result.get('starting_node_error')}. Scans are skipped until "
                       "it answers again; no node or link state is changed meanwhile.",
            'severity': 'warning'
        }]
    if not _gateway_lost:
        return []
    _gateway_lost = False
    return [{
        'type': database.EVENT_GATEWAY_RESTORED,
        'node': None,
        'details': "Starting node reachable again; scanning resumed",
        'severity': 'info'
    }]


def log_scan_events(events):
    for event in events:
        database.log_event(
            event_type=event['type'],
            node_name=event.get('node'),
            details=event.get('details'),
            severity=event.get('severity', 'info')
        )


def finish_scan_result(result, scan_started):
    """Write CouchDB observations (just the heartbeat for an aborted scan) and
    strip internal fields from the result."""
    result['timestamp'] = datetime.now().isoformat()
    result['couchdb_write'] = write_observations(
        result,
        duration_ms=int((monotonic() - scan_started) * 1000)
    )
    result['observation_count'] = len(result.get('observation_docs', []))
    result.pop('observation_docs', None)
    result.pop('seen_names', None)
    return result


def run_targeted_scan(target, max_depth=0):
    """
    Scan one requested node without running global stale-node/link cleanup.

    This is intended for the node detail page where an operator wants to refresh
    one node's sysinfo and immediate LQM/service data without causing unrelated
    nodes to be marked inactive.
    """
    logger.info("Starting targeted scan for %s", target)
    scan_started = monotonic()
    start_url = normalize_start_url(target)
    # Gate on the configured starting node, not the target: if we can't reach
    # the mesh at all, the target failing says nothing about the target.
    result = discover_network(
        start_url=start_url, max_depth=max_depth, record_depth=False,
        gateway_url=get_starting_node_url()
    )

    all_events = gateway_transition_events(result) + result.get('events', [])
    log_scan_events(all_events)

    result['events'] = all_events
    finish_scan_result(result, scan_started)
    logger.info(
        "Targeted scan complete for %s: %s nodes, %s links",
        target,
        result.get('nodes_found'),
        result.get('links_found')
    )
    return result


def get_starting_node_url():
    """Get the current starting node URL"""
    return database.get_setting('starting_node', config.STARTING_NODE)


def set_starting_node_url(url):
    """Set the starting node URL"""
    database.set_setting('starting_node', url)
    logger.info(f"Starting node updated to: {url}")

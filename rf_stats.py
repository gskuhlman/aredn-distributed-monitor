"""
RF Statistics Collection Module for AREDN Network Monitor
Handles ping tests, iperf3 throughput tests, and historical data collection
"""

import subprocess
import re
import json
import logging
import time
import platform
import requests
from urllib.parse import urlencode
from datetime import datetime
from collections import deque
import threading

# Try to import eventlet.tpool for non-blocking subprocess execution
try:
    import eventlet.tpool
    USE_TPOOL = True
except ImportError:
    USE_TPOOL = False

import config
import database

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Iperf test queue and state
iperf_queue = deque()
iperf_running = False
iperf_lock = threading.Lock()


def _run_subprocess(cmd, timeout):
    """Helper to run subprocess - can be executed in tpool for non-blocking behavior."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ping_node(ip_address, count=5, timeout=5):
    """
    Execute ICMP ping to a node.

    Args:
        ip_address: IP address to ping
        count: Number of ping packets (default 5)
        timeout: Timeout in seconds (default 5)

    Returns:
        dict with {min, avg, max, loss} or None on failure
    """
    if not ip_address:
        return None

    try:
        # Determine platform-specific ping command
        system = platform.system().lower()

        if system == 'windows':
            # Windows ping: -n count, -w timeout (in milliseconds)
            cmd = ['ping', '-n', str(count), '-w', str(timeout * 1000), ip_address]
        else:
            # Linux/macOS ping: -c count, -W timeout (in seconds)
            cmd = ['ping', '-c', str(count), '-W', str(timeout), ip_address]

        subprocess_timeout = timeout + 3  # Allow a bit of extra time beyond ping timeout

        # Use eventlet.tpool to run subprocess in a real thread (non-blocking for eventlet)
        if USE_TPOOL:
            result = eventlet.tpool.execute(_run_subprocess, cmd, subprocess_timeout)
        else:
            result = _run_subprocess(cmd, subprocess_timeout)

        output = result.stdout

        # Parse ping output based on platform
        if system == 'windows':
            # Windows output: "Minimum = 1ms, Maximum = 5ms, Average = 2ms"
            # Also: "Packets: Sent = 5, Received = 5, Lost = 0 (0% loss)"
            # Single ping: "Reply from X.X.X.X: bytes=32 time=Xms TTL=XX"

            # Extract loss percentage
            loss_match = re.search(r'\((\d+)%\s*loss\)', output)
            loss = float(loss_match.group(1)) if loss_match else 100.0

            # Extract min/max/avg from statistics (for count > 1)
            stats_match = re.search(
                r'Minimum\s*=\s*(\d+)ms.*Maximum\s*=\s*(\d+)ms.*Average\s*=\s*(\d+)ms',
                output
            )

            if stats_match and loss < 100:
                ping_min = float(stats_match.group(1))
                ping_max = float(stats_match.group(2))
                ping_avg = float(stats_match.group(3))
                # Approximate jitter from range on Windows (no mdev available)
                jitter = round((ping_max - ping_min) / 2, 3) if count > 1 else 0.0
                return {
                    'min': ping_min,
                    'avg': ping_avg,
                    'max': ping_max,
                    'loss': loss,
                    'jitter': jitter
                }

            # For single ping (count=1), parse individual reply line
            reply_match = re.search(r'Reply from.*time[=<](\d+)ms', output)
            if reply_match:
                time_ms = float(reply_match.group(1))
                return {
                    'min': time_ms,
                    'avg': time_ms,
                    'max': time_ms,
                    'loss': 0.0,
                    'jitter': 0.0
                }

            # Check for "Request timed out" for single ping
            if 'Request timed out' in output or 'Destination host unreachable' in output:
                return {
                    'min': None,
                    'avg': None,
                    'max': None,
                    'loss': 100.0,
                    'jitter': None
                }
        else:
            # Linux/macOS output: "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms"
            # Also: "5 packets transmitted, 5 received, 0% packet loss"

            # Extract loss percentage
            loss_match = re.search(r'(\d+)%\s*packet loss', output)
            loss = float(loss_match.group(1)) if loss_match else 100.0

            # Extract min/avg/max/mdev (jitter)
            stats_match = re.search(
                r'rtt min/avg/max/\S+\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)',
                output
            )

            if stats_match and loss < 100:
                return {
                    'min': float(stats_match.group(1)),
                    'avg': float(stats_match.group(2)),
                    'max': float(stats_match.group(3)),
                    'loss': loss,
                    'jitter': float(stats_match.group(4))
                }

        # If we got here, ping failed (100% loss or unparseable output)
        return {
            'min': None,
            'avg': None,
            'max': None,
            'loss': 100.0,
            'jitter': None
        }

    except subprocess.TimeoutExpired:
        logger.warning(f"Ping timeout for {ip_address}")
        return {'min': None, 'avg': None, 'max': None, 'loss': 100.0}
    except Exception as e:
        logger.error(f"Ping error for {ip_address}: {e}")
        return {'min': None, 'avg': None, 'max': None, 'loss': 100.0}


def ping_via_aredn(target, source_node_ip=None):
    """
    Ping a target using an AREDN node's built-in ping capability.

    This allows pinging nodes that aren't in our local database by using
    an AREDN node as a proxy. The target can be a hostname or IP.

    Args:
        target: Target hostname (e.g., 'kf9mt-node4') or IP address
        source_node_ip: IP of node to run ping from (defaults to starting node)

    Returns:
        dict with {min, avg, max, loss} or None on failure
    """
    if not target:
        return None

    try:
        # Get the starting node URL from settings
        starting_node = database.get_setting('starting_node', config.STARTING_NODE)

        # Extract just the hostname/IP from the URL
        import urllib.parse
        parsed = urllib.parse.urlparse(starting_node)
        source_host = parsed.netloc or parsed.path.split('/')[0]

        if source_node_ip:
            source_host = source_node_ip

        # Add .local.mesh suffix if target looks like a hostname without domain
        target_addr = target
        if not '.' in target and not target.replace('.', '').isdigit():
            target_addr = f"{target}.local.mesh"

        # Use AREDN's built-in ping via fping (called from web interface)
        # Format: http://<node>/cgi-bin/ping?server=<target>
        ping_url = f"http://{source_host}/cgi-bin/ping?server={target_addr}"

        logger.info(f"Running ping via AREDN API: {source_host} -> {target_addr}")

        response = requests.get(ping_url, timeout=15)

        if response.status_code != 200:
            logger.warning(f"AREDN ping API returned status {response.status_code}")
            return None

        output = response.text

        # Check for errors
        if 'error' in output.lower() or 'unknown host' in output.lower():
            logger.warning(f"AREDN ping failed: {output[:100]}")
            return None

        # Parse fping output - format varies but typically:
        # "hostname : xmt/rcv/%loss = 5/5/0%, min/avg/max = 1.23/2.34/3.45"
        # Or ping output: "round-trip min/avg/max = X/X/X ms"

        # Try to find loss percentage
        loss_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:loss|packet loss)', output)
        loss = float(loss_match.group(1)) if loss_match else None

        # Try to find min/avg/max - fping style
        stats_match = re.search(r'min/avg/max\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)', output)
        if stats_match:
            ping_min = float(stats_match.group(1))
            ping_max = float(stats_match.group(3))
            return {
                'min': ping_min,
                'avg': float(stats_match.group(2)),
                'max': ping_max,
                'loss': loss if loss is not None else 0.0,
                'jitter': round((ping_max - ping_min) / 2, 3)
            }

        # Try alternate format (Windows/standard ping)
        alt_match = re.search(r'Minimum\s*=\s*(\d+).*Maximum\s*=\s*(\d+).*Average\s*=\s*(\d+)', output)
        if alt_match:
            ping_min = float(alt_match.group(1))
            ping_max = float(alt_match.group(2))
            return {
                'min': ping_min,
                'avg': float(alt_match.group(3)),
                'max': ping_max,
                'loss': loss if loss is not None else 0.0,
                'jitter': round((ping_max - ping_min) / 2, 3)
            }

        # Try to find mdev (Linux rtt output inside AREDN response)
        rtt_match = re.search(
            r'rtt min/avg/max/\S+\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)',
            output
        )
        if rtt_match:
            return {
                'min': float(rtt_match.group(1)),
                'avg': float(rtt_match.group(2)),
                'max': float(rtt_match.group(3)),
                'loss': loss if loss is not None else 0.0,
                'jitter': float(rtt_match.group(4))
            }

        # If we got some response but couldn't parse stats, check if it looks successful
        if 'alive' in output.lower() or 'bytes from' in output.lower():
            return {
                'min': None,
                'avg': None,
                'max': None,
                'loss': 0.0,
                'jitter': None
            }

        logger.warning(f"Could not parse AREDN ping output: {output[:200]}")
        return None

    except requests.Timeout:
        logger.warning(f"AREDN ping API timeout for {target}")
        return None
    except requests.RequestException as e:
        logger.error(f"AREDN ping API request error: {e}")
        return None
    except Exception as e:
        logger.error(f"AREDN ping error for {target}: {e}")
        return None


def _enrich_hops(hops):
    """Label hops that are bare IPs with the AREDN node name when we know it."""
    try:
        ip_map = database.get_ip_name_map()
    except Exception:
        ip_map = {}
    for hop in hops:
        ip = hop.get('ip')
        if ip and (not hop.get('host') or hop['host'] == ip):
            name = ip_map.get(ip)
            if name:
                hop['host'] = name
    return hops


def _parse_traceroute(output):
    """Parse `tracert` (Windows) or `traceroute` (Linux) text into hop dicts."""
    hops = []
    ip_re = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3})')
    for line in output.splitlines():
        m = re.match(r'\s*(\d+)\s+(.*)', line)
        if not m:
            continue
        hop_num = int(m.group(1))
        rest = m.group(2)
        ip_match = ip_re.search(rest)
        ip = ip_match.group(1) if ip_match else None
        times = re.findall(r'<?\s*([\d.]+)\s*ms', rest)
        ms = float(times[0]) if times else None
        timeout = ip is None and ('*' in rest or 'timed out' in rest.lower())
        hops.append({'hop': hop_num, 'host': ip, 'ip': ip, 'ms': ms, 'timeout': timeout})
    return hops


def traceroute_local(target, max_hops=30, timeout=2):
    """Run a traceroute FROM the scanner (this host) to a target IP/hostname.

    This is the default "from the scanner" trace used by the network-page node
    panel, analogous to ping_node. Hops are labeled with AREDN node names where
    known.
    """
    if not target:
        return None
    try:
        system = platform.system().lower()
        if system == 'windows':
            cmd = ['tracert', '-d', '-h', str(max_hops), '-w', str(timeout * 1000), target]
        else:
            cmd = ['traceroute', '-n', '-m', str(max_hops), '-w', str(timeout), '-q', '1', target]
        subprocess_timeout = max_hops * timeout + 15
        if USE_TPOOL:
            result = eventlet.tpool.execute(_run_subprocess, cmd, subprocess_timeout)
        else:
            result = _run_subprocess(cmd, subprocess_timeout)
        hops = _parse_traceroute(result.stdout)
        if not hops:
            return None
        _enrich_hops(hops)
        return {'target': target, 'source': 'scanner', 'hops': hops, 'raw': result.stdout[:2000]}
    except subprocess.TimeoutExpired:
        logger.warning("Local traceroute timeout for %s", target)
        return None
    except FileNotFoundError:
        logger.error("traceroute/tracert not found on this host")
        return None
    except Exception as e:
        logger.error("Local traceroute error for %s: %s", target, e)
        return None


def traceroute_via_aredn(target, source_node_ip=None):
    """Run a traceroute FROM an AREDN node toward a target, via its CGI.

    Mirrors ping_via_aredn: the trace originates at source_node_ip (any reachable
    node), defaulting to the starting node. This lets us see where the route to a
    target dies from an arbitrary vantage point in the mesh, not just the collector.

    Returns {'target', 'source', 'hops': [{hop, host, ip, ms, timeout}], 'raw'}
    or None on failure.
    """
    if not target:
        return None
    try:
        import urllib.parse
        starting_node = database.get_setting('starting_node', config.STARTING_NODE)
        parsed = urllib.parse.urlparse(starting_node)
        source_host = parsed.netloc or parsed.path.split('/')[0]
        if source_node_ip:
            source_host = source_node_ip

        target_addr = target
        if '.' not in target and not target.replace('.', '').isdigit():
            target_addr = f"{target}.local.mesh"

        url = f"http://{source_host}/cgi-bin/traceroute?server={urllib.parse.quote(target_addr)}"
        logger.info("Running traceroute via AREDN API: %s -> %s", source_host, target_addr)
        response = requests.get(url, timeout=45)
        if response.status_code != 200:
            logger.warning("AREDN traceroute API returned status %s", response.status_code)
            return None

        text = re.sub(r'<[^>]+>', '', response.text)
        if 'Provide a server name' in text or '<title>ERROR' in response.text:
            logger.warning("AREDN traceroute error for %s", target)
            return None

        hops = []
        for line in text.splitlines():
            m = re.match(r'\s*(\d+)\s+(.*)', line)
            if not m:
                continue
            hop_num = int(m.group(1))
            rest = m.group(2).strip()
            if rest.startswith('*'):
                hops.append({'hop': hop_num, 'host': None, 'ip': None, 'ms': None, 'timeout': True})
                continue
            ip_match = re.search(r'\(([\d.]+)\)', rest)
            ip = ip_match.group(1) if ip_match else None
            host = rest.split('(')[0].strip() if ip_match else rest.split()[0]
            times = re.findall(r'([\d.]+)\s*ms', rest)
            ms = float(times[0]) if times else None
            hops.append({'hop': hop_num, 'host': host or None, 'ip': ip, 'ms': ms, 'timeout': False})

        if not hops:
            return None
        _enrich_hops(hops)
        return {'target': target_addr, 'source': source_host, 'hops': hops,
                'raw': text.strip()[:2000]}

    except requests.Timeout:
        logger.warning("AREDN traceroute API timeout for %s", target)
        return None
    except requests.RequestException as e:
        logger.error("AREDN traceroute request error: %s", e)
        return None
    except Exception as e:
        logger.error("Traceroute error for %s: %s", target, e)
        return None


def _node_address(node_name, fallback_ip=None):
    """Return a node address suitable for AREDN CGI calls."""
    if node_name:
        return node_name if '.' in node_name else f"{node_name}.local.mesh"
    return fallback_ip


def run_iperf_test(target_ip, duration=5, bandwidth_limit='10M',
                   source_node_ip=None, source_node_name=None, target_node_name=None):
    """
    Run iperf3 test using AREDN node's built-in iperf API.

    Uses the AREDN node's web interface to trigger an iperf test from the
    starting node to the target node. This doesn't require iperf3 installed locally.

    Args:
        target_ip: IP address of target node (must have iperf3 server)
        duration: Test duration in seconds (default 5, not used with AREDN API)
        bandwidth_limit: Bandwidth limit (not used with AREDN API)
        source_node_ip: IP of node to run test from (defaults to starting node)
        source_node_name: AREDN node name to run the client from
        target_node_name: AREDN node name to use as the server

    Returns:
        dict with {tx_mbps, rx_mbps} or None on failure
    """
    if not target_ip:
        return None

    try:
        # Get the starting node URL from settings
        starting_node = database.get_setting('starting_node', config.STARTING_NODE)

        # Extract just the hostname/IP from the URL
        # URL format: http://hostname/cgi-bin/sysinfo.json?...
        import urllib.parse
        parsed = urllib.parse.urlparse(starting_node)
        source_host = parsed.netloc or parsed.path.split('/')[0]

        if source_node_ip:
            source_host = source_node_ip
        elif source_node_name:
            source_host = _node_address(source_node_name)

        server = _node_address(target_node_name, target_ip)

        # Use AREDN's built-in iperf API
        # Format: http://<client_node>/cgi-bin/iperf?server=<server_node>&protocol=tcp
        query = urlencode({'server': server, 'protocol': 'tcp'})
        iperf_url = f"http://{source_host}/cgi-bin/iperf?{query}"

        logger.info(f"Running iperf test via AREDN API: {source_host} -> {server}")

        response = requests.get(iperf_url, timeout=30)

        if response.status_code != 200:
            logger.warning(f"AREDN iperf API returned status {response.status_code}")
            return None

        # Parse the HTML response to extract throughput
        # Response format: HTML with iperf output in <pre> tags
        output = response.text

        # Check for error
        lowered = output.lower()
        if 'server error' in lowered or 'no such server' in lowered or 'unable to connect' in lowered:
            logger.warning("AREDN iperf failed: %s", re.sub(r'<[^>]+>', ' ', output)[:300])
            return None

        # Parse iperf output to extract throughput
        # Look for the summary line: [SUM]   0.00-10.00  sec  XX.X MBytes  XX.X Mbits/sec
        # Or individual: [  5]   0.00-10.00  sec  XX.X MBytes  XX.X Mbits/sec

        # Try to find sender/receiver summary
        tx_mbps = None
        rx_mbps = None

        # Look for lines with Mbits/sec or Gbits/sec
        lines = output.split('\n')
        bitrate_values = []

        for line in lines:
            # Match patterns like "46.5 Gbits/sec", "125 Mbits/sec", or "850 Kbits/sec"
            gbits_match = re.search(r'([\d.]+)\s*Gbits/sec', line)
            mbits_match = re.search(r'([\d.]+)\s*Mbits/sec', line)
            kbits_match = re.search(r'([\d.]+)\s*Kbits/sec', line)

            if gbits_match:
                bitrate_values.append(float(gbits_match.group(1)) * 1000)  # Convert to Mbps
            elif mbits_match:
                bitrate_values.append(float(mbits_match.group(1)))
            elif kbits_match:
                bitrate_values.append(float(kbits_match.group(1)) / 1000)

        if bitrate_values:
            # Take average of all samples (excluding the last summary if present)
            # Typically the last few values are the summary
            avg_mbps = sum(bitrate_values) / len(bitrate_values)
            tx_mbps = round(avg_mbps, 2)
            rx_mbps = round(avg_mbps, 2)  # AREDN API shows one direction

            logger.info(f"Iperf result: {tx_mbps} Mbps")
            return {
                'tx_mbps': tx_mbps,
                'rx_mbps': rx_mbps
            }

        logger.warning(f"Could not parse iperf output from AREDN API")
        return None

    except requests.Timeout:
        logger.warning(f"AREDN iperf API timeout for {target_ip}")
        return None
    except requests.RequestException as e:
        logger.error(f"AREDN iperf API request error: {e}")
        return None
    except Exception as e:
        logger.error(f"iperf error for {target_ip}: {e}")
        return None


def record_rf_link_stats():
    """
    Record current quality/SNR stats for all RF links.
    Called after each network scan.
    """
    rf_links = database.get_rf_links()
    count = 0

    for link in rf_links:
        database.insert_link_history(
            source_node=link['source_node'],
            target_node=link['target_node'],
            link_type=link['link_type'],
            quality=link.get('quality'),
            snr=link.get('snr'),
            rev_snr=link.get('rev_snr'),
            blocked=link.get('blocked'),
            blocked_reason=link.get('blocked_reason'),
            raw_tracker=link.get('raw_tracker'),
            sample_type='scan'
        )
        count += 1

    if count > 0:
        logger.info(f"Recorded quality/SNR for {count} RF links")

    return count


def run_ping_round(socketio=None):
    """
    Run ping tests for all RF links.
    Staggers pings to avoid network flooding.

    Args:
        socketio: Optional SocketIO instance for real-time updates
    """
    rf_links = database.get_rf_links()

    if not rf_links:
        logger.debug("No RF links to ping")
        return

    # Calculate stagger delay (spread pings across ~50 seconds of a 60s interval)
    stagger_delay = min(50.0 / len(rf_links), 5.0) if rf_links else 0

    for link in rf_links:
        # Get target node's IP
        target_node = database.get_node(link['target_node'])
        if not target_node or not target_node.get('ip'):
            continue

        target_ip = target_node['ip']

        # Run ping
        ping_result = ping_node(target_ip, count=config.PING_COUNT, timeout=config.PING_TIMEOUT)

        if ping_result:
            # Record to database
            database.update_link_history_ping(
                source_node=link['source_node'],
                target_node=link['target_node'],
                ping_min=ping_result.get('min'),
                ping_avg=ping_result.get('avg'),
                ping_max=ping_result.get('max'),
                ping_loss=ping_result.get('loss'),
                jitter=ping_result.get('jitter')
            )

            # Emit real-time update
            if socketio:
                socketio.emit('rf_stats_update', {
                    'link': {
                        'source': link['source_node'],
                        'target': link['target_node']
                    },
                    'timestamp': datetime.now().isoformat(),
                    'ping': ping_result
                })

            logger.debug(f"Ping {link['source_node']}->{link['target_node']}: {ping_result.get('avg')}ms")

        # Stagger delay between pings
        if stagger_delay > 0:
            time.sleep(stagger_delay)

    logger.info(f"Completed ping round for {len(rf_links)} RF links")


def queue_iperf_test(source_node, target_node, priority=5):
    """
    Add an iperf test to the queue.

    Args:
        source_node: Source node name
        target_node: Target node name
        priority: Priority (1=highest, 10=lowest)
    """
    with iperf_lock:
        # Check if already queued
        for item in iperf_queue:
            if item['source'] == source_node and item['target'] == target_node:
                return  # Already queued

        iperf_queue.append({
            'source': source_node,
            'target': target_node,
            'priority': priority,
            'queued_at': datetime.now()
        })


def queue_all_rf_links_for_iperf():
    """Queue all RF links for iperf testing"""
    rf_links = database.get_rf_links()

    for link in rf_links:
        # Only queue if link quality is good enough
        if link.get('quality', 0) >= config.QUALITY_THRESHOLD_IPERF:
            queue_iperf_test(link['source_node'], link['target_node'])


def process_iperf_queue(socketio=None):
    """
    Process the iperf test queue.
    Only runs ONE test at a time to avoid network congestion.

    Args:
        socketio: Optional SocketIO instance for real-time updates
    """
    global iperf_running

    with iperf_lock:
        if iperf_running:
            logger.debug("Iperf test already running, skipping")
            return

        if not iperf_queue:
            logger.debug("Iperf queue is empty")
            return

        # Sort by priority and get highest priority item
        sorted_queue = sorted(iperf_queue, key=lambda x: x['priority'])
        test_item = sorted_queue[0]
        iperf_queue.remove(test_item)
        iperf_running = True

    try:
        source_node = test_item['source']
        target_node = test_item['target']

        source = database.get_node(source_node)
        source_ip = source.get('ip') if source else None

        # Get target node's IP
        target = database.get_node(target_node)
        if not target or not target.get('ip'):
            logger.warning(f"Cannot run iperf: no IP for {target_node}")
            return

        target_ip = target['ip']

        # Check link quality before running
        link = database.get_link(source_node, target_node)
        if link and link.get('quality', 0) < config.QUALITY_THRESHOLD_IPERF:
            logger.info(f"Skipping iperf {source_node}->{target_node}: quality too low ({link.get('quality')}%)")
            return

        # Emit test started
        if socketio:
            socketio.emit('iperf_test_status', {
                'link': {'source': source_node, 'target': target_node},
                'status': 'running'
            })

        logger.info(f"Running iperf test: {source_node} -> {target_node} ({target_ip})")

        # Run iperf test
        result = run_iperf_test(
            target_ip,
            duration=config.IPERF_DURATION,
            bandwidth_limit=config.IPERF_BANDWIDTH,
            source_node_ip=source_ip,
            source_node_name=source_node,
            target_node_name=target_node
        )

        if result:
            # Record to database
            database.update_link_history_throughput(
                source_node=source_node,
                target_node=target_node,
                throughput_tx=result.get('tx_mbps'),
                throughput_rx=result.get('rx_mbps')
            )

            # Emit result
            if socketio:
                socketio.emit('iperf_test_status', {
                    'link': {'source': source_node, 'target': target_node},
                    'status': 'complete',
                    'result': result
                })

                socketio.emit('rf_stats_update', {
                    'link': {'source': source_node, 'target': target_node},
                    'timestamp': datetime.now().isoformat(),
                    'throughput': result
                })

            logger.info(f"Iperf complete: {source_node}->{target_node}: TX={result['tx_mbps']}Mbps, RX={result['rx_mbps']}Mbps")
        else:
            # Emit failure
            if socketio:
                socketio.emit('iperf_test_status', {
                    'link': {'source': source_node, 'target': target_node},
                    'status': 'failed'
                })
            logger.warning(f"Iperf failed: {source_node} -> {target_node}")

    finally:
        with iperf_lock:
            iperf_running = False


def cleanup_old_history():
    """Clean up old link history, node health, and link state-log records"""
    hours = config.HISTORY_RETENTION_HOURS
    count = database.cleanup_link_history(hours=hours)
    if count > 0:
        logger.info(f"Cleaned up {count} old link history records")

    health_count = database.cleanup_node_health(config.NODE_HEALTH_RETENTION_HOURS)
    if health_count > 0:
        logger.info(f"Cleaned up {health_count} old node health records")

    state_count = database.cleanup_link_state_log(config.LINK_STATE_LOG_RETENTION_DAYS)
    if state_count > 0:
        logger.info(f"Cleaned up {state_count} old link state-log records")

    return count


# ============ Incident Mode ============
#
# When a watched node's link is dropped, LQM-blocked, or marginal, we sample
# that one link hard and bidirectionally instead of standing down. Pinging from
# each end (via the node's own ping CGI) isolates which direction/hop is bad,
# and high cadence catches sub-scan-interval flapping. Samples are stored with
# sample_type='incident' so reports can separate them from routine scans.

_incident_active = set()
_incident_lock = threading.Lock()


def select_incident_links():
    """Return watched-node RF links currently worth incident probing."""
    candidates = []
    seen = set()
    for link in database.get_all_links():
        if link.get('link_type') != 'RF' or link.get('status') == 'removed':
            continue
        source = link.get('source_node')
        target = link.get('target_node')
        if not (config.is_watched_node(source) or config.is_watched_node(target)):
            continue

        dropped = link.get('status') == 'dropped'
        blocked = bool(link.get('blocked'))
        marginal = (link.get('quality') or 0) <= config.INCIDENT_MARGINAL_QUALITY
        if not (dropped or blocked or marginal):
            continue

        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(link)
    return candidates


def _record_incident_ping(source_node, target_node, ping_result):
    """Persist one incident probe sample (direction = source -> target)."""
    if not ping_result:
        return
    database.insert_link_history(
        source_node=source_node,
        target_node=target_node,
        link_type='RF',
        ping_min=ping_result.get('min'),
        ping_avg=ping_result.get('avg'),
        ping_max=ping_result.get('max'),
        ping_loss=ping_result.get('loss'),
        jitter=ping_result.get('jitter'),
        sample_type='incident'
    )


def run_incident_capture(link, socketio=None):
    """Bidirectionally probe one struggling link for INCIDENT_DURATION seconds."""
    source_node = link['source_node']
    target_node = link['target_node']
    key = (source_node, target_node)

    with _incident_lock:
        if key in _incident_active:
            return
        _incident_active.add(key)

    source = database.get_node(source_node)
    target = database.get_node(target_node)
    source_ip = source.get('ip') if source else None
    target_ip = target.get('ip') if target else None

    logger.info("Incident capture starting: %s <-> %s", source_node, target_node)
    if socketio:
        socketio.emit('incident_started', {'source': source_node, 'target': target_node})

    started = time.monotonic()
    rounds = 0
    try:
        while time.monotonic() - started < config.INCIDENT_DURATION:
            rounds += 1
            # source -> target
            forward = ping_via_aredn(target_node, source_node_ip=source_ip)
            _record_incident_ping(source_node, target_node, forward)
            # target -> source (isolates the reverse direction)
            reverse = ping_via_aredn(source_node, source_node_ip=target_ip)
            _record_incident_ping(target_node, source_node, reverse)

            if socketio:
                socketio.emit('rf_stats_update', {
                    'link': {'source': source_node, 'target': target_node},
                    'timestamp': datetime.now().isoformat(),
                    'sample_type': 'incident',
                    'forward_ping': forward,
                    'reverse_ping': reverse
                })

            time.sleep(config.INCIDENT_PROBE_INTERVAL)
    finally:
        with _incident_lock:
            _incident_active.discard(key)
        logger.info("Incident capture complete: %s <-> %s (%d rounds)",
                    source_node, target_node, rounds)
        if socketio:
            socketio.emit('incident_complete', {
                'source': source_node, 'target': target_node, 'rounds': rounds
            })


def maybe_run_incident_probes(socketio=None):
    """Launch incident captures for any watched links currently in trouble.

    Each capture runs in its own background task; a guard set prevents launching
    a second capture for a link already being probed.
    """
    if not (config.INCIDENT_MODE_ENABLED and config.RF_STATS_ENABLED):
        return []

    launched = []
    for link in select_incident_links():
        key = (link['source_node'], link['target_node'])
        with _incident_lock:
            if key in _incident_active:
                continue
        launched.append(key)
        if socketio is not None:
            socketio.start_background_task(run_incident_capture, link, socketio)
        else:
            run_incident_capture(link, None)
    if launched:
        logger.info("Launched %d incident capture(s)", len(launched))
    return launched


def probe_mesh_reachability(socketio=None):
    """Ask reachable neighbors to ping nodes the scanner can't poll.

    Distinguishes "reachable via mesh" (a neighbor can route to it) from "really
    down" (a neighbor hears it on RF but cannot reach it). Bounded per cycle and
    rate-limited per node via config, and runs in a background task so it never
    blocks the scan.
    """
    if not (config.MESH_PROBE_ENABLED and config.RF_STATS_ENABLED):
        return []

    candidates = database.get_via_mesh_candidates(
        config.MESH_PROBE_COOLDOWN_SECONDS, config.MESH_PROBE_MAX_PER_CYCLE
    )
    results = []
    for cand in candidates:
        node = cand['node']
        neighbor = cand['neighbor']
        target = cand.get('target_ip') or node  # IP routes regardless of DNS
        result = ping_via_aredn(target, source_node_ip=cand.get('neighbor_ip'))
        loss = (result or {}).get('loss')
        reachable = bool(result) and (loss is None or loss < 100)
        response_ms = (result or {}).get('avg')
        database.record_mesh_probe(node, neighbor, reachable, response_ms=response_ms)
        results.append((node, neighbor, reachable))
        logger.info("Mesh probe: %s via %s -> %s",
                    node, neighbor, 'reachable' if reachable else 'UNREACHABLE')
        if socketio:
            socketio.emit('mesh_probe_result', {
                'node': node, 'prober': neighbor, 'reachable': reachable
            })
    if results:
        logger.info("Completed %d mesh reachability probe(s)", len(results))
    return results


def get_rf_stats_summary():
    """Get summary of RF stats collection status"""
    rf_links = database.get_rf_links()

    return {
        'rf_link_count': len(rf_links),
        'iperf_queue_size': len(iperf_queue),
        'iperf_running': iperf_running,
        'enabled': config.RF_STATS_ENABLED
    }

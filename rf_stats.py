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
                return {
                    'min': float(stats_match.group(1)),
                    'avg': float(stats_match.group(3)),
                    'max': float(stats_match.group(2)),
                    'loss': loss
                }

            # For single ping (count=1), parse individual reply line
            # Format: "Reply from X.X.X.X: bytes=32 time=5ms TTL=64"
            # Or with <1ms: "Reply from X.X.X.X: bytes=32 time<1ms TTL=64"
            reply_match = re.search(r'Reply from.*time[=<](\d+)ms', output)
            if reply_match:
                time_ms = float(reply_match.group(1))
                return {
                    'min': time_ms,
                    'avg': time_ms,
                    'max': time_ms,
                    'loss': 0.0
                }

            # Check for "Request timed out" for single ping
            if 'Request timed out' in output or 'Destination host unreachable' in output:
                return {
                    'min': None,
                    'avg': None,
                    'max': None,
                    'loss': 100.0
                }
        else:
            # Linux/macOS output: "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms"
            # Also: "5 packets transmitted, 5 received, 0% packet loss"

            # Extract loss percentage
            loss_match = re.search(r'(\d+)%\s*packet loss', output)
            loss = float(loss_match.group(1)) if loss_match else 100.0

            # Extract min/avg/max
            stats_match = re.search(
                r'rtt min/avg/max/\S+\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)',
                output
            )

            if stats_match and loss < 100:
                return {
                    'min': float(stats_match.group(1)),
                    'avg': float(stats_match.group(2)),
                    'max': float(stats_match.group(3)),
                    'loss': loss
                }

        # If we got here, ping failed (100% loss or unparseable output)
        return {
            'min': None,
            'avg': None,
            'max': None,
            'loss': 100.0
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
            return {
                'min': float(stats_match.group(1)),
                'avg': float(stats_match.group(2)),
                'max': float(stats_match.group(3)),
                'loss': loss if loss is not None else 0.0
            }

        # Try alternate format (Windows/standard ping)
        alt_match = re.search(r'Minimum\s*=\s*(\d+).*Maximum\s*=\s*(\d+).*Average\s*=\s*(\d+)', output)
        if alt_match:
            return {
                'min': float(alt_match.group(1)),
                'avg': float(alt_match.group(3)),
                'max': float(alt_match.group(2)),
                'loss': loss if loss is not None else 0.0
            }

        # If we got some response but couldn't parse stats, check if it looks successful
        if 'alive' in output.lower() or 'bytes from' in output.lower():
            return {
                'min': None,
                'avg': None,
                'max': None,
                'loss': 0.0
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


def run_iperf_test(target_ip, duration=5, bandwidth_limit='10M', source_node_ip=None):
    """
    Run iperf3 test using AREDN node's built-in iperf API.

    Uses the AREDN node's web interface to trigger an iperf test from the
    starting node to the target node. This doesn't require iperf3 installed locally.

    Args:
        target_ip: IP address of target node (must have iperf3 server)
        duration: Test duration in seconds (default 5, not used with AREDN API)
        bandwidth_limit: Bandwidth limit (not used with AREDN API)
        source_node_ip: IP of node to run test from (defaults to starting node)

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

        # Use AREDN's built-in iperf API
        # Format: http://<node>/cgi-bin/iperf?server=<target>&protocol=tcp
        iperf_url = f"http://{source_host}/cgi-bin/iperf?server={target_ip}&protocol=tcp"

        logger.info(f"Running iperf test via AREDN API: {source_host} -> {target_ip}")

        response = requests.get(iperf_url, timeout=30)

        if response.status_code != 200:
            logger.warning(f"AREDN iperf API returned status {response.status_code}")
            return None

        # Parse the HTML response to extract throughput
        # Response format: HTML with iperf output in <pre> tags
        output = response.text

        # Check for error
        if 'SERVER ERROR' in output or 'no such server' in output.lower():
            logger.warning(f"AREDN iperf failed: server not reachable")
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
            # Match patterns like "46.5 Gbits/sec" or "125 Mbits/sec"
            gbits_match = re.search(r'([\d.]+)\s*Gbits/sec', line)
            mbits_match = re.search(r'([\d.]+)\s*Mbits/sec', line)

            if gbits_match:
                bitrate_values.append(float(gbits_match.group(1)) * 1000)  # Convert to Mbps
            elif mbits_match:
                bitrate_values.append(float(mbits_match.group(1)))

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
            snr=link.get('snr')
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
                ping_loss=ping_result.get('loss')
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
            bandwidth_limit=config.IPERF_BANDWIDTH
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
    """Clean up old link history records"""
    hours = config.HISTORY_RETENTION_HOURS
    count = database.cleanup_link_history(hours=hours)
    if count > 0:
        logger.info(f"Cleaned up {count} old link history records")
    return count


def get_rf_stats_summary():
    """Get summary of RF stats collection status"""
    rf_links = database.get_rf_links()

    return {
        'rf_link_count': len(rf_links),
        'iperf_queue_size': len(iperf_queue),
        'iperf_running': iperf_running,
        'enabled': config.RF_STATS_ENABLED
    }

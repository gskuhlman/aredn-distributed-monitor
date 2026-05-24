"""
AREDN Network Monitor - Flask Application
Main entry point for the web application
"""

# Monkey-patch standard library for eventlet compatibility
# MUST be done before any other imports that use threading
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
import logging
import atexit
import os
from datetime import datetime, timedelta

import config
import database
import scanner
import rf_stats
import couch_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'aredn-monitor-secret-key'

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Initialize scheduler with eventlet-compatible executor
# Use a small thread pool since eventlet monkey-patches threading
executors = {
    'default': ThreadPoolExecutor(max_workers=3)
}
job_defaults = {
    'coalesce': True,  # Combine multiple pending executions into one
    'max_instances': 1,  # Only one instance of each job at a time
    'misfire_grace_time': 60  # Allow 60 seconds grace period for misfires
}
scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults)

# Track scan state
scan_state = {
    'is_scanning': False,
    'last_scan': None,
    'last_result': None,
    'last_scan_finished': None  # Timestamp when last scan completed
}

# Minimum gap between scans (seconds)
MIN_SCAN_GAP = 10

# Track active ping sessions per client (sid -> {node_name, session_id})
active_ping_sessions = {}
ping_session_counter = 0


def get_poll_interval_seconds():
    """Get configured scan interval with the same bounds as the settings API."""
    try:
        return max(10, min(600, int(database.get_setting('poll_interval', config.POLL_INTERVAL))))
    except (TypeError, ValueError):
        return config.POLL_INTERVAL


def auto_scan_enabled():
    """Return whether automatic network scanning is enabled."""
    return database.get_setting('auto_scan', 'true') == 'true'


def schedule_next_network_scan(delay_seconds=None):
    """Schedule one automatic network scan after the requested delay."""
    if not auto_scan_enabled():
        job = scheduler.get_job('network_scan')
        if job:
            scheduler.remove_job('network_scan')
        return

    delay = get_poll_interval_seconds() if delay_seconds is None else delay_seconds
    run_at = datetime.now() + timedelta(seconds=delay)
    scheduler.add_job(
        scheduled_scan,
        'date',
        run_date=run_at,
        id='network_scan',
        replace_existing=True
    )
    logger.info(f"Next automatic scan scheduled for {run_at.isoformat(timespec='seconds')}")


def scheduled_scan():
    """Background task to scan the network"""
    global scan_state

    if scan_state['is_scanning']:
        logger.warning("Scan already in progress, skipping")
        return

    # Ensure minimum gap between scans
    if scan_state['last_scan_finished']:
        elapsed = (datetime.now() - scan_state['last_scan_finished']).total_seconds()
        if elapsed < MIN_SCAN_GAP:
            wait_time = MIN_SCAN_GAP - elapsed
            logger.info(f"Waiting {wait_time:.1f}s before starting scan (minimum gap: {MIN_SCAN_GAP}s)")
            socketio.sleep(wait_time)

    scan_state['is_scanning'] = True

    # Notify clients that scan is starting
    socketio.emit('scan_started', {'timestamp': datetime.now().isoformat()})

    try:
        result = scanner.run_scan()
        scan_state['last_scan'] = result['timestamp']
        scan_state['last_result'] = result

        # Check if starting node was unreachable
        if result.get('starting_node_error'):
            socketio.emit('starting_node_error', {
                'error': result['starting_node_error'],
                'timestamp': datetime.now().isoformat()
            })
            logger.error(f"Starting node error: {result['starting_node_error']}")

        # Record RF link stats (quality/SNR) to history
        if config.RF_STATS_ENABLED:
            rf_stats.record_rf_link_stats()

        # Emit notifications for dropped links
        for link in result.get('dropped_links', []):
            socketio.emit('link_dropped', {
                'source': link['source'],
                'target': link['target'],
                'type': link['type']
            })

        # Emit notifications for inactive nodes
        for node in result.get('inactive_nodes', []):
            socketio.emit('node_inactive', {
                'node': node['name'],
                'ip': node.get('ip')
            })

        # Emit all events from this scan for real-time log updates
        for event in result.get('events', []):
            socketio.emit('new_event', event)

        # Get updated network data
        network_data = database.get_network_graph_data()

        # Notify clients with updated data
        socketio.emit('scan_complete', {
            'result': result,
            'network': network_data
        })

    except Exception as e:
        logger.error(f"Scan error: {e}")
        socketio.emit('scan_error', {'error': str(e)})

    finally:
        scan_state['is_scanning'] = False
        scan_state['last_scan_finished'] = datetime.now()
        schedule_next_network_scan()


# ============ Web Routes ============

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/nodes/<name>')
def node_page(name):
    """Full node information page."""
    return render_template('node.html', node_name=name.lower())


# ============ API Routes ============

@app.route('/api/health')
def api_get_health():
    """Get app health and optional CouchDB connectivity."""
    couch_status = {
        'configured': bool(config.COUCH_URL),
        'ok': None,
        'error': None
    }

    client = couch_client.configured_client()
    if client:
        try:
            client.health()
            couch_status['ok'] = True
        except Exception as exc:
            couch_status['ok'] = False
            couch_status['error'] = str(exc)

    return jsonify({
        'ok': True,
        'app': 'aredn-network-monitor',
        'collector_id': config.COLLECTOR_ID,
        'collector_site': config.COLLECTOR_SITE,
        'couchdb': couch_status
    })


@app.route('/api/collectors')
def api_get_collectors():
    """Get known collectors from replicated heartbeat observations."""
    client = couch_client.configured_client()
    if not client:
        return jsonify([{
            'collector_id': config.COLLECTOR_ID,
            'collector_site': config.COLLECTOR_SITE,
            'last_heartbeat': scan_state['last_scan'],
            'source': 'local_runtime'
        }])

    try:
        docs = client.find(
            selector={'type': 'collector_heartbeat'},
            sort=[{'observed_at': 'desc'}],
            limit=500
        )
    except Exception as exc:
        return jsonify({'error': str(exc), 'collectors': []}), 503

    collectors = {}
    for doc in docs:
        collector_id = doc.get('collector_id')
        if not collector_id or collector_id in collectors:
            continue
        collectors[collector_id] = {
            'collector_id': collector_id,
            'collector_site': doc.get('collector_site'),
            'last_heartbeat': doc.get('observed_at'),
            'node_count_attempted': doc.get('node_count_attempted'),
            'node_count_reachable': doc.get('node_count_reachable'),
            'node_count_failed': doc.get('node_count_failed'),
            'source': 'couchdb'
        }

    return jsonify(list(collectors.values()))


@app.route('/api/nodes')
def api_get_nodes():
    """Get all nodes"""
    nodes = database.get_all_nodes()
    return jsonify(nodes)


@app.route('/api/nodes/active')
def api_get_active_nodes():
    """Get active nodes only"""
    nodes = database.get_active_nodes()
    return jsonify(nodes)


@app.route('/api/node/<name>')
def api_get_node(name):
    """Get a specific node with its services"""
    node = database.get_node(name.lower())
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    services = database.get_node_services(name.lower())
    links = database.get_node_links(name.lower())

    return jsonify({
        'node': node,
        'services': services,
        'links': links
    })


@app.route('/api/nodes/all')
def api_get_all_nodes():
    """Get all nodes ever seen, with latest link info"""
    nodes = database.get_all_nodes()
    for node in nodes:
        node['links_count'] = len(database.get_node_links(node['name']))
        node['services_list'] = database.get_node_services(node['name'])
    return jsonify(nodes)


@app.route('/api/nodes/detail/<name>')
def api_get_node_detail(name):
    """Get detailed information about a node"""
    node_name = name.lower()
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    services = database.get_node_services(node_name)
    links = database.get_node_links(node_name)
    connectivity_log = database.get_node_connectivity_log(node_name)

    return jsonify({
        'node': node,
        'services': services,
        'links': links,
        'connectivity_log': connectivity_log
    })


@app.route('/api/nodes/full/<name>')
def api_get_node_full(name):
    """Get all locally available information for one node."""
    node_name = name.lower()
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    hours = request.args.get('hours', 24, type=int)
    event_limit = request.args.get('event_limit', 500, type=int)

    return jsonify({
        'node': node,
        'services': database.get_node_services(node_name),
        'active_links': database.get_node_links(node_name),
        'all_links': database.get_node_all_links(node_name),
        'connectivity_log': database.get_node_connectivity_log(node_name, limit=event_limit),
        'quality_history': database.get_node_history(node_name, hours=hours),
        'ping_history': database.get_node_ping_history(node_name, hours=hours)
    })


@app.route('/api/nodes/scan/<name>', methods=['POST'])
def api_scan_node(name):
    """Run a targeted scan against one known node."""
    node_name = name.lower()
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    if not node.get('ip'):
        return jsonify({'error': f'Node "{node_name}" has no IP address'}), 400

    try:
        result = scanner.run_targeted_scan(node['ip'], max_depth=0)
        network_data = database.get_network_graph_data()
        socketio.emit('scan_complete', {
            'result': result,
            'network': network_data
        })
        return jsonify({'success': True, 'result': result})
    except Exception as exc:
        logger.error("Targeted scan failed for %s: %s", node_name, exc)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/nodes/history/<name>')
def api_get_node_history(name):
    """Get link quality/SNR history for a node"""
    node_name = name.lower()
    hours = request.args.get('hours', 24, type=int)
    history = database.get_node_history(node_name, hours=hours)
    return jsonify(history)


@app.route('/api/nodes/ping-history/<name>')
def api_get_node_ping_history(name):
    """Get ping history for links involving this node"""
    node_name = name.lower()
    hours = request.args.get('hours', 24, type=int)
    history = database.get_node_ping_history(node_name, hours=hours)
    return jsonify(history)


@app.route('/api/nodes/connectivity/<name>')
def api_get_node_connectivity(name):
    """Get connectivity events for a node"""
    node_name = name.lower()
    limit = request.args.get('limit', 200, type=int)
    log = database.get_node_connectivity_log(node_name, limit=limit)
    return jsonify(log)


@app.route('/api/nodes/delete/<name>', methods=['POST'])
def api_delete_node(name):
    """Delete a node and all its related data"""
    node_name = name.lower()
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    count = database.delete_node(node_name)
    return jsonify({'success': True, 'deleted': count})


@app.route('/api/links')
def api_get_links():
    """Get all links"""
    links = database.get_all_links()
    return jsonify(links)


@app.route('/api/links/active')
def api_get_active_links():
    """Get active links only"""
    links = database.get_active_links()
    return jsonify(links)


@app.route('/api/network')
def api_get_network():
    """Get network graph data for vis.js"""
    data = database.get_network_graph_data()
    return jsonify(data)


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get current settings"""
    settings = database.get_all_settings()

    # Add defaults if not set
    if 'starting_node' not in settings:
        settings['starting_node'] = config.STARTING_NODE
    if 'show_tunnels' not in settings:
        settings['show_tunnels'] = 'true' if config.SHOW_TUNNELS else 'false'
    if 'max_depth' not in settings:
        settings['max_depth'] = config.MAX_DEPTH
    if 'auto_scan' not in settings:
        settings['auto_scan'] = 'true'
    if 'poll_interval' not in settings:
        settings['poll_interval'] = config.POLL_INTERVAL
    else:
        settings['poll_interval'] = int(settings['poll_interval'])
    if 'link_remove_after_minutes' not in settings:
        settings['link_remove_after_minutes'] = config.LINK_REMOVE_AFTER // 60
    else:
        settings['link_remove_after_minutes'] = int(settings['link_remove_after_minutes'])

    settings['link_timeout'] = config.LINK_TIMEOUT
    settings['quality_good'] = config.QUALITY_GOOD
    settings['quality_poor'] = config.QUALITY_POOR

    return jsonify(settings)


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    """Update settings"""
    data = request.get_json()

    if 'starting_node' in data:
        scanner.set_starting_node_url(data['starting_node'])

    if 'show_tunnels' in data:
        database.set_setting('show_tunnels', 'true' if data['show_tunnels'] else 'false')
        logger.info(f"Show tunnels updated to: {data['show_tunnels']}")

    if 'max_depth' in data:
        max_depth = max(1, min(20, int(data['max_depth'])))  # Clamp between 1-20
        database.set_setting('max_depth', str(max_depth))
        logger.info(f"Max depth updated to: {max_depth}")

    if 'auto_scan' in data:
        auto_scan = 'true' if data['auto_scan'] else 'false'
        database.set_setting('auto_scan', auto_scan)
        logger.info(f"Auto scan updated to: {auto_scan}")

        # Update scheduler
        if data['auto_scan']:
            if not scan_state['is_scanning']:
                schedule_next_network_scan()
            logger.info(f"Scheduler resumed with completion-based interval")
        else:
            job = scheduler.get_job('network_scan')
            if job:
                scheduler.remove_job('network_scan')
                logger.info("Scheduler paused")

    if 'poll_interval' in data:
        poll_interval = max(10, min(600, int(data['poll_interval'])))  # Clamp between 10-600
        database.set_setting('poll_interval', str(poll_interval))
        logger.info(f"Poll interval updated to: {poll_interval}s")

        # Reschedule if auto_scan is enabled
        if auto_scan_enabled() and not scan_state['is_scanning']:
            schedule_next_network_scan()
            logger.info(f"Scheduler rescheduled with {poll_interval}s completion-based interval")

    if 'link_remove_after_minutes' in data:
        link_remove_after = max(1, min(10080, int(data['link_remove_after_minutes'])))
        database.set_setting('link_remove_after_minutes', str(link_remove_after))
        logger.info(f"Dropped link retention updated to: {link_remove_after} minutes")

    return jsonify({'success': True, 'settings': database.get_all_settings()})


@app.route('/api/scan', methods=['POST'])
def api_trigger_scan():
    """Trigger an immediate scan"""
    if scan_state['is_scanning']:
        return jsonify({'error': 'Scan already in progress'}), 409

    # Run scan in background
    socketio.start_background_task(scheduled_scan)

    return jsonify({'success': True, 'message': 'Scan started'})


@app.route('/api/status')
def api_get_status():
    """Get current scan status"""
    return jsonify({
        'is_scanning': scan_state['is_scanning'],
        'last_scan': scan_state['last_scan'],
        'last_result': scan_state['last_result'],
        'node_count': len(database.get_active_nodes()),
        'link_count': len(database.get_active_links())
    })


@app.route('/api/events')
def api_get_events():
    """Get event log"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    events = database.get_events(limit=limit, offset=offset)
    return jsonify(events)


@app.route('/api/events/clear', methods=['POST'])
def api_clear_events():
    """Clear old events"""
    days = request.args.get('days', 30, type=int)
    count = database.clear_old_events(days=days)
    return jsonify({'success': True, 'cleared': count})


# ============ RF Stats API Routes ============

@app.route('/api/rf-stats/links')
def api_get_rf_links():
    """Get all RF links with current stats"""
    links = database.get_rf_links_with_latest_stats()
    return jsonify(links)


@app.route('/api/rf-stats/history/<source>/<target>')
def api_get_link_history(source, target):
    """Get historical data for a specific link"""
    hours = request.args.get('hours', 24, type=int)
    history = database.get_link_history(source, target, hours=hours)
    return jsonify(history)


@app.route('/api/rf-stats/history')
def api_get_all_rf_history():
    """Get summary history for all RF links"""
    hours = request.args.get('hours', 24, type=int)
    history = database.get_all_rf_links_history(hours=hours)
    return jsonify(history)


@app.route('/api/rf-stats/summary')
def api_get_rf_stats_summary():
    """Get RF stats collection status"""
    summary = rf_stats.get_rf_stats_summary()
    return jsonify(summary)


@app.route('/api/ping/<node_name>', methods=['POST'])
def api_ping_node(node_name):
    """Simple ping to a node - used by node panel and RF stats"""
    # Normalize node name to lowercase
    node_name = node_name.lower()

    # Get node's IP
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': f'Node "{node_name}" not found'}), 404
    if not node.get('ip'):
        return jsonify({'error': f'Node "{node_name}" has no IP address'}), 404

    # Run single ping (count=1 for quick response)
    result = rf_stats.ping_node(node['ip'], count=1, timeout=2)

    if result:
        return jsonify({
            'success': result.get('loss', 100) < 100,
            'node': node_name,
            'ip': node['ip'],
            'time': result.get('avg'),
            'loss': result.get('loss', 100)
        })

    return jsonify({'error': 'Ping failed'}), 500


@app.route('/api/rf-stats/test/<source>/<target>', methods=['POST'])
def api_trigger_rf_test(source, target):
    """Manually trigger a ping or iperf test for a specific link"""
    test_type = request.args.get('type', 'ping')

    # Normalize node names to lowercase (database stores them in lowercase)
    source = source.lower()
    target = target.lower()

    # Try to get target node's IP from database
    target_node = database.get_node(target)
    target_ip = target_node.get('ip') if target_node else None

    if test_type == 'ping':
        result = None

        # If we have the target IP, ping directly
        if target_ip:
            result = rf_stats.ping_node(target_ip)
        else:
            # Target not in database - use AREDN node's built-in ping
            # This allows pinging nodes beyond our scan depth
            result = rf_stats.ping_via_aredn(target)

        if result:
            database.update_link_history_ping(
                source, target,
                result.get('min'), result.get('avg'),
                result.get('max'), result.get('loss')
            )
            return jsonify({'success': True, 'result': result})
        return jsonify({'error': 'Ping failed - target may not be reachable'}), 500

    elif test_type == 'iperf':
        source_node = database.get_node(source)
        source_ip = source_node.get('ip') if source_node else None

        # iPerf requires source and target AREDN nodes. The source node is the
        # AREDN client; the target node is the temporary iperf3 server.
        if target_ip:
            result = rf_stats.run_iperf_test(
                target_ip,
                source_node_ip=source_ip,
                source_node_name=source,
                target_node_name=target
            )
        else:
            # Try using target hostname with .local.mesh suffix
            target_hostname = f"{target}.local.mesh"
            result = rf_stats.run_iperf_test(
                target_hostname,
                source_node_ip=source_ip,
                source_node_name=source,
                target_node_name=target
            )

        if result:
            database.update_link_history_throughput(
                source, target,
                result.get('tx_mbps'), result.get('rx_mbps')
            )
            return jsonify({'success': True, 'result': result})
        return jsonify({'error': 'iPerf test failed - target may not be reachable'}), 500

    return jsonify({'error': 'Invalid test type'}), 400


# ============ SocketIO Events ============

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info("Client connected")

    # Send current state
    emit('status', {
        'is_scanning': scan_state['is_scanning'],
        'last_scan': scan_state['last_scan']
    })

    # Send current network data
    emit('network_update', database.get_network_graph_data())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    from flask import request as flask_request

    sid = flask_request.sid

    # Stop any active ping for this client
    if sid in active_ping_sessions:
        del active_ping_sessions[sid]

    logger.info(f"Client {sid} disconnected")


@socketio.on('request_scan')
def handle_request_scan():
    """Handle scan request from client"""
    if not scan_state['is_scanning']:
        socketio.start_background_task(scheduled_scan)
        emit('scan_acknowledged', {'message': 'Scan started'})
    else:
        emit('scan_acknowledged', {'message': 'Scan already in progress'})


@socketio.on('request_network')
def handle_request_network():
    """Handle request for current network data"""
    emit('network_update', database.get_network_graph_data())


@socketio.on('request_events')
def handle_request_events(data=None):
    """Handle request for event log"""
    limit = 100
    if data and 'limit' in data:
        limit = data['limit']
    events = database.get_events(limit=limit)
    emit('events_update', events)


@socketio.on('start_node_ping')
def handle_start_node_ping(data):
    """Start continuous ping to a node"""
    global ping_session_counter
    from flask import request as flask_request

    node_name = data.get('node')
    if not node_name:
        emit('ping_error', {'error': 'No node specified'})
        return

    # Get node's IP
    node = database.get_node(node_name.lower())
    if not node or not node.get('ip'):
        emit('ping_error', {'error': 'Node IP not found'})
        return

    ip = node['ip']
    sid = flask_request.sid

    # Generate unique session ID for this ping
    ping_session_counter += 1
    session_id = ping_session_counter

    # Store the new session (this automatically invalidates any previous session)
    active_ping_sessions[sid] = {'node': node_name, 'session_id': session_id}

    logger.info(f"Starting continuous ping to {node_name} ({ip}) for client {sid}, session {session_id}")
    emit('ping_started', {'node': node_name, 'ip': ip})

    # Start ping loop using socketio's background task
    def ping_loop():
        nonlocal session_id, node_name, ip, sid

        logger.debug(f"Ping loop started for session {session_id}")

        while True:
            # Check if this session is still active
            current_session = active_ping_sessions.get(sid)
            if not current_session or current_session.get('session_id') != session_id:
                logger.debug(f"Session {session_id} no longer active, exiting loop")
                break

            result = rf_stats.ping_node(ip, count=1, timeout=2)

            # Check again after ping (it may have taken a few seconds)
            current_session = active_ping_sessions.get(sid)
            if not current_session or current_session.get('session_id') != session_id:
                logger.debug(f"Session {session_id} cancelled during ping, exiting loop")
                break

            ping_data = {
                'node': node_name,
                'ip': ip,
                'success': result.get('loss', 100) < 100 if result else False,
                'time': result.get('avg') if result else None,
                'loss': result.get('loss') if result else 100
            }
            socketio.emit('ping_result', ping_data)

            # Wait 1 second between pings
            socketio.sleep(1)

        logger.info(f"Stopped ping to {node_name} ({ip}), session {session_id}")

    socketio.start_background_task(ping_loop)


@socketio.on('stop_node_ping')
def handle_stop_node_ping(data=None):
    """Stop continuous ping"""
    from flask import request as flask_request

    sid = flask_request.sid
    current_session = active_ping_sessions.get(sid)
    node_name = current_session.get('node') if current_session else None

    if sid in active_ping_sessions:
        del active_ping_sessions[sid]
        logger.info(f"Stopped ping for client {sid}")
        emit('ping_stopped', {'node': node_name})


# ============ Startup ============

def run_ping_round_task():
    """Background task for ping tests"""
    if config.RF_STATS_ENABLED:
        rf_stats.run_ping_round(socketio)


def process_iperf_queue_task():
    """Background task for iperf queue processing"""
    if config.RF_STATS_ENABLED:
        # Queue all RF links for testing periodically
        rf_stats.queue_all_rf_links_for_iperf()
        rf_stats.process_iperf_queue(socketio)


def cleanup_history_task():
    """Background task for cleaning up old history"""
    rf_stats.cleanup_old_history()


def start_scheduler():
    """Start the background scheduler"""
    # Network scans are scheduled one at a time and re-armed after each scan completes.
    if auto_scan_enabled():
        schedule_next_network_scan(delay_seconds=5)

    # RF Stats jobs (if enabled)
    if config.RF_STATS_ENABLED:
        # Ping round every PING_INTERVAL seconds
        scheduler.add_job(
            run_ping_round_task,
            'interval',
            seconds=config.PING_INTERVAL,
            id='rf_ping_scan',
            replace_existing=True
        )

        # Iperf queue processing every IPERF_INTERVAL seconds
        scheduler.add_job(
            process_iperf_queue_task,
            'interval',
            seconds=config.IPERF_INTERVAL,
            id='rf_iperf_processor',
            replace_existing=True
        )

        # History cleanup every hour
        scheduler.add_job(
            cleanup_history_task,
            'interval',
            hours=1,
            id='rf_history_cleanup',
            replace_existing=True
        )

        logger.info(f"RF Stats enabled: ping every {config.PING_INTERVAL}s, iperf every {config.IPERF_INTERVAL}s")

    scheduler.start()
    logger.info("Scheduler started")


# Ensure scheduler shuts down cleanly on exit
def shutdown_scheduler():
    """Safely shutdown the scheduler"""
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down cleanly")
    except Exception as e:
        logger.warning(f"Error shutting down scheduler: {e}")

atexit.register(shutdown_scheduler)


if __name__ == '__main__':
    logger.info("Starting AREDN Network Monitor")

    # Initialize database
    database.init_db()

    # Only start scheduler in the main process (not the reloader)
    # When debug=True, Flask uses a reloader that spawns a child process
    # WERKZEUG_RUN_MAIN is set to 'true' in the child process
    if not config.DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        # Start scheduler
        start_scheduler()

        if auto_scan_enabled():
            logger.info("Initial scan scheduled to run in 5 seconds...")

    # Start server
    logger.info(f"Starting server on {config.HOST}:{config.PORT}")
    socketio.run(
        app,
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        use_reloader=config.DEBUG
    )

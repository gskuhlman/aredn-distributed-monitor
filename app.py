"""
AREDN Network Monitor - Flask Application
Main entry point for the web application
"""

# Use the OS resolver instead of eventlet's greendns. greendns reads DNS
# servers from every adapter (including disconnected ones on Windows) and
# times out before reaching the mesh node's DNS, so localnode.local.mesh
# fails to resolve. Must be set before eventlet is imported.
import os
import struct
os.environ.setdefault('EVENTLET_NO_GREENDNS', 'yes')

# Monkey-patch standard library for eventlet compatibility
# MUST be done before any other imports that use threading
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, jsonify, request, send_from_directory, abort
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
import logging
import atexit
import time
import os
from datetime import datetime, timedelta

import config
import database
import scanner
import rf_stats
import gateway
import reports
import health_score
import heatmap
import swimlanes
import unreachable
import cleanup
import link_diagnostics
import infra_audit
import drift
import aim_check
import power_advisor
import airtime
import change_log
import notes
import channel_map
import channel_planner
import link_watch
import route_watch
import path_view
import spof
import site_dashboard
import capacity
import weekly_report
import link_budget
import site_matrix
import collector_health
import weather
import link_health
import mesh_diagnostics
import incident_report
import troubleshoot
import voip
import voip_session
import couch_client
import notify
import maintenance

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


def selected_scan_enabled():
    """Return whether scans should be limited to selected nodes."""
    return database.get_setting('scan_selected_only', 'false') == 'true'


def run_configured_scan():
    """Run the currently configured scan mode."""
    if not selected_scan_enabled():
        return scanner.run_scan()

    selected_names = database.get_selected_node_names()
    result = {
        'nodes_found': 0,
        'links_found': 0,
        'nodes_visited': 0,
        'max_depth_reached': 0,
        'errors': [],
        'events': [],
        'timestamp': datetime.now().isoformat(),
        'starting_node_error': None,
        'aborted': False,
        'selected_scan': True,
        'selected_nodes': selected_names,
        'inactive_nodes': [],
        'dropped_links': []
    }

    if not selected_names:
        result['errors'].append('Selected-node scan mode is enabled, but no nodes are selected')
        return result

    for node_name in selected_names:
        node = database.get_node(node_name)
        if not node or not node.get('ip'):
            result['errors'].append(f'Selected node "{node_name}" has no pollable IP address')
            continue

        node_result = scanner.run_targeted_scan(node['ip'], max_depth=0, target_name=node_name)
        result['events'].extend(node_result.get('events', []))
        if node_result.get('aborted'):
            # Lost the starting node: stop, the remaining nodes can't be judged.
            result['aborted'] = True
            result['starting_node_error'] = node_result.get('starting_node_error')
            result['errors'].extend(node_result.get('errors', []))
            break
        result['nodes_found'] += node_result.get('nodes_found', 0)
        result['links_found'] += node_result.get('links_found', 0)
        result['nodes_visited'] += node_result.get('nodes_visited', 0)
        result['max_depth_reached'] = max(
            result['max_depth_reached'],
            node_result.get('max_depth_reached', 0)
        )
        result['errors'].extend(node_result.get('errors', []))

    result['timestamp'] = datetime.now().isoformat()
    return result


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

    scan_started = time.monotonic()
    try:
        result = run_configured_scan()
        scan_state['last_scan'] = result['timestamp']
        scan_state['last_result'] = result

        # Starting-node health: measure DNS/connect/HTTP to the starting node
        # (and the LAN gateway if that fails) off the scan path.
        socketio.start_background_task(collector_health.record, 'scan', result,
                                       int((time.monotonic() - scan_started) * 1000))

        # Check if starting node was unreachable
        if result.get('starting_node_error'):
            socketio.emit('starting_node_error', {
                'error': result['starting_node_error'],
                'timestamp': datetime.now().isoformat()
            })
            logger.error(f"Starting node error: {result['starting_node_error']}")

        if result.get('aborted'):
            # Results were discarded; skip RF history/probing (they'd act on
            # stale state or fail for our own lack of connectivity).
            for event in result.get('events', []):
                socketio.emit('new_event', event)
            socketio.emit('scan_aborted', {
                'error': result.get('starting_node_error'),
                'timestamp': result['timestamp']
            })
            return

        # Record RF link stats (quality/SNR) to history
        if config.RF_STATS_ENABLED:
            rf_stats.record_rf_link_stats()

        # --- Link Watch: noise-floor and one-way link detectors (transitions only) ---
        if config.LINK_WATCH_ENABLED:
            try:
                for event in link_watch.run_after_scan():
                    socketio.emit('new_event', event)
            except Exception as exc:
                logger.error("Link watch error: %s", exc)
        # --- end Link Watch ---

        # Kick off high-rate incident probing for any watched link in trouble.
        if config.INCIDENT_MODE_ENABLED:
            rf_stats.maybe_run_incident_probes(socketio)

        # Ask neighbors to confirm reachability of nodes we couldn't poll, so we
        # can tell "reachable via mesh" from "really down". Background task.
        if config.MESH_PROBE_ENABLED:
            socketio.start_background_task(rf_stats.probe_mesh_reachability, socketio)

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
        socketio.start_background_task(collector_health.record, 'scan', None,
                                       int((time.monotonic() - scan_started) * 1000), str(e))

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


@app.route('/sites/<code>')
def site_page(code):
    """Site dashboard page (site_dashboard.py)."""
    return render_template('site.html', site_code=code.upper())


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
    node_name = name.lower()
    node = database.get_observed_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    services = database.get_node_services(node_name)
    links = database.get_node_all_links(node_name) if node.get('is_link_only') else database.get_node_links(node_name)

    return jsonify({
        'node': node,
        'services': services,
        'links': links
    })


@app.route('/api/nodes/all')
def api_get_all_nodes():
    """Get all node names ever seen, including link-only endpoints."""
    nodes = database.get_all_observed_nodes()
    return jsonify(nodes)


@app.route('/api/nodes/detail/<name>')
def api_get_node_detail(name):
    """Get detailed information about a node"""
    node_name = name.lower()
    node = database.get_observed_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    services = database.get_node_services(node_name)
    links = database.get_node_all_links(node_name)
    node['rf_span'] = infra_audit.annotate_link_rf(node_name, links, node)
    connectivity_log = database.get_node_observed_events(node_name)

    return jsonify({
        'node': node,
        'services': services,
        'links': links,
        'connectivity_log': connectivity_log,
        'unreachable_cause': unreachable.node_cause(node_name),
        'aim': _node_aim(node_name)
    })


@app.route('/api/nodes/full/<name>')
def api_get_node_full(name):
    """Get all locally available information for one node."""
    node_name = name.lower()
    node = database.get_observed_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    hours = request.args.get('hours', 24, type=int)
    event_limit = request.args.get('event_limit', 500, type=int)

    all_links = database.get_node_all_links(node_name)
    node['rf_span'] = infra_audit.annotate_link_rf(node_name, all_links, node)
    # Reachability for this node AND its peers (one shared derivation).
    names = {node_name}
    for l in all_links:
        names.add(l['source_node'])
        names.add(l['target_node'])
    reach_map = database.get_reach_status_map(list(names))

    return jsonify({
        'node': node,
        'services': database.get_node_services(node_name),
        'active_links': database.get_node_links(node_name),
        'all_links': all_links,
        'connectivity_log': database.get_node_observed_events(node_name, limit=event_limit),
        'quality_history': database.get_node_history(node_name, hours=hours),
        'ping_history': database.get_node_ping_history(node_name, hours=hours),
        'link_health': link_health.analyze_node_links(node_name, hours=hours),
        'node_health': database.get_node_health_history(node_name, hours=hours),
        'incident_samples': database.get_incident_samples(node_name, hours=hours),
        'flap_report': database.get_link_flap_report(hours=hours, node=node_name),
        'reach': reach_map.get(node_name),
        'unreachable_cause': unreachable.node_cause(node_name, hours=hours),
        'aim': _node_aim(node_name),
        'maintenance': _node_maintenance(node_name),
        'peer_reach': reach_map
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
        result = scanner.run_targeted_scan(node['ip'], max_depth=0, target_name=node_name)
        if result.get('aborted'):
            return jsonify({'error': result.get('starting_node_error'), 'result': result}), 503
        network_data = database.get_network_graph_data()
        socketio.emit('scan_complete', {
            'result': result,
            'network': network_data
        })
        return jsonify({'success': True, 'result': result})
    except Exception as exc:
        logger.error("Targeted scan failed for %s: %s", node_name, exc)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/nodes/selected')
def api_get_selected_nodes():
    """Get nodes included in selected-node mode."""
    return jsonify({'selected_nodes': database.get_selected_node_names()})


@app.route('/api/nodes/selected/<name>', methods=['POST'])
def api_set_selected_node(name):
    """Include or exclude a node from selected-node mode."""
    node_name = name.lower()
    node = database.get_observed_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json(silent=True) or {}
    selected = bool(data.get('selected'))
    is_selected = database.set_node_selected(node_name, selected)
    network_data = database.get_network_graph_data()
    socketio.emit('network_update', network_data)
    return jsonify({
        'success': True,
        'node': node_name,
        'is_selected': is_selected,
        'selected_nodes': database.get_selected_node_names()
    })


@app.route('/api/nodes/infrastructure')
def api_get_infrastructure_nodes():
    """Infrastructure nodes with their site codes, plus the site-name map."""
    return jsonify({'infrastructure_nodes': database.get_infrastructure_nodes(),
                    'sites': database.get_site_names()})


@app.route('/api/nodes/infrastructure/<name>', methods=['POST'])
def api_set_infrastructure_node(name):
    """Mark or unmark a node as infrastructure ({infrastructure: bool, site?})."""
    node_name = name.lower()
    if not database.get_observed_node(node_name):
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    site = (data.get('site') or '').strip().upper() or None
    is_infra = database.set_node_infrastructure(node_name, bool(data.get('infrastructure')), site=site)
    socketio.emit('network_update', database.get_network_graph_data())
    info = database.infrastructure_info(node_name)
    return jsonify({'success': True, 'node': node_name, 'is_infrastructure': is_infra,
                    'infrastructure_site': info['infrastructure_site'],
                    'sites': database.get_site_names()})


@app.route('/api/nodes/tags')
def api_get_node_tags():
    """Get node names for each infrastructure tag (permanent / event)."""
    return jsonify(database.get_tagged_node_names())


@app.route('/api/nodes/tags/<name>', methods=['POST'])
def api_set_node_tag(name):
    """Add or remove an infrastructure tag: {"tag": "permanent"|"event", "enabled": bool}."""
    node_name = name.lower()
    node = database.get_observed_node(node_name)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json(silent=True) or {}
    tag = data.get('tag')
    if tag not in database.NODE_TAGS:
        return jsonify({'error': f"tag must be one of {', '.join(database.NODE_TAGS)}"}), 400

    flags = database.set_node_tag(node_name, tag, bool(data.get('enabled')))
    network_data = database.get_network_graph_data()
    socketio.emit('network_update', network_data)
    return jsonify({'success': True, 'node': node_name, **flags})


@app.route('/api/node-positions', methods=['GET'])
def api_get_node_positions():
    """Get the saved graph layout."""
    return jsonify({'positions': database.get_node_positions()})


@app.route('/api/node-positions', methods=['POST'])
def api_save_node_positions():
    """Save the graph layout as {positions: {node_name: {x, y}}}."""
    data = request.get_json(silent=True) or {}
    positions = data.get('positions')
    if not isinstance(positions, dict):
        return jsonify({'error': 'positions must be an object of {node_name: {x, y}}'}), 400

    saved = database.save_node_positions(positions)
    logger.info(f"Saved node layout: {saved} positions")
    return jsonify({'success': True, 'saved': saved})


@app.route('/api/node-positions', methods=['DELETE'])
def api_clear_node_positions():
    """Delete the saved graph layout."""
    database.clear_node_positions()
    logger.info("Cleared saved node layout")
    return jsonify({'success': True})


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
    log = database.get_node_observed_events(node_name, limit=limit)
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
    if 'scan_selected_only' not in settings:
        settings['scan_selected_only'] = 'false'
    if 'poll_interval' not in settings:
        settings['poll_interval'] = config.POLL_INTERVAL
    else:
        settings['poll_interval'] = int(settings['poll_interval'])
    if 'link_remove_after_minutes' not in settings:
        settings['link_remove_after_minutes'] = config.LINK_REMOVE_AFTER // 60
    else:
        settings['link_remove_after_minutes'] = int(settings['link_remove_after_minutes'])
    if 'new_node_days' not in settings:
        settings['new_node_days'] = config.NEW_NODE_DAYS
    else:
        settings['new_node_days'] = int(settings['new_node_days'])
    if 'database_retention_days' not in settings:
        settings['database_retention_days'] = config.DATABASE_RETENTION_DAYS
    else:
        settings['database_retention_days'] = int(settings['database_retention_days'])

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

    if 'scan_selected_only' in data:
        scan_selected_only = 'true' if data['scan_selected_only'] else 'false'
        database.set_setting('scan_selected_only', scan_selected_only)
        logger.info(f"Selected-node scan mode updated to: {scan_selected_only}")

        if auto_scan_enabled() and not scan_state['is_scanning']:
            schedule_next_network_scan()

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
        logger.info(f"Dropped link display retention updated to: {link_remove_after} minutes")

    if 'new_node_days' in data:
        new_node_days = max(1, min(3650, int(data['new_node_days'])))
        database.set_setting('new_node_days', str(new_node_days))
        logger.info(f"New node announcement window updated to: {new_node_days} days")

    if 'database_retention_days' in data:
        database_retention_days = max(1, min(3650, int(data['database_retention_days'])))
        database.set_setting('database_retention_days', str(database_retention_days))
        logger.info(f"Database retention updated to: {database_retention_days} days")

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


def gateway_error_response():
    """503 response for a probe refused/discarded because the starting node is
    unreachable (the failure is ours, not the target's)."""
    return jsonify({'error': gateway.unreachable_message(), 'gateway_unreachable': True}), 503


@app.route('/api/rf-stats/links')
def api_get_rf_links():
    """Get all RF links with current stats"""
    links = database.get_rf_links_with_latest_stats()
    return jsonify(links)


@app.route('/api/rf-stats/ping-all', methods=['POST'])
def api_rf_ping_all():
    """Ping RF links from the collector concurrently and record the results, then
    return them so the overview table can repaint with fresh data.

    Optional JSON body ``{"links": [["src","tgt"], ...]}`` restricts the sweep to
    a caller-supplied set (e.g. only the filtered rows); otherwise all active RF
    links are pinged. Optional ``{"clear": true}`` nulls previous ping/throughput
    results in history before pinging."""
    body = request.get_json(silent=True) or {}
    links = body.get('links')
    do_clear = bool(body.get('clear', False))
    try:
        results = rf_stats.ping_all_rf_links(links=links, clear_first=do_clear)
    except rf_stats.GatewayUnreachable:
        return gateway_error_response()
    response = {'results': results}
    if any(r.get('aborted') for r in results):
        response['error'] = gateway.unreachable_message()
        response['gateway_unreachable'] = True
    return jsonify(response)


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


@app.route('/api/reports/infrastructure', methods=['GET', 'POST'])
def api_infrastructure_review():
    """Audit infrastructure radio settings. POST {refresh: true} first pulls
    current settings from each infrastructure node."""
    refresh = request.method == 'POST' and bool((request.get_json(silent=True) or {}).get('refresh', True))
    result = infra_audit.run_review(refresh=refresh)
    if result.get('gateway_unreachable'):
        return jsonify(result), 503
    return jsonify(result)


@app.route('/api/reports/aim')
def api_antenna_aim():
    """Antenna aim check: set azimuth/elevation vs. the bearings of the RF
    peers each directional radio hears, plus location sanity (?node= for one
    radio, ignoring scope)."""
    try:
        return jsonify(aim_check.run_report(scope=request.args.get('scope', 'all'),
                                            node=request.args.get('node') or None))
    except Exception as e:
        logger.exception("Antenna aim report failed")
        return jsonify({'error': str(e)}), 500


@app.route('/api/power-advisor/<node>')
def api_power_advisor(node):
    """Predicted neighbour SNR after a TX power cut (?cut_db=, ?hours=24).
    Without cut_db: the 0..20 dB curve and the largest safe cut. Refuses
    infrastructure radios (their power is never cut)."""
    try:
        hours = max(1, min(24 * 31, int(request.args.get('hours', 24))))
        cut = request.args.get('cut_db')
        cut = float(cut) if cut not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'hours and cut_db must be numbers'}), 400
    result = power_advisor.advise(node, cut_db=cut, hours=hours)
    if result.get('error'):
        return jsonify(result), 404
    return jsonify(result)


def _node_aim(node_name):
    """Aim summary for the node views; never breaks the page."""
    try:
        return aim_check.radio_aim(node_name)
    except Exception:
        logger.exception("Aim summary failed for %s", node_name)
        return None


@app.route('/api/reports/drift')
def api_settings_drift():
    """Firmware/settings drift across RF links, sites and the whole mesh."""
    scope = request.args.get('scope', 'all')
    try:
        return jsonify(drift.run_report(scope=scope))
    except Exception as e:
        logger.exception("Settings drift report failed")
        return jsonify({'error': str(e)}), 500


# ---- Sector Airtime (TODO #6) ----
@app.route('/api/reports/airtime', methods=['GET', 'POST'])
def api_sector_airtime():
    """Per-sector clients, hearing matrix, hidden pairs and contention.
    POST {overrides, scope?, hours?} previews a Channel Map what-if plan."""
    body = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
    scope = body.get('scope') or request.args.get('scope', 'all')
    try:
        hours = int(body.get('hours') or request.args.get('hours') or 24)
    except (TypeError, ValueError):
        return jsonify({'error': 'hours must be a number'}), 400
    try:
        return jsonify(airtime.run_report(scope=scope, hours=hours, overrides=body.get('overrides') or None))
    except Exception as e:
        logger.exception("Sector airtime report failed")
        return jsonify({'error': str(e)}), 500
# ---- end Sector Airtime ----


# ---- Channel Map (TODO #3) ----
def _pmtiles_header(path):
    """Zoom range and bounds from a PMTiles v3 header (first 127 bytes)."""
    with open(path, 'rb') as f:
        head = f.read(127)
    if len(head) < 127 or head[:7] != b'PMTiles' or head[7] != 3:
        return None
    min_zoom, max_zoom = head[100], head[101]
    west, south, east, north = (v / 1e7 for v in struct.unpack('<4i', head[102:118]))
    return {'min_zoom': min_zoom, 'max_zoom': max_zoom, 'bounds': [west, south, east, north]}


@app.route('/api/map/base')
def api_map_base():
    """Offline base map layers for the Channel Map: every .pmtiles extract in
    the maps folder, least detailed first (drawn underneath). Empty means
    online OpenStreetMap tiles."""
    layers = []
    folder = config.OFFLINE_MAP_DIR
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        path = os.path.join(folder, name)
        if not name.endswith('.pmtiles') or not os.path.isfile(path):
            continue
        header = _pmtiles_header(path)
        if not header:
            continue
        stat = os.stat(path)
        layers.append({'name': name[:-len('.pmtiles')], 'url': f'/maps/{name}', 'size_bytes': stat.st_size,
                       'updated': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                       **header})
    layers.sort(key=lambda l: (l['max_zoom'], -(l['bounds'][2] - l['bounds'][0]) * (l['bounds'][3] - l['bounds'][1])))
    return jsonify({'offline': bool(layers), 'layers': layers})


@app.route('/maps/<path:filename>')
def offline_map_file(filename):
    """Serve the offline map with HTTP range support (the PMTiles reader
    fetches byte ranges, not the whole file)."""
    if not filename.endswith('.pmtiles'):
        abort(404)
    return send_from_directory(os.path.abspath(config.OFFLINE_MAP_DIR), filename, conditional=True)


@app.route('/api/channel-map')
def api_channel_map():
    """Located radios, channel conflicts and RF links for the Channel Map tab."""
    return jsonify(channel_map.build_map(
        scope=request.args.get('scope', 'all'),
        max_km=request.args.get('max_km'),
        center=request.args.get('center'),
        include_inactive=request.args.get('include_inactive') in ('1', 'true')))


@app.route('/api/channel-map/what-if', methods=['POST'])
def api_channel_map_what_if():
    """Recompute conflicts with temporary channel overrides (nothing is saved).
    Body: {overrides: {name: {frequency, chanbw}} or "name freq width" lines,
    scope?, max_km?, center?, include_inactive?}"""
    body = request.get_json(silent=True) or {}
    return jsonify(channel_map.build_map(
        scope=body.get('scope', 'all'), max_km=body.get('max_km'), center=body.get('center'),
        overrides=body.get('overrides') or {}, include_inactive=bool(body.get('include_inactive'))))


# ---- Channel planner (TODO #4) ----
@app.route('/api/channel-planner', methods=['POST'])
def api_channel_planner():
    """Ranked channel plans for a site or radio list (nothing is saved).
    Body: {site? | radios?: [...], band?: [low, high], widths?: [..] | {name: [..]},
    locked?: [...], move_clients?: bool, top?: N, compare?: "name freq width" lines,
    idle_off?: bool (default true: radios with no RF links are suggested off),
    separate_sectors?: bool (one channel per radio on a mast, as AREDN recommends)}"""
    body = request.get_json(silent=True) or {}
    radios = body.get('radios')
    if isinstance(radios, str):
        radios = [n for n in radios.replace(',', ' ').split() if n]
    locked = body.get('locked')
    if isinstance(locked, str):
        locked = [n for n in locked.replace(',', ' ').split() if n]
    band = body.get('band')
    if band:
        try:
            band = [float(band[0]), float(band[1])]
        except (TypeError, ValueError, IndexError):
            return jsonify({'error': 'band must be [low, high] in MHz'}), 400
        if band[1] - band[0] < 10:
            return jsonify({'error': 'band is too narrow'}), 400
    if not body.get('site') and not radios:
        return jsonify({'error': 'site or radios is required'}), 400
    try:
        top = max(1, min(10, int(body.get('top') or 3)))
    except (TypeError, ValueError):
        top = 3
    result = channel_planner.plan(
        site=body.get('site'), radios=radios, band=band, widths=body.get('widths'),
        locked=locked, move_clients=body.get('move_clients', True) is not False,
        top=top, compare=body.get('compare') or None, idle_off=body.get('idle_off', True) is not False,
        separate_sectors=bool(body.get('separate_sectors')))
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)
# ---- end Channel Map ----
# --- Link Watch routes ---
@app.route('/api/reports/noise')
def api_noise_report():
    """Noise-floor watch: all radios' status, or ?node= one radio's series,
    baseline and hour-of-day profile."""
    hours = max(1, min(24 * 31, request.args.get('hours', 72, type=int)))
    days = max(1, min(31, request.args.get('days', 7, type=int)))
    return jsonify(link_watch.noise_report(node=request.args.get('node') or None, hours=hours, days=days))


@app.route('/api/reports/one-way')
def api_one_way_report():
    """One-way link alarm: flagged, pending and recently cleared RF pairs."""
    hours = max(1, min(24 * 31, request.args.get('hours', 24, type=int)))
    return jsonify(link_watch.one_way_report(hours=hours))
# --- end Link Watch routes ---


# --- Route Watch routes (route_watch.py) ---
def _hours_arg(default=24, limit=24 * 31):
    try:
        return max(1, min(limit, int(request.args.get('hours', default))))
    except (TypeError, ValueError):
        return default


@app.route('/api/routes', methods=['GET'])
def api_routes():
    """Watched paths with status, last route, baseline and hop-count series."""
    return jsonify(route_watch.list_report(hours=_hours_arg()))


@app.route('/api/routes', methods=['POST'])
def api_routes_add():
    """Add a path {source? (empty = starting node), target, label?}."""
    body = request.get_json(silent=True) or {}
    try:
        path_id = route_watch.add_path(body.get('source'), body.get('target'), body.get('label'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'id': path_id})


@app.route('/api/routes/<int:path_id>', methods=['DELETE'])
def api_routes_delete(path_id):
    """Delete a manual path (an automatic one is disabled instead)."""
    if not route_watch.delete_path(path_id):
        return jsonify({'error': 'Path not found'}), 404
    return jsonify({'success': True})


@app.route('/api/routes/<int:path_id>/history')
def api_routes_history(path_id):
    result = route_watch.path_history(path_id, hours=_hours_arg())
    if result is None:
        return jsonify({'error': 'Path not found'}), 404
    return jsonify(result)


@app.route('/api/routes/<int:path_id>/run', methods=['POST'])
def api_routes_run(path_id):
    """Traceroute one path now (gated by the starting node)."""
    result = route_watch.run_one(path_id)
    if result is None:
        return jsonify({'error': 'Path not found'}), 404
    for event in result['events']:
        socketio.emit('new_event', event)
    if result.get('aborted'):
        return jsonify({'error': result['aborted'], 'gateway_unreachable': result['aborted'] != 'busy'}), 503
    return jsonify(result)


@app.route('/api/path')
def api_path_view():
    """Expected path, watched-route history and per-hop link quality between
    source (empty = starting node) and target (path_view.py)."""
    try:
        return jsonify(path_view.view(request.args.get('source'), request.args.get('target'),
                                      hours=_hours_arg()))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/path/live', methods=['POST'])
def api_path_live():
    """Traceroute source -> target now and grade each hop (503 when the
    starting node is unreachable)."""
    body = request.get_json(silent=True) or {}
    try:
        result = path_view.live(body.get('source'), body.get('target'), hours=int(body.get('hours') or 24))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if result.get('error'):
        return jsonify(result), 503 if result.get('gateway_unreachable') else 502
    return jsonify(result)


@app.route('/api/reports/spof')
def api_report_spof():
    """Links and nodes whose loss cuts nodes off from the starting node,
    ranked by nodes behind x weakness (spof.py); tunnels=0 = internet down."""
    return jsonify(spof.report(hours=_hours_arg(),
                               exclude_tunnels=request.args.get('tunnels') in ('0', 'false', 'no')))


@app.route('/api/sites')
def api_sites():
    return jsonify({'sites': site_dashboard.sites()})


@app.route('/api/sites/<code>')
def api_site(code):
    """Site dashboard data: radios, spectrum pairs, clients per sector, uplink
    paths, dependencies, health scores, events, notes, maintenance."""
    result = site_dashboard.build(code, hours=_hours_arg())
    if result is None:
        return jsonify({'error': f'No infrastructure nodes with site {code.upper()}',
                        'sites': site_dashboard.sites()}), 404
    return jsonify(result)


# --- Capacity + scheduled iPerf (capacity.py) ---
@app.route('/api/capacity')
def api_capacity():
    """Capacity per key link direction: overnight iPerf capacity, PHY rate,
    traffic estimate, spare, nightly trend; plus the schedule's state."""
    return jsonify(capacity.report(hours=_hours_arg(168)))


@app.route('/api/capacity/link')
def api_capacity_link():
    source = (request.args.get('source') or '').strip().lower()
    target = (request.args.get('target') or '').strip().lower()
    if not source or not target:
        return jsonify({'error': 'source and target are required'}), 400
    return jsonify(capacity.link_series(source, target, hours=_hours_arg(168)))


@app.route('/api/capacity/links', methods=['POST'])
def api_capacity_links():
    """Add a key pair {source, target} or turn one on/off {source, target, enabled}."""
    body = request.get_json(silent=True) or {}
    try:
        if 'enabled' in body:
            capacity.set_enabled((body.get('source') or '').lower(), (body.get('target') or '').lower(),
                                 bool(body['enabled']))
        else:
            capacity.add_link(body.get('source'), body.get('target'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'key_links': len(capacity.key_links())})


@app.route('/api/capacity/run', methods=['POST'])
def api_capacity_run():
    """Start the scheduled iPerf pass now (background; ends at the schedule end
    or after one round when run outside the window)."""
    if capacity.schedule_state['running']:
        return jsonify({'error': 'already running', 'state': capacity.schedule_state}), 409
    if not gateway.is_reachable():
        return gateway_error_response()
    socketio.start_background_task(run_iperf_schedule_task, True)
    return jsonify({'success': True, 'planned': len(capacity.key_links())})


def run_iperf_schedule_task(manual=False):
    try:
        deadline = datetime.now() + timedelta(hours=1) if manual else None
        result = capacity.run_schedule(deadline=deadline, rounds=1 if manual else None)
        logger.info("Scheduled iPerf: %s", result)
    except Exception as exc:
        logger.error("Scheduled iPerf error: %s", exc)
# --- end Capacity ---


# --- Weekly report (weekly_report.py) ---
@app.route('/api/weekly-reports')
def api_weekly_reports():
    return jsonify({'reports': weekly_report.list_reports(),
                    'channels': weekly_report.get_channels(),
                    'schedule': {'enabled': config.WEEKLY_REPORT_ENABLED, 'day': config.WEEKLY_REPORT_DAY,
                                 'time': config.WEEKLY_REPORT_TIME}})


@app.route('/api/weekly-reports/<int:report_id>')
def api_weekly_report(report_id):
    r = weekly_report.get(report_id)
    if not r:
        return jsonify({'error': 'Report not found'}), 404
    return jsonify(r)


@app.route('/api/weekly-reports', methods=['POST'])
def api_weekly_report_generate():
    """Build and save a report for the last 7 days now; {send: true} also sends it."""
    body = request.get_json(silent=True) or {}
    return jsonify(weekly_report.generate(trigger='manual', send_now=bool(body.get('send'))))


@app.route('/api/weekly-reports/<int:report_id>/send', methods=['POST'])
def api_weekly_report_send(report_id):
    body = request.get_json(silent=True) or {}
    sent = weekly_report.send(report_id, channels=body.get('channels'))
    if sent is None:
        return jsonify({'error': 'Report not found'}), 404
    return jsonify({'sent': sent})


@app.route('/api/weekly-reports/<int:report_id>', methods=['DELETE'])
def api_weekly_report_delete(report_id):
    if not weekly_report.delete(report_id):
        return jsonify({'error': 'Report not found'}), 404
    return jsonify({'success': True})


@app.route('/api/weekly-reports/channels', methods=['POST'])
def api_weekly_report_channels():
    """Channels the scheduled report is sent on (names only; secrets stay in notify)."""
    body = request.get_json(silent=True) or {}
    return jsonify({'channels': weekly_report.set_channels(body.get('channels') or [])})


def run_weekly_report_task():
    try:
        result = weekly_report.generate(trigger='scheduled', send_now=True)
        logger.info("Weekly report %s saved, sent: %s", result['id'], result['sent'])
    except Exception as exc:
        logger.error("Weekly report error: %s", exc)
# --- end Weekly report ---


# --- Link budget + terrain (link_budget.py) ---
@app.route('/api/link-budget/<a>/<b>')
def api_link_budget(a, b):
    """Predicted vs measured signal both ways, terrain profile and Fresnel
    clearance for an RF pair (elevations from the cache or Open-Meteo)."""
    result = link_budget.budget(a, b, hours=_hours_arg())
    if result.get('error'):
        return jsonify(result), 404
    return jsonify(result)


@app.route('/api/reports/link-budget')
def api_link_budget_report():
    """Link budget summary for every current RF link with an infrastructure end."""
    return jsonify(link_budget.infrastructure_links(hours=_hours_arg()))
# --- end Link budget ---


def run_route_watch_task():
    """Scheduled Route Watch run; never blocks scans (own APScheduler job)."""
    try:
        result = route_watch.run_paths()
        for event in result['events']:
            socketio.emit('new_event', event)
        if result.get('aborted'):
            logger.info("Route watch skipped: %s", result['aborted'])
    except Exception as exc:
        logger.error("Route watch error: %s", exc)
# --- end Route Watch routes ---


@app.route('/api/reports/site-matrix', methods=['GET', 'POST'])
def api_site_matrix():
    """Site neighbor matrix: ?site=COL or ?radios=a,b,c, &hours=24. POST
    {site|radios, hours, overrides} adds planned channel relations (what-if)."""
    body = request.get_json(silent=True) or {}
    site = body.get('site') or request.args.get('site')
    radios = body.get('radios') or request.args.get('radios')
    if isinstance(radios, str):
        radios = [n for n in radios.replace(',', ' ').split() if n]
    if not site and not radios:
        return jsonify({'error': 'site or radios is required'}), 400
    try:
        hours = int(body.get('hours') or request.args.get('hours') or 24)
    except (TypeError, ValueError):
        return jsonify({'error': 'hours must be a number'}), 400
    result = site_matrix.build(site=site, radios=radios, hours=hours,
                               overrides=body.get('overrides') or request.args.get('overrides') or None)
    if result.get('error'):
        return jsonify(result), 404
    return jsonify(result)


@app.route('/api/rf-stats/diagnose/<a>/<b>')
def api_rf_diagnose_pair(a, b):
    """Diagnose everything known about the link between two nodes."""
    hours = max(1, min(24 * 31, request.args.get('hours', 24, type=int)))
    return jsonify(link_diagnostics.diagnose_pair(a, b, hours=hours))


@app.route('/api/rf-stats/diagnose/<a>/<b>/live', methods=['POST'])
def api_rf_diagnose_pair_live(a, b):
    """Ping/traceroute between the two nodes from their own CGI."""
    result = link_diagnostics.run_live_checks(a, b)
    if result.get('gateway_unreachable'):
        return jsonify(result), 503
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


# --- change log (TODO #1) ---------------------------------------------------

@app.route('/api/changes', methods=['GET'])
def api_list_changes():
    """Channel/power/antenna/firmware changes, newest first."""
    node = (request.args.get('node') or '').strip().lower() or None
    hours = request.args.get('hours', type=int)
    limit = max(1, min(2000, request.args.get('limit', 500, type=int)))
    return jsonify({'changes': change_log.list_changes(node=node, hours=hours, limit=limit),
                    'categories': list(change_log.CATEGORIES)})


@app.route('/api/changes', methods=['POST'])
def api_add_change():
    """Record a manual change {node_name, description, category?, timestamp?, peer_node?, author?}."""
    data = request.get_json(silent=True) or {}
    try:
        change = change_log.add_change(
            data.get('node_name'), data.get('description'), data.get('category') or 'other',
            data.get('timestamp'), data.get('peer_node'), data.get('author'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'change': change})


@app.route('/api/changes/<int:change_id>', methods=['DELETE'])
def api_delete_change(change_id):
    if not change_log.delete_change(change_id):
        return jsonify({'error': 'Change not found'}), 404
    return jsonify({'success': True})


@app.route('/api/changes/<int:change_id>/compare')
def api_compare_change(change_id):
    """Before vs. after stats for every RF link of the changed node."""
    hours = request.args.get('hours', 24, type=int)
    result = change_log.compare_change(change_id, window_hours=hours)
    if result is None:
        return jsonify({'error': 'Change not found'}), 404
    return jsonify(result)

# --- end change log ---------------------------------------------------------

# --- notes on nodes, links and sites (TODO #23) ----------------------------

@app.route('/api/notes', methods=['GET'])
def api_list_notes():
    """Notes for ?node (its own, its links' and its site's), ?node&peer (the
    pair, both nodes and both sites), ?site, or all; ?pinned=1 for pinned only."""
    pinned = request.args.get('pinned')
    return jsonify({'notes': notes.list_notes(
        node=request.args.get('node'), peer=request.args.get('peer'), site=request.args.get('site'),
        pinned=None if pinned in (None, '') else pinned in ('1', 'true'),
        limit=request.args.get('limit', 200, type=int))})


@app.route('/api/notes', methods=['POST'])
def api_add_note():
    """{target_type?, node_name?, peer_node?, site?, text, author?, pinned?, event_time?}"""
    try:
        note = notes.add_note(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'note': note})


@app.route('/api/notes/<int:note_id>', methods=['PATCH'])
def api_update_note(note_id):
    """Edit {text?, pinned?, event_time?, author?}."""
    try:
        note = notes.update_note(note_id, request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if not note:
        return jsonify({'error': 'Note not found'}), 404
    return jsonify({'success': True, 'note': note})


@app.route('/api/notes/<int:note_id>', methods=['DELETE'])
def api_delete_note(note_id):
    if not notes.delete_note(note_id):
        return jsonify({'error': 'Note not found'}), 404
    return jsonify({'success': True})

# --- end notes ---------------------------------------------------------------


# --- maintenance windows (TODO #22) -------------------------------------------

def _node_maintenance(node_name):
    """The window covering a node now (for the node page), plus its site code."""
    try:
        w = maintenance.covering_window(node_name)
        return {'active': w, 'site': maintenance.site_of(node_name)}
    except Exception as exc:
        logger.debug(f"maintenance lookup failed for {node_name}: {exc}")
        return {'active': None, 'site': None}


@app.route('/api/maintenance', methods=['GET'])
def api_list_maintenance():
    """Maintenance windows ending within the last `days` (default 14), plus active ones."""
    days = max(1, min(90, request.args.get('days', 14, type=int)))
    return jsonify({'windows': maintenance.list_windows(days=days),
                    'active': maintenance.active_windows(),
                    'grace_minutes': config.MAINTENANCE_GRACE_MINUTES,
                    'sites': database.get_site_names(), 'scopes': list(maintenance.SCOPES)})


@app.route('/api/maintenance/active')
def api_active_maintenance():
    """Windows covering now (grace period included), for the header banner."""
    return jsonify({'active': maintenance.active_windows(),
                    'grace_minutes': config.MAINTENANCE_GRACE_MINUTES})


@app.route('/api/maintenance', methods=['POST'])
def api_create_maintenance():
    """{scope, site?, nodes?, start?, end? | duration_minutes?, note?, author?,
    include_rf_peers?, change_log?}"""
    try:
        window = maintenance.create_window(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    socketio.emit('maintenance_update', {'active': maintenance.active_windows()})
    return jsonify({'success': True, 'window': window})


@app.route('/api/maintenance/<int:window_id>', methods=['PATCH'])
def api_update_maintenance(window_id):
    """{end_now: true} | {extend_minutes: N} | {end: ts} | {note}"""
    try:
        window = maintenance.update_window(window_id, request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if window is None:
        return jsonify({'error': 'Window not found'}), 404
    socketio.emit('maintenance_update', {'active': maintenance.active_windows()})
    return jsonify({'success': True, 'window': window})


@app.route('/api/maintenance/<int:window_id>', methods=['DELETE'])
def api_delete_maintenance(window_id):
    if not maintenance.delete_window(window_id):
        return jsonify({'error': 'Window not found'}), 404
    socketio.emit('maintenance_update', {'active': maintenance.active_windows()})
    return jsonify({'success': True})

# --- end maintenance windows ---------------------------------------------------


@app.route('/api/rf-stats/summary')
def api_get_rf_stats_summary():
    """Get RF stats collection status"""
    summary = rf_stats.get_rf_stats_summary()
    return jsonify(summary)


# ============ Time-Window Reports ============

def _report_args():
    """Common Reports-tab query args: the window plus the page filters."""
    cats = request.args.get('events')
    return {
        'start': request.args.get('start'),
        'end': request.args.get('end'),
        'scope': request.args.get('scope', 'all'),
        'link_type': request.args.get('link_type', 'all'),
        'search': request.args.get('search', ''),
        'sample_type': request.args.get('sample_type', 'all'),
        'event_categories': cats.split(',') if cats else None,
        'severity': request.args.get('severity', 'all'),
    }


@app.route('/api/reports/window')
def api_report_window():
    """Everything collected in [start, end), filtered like the other pages."""
    try:
        return jsonify(reports.window_report(**_report_args()))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/reports/window/node/<name>')
def api_report_window_node(name):
    try:
        return jsonify(reports.node_series(
            name.lower(), request.args.get('start'), request.args.get('end')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


# --- Health score + what changed (health_score.py) ---
@app.route('/api/reports/health-score')
def api_health_score():
    """Per-bucket 0-100 scores for nodes (kind=node) or RF links (kind=link)
    in [start, end), filtered like the Reports tab; worst drops first."""
    try:
        return jsonify(health_score.scores(
            request.args.get('start'), request.args.get('end'),
            kind='link' if request.args.get('kind') == 'link' else 'node',
            scope=request.args.get('scope', 'all'),
            link_type=request.args.get('link_type', 'all'),
            search=request.args.get('search', '')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/reports/what-changed')
def api_what_changed():
    """Events, reboots, config/noise/route changes, starting-node outages,
    maintenance and notes around [start, end) for node (and optional peer)."""
    node = (request.args.get('node') or '').strip().lower()
    if not node:
        return jsonify({'error': 'node is required'}), 400
    peer = (request.args.get('peer') or '').strip().lower() or None
    try:
        return jsonify(health_score.what_changed(
            node, request.args.get('start'), request.args.get('end'), peer=peer,
            pad_minutes=request.args.get('pad_minutes', type=float)))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400
# --- end health score ---


@app.route('/api/reports/heatmap')
def api_report_heatmap():
    """Node x hour heatmap (heatmap.py): metric=reach|lq, fold=1 for hour of day."""
    try:
        return jsonify(heatmap.build(
            request.args.get('start'), request.args.get('end'),
            metric=request.args.get('metric', 'reach'),
            fold=request.args.get('fold') in ('1', 'true'),
            scope=request.args.get('scope', 'all'),
            link_type=request.args.get('link_type', 'all'),
            search=request.args.get('search', '')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/reports/swimlanes')
def api_report_swimlanes():
    """One lane per link: good/degraded/flapping/down per bucket, with reboot
    and config-change markers (swimlanes.py)."""
    try:
        return jsonify(swimlanes.build(
            request.args.get('start'), request.args.get('end'),
            scope=request.args.get('scope', 'all'),
            link_type=request.args.get('link_type', 'all'),
            search=request.args.get('search', '')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/reports/unreachable')
def api_report_unreachable():
    """Why each node with failed polls was unreachable (unreachable.py):
    start/end (local YYYY-MM-DDTHH:MM) or hours, plus scope/link_type/search."""
    try:
        return jsonify(reports.unreachable_report(
            start=request.args.get('start'), end=request.args.get('end'),
            hours=request.args.get('hours', 24, type=float),
            scope=request.args.get('scope', 'all'),
            link_type=request.args.get('link_type', 'all'),
            search=request.args.get('search', '')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/collector/health')
def api_collector_health():
    """Starting-node health (collector_health.py): start/end (local
    YYYY-MM-DDTHH:MM) or the last `hours` (default 24)."""
    try:
        start = request.args.get('start')
        end = request.args.get('end')
        if start or end:
            start_dt, end_dt = reports.parse_window(start, end)
        else:
            end_dt = datetime.now()
            hours = min(max(request.args.get('hours', 24, type=float), 0.25), 24 * 32)
            start_dt = end_dt - timedelta(hours=hours)
        return jsonify(collector_health.report(start_dt.strftime(reports.TS_FMT),
                                               end_dt.strftime(reports.TS_FMT)))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/collector/health/check', methods=['POST'])
def api_collector_health_check():
    """Measure the path to the starting node now and store it (kind 'manual')."""
    row = collector_health.record('manual')
    if row is None:
        return jsonify({'error': 'Measurement failed'}), 500
    return jsonify({'success': True, 'check': row})


# --- Weather (weather.py) ---
def _weather_window():
    """(start, end) local timestamps from ?start&end (YYYY-MM-DDTHH:MM) or ?hours."""
    start, end = request.args.get('start'), request.args.get('end')
    if start and end:
        try:
            s = datetime.strptime(start.replace(' ', 'T')[:16], '%Y-%m-%dT%H:%M')
            e = datetime.strptime(end.replace(' ', 'T')[:16], '%Y-%m-%dT%H:%M')
        except ValueError:
            raise ValueError('start and end must be YYYY-MM-DDTHH:MM')
        if e <= s or (e - s).days > 32:
            raise ValueError('end must be after start, at most 32 days')
        return s.strftime('%Y-%m-%d %H:%M:%S'), e.strftime('%Y-%m-%d %H:%M:%S')
    hours = min(max(1, request.args.get('hours', 72, type=int)), 24 * 32)
    e = datetime.now()
    return (e - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S'), e.strftime('%Y-%m-%d %H:%M:%S')


@app.route('/api/weather')
def api_weather():
    """Stored weather observations in a window plus station/poller status."""
    try:
        start, end = _weather_window()
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'status': weather.status(), 'start': start, 'end': end,
                    'observations': weather.observations(start, end)})


@app.route('/api/weather/correlation')
def api_weather_correlation():
    """Weather vs SNR for one link (?source&target) or one radio (?source)."""
    source = (request.args.get('source') or '').strip()
    if not source:
        return jsonify({'error': 'source is required'}), 400
    try:
        start, end = _weather_window() if request.args.get('start') else (None, None)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    hours = min(max(1, request.args.get('hours', 168, type=int)), 24 * 32)
    return jsonify(weather.correlation(source, request.args.get('target'), hours=hours, start=start, end=end))


@app.route('/api/weather/summary')
def api_weather_summary():
    """RF link directions ranked by rain fade, wind sensitivity and temperature."""
    try:
        start, end = _weather_window() if request.args.get('start') else (None, None)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    hours = min(max(1, request.args.get('hours', 168, type=int)), 24 * 32)
    return jsonify(weather.summary(hours=hours, start=start, end=end))


@app.route('/api/weather/refresh', methods=['POST'])
def api_weather_refresh():
    """Fetch observations from NWS now (backfills when nothing is stored)."""
    if not config.WEATHER_ENABLED:
        return jsonify({'error': 'Weather is disabled (WEATHER_ENABLED=0)'}), 409
    result = weather.fetch()
    if result.get('error'):
        return jsonify(result), 502
    return jsonify({'success': True, **result})
# --- end Weather ---


@app.route('/api/reports/window/link/<source>/<target>')
def api_report_window_link(source, target):
    try:
        return jsonify(reports.link_series(
            source.lower(), target.lower(), request.args.get('start'),
            request.args.get('end'), request.args.get('sample_type', 'all')))
    except reports.ReportError as exc:
        return jsonify({'error': str(exc)}), 400


# ============ Diagnostic Reports ============

@app.route('/api/reports/flaps')
def api_report_flaps():
    """Flap-rate report: transitions per link with the dominant LQM block reason."""
    hours = request.args.get('hours', 24, type=int)
    node = request.args.get('node', type=str)
    min_transitions = request.args.get('min_transitions', 1, type=int)
    if node:
        node = node.lower()
    report = database.get_link_flap_report(
        hours=hours, node=node, min_transitions=min_transitions
    )
    return jsonify({'hours': hours, 'node': node, 'links': report})


@app.route('/api/reports/asymmetry')
def api_report_asymmetry():
    """Links whose two directions disagree on signal (asymmetry report)."""
    min_delta = request.args.get('min_delta', 3.0, type=float)
    return jsonify({'min_delta': min_delta, 'links': database.get_link_asymmetry_report(min_delta=min_delta)})


@app.route('/api/reports/mesh-health')
def api_report_mesh_health():
    """Network-wide health check: near-far, SNR/rate imbalance, loss, VoIP, etc.

    Scoped to nodes reachable from the scan seed without crossing a scan boundary.
    """
    hours = request.args.get('hours', 24, type=int)
    try:
        return jsonify(mesh_diagnostics.run_health_check(hours=hours))
    except Exception as e:
        logger.exception("Mesh health check failed")
        return jsonify({'error': str(e)}), 500


# ============ Scan Boundaries ============

@app.route('/api/scan-boundaries')
def api_get_scan_boundaries():
    """Nodes that stop or exclude BFS discovery (keeps remote meshes out)."""
    return jsonify({'boundaries': database.get_scan_boundaries()})


@app.route('/api/scan-boundaries/<name>', methods=['POST'])
def api_set_scan_boundary(name):
    """Set a node's boundary mode: 'exclude', 'stop', or null/'' to clear."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode') or None
    try:
        result = database.set_scan_boundary(name, mode)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    logger.info(f"Scan boundary for {name.lower()} set to {result}")
    return jsonify({
        'success': True,
        'node': name.lower(),
        'mode': result,
        'boundaries': database.get_scan_boundaries()
    })


# ============ Cleanup (stale and junk nodes) ============

@app.route('/api/cleanup/candidates')
def api_cleanup_candidates():
    """Junk, stale and known-unreachable nodes, grouped by reason."""
    stale_days = max(1, request.args.get('stale_days', cleanup.STALE_DAYS, type=int))
    hours = min(max(1, request.args.get('hours', cleanup.WINDOW_HOURS, type=int)), 24 * 31)
    return jsonify(cleanup.candidates(stale_days=stale_days, hours=hours))


def _cleanup_names():
    data = request.get_json(silent=True) or {}
    names = data.get('names')
    if not isinstance(names, list) or not names:
        return None, bool(data.get('force'))
    return names, bool(data.get('force'))


@app.route('/api/cleanup/exclude', methods=['POST'])
def api_cleanup_exclude():
    """Exclude nodes from scanning: {names: [...], force?}."""
    names, force = _cleanup_names()
    if names is None:
        return jsonify({'error': 'names must be a non-empty list'}), 400
    return jsonify({'success': True, **cleanup.exclude(names, force=force)})


@app.route('/api/cleanup/unexclude', methods=['POST'])
def api_cleanup_unexclude():
    """Remove the exclude scan boundary from nodes: {names: [...]}."""
    names, _ = _cleanup_names()
    if names is None:
        return jsonify({'error': 'names must be a non-empty list'}), 400
    return jsonify({'success': True, **cleanup.unexclude(names)})


@app.route('/api/cleanup/delete', methods=['POST'])
def api_cleanup_delete():
    """Delete node records and their history: {names: [...], force?}."""
    names, force = _cleanup_names()
    if names is None:
        return jsonify({'error': 'names must be a non-empty list'}), 400
    result = cleanup.delete(names, force=force)
    if result['deleted']:
        socketio.emit('network_update', database.get_network_graph_data())
    return jsonify({'success': True, **result})


@app.route('/api/reports/link-state/<source>/<target>')
def api_report_link_state(source, target):
    """Raw state-change log for a single directional link."""
    hours = request.args.get('hours', 24, type=int)
    log = database.get_link_state_log(source.lower(), target.lower(), hours=hours)
    return jsonify(log)


@app.route('/api/node/<name>/health')
def api_node_health(name):
    """Node health time series (uptime/load/memory) for charts and reports."""
    hours = request.args.get('hours', 24, type=int)
    return jsonify(database.get_node_health_history(name.lower(), hours=hours))


@app.route('/api/reports/incident/<name>')
def api_report_incident(name):
    """Problem report for a node: deterministic findings + optional AI summary."""
    hours = request.args.get('hours', 24, type=int)
    report = incident_report.build_report(name.lower(), hours=hours)
    return jsonify(report)


@app.route('/api/troubleshoot/<name>', methods=['POST'])
def api_troubleshoot(name):
    """Actively troubleshoot a node: enumerate problems and run live probes."""
    hours = request.args.get('hours', 24, type=int)
    result = troubleshoot.troubleshoot_node(name.lower(), hours=hours)
    if result.get('error'):
        return jsonify(result), 404
    return jsonify({'success': True, 'result': result})


@app.route('/api/voip/endpoints')
def api_voip_endpoints():
    """List VOIP endpoints (phones/PBX) for the Call-Quality view."""
    return jsonify(voip.list_endpoints())


@app.route('/api/voip/ping-all', methods=['POST'])
def api_voip_ping_all():
    """Ping every VOIP endpoint device from the collector for a reachability sweep."""
    try:
        results = voip.ping_all_endpoints()
    except rf_stats.GatewayUnreachable:
        return gateway_error_response()
    response = {'results': results}
    if any(r.get('aborted') for r in results):
        response['error'] = gateway.unreachable_message()
        response['gateway_unreachable'] = True
    return jsonify(response)


@app.route('/api/voip/call-quality/<source>/<target>', methods=['POST'])
def api_voip_call_quality(source, target):
    """Run live end-to-end VOIP diagnostics between two endpoints."""
    codec = request.args.get('codec')
    result = voip.run_call_quality(source.lower(), target.lower(), codec=codec)
    if result.get('gateway_unreachable'):
        return jsonify(result), 503
    if result.get('error'):
        return jsonify(result), 404
    return jsonify({'success': True, 'result': result})


@app.route('/api/voip/last/<source>/<target>')
def api_voip_last(source, target):
    """Return the cached last VOIP test for a pair+codec (instant repaint)."""
    codec = voip._resolve_codec(request.args.get('codec'))
    cached = database.get_voip_test(source.lower(), target.lower(), codec)
    return jsonify({'result': cached})


# ---- Streamed VoIP sessions (two-ended / one-ended) ----

@app.route('/api/voip/session/start', methods=['POST'])
def api_voip_session_start():
    """Start a streamed VoIP session (two-ended with agent, or one-ended)."""
    data = request.get_json(silent=True) or {}
    mode = (data.get('mode') or 'full').strip().lower()
    if mode not in ('full', 'one_ended'):
        return jsonify({'error': 'mode must be "full" or "one_ended"'}), 400
    remote_host = (data.get('remote_host') or '').strip()
    if not remote_host:
        return jsonify({'error': 'remote_host is required'}), 400
    source_label = (data.get('source') or 'collector').strip()
    target_label = (data.get('target') or remote_host).strip()
    agent_key = (data.get('agent_key') or '').strip()
    pps = data.get('pps', config.VOIP_STREAM_PPS)
    packet_bytes = data.get('packet_bytes', config.VOIP_STREAM_PACKET_BYTES)
    source_node_ip = (data.get('source_node_ip') or '').strip() or None

    if mode == 'full' and not agent_key:
        return jsonify({'error': 'agent_key is required for a two-ended session'}), 400

    session, err = voip_session.start_session(
        mode, source_label, target_label, remote_host,
        agent_key=agent_key, pps=pps, packet_bytes=packet_bytes,
        source_node_ip=source_node_ip)
    if err:
        return jsonify({'error': err}), 409
    return jsonify({'success': True, 'session': session.snapshot()})


@app.route('/api/voip/session/stop', methods=['POST'])
def api_voip_session_stop():
    sid = voip_session.stop_session()
    if sid is None:
        return jsonify({'error': 'no session running'}), 404
    return jsonify({'success': True, 'session_id': sid})


@app.route('/api/voip/session/status')
def api_voip_session_status():
    s = voip_session.current_session()
    if not s:
        return jsonify({'running': False})
    return jsonify(s.snapshot())


@app.route('/api/voip/session/event', methods=['POST'])
def api_voip_session_event():
    s = voip_session.current_session()
    if not s or s.done:
        return jsonify({'error': 'no session running'}), 404
    data = request.get_json(silent=True) or {}
    tag = (data.get('tag') or '').strip().upper()
    note = (data.get('note') or '').strip()
    if not tag:
        return jsonify({'error': 'tag required'}), 400
    s.mark_event(tag, note)
    return jsonify({'success': True})


@app.route('/api/voip/session/refresh-routes', methods=['POST'])
def api_voip_session_refresh_routes():
    s = voip_session.current_session()
    if not s or s.done:
        return jsonify({'error': 'no session running'}), 404
    s.force_route.set()
    return jsonify({'success': True})


@app.route('/api/voip/sessions')
def api_voip_sessions():
    return jsonify({'sessions': database.list_voip_sessions()})


@app.route('/api/voip/session/<int:session_id>')
def api_voip_session_detail(session_id):
    s = database.get_voip_session(session_id)
    if not s:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'session': s,
                    'samples': database.get_voip_samples(session_id),
                    'events': database.get_voip_events(session_id),
                    'routes': database.get_voip_routes(session_id)})


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
    if not rf_stats.ping_ok(result) and gateway.failure_is_ours():
        return gateway_error_response()

    if result:
        return jsonify({
            'success': result.get('loss', 100) < 100,
            'node': node_name,
            'ip': node['ip'],
            'time': result.get('avg'),
            'loss': result.get('loss', 100)
        })

    return jsonify({'error': 'Ping failed'}), 500


@app.route('/api/traceroute/<node_name>', methods=['POST'])
def api_traceroute_node(node_name):
    """Traceroute FROM the scanner (this host) TO a node - used by the node panel."""
    node_name = node_name.lower()
    node = database.get_node(node_name)
    if not node:
        return jsonify({'error': f'Node "{node_name}" not found'}), 404

    target = node.get('ip') or node_name
    result = rf_stats.traceroute_local(target)
    if result:
        result['node'] = node_name
        return jsonify({'success': True, 'result': result})
    if gateway.failure_is_ours():
        return gateway_error_response()
    return jsonify({'error': 'Traceroute failed or target unreachable'}), 500


@app.route('/api/rf-stats/test/<source>/<target>', methods=['POST'])
def api_trigger_rf_test(source, target):
    """Manually trigger a ping or iperf test for a specific link"""
    test_type = request.args.get('type', 'ping')

    # Normalize node names to lowercase (database stores them in lowercase)
    source = source.lower()
    target = target.lower()

    if not gateway.is_reachable():
        return gateway_error_response()

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

        if not rf_stats.ping_ok(result) and gateway.failure_is_ours():
            return gateway_error_response()
        if result:
            database.update_link_history_ping(
                source, target,
                result.get('min'), result.get('avg'),
                result.get('max'), result.get('loss'),
                jitter=result.get('jitter')
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

        if result or not gateway.failure_is_ours():
            capacity.record(source, target, result, kind='manual')
        if result:
            database.update_link_history_throughput(
                source, target,
                result.get('tx_mbps'), result.get('rx_mbps')
            )
            return jsonify({'success': True, 'result': result})
        if gateway.failure_is_ours():
            return gateway_error_response()
        return jsonify({'error': 'iPerf test failed - target may not be reachable'}), 500

    elif test_type == 'traceroute':
        # Run the trace FROM the source node (arbitrary vantage point) toward the
        # target, using the source node's own traceroute CGI.
        source_node = database.get_node(source)
        source_ip = source_node.get('ip') if source_node else None
        result = rf_stats.traceroute_via_aredn(target, source_node_ip=source_ip)
        if result:
            return jsonify({'success': True, 'result': result})
        if gateway.failure_is_ours():
            return gateway_error_response()
        return jsonify({'error': 'Traceroute failed - source node unreachable or target invalid'}), 500

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
            if not ping_data['success'] and gateway.failure_is_ours():
                ping_data['gateway_unreachable'] = True
                ping_data['error'] = gateway.unreachable_message()
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


# ============ Notifications (#21) ============
# Secrets are masked in every response; see notify.py.

@app.route('/api/notifications/settings', methods=['GET'])
def api_notifications_settings():
    return jsonify({
        'config': notify.masked_config(),
        'status': notify.status(),
        'event_types': notify.EVENT_TYPES,
        'severities': list(notify.SEVERITY_RANK.keys()),
        'scopes': notify.SCOPES,
        'channels': notify.CHANNELS,
    })


@app.route('/api/notifications/settings', methods=['POST'])
def api_notifications_update_settings():
    try:
        cfg = notify.update_config(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'config': cfg, 'status': notify.status()})


@app.route('/api/notifications/rules', methods=['GET'])
def api_notifications_rules():
    return jsonify({'rules': notify.get_rules()})


@app.route('/api/notifications/rules', methods=['POST'])
def api_notifications_save_rule():
    try:
        rule = notify.save_rule(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    return jsonify({'success': True, 'rule': rule})


@app.route('/api/notifications/rules', methods=['DELETE'])
@app.route('/api/notifications/rules/<int:rule_id>', methods=['DELETE'])
def api_notifications_delete_rule(rule_id=None):
    rule_id = rule_id or request.args.get('id', type=int) or (request.get_json(silent=True) or {}).get('id')
    if not rule_id:
        return jsonify({'error': 'Rule id required'}), 400
    if not notify.delete_rule(rule_id):
        return jsonify({'error': 'Rule not found'}), 404
    return jsonify({'success': True})


@app.route('/api/notifications/test', methods=['POST'])
def api_notifications_test():
    channel = (request.get_json(silent=True) or {}).get('channel')
    if channel not in notify.CHANNELS:
        return jsonify({'error': 'Unknown channel'}), 400
    result = notify.send_test(channel)
    return jsonify(result), (200 if result.get('success') else 502)


@app.route('/api/notifications/log')
def api_notifications_log():
    return jsonify({'log': notify.get_log(request.args.get('limit', 100, type=int))})

# ============ End notifications ============


def cleanup_history_task():
    """Background task for cleaning up old history"""
    rf_stats.cleanup_old_history()
    capacity.cleanup()


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

    if config.ROUTE_WATCH_ENABLED:
        scheduler.add_job(
            run_route_watch_task,
            'interval',
            minutes=config.ROUTE_WATCH_INTERVAL_MINUTES,
            id='route_watch',
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=2)
        )
    # NWS weather observations (background; failures never touch scans)
    weather.install(scheduler)
    # Overnight iPerf on key links (capacity.py) and the weekly report.
    capacity.install(scheduler, run_iperf_schedule_task)
    weekly_report.install(scheduler, run_weekly_report_task)

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

        # Notifications: listen to logged events (sends only when enabled)
        notify.install(socketio.start_background_task)

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

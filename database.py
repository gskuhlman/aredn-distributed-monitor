"""
Database module for AREDN Network Monitor
SQLite database setup and CRUD operations
"""

import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
import config


def local_timestamp():
    """Return current local timestamp as string"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _parse_local_timestamp(timestamp):
    """Parse timestamps written by this application."""
    try:
        return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def get_db_path():
    return config.DATABASE_PATH


@contextmanager
def get_connection():
    """Context manager for database connections"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize database tables"""
    with get_connection() as conn:
        cursor = conn.cursor()

        # Nodes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                ip TEXT,
                description TEXT,
                model TEXT,
                firmware_version TEXT,
                lat REAL,
                lon REAL,
                rf_frequency TEXT,
                rf_channel TEXT,
                first_seen DATETIME,
                last_seen DATETIME,
                is_active BOOLEAN DEFAULT 1,
                is_supernode BOOLEAN DEFAULT 0
            )
        ''')

        # Add rf_frequency column if it doesn't exist (migration)
        try:
            cursor.execute('ALTER TABLE nodes ADD COLUMN rf_frequency TEXT')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE nodes ADD COLUMN rf_channel TEXT')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE nodes ADD COLUMN is_supernode BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Links table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node TEXT NOT NULL,
                target_node TEXT NOT NULL,
                link_type TEXT NOT NULL,
                quality INTEGER DEFAULT 0,
                snr INTEGER,
                distance INTEGER,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                stable_since DATETIME DEFAULT CURRENT_TIMESTAMP,
                drop_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'good',
                UNIQUE(source_node, target_node)
            )
        ''')

        # Services table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                name TEXT NOT NULL,
                protocol TEXT,
                link TEXT,
                ip TEXT,
                UNIQUE(node_name, name, ip)
            )
        ''')

        # Settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS selected_nodes (
                node_name TEXT PRIMARY KEY,
                selected_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Saved graph layout (node positions from the vis.js network map)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS node_positions (
                node_name TEXT PRIMARY KEY,
                x REAL NOT NULL,
                y REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Events table for logging
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                node_name TEXT,
                details TEXT,
                severity TEXT DEFAULT 'info'
            )
        ''')

        # Link history table for RF statistics over time
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS link_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                source_node TEXT NOT NULL,
                target_node TEXT NOT NULL,
                link_type TEXT NOT NULL,
                quality INTEGER,
                snr INTEGER,
                ping_min REAL,
                ping_avg REAL,
                ping_max REAL,
                ping_loss REAL,
                throughput_tx REAL,
                throughput_rx REAL
            )
        ''')

        # Create indexes for performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_node)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_node)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_services_node ON services(node_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_link_history_link ON link_history(source_node, target_node, timestamp DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_link_history_timestamp ON link_history(timestamp DESC)')

        for column, definition in (
            ('mac_address', 'TEXT'),
            ('canonical_ip', 'TEXT'),
            ('identity_status', 'TEXT'),
            ('routability_status', 'TEXT'),
            ('lqm_status_message', 'TEXT'),
            ('signal', 'TEXT'),
            ('noise', 'TEXT'),
            ('tx_rate', 'TEXT'),
            ('rx_rate', 'TEXT')
        ):
            try:
                cursor.execute(f'ALTER TABLE links ADD COLUMN {column} {definition}')
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Add jitter column to link_history
        try:
            cursor.execute('ALTER TABLE link_history ADD COLUMN jitter REAL')
        except sqlite3.OperationalError:
            pass  # Column already exists


def delete_node(name):
    """Delete a node and its link/service/history state."""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Delete services
        cursor.execute('DELETE FROM services WHERE node_name = ?', (name,))
        # Delete links (both directions)
        cursor.execute('DELETE FROM links WHERE source_node = ? OR target_node = ?', (name, name))
        # Delete link history (both directions)
        cursor.execute('DELETE FROM link_history WHERE source_node = ? OR target_node = ?', (name, name))
        # Delete node
        cursor.execute('DELETE FROM nodes WHERE name = ?', (name,))
        deleted = cursor.rowcount
        cursor.execute('DELETE FROM selected_nodes WHERE node_name = ?', (name,))
        return deleted


def prune_old_nodes(days):
    """Delete nodes and related local state when they exceed database retention."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM nodes WHERE last_seen < ?', (cutoff,))
        names = [row['name'] for row in cursor.fetchall()]
        if not names:
            return 0

        placeholders = ','.join('?' * len(names))
        cursor.execute(f'DELETE FROM services WHERE node_name IN ({placeholders})', names)
        cursor.execute(
            f'DELETE FROM links WHERE source_node IN ({placeholders}) OR target_node IN ({placeholders})',
            names + names
        )
        cursor.execute(
            f'DELETE FROM link_history WHERE source_node IN ({placeholders}) OR target_node IN ({placeholders})',
            names + names
        )
        cursor.execute(f'DELETE FROM nodes WHERE name IN ({placeholders})', names)
        cursor.execute(f'DELETE FROM selected_nodes WHERE node_name IN ({placeholders})', names)
        return len(names)


def get_node_history(name, hours=24):
    """Get link history for a node (as source or target) within time range"""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM link_history
            WHERE (source_node = ? OR target_node = ?)
            AND timestamp > ?
            ORDER BY timestamp ASC
        ''', (name, name, cutoff))
        return [dict(row) for row in cursor.fetchall()]


def get_node_ping_history(name, hours=24):
    """Get ping history for links involving this node (where ping_avg is not null)"""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, source_node, target_node, link_type,
                   ping_min, ping_avg, ping_max, ping_loss, jitter
            FROM link_history
            WHERE (source_node = ? OR target_node = ?)
            AND timestamp > ?
            AND ping_avg IS NOT NULL
            ORDER BY timestamp ASC
        ''', (name, name, cutoff))
        return [dict(row) for row in cursor.fetchall()]


def get_node_connectivity_log(name, limit=200):
    """Get events related to a node"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE node_name = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (name, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_node_all_links(name):
    """Get all links (including dropped/removed) for a node"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM links
            WHERE (source_node = ? OR target_node = ?)
            ORDER BY last_seen DESC
        ''', (name, name))
        return [dict(row) for row in cursor.fetchall()]


def get_node_observed_events(name, limit=200):
    """Get events where a node was the event subject or mentioned in link details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE node_name = ?
            OR details LIKE ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (name, f'%{name}%', limit))
        return [dict(row) for row in cursor.fetchall()]


def build_link_only_node(name):
    """Build a synthetic node record from link/event evidence."""
    links = get_node_all_links(name)
    events = get_node_observed_events(name, limit=1)
    if not links and not events:
        return None

    first_seen_values = [link.get('first_seen') for link in links if link.get('first_seen')]
    first_seen_values.extend(event.get('timestamp') for event in events if event.get('timestamp'))
    last_seen_values = [link.get('last_seen') for link in links if link.get('last_seen')]
    last_seen_values.extend(event.get('timestamp') for event in events if event.get('timestamp'))
    active_links = [link for link in links if link.get('status') != 'removed']
    reporters = sorted({
        link['source_node'] if link['target_node'] == name else link['target_node']
        for link in links
    })
    mac_addresses = sorted({link.get('mac_address') for link in links if link.get('mac_address')})
    canonical_ips = sorted({link.get('canonical_ip') for link in links if link.get('canonical_ip')})
    identity_values = {link.get('identity_status') for link in links if link.get('identity_status')}
    identity_status = 'mac_only' if 'mac_only' in identity_values else 'lqm_only'
    routability_values = {link.get('routability_status') for link in links if link.get('routability_status')}
    if 'routable' in routability_values:
        routability_status = 'routable'
    elif 'not_routable' in routability_values:
        routability_status = 'not_routable'
    else:
        routability_status = 'unknown'

    if not active_links:
        lqm_status_message = 'Stale LQM entry'
    elif routability_status == 'not_routable':
        lqm_status_message = 'Seen by LQM, not currently routable'
    elif any(ip and ':' in ip for ip in canonical_ips):
        lqm_status_message = 'IPv6 link-local only'
    elif not canonical_ips:
        lqm_status_message = 'Awaiting host/IP mapping'
    elif identity_status == 'mac_only':
        lqm_status_message = 'MAC-only neighbor'
    else:
        lqm_status_message = 'LQM-only neighbor'

    return {
        'name': name,
        'ip': None,
        'description': 'Seen in link data, but not successfully polled by this collector',
        'model': None,
        'firmware_version': None,
        'lat': None,
        'lon': None,
        'rf_frequency': None,
        'rf_channel': None,
        'first_seen': min(first_seen_values) if first_seen_values else None,
        'last_seen': max(last_seen_values) if last_seen_values else None,
        'is_active': 0,
        'is_supernode': 0,
        'is_link_only': True,
        'observed_status': 'link-only' if active_links else 'removed',
        'identity_status': identity_status,
        'routability_status': routability_status,
        'lqm_status_message': lqm_status_message,
        'mac_addresses': mac_addresses,
        'canonical_ips': canonical_ips,
        'reporters': reporters,
        'links_count': len(links),
        'active_links_count': len(active_links),
        'services_list': []
    }


def get_observed_node(name):
    """Get a real node or a synthetic link-only node."""
    node = get_node(name)
    if node:
        node['is_link_only'] = False
        node['observed_status'] = 'active' if node.get('is_active') == 1 else 'inactive'
        node['is_selected'] = is_node_selected(name)
        return node
    link_only = build_link_only_node(name)
    if link_only:
        link_only['is_selected'] = is_node_selected(name)
    return link_only


def get_all_observed_nodes():
    """Get all polled nodes plus link-only endpoint names ever seen in links."""
    nodes = get_all_nodes()
    node_names = {node['name'] for node in nodes}
    observed = []

    for node in nodes:
        item = dict(node)
        item['is_link_only'] = False
        item['observed_status'] = 'active' if item.get('is_active') == 1 else 'inactive'
        item['is_selected'] = is_node_selected(item['name'])
        item['links_count'] = len(get_node_all_links(item['name']))
        item['active_links_count'] = len(get_node_links(item['name']))
        item['services_list'] = get_node_services(item['name'])
        observed.append(item)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT source_node AS name FROM links
            UNION
            SELECT target_node AS name FROM links
            ORDER BY name
        ''')
        link_names = [row['name'] for row in cursor.fetchall()]

    for name in link_names:
        if name in node_names:
            continue
        link_only = build_link_only_node(name)
        if link_only:
            link_only['is_selected'] = is_node_selected(name)
            observed.append(link_only)

    return sorted(observed, key=lambda item: item.get('name') or '')


# ============ Node Operations ============

def upsert_node(name, ip=None, description=None, model=None,
                firmware_version=None, lat=None, lon=None,
                rf_frequency=None, rf_channel=None, is_supernode=False):
    """Insert or update a node"""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO nodes (name, ip, description, model, firmware_version, lat, lon,
                             rf_frequency, rf_channel, first_seen, last_seen, is_active, is_supernode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET
                ip = COALESCE(excluded.ip, ip),
                description = COALESCE(excluded.description, description),
                model = COALESCE(excluded.model, model),
                firmware_version = COALESCE(excluded.firmware_version, firmware_version),
                lat = COALESCE(excluded.lat, lat),
                lon = COALESCE(excluded.lon, lon),
                rf_frequency = COALESCE(excluded.rf_frequency, rf_frequency),
                rf_channel = COALESCE(excluded.rf_channel, rf_channel),
                last_seen = ?,
                is_active = 1,
                is_supernode = excluded.is_supernode
        ''', (name, ip, description, model, firmware_version, lat, lon,
              rf_frequency, rf_channel, now, now, is_supernode, now))


def get_node(name):
    """Get a single node by name"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM nodes WHERE name = ?', (name,))
        row = cursor.fetchone()
        return dict(row) if row else None


def has_recent_node_event(name, days):
    """Return whether this node has any retained event within the recent-node window."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1 FROM events
            WHERE node_name = ?
            AND timestamp >= ?
            LIMIT 1
        ''', (name, cutoff))
        return cursor.fetchone() is not None


def get_all_nodes():
    """Get all nodes"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM nodes ORDER BY name')
        return [dict(row) for row in cursor.fetchall()]


def get_active_nodes():
    """Get all active nodes"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM nodes WHERE is_active = 1 ORDER BY name')
        return [dict(row) for row in cursor.fetchall()]


def mark_node_inactive(name):
    """Mark a node as inactive"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE nodes SET is_active = 0 WHERE name = ?', (name,))


def get_nodes_to_mark_inactive(timeout_seconds):
    """Get nodes that will be marked as inactive (for notifications)"""
    cutoff_str = (datetime.now() - timedelta(seconds=timeout_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT name, ip FROM nodes
            WHERE last_seen < ?
            AND is_active = 1
        ''', (cutoff_str,))
        return [{'name': row['name'], 'ip': row['ip']} for row in cursor.fetchall()]


def mark_stale_nodes_inactive(timeout_seconds):
    """Mark nodes not seen within timeout as inactive"""
    cutoff_str = (datetime.now() - timedelta(seconds=timeout_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE nodes
            SET is_active = 0
            WHERE last_seen < ?
            AND is_active = 1
        ''', (cutoff_str,))
        return cursor.rowcount


def get_orphan_nodes():
    """Get active nodes that have no active links (status != 'removed')"""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Find active nodes that don't appear in any non-removed link
        cursor.execute('''
            SELECT n.name, n.ip FROM nodes n
            WHERE n.is_active = 1
            AND NOT EXISTS (
                SELECT 1 FROM links l
                WHERE (l.source_node = n.name OR l.target_node = n.name)
                AND l.status != 'removed'
            )
        ''')
        return [{'name': row['name'], 'ip': row['ip']} for row in cursor.fetchall()]


def mark_orphan_nodes_inactive():
    """Mark active nodes with no active links as inactive"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE nodes
            SET is_active = 0
            WHERE is_active = 1
            AND NOT EXISTS (
                SELECT 1 FROM links l
                WHERE (l.source_node = nodes.name OR l.target_node = nodes.name)
                AND l.status != 'removed'
            )
        ''')
        return cursor.rowcount


# ============ Link Operations ============

def upsert_link(source_node, target_node, link_type, quality=0, snr=None, distance=None,
                mac_address=None, canonical_ip=None, identity_status=None,
                routability_status=None, lqm_status_message=None,
                signal=None, noise=None, tx_rate=None, rx_rate=None):
    """Insert or update a link"""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()

        # Check if link exists and was previously dropped
        cursor.execute('''
            SELECT status, drop_count FROM links
            WHERE source_node = ? AND target_node = ?
        ''', (source_node, target_node))
        existing = cursor.fetchone()

        if existing and existing['status'] in ('dropped', 'removed'):
            # Link was dropped/removed, now back - increment drop_count, reset stable_since
            cursor.execute('''
                UPDATE links SET
                    link_type = ?,
                    quality = ?,
                    snr = ?,
                    distance = ?,
                    mac_address = ?,
                    canonical_ip = ?,
                    identity_status = ?,
                    routability_status = ?,
                    lqm_status_message = ?,
                    signal = ?,
                    noise = ?,
                    tx_rate = ?,
                    rx_rate = ?,
                    last_seen = ?,
                    stable_since = ?,
                    drop_count = drop_count + 1,
                    status = 'good'
                WHERE source_node = ? AND target_node = ?
            ''', (link_type, quality, snr, distance, mac_address, canonical_ip,
                  identity_status, routability_status, lqm_status_message,
                  signal, noise, tx_rate, rx_rate,
                  now, now, source_node, target_node))
        else:
            # Normal upsert
            cursor.execute('''
                INSERT INTO links (source_node, target_node, link_type, quality, snr, distance,
                                 mac_address, canonical_ip, identity_status, routability_status,
                                 lqm_status_message, signal, noise, tx_rate, rx_rate,
                                 first_seen, last_seen, stable_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_node, target_node) DO UPDATE SET
                    link_type = excluded.link_type,
                    quality = excluded.quality,
                    snr = COALESCE(excluded.snr, snr),
                    distance = COALESCE(excluded.distance, distance),
                    mac_address = COALESCE(excluded.mac_address, mac_address),
                    canonical_ip = COALESCE(excluded.canonical_ip, canonical_ip),
                    identity_status = COALESCE(excluded.identity_status, identity_status),
                    routability_status = COALESCE(excluded.routability_status, routability_status),
                    lqm_status_message = COALESCE(excluded.lqm_status_message, lqm_status_message),
                    signal = COALESCE(excluded.signal, signal),
                    noise = COALESCE(excluded.noise, noise),
                    tx_rate = COALESCE(excluded.tx_rate, tx_rate),
                    rx_rate = COALESCE(excluded.rx_rate, rx_rate),
                    last_seen = ?,
                    status = 'good'
            ''', (source_node, target_node, link_type, quality, snr, distance,
                  mac_address, canonical_ip, identity_status, routability_status,
                  lqm_status_message, signal, noise, tx_rate, rx_rate,
                  now, now, now, now))


def get_link(source_node, target_node):
    """Get a specific link"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM links
            WHERE source_node = ? AND target_node = ?
        ''', (source_node, target_node))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_links():
    """Get all links"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM links ORDER BY source_node, target_node')
        return [dict(row) for row in cursor.fetchall()]


def get_active_links():
    """Get links that are not removed (status != 'removed')"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM links
            WHERE status != 'removed'
            ORDER BY source_node, target_node
        ''')
        return [dict(row) for row in cursor.fetchall()]


def get_node_links(node_name):
    """Get active links for a specific node (as source or target), excluding removed links"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM links
            WHERE (source_node = ? OR target_node = ?)
            AND status != 'removed'
        ''', (node_name, node_name))
        return [dict(row) for row in cursor.fetchall()]


def get_links_to_drop(timeout_seconds):
    """Get links that will be marked as dropped (for notifications)"""
    cutoff_str = (datetime.now() - timedelta(seconds=timeout_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT source_node, target_node, link_type FROM links
            WHERE last_seen < ?
            AND status != 'dropped' AND status != 'removed'
        ''', (cutoff_str,))
        return [{'source': row['source_node'], 'target': row['target_node'],
                 'type': row['link_type']} for row in cursor.fetchall()]


def get_missing_source_links(source_node, current_targets):
    """Get active links from a scanned source node that were absent in this scan."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if current_targets:
            placeholders = ','.join('?' * len(current_targets))
            cursor.execute(f'''
                SELECT source_node, target_node, link_type FROM links
                WHERE source_node = ?
                AND status != 'dropped' AND status != 'removed'
                AND target_node NOT IN ({placeholders})
            ''', (source_node, *current_targets))
        else:
            cursor.execute('''
                SELECT source_node, target_node, link_type FROM links
                WHERE source_node = ?
                AND status != 'dropped' AND status != 'removed'
            ''', (source_node,))

        return [{'source': row['source_node'], 'target': row['target_node'],
                 'type': row['link_type']} for row in cursor.fetchall()]


def mark_missing_source_links_dropped(source_node, current_targets):
    """Immediately mark links from a scanned source node as dropped when absent."""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        if current_targets:
            placeholders = ','.join('?' * len(current_targets))
            cursor.execute(f'''
                UPDATE links
                SET status = 'dropped',
                    last_seen = ?
                WHERE source_node = ?
                AND status != 'dropped' AND status != 'removed'
                AND target_node NOT IN ({placeholders})
            ''', (now, source_node, *current_targets))
        else:
            cursor.execute('''
                UPDATE links
                SET status = 'dropped',
                    last_seen = ?
                WHERE source_node = ?
                AND status != 'dropped' AND status != 'removed'
            ''', (now, source_node))
        return cursor.rowcount


def mark_link_dropped(source_node, target_node):
    """Mark a specific link as dropped and start its dropped retention timer."""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE links
            SET status = 'dropped',
                last_seen = ?
            WHERE source_node = ?
            AND target_node = ?
            AND status != 'dropped' AND status != 'removed'
        ''', (now, source_node, target_node))
        return cursor.rowcount


def mark_stale_links_dropped(timeout_seconds):
    """Mark links not seen within timeout as dropped"""
    cutoff_str = (datetime.now() - timedelta(seconds=timeout_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE links
            SET status = 'dropped'
            WHERE last_seen < ?
            AND status != 'dropped' AND status != 'removed'
        ''', (cutoff_str,))
        return cursor.rowcount


def remove_old_dropped_links(remove_after_seconds):
    """Remove links that have been dropped for too long"""
    cutoff_str = (datetime.now() - timedelta(seconds=remove_after_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE links
            SET status = 'removed'
            WHERE last_seen < ?
            AND status = 'dropped'
        ''', (cutoff_str,))
        return cursor.rowcount


def get_links_to_remove(remove_after_seconds):
    """Get dropped links that will be removed from display."""
    cutoff_str = (datetime.now() - timedelta(seconds=remove_after_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT source_node, target_node, link_type FROM links
            WHERE last_seen < ?
            AND status = 'dropped'
        ''', (cutoff_str,))
        return [{'source': row['source_node'], 'target': row['target_node'],
                 'type': row['link_type']} for row in cursor.fetchall()]


def update_link_status(link_id, status):
    """Update a link's status"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE links SET status = ? WHERE id = ?', (status, link_id))


# ============ Service Operations ============

def upsert_service(node_name, name, protocol=None, link=None, ip=None):
    """Insert or update a service"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO services (node_name, name, protocol, link, ip)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_name, name, ip) DO UPDATE SET
                protocol = COALESCE(excluded.protocol, protocol),
                link = COALESCE(excluded.link, link)
        ''', (node_name, name, protocol, link, ip))


def get_node_services(node_name):
    """Get all services for a node"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM services WHERE node_name = ?', (node_name,))
        return [dict(row) for row in cursor.fetchall()]


def get_all_services():
    """Get all services"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM services ORDER BY node_name, name')
        return [dict(row) for row in cursor.fetchall()]


def clear_node_services(node_name):
    """Remove all services for a node (before re-adding current ones)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM services WHERE node_name = ?', (node_name,))


# ============ Settings Operations ============

def get_setting(key, default=None):
    """Get a setting value"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row['value'] if row else default


def set_setting(key, value):
    """Set a setting value"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''', (key, value))


def get_all_settings():
    """Get all settings as a dictionary"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM settings')
        return {row['key']: row['value'] for row in cursor.fetchall()}


# ============ Selected Node Operations ============

def get_selected_node_names():
    """Get node names included in selected-node scan/display mode."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT node_name FROM selected_nodes ORDER BY node_name')
        return [row['node_name'] for row in cursor.fetchall()]


def is_node_selected(name):
    """Return whether a node is included in selected-node mode."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM selected_nodes WHERE node_name = ?', (name,))
        return cursor.fetchone() is not None


def set_node_selected(name, selected):
    """Include or exclude a node from selected-node mode."""
    node_name = (name or '').lower()
    with get_connection() as conn:
        cursor = conn.cursor()
        if selected:
            cursor.execute('''
                INSERT INTO selected_nodes (node_name, selected_at)
                VALUES (?, ?)
                ON CONFLICT(node_name) DO UPDATE SET selected_at = excluded.selected_at
            ''', (node_name, local_timestamp()))
        else:
            cursor.execute('DELETE FROM selected_nodes WHERE node_name = ?', (node_name,))
    return is_node_selected(node_name)


# ============ Node Layout Operations ============

def save_node_positions(positions):
    """Upsert saved graph positions from a {node_name: {x, y}} mapping.

    Positions for nodes not in the mapping are preserved, so saving while a
    display filter hides part of the graph does not discard the hidden nodes'
    layout. Entries without valid numeric x/y are skipped. Returns the number
    of positions saved.
    """
    now = local_timestamp()
    rows = []
    for name, pos in (positions or {}).items():
        if not name or not isinstance(pos, dict):
            continue
        try:
            rows.append((str(name).lower(), float(pos['x']), float(pos['y']), now))
        except (KeyError, TypeError, ValueError):
            continue

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany('''
            INSERT INTO node_positions (node_name, x, y, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_name) DO UPDATE SET
                x = excluded.x,
                y = excluded.y,
                updated_at = excluded.updated_at
        ''', rows)
    return len(rows)


def get_node_positions():
    """Get the saved graph layout as {node_name: {x, y}}."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT node_name, x, y FROM node_positions')
        return {row['node_name']: {'x': row['x'], 'y': row['y']} for row in cursor.fetchall()}


def clear_node_positions():
    """Delete the saved graph layout."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM node_positions')


# ============ Event Operations ============

# Event types
EVENT_NODE_DISCOVERED = 'node_discovered'
EVENT_NODE_OFFLINE = 'node_offline'
EVENT_NODE_ONLINE = 'node_online'
EVENT_NODE_DEGRADED = 'node_degraded'
EVENT_LINK_NEW = 'link_new'
EVENT_LINK_DROPPED = 'link_dropped'
EVENT_LINK_REMOVED = 'link_removed'
EVENT_LINK_RESTORED = 'link_restored'
EVENT_FREQUENCY_CHANGE = 'frequency_change'


def trim_connectivity_log(days=None):
    """Trim file-based connectivity log entries older than the retention window."""
    retention_days = days if days is not None else config.CONNECTIVITY_LOG_RETENTION_DAYS
    cutoff = datetime.now() - timedelta(days=retention_days)
    path = config.CONNECTIVITY_LOG_PATH

    try:
        with open(path, 'r', encoding='utf-8') as log_file:
            lines = log_file.readlines()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0

    kept_lines = []
    removed = 0
    for line in lines:
        timestamp = line[:19]
        parsed = _parse_local_timestamp(timestamp)
        if parsed is None or parsed >= cutoff:
            kept_lines.append(line)
        else:
            removed += 1

    if removed:
        try:
            with open(path, 'w', encoding='utf-8') as log_file:
                log_file.writelines(kept_lines)
        except OSError:
            return 0

    return removed


def append_connectivity_log(timestamp, event_type, node_name=None, details=None, severity='info'):
    """Append a connectivity event to the file-based log."""
    node = node_name or ''
    message = details or ''
    line = f"{timestamp}\t{severity}\t{event_type}\t{node}\t{message}\n"
    try:
        with open(config.CONNECTIVITY_LOG_PATH, 'a', encoding='utf-8') as log_file:
            log_file.write(line)
    except OSError:
        return False
    return True


def log_event(event_type, node_name=None, details=None, severity='info'):
    """Log an event to the database"""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO events (timestamp, event_type, node_name, details, severity)
            VALUES (?, ?, ?, ?, ?)
        ''', (now, event_type, node_name, details, severity))
        append_connectivity_log(now, event_type, node_name, details, severity)
        return cursor.lastrowid


def get_events(limit=100, offset=0, event_types=None):
    """Get recent events, optionally filtered by type"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if event_types:
            placeholders = ','.join('?' * len(event_types))
            cursor.execute(f'''
                SELECT * FROM events
                WHERE event_type IN ({placeholders})
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            ''', (*event_types, limit, offset))
        else:
            cursor.execute('''
                SELECT * FROM events
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset))
        return [dict(row) for row in cursor.fetchall()]


def get_events_since(timestamp):
    """Get events since a given timestamp"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE timestamp > ?
            ORDER BY timestamp ASC
        ''', (timestamp,))
        return [dict(row) for row in cursor.fetchall()]


def clear_old_events(days=30):
    """Remove events older than specified days"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM events WHERE timestamp < ?', (cutoff,))
        return cursor.rowcount


# ============ Link History Operations (RF Stats) ============

def insert_link_history(source_node, target_node, link_type, quality=None, snr=None,
                        ping_min=None, ping_avg=None, ping_max=None, ping_loss=None,
                        jitter=None, throughput_tx=None, throughput_rx=None):
    """Insert a new link history record"""
    now = local_timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO link_history (timestamp, source_node, target_node, link_type,
                                     quality, snr, ping_min, ping_avg, ping_max, ping_loss,
                                     jitter, throughput_tx, throughput_rx)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now, source_node, target_node, link_type, quality, snr,
              ping_min, ping_avg, ping_max, ping_loss, jitter, throughput_tx, throughput_rx))
        return cursor.lastrowid


def get_link_history(source_node, target_node, hours=24, limit=2000):
    """Get history for a specific link within the time range"""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM link_history
            WHERE source_node = ? AND target_node = ?
            AND timestamp > ?
            ORDER BY timestamp ASC
            LIMIT ?
        ''', (source_node, target_node, cutoff, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_all_rf_links_history(hours=24):
    """Get recent history for all RF links"""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM link_history
            WHERE link_type = 'RF'
            AND timestamp > ?
            ORDER BY source_node, target_node, timestamp ASC
        ''', (cutoff,))
        return [dict(row) for row in cursor.fetchall()]


def get_rf_links():
    """Get all active RF-type links"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM links
            WHERE link_type = 'RF'
            AND status != 'removed'
            ORDER BY source_node, target_node
        ''')
        return [dict(row) for row in cursor.fetchall()]


def get_rf_links_with_latest_stats():
    """Get RF links with their most recent history stats"""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Get RF links with latest history entry
        cursor.execute('''
            SELECT l.*, h.ping_avg, h.ping_loss, h.throughput_tx, h.throughput_rx,
                   h.timestamp as last_test_time
            FROM links l
            LEFT JOIN (
                SELECT source_node, target_node, ping_avg, ping_loss,
                       throughput_tx, throughput_rx, timestamp,
                       ROW_NUMBER() OVER (PARTITION BY source_node, target_node
                                         ORDER BY timestamp DESC) as rn
                FROM link_history
            ) h ON l.source_node = h.source_node
                AND l.target_node = h.target_node
                AND h.rn = 1
            WHERE l.link_type = 'RF'
            AND l.status != 'removed'
            ORDER BY l.source_node, l.target_node
        ''')
        return [dict(row) for row in cursor.fetchall()]


def get_latest_link_stats(source_node, target_node):
    """Get the most recent stats for a specific link"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM link_history
            WHERE source_node = ? AND target_node = ?
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (source_node, target_node))
        row = cursor.fetchone()
        return dict(row) if row else None


def cleanup_link_history(hours=24):
    """Remove link history records older than specified hours"""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM link_history WHERE timestamp < ?', (cutoff,))
        return cursor.rowcount


def update_link_history_ping(source_node, target_node, ping_min, ping_avg, ping_max, ping_loss,
                             jitter=None):
    """Update the most recent history record with ping data, or insert new if none recent"""
    now = local_timestamp()
    # Check if there's a recent record (within last 2 minutes) to update
    cutoff = (datetime.now() - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id FROM link_history
            WHERE source_node = ? AND target_node = ?
            AND timestamp > ?
            AND ping_avg IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (source_node, target_node, cutoff))
        row = cursor.fetchone()

        if row:
            # Update existing record
            cursor.execute('''
                UPDATE link_history
                SET ping_min = ?, ping_avg = ?, ping_max = ?, ping_loss = ?, jitter = ?
                WHERE id = ?
            ''', (ping_min, ping_avg, ping_max, ping_loss, jitter, row['id']))
        else:
            # Get link info for new record
            cursor.execute('''
                SELECT link_type, quality, snr FROM links
                WHERE source_node = ? AND target_node = ?
            ''', (source_node, target_node))
            link = cursor.fetchone()
            if link:
                cursor.execute('''
                    INSERT INTO link_history (timestamp, source_node, target_node, link_type,
                                             quality, snr, ping_min, ping_avg, ping_max, ping_loss,
                                             jitter)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (now, source_node, target_node, link['link_type'],
                      link['quality'], link['snr'], ping_min, ping_avg, ping_max, ping_loss,
                      jitter))


def update_link_history_throughput(source_node, target_node, throughput_tx, throughput_rx):
    """Update the most recent history record with throughput data, or insert new if none recent"""
    now = local_timestamp()
    # Check if there's a recent record (within last 2 minutes) to update
    cutoff = (datetime.now() - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id FROM link_history
            WHERE source_node = ? AND target_node = ?
            AND timestamp > ?
            AND throughput_tx IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (source_node, target_node, cutoff))
        row = cursor.fetchone()

        if row:
            # Update existing record
            cursor.execute('''
                UPDATE link_history
                SET throughput_tx = ?, throughput_rx = ?
                WHERE id = ?
            ''', (throughput_tx, throughput_rx, row['id']))
        else:
            # Get link info for new record
            cursor.execute('''
                SELECT link_type, quality, snr FROM links
                WHERE source_node = ? AND target_node = ?
            ''', (source_node, target_node))
            link = cursor.fetchone()
            if link:
                cursor.execute('''
                    INSERT INTO link_history (timestamp, source_node, target_node, link_type,
                                             quality, snr, throughput_tx, throughput_rx)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (now, source_node, target_node, link['link_type'],
                      link['quality'], link['snr'], throughput_tx, throughput_rx))


# ============ Utility Functions ============

def get_link_color(link):
    """Determine link color based on quality, status, and link type"""
    link_type = link.get('link_type', '').upper()

    # Dropped links are always red
    if link.get('status') == 'dropped':
        return 'red'

    # DTD links are blue while active (wired connections)
    if link_type == 'DTD':
        return 'blue'

    quality = link.get('quality', 0)

    # Tunnels and Xlinks don't have traditional quality metrics
    # If they're active (not dropped), treat them as good quality
    if link_type in ('TUN', 'TUNNEL', 'VTUN', 'WIREGUARD', 'WG', 'XLINK'):
        if quality == 0 or quality >= 100:
            return 'green'
        # Some tunnels may report actual quality - use it
        if quality > config.QUALITY_GOOD:
            return 'green'
        elif quality > config.QUALITY_POOR:
            return 'yellow'
        else:
            return 'red'

    # RF links - color based purely on quality percentage
    if quality > config.QUALITY_GOOD:  # >85%
        return 'green'
    elif quality > config.QUALITY_POOR:  # >50%
        return 'yellow'
    else:  # <=50%
        return 'red'


def get_starting_node_firmware():
    """Get the firmware version of the starting node (first active node)"""
    # Get the starting node setting
    starting_url = get_setting('starting_node', config.STARTING_NODE)
    # Extract hostname from URL
    host = starting_url.replace('http://', '').replace('https://', '').split('/')[0].split('.')[0].lower()

    with get_connection() as conn:
        cursor = conn.cursor()
        # Try to find a node that matches the starting hostname
        cursor.execute('SELECT firmware_version FROM nodes WHERE name LIKE ? AND is_active = 1', (f'%{host}%',))
        row = cursor.fetchone()
        if row:
            return row['firmware_version']

        # Fallback: get the first active node's firmware
        cursor.execute('SELECT firmware_version FROM nodes WHERE is_active = 1 ORDER BY first_seen LIMIT 1')
        row = cursor.fetchone()
        return row['firmware_version'] if row else None


def get_service_icon(service_name):
    """Get icon character for a service based on its name"""
    name = service_name.lower()

    if 'phone' in name or 'voip' in name or 'sip' in name or 'extension' in name or 'direct ip' in name:
        return '\u260E'  # Phone
    if 'meshchat' in name or 'chat' in name:
        return '\u2709'  # Envelope/message
    if 'pbx' in name or 'asterisk' in name or 'freepbx' in name:
        return '\u2706'  # Telephone
    if 'camera' in name or 'cam' in name or 'video' in name or 'stream' in name:
        return '\u25CE'  # Camera/bullseye
    if 'weather' in name or 'weewx' in name:
        return '\u2600'  # Sun
    if 'winlink' in name:
        return '\u2709'  # Envelope
    if 'web' in name or 'http' in name:
        return '\u2302'  # House/web
    return '\u2022'  # Bullet point for unknown


def get_network_graph_data():
    """Get data formatted for vis.js network graph"""
    active_nodes = get_active_nodes()
    all_nodes = get_all_nodes()
    links = get_active_links()
    selected_node_names = set(get_selected_node_names())

    # Create sets for quick lookup
    all_node_names = {n['name'] for n in all_nodes}
    active_node_names = {n['name'] for n in active_nodes}
    supernode_names = {n['name'] for n in active_nodes if n.get('is_supernode')}
    active_non_supernode_names = active_node_names - supernode_names

    # Find inactive nodes that are DIRECTLY connected to active NON-SUPERNODE nodes
    # (don't show nodes that are only connected to supernodes - they're "beyond" the supernode)
    inactive_nodes_to_show = set()
    for link in links:
        source = link['source_node']
        target = link['target_node']
        # If one end is an active non-supernode and the other is not active, show the inactive one
        if source in active_non_supernode_names and target not in active_node_names:
            inactive_nodes_to_show.add(target)
        elif target in active_non_supernode_names and source not in active_node_names:
            inactive_nodes_to_show.add(source)

    # Include active nodes + inactive nodes directly connected to active nodes
    nodes = []
    nodes_to_show = set()
    for node in all_nodes:
        if node['name'] in active_node_names:
            node['is_inactive'] = False
            nodes.append(node)
            nodes_to_show.add(node['name'])
        elif node['name'] in inactive_nodes_to_show:
            # Inactive node but directly connected to an active node
            node['is_inactive'] = True
            nodes.append(node)
            nodes_to_show.add(node['name'])

    # Keep link-only endpoints visible when a scanned node reports them in LQM
    # but the scanner has not been able to poll that endpoint as a node.
    link_only_nodes = set()
    for link in links:
        source = link['source_node']
        target = link['target_node']
        if source in nodes_to_show and target not in all_node_names:
            link_only_nodes.add(target)
        elif target in nodes_to_show and source not in all_node_names:
            link_only_nodes.add(source)

    for node_name in sorted(link_only_nodes):
        reported_links = []
        for link in links:
            if link['source_node'] == node_name or link['target_node'] == node_name:
                reporter = link['target_node'] if link['source_node'] == node_name else link['source_node']
                reported_links.append({
                    'reporter': reporter,
                    'source': link['source_node'],
                    'target': link['target_node'],
                    'mac_address': link.get('mac_address'),
                    'canonical_ip': link.get('canonical_ip'),
                    'identity_status': link.get('identity_status'),
                    'routability_status': link.get('routability_status'),
                    'lqm_status_message': link.get('lqm_status_message'),
                    'signal': link.get('signal'),
                    'noise': link.get('noise'),
                    'tx_rate': link.get('tx_rate'),
                    'rx_rate': link.get('rx_rate'),
                    'link_type': link['link_type'],
                    'quality': link['quality'],
                    'snr': link.get('snr'),
                    'status': link['status'],
                    'last_seen': link['last_seen']
                })

        nodes_to_show.add(node_name)
        nodes.append({
            'name': node_name,
            'ip': None,
            'description': 'Seen as a link endpoint, but not pollable by this collector',
            'model': None,
            'firmware_version': None,
            'lat': None,
            'lon': None,
            'rf_frequency': None,
            'rf_channel': None,
            'first_seen': None,
            'last_seen': None,
            'is_active': 0,
            'is_supernode': 0,
            'is_inactive': False,
            'is_link_only': True,
            'is_selected': node_name in selected_node_names,
            'reported_links': reported_links
        })

    # Filter links to only include those where BOTH ends are in nodes_to_show
    links = [link for link in links if link['source_node'] in nodes_to_show and link['target_node'] in nodes_to_show]

    # Get reference firmware version
    reference_firmware = get_starting_node_firmware()

    # Build node data for vis.js
    vis_nodes = []

    for node in nodes:
        link_only = node.get('is_link_only', False)
        firmware = node.get('firmware_version', '')
        firmware_mismatch = reference_firmware and firmware and firmware != reference_firmware
        rf_freq = node.get('rf_frequency', '')
        node_name = node['name']
        supernode = node.get('is_supernode', False)

        # Get services for this node and build icon string
        services = [] if link_only else get_node_services(node_name)
        service_icons = ' '.join([get_service_icon(s.get('name', '')) for s in services])

        # Build label with name, frequency, and service icons
        label_parts = [node_name]
        if rf_freq:
            label_parts.append(f"{rf_freq} MHz")
        if service_icons:
            label_parts.append(service_icons)
        label = '\n'.join(label_parts)

        # Build tooltip with service names
        if link_only:
            title_parts = [
                node_name,
                'Link-only endpoint',
                'Seen in a neighbor LQM table, but this collector has not polled sysinfo for it.',
                'Common causes: no routable canonical IP in LQM, depth/supernode boundary, DNS issue, or unreachable node.'
            ]
        else:
            title_parts = [node_name, node.get('model', 'Unknown model'), f"Firmware: {firmware}"]
        if supernode:
            title_parts.append("** SUPERNODE **")
        if services:
            title_parts.append("Services: " + ', '.join([s.get('name', '') for s in services]))
        title = '\n'.join(title_parts)

        vis_nodes.append({
            'id': node_name,
            'label': label,
            'title': title,
            'model': node.get('model'),
            'ip': node.get('ip'),
            'lat': node.get('lat'),
            'lon': node.get('lon'),
            'firmware': firmware,
            'firmware_mismatch': firmware_mismatch,
            'rf_frequency': rf_freq,
            'is_supernode': supernode,
            'is_selected': node_name in selected_node_names,
            'is_inactive': node.get('is_inactive', False),
            'is_link_only': link_only,
            'reported_links': node.get('reported_links', []),
            'identity_status': node.get('identity_status'),
            'routability_status': node.get('routability_status'),
            'lqm_status_message': node.get('lqm_status_message'),
            'mac_addresses': node.get('mac_addresses', []),
            'canonical_ips': node.get('canonical_ips', []),
            'node_type': 'link_only' if link_only else 'main'
        })

    # Build edge data for vis.js
    vis_edges = []

    # COLOR = Quality (or fixed color for certain link types)
    quality_color_map = {
        'green': '#27ae60',   # Good quality (>85%)
        'yellow': '#f39c12',  # Poor quality (50-85%)
        'red': '#e74c3c',     # Bad quality (<50%) or dropped
        'blue': '#3498db',    # DTD links (always blue)
        'gray': '#7f8c8d'     # Unknown/no data
    }

    # First pass: collect all links and find best quality for each pair
    # (links can be asymmetric - A→B may have different quality than B→A)
    link_pairs = {}
    for link in links:
        pair = tuple(sorted([link['source_node'], link['target_node']]))
        if pair not in link_pairs:
            link_pairs[pair] = link.copy()
        else:
            # Use the LOWER quality of bidirectional links (conservative)
            existing = link_pairs[pair]
            if link.get('quality', 0) < existing.get('quality', 0):
                link_pairs[pair]['quality'] = link.get('quality', 0)
            # Keep the worse SNR too
            if link.get('snr') and existing.get('snr'):
                if link['snr'] < existing['snr']:
                    link_pairs[pair]['snr'] = link['snr']

    for pair, link in link_pairs.items():
        link_type = link['link_type'].upper()

        # COLOR = Quality (same for all link types)
        link_color_status = get_link_color(link)
        quality_color = quality_color_map.get(link_color_status, '#27ae60')

        # PATTERN = Link Type
        # RF: Solid line, normal width
        # DTD: Thick solid line (direct wired connection)
        # Tunnel (old): Dashed line [10, 10]
        # Wireguard: Dotted line [3, 3]
        # Xlink: Dash-dot pattern [15, 5, 3, 5]
        dashes = False
        width = 2
        length = None  # Use physics default

        if link_type == 'DTD':
            # DTD: thick solid line, very short length (keeps paired nodes close)
            dashes = False
            width = 5
            length = 20
        elif link_type == 'XLINK':
            # Xlink: dash-dot pattern, slightly thicker for visibility
            dashes = [12, 4, 2, 4]
            width = 3
        elif link_type in ('TUN', 'TUNNEL', 'VTUN'):
            # Old-style tunnel: dashed line, longer length
            dashes = [10, 10]
            width = 1
            length = 300
        elif link_type in ('WIREGUARD', 'WG'):
            # Wireguard: dotted line, longer length
            dashes = [3, 3]
            width = 1
            length = 300
        # else: RF - solid line (dashes = False, width = 2)

        # Build edge object
        edge = {
            'from': link['source_node'],
            'to': link['target_node'],
            'color': {'color': quality_color, 'highlight': quality_color},
            'width': width,
            'dashes': dashes,
            'title': f"Type: {link['link_type']}\nQuality: {link['quality']}%\nSNR: {link.get('snr', 'N/A')}",
            'link_type': link['link_type'],
            'quality': link['quality'],
            'snr': link.get('snr'),
            'status': link['status'],
            'drop_count': link.get('drop_count', 0),
            'last_seen': link.get('last_seen'),
            'mac_address': link.get('mac_address'),
            'canonical_ip': link.get('canonical_ip'),
            'identity_status': link.get('identity_status'),
            'routability_status': link.get('routability_status'),
            'lqm_status_message': link.get('lqm_status_message'),
            'signal': link.get('signal'),
            'noise': link.get('noise'),
            'tx_rate': link.get('tx_rate'),
            'rx_rate': link.get('rx_rate')
        }

        # Only add length if specified (don't send null)
        if length is not None:
            edge['length'] = length

        vis_edges.append(edge)

    return {'nodes': vis_nodes, 'edges': vis_edges}


# Initialize database on module load
init_db()

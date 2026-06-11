"""
AREDN Network Monitor Configuration
"""

import os


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default

# Starting node - can be overridden via web UI
STARTING_NODE = os.environ.get(
    "STARTING_NODE",
    "http://localnode.local.mesh/cgi-bin/sysinfo.json?lqm=1&hosts=1&services=1&services_local=1"
)

# Polling interval in seconds (increase if scans take too long)
POLL_INTERVAL = env_int("POLL_INTERVAL_SECONDS", 60)

# Link timeout thresholds (in seconds)
LINK_TIMEOUT = env_int("LINK_TIMEOUT_SECONDS", 300)  # 5 minutes - fallback timeout for links/nodes not seen by scans
LINK_REMOVE_AFTER = env_int("LINK_REMOVE_AFTER_SECONDS", 3600)  # 60 minutes - remove dropped links from display
NEW_NODE_DAYS = env_int("NEW_NODE_DAYS", 30)  # Announce nodes as new only when not seen recently
DATABASE_RETENTION_DAYS = env_int("DATABASE_RETENTION_DAYS", 90)  # Keep inactive node records this many days

# Connectivity event log
CONNECTIVITY_LOG_PATH = "connectivity.log"
CONNECTIVITY_LOG_RETENTION_DAYS = 30

# Link quality thresholds (0-100)
QUALITY_GOOD = 85  # Above this = green
QUALITY_POOR = 50  # Above this = yellow, below = red

# Connection types to show (filter out tunnels)
SHOW_TUNNELS = env_bool("SHOW_TUNNELS", False)

# Maximum hops from starting node during discovery
MAX_DEPTH = env_int("MAX_DEPTH", 5)

# Database file path
DATABASE_PATH = os.environ.get("DATABASE_PATH", "aredn_monitor.db")

# Request timeout for node queries (seconds)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT_SECONDS", 30)

# Web server settings
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = env_int("PORT", 5000)
DEBUG = env_bool("DEBUG", False)  # Disabled - eventlet doesn't work well with werkzeug's reloader

# ============ Distributed CouchDB Configuration ============

COLLECTOR_ID = os.environ.get("COLLECTOR_ID", "collector-local")
COLLECTOR_SITE = os.environ.get("COLLECTOR_SITE", "Local Collector")
COLLECTOR_VERSION = os.environ.get("COLLECTOR_VERSION", "0.1.0")

# Empty COUCH_URL keeps the existing SQLite-only behavior.
COUCH_URL = os.environ.get("COUCH_URL", "").rstrip("/")
COUCH_DB = os.environ.get("COUCH_DB", "aredn_monitor")
LOCAL_CONFIG_DB = os.environ.get("LOCAL_CONFIG_DB", "aredn_local_config")
STORE_RAW_SYSINFO = env_bool("STORE_RAW_SYSINFO", False)

# ============ RF Statistics Configuration ============

# Enable/disable RF stats collection
RF_STATS_ENABLED = True

# Ping test settings
PING_INTERVAL = 60      # Seconds between ping rounds
PING_COUNT = 5          # Number of pings per test
PING_TIMEOUT = 5        # Timeout per ping in seconds

# Iperf3 test settings
IPERF_INTERVAL = 300    # Seconds between iperf queue processing (5 minutes)
IPERF_DURATION = 5      # Duration of each iperf test in seconds
IPERF_BANDWIDTH = '10M' # Bandwidth limit to avoid overwhelming network

# Quality threshold for running iperf tests (skip if below this)
QUALITY_THRESHOLD_IPERF = 50

# History retention
HISTORY_RETENTION_HOURS = 24  # How long to keep historical data

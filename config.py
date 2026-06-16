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

# Request timeout for node queries (seconds). Kept modest because a slow/dead
# node otherwise stalls its scan wave; the bare-sysinfo fallback catches nodes
# that are merely slow to assemble the full response.
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT_SECONDS", 15)

# How many nodes to poll concurrently within a BFS depth level. Serial polling
# of a large mesh makes the scan cycle far longer than LINK_TIMEOUT, which marks
# healthy nodes "unreachable" just because the scan can't revisit them in time.
SCAN_CONCURRENCY = env_int("SCAN_CONCURRENCY", 12)

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

# ============ Diagnostics / Incident Mode ============

# Persist the full raw LQM tracker dict per link sample so reports can mine
# fields we have not promoted to columns yet. Trackers are small.
STORE_RAW_TRACKER = env_bool("STORE_RAW_TRACKER", True)

# Capture per-poll node health (uptime, load, memory) into node_health_history.
NODE_HEALTH_ENABLED = env_bool("NODE_HEALTH_ENABLED", True)
NODE_HEALTH_RETENTION_HOURS = env_int("NODE_HEALTH_RETENTION_HOURS", 24 * 14)

# Structured link state-change log (used by the flap report). Kept longer than
# RF history because it is tiny (one row per transition, not per sample).
LINK_STATE_LOG_RETENTION_DAYS = env_int("LINK_STATE_LOG_RETENTION_DAYS", 30)

# Incident mode: when a watched node's link drops or goes marginal, sample that
# one link hard (bidirectionally) for a short window instead of standing down.
INCIDENT_MODE_ENABLED = env_bool("INCIDENT_MODE_ENABLED", True)

# Comma-separated node-name substrings to watch closely (e.g. "w0gq-col").
# Empty string watches nothing; "*" watches every node.
def _parse_watched(raw):
    return [item.strip().lower() for item in (raw or "").split(",") if item.strip()]

WATCHED_NODES = _parse_watched(os.environ.get("WATCHED_NODES", "w0gq-col"))

# A link at/under this quality is "marginal" and worth incident probing.
INCIDENT_MARGINAL_QUALITY = env_int("INCIDENT_MARGINAL_QUALITY", 70)

# Mesh reachability probes: when the scanner can't poll a node but a reachable
# neighbor reports a live link to it, ask that neighbor to ping it. A success
# confirms "reachable via mesh"; a failure (neighbor hears it on RF but can't
# route to it) is strong evidence the node is really down/wedged.
MESH_PROBE_ENABLED = env_bool("MESH_PROBE_ENABLED", True)
# Cap probes per scan cycle and re-probe interval, to avoid flooding the mesh.
MESH_PROBE_MAX_PER_CYCLE = env_int("MESH_PROBE_MAX_PER_CYCLE", 8)
MESH_PROBE_COOLDOWN_SECONDS = env_int("MESH_PROBE_COOLDOWN_SECONDS", 300)
# A mesh probe result is "recent" (trusted for status) for this long.
MESH_PROBE_FRESH_SECONDS = env_int("MESH_PROBE_FRESH_SECONDS", 900)
# Total incident capture window and gap between probe rounds, in seconds.
INCIDENT_DURATION = env_int("INCIDENT_DURATION_SECONDS", 90)
INCIDENT_PROBE_INTERVAL = env_int("INCIDENT_PROBE_INTERVAL_SECONDS", 5)
INCIDENT_PING_COUNT = env_int("INCIDENT_PING_COUNT", 3)


# ============ Incident Report (deterministic + optional AI summary) ============

# The deterministic evidence bundle always works offline. The AI narrative is
# opt-in and never in the live scan/outage path or the status-decision path.
INCIDENT_REPORT_AI_ENABLED = env_bool("INCIDENT_REPORT_AI_ENABLED", False)
INCIDENT_REPORT_MODEL = os.environ.get("INCIDENT_REPORT_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ============ VOIP Diagnostics ============

# Default voice codec for MOS + capacity math; the UI can override per test.
# 'mixed' is conservative (G.711 bitrate for capacity, G.729 loss model for MOS).
VOIP_CODEC = os.environ.get("VOIP_CODEC", "mixed")
# Per-call on-the-wire bitrate (kbps) by codec, for concurrent-call capacity.
VOIP_CODEC_KBPS = {'g711': 87, 'g729': 31, 'opus': 45, 'mixed': 87}
# Fraction of measured throughput usable for calls (leave headroom for signaling/jitter).
VOIP_CAPACITY_HEADROOM = float(os.environ.get("VOIP_CAPACITY_HEADROOM", "0.75"))
# Path MTU at/under which to warn (wireguard tunnels often need ~1420; RTP fragments below).
VOIP_WG_SAFE_MTU = env_int("VOIP_WG_SAFE_MTU", 1400)
# DF-bit ping payload sizes (descending) for the path-MTU sweep; +28 = IP+ICMP header.
VOIP_MTU_PROBE_SIZES = [1472, 1450, 1422, 1400, 1372, 1280]


def is_watched_node(node_name):
    """Return True when this node should get incident-mode attention."""
    if not node_name:
        return False
    if "*" in WATCHED_NODES:
        return True
    name = node_name.lower()
    return any(token in name for token in WATCHED_NODES)

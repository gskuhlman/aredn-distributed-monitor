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
CONNECTIVITY_LOG_RETENTION_DAYS = env_int("CONNECTIVITY_LOG_RETENTION_DAYS", 31)

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

# Offline base map for the Channel Map tab: Protomaps .pmtiles extracts made
# by fetch_offline_map.py. Every file in the folder is drawn, less detailed
# ones underneath; OFFLINE_MAP_FILE is the script's default output name.
OFFLINE_MAP_DIR = os.environ.get("OFFLINE_MAP_DIR", "maps")
OFFLINE_MAP_FILE = os.environ.get("OFFLINE_MAP_FILE", "area.pmtiles")

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
# Long enough for the Reports page to scrub back a month.
HISTORY_RETENTION_HOURS = env_int("HISTORY_RETENTION_HOURS", 24 * 31)
# raw_tracker blobs (~1KB each) are nulled out sooner to keep the DB small.
RAW_TRACKER_RETENTION_HOURS = env_int("RAW_TRACKER_RETENTION_HOURS", 24)

# ============ Diagnostics / Incident Mode ============

# Persist the full raw LQM tracker dict per link sample so reports can mine
# fields we have not promoted to columns yet. Trackers are small.
STORE_RAW_TRACKER = env_bool("STORE_RAW_TRACKER", True)

# Capture per-poll node health (uptime, load, memory) into node_health_history.
NODE_HEALTH_ENABLED = env_bool("NODE_HEALTH_ENABLED", True)
NODE_HEALTH_RETENTION_HOURS = env_int("NODE_HEALTH_RETENTION_HOURS", 24 * 31)

# Structured link state-change log (used by the flap report). Kept longer than
# RF history because it is tiny (one row per transition, not per sample).
LINK_STATE_LOG_RETENTION_DAYS = env_int("LINK_STATE_LOG_RETENTION_DAYS", 31)

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

# --- Link Watch (link_watch.py): noise-floor watch and one-way link alarm ---
# Noise = signal - SNR from each radio's own LQM trackers, vs. a rolling
# baseline (same hour of day once NOISE_HOUR_MIN_DAYS days exist).
LINK_WATCH_ENABLED = env_bool("LINK_WATCH_ENABLED", True)
NOISE_JUMP_DB = env_int("NOISE_JUMP_DB", 5)              # dB above baseline to alarm
NOISE_CLEAR_DB = env_int("NOISE_CLEAR_DB", 2)            # dB above baseline to clear
NOISE_JUMP_SCANS = env_int("NOISE_JUMP_SCANS", 2)        # consecutive scans to raise/clear
NOISE_BASELINE_HOURS = env_int("NOISE_BASELINE_HOURS", 72)
NOISE_BASELINE_EXCLUDE_MINUTES = env_int("NOISE_BASELINE_EXCLUDE_MINUTES", 60)
NOISE_MIN_BASELINE_SAMPLES = env_int("NOISE_MIN_BASELINE_SAMPLES", 20)
NOISE_HOUR_MIN_DAYS = env_int("NOISE_HOUR_MIN_DAYS", 3)
# One-way: one end hears >= HIGH while the other hears <= LOW (LQ %), Babel
# cost infinite on one side with SNR >= SNR_OK both ways, or MAC-only identity.
ONE_WAY_HIGH_LQ = env_int("ONE_WAY_HIGH_LQ", 80)
ONE_WAY_LOW_LQ = env_int("ONE_WAY_LOW_LQ", 40)
ONE_WAY_SNR_OK = env_int("ONE_WAY_SNR_OK", 15)
ONE_WAY_SCANS = env_int("ONE_WAY_SCANS", 2)
ONE_WAY_ESCALATE_MINUTES = env_int("ONE_WAY_ESCALATE_MINUTES", 60)
ONE_WAY_FORGET_MINUTES = env_int("ONE_WAY_FORGET_MINUTES", 60)
# --- end Link Watch ---
# Maintenance windows (maintenance.py): alarms stay quiet this many minutes
# after a window ends while radios rejoin.
MAINTENANCE_GRACE_MINUTES = env_int("MAINTENANCE_GRACE_MINUTES", 10)
# Power-cut advisor (power_advisor.py): a neighbour stays "ok" while its
# median SNR of the radio after the cut is >= MIN_SNR + MARGIN (and its fade
# level >= MIN_SNR). 15 dB is AREDN LQM's default minimum SNR.
POWER_ADVISOR_MIN_SNR = env_int("POWER_ADVISOR_MIN_SNR", 15)
POWER_ADVISOR_MARGIN_DB = env_int("POWER_ADVISOR_MARGIN_DB", 6)

# --- Route Watch (route_watch.py): periodic traceroutes along key paths ---
ROUTE_WATCH_ENABLED = env_bool("ROUTE_WATCH_ENABLED", True)
ROUTE_WATCH_INTERVAL_MINUTES = env_int("ROUTE_WATCH_INTERVAL_MINUTES", 15)
ROUTE_BASELINE_HOURS = env_int("ROUTE_BASELINE_HOURS", 24)       # baseline = most common route in this window
ROUTE_BASELINE_MIN_RUNS = env_int("ROUTE_BASELINE_MIN_RUNS", 3)  # runs before a baseline exists
ROUTE_CHANGE_RUNS = env_int("ROUTE_CHANGE_RUNS", 2)              # runs in a row to raise / clear
ROUTE_DETOUR_HOPS = env_int("ROUTE_DETOUR_HOPS", 2)              # this many more hops = detour
ROUTE_HISTORY_RETENTION_DAYS = env_int("ROUTE_HISTORY_RETENTION_DAYS", 31)
# --- end Route Watch ---
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

# ---- Two-ended / one-ended streamed sessions (ported from voip_diag) ----
# Default stream characteristics resemble a 20ms-packtized RTP voice call.
VOIP_AGENT_PORT = env_int("VOIP_AGENT_PORT", 8765)      # agent HTTP control port
VOIP_PROBE_UDP_PORT = env_int("VOIP_PROBE_UDP_PORT", 8766)  # UDP stream + echo port
VOIP_STREAM_PPS = env_int("VOIP_STREAM_PPS", 50)
VOIP_STREAM_PACKET_BYTES = env_int("VOIP_STREAM_PACKET_BYTES", 172)
# Route refresh cadence during a session (seconds).
VOIP_SESSION_ROUTE_INTERVAL = env_int("VOIP_SESSION_ROUTE_INTERVAL", 30)
# Max session wall-clock length; the UI start button enforces nothing, this is a
# safety net so a forgotten test doesn't stream forever.
VOIP_SESSION_MAX_SECONDS = env_int("VOIP_SESSION_MAX_SECONDS", 2 * 3600)
# Only one streamed session runs at a time (it owns the UDP probe port).



def is_watched_node(node_name):
    """Return True when this node should get incident-mode attention."""
    if not node_name:
        return False
    if "*" in WATCHED_NODES:
        return True
    name = node_name.lower()
    return any(token in name for token in WATCHED_NODES)

# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

AREDN Network Monitor is currently a Flask web application for monitoring AREDN mesh networks. It uses Socket.IO for live updates, vis.js for the graph, Chart.js for RF history, and SQLite for the existing dashboard state.

The repository is being transitioned toward a distributed, offline-capable monitor. Do not rewrite the front end before mapping the existing code and identifying the smallest backend/data-layer change needed. The first distributed seam is already present: optional append-only CouchDB observation writes alongside the existing SQLite updates.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the existing app
python app.py
# Server starts at http://localhost:5000

# Run via systemd user service (recommended for persistent operation)
systemctl --user daemon-reload
systemctl --user enable --now aredn-monitor

# Validate Python syntax
python -m py_compile app.py scanner.py config.py database.py rf_stats.py observations.py couch_client.py

# Bootstrap CouchDB databases and indexes when COUCH_URL is configured
python couch_client.py
```

There is no full test suite or linting currently configured.

## Architecture

### Backend Components

- `app.py`: Flask entry point with REST routes, Socket.IO handlers, APScheduler setup, and health/collector endpoints.
- `scanner.py`: BFS network discovery starting from the seed node. Fetches AREDN `sysinfo.json`, extracts node metadata and link quality metrics, updates SQLite, and optionally writes CouchDB observations.
- `database.py`: Legacy SQLite operations for nodes, links, services, events, settings, and link history.
- `observations.py`: Storage-neutral builders for deterministic append-only observation documents.
- `couch_client.py`: Small CouchDB client for health checks, database/index bootstrap, Mango `_find`, and `_bulk_docs`.
- `rf_stats.py`: Ping tests and iPerf throughput benchmarks with queue-based processing.
- `config.py`: Central configuration and environment-variable overrides.

### Frontend Components

- `templates/index.html`: Single-page app with Network, RF Stats, and Nodes tabs.
- `static/js/network.js`: vis.js graph initialization, Socket.IO message handling, node position persistence, settings, and event-log UI.
- `static/js/node-page.js`: Full node detail page, targeted node scan, per-link ping/iPerf actions, node log search, and charts.
- `static/js/nodes.js`: Nodes table, filters, detail panel, node history charts, and delete action.
- `static/js/rf-stats.js`: Chart.js graphs for quality, SNR, latency, throughput, and RF test actions.
- `static/css/style.css`: Application layout and visual styling.

## Current Data Flow

1. APScheduler or a user action triggers `scheduled_scan()` in `app.py`.
2. `scanner.discover_network()` performs BFS traversal from the configured seed node.
3. `scanner.py` updates legacy SQLite state through `database.py`.
4. If `COUCH_URL` is configured, `scanner.py` also builds append-only observation docs and writes them with CouchDB `_bulk_docs`.
5. `socketio.emit('scan_complete', ...)` broadcasts current network data from SQLite to connected clients.
6. The front end updates the vis.js graph, node views, RF stats, and event log.

## Distributed Observation Model

The CouchDB path is append-only and deterministic:

- `node_observation`: one per attempted node poll, including failed polls
- `link_observation`: one per observed link from sysinfo/LQM data
- `service_observation`: one per service seen on a node
- `collector_heartbeat`: one per scan cycle

Document IDs include collector ID, UTC poll timestamp, and target slug. Retries should hit the same document ID; CouchDB conflicts for those IDs are duplicate/retry cases, not fatal errors.

Do not introduce shared mutable documents like `node:<name>` with a global `status` field for collectors to update. Distributed status must be derived from observations and heartbeats.

## Status Semantics

The future distributed status rules must be conservative:

- `UP`: at least one healthy/recent collector saw the node recently.
- `LOCALLY_UNREACHABLE`: the local collector cannot see it, but another recent collector can.
- `PARTITIONED`: recent healthy collectors disagree on visibility.
- `STALE`: no recent sighting, but collector coverage is insufficient to confirm down.
- `DOWN_CONFIRMED`: no recent sighting and multiple healthy collectors provide strong unreachable evidence.
- `UNKNOWN`: no useful observations exist.

Never mark a node globally down merely because the local collector cannot see it.

## REST API

Existing dashboard routes:

- `GET /api/network`
- `GET /api/nodes`, `/api/nodes/active`, `/api/nodes/all`
- `GET /api/node/<name>`, `/api/nodes/detail/<name>`
- `GET /api/nodes/full/<name>`
- `POST /api/nodes/scan/<name>`
- `GET /nodes/<name>`
- `GET /api/links`, `/api/links/active`
- `GET/POST /api/settings`
- `POST /api/scan`
- `GET /api/status`
- `GET /api/events`
- `GET/POST /api/rf-stats/*`

Distributed/readiness routes:

- `GET /api/health`: app health and optional CouchDB connectivity
- `GET /api/collectors`: known collectors from CouchDB heartbeats when configured, otherwise local runtime fallback

## WebSocket Events

Server emits:

- `scan_started`
- `scan_complete`
- `network_update`
- `link_dropped`
- `node_inactive`
- `new_event`
- `rf_stats_update`
- `iperf_test_status`

Client emits:

- `request_scan`
- `request_network`
- `request_events`

## Configuration

Important settings in `config.py` and environment variables:

- `STARTING_NODE`: Seed URL for discovery.
- `POLL_INTERVAL_SECONDS` / `POLL_INTERVAL`: Scan frequency.
- `REQUEST_TIMEOUT_SECONDS` / `REQUEST_TIMEOUT`: AREDN request timeout.
- `MAX_DEPTH`: Max hops from seed.
- `LINK_TIMEOUT_SECONDS` / `LINK_TIMEOUT`: Legacy dropped/inactive threshold.
- `LINK_REMOVE_AFTER_SECONDS` / `LINK_REMOVE_AFTER`: Legacy dropped-link removal threshold.
- `RF_STATS_ENABLED`: Enable ping/iPerf collection.
- `COLLECTOR_ID`: Stable collector ID for observation docs.
- `COLLECTOR_SITE`: Human-readable collector site.
- `COUCH_URL`: Enables CouchDB observation writes when set.
- `COUCH_DB`: Replicated monitoring database.
- `LOCAL_CONFIG_DB`: Local-only config database.
- `STORE_RAW_SYSINFO`: Reserved for raw sysinfo retention.

Settings edited through `/api/settings` persist to SQLite and currently affect legacy scanner behavior.

## Key Development Rules

- Preserve existing front-end routes and JSON shapes unless intentionally migrating one view at a time.
- Keep SQLite current-state compatibility until replacement reporting endpoints are ready.
- Keep collector logic, CouchDB access, status logic, and reporting API separated.
- Do not bury distributed status rules in UI JavaScript.
- Treat network failures as expected behavior and record failed observations.
- Use UTC timestamps in observation documents.
- Avoid storing secrets in replicated monitoring documents.
- Keep normal CouchDB writes append-only to avoid replication conflicts.

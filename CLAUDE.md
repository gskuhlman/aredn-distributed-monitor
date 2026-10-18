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
python -m py_compile app.py scanner.py config.py database.py rf_stats.py observations.py couch_client.py voip.py voip_proto.py voip_session.py agent.py mesh_diagnostics.py gateway.py reports.py link_diagnostics.py infra_audit.py change_log.py channel_map.py notify.py

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
- `voip.py`: On-demand VOIP call-quality diagnostics (MOS, segment attribution, capacity, MTU).
- `voip_proto.py`: Shared wire protocol + helpers for streamed VoIP sessions (stdlib-only; also imported by `agent.py`).
- `voip_session.py`: Two-ended (remote agent) / one-ended streamed voice-like UDP session orchestration + correlation report.
- `agent.py`: Standalone remote agent (stdlib-only) run on the far-end PC for two-ended sessions. Ship with `voip_proto.py`.
- `mesh_diagnostics.py`: Network-wide Mesh Health Check (near-far/overpowering radios, SNR and TX/RX imbalance, one-way links, loss, VoIP MOS, flapping, noise, congestion, node health, hidden nodes, single points of failure). Read-only over SQLite; scoped by scan boundaries.
- `gateway.py`: Starting-node reachability gate (cached). Scans, ping rounds, ping-all, iPerf, incident/mesh probes, and VoIP tests/sessions check it before starting and before recording a failure; if the starting node is unreachable they abort and discard instead of blaming the target.
- `reports.py`: Read-only time-window reporting (Reports tab): per-node poll health, per-link RF stats, state changes, events, VoIP, and bucketed series, filtered like the other pages.
- `link_diagnostics.py`: Pair diagnosis for the RF Stats link view. Combines both ends' LQM trackers (including a MAC-only alias when one end lost the other's hostname), promoted LQM history columns, state log, node health/config, each end's full RF neighbor list (receiver-wide loss, near-far/overpowering neighbors with a suggested power cut), and radios at each site or in each antenna's beam tagged by channel relation (same/overlap/adjacent/near/clear) into ranked findings and a verdict; `run_live_checks` pings/traceroutes between the two nodes via their CGI.
- `infra_audit.py`: Infrastructure Review (Diagnostics tab): audits infrastructure nodes' stored radio settings (optionally refreshed live) for site channel plans, overlapping radios in antenna beams, azimuth vs. neighbor bearings, PtP pairing, firmware, user blocks, reachability, near-far, plus a verdict per RF link touching infrastructure.
- `change_log.py`: Change log (Changes tab): manual channel/power/antenna/firmware/other entries plus `auto` entries imported from `frequency_change`, `radio_config_change` and `firmware_change` events (SQLite `change_log`, `event_id` unique). `compare()` measures every RF link of the changed node (or the given pair) over equal windows before/after the change: LQ, quality, SNR, noise (signal - SNR), retry/fail rates from cumulative counters, bitrates, ping, node-reported peer downs, reachability, with per-metric better/worse/same verdicts and aligned series.
- `channel_map.py`: Channel Map tab data: every located radio (channel, width, edges, antenna beam; beamwidth from stored value, antenna description or gain), sites (radios within 150 m), RF links, and channel conflicts (overlap/adjacent/near, not same channel) between radios on one mast or within 25 km with at least one aimed at the other, ranked by severity. Radios inactive but seen in the last 7 days still count. What-if overrides (frequency/width per radio) are applied in memory only and compared against the stored plan.
- `notify.py`: Operator notifications (email/SMTP, Discord webhook, ntfy, generic JSON webhook). `database.log_event` hands each committed event to listeners; `notify.install()` (called only from the `__main__` startup path) registers a non-blocking listener plus a worker that applies rules (event types, min severity, scope any/infrastructure/selected/node list, channels) and anti-spam: mute-until, quiet hours, `is_suppressed()` hook (for future maintenance windows), per (rule, node/link, type) cooldown, flap hold (problem + recovery within N minutes -> one "flapped" note; recoveries only for alerted problems), and per-channel digest. Tables `notification_config`, `notification_rules`, `notifications_log` (retention = `CONNECTIVITY_LOG_RETENTION_DAYS`).
- `config.py`: Central configuration and environment-variable overrides.

### Frontend Components

- `templates/index.html`: Single-page app with Network, RF Stats, and Nodes tabs.
- `static/js/network.js`: vis.js graph initialization, Socket.IO message handling, node position persistence, settings, and event-log UI.
- `static/js/node-page.js`: Full node detail page, targeted node scan, per-link ping/iPerf actions, node log search, and charts.
- `static/js/nodes.js`: Nodes table, filters, detail panel, node history charts, and delete action.
- `static/js/rf-stats.js`: Chart.js graphs for quality, SNR, latency, throughput, and RF test actions.
- `static/js/link-diagnosis.js`: RF Stats pair diagnosis panel (verdict, findings, direction/node comparison, colocated radios, state log, live path check, per-direction LQM charts).
- `static/js/notifications.js`: Notifications section on the Diagnostics tab (channels, timing, quiet hours, mute, rules, send log, per-channel test).
- `static/js/infra-review.js`: Infrastructure Review section on the Diagnostics tab (findings, per-site spectrum chart and settings table, link verdicts).
- `static/js/channel-map.js`: Channel Map tab (Leaflet 1.9.4 lazy-loaded from cdnjs, OpenStreetMap tiles): radios colored by frequency, beam wedges, RF links, dashed conflict lines and mast rings, filters (scope, band, frequency range, severity), radio side panel, and what-if channel plan. Hooks its own tab button; no changes to rf-stats.js tab switching.
- `static/js/rf-channel.js`: Shared node channel display (centre/width/edge frequencies and channel numbers) and connected-peer channel relation (overlap/adjacent/near/same/clear) for the Nodes tab detail panel and node page; values come from `infra_audit.annotate_link_rf` via `/api/nodes/detail` and `/api/nodes/full` (`node.rf_span`, `link.peer_rf`).
- `static/js/changes.js`: Changes tab (change list, filters, add form, before/after metrics and overlay charts; `/#changes/<id>` deep link). Also registers a `changeMarkers` Chart.js plugin that RF Stats uses to draw change times on link charts.
- `static/js/node-changes.js`: Node page Changes section with an inline Add change form.
- `static/js/reports.js`: Reports tab: start/end window scrubbing (presets, prev/next, slider), filters, timeline, tables, and node/link drill-down charts.
- `static/css/style.css`: Application layout and visual styling.

## Current Data Flow

1. APScheduler or a user action triggers `scheduled_scan()` in `app.py`.
2. `scanner.discover_network()` performs BFS traversal from the configured seed node. It fetches everything first (no DB writes), probing the seed after every wave.
3. If the seed (the collector's gateway onto the mesh) is unreachable at the start or stops answering mid-scan, the scan is aborted and all results are discarded: no node/link/health writes, no timeout-based aging, only a `gateway_lost` event (on transition) and a heartbeat with `scan_aborted: true`. Targeted scans are gated on the configured seed too. Otherwise `scanner.py` commits the results to legacy SQLite state through `database.py`.
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
- `GET /api/rf-stats/diagnose/<a>/<b>?hours=N`, `POST /api/rf-stats/diagnose/<a>/<b>/live`: pair diagnosis and live path check

Change log routes:

- `GET /api/changes?node&hours&limit`, `POST /api/changes` `{node_name, description, category?, timestamp?, peer_node?, author?}`, `DELETE /api/changes/<id>`
- `GET /api/changes/<id>/compare?hours=24`: before vs. after stats for the changed node's RF links (the before window shrinks to match while the after window is still filling)
Notification routes (secrets always masked as `********`; posting the mask back keeps the stored value, an empty string clears it):

- `GET/POST /api/notifications/settings`, `GET/POST /api/notifications/rules`, `DELETE /api/notifications/rules/<id>`
- `POST /api/notifications/test` `{channel}`, `GET /api/notifications/log?limit`

Infrastructure routes:

- `GET /api/nodes/infrastructure`, `POST /api/nodes/infrastructure/<name>` `{infrastructure: bool, site?}`: mark towers/high sites. Site defaults to the location code in the name (`w0gq-col-...` -> `COL`); site names come from the `site_names` setting over defaults (COL Collins Tower, MMC Mt Mercy Hospital, EOC Linn County EOC). Stored in SQLite `infrastructure_nodes`, exposed as `is_infrastructure` on node and graph JSON.
- `GET|POST /api/reports/infrastructure` (`POST {refresh: true}` re-reads settings from the nodes first)
- `GET /api/channel-map?scope&max_km&center&include_inactive`, `POST /api/channel-map/what-if` `{overrides: {name: {frequency, chanbw}} | "name freq width" lines, scope?, ...}`: channel map data and a non-persistent what-if recompute (response adds `what_if` with baseline counts, resolved/introduced/changed conflicts)

Mesh health and scan-boundary routes:

- `GET /api/reports/mesh-health?hours=N`: findings report for the in-scope mesh
- `GET /api/scan-boundaries`, `POST /api/scan-boundaries/<name>` `{mode: 'exclude'|'stop'|null}`: limit BFS discovery. `exclude` = never poll the node (nothing reachable only through it is scanned); `stop` = poll it but don't traverse past it (like a supernode). The seed is always scanned. Boundaries live in the local SQLite `scan_boundaries` table, not CouchDB.

Time-window report routes (Reports tab; `start`/`end` as `YYYY-MM-DDTHH:MM` local time, max 32 days):

- `GET /api/reports/window?start&end&scope&link_type&search&sample_type&events&severity`
- `GET /api/reports/window/node/<name>?start&end`, `GET /api/reports/window/link/<source>/<target>?start&end&sample_type`

Distributed/readiness routes:

- `GET /api/health`: app health and optional CouchDB connectivity
- `GET /api/collectors`: known collectors from CouchDB heartbeats when configured, otherwise local runtime fallback

VoIP session routes (streamed two-ended / one-ended probes):

- `POST /api/voip/session/start`, `POST /api/voip/session/stop`
- `GET /api/voip/session/status` (live metrics snapshot for polling UI)
- `POST /api/voip/session/event`, `POST /api/voip/session/refresh-routes`
- `GET /api/voip/sessions`, `GET /api/voip/session/<id>` (history + samples/events/routes)

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
- `HISTORY_RETENTION_HOURS` (31 days), `RAW_TRACKER_RETENTION_HOURS` (24h), `NODE_HEALTH_RETENTION_HOURS` (31 days), `LINK_STATE_LOG_RETENTION_DAYS` / `CONNECTIVITY_LOG_RETENTION_DAYS` (31): history kept for the Reports tab.
- `VOIP_AGENT_PORT` / `VOIP_PROBE_UDP_PORT`: remote agent control (8765) and UDP probe (8766) ports.
- `VOIP_STREAM_PPS` / `VOIP_STREAM_PACKET_BYTES`: streamed-session defaults (50 pps x 172 bytes ≈ 20ms RTP).
- `VOIP_SESSION_ROUTE_INTERVAL` / `VOIP_SESSION_MAX_SECONDS`: route-refresh cadence and session safety cap.
- `NOTIFY_SMTP_PASSWORD`, `NOTIFY_DISCORD_WEBHOOK`, `NOTIFY_NTFY_TOKEN`, `NOTIFY_WEBHOOK_TOKEN`: optional env overrides for notification secrets (otherwise stored in the local `notification_config` table, edited on the Diagnostics tab).

Settings edited through `/api/settings` persist to SQLite and currently affect legacy scanner behavior.

## Infrastructure Nodes

Infrastructure nodes (towers/high sites, marked like Selected nodes) appear as a filter on every page, as diamonds on the graph, and change diagnostics: never recommend lowering an infrastructure node's TX power; move near-far fixes to the non-infrastructure end. The scanner stores each node's reported radio settings (`rf_mode`, `rf_ssid`, `rf_chanbw`, antenna azimuth/elevation/height/gain/beamwidth, LQM user blocks) and logs a `radio_config_change` event when mode, SSID or width changes. PtP roles: `ptp`/`ptmp` = access point, `sta` = station, `adhoc` = mesh.

## Key Development Rules

- Preserve existing front-end routes and JSON shapes unless intentionally migrating one view at a time.
- Keep SQLite current-state compatibility until replacement reporting endpoints are ready.
- Keep collector logic, CouchDB access, status logic, and reporting API separated.
- Do not bury distributed status rules in UI JavaScript.
- Treat network failures as expected behavior and record failed observations.
- Use UTC timestamps in observation documents.
- Avoid storing secrets in replicated monitoring documents.
- Notification secrets stay local: only the SQLite `notification_config` table or `NOTIFY_*` env vars; never in the `settings` table (returned by `/api/settings`), CouchDB, logs, or API responses.
- Keep normal CouchDB writes append-only to avoid replication conflicts.

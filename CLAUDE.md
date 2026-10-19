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
python -m py_compile app.py scanner.py config.py database.py rf_stats.py observations.py couch_client.py voip.py voip_proto.py voip_session.py agent.py mesh_diagnostics.py gateway.py reports.py link_diagnostics.py infra_audit.py change_log.py channel_map.py notify.py unreachable.py link_watch.py fetch_offline_map.py drift.py cleanup.py collector_health.py

# Bootstrap CouchDB databases and indexes when COUCH_URL is configured
python couch_client.py

# Offline base map for the Channel Map (needs the go-pmtiles CLI in tools/ or on PATH)
python fetch_offline_map.py --dry-run
python fetch_offline_map.py
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
- `drift.py`: Settings Drift (Diagnostics tab): firmware and radio-setting mismatches from stored node settings. Per RF link: mode pairing (access point <-> station, mesh <-> mesh), SSID, width, frequency (critical when both ends' settings were read in the last day, else warning), firmware major version, nightly vs. release. Per site (radios within 150 m): mixed firmware, mesh radios whose SSID/width differ from the site's other mesh radios on the same band, LQM user blocks that differ between sector radios. Network-wide: firmware classes against the newest release seen (current, older point release, previous release line, very old = 3.24 or older or two lines behind, nightly age), nodes never read. sysinfo does not expose LQM distance/SNR thresholds or the radio distance setting, so those are not compared. Scoped by `reports.resolve_scope` and scan boundaries (`mesh_diagnostics.compute_scope`).
- `change_log.py`: Change log (Changes tab): manual channel/power/antenna/firmware/other entries plus `auto` entries imported from `frequency_change`, `radio_config_change` and `firmware_change` events (SQLite `change_log`, `event_id` unique). `compare()` measures every RF link of the changed node (or the given pair) over equal windows before/after the change: LQ, quality, SNR, noise (signal - SNR), retry/fail rates from cumulative counters, bitrates, ping, node-reported peer downs, reachability, with per-metric better/worse/same verdicts and aligned series.
- `channel_map.py`: Channel Map tab data: every located radio (channel, width, edges, antenna beam; beamwidth from stored value, antenna description or gain), sites (radios within 150 m), RF links, and channel conflicts (overlap/adjacent/near, not same channel) between radios on one mast or within 25 km with at least one aimed at the other, ranked by severity. Radios inactive but seen in the last 7 days still count. What-if overrides (frequency/width per radio) are applied in memory only and compared against the stored plan.
- `notify.py`: Operator notifications (email/SMTP, Discord webhook, ntfy, generic JSON webhook). `database.log_event` hands each committed event to listeners; `notify.install()` (called only from the `__main__` startup path) registers a non-blocking listener plus a worker that applies rules (event types, min severity, scope any/infrastructure/selected/node list, channels) and anti-spam: mute-until, quiet hours, `is_suppressed()` hook (for future maintenance windows), per (rule, node/link, type) cooldown, flap hold (problem + recovery within N minutes -> one "flapped" note; recoveries only for alerted problems), and per-channel digest. Tables `notification_config`, `notification_rules`, `notifications_log` (retention = `CONNECTIVITY_LOG_RETENTION_DAYS`).
- `unreachable.py`: Unreachable classifier. Sorts every node with failed scanner polls or mesh probes in a window into a cause (starting_node_outage, placeholder, excluded, legacy_tunnel_only, pings_no_http, intermittent, stale, not_routable, behind_supernode, never_resolves, really_down). Only `really_down`/`intermittent` count as unreachable in Reports; the rest are explained. Uses `fail_reason`/`reported_by`/`via_link_type`, which the scanner records on failed polls in `node_health_history`, plus links/nodes state for older rows.
- `cleanup.py`: Cleanup of stale and junk nodes (Diagnostics tab). Lists candidates grouped by reason with a suggested action: `fake_name` (bare number / IP fragment like "10": delete), `placeholder` (`mac-…`: exclude), `legacy_tunnel_only` / `pings_no_http` / `never_resolves` (from unreachable.py: exclude), `stale` (not seen in `stale_days`: delete, or exclude while a neighbor still lists it and the scanner keeps polling it), `excluded` (un-exclude). Infrastructure, Permanent/Event-tagged, selected and starting nodes are never offered and are refused by the actions; an exclude that would cut healthy nodes off from the starting node (BFS over current links) is refused unless forced. Delete = `database.delete_node` plus the node's `node_health_history` rows.
- `link_watch.py`: Link Watch, run after every committed (never aborted) scan from `scheduled_scan`. Noise-floor watch: each radio's noise (signal - SNR from its own trackers in `link_history`) vs. a rolling baseline (same hour of day once 3+ days exist, else window median, last hour excluded). One-way link alarm: an RF pair where one end hears the other well and the other doesn't (LQ asymmetry, infinite Babel cost with good SNR, MAC-only identity, not listed at all), reusing `link_diagnostics._direction`; only judged when both ends were polled recently and they aren't wired together. Hysteresis state in SQLite `link_watch_state`; events fire on transitions only.
- `collector_health.py`: Starting-node health (Reports tab "Starting Node" section). After every scan `scheduled_scan` starts a background task that measures the path to the starting node (DNS via the OS resolver, skipped for an IP; TCP connect; bare sysinfo over HTTP; error class) and, only when that fails, pings this PC's default gateway, storing it with the scan outcome (aborted, duration, nodes attempted/reachable) in SQLite `collector_health` (retention `NODE_HEALTH_RETENTION_HOURS`). `report()` derives outages (contiguous failures, cause: LAN down / DNS / refused / no answer / lost mid-scan ...), latency and DNS percentiles, lost scans, an hour-of-day profile and a bucketed series with scanner polls. Older history comes from `gateway_lost`/`gateway_restored` events and failed polls the old scanner stored under the starting node's URL slug (e.g. `10`).
- `config.py`: Central configuration and environment-variable overrides.

### Frontend Components

- `templates/index.html`: Single-page app with Network, RF Stats, and Nodes tabs.
- `static/js/network.js`: vis.js graph initialization, Socket.IO message handling, node position persistence, settings, and event-log UI.
- `static/js/node-page.js`: Full node detail page, targeted node scan, per-link ping/iPerf actions, node log search, and charts.
- `static/js/nodes.js`: Nodes table, filters, detail panel, node history charts, and delete action.
- `static/js/rf-stats.js`: Chart.js graphs for quality, SNR, latency, throughput, and RF test actions.
- `static/js/link-diagnosis.js`: RF Stats pair diagnosis panel (verdict, findings, direction/node comparison, colocated radios, state log, live path check, per-direction LQM charts).
- `static/js/drift.js`: Settings Drift section on the Diagnostics tab (findings, firmware histogram, per-site table highlighting cells that differ from the site majority, RF link table). Self-initializing; reuses `InfraReview.renderFinding`.
- `static/js/notifications.js`: Notifications section on the Diagnostics tab (channels, timing, quiet hours, mute, rules, send log, per-channel test).
- `static/js/infra-review.js`: Infrastructure Review section on the Diagnostics tab (findings, per-site spectrum chart and settings table, link verdicts).
- `static/js/channel-map.js`: Channel Map tab (bundled Leaflet 1.9.4 in `static/vendor`, lazy-loaded; base map is the offline Protomaps extract `maps/area.pmtiles` drawn by bundled protomaps-leaflet 5.1.0 when present, with online OpenStreetMap tiles as a switchable fallback): radios colored by frequency, beam wedges, RF links, dashed conflict lines and mast rings, filters (scope, band, frequency range, severity), radio side panel, and what-if channel plan. Hooks its own tab button; no changes to rf-stats.js tab switching.
- `static/js/cleanup.js`: Cleanup section on the Diagnostics tab (candidates grouped by reason, per-group select-all, Exclude / Un-exclude / Delete with a confirm).
- `static/js/collector-health.js`: Starting Node section of the Reports tab (status tiles, outage timeline, response-time and polls charts, hour-of-day outage profile, outage table, Check now); loaded by `ReportsModule.load()` with the Reports window.
- `static/js/link-watch.js`: Link Watch section on the Diagnostics tab (one-way alarms with a Diagnose button into RF Stats, noise table, per-radio noise series with baseline and hour-of-day profile).
- `static/js/rf-channel.js`: Shared node channel display (centre/width/edge frequencies and channel numbers) and connected-peer channel relation (overlap/adjacent/near/same/clear) for the Nodes tab detail panel and node page; values come from `infra_audit.annotate_link_rf` via `/api/nodes/detail` and `/api/nodes/full` (`node.rf_span`, `link.peer_rf`).
- `static/js/changes.js`: Changes tab (change list, filters, add form, before/after metrics and overlay charts; `/#changes/<id>` deep link). Also registers a `changeMarkers` Chart.js plugin that RF Stats uses to draw change times on link charts.
- `static/js/node-changes.js`: Node page Changes section with an inline Add change form.
- `static/js/unreachable-cause.js`: Shared "why unreachable" block for the node page and Nodes detail panel (`unreachable_cause` from `/api/nodes/full` and `/api/nodes/detail`).
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
- `GET /api/reports/drift?scope=all|infrastructure|infrastructure-connected|selected|selected-connected`: settings drift report (`findings`, `links`, `sites` with `norms` and per-radio `differs`, `firmware` histogram, `latest_release`)
- `GET|POST /api/reports/infrastructure` (`POST {refresh: true}` re-reads settings from the nodes first)
- `GET /api/map/base`: `{offline, url, size_bytes, updated}` for the offline map; `GET /maps/<file>.pmtiles` serves it with HTTP range requests
- `GET /api/channel-map?scope&max_km&center&include_inactive`, `POST /api/channel-map/what-if` `{overrides: {name: {frequency, chanbw}} | "name freq width" lines, scope?, ...}`: channel map data and a non-persistent what-if recompute (response adds `what_if` with baseline counts, resolved/introduced/changed conflicts)

Cleanup routes (`names` is a list; protected nodes are always refused and listed in `refused`):

- `GET /api/cleanup/candidates?stale_days=30&hours=72`
- `POST /api/cleanup/exclude` `{names, force?}` (force allows an exclude that cuts healthy nodes off), `POST /api/cleanup/unexclude` `{names}`, `POST /api/cleanup/delete` `{names, force?}` (force allows deleting active nodes)

Starting-node health routes:

- `GET /api/collector/health?start&end` (or `hours`, default 24): `summary` (state, last answer, latency/DNS/connect percentiles, outages, lost scans, availability), `outages`, `series`, `hour_profile`, `recent` checks
- `POST /api/collector/health/check`: measure the path to the starting node now and store it (kind `manual`)

Link Watch routes:

- `GET /api/reports/noise?hours=72&days=7`: every radio's current noise, baseline, delta and alarm state; with `&node=<name>`, that radio's series, baseline and 24-hour profile
- `GET /api/reports/one-way?hours=24`: `active` (alarmed), `pending` (one-way now, not yet confirmed) and `cleared` (within `hours`) RF pairs with reasons and both ends' LQ/quality/SNR/cost

Link Watch events (transitions only, with hysteresis; also categorized in Reports): `noise_jump` (warning), `noise_cleared` (info), `one_way_link` (warning, or critical when it touches infrastructure or is still one-way after `ONE_WAY_ESCALATE_MINUTES`), `one_way_link_cleared` (info; also raised when a flagged pair stops being evaluated for `ONE_WAY_FORGET_MINUTES`). Format: noise events have `node_name` = the radio; one-way events have `node_name` = the deaf end and details starting `RF link to <peer>: ...` (same shape as `link_dropped`, matched by notifications).

Mesh health and scan-boundary routes:

- `GET /api/reports/mesh-health?hours=N`: findings report for the in-scope mesh
- `GET /api/scan-boundaries`, `POST /api/scan-boundaries/<name>` `{mode: 'exclude'|'stop'|null}`: limit BFS discovery. `exclude` = never poll the node (nothing reachable only through it is scanned); `stop` = poll it but don't traverse past it (like a supernode). The seed is always scanned. Boundaries live in the local SQLite `scan_boundaries` table, not CouchDB.

Time-window report routes (Reports tab; `start`/`end` as `YYYY-MM-DDTHH:MM` local time, max 32 days):

- `GET /api/reports/window?start&end&scope&link_type&search&sample_type&events&severity`
- `GET /api/reports/unreachable?start&end` (or `hours`) `&scope&link_type&search`: unreachable cause per node (unreachable.py) with real vs. explained counts. `/api/reports/window` also returns `unreachable` rows, per-node `cause`, and summary counts that exclude explained failures.
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
- `OFFLINE_MAP_DIR` (`maps`) / `OFFLINE_MAP_FILE` (`area.pmtiles`): offline Channel Map base map, written by `fetch_offline_map.py` (newest Protomaps daily build, extracted around the located nodes within `--radius-km` of their median position).
- `NOTIFY_SMTP_PASSWORD`, `NOTIFY_DISCORD_WEBHOOK`, `NOTIFY_NTFY_TOKEN`, `NOTIFY_WEBHOOK_TOKEN`: optional env overrides for notification secrets (otherwise stored in the local `notification_config` table, edited on the Diagnostics tab).
- `LINK_WATCH_ENABLED`; noise: `NOISE_JUMP_DB` (5), `NOISE_CLEAR_DB` (2), `NOISE_JUMP_SCANS` (2), `NOISE_BASELINE_HOURS` (72), `NOISE_BASELINE_EXCLUDE_MINUTES` (60), `NOISE_MIN_BASELINE_SAMPLES` (20), `NOISE_HOUR_MIN_DAYS` (3); one-way: `ONE_WAY_HIGH_LQ` (80), `ONE_WAY_LOW_LQ` (40), `ONE_WAY_SNR_OK` (15 dB), `ONE_WAY_SCANS` (2), `ONE_WAY_ESCALATE_MINUTES` (60), `ONE_WAY_FORGET_MINUTES` (60).

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

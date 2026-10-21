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
python -m py_compile app.py scanner.py config.py database.py rf_stats.py observations.py couch_client.py voip.py voip_proto.py voip_session.py agent.py mesh_diagnostics.py gateway.py reports.py link_diagnostics.py infra_audit.py change_log.py channel_map.py notify.py unreachable.py link_watch.py fetch_offline_map.py drift.py cleanup.py collector_health.py maintenance.py channel_planner.py notes.py aim_check.py site_matrix.py airtime.py power_advisor.py route_watch.py weather.py health_score.py heatmap.py swimlanes.py path_view.py spof.py site_dashboard.py capacity.py weekly_report.py link_budget.py

# Bootstrap CouchDB databases and indexes when COUCH_URL is configured
python couch_client.py

# Offline base map for the Channel Map (needs the go-pmtiles CLI in tools/ or on PATH)
python fetch_offline_map.py --dry-run
python fetch_offline_map.py --bbox=-96.64,40.37,-90.14,43.51 --maxzoom 14 --output iowa
python fetch_offline_map.py --bbox=-92.30,41.42,-90.89,42.65 --maxzoom 15 --output linn-area
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
- `mesh_diagnostics.py`: Network-wide Mesh Health Check (near-far/overpowering radios, SNR and TX/RX imbalance, one-way links, loss, VoIP MOS, flapping, noise, congestion, node health, hidden nodes, single points of failure, overlapping/adjacent channels from Channel Map conflicts: one finding per radio with a struggling RF link (with a pair to diagnose), one per mast, one summary for the rest; radios on TDWR or, when struggling, UNII-3 WiFi). Read-only over SQLite; scoped by scan boundaries. The Diagnostics tab shows a Diagnose button (RF Stats pair diagnosis) on two-node findings and on `evidence.diagnose` pairs.
- `gateway.py`: Starting-node reachability gate (cached). Scans, ping rounds, ping-all, iPerf, incident/mesh probes, and VoIP tests/sessions check it before starting and before recording a failure; if the starting node is unreachable they abort and discard instead of blaming the target.
- `reports.py`: Read-only time-window reporting (Reports tab): per-node poll health, per-link RF stats, state changes, events, VoIP, and bucketed series, filtered like the other pages.
- `link_diagnostics.py`: Pair diagnosis for the RF Stats link view. Combines both ends' LQM trackers (including a MAC-only alias when one end lost the other's hostname), promoted LQM history columns, state log, node health/config, each end's full RF neighbor list (receiver-wide loss, near-far/overpowering neighbors with a suggested power cut), and the radios around each end judged like the Channel Map (at its site within 150 m or DtD-cabled, in its antenna's beam, or aimed at it, within 25 km, seen in the last 7 days; edge-based relation same/overlap/adjacent/near/clear using each radio's own width, with the Channel Map severity), plus the link's channel against the planner's noise ranges (TDWR, busy WiFi, UNII-4, C-V2X), into ranked findings (an `ok` finding when nothing overlaps) and a verdict; RF trackers for a node cabled to that end (DtD, listed by either end) are left out of its neighbour list, an end whose radio is off gets no channel checks, and a DtD-wired pair gets a verdict that the link itself is fine (`wired`) plus `diagnose` pairs for each radio's struggling RF links, shown as buttons; the response adds `nearby_span` (each end's receive span); `run_live_checks` pings/traceroutes between the two nodes via their CGI.
- `infra_audit.py`: Infrastructure Review (Diagnostics tab): audits infrastructure nodes' stored radio settings (optionally refreshed live) for site channel plans, overlapping radios in antenna beams, azimuth vs. neighbor bearings, PtP pairing, firmware, user blocks, reachability, near-far, plus a verdict per RF link touching infrastructure.
- `drift.py`: Settings Drift (Diagnostics tab): firmware and radio-setting mismatches from stored node settings. Per RF link: mode pairing (access point <-> station, mesh <-> mesh), SSID, width, frequency (critical when both ends' settings were read in the last day, else warning), firmware major version, nightly vs. release. Per site (radios within 150 m): mixed firmware, mesh radios whose SSID/width differ from the site's other mesh radios on the same band, LQM user blocks that differ between sector radios. Network-wide: firmware classes against the newest release seen (current, older point release, previous release line, very old = 3.24 or older or two lines behind, nightly age), nodes never read. sysinfo does not expose LQM distance/SNR thresholds or the radio distance setting, so those are not compared. Scoped by `reports.resolve_scope` and scan boundaries (`mesh_diagnostics.compute_scope`).
- `airtime.py`: Sector Airtime (Diagnostics tab). Read-only. A sector is any transmitting radio with 2+ current RF neighbours (an RF tracker in either direction; pairs also cabled by DtD are ignored, since one end can keep an old RF tracker for a node whose radio is off). Per sector: clients with SNR/LQ, tx fail and retry rates from deltas of the cumulative LQM counters in `link_history` (scan samples), bitrates, packets/s and a rough airtime estimate (packets/s x `FRAME_BYTES` / bitrate: AREDN reports no channel busy); a who-hears-whom matrix and hidden pairs, confirmed by the radios' own LQM hidden-node lists (`nodes.lqm_hidden_nodes`); a contention group of same/overlapping-channel radios on the mast, heard, or facing it within `channel_map.CONFLICT_KM`. Per mast (channel_map sites): shared-channel groups of radios serving outside clients (shared airtime; cites AREDN's "Collocated Nodes" guidance), overlapping/adjacent mast pairs, clients on the same channel as another sector (in its beam or only its back lobe) and clients linked to a sector not aimed at them. Optional Channel Map what-if overrides preview a plan (and flag RF links the plan would strand); measured data stays from today's channels. Never recommends TX power changes on infrastructure.
- `change_log.py`: Change log (Changes tab): manual channel/power/antenna/firmware/other entries plus `auto` entries imported from `frequency_change`, `radio_config_change` and `firmware_change` events (SQLite `change_log`, `event_id` unique). `compare()` measures every RF link of the changed node (or the given pair) over equal windows before/after the change: LQ, quality, SNR, noise (signal - SNR), retry/fail rates from cumulative counters, bitrates, ping, node-reported peer downs, reachability, with per-metric better/worse/same verdicts and aligned series.
- `channel_map.py`: Channel Map tab data: every located radio (channel, width, edges, antenna beam; beamwidth from stored value, antenna description or gain), sites (radios within 150 m), RF links, and channel conflicts (overlap/adjacent/near, not same channel) between radios on one mast or within 25 km with at least one aimed at the other, ranked by severity. Radios inactive but seen in the last 7 days still count. What-if overrides (frequency/width per radio) are applied in memory only and compared against the stored plan.
- `channel_planner.py`: Channel planner (Channel Map tab "Plan channels"). Groups radios that must share a channel (a site's radios plus their RF-linked clients/PtP far ends when "move clients" is on), then picks one centre/width per group with the whole span inside the band (default 5650–5925). Cost: Channel Map conflict severities against every other transmitting radio in range (`channel_map._severity`, beams, mast), same-channel reuse (on one mast it is shared airtime: AREDN's channel-planning guide says colocated sectors on one channel can halve site throughput and recommends a channel per sector; back-to-back antennas >= 150° apart = warning `shared_airtime`, overlapping beams or `separate_sectors` = critical `colocated_same`), infrastructure at another site aimed at the radio either way (a narrow-beam backbone dish aimed at it must not share its channel: critical; any such infrastructure pair needs a 20 MHz guard: adjacent/overlap critical, near warning), noise ranges (TDWR + its first 10 MHz, DFS WiFi, UNII-3 heavy, UNII-4, C-V2X), measured noise (72 h median signal - SNR scaled to 20 MHz, of the radio or radios on its mast, on the part of the band a candidate covers; radios overlapping their own mast neighbours are skipped), RF links to radios outside the plan (effectively forbidden), narrower widths (lost capacity) and a small per-radio change cost. Greedy + coordinate descent from the current plan and 40 seeded random starts, then iterated local search (re-plan 2-3 groups from the best plan, up to `PERTURB_ROUNDS` within `SEARCH_BUDGET_S`); groups are built in sorted order so the same data always gives the same plans; distinct optima ranked; each top plan is checked with the Channel Map what-if. Point-to-point antennas (aimed, beam <= 45°) with no RF links are suggested off (`turn_off`, `name off` what-if lines) instead of getting a channel, unless locked or `idle_off` is false; omnis and sectors always stay on. Only frequency/width/off are suggested, never TX power. Nothing is stored.
- `notify.py`: Operator notifications (email/SMTP, Discord webhook, ntfy, generic JSON webhook). `database.log_event` hands each committed event to listeners; `notify.install()` (called only from the `__main__` startup path) registers a non-blocking listener plus a worker that applies rules (event types, min severity, scope any/infrastructure/selected/node list, channels) and anti-spam: mute-until, quiet hours, `is_suppressed()` hook (for future maintenance windows), per (rule, node/link, type) cooldown, flap hold (problem + recovery within N minutes -> one "flapped" note; recoveries only for alerted problems), and per-channel digest. Tables `notification_config`, `notification_rules`, `notifications_log` (retention = `CONNECTIVITY_LOG_RETENTION_DAYS`).
- `unreachable.py`: Unreachable classifier. Sorts every node with failed scanner polls or mesh probes in a window into a cause (starting_node_outage, placeholder, excluded, legacy_tunnel_only, pings_no_http, intermittent, stale, not_routable, behind_supernode, never_resolves, really_down). Only `really_down`/`intermittent` count as unreachable in Reports; the rest are explained. Uses `fail_reason`/`reported_by`/`via_link_type`, which the scanner records on failed polls in `node_health_history`, plus links/nodes state for older rows.
- `cleanup.py`: Cleanup of stale and junk nodes (Diagnostics tab). Lists candidates grouped by reason with a suggested action: `fake_name` (bare number / IP fragment like "10": delete), `placeholder` (`mac-…`: exclude), `legacy_tunnel_only` / `pings_no_http` / `never_resolves` (from unreachable.py: exclude), `stale` (not seen in `stale_days`: delete, or exclude while a neighbor still lists it and the scanner keeps polling it), `excluded` (un-exclude). Infrastructure, Permanent/Event-tagged, selected and starting nodes are never offered and are refused by the actions; an exclude that would cut healthy nodes off from the starting node (BFS over current links) is refused unless forced. Delete = `database.delete_node` plus the node's `node_health_history` rows.
- `link_watch.py`: Link Watch, run after every committed (never aborted) scan from `scheduled_scan`. Noise-floor watch: each radio's noise (signal - SNR from its own trackers in `link_history`) vs. a rolling baseline (same hour of day once 3+ days exist, else window median, last hour excluded). One-way link alarm: an RF pair where one end hears the other well and the other doesn't (LQ asymmetry, infinite Babel cost with good SNR, MAC-only identity, not listed at all), reusing `link_diagnostics._direction`; only judged when both ends were polled recently and they aren't wired together. Hysteresis state in SQLite `link_watch_state`; events fire on transitions only.
- `route_watch.py`: Route Watch (Diagnostics tab). Every `ROUTE_WATCH_INTERVAL_MINUTES` (own APScheduler job, never blocks scans) traceroutes key paths through the source node's traceroute CGI (`rf_stats.traceroute_via_aredn`, starting node when the source is empty): automatic paths from the starting node to one node per infrastructure site and site to site (re-synced each run; removing one disables it), plus operator paths. SQLite `route_paths`, `route_history` (hops with node name from the hostname minus `dtdlink.`/`.local.mesh`, how each hop was reached: DtD from the hostname, tunnel for 172.31.0.0/16, else the links table type; infrastructure flag), `route_state`. Baseline = most common route over `ROUTE_BASELINE_HOURS` (timed-out hops match anything, at least `ROUTE_BASELINE_MIN_RUNS` runs). Gated by gateway.py (run aborted and discarded when the starting node is unreachable); paths with an end in a maintenance window are skipped and their counters reset.
- `site_matrix.py`: Site neighbor matrix (Diagnostics tab "Site Matrix"). Every radio at a site (infrastructure site code plus radios within `SITE_METERS` of it) or in a radio list, against every radio it hears: both directions per cell from current LQM trackers (the far end's own tracker when polled, else the tracker's `rev_snr`/`rev_quality`) and scan-sample medians from `link_history` over the window (so intermittent and MAC-only neighbors appear). Flags one-way (`link_watch.evaluate_pair`, plus the active Link Watch alarm), lopsided SNR (> 10 dB), LQM blocked, MAC-only (with the probable owner from known MACs), colocated radios hearing each other, clients also heard by a site radio other than their serving one (best two-way SNR), and site radios hearing nothing. "Current" is judged as of the latest scan (`as_of`) so a stopped scanner does not make every radio look deaf. Optional what-if overrides add planned channel relations. Read-only.
- `collector_health.py`: Starting-node health (Reports tab "Starting Node" section). After every scan `scheduled_scan` starts a background task that measures the path to the starting node (DNS via the OS resolver, skipped for an IP; TCP connect; bare sysinfo over HTTP; error class) and, only when that fails, pings this PC's default gateway, storing it with the scan outcome (aborted, duration, nodes attempted/reachable) in SQLite `collector_health` (retention `NODE_HEALTH_RETENTION_HOURS`). `report()` derives outages (contiguous failures, cause: LAN down / DNS / refused / no answer / lost mid-scan ...), latency and DNS percentiles, lost scans, an hour-of-day profile and a bucketed series with scanner polls. Older history comes from `gateway_lost`/`gateway_restored` events and failed polls the old scanner stored under the starting node's URL slug (e.g. `10`).
- `weather.py`: Weather correlation (Reports tab "Weather", chart overlay). Polls the NWS API (api.weather.gov, no key; User-Agent `WEATHER_USER_AGENT`) every `WEATHER_POLL_MINUTES` from `start_scheduler` (`weather.install`), backfilling `WEATHER_BACKFILL_DAYS` on first run; station = `WEATHER_STATION` or the nearest NWS station to the median infrastructure position (via `/points` -> `observationStations`, cached in `weather_meta`). Observations in SQLite `weather_observations` (station, UTC + local time, temp, dewpoint, humidity, wind/gust/direction, precipitation 1/3/6 h, `wet` from precipitation or rain/snow/storm wording), kept `HISTORY_RETENTION_HOURS`. Failures (offline) are logged and recorded as `last_fetch.error`, never touch scans. Correlation per link direction from its own scan samples matched to the nearest observation within 45 min: SNR dry vs wet (rain fade >= 3 dB with >= 6 wet samples over >= 2 h), per-hour SNR spread and level in windy (gust or wind >= 30 km/h, >= 6 hours) vs calm (< 15 km/h) hours, hourly SNR correlation with temperature (|r| >= 0.6 over >= 8 °C) and below-freezing drop. Afternoon wind and warmth go together, so verdicts are hints.
- `health_score.py`: Health score + "what changed?" (Reports tab "Health Score"). Per bucket (15 min up to 12 h windows, else hourly or larger) node score = weighted reachability (scanner polls answered) and the RF link quality it reports, minus 20 per reboot and 5 per dropped link (max 20); RF link score = quality, SNR (5 dB = 0, 30 dB = 100) and ping loss, minus 15 per node-reported down/blocked change (max 45). Buckets in maintenance are flagged and left out of min/avg/drop. Items sorted biggest drop first, nodes that never answered last (`never_ok`). `what_changed(node, start, end, peer?)` lists events (reboots, radio/frequency/firmware changes, noise, one-way, routes, neighbours, link state, reachability, starting node), uptime resets, new links, change-log entries, collector_health failures, notes with a time and maintenance windows around the range (padded); RF neighbours' reboots/config/noise count too. Read-only.
- `heatmap.py`: Node x hour heatmap (Reports tab): reachability or RF quality per node per hour (or folded to hour of day), rows grouped by infrastructure site or callsign, plus columns where a whole group was below 50 together. Read-only.
- `swimlanes.py`: Link swimlanes (Reports tab): one lane per link pair, per bucket good / degraded (blocked or quality < 50) / flapping (2+ node-reported downs) / down (half the bucket) / unknown (scanner-only) / none, reconstructed from `link_state_log` (state before the window included) and `link_history`; reboot, config and change-log markers, maintenance; DtD pairs ignore leftover RF trackers; never-up lanes listed last. Read-only.
- `path_view.py`: Path View (Diagnostics tab): expected path (Dijkstra over usable links: good, or dropped but seen within an hour of the newest sighting; RF cost 100/quality, DtD 0.1, tunnels 3), routes Route Watch recorded for a watched path with the same ends, and `live()` traceroute from the source node's CGI (gated); each hop graded good/fair/poor/tunnel/unknown from both directions' current quality and the window median.
- `spof.py`: Single points of failure (Diagnostics tab, Channel Map overlay): bridges (pairs whose loss cuts polled nodes off from the starting node) ranked by risk = nodes behind x weakness (1 - quality for RF, 0.5 tunnel, 0.1 DtD; a pair with DtD counts as DtD), and articulation nodes; `exclude_tunnels` = internet down. Read-only.
- `site_dashboard.py`: Site dashboard (`/sites/<code>`): radios at an infrastructure site (plus radios within `infra_audit.SITE_METERS`) with channel span and health (polls, response, uptime, load, reboots), spectrum pairs, clients per sector, Route Watch paths touching the site and the expected path from the starting node, what the site depends on / carries with the internet down (spof), health scores, events, site notes, maintenance. Read-only.
- `capacity.py`: Capacity + scheduled iPerf (Reports tab "Capacity"). Every iPerf attempt (queue, manual, scheduled; failures too) goes to SQLite `iperf_results` (one-time import from `link_history.throughput_tx`). Nightly cron at `IPERF_SCHEDULE_START`: `IPERF_SCHEDULE_ROUNDS` passes over key links (RF links with an infrastructure end, both directions, plus `iperf_schedule` operator pairs; disabled pairs skipped), one test at a time sharing rf_stats' iPerf lock, none after `IPERF_SCHEDULE_END`, maintenance skipped, gated. Report per direction: capacity (median of scheduled runs over 7 nights, else 90th percentile), PHY rate, efficiency (median per-run iPerf / PHY), traffic estimate (median / p90 of LQM tx packets between scans x 600 bytes over 6 h, intervals with a test skipped), spare, nightly trend flagged `CAPACITY_DROP_PCT` below usual.
- `weekly_report.py`: Weekly report (Reports tab): headline (starting-node availability, really down / intermittent, warnings, reboots, maintenance), worst links/nodes by health score, new problems (warning/critical (type, node) not seen the week before; routine link drops/degraded only counted; says when there is no earlier week), Infrastructure Review findings and link verdicts, change log, capacity flags / backbone capacity, internet-down single points of failure, weather-flagged links; each section isolated. SQLite `weekly_reports` (JSON + text). Cron `WEEKLY_REPORT_DAY` `WEEKLY_REPORT_TIME`, sent via `notify.send` on the `weekly_report_channels` setting (channel names only; falls back to `WEEKLY_REPORT_CHANNELS`); chat channels get a short version.
- `link_budget.py`: Link budget + terrain (RF Stats diagnosis "Link budget & terrain", Diagnostics "Link Budget"). Predicted signal = TX power (`rf_txpower` or `LINK_BUDGET_TX_DBM`) + gains (stored, else by model, else 15 dBi) - horizontal off-axis loss (12 (angle/beamwidth)^2, max 25) - FSPL - knife-edge obstruction (ITU-R P.526 at the worst point) - 1 dB cable each end, against the median measured `link_history.signal`; terrain profile from Open-Meteo elevations (`ELEVATION_API_URL`, cached in SQLite `elevation_cache`, `ELEVATION_ENABLED=0` = offline), k = 4/3 earth bulge, first Fresnel zone clearance (60% wanted). Confidence low when a gain or height was assumed (heights under 2 m treated as unset). Read-only apart from the elevation cache.
- `maintenance.py`: Maintenance windows (Diagnostics tab, node page, header banner). SQLite `maintenance_windows` (start/end local times, scope `all` | `site` | `nodes`, optional `include_rf_peers` so a sector's RF clients are covered too, note, author, optional linked `change_log` entry). A link is in maintenance when either end is; each window's effective end adds `MAINTENANCE_GRACE_MINUTES`. Consumers: `notify.is_suppressed` (events for covered nodes/links, or any event during an `all` window, are logged as suppressed and never sent; their recoveries are dropped), `link_watch` (no raise/clear/escalate for covered radios/pairs, counters reset; a radio's noise baseline restarts after each window that covered it), `unreachable` (failed polls inside a window are set aside; only-maintenance failures get cause `maintenance`, explained), `reports` (window list, per-bucket shading, events flagged `in_maintenance` and left out of the warning count, maintenance failed polls moved to their own series). Logs `maintenance_scheduled`/`maintenance_started`/`maintenance_ended` system events.
- `notes.py`: Operator notes on nodes, links and sites ("tilted 2° on 10/20", "call KF9MT first"). SQLite `notes` (created by `ensure_tables()`; target_type `node` | `link` (names stored sorted) | `site`, text, author, pinned, optional `event_time` for chart markers). A node query returns its own notes, notes on any of its links and on its infrastructure site; a node+peer query returns the pair's link notes plus both nodes' and both sites'. Local operator data, never replicated to CouchDB.
- `aim_check.py`: Antenna aim check (Diagnostics tab "Antenna Aim", node page, Nodes panel, Channel Map radio panel). For each located directional radio (azimuth set with a beam under 360°, or an antenna description that says so): RF peers from current links plus 72 h of link history (SNR each way), bearing/distance to each, strongest peer and SNR-weighted bearing. Dish/panel (beam <= 45°): peer outside beam/2 + 10° with a good link (SNR >= 20) = azimuth setting wrong, with a weak link = possible misaim. Sector: clients outside its coverage, or nearer another sector's boresight on the same mast. Also azimuth unset (suggests the bearing to the strongest peer), downtilt vs. the angle from both antenna heights (no terrain), and locations: wrong longitude/latitude sign vs. the mesh, 0,0, unrelated callsigns sharing one position, and node-reported link distance vs. coordinates (when the far end agrees with the coordinates, the reporting node's own location setting is stale). Never recommends TX power. Read-only; scoped like the other reports.
- `power_advisor.py`: Power-cut advisor (node page "Power Advisor", linked prefilled from near-far findings in link diagnosis and mesh health). For a radio, every RF neighbour that hears it (its own tracker of the radio in links/link_history, falling back to the radio's `rev_snr`; cabled DtD peers' leftover RF trackers ignored): median and 10th-percentile SNR over `hours`; a cut lowers both by the cut. `ok` = median >= `POWER_ADVISOR_MIN_SNR` + `POWER_ADVISOR_MARGIN_DB` and fade >= min, `marginal` = median >= min, `lost` below. Curve for 0..20 dB, largest safe cut (every intended peer ok), comfortable cut (weakest intended peer near 30 dB), estimated bitrate (measured rate scaled by an 802.11ac MCS table), and near-far benefit at receivers where the radio is hot (link_diagnostics thresholds). Infrastructure radios are refused, listing the non-infrastructure neighbours they hear instead. "Current" is judged as of the latest scan when the scanner has stopped. AREDN sysinfo has no TX power, so cuts are relative dB (`nodes.rf_txpower` is stored if a firmware reports `meshrf.txpower`).
- `config.py`: Central configuration and environment-variable overrides.

### Frontend Components

- `templates/index.html`: Single-page app. The header page menu (`nav-menu.js`) is one drop-down button grouping the tab buttons: Network (Topology = the vis.js graph, internal tab id `network`; Map; Channel Map), Links & nodes (RF Stats, Nodes, VOIP), Analysis (Diagnostics, Reports, Changes). The header graph filters (RF only, Infrastructure only, infrastructure tag, Only selected nodes) are shown only on Topology and Map; elsewhere a badge shows when scans are limited to selected nodes. The items are still `.tab-btn[data-tab]` buttons, so `document.querySelector('.tab-btn[data-tab="…"]').click()` keeps working everywhere.
- `static/js/nav-menu.js`: Opens/closes the page menu (outside click, Escape), keeps its label in step with the clicked tab, and shows the header graph filters only on the Topology and Map tabs (`FILTER_TABS`).
- `static/js/geo-map.js`: Map tab: the Topology graph (network.js's filtered, colored `nodesDataset`/`edgesDataset`, so the header filters apply) on the Channel Map base map (`ChannelMap.ensureBase` / `addBaseLayers`). Radios within 60 m are fanned out in a pixel ring around their true position, nodes without a location are drawn at a located DtD neighbour (dashed border) or listed, tunnels/DtD/names toggles, first view and Fit = the densest 25 km cluster. Clicking a node opens the shared node side panel; clicking an RF link opens it in RF Stats. Wraps `updateNetwork` to redraw after each scan while open.
- `static/js/network.js`: vis.js graph initialization, Socket.IO message handling, node position persistence, settings, and event-log UI.
- `static/js/node-page.js`: Full node detail page, targeted node scan, per-link ping/iPerf actions, node log search, and charts.
- `static/js/nodes.js`: Nodes table, filters, detail panel, node history charts, and delete action.
- `static/js/rf-stats.js`: Chart.js graphs for quality, SNR, latency, throughput, and RF test actions.
- `static/js/link-diagnosis.js`: RF Stats pair diagnosis panel (verdict, findings, direction/node comparison, colocated radios, state log, live path check, per-direction LQM charts).
- `static/js/aim-check.js`: Antenna Aim section on the Diagnostics tab (findings, per-radio table: antenna, set azimuth/tilt, strongest peer, SNR-weighted bearing, offset, verdict, suggested azimuth), the shared aim block for the node page and Nodes detail panel (`aim` from `/api/nodes/full` and `/api/nodes/detail`), and an aim block appended to the Channel Map radio panel (wraps `ChannelMap.renderPanel`).
- `static/js/power-advisor.js`: Node page Power Advisor (cut slider prefilled from `?power_cut=N#power-advisor`, neighbour table with current/predicted SNR, status and estimated bitrate, near-far benefit, Chart.js predicted SNR vs cut with the ok and LQM-minimum lines; refusal with alternatives for infrastructure radios).
- `static/js/drift.js`: Settings Drift section on the Diagnostics tab (findings, firmware histogram, per-site table highlighting cells that differ from the site majority, RF link table). Self-initializing; reuses `InfraReview.renderFinding`.
- `static/js/airtime.js`: Sector Airtime section on the Diagnostics tab (findings, per-mast table with shared/overlapping pairs and cross-sector clients, per-sector client table, hearing matrix with hidden pairs, contention group; "Preview a channel plan" posts what-if lines or copies the Channel Map what-if).
- `static/js/notifications.js`: Notifications section on the Diagnostics tab (channels, timing, quiet hours, mute, rules, send log, per-channel test).
- `static/js/infra-review.js`: Infrastructure Review section on the Diagnostics tab (findings, per-site spectrum chart and settings table, link verdicts).
- `static/js/channel-map.js`: Channel Map tab (bundled Leaflet 1.9.4 in `static/vendor`, lazy-loaded; base map is the offline Protomaps extracts in `maps/` drawn by bundled protomaps-leaflet 5.1.0 when present, stacked least detailed first, each more detailed layer only inside its bounds and above the zoom where the layer below runs out, with online OpenStreetMap tiles drawn on top by default so the map is complete when online and failed tiles stay transparent over the offline map; layer switch: Online + offline / Offline only / Online only): radios colored by frequency, beam wedges, RF links, dashed conflict lines and mast rings, filters (scope, band, frequency range, severity), radio side panel, and what-if channel plan. Hooks its own tab button; no changes to rf-stats.js tab switching.
- `static/js/channel-planner.js`: "Plan channels" panel in the Channel Map side panel (opened by the toolbar "Plan channels" button) (site or radio list, band, widths, locked radios, move clients, compare with the what-if box): ranked plans with old -> new table, before/after spectrum, reasons and remaining issues, whole-area what-if delta; "Show on map" loads a plan into the map's what-if; "Schedule…" (confirmed) creates a maintenance window for the changed radios plus one `channel` change-log entry per radio. Widens the side panel while open.
- `static/js/cleanup.js`: Cleanup section on the Diagnostics tab (candidates grouped by reason, per-group select-all, Exclude / Un-exclude / Delete with a confirm).
- `static/js/collector-health.js`: Starting Node section of the Reports tab (status tiles, outage timeline, response-time and polls charts, hour-of-day outage profile, outage table, Check now); loaded by `ReportsModule.load()` with the Reports window.
- `static/js/weather.js`: `Weather` overlay (toggle `rf-weather-toggle` on RF Stats, `np-weather-toggle` on the node page, remembered in localStorage): adds rain bars and temperature / wind-gust lines as `$weather` datasets on right-hand axes of the quality and SNR charts, removed cleanly when off; `WeatherPanel`: Reports tab Weather section (station, latest observation, flagged links, rain / wind / temperature rankings, Fetch now), loaded with the Reports window.
- `static/js/health-score.js`, `heatmap.js`, `swimlanes.js`, `capacity.js`, `weekly-report.js`: Reports tab sections. Each registers itself in `window.ReportPanels`; `ReportsModule.load()` calls `panel.load(queryString)` after every window load (capacity and weekly report load once; Refresh for more). Health Score: score bars per bucket, click one for "what changed". Heatmap: one canvas, hover tooltip, click opens the node in the Reports detail panel. Swimlanes: SVG lanes with markers, click opens the link. Capacity: table, nightly sparkline, Chart.js run history, key-link add / unschedule, "Run iPerf pass now". Weekly report: list, text view, channel checkboxes, Generate / Send (confirmed) / Delete.
- `static/js/path-view.js`, `spof.js`, `link-budget.js`: Diagnostics sections Path View (also opened from Route Watch "Path" and SPOF), Single Points of Failure ("Show on map" draws the ranked links on the Channel Map with a remove control) and Link Budget (also used lazily by the RF Stats diagnosis, with a terrain / Fresnel Chart.js profile). `link-diagnosis.js` also handles the `/#diagnose/<a>/<b>` deep link.
- `templates/site.html`, `static/js/site-page.js`: Site dashboard page; reuses `InfraReview.renderSpectrum`, the Notes widget (site notes) and the maintenance banner. Linked from the node page (infrastructure site) and above the Infrastructure Review.
- `static/js/maintenance.js`: Maintenance section on the Diagnostics tab (create form with scope/site/nodes, now + 1/2/4/8 h or start/end, change-log option; list with End now / +1 h / Cancel), the header banner on the main and node pages while a window is active (polled every minute plus the `maintenance_update` Socket.IO event), and the node page "Start maintenance" quick action (this node or its site).
- `static/js/link-watch.js`: Link Watch section on the Diagnostics tab (one-way alarms with a Diagnose button into RF Stats, noise table, per-radio noise series with baseline and hour-of-day profile).
- `static/js/route-watch.js`: Route Watch section on the Diagnostics tab (paths with status, route now as hop chips: not-usual hops highlighted, tunnels red, how each hop was reached; usual hop count, 24 h hop-count sparkline, Routes history per path, Trace now, Remove, add-path form).
- `static/js/site-matrix.js`: Site Matrix section on the Diagnostics tab (site or radio list, window, optional Channel Map what-if): heatmap of row-hears / column-hears SNR colored by the weaker direction, flag markers, row/column summaries, findings; clicking a cell or Diagnose opens the pair in RF Stats.
- `static/js/rf-channel.js`: Shared node channel display (centre/width/edge frequencies and channel numbers) and connected-peer channel relation (overlap/adjacent/near/same/clear) for the Nodes tab detail panel and node page; values come from `infra_audit.annotate_link_rf` via `/api/nodes/detail` and `/api/nodes/full` (`node.rf_span`, `link.peer_rf`).
- `static/js/changes.js`: Changes tab (change list, filters, add form, before/after metrics and overlay charts; `/#changes/<id>` deep link). Also registers a `changeMarkers` Chart.js plugin that RF Stats uses to draw change times on link charts.
- `static/js/notes.js`: Shared notes widget (`Notes.mount(el, {node, peer?, site?, compact?, peers?})`, add/edit/pin/delete, pinned + latest few in compact mode) used on the node page Notes section, the Nodes tab detail panel, the network side panel, the RF Stats link diagnosis (pair + both nodes, separate from computed findings) and the Channel Map radio panel. Caches pinned notes for the Nodes table flag (`Notes.indicator`), and `Notes.markCharts()` sets `chart.$noteMarkers` (drawn as dotted amber lines by the `changeMarkers` plugin in changes.js) on RF Stats link charts, Nodes tab quality/SNR charts and node page charts. All note text is escaped.
- `static/js/node-changes.js`: Node page Changes section with an inline Add change form.
- `static/js/unreachable-cause.js`: Shared "why unreachable" block for the node page and Nodes detail panel (`unreachable_cause` from `/api/nodes/full` and `/api/nodes/detail`).
- `static/js/reports.js`: Reports tab: start/end window scrubbing (presets, prev/next, slider), filters, timeline, tables, and node/link drill-down charts.
- `static/css/style.css`: Application layout and visual styling.

## Current Data Flow

1. APScheduler or a user action triggers `scheduled_scan()` in `app.py`.
2. `scanner.discover_network()` performs BFS traversal from the configured seed node. It fetches everything first (no DB writes), probing the seed after every wave.
3. If the seed (the collector's gateway onto the mesh) is unreachable at the start or stops answering mid-scan, the scan is aborted and all results are discarded: no node/link/health writes, no timeout-based aging, only a `gateway_lost` event (on transition) and a heartbeat with `scan_aborted: true`. Targeted scans are gated on the configured seed too. A seed that answers only bare sysinfo (full sysinfo timed out: no neighbour list) also aborts the scan (`abort_reason: seed_degraded`, a `scan_incomplete` event on transition, not `gateway_lost`), since committing it would age every link out as dropped. A committed scan that reaches fewer than half of the nodes active before it (10+ active) holds link/node timeout aging for that one scan (`aging_held`); a second short scan in a row ages normally; `scan_complete_again` when scans recover. Otherwise `scanner.py` commits the results to legacy SQLite state through `database.py`.
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
Maintenance window routes (maintenance.py):

- `GET /api/maintenance?days=14` (windows with status active/grace/upcoming/past and covered nodes, plus `active`, `grace_minutes`, `sites`), `GET /api/maintenance/active`
- `POST /api/maintenance` `{scope, site?, nodes?, start?, end? | duration_minutes?, note?, author?, include_rf_peers?, change_log?}`
- `PATCH /api/maintenance/<id>` `{end_now: true} | {extend_minutes} | {end} | {note}`, `DELETE /api/maintenance/<id>`
- `/api/reports/window` adds `maintenance` (windows), per-event `in_maintenance`, per-bucket `in_maintenance`/`maintenance_warnings`/`maintenance_unreachable`, per-node `maintenance_unreachable`, and summary `maintenance_windows`/`maintenance_events`/`maintenance_unreachable_polls`; `/api/nodes/full/<name>` adds `maintenance` `{active, site}`.

Notes routes (notes.py):

- `GET /api/notes?node&peer&site&pinned&limit`: newest first, pinned first; see `notes.list_notes` for which notes a node / pair / site query includes
- `POST /api/notes` `{target_type?: node|link|site, node_name?, peer_node?, site?, text, author?, pinned?, event_time?}` (a site note may name an infrastructure node instead of the site), `PATCH /api/notes/<id>` `{text?, pinned?, event_time?, author?}`, `DELETE /api/notes/<id>`

Notification routes (secrets always masked as `********`; posting the mask back keeps the stored value, an empty string clears it):

- `GET/POST /api/notifications/settings`, `GET/POST /api/notifications/rules`, `DELETE /api/notifications/rules/<id>`
- `POST /api/notifications/test` `{channel}`, `GET /api/notifications/log?limit`

Infrastructure routes:

- `GET /api/nodes/infrastructure`, `POST /api/nodes/infrastructure/<name>` `{infrastructure: bool, site?}`: mark towers/high sites. Site defaults to the location code in the name (`w0gq-col-...` -> `COL`); site names come from the `site_names` setting over defaults (COL Collins Tower, MMC Mt Mercy Hospital, EOC Linn County EOC). Stored in SQLite `infrastructure_nodes`, exposed as `is_infrastructure` on node and graph JSON.
- `GET /api/reports/drift?scope=all|infrastructure|infrastructure-connected|selected|selected-connected`: settings drift report (`findings`, `links`, `sites` with `norms` and per-radio `differs`, `firmware` histogram, `latest_release`)
- `GET /api/reports/aim?scope=` (or `?node=<name>` for one radio, ignoring scope): antenna aim findings, per-radio rows (`peers` with bearing/offset/SNR, `main_peer`, `best_bearing`, `offset`, `verdict`, `suggested_azimuth`, `expected_elevation`, `location_problem`) and verdict counts
- `GET /api/power-advisor/<node>?cut_db&hours=24`: `neighbours` (snr, snr_fade, snr_after, status, rate, rate_after, intended), `curve` (0..20 dB), `suggested_cut_db`, `comfortable_cut_db`, `near_far`, `thresholds_by_neighbour`, `as_of`; infrastructure radios return `refused` with `alternatives`. Near-far findings carry `power_advisor: {node, cut_db}` (link diagnosis on the finding, mesh health in `evidence`) when the loud end is not infrastructure.
- `GET /api/reports/airtime?scope&hours=24`, `POST /api/reports/airtime` `{overrides, scope?, hours?}`: sector airtime report (`sectors`, `masts`, `findings`, `counts`, `data_notes`, `what_if`, `what_if_breaks`)
- `GET|POST /api/reports/infrastructure` (`POST {refresh: true}` re-reads settings from the nodes first)
- `GET /api/map/base`: `{offline, layers: [{name, url, min_zoom, max_zoom, bounds, size_bytes, updated}]}` for every `.pmtiles` file in `OFFLINE_MAP_DIR` (from each file's header), least detailed first; `GET /maps/<file>.pmtiles` serves them with HTTP range requests
- `GET /api/channel-map?scope&max_km&center&include_inactive`, `POST /api/channel-map/what-if` `{overrides: {name: {frequency, chanbw} | {off: true}} | "name freq width" / "name off" lines, scope?, ...}`: channel map data and a non-persistent what-if recompute (response adds `what_if` with baseline counts, resolved/introduced/changed conflicts)
- `POST /api/channel-planner` `{site | radios, band?: [low, high], widths?: [..] | {name: [..]} (default: keep each group's width), locked?, move_clients? (true), idle_off? (true), separate_sectors? (false), top? (3), compare?: "name freq width" lines}`: `groups`, `turn_off`, `noise_samples`, `current` (score of today's channels), ranked `plans` (per radio old/new channel and span, per group reasons and issues, counts, `what_if_text`, Channel Map `what_if` delta), optional `compare` (the operator's plan scored the same way, with split groups and out-of-band radios), `notes`, `elapsed_ms`

Health score, heatmap, swimlanes (window arguments as for `/api/reports/window`):

- `GET /api/reports/health-score?start&end&kind=node|link&scope&link_type&search`, `GET /api/reports/what-changed?node&peer?&start&end&pad_minutes?`
- `GET /api/reports/heatmap?start&end&metric=reach|lq&fold=0|1&scope...`, `GET /api/reports/swimlanes?start&end&scope&link_type&search`

Paths, single points of failure, sites, link budget:

- `GET /api/path?source?&target&hours`, `POST /api/path/live` `{source?, target}` (503 when the starting node is unreachable)
- `GET /api/reports/spof?tunnels=0|1&hours`
- `GET /sites/<code>`, `GET /api/sites`, `GET /api/sites/<code>?hours` (404 with `sites` when unknown)
- `GET /api/link-budget/<a>/<b>?hours`, `GET /api/reports/link-budget?hours`

Capacity and weekly report:

- `GET /api/capacity?hours=168`, `GET /api/capacity/link?source&target&hours`, `POST /api/capacity/links` `{source, target}` (add) or `{source, target, enabled}`, `POST /api/capacity/run` (one pass now in the background; 409 while running)
- `GET /api/weekly-reports`, `GET /api/weekly-reports/<id>`, `POST /api/weekly-reports` `{send?}`, `POST /api/weekly-reports/<id>/send` `{channels?}`, `DELETE /api/weekly-reports/<id>`, `POST /api/weekly-reports/channels` `{channels}`

Cleanup routes (`names` is a list; protected nodes are always refused and listed in `refused`):

- `GET /api/cleanup/candidates?stale_days=30&hours=72`
- `POST /api/cleanup/exclude` `{names, force?}` (force allows an exclude that cuts healthy nodes off), `POST /api/cleanup/unexclude` `{names}`, `POST /api/cleanup/delete` `{names, force?}` (force allows deleting active nodes)

Weather routes (`start`/`end` as `YYYY-MM-DDTHH:MM`, or `hours`):

- `GET /api/weather?start&end` (or `hours`, default 72): `status` (station, counts, latest observation, last fetch/error) and `observations`
- `GET /api/weather/correlation?source&target?&hours=168`: one link (both directions) or, without target, every RF tracker of one radio: per direction rain/wind/temperature stats and `verdicts`
- `GET /api/weather/summary?hours=168` (or `start&end`): link directions ranked by rain drop, wind spread and temperature correlation, `flagged`, `notes`
- `POST /api/weather/refresh`: fetch from NWS now (409 when disabled, 502 when unreachable)

Starting-node health routes:

- `GET /api/collector/health?start&end` (or `hours`, default 24): `summary` (state, last answer, latency/DNS/connect percentiles, outages, lost scans, availability), `outages`, `series`, `hour_profile`, `recent` checks
- `POST /api/collector/health/check`: measure the path to the starting node now and store it (kind `manual`)

Link Watch routes:

- `GET /api/reports/noise?hours=72&days=7`: every radio's current noise, baseline, delta and alarm state; with `&node=<name>`, that radio's series, baseline and 24-hour profile
- `GET /api/reports/one-way?hours=24`: `active` (alarmed), `pending` (one-way now, not yet confirmed) and `cleared` (within `hours`) RF pairs with reasons and both ends' LQ/quality/SNR/cost
- `GET /api/reports/site-matrix?site=COL&hours=24` (or `radios=a,b,c`), `POST` the same as JSON plus `overrides` (what-if lines): `rows`, `columns`, `cells[row][col]` (snr, their_snr, quality, their_quality, lq, signal, noise, status, blocked_reason, relation, planned_relation, distance_m, a_beam/b_beam, flags, window medians), `findings`, `counts`, `as_of`

Link Watch events (transitions only, with hysteresis; also categorized in Reports): `noise_jump` (warning), `noise_cleared` (info), `one_way_link` (warning, or critical when it touches infrastructure or is still one-way after `ONE_WAY_ESCALATE_MINUTES`), `one_way_link_cleared` (info; also raised when a flagged pair stops being evaluated for `ONE_WAY_FORGET_MINUTES`). Format: noise events have `node_name` = the radio; one-way events have `node_name` = the deaf end and details starting `RF link to <peer>: ...` (same shape as `link_dropped`, matched by notifications).

Route Watch routes and events (`route_watch.py`; events on transitions only, after `ROUTE_CHANGE_RUNS` runs in a row, `node_name` = path target, details start `Route <label>: ...`):

- `GET /api/routes?hours=24`: paths with status (learning/normal/changed/detour), last trace (hops, off-backbone hops), baseline, reasons, hop-count series
- `GET /api/routes/<id>/history?hours=24`: runs and the routes the path switched to (hops marked against the baseline)
- `POST /api/routes` `{source? (empty = starting node), target, label?}`, `DELETE /api/routes/<id>` (manual: deleted with history; auto: disabled), `POST /api/routes/<id>/run` (trace now; 503 when the starting node is unreachable)
- Events: `route_changed` (info: differs from the baseline), `route_detour` (warning: `ROUTE_DETOUR_HOPS`+ more hops, or a tunnel hop / non-infrastructure node the baseline didn't use), `route_restored` (info: back on the baseline, or the new route became the usual one). Default notification rule "Route detour" (new installs only; rules are seeded once).

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
- `maintenance_update` (`{active}` after a window is created, changed or deleted)
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
- `OFFLINE_MAP_DIR` (`maps`) / `OFFLINE_MAP_FILE` (`area.pmtiles`, the script's default `--output`): offline Channel Map base maps, all files in the folder layered by detail, written by `fetch_offline_map.py` (newest Protomaps daily build, extracted around the located nodes within `--radius-km` of their median position).
- `NOTIFY_SMTP_PASSWORD`, `NOTIFY_DISCORD_WEBHOOK`, `NOTIFY_NTFY_TOKEN`, `NOTIFY_WEBHOOK_TOKEN`: optional env overrides for notification secrets (otherwise stored in the local `notification_config` table, edited on the Diagnostics tab).
- `LINK_WATCH_ENABLED`; noise: `NOISE_JUMP_DB` (5), `NOISE_CLEAR_DB` (2), `NOISE_JUMP_SCANS` (2), `NOISE_BASELINE_HOURS` (72), `NOISE_BASELINE_EXCLUDE_MINUTES` (60), `NOISE_MIN_BASELINE_SAMPLES` (20), `NOISE_HOUR_MIN_DAYS` (3); one-way: `ONE_WAY_HIGH_LQ` (80), `ONE_WAY_LOW_LQ` (40), `ONE_WAY_SNR_OK` (15 dB), `ONE_WAY_SCANS` (2), `ONE_WAY_ESCALATE_MINUTES` (60), `ONE_WAY_FORGET_MINUTES` (60).
- `ROUTE_WATCH_ENABLED`, `ROUTE_WATCH_INTERVAL_MINUTES` (15), `ROUTE_BASELINE_HOURS` (24), `ROUTE_BASELINE_MIN_RUNS` (3), `ROUTE_CHANGE_RUNS` (2), `ROUTE_DETOUR_HOPS` (2), `ROUTE_HISTORY_RETENTION_DAYS` (31): Route Watch.
- `MAINTENANCE_GRACE_MINUTES` (10): alarms and notifications stay held this long after a maintenance window ends.
- `POWER_ADVISOR_MIN_SNR` (15, AREDN LQM's default minimum SNR) / `POWER_ADVISOR_MARGIN_DB` (6): power advisor thresholds.
- `IPERF_SCHEDULE_ENABLED` (true), `IPERF_SCHEDULE_START` ("02:00"), `IPERF_SCHEDULE_END` ("04:30"), `IPERF_SCHEDULE_ROUNDS` (2), `IPERF_SCHEDULE_GAP_SECONDS` (30), `CAPACITY_DROP_PCT` (25): overnight iPerf and capacity flags.
- `WEEKLY_REPORT_ENABLED` (true), `WEEKLY_REPORT_DAY` ("mon"), `WEEKLY_REPORT_TIME` ("07:00"), `WEEKLY_REPORT_CHANNELS` (empty = save only; the Reports tab setting overrides).
- `ELEVATION_ENABLED` (true), `ELEVATION_API_URL` (Open-Meteo), `ELEVATION_TIMEOUT_SECONDS` (15), `LINK_BUDGET_TX_DBM` (25): link budget terrain and assumed TX power.
- `WEATHER_ENABLED` (true), `WEATHER_USER_AGENT` ("aredn-monitor (local)"; NWS asks for a contact, e.g. "aredn-monitor (you@example.com)"), `WEATHER_STATION` (empty = nearest NWS station), `WEATHER_POLL_MINUTES` (30), `WEATHER_BACKFILL_DAYS` (7), `WEATHER_TIMEOUT_SECONDS` (15).

Settings edited through `/api/settings` persist to SQLite and currently affect legacy scanner behavior.

## Infrastructure Nodes

Infrastructure nodes (towers/high sites, marked like Selected nodes) appear as a filter on every page, as diamonds on the graph, and change diagnostics: never recommend lowering an infrastructure node's TX power; move near-far fixes to the non-infrastructure end. The scanner stores each node's reported radio settings (`rf_mode`, `rf_ssid`, `rf_chanbw`, antenna azimuth/elevation/height/gain/beamwidth, LQM user blocks, LQM hidden-node list) and logs a `radio_config_change` event when mode, SSID or width changes. PtP roles: `ptp`/`ptmp` = access point, `sta` = station, `adhoc` = mesh.

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

# AREDN Network Monitor

A web application for monitoring, troubleshooting and planning AREDN (Amateur Radio Emergency Data Network) mesh networks: live topology and maps, RF link health, routing, services and phones, alerts to node owners, channel planning, reports and a wall display.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.0-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Accounts, permissions and alerts](#accounts-permissions-and-alerts)
- [Requirements](#requirements) / [Installation](#installation) / [Usage](#usage) / [Docker](#docker)
- [Configuration](#configuration)
- [Project structure](#project-structure) / [API](#api-endpoints)

## How it works

One PC on the mesh (the *collector*) scans the network on an interval, starting from a seed node (the *starting node*) and walking every neighbour it finds (BFS over AREDN `sysinfo.json`). Results go into a local SQLite database, which every page and report reads.

- **Gated on the starting node:** if this PC can't reach its starting node, a scan, ping round, trace or check is aborted and thrown away instead of marking the whole mesh down. The Reports page "Starting Node" section shows outages on this side separately.
- **Extra sources, all optional:** traceroutes through node CGI pages, pings and iPerf, SSH into infrastructure radios (key login only), a Prometheus that already scrapes AREDN's `/cgi-bin/metrics`, NWS weather, syslog, and phone/PBX lists.
- **Distributed path (optional):** with `COUCH_URL` set, scans also write append-only observation documents to CouchDB so several collectors can be combined. A node one collector can't see is not automatically down; it is only not visible from that collector.

All times are this PC's local time (Central by default) and are labelled with the zone.

## Features

The header menu groups the pages; a shared filter bar under it (Nodes: All / Selected / Infrastructure + neighbours, Tag, Link type, Time from 1 h to 1 year, Search) applies to every page that can use it and is kept in the page URL.

### Overview

- **Status** (home page): needs-attention tiles (starting node, Mesh Health findings, one-way links, noise, route detours, radio alarms, services down, phones down, silenced services, new nodes, maintenance); **What's Happening** ticker of incidents, maintenance, warnings and recoveries, each line linking to the page for that problem; **Status notes** (pinned operator messages with a start and end, also shown on the wall and the club page); Mesh Health Check; Link Watch (noise-floor jumps, one-way links); Node Uptime; On the Air tape; New Nodes; Service Checks.
- **Incidents:** automatic incidents for a lost starting node, a site outage (every radio at a site down; cause power or network) and nodes cut off behind one failed link, so one alert goes out per incident instead of one per node; manual incidents with a timeline, notes and linked changes.
- **Agencies board** (`/board/agencies`) and **Wall display** (`/wall`): one tile per served agency or site, ticker line, plain-language details, the mesh on a real map, 24 h on-the-air tape, storm banner; full screen with F.

### Network

- **Topology:** vis.js graph, link colour by quality and pattern by type (RF, DtD, tunnel, WireGuard), supernodes, service icons, firmware mismatches, saved layout.
- **Map:** the topology on a real map (offline Protomaps extract and/or online tiles) with overlays for RF quality, single points of failure, Route Watch paths, channel conflicts, "what if this node goes down" and playback over time.
- **Channel Map:** every radio by frequency with antenna beams, RF links and channel conflicts; what-if channel plans.
- **Spectrum:** frequency across, locations down: what is free, partly used or busy at each site from WiFi scans and nearby mesh radios; history ("as of"), scan reminders; a reference list of **who else uses these frequencies** (WiFi/U-NII, ISM, radars, cellular/CBRS, C-V2X, drone video, satellites, with what shares spectrum with ham/AREDN) and WiFi/AREDN channel tables; the WiFi scan results.
- **Sites:** a dashboard per infrastructure site (radios, health, clients per sector, paths, what it depends on, spectrum waterfall, notes, maintenance).

### Nodes and links

- **Nodes:** table and detail panel; a **full node page** per node (`/nodes/<name>`) grouped into Overview, Radio, Links, History and Tools: link health, services with their checks and who they alert, alignment, power advisor, Prometheus and SSH radio details, interface traffic, syslog, changes, notes, setup checklist.
- **Links:** RF statistics, ping/iPerf, pair diagnosis with ranked findings and a verdict, link budget and terrain, link stability (flapping and asymmetric links).
- **VoIP & Phones:** phone and PBX reachability pings with alarms, continuous call quality (jitter, MOS) per node, two-ended streamed voice tests.
- **Services & Devices:** every advertised service by category with its check state, devices on each node's LAN, and **Silence / Ignore / Enable** per service or device (with a "show silenced and ignored" filter).

### Checks

- **Diagnostics:** every check on one page: the Mesh Health sections from Status, all of Radio & RF, Routing and Setup, Link Stability and the WiFi Survey, with a side menu to jump between groups. The same sections stay on their own pages.
- **Radio & RF:** sector airtime, site neighbour matrix, antenna aim, link budget and link potential, ghost trackers, per-link alert rules, slow degradation, noise sources.
- **Routing:** Route Watch (traceroutes of key paths, detours, flapping, "why did it change?", traffic along a route), path view, single points of failure, Babel costs, tunnels.
- **Setup:** infrastructure review, settings drift (firmware and radio settings), **node hygiene** score with fixes per owner, cleanup of stale and junk nodes.

### Planning

- **Channel Planning:** a channel planner that scores plans against conflicts, measured noise, WiFi scans and protected radios; WiFi surveys and scan results; rollouts that track each radio's change and its links coming back.

### History

- **Reports:** any time window: node and link tables, events, unreachable causes, timeline, starting-node health (response time, DNS, outages), weather correlation, health score, heatmap, link swimlanes, capacity (nightly iPerf), VoIP (phone reachability, call tests).
- **Weekly report:** saved and optionally sent every week: headline, **site uptime**, **services** (availability and outages), **node hygiene** (average and trend, most common fixes, action items for every node by owner), site outages, worst links and nodes, new problems, infrastructure review, changes with before/after verdicts, capacity, single points of failure, weather.
- **Changes:** change log (manual, plus automatic from scans, syslog and SSH), before/after comparison, Refresh to reload with the filters.
- **Node Logs:** syslog search with filters, rates and CSV export.
- **Club report** (`/report/club`) and **club status page** (`/export/club-status`): printable and embeddable summaries.

### Admin

- **Maintenance** windows (alarms held, reports annotated), **Notifications** (e-mail, Discord, ntfy, webhooks; rules, quiet hours, digests, acknowledgement and escalation, suppressions with review dates), scanning settings, **Account & alerts** (`/account`), and `/admin` (login, database care with backups, SSH key and status, collectors, audit log).
- **Field mode** (`/field/`): installable phone page with a map, node list and an antenna alignment meter.
- **Embeds:** `/embed/node/<name>`, `/embed/site/<code>` and SVG badges for other websites.

## Accounts, permissions and alerts

- **Reading** every page needs no login. **Changes** (scans, settings, deletes and so on) need a login once an admin password is set on `/admin` (or `ADMIN_PASSWORD`, or a trusted proxy header).
- **Users:** every node owner gets an account automatically: the user id is the callsign at the start of the node name (`kf9mt-home-litebeam` -> `KF9MT`). New accounts have **no password**, so they can't log in until an admin sets one on `/account`. Admins can add users who own no node, give or take away **Admin** (applies at once) and put users on the **Infra team**.
- **Logging in:** the login box takes a callsign and password; leave the callsign empty to use the admin password.
- **Alerts to people:** each service alerts its **node owner** by default. On a node page's Services table, "Alert me" / "Stop alerting me" changes your own alerts and admins can add (+) or remove (x) anyone. Each user sets an e-mail address and/or an ntfy topic on `/account`, picks which services alert them, sends a test and changes their password. A password is required to configure alerts.
- **Infrastructure team:** members get alerts for events on infrastructure nodes, choosing groups (nodes, links, radio, services, phones, routing, incidents) and a minimum severity.
- Personal alerts go through the same rules as operator notifications: maintenance windows, open incidents (one alert per incident), suppressions, flap hold and cooldown. E-mail uses the SMTP server set on the Notifications page; ntfy works without it.
- **Silence and ignore:** admins can silence a service or device (keep checking, no alarm) for 1 h, 6 h, 1 day, 1 week, 1 month or until enabled again, or ignore it (stop checking). A node owner can silence and re-enable their own node's services.
- **Backups:** nightly database backups (and Back up now) on `/admin`; admins can download them. A backup holds e-mail addresses and password hashes, so keep downloaded copies private.

## Requirements

- Python 3.11+ (the Docker image uses 3.12)
- Access to an AREDN mesh network
- Optional: Apache CouchDB 3.x for distributed observation storage

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/gskuhlman/aredn-distributed-monitor.git
   cd aredn-distributed-monitor
   ```

2. Create a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

   On Windows:

   ```powershell
   venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure the starting node in `config.py`, through the web settings panel, or by environment variable:

   ```bash
   STARTING_NODE=http://localnode.local.mesh/cgi-bin/sysinfo.json?lqm=1&hosts=1&services=1&services_local=1
   ```

## Usage

Start the application directly:

```bash
python app.py
```

Open `http://localhost:5000`.

The network scans automatically when auto-scan is enabled. You can also click **Scan now**, open **Settings**, or pick a page from the header menu. Run the tests with `python -m pytest` (each test uses its own temporary database; the tests never touch the live database or the network).

### Docker

The monitor also runs in Docker on a Linux workstation on the mesh, with host networking and the database, backups, maps and SSH key in a data folder:

```bash
docker compose up -d --build
```

See [docs/DOCKER.md](docs/DOCKER.md) for host requirements, moving an existing database, Portainer and day-to-day commands.

### Running as a systemd Service

`aredn-monitor.service` is included for persistent background running. It supports both **user-level** and **system-level** systemd; user-level is recommended when running as the application user.

**User service (recommended):**

```bash
systemctl --user daemon-reload
systemctl --user enable --now aredn-monitor
```

**System service (requires root / `sudo`):**

```bash
sudo cp aredn-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aredn-monitor
```

If you need environment variables (e.g. `COUCH_URL`, `STARTING_NODE`), add an `Environment=` line to the service file or point `EnvironmentFile=` to a file with `KEY=VALUE` entries.

**Note:** The previous service file included `ProtectSystem=strict` and `ProtectHome=read-only` hardening that caused immediate crashes. The current version removes these directives so the service starts cleanly.

## Configuration

Edit `config.py` or set environment variables.

| Setting | Default | Description |
|---------|---------|-------------|
| `STARTING_NODE` | `http://localnode.local.mesh/...` | Seed node for discovery |
| `POLL_INTERVAL_SECONDS` / `POLL_INTERVAL` | `60` | Seconds between automatic scans |
| `REQUEST_TIMEOUT_SECONDS` / `REQUEST_TIMEOUT` | `10` | Timeout for AREDN sysinfo requests |
| `MAX_DEPTH` | `5` | Maximum hops from starting node |
| `LINK_TIMEOUT_SECONDS` / `LINK_TIMEOUT` | `300` | Seconds before legacy SQLite status marks nodes/links inactive |
| `LINK_REMOVE_AFTER_SECONDS` / `LINK_REMOVE_AFTER` | `3600` | Seconds before old dropped links are hidden from the graph display |
| `NEW_NODE_DAYS` | `30` | Days used to suppress repeat "new node" announcements for recently seen nodes |
| `DATABASE_RETENTION_DAYS` | `90` | Days to keep inactive nodes and related local state in SQLite |
| `SHOW_TUNNELS` | `false` | Show tunnel/WireGuard links |
| `DATABASE_PATH` | `aredn_monitor.db` | SQLite database path |
| `QUALITY_GOOD` | `85` | Threshold for good quality |
| `QUALITY_POOR` | `50` | Threshold for poor quality |

Many more settings (Route Watch, SSH collection, Prometheus, weather, weekly report, backups, phone pings, service checks and alarm thresholds) are listed with their defaults in `config.py` and in the Configuration section of `CLAUDE.md`. Optional offline base maps for the maps come from `fetch_offline_map.py` (see `CLAUDE.md`).

### Optional Distributed CouchDB Settings

Leave `COUCH_URL` unset for legacy SQLite-only operation.

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `COLLECTOR_ID` | `collector-local` | Stable collector identifier used in observation document IDs |
| `COLLECTOR_SITE` | `Local Collector` | Human-readable collector site |
| `COLLECTOR_VERSION` | `0.1.0` | Collector version written to heartbeat docs |
| `COUCH_URL` | unset | CouchDB base URL, for example `http://admin:password@localhost:5984` |
| `COUCH_DB` | `aredn_monitor` | Replicated monitoring database |
| `LOCAL_CONFIG_DB` | `aredn_local_config` | Local-only config database |
| `STORE_RAW_SYSINFO` | `false` | Reserved for optional raw sysinfo retention |

Bootstrap CouchDB databases and indexes:

```bash
python couch_client.py
```

This verifies CouchDB, creates the monitor database, creates the local config database, creates `_replicator`, and installs Mango indexes.

### Collector-Only Node

Use this mode for a node whose main purpose is data collection rather than serving as the primary dashboard. It still runs the Flask process because the scanner, scheduler, health endpoint, and CouchDB writer live in the same app.

1. Install the application as usual and make sure the host can reach its local AREDN node.

2. Set a stable collector identity and point it at CouchDB:

   ```bash
   export COLLECTOR_ID=site-east-collector
   export COLLECTOR_SITE="East Site"
   export STARTING_NODE=http://localnode.local.mesh/cgi-bin/sysinfo.json?lqm=1\&hosts=1\&services=1\&services_local=1
   export COUCH_URL=http://admin:password@couchdb-host:5984
   export COUCH_DB=aredn_monitor
   ```

3. Bootstrap CouchDB once from any collector with database admin credentials:

   ```bash
   python couch_client.py
   ```

4. Run the collector with a local-only listener if operators do not need to browse to it remotely:

   ```bash
   HOST=127.0.0.1 PORT=5000 python app.py
   ```

   For a systemd deployment, set the same environment variables in `aredn-monitor.service` or an EnvironmentFile and start the service with `systemctl`.

5. Verify collection:

   ```bash
   curl http://127.0.0.1:5000/api/health
   curl http://127.0.0.1:5000/api/collectors
   ```

Collector-only deployments should keep automatic scanning enabled through the Settings panel so scans continue on the configured interval. The local web UI can still be used for troubleshooting, but the durable distributed data is the append-only CouchDB observation stream.

## Project Structure

```text
aredn-distributed-monitor/
|-- app.py                # Flask app: routes, Socket.IO, scheduler jobs
|-- config.py             # settings and environment overrides
|-- scanner.py            # BFS discovery, gated on the starting node
|-- database.py           # SQLite current state, events, history
|-- auth.py, users.py     # login, admin power, per-callsign accounts
|-- notify.py             # operator notifications; user_alerts.py: alerts to people
|-- *.py                  # one module per feature (route_watch, channel_planner, incidents, ...)
|-- static/js/            # one script per page or section
|-- static/css/style.css
|-- templates/            # index.html (single-page app), node, site, wall, account, admin, ...
|-- tests/                # pytest suite with fixture databases
|-- docs/DOCKER.md        # Docker deployment
|-- Dockerfile, docker-compose.yml, aredn-monitor.service
```

`CLAUDE.md` describes every module, page, setting and route in detail.

## Distributed Observation Model

When CouchDB is configured, each scan writes immutable documents with deterministic IDs:

- `node_observation`: one per attempted node poll, including failed polls
- `link_observation`: one per observed link from sysinfo/LQM data
- `service_observation`: one per service seen on a node
- `collector_heartbeat`: one per scan cycle

Document IDs include collector ID, UTC poll timestamp, and target slug. This avoids normal CouchDB conflicts and makes retries idempotent. These observations are the source for the planned distributed reporting model; the existing dashboard still reads SQLite-derived current state until reporting endpoints are expanded.

## API Endpoints

Every page is backed by a JSON API under `/api/`. The core routes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Application health and optional CouchDB connectivity |
| `/api/collectors` | GET | Collector heartbeat summary |
| `/api/network` | GET | Graph data for the topology |
| `/api/nodes`, `/api/nodes/all`, `/api/nodes/full/<name>` | GET | Nodes and full node details |
| `/api/nodes/scan/<name>` | POST | Targeted scan of one node |
| `/api/links`, `/api/links/active` | GET | Links |
| `/api/scan` | POST | Scan now |
| `/api/events` | GET | Event log |
| `/api/reports/window` | GET | Reports for a time window |
| `/api/weekly-reports` | GET/POST | Saved weekly reports; generate one |
| `/api/services/checks`, `/api/services/mute` | GET, POST/DELETE | Service checks; silence / ignore / enable |
| `/api/service-alerts` | GET/POST | Who each service alerts |
| `/api/me`, `/api/users` | GET/PATCH, GET/POST | Your account; users (admin) |
| `/api/status-notes` | GET/POST | Status notes |
| `/api/admin/database/backups/<file>` | GET | Download a backup (admin) |

The full list (Route Watch, channel planner, spectrum, incidents, phones, SSH, Prometheus, weather and more) is in `CLAUDE.md` under REST API. Pages: `/nodes/<name>`, `/sites/<code>`, `/wall`, `/board/agencies`, `/account`, `/admin`, `/field/`, `/report/club`, `/export/club-status`.

## WebSocket Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `scan_started` | Server to client | Scan has begun |
| `scan_complete` | Server to client | Scan finished with results |
| `network_update` | Server to client | Updated network data |
| `link_dropped` | Server to client | Link connection lost |
| `node_inactive` | Server to client | Node went inactive in legacy status |
| `new_event` | Server to client | New event-log entry |
| `phones_update` | Server to client | Phone pings finished after a scan |
| `maintenance_update` | Server to client | A maintenance window was created, changed or deleted |
| `request_scan` | Client to server | Request immediate scan |
| `request_network` | Client to server | Request current network graph |
| `request_events` | Client to server | Request recent events |

## Deployment Notes

- **Single collector (today):** run with Docker (`docs/DOCKER.md`) or as a systemd service (`aredn-monitor.service`) on a PC on the mesh. Data lives in SQLite with nightly backups.
- **Several collectors (planned):** each collector runs the app plus CouchDB, writes append-only observations and replicates them to peers; `/api/status/distributed` derives node status from all collectors' observations and heartbeats.

## Implementation Notes

- Do not rewrite the front end before mapping the existing API and data flow.
- Keep collector, CouchDB observation writes, status calculation, and reporting endpoints separated.
- Existing SQLite status fields are legacy current-state views, not the final distributed truth.
- Future distributed status should be derived from recent observations and collector heartbeats, not stored as a mutable shared node-status document.
- Prefer `STALE`, `PARTITIONED`, or `LOCALLY_UNREACHABLE` over `DOWN_CONFIRMED` unless multiple healthy collectors provide strong evidence.

## License

MIT License - feel free to use and modify for your amateur radio network monitoring needs.

## Acknowledgments

- [AREDN Project](https://www.arednmesh.org/) for the mesh networking firmware
- [vis.js](https://visjs.org/) for the network visualization library
- [Flask](https://flask.palletsprojects.com/) and [Flask-SocketIO](https://flask-socketio.readthedocs.io/) for the web framework

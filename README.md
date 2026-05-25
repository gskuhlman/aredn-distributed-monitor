# AREDN Network Monitor

A real-time web application for monitoring and visualizing AREDN (Amateur Radio Emergency Data Network) mesh networks.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.0-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Current Direction

This repository is a working copy of the older single-node monitor. The front end has not been rewritten. The current implementation keeps the existing Flask, Socket.IO, vis.js, SQLite, and RF statistics behavior intact while adding the first backend/data-layer seam for a distributed, partition-tolerant design.

The new distributed path is append-only CouchDB observations:

- Existing SQLite tables still power the current dashboard routes.
- `scanner.py` still performs the current AREDN discovery and updates SQLite.
- When `COUCH_URL` is configured, scans also write deterministic append-only node, link, service, and collector heartbeat documents to CouchDB.
- When `COUCH_URL` is not configured, the app behaves like the legacy SQLite-only monitor.

Important status principle for the new design: a node that is not visible from one collector is not automatically down. It is only not visible from that collector at that time.

## Features

- **Network Discovery**: Automatically discovers nodes via BFS traversal starting from a configurable seed node.
- **Real-time Updates**: Live updates via WebSocket using Socket.IO.
- **Interactive Visualization**: Drag-and-drop network graph using vis.js.
- **Link Quality Monitoring**: Color-coded links for good, poor, bad/dropped, and DTD links.
- **Link Type Identification**: Line patterns distinguish RF, DTD, tunnel, WireGuard, and Xlink connections.
- **Supernode Detection**: Purple highlighting for supernodes, with discovery stopping at supernode boundaries.
- **Service Icons**: Shows available services such as phone, MeshChat, PBX, camera, weather, and iPerf.
- **Firmware Mismatch Detection**: Orange highlighting for nodes with mismatched firmware.
- **Persistent Layout**: Node positions saved to browser localStorage.
- **RF Statistics**: Optional ping and iPerf history for RF links.
- **Full Node Information Page**: Per-node page with current metadata, links, services, charts, log history, targeted scan, ping, and iPerf actions.
- **Event Log Search**: Filter the event log by partial node name or log text.
- **Optional CouchDB Observation Writes**: Writes append-only observations for distributed/offline replication when configured.
- **Collector Health Endpoint**: Reports local collector identity and optional CouchDB connectivity.

## Requirements

- Python 3.11+
- Access to an AREDN mesh network
- Optional: Apache CouchDB 3.x for distributed observation storage

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/gskuhlman/AREDN_netmonitor.git
   cd AREDN_netmonitor
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

Start the application:

```bash
python app.py
```

Open `http://localhost:5000`.

The network scans automatically when auto-scan is enabled. You can also click **Scan Now**, open **Settings**, click graph nodes for details, or use the RF Stats and Nodes tabs.

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
AREDN_netmonitor/
|-- app.py                  # Flask application entry point
|-- config.py               # Configuration settings and env overrides
|-- database.py             # Legacy SQLite current-state operations
|-- scanner.py              # Network discovery and observation write hook
|-- observations.py         # Append-only observation document builders
|-- couch_client.py         # Small CouchDB client and bootstrap command
|-- rf_stats.py             # RF statistics: ping and iperf testing
|-- requirements.txt        # Python dependencies
|-- aredn-monitor.service   # Systemd service file
|-- static/
|   |-- css/
|   |   `-- style.css
|   `-- js/
|       |-- network.js
|       |-- nodes.js
|       `-- rf-stats.js
`-- templates/
    `-- index.html
```

## Distributed Observation Model

When CouchDB is configured, each scan writes immutable documents with deterministic IDs:

- `node_observation`: one per attempted node poll, including failed polls
- `link_observation`: one per observed link from sysinfo/LQM data
- `service_observation`: one per service seen on a node
- `collector_heartbeat`: one per scan cycle

Document IDs include collector ID, UTC poll timestamp, and target slug. This avoids normal CouchDB conflicts and makes retries idempotent. These observations are the source for the planned distributed reporting model; the existing dashboard still reads SQLite-derived current state until reporting endpoints are expanded.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Application health and optional CouchDB connectivity |
| `/api/collectors` | GET | Collector heartbeat summary from CouchDB when configured |
| `/api/nodes` | GET | Get all nodes |
| `/api/nodes/active` | GET | Get active nodes only |
| `/api/nodes/all` | GET | Get all known nodes with link/service counts |
| `/api/node/<name>` | GET | Get node details with services |
| `/api/nodes/detail/<name>` | GET | Get node detail, links, services, and connectivity log |
| `/api/nodes/full/<name>` | GET | Get all locally available node details for the full node page |
| `/api/nodes/scan/<name>` | POST | Run a targeted scan against one known node |
| `/api/links` | GET | Get all links |
| `/api/links/active` | GET | Get active links only |
| `/api/network` | GET | Get graph data for vis.js |
| `/api/settings` | GET/POST | Get or update settings |
| `/api/scan` | POST | Trigger immediate scan |
| `/api/status` | GET | Get current scan status |
| `/api/events` | GET | Get event log |
| `/api/rf-stats/*` | GET/POST | RF statistics and tests |

Full node pages are available at `/nodes/<name>`.

## WebSocket Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `scan_started` | Server to client | Scan has begun |
| `scan_complete` | Server to client | Scan finished with results |
| `network_update` | Server to client | Updated network data |
| `link_dropped` | Server to client | Link connection lost |
| `node_inactive` | Server to client | Node went inactive in legacy status |
| `new_event` | Server to client | New event-log entry |
| `request_scan` | Client to server | Request immediate scan |
| `request_network` | Client to server | Request current network graph |
| `request_events` | Client to server | Request recent events |

## Deployment Notes

The existing `aredn-monitor.service` supports a legacy single-process deployment. For distributed CouchDB operation, each collector should eventually run:

- CouchDB
- the Python scanner/reporting app
- local configuration
- CouchDB replication jobs to central or peer collectors

Docker Compose and persistent replication setup are planned next steps; they are not yet present in this repository.

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

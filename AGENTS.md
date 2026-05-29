# Agent Notes for aredn-distributed-monitor

## Commands

- **Run the app:** `cd /home/w0gsk/aredn-distributed-monitor && source venv/bin/activate && python app.py`
- **Validate syntax:** `python -m py_compile app.py scanner.py config.py database.py rf_stats.py observations.py couch_client.py`
- **Bootstrap CouchDB:** `python couch_client.py` (requires `COUCH_URL` env var)

## Architecture

- `app.py` — Flask + SocketIO entry point. Uses **eventlet** (`eventlet.monkey_patch()` at line 8). Must not use Flask debug reloader with eventlet.
- `scanner.py` — BFS network discovery from `STARTING_NODE`, writes to SQLite, optionally to CouchDB.
- `database.py` — Legacy SQLite current-state operations.
- `observations.py` — Append-only CouchDB document builders.
- `couch_client.py` — CouchDB client + bootstrap.
- `rf_stats.py` — Ping and iPerf tests. Requires `iperf3` binary.
- `config.py` — Central config with env var overrides.

## Key Config (env vars override `config.py`)

- `STARTING_NODE` — Seed for BFS discovery.
- `COUCH_URL` — If set, enables CouchDB observation writes.
- `COLLECTOR_ID` / `COLLECTOR_SITE` — Identity for distributed observations.
- `HOST` / `PORT` — Bind address (default `0.0.0.0:5000`).
- `DEBUG` — Keep `False`; eventlet + werkzeug reloader is unstable.

## Operational Gotchas

- **iperf3 not installed by default** — `sudo apt-get install -y iperf3` required for RF throughput tests.
- **Docker available but service inactive** — `sudo systemctl start docker` needed for CouchDB container.
- **User not in `docker` group** — May need `sudo usermod -aG docker $USER` then re-login.
- **Seed node `10.98.52.254` not reachable from this host** — The node is only reachable when connected to the AREDN mesh network.
- **No test suite or linter configured** — Use `python -m py_compile` for basic validation.
- **Systemd service** — File present at `aredn-monitor.service`. This now uses a simple user-service template without `ProtectSystem`/`ProtectHome` hardening (those directives caused exit 216). Use `systemctl --user enable --now aredn-monitor.service` for user-level operation.

## Systemd Service

```bash
# User-level (recommended, for the current user)
systemctl --user daemon-reload
systemctl --user enable --now aredn-monitor.service

# System-level (requires root)
sudo cp aredn-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aredn-monitor.service
```

# Running the monitor in Docker

The container runs `python app.py` with everything it writes under `/data`
(bind-mounted to `./data` next to `docker-compose.yml`):

| In `/data` | What |
|---|---|
| `aredn_monitor.db` | the SQLite database (all history, settings, notes, login hash, notification secrets) |
| `backups/` | nightly backups (`BACKUP_TIME`, keeps `BACKUP_KEEP`) |
| `ssh/` | SSH key pair and pinned host keys for the radio-details collector |
| `maps/` | offline Channel Map extracts (`*.pmtiles`) |
| `connectivity.log` | connectivity log |

## Host requirements

- A **Linux** workstation with Docker Engine and the compose plugin. The
  compose file uses `network_mode: host`, so `*.local.mesh` names resolve
  through the workstation's own DNS (its AREDN node), and pings,
  traceroutes, iPerf and SSH leave from its mesh interface. Docker Desktop on
  Windows/macOS does not give a container the host's real network; on a
  Windows workstation run the app directly (`start.bat`) instead.
- The workstation must resolve `localnode.local.mesh` (or set
  `STARTING_NODE`). Check with `ping localnode.local.mesh` on the host.
- Its clock and time zone should be Central; the container sets
  `TZ=America/Chicago` itself.

## First start on a new workstation

```bash
git clone https://github.com/gskuhlman/aredn-distributed-monitor.git
cd aredn-distributed-monitor
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Open `http://<workstation>:5000`. An empty database starts discovering from
the starting node within a few seconds.

## Moving the existing monitor (keep the history)

Only one copy should scan and send notifications: stop the old one first.

1. On the old PC stop the app (so the database is not being written), then
   copy these into the new workstation's `data/` folder:
   - `aredn_monitor.db` (a copy of the newest `backups/aredn_monitor-*.db`
     also works; it was taken with the SQLite backup API)
   - `ssh/aredn_monitor_ed25519`, `ssh/aredn_monitor_ed25519.pub`,
     `ssh/known_hosts` -> `data/ssh/`
   - `maps/*.pmtiles` -> `data/maps/`
2. On the workstation fix the key's permissions (ssh refuses a private key
   others can read):
   ```bash
   chmod 700 data/ssh && chmod 600 data/ssh/aredn_monitor_ed25519
   ```
3. `docker compose up -d --build`.

The nodes already trust the key, so SSH collection keeps working; the pinned
host keys carry over too. Settings, notification channels, the login
password and the per-install session key all live in the database.

## Day to day

```bash
docker compose logs -f --tail 200    # app log
docker compose restart               # after changing environment settings
git pull && docker compose up -d --build   # update to a new version
docker compose down                  # stop
```

Settings in `config.py` can be overridden in the `environment:` block of
`docker-compose.yml` (e.g. `COLLECTOR_ID`, `PORT`, `PROMETHEUS_URL`,
`SYSLOG_ENABLED`). Back up `data/` (the nightly backups land in
`data/backups/`).

## Notes

- The container runs as root so ping and traceroute can open raw sockets on
  the host network; it only listens on `PORT` (5000), plus UDP `SYSLOG_PORT`
  when `SYSLOG_ENABLED` is on.
- `fetch_offline_map.py` is not part of the image; make map extracts on
  another machine and drop the `.pmtiles` files into `data/maps/`.
- Alert acknowledge links use `escalation.ack_base_url` (Notifications page),
  else this host's name and port; set it if the workstation's name is not
  what phones on the mesh can reach.

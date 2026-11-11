# TODO: feature ideas

Ideas from a project review (2026-10-18), grounded in problems already found:
the MMC↔Collins one-way link, overlapping Collins sectors, near-far at kf9mt,
legacy-tunnel nodes that can't be reached, and overnight starting-node outages.

Recommended order: 2, 1, 3, 8, 7, 21.

## Top picks

- [x] 1. **Change log + before/after.** Record channel/power/antenna changes (from
      `radio_config_change` events or manual entry); show a link's LQ, SNR, noise and
      retries for 24 h before vs. after, side by side.
- [x] 2. **"Why is this node unreachable?" classifier.** Never resolves, legacy TUN
      tunnel route only, answers ping but not HTTP, not seen in more than 30 days, behind a supernode, or
      starting-node outage. Count only "really down" nodes as unreachable in Reports.
- [x] 3. **Area channel map.** Map with each radio colored by frequency, antenna beams
      as wedges, and lines between radios whose channels overlap and point at each other.
- [x] 4. **Channel planner.** Pick a site (or radios plus their clients) and get suggested
      channels using band edges, busy WiFi ranges, beams, guard bands, and clients that
      must follow their sector. Simulate the result before applying it.
- [x] 5. **Health score + "what changed?"** Score each node/link 0–100 over time;
      clicking a drop lists new neighbors, reboots, config changes, noise jumps, route
      changes and starting-node outages in that window.

## Troubleshooting checks

- [x] 6. **Hidden-node / airtime check per sector.** Clients that can't hear each other,
      combined with channel busy and retries; suggest RTS/CTS or fewer clients.
- [x] 7. **Noise-floor watch.** Noise (signal − SNR) per radio over time; alert on jumps
      of 5 dB or more; show the time-of-day pattern (WiFi follows when people are home).
- [x] 8. **One-way link alarm.** One end's LQ high and the other low, routing cost
      infinite, or one end knows the other only by MAC. Flag it continuously instead of
      only when someone runs a diagnosis.
- [x] 9. **Route-change log.** Periodic traceroute along key paths (starting node to
      each site, MMC↔COL↔EOC); record hop count and path; alert on detours.
- [x] 10. **Starting-node health page.** Outages, latency, DNS resolution time and lost
      scans, to separate problems with this PC or its uplink from problems on the mesh.
- [x] 11. **Power-cut advisor.** Predict receive levels at every neighbor after a
      proposed TX power cut, so nobody drops below the SNR they need. Never cut
      infrastructure power.
- [x] 12. **Firmware/settings drift report.** Mismatches in firmware, mode, SSID, width,
      distance setting and LQM settings, within a site and across each link.
- [x] 13. **Weather correlation.** Overlay NWS rain, temperature and wind on SNR/LQ
      (rain fade, ice, wind-shifted antennas).
- [x] 14. **Antenna aim check.** Compare each antenna's set azimuth with the bearings of
      the neighbors it hears best; flag misaims or unset azimuths.

## New ways to view the data

- [x] 15. **Heatmap, node × hour.** Colored by reachability or LQ; shows whole sites
      dropping together and nightly patterns.
- [x] 16. **Site neighbor matrix.** Every radio at a site against every radio it hears,
      with SNR/LQ per cell; one-way and lopsided pairs stand out.
- [x] 17. **Link swimlanes.** One bar per link, colored good/flapping/down over time,
      with reboots and config changes marked.
- [x] 18. **Path view.** Current and historical route between two nodes, each hop
      colored by link quality.
- [x] 19. **Site dashboard.** Per-site page (COL, MMC, EOC): radios, spectrum chart,
      clients per sector, uplink paths, node health.
- [x] 20. **Weakest-link / single-point-of-failure map.** Rank links whose loss splits
      the mesh, weighted by how many nodes are behind each one.

## Alerts and operations

- [x] 21. **Notifications.** Email, Discord, ntfy or APRS when an infrastructure node
      goes down, a sector loses a client, the starting node is lost, or a link score
      drops; respect maintenance windows.
- [x] 22. **Maintenance windows.** Planned work is excluded from reports and alarms.
- [x] 23. **Notes on nodes and links.** For example "tilted 2° on 10/20" or "call
      KF9MT first", shown in diagnoses and on charts.
- [x] 24. **Cleanup of stale and junk entries.** Exclude or delete nodes that never
      answer (k9lrd-home, the fake "10" node, `mac-…` placeholders). Exclude the
      kc0wkp-ar750 legacy TUN clients from scanning.
- [x] 25. **Weekly report.** Saved or emailed summary of the worst links, new problems,
      infrastructure verdicts and changes.

## Planning

- [x] 26. **Link budget + terrain.** Free-space loss, antenna gains, Fresnel zone and
      elevation data give a predicted SNR to compare with the measured SNR
      (obstructions, misaims).
- [x] 27. **Capacity view.** iPerf results with bit rate and channel busy give the spare
      capacity on each backbone link (MMC–COL, MMC–EOC).
- [x] 28. **Scheduled iPerf.** Overnight runs on key links, trended over time.

## Round 2 ideas (2026-10-20)

Grounded in what turned up since: a fake RF tracker named after a node whose radio is off,
a planner that gave different plans after each restart, 252 links aged out by one short scan,
and a channel plan that now has to be carried out by hand across three sites.

All built 2026-10-24 (see CLAUDE.md for each module). Not verified against live radios: the WiFi scan page path (#34; import works), alignment readings (#31) and service probes (#36) were only tested off the network.

### Carrying out changes

- [x] 29. **Plan rollout tracker.** Turn a planner plan into an ordered checklist (clients before
      their sector, backbones last), tick each step off automatically when the scanner sees the
      radio on its new channel, and flag a link that hasn't come back within N minutes.
- [x] 30. **Automatic before/after verdict.** 24 h after a `frequency_change` or channel change-log
      entry, run the Changes comparison by itself and send "better / worse / same" per link
      (notification + weekly report).
- [x] 31. **Antenna alignment mode.** Phone-friendly page that polls one radio's LQM every 1–2 s
      and shows SNR/signal to a chosen peer as a big number, bar and tone, with the best value
      seen, for tower work.
- [x] 32. **Field mode / PWA.** Installable, phone-sized Map + node pages that work from the
      offline maps and the last data when the laptop has no internet.

### Troubleshooting checks

- [x] 33. **Ghost tracker check.** RF trackers that can't be what they claim: named after a node
      whose radio is off, a node cabled to the radio, LQM distance far from the coordinates, or one
      MAC under two names. List them (Mesh Health) and leave them out of every RF check.
- [x] 34. **WiFi survey at the sites.** Use the nodes' own WiFi scan (where the firmware exposes it)
      on a schedule to record non-mesh access points per channel near COL/MMC/EOC, and feed
      that into the planner's noise cost instead of the fixed UNII-3/UNII-4 guesses.
- [x] 35. **Babel cost view.** Read Babel neighbour costs/metrics from the nodes and show where
      routing prefers a tunnel or a long detour over an RF path that looks fine.
- [x] 36. **Service checks.** Probe the services nodes advertise (web pages, PBX, cameras,
      Winlink) and alert when one stops answering while its node is still up.
- [x] 37. **Node hygiene score.** Per node: location set and plausible, antenna/azimuth set,
      firmware current, description, contact; a "fix these" list for each owner.

### New ways to view the data

- [x] 38. **Map playback.** Time slider on the Map page replaying links, colors and outages over a
      chosen window (from link_state_log), to see how an event spread.
- [x] 39. **Map overlays.** Toggle SNR/quality coloring, SPOF links, Route Watch paths and channel
      conflicts on the Map page; click a node for "what loses connectivity if this goes down".
- [x] 40. **Spectrum waterfall per site.** Channel occupancy and measured noise per 5 MHz slot over
      time at each site, with planned changes marked.

### Operations and robustness

- [x] 41. **Test suite.** pytest with a small fixture database: scan guards (seed degraded, short
      scan aging), planner determinism and plan quality, pair diagnosis (wired pair, radio off),
      unreachable classifier, reports. Run before each commit.
- [x] 42. **Login for changes.** The UI has no authentication: anyone on the LAN can delete nodes,
      schedule maintenance or send the weekly report. Read-only by default, a password (or
      reverse-proxy auth) for actions.
- [x] 43. **Database care page.** Size per table, growth per day, retention settings, VACUUM, and a
      nightly backup of the SQLite file (keep N copies).
- [x] 44. **Distributed status.** Build the multi-collector status rules from CLAUDE.md (UP /
      LOCALLY_UNREACHABLE / PARTITIONED / STALE / DOWN_CONFIRMED) from CouchDB observations, and a
      second collector to test it.
- [x] 45. **New node welcome.** When a node appears for the first time: notify, show its hygiene
      checklist (#37), suggest a channel from the planner for its site, and mark it to watch for
      a week.
- [x] 46. **Club report export.** One-page PDF/printable summary (map, health, changes made, what's
      next) for meetings.

## Round 3 ideas (2026-10-24)

Grounded in what turned up while building Round 2: link_history is 80% of the database and grows
about 15 MB a day, one collector can only ever say UP / STALE, three Round 2 features have never
met a live radio, and alarms for one root cause (the starting node, a site losing power) still
arrive as many separate events.

### Verify and harden

- [ ] 47. **Live check of Round 2 radio features.** With the operator present: find which WiFi scan
      page the firmware offers (#34), watch alignment readings update during a real adjustment
      (#31), and confirm service probes against a few known services (#36). Fix what differs.
- [x] 48. **History rollup.** Keep raw link_history scan samples for 7 days, then hourly
      aggregates (median/p10/p90 SNR, LQ, counters) for the rest of the retention, so the database
      stops growing ~15 MB/day and old Reports windows load faster.
      *Built with 30 days of raw scan samples (`LINK_HISTORY_RAW_DAYS`), then hourly rollups kept 400 days (rollup.py).*
- [ ] 49. **Second collector kit.** A setup page and script for another PC: `COLLECTOR_ID`,
      `COUCH_URL`, CouchDB replication check, clock skew check, and a "collectors agree?" view, so
      distributed status (#44) can say LOCALLY_UNREACHABLE / PARTITIONED / DOWN_CONFIRMED for real.
- [x] 50. **Audit log.** Who did what and when: deletes, excludes, maintenance, rollouts,
      settings, password changes, with the client address and (once logged in) the session.

### Smarter alarms

- [x] 51. **Incident grouping.** Fold events that share one cause into one incident with a root
      cause and a timeline: starting node lost, a site where every radio dropped at once, a
      backbone link whose loss cut off the nodes behind it (spof). One notification per incident.
- [x] 52. **Site power outage detection.** All radios at a site unreachable together, then all
      back with uptime reset = power, not RF. Say so in the alarm and in the weekly report.
- [x] 53. **Slow degradation watch.** Per RF link direction, the 30-day SNR/LQ trend and the gap
      to the link budget prediction (#26); flag links slipping a few dB per month (antenna
      moving, corrosion, foliage) before they fail.
- [x] 54. **Storm warning.** Use the NWS forecast with the measured rain fade per link (#13):
      "heavy rain expected at 18:00; these 3 links lost 6+ dB in the last rain".
- [x] 55. **Notification acknowledgement.** Ack an alert from the page or a link in the message;
      unacknowledged critical alerts repeat or escalate to a second channel after N minutes.

### Troubleshooting

- [x] 56. **Noise source finder.** When noise jumps on several directional radios at about the
      same time, intersect their antenna beams on the Channel Map to point at the likely
      interference area.
- [x] 57. **Tunnel health.** Per tunnel (legacy and WireGuard): up time, flaps, latency, which
      nodes reach the mesh only through it, and what loses the mesh if the internet goes down.
- [ ] 58. **Firmware upgrade planner.** Order upgrades far ends first so no node loses its path
      mid-upgrade, tracked like a rollout (#29) and ticked off when the scanner sees the new
      version.

### For the club

- [ ] 59. **"Can I reach the mesh from here?"** Enter an address or click the map: nearest sites in
      view, predicted signal per sector from the link budget and terrain, suggested antenna and
      aim bearing. For new members.
- [x] 60. **Served-agency status board.** Big tiles for the EOC, hospital and other served
      agencies: their nodes and services, path health to them, uptime this month. For use during
      an activation.
- [ ] 61. **Prometheus / Grafana export.** Read-only `/metrics` (node up, link SNR/LQ, noise, scan
      duration) with an optional token, for anyone who already runs Grafana.
- [x] 62. **Global search.** One box in the header for node, site, MAC, IP, callsign or service
      that jumps to the right page.
      *Built: search.py + /api/search and global-search.js on every page with a header ("/" to focus);
      also LAN devices and phones. A callsign opens the Nodes list filtered to it (`?nodes-search=`).*

## Round 4 ideas (2026-10-25)

From the W0GSK Grafana dashboard (AREDN's built-in Prometheus exporter, `/cgi-bin/metrics`,
firmware 3.24.4.0+), other mesh/WISP tools (MeshMap, Freifunk meshviewer, UISP, LibreNMS) and what
the Spectrum page showed. sysinfo.json only gives free memory (`sysinfo.freememory`, KB, now
captured); the metrics page has everything else, at ~60 KB a poll. Our own polling stays; these add to it.

### Node health from the metrics page

- [x] 63. **Prometheus details on the node page.** Keep our own polling as it is; add a section
      to the node page that pulls extra details for that node from W0GSK's Prometheus (behind the
      Grafana at w0gsk-ubuntu:3125, which already scrapes `/cgi-bin/metrics`): memory total and
      used, CPU, flash, conntrack, per-interface traffic, per-station MCS. Read on demand when the
      page opens; nothing replaces the scanner's data, and the page still works when Prometheus
      is unreachable.
- [x] 64. **Full node health.** Total RAM and % used, CPU busy, root filesystem free, conntrack
      entries vs limit, process count and file descriptors, on the node page and in Mesh Health
      (low memory, conntrack near full, flash nearly full, CPU pegged).
      *Built from Prometheus: node page tiles and Mesh Health `resources`. conntrack and file descriptors are not in the exporter, so they are listed as unavailable.*
- [x] 65. **Traffic per interface.** wlan0 / eth / DtD / tunnel bytes and packets per second,
      drops, errors, FIFO and frame errors; "busiest radios" ranking. Shown in Capacity next to
      the LQM packet estimate, not instead of it: the estimate covers every RF link from our own
      polls, the measured traffic covers what Prometheus scrapes, and the two together show when
      the estimate is off.
- [x] 66. **Per-station MCS and signal.** `node_wifi_station_*` transmit/receive MCS, rate, signal
      and noise per client: an MCS chart per link next to SNR, and an alarm when MCS falls while
      SNR holds (interference, not path loss) -- exactly the COL<->MMC 7.2 Mbps case.
      *Built: RF Stats "MCS, rate & LQM ping", `mcs_drop` alarm, and Mesh Health `mcs` for links stuck at a low MCS with good SNR.*
- [x] 67. **LQM ping success and quality** (`node_lqm_tracker_ping_*`) per link in RF Stats and
      Link Health, alongside our own pings.
- [x] 68. **Babel route and neighbour counts over time** (`node_arednlink_neighbors_count_total`,
      routes): a sudden route-count drop flags a partition before nodes go unreachable.

### Seeing the network

- [x] 69. **Link potential plot (UISP style).** Every RF link as a dot: distance across, SNR or
      capacity up, coloured by measured / theoretical capacity from the link budget. Links far
      below the curve are the ones to re-aim or re-channel.
      *Built: Diagnostics "Link Potential" (PHY rate vs what the SNR supports; iPerf only as a fallback).*
- [x] 70. **Spectrum history.** The Spectrum page over time: replay WiFi surveys and our channel
      changes per site, and "what changed since the last scan" per slot.
- [x] 71. **Scan reminders.** Spectrum rows marked "not scanned" or older than 14 days get a
      to-do (radio, why, how long the link drops), and a "scan all sector radios tonight" job for
      infrastructure when WiFi survey is enabled.
- [x] 72. **Embeddable panels.** Like meshviewer's nodeInfos: per-node and per-site image/iframe
      URLs (or links into Grafana) so the club website or a Grafana dashboard can show our charts.
      *Built: /embed/node/<name>, /embed/site/<code> and badge.svg for each.*

### Alarms

- [x] 73. **Per-link alerts on PtMP sectors.** Alert rules that target one client link of a sector
      (SNR, MCS, capacity, flaps), not just the whole radio -- the gap LibreNMS/SolarWinds users
      hit with multipoint radios.
- [x] 74. **Resource alarms.** Memory low / falling for days (leak before a reboot), conntrack near
      limit, root FS full, using the metrics from #64.
      *Built: memory low / leak trend, flash, /tmp, CPU (prom_watch.py). No conntrack metric to alarm on.*

## Round 5 ideas (2026-11-09, from MeshWatch at meshwatch.hosted-by.me)

MeshWatch (v3.3.0) runs on ct-meshwatch behind kd0cgn-prox; its PyCams viewer (port 15001)
pulls the RTSP camera streams seen crossing the MMC tunnel.

- [ ] 75. **MeshWatch as a second collector.** It sees the mesh from kd0cgn-prox's side; feed its
      node view into distributed_status (UP / LOCALLY_UNREACHABLE / DOWN_CONFIRMED). Needs a
      readable API and the owner's agreement.
- [x] 76. **24 h reachability tape per node.** One slot per poll (heard / degraded / missed) on
      the Nodes list and node page, grouped by callsign, "N of M on the air" headline.
- [x] 77. **Wall display for an operations room.** 1080p page: every node as a tile with its tape,
      needs attention, activity, offline mesh plot; live indicator red when data stops, full
      screen with F, wake lock, reloads itself after an update.
      *Built: /wall (agency tiles, ticker, located mesh plot, 24 h tape, storm banner); every node as its own tile was left out in favour of the tape rows.*
- [x] 78. **Continuous call quality per node.** Jitter (ping mdev) and E-model MOS from each poll,
      alerts at jitter > 20 / 50 ms and MOS <= 3.6 / 3.1; pairs with Route Watch flapping.
- [ ] 79. **Asterisk PBX status over SSH.** Active calls, registered vs unreachable peers
      (`core show channels concise`, `pjsip show endpoints`), alert when a phone drops off.
- [x] 80. **Interface traffic history.** Store per-interface counters (tunnel, WAN, DtD) from the
      SSH fast runs and chart 1 h .. 7 d, e.g. how much went over the MMC tunnel this week.
- [x] 81. **Services catalog and devices.** Every advertised service grouped by category
      (cameras, weather, phones, Winlink, chat, time), "new today" badge, services that left
      listed for a week; devices = hosts plugged into each node.
- [x] 82. **Tunnel dependency.** RF native vs needs a tunnel vs off air summary (spof with tunnels
      removed), plus an alert when a node loses its last RF path.
- [ ] 83. **Smaller ones.** "Stored" state for retired nodes (not counted as down); "polled 5 min
      ago, next in 4" in the header; CSV export of the node list; long-term rollups (monthly
      kept forever); in-app "What changed" release notes; audit log of who changed what.
- [ ] 84. **Cross-check MeshWatch's findings.** w0gq-col-rkt-w 86 failed TX/min and
      w0gq-col-sq-2mmc 61/min; KD0CGN nodes with 25-40 ms jitter (route flapping?).

## Round 6 ideas (2026-11-09, from NM5PB's CommDeck at meshnoc-nm5pb-lin01.local.mesh)

CommDeck runs on NM5PB-VM: workstation (map, inventory by Babel metric, iperf3 box, services,
attached devices), Link Health map, Log Viewer (syslog), Incidents, Changes, Alignment (login).

- [x] 85. **Central syslog receiver.** Nodes send AREDN "Remote logging" (UDP 514) to us; log page
      with node / level / service filters, WireGuard / Network / Auth / Kernel chips, collapse
      duplicates, saved views, event-rate chart, CSV export. Today we only read logread over SSH daily.
      *Built: Node Logs page fed by the SSH logread snapshots now; the UDP receiver exists but is off (`SYSLOG_ENABLED`) until nodes are pointed at this PC, since they send to NM5PB today.*
- [x] 86. **Incidents.** An investigation record: symptoms, suspected cause, current action,
      owner contacted, resolution, status open / monitoring / resolved / closed, timeline, linked
      changes and notes (e.g. their INC-2026-0001 Collins South / KF9MT location, = our TODO).
- [x] 87. **Suppressions with a review date.** Muting an alert for a node/link needs a reason and
      a review date; "review overdue" banner when it passes (their CHG-2026-0001).
- [x] 88. **Tunnel health.** Measured RTT and loss per tunnel, a tunnel-health % next to RF link
      health, "tunnel health degraded" per pair (they flag KD0CGN-Prox -- W0GQ-MMC-AC2), and
      dependency lines: "25 nodes, 32 services depend on this tunnel".
- [x] 89. **Live insight ticker.** A scrolling line of the newest findings (high loss, low SNR
      unchanged for 1 h, tunnel degraded, dependencies) for the Status page or wall (#77).
- [ ] 90. **Weather radar overlay on the map** (RainViewer tiles) to set beside rain fade (#weather).
- [ ] 91. **Quick iperf3 box** on the home page: pick two nodes, TCP/UDP, Run test, last result
      and history (we have iPerf in Links; this is just faster to reach).
- [x] 92. **Our SSH collector in their syslog.** NM5PB's log viewer (all W0GQ nodes already send
      syslog to nm5pb-lin01, 10.246.137.98:514) shows this PC (10.42.92.116) logging in to every
      infrastructure radio every 15 min (3 dropbear lines each), sometimes "Connection reset by
      peer". On W0GQ-COL-RKT-S every run also logs "ath10k_pci: failed to request fw stats: -145"
      (timeout): stop reading fw_stats on a radio once it times out there (Collins South has
      "ath10k mesh unresponsive" resets, their INC-2026-0001), and close sessions cleanly.
      *Built: a backed-off radio's fw_stats is retried once a week on a slow run (`SSH_FW_STATS_RETRY_DAYS`),
      not on every daily slow run; sessions already ended with `exit` (dropbear logs "Disconnect
      received"), and on SIGTERM (docker stop) the app now starts no new session and lets the open
      one finish before exiting.*
- [ ] 93. **Read NM5PB's log collector instead of running our own** (#85), if it has an API and
      NM5PB agrees. Seen there now: KD0CGN-Prox (starting node) babeld "kernel_route(ADD):
      Operation not permitted" for 192.168.1.214/32 x191 in 3 h; KF0JCL-QTH lan_monitor ucode
      exception (services.uc:259) every 2 min and a LAN WiFi client re-joining 78 times;
      W0GQ-MMC-AC2 firewall reload on each guest-WiFi reconnect.

## Pending radio work (outside the app)

Channel plan for Collins + MMC + EOC, 2026-11-06, from WiFi scans (pasted /a/scan tables of col-rkt-n,
col-rkt-e, col-rkt-s, col-rkt-w, col-sq-2mmc, eoc-rkt-o13, eoc-pb5ac-2mmc; mmc-rkt-o13 returned
nothing). Replaces the 2026-10-20 plan, whose COL S on 5775 sits on ch 153/157, among the busiest WiFi
the Collins scans heard (75 APs on ch 136, 64 on 149, 47 on 157; nothing at all on 5855–5925). One
channel per sector, as AREDN's channel-planning guide recommends; no TDWR within range of Cedar Rapids.
Collins has about 90 MHz of clean spectrum (5835–5925), not enough for four 20 MHz sectors with 20 MHz
guards, so three sectors sit there (W–S 20 MHz apart, the back-to-back N–S pair 10 MHz) and COL E
takes 5650–5670 (14 weak APs, -77 dBm). This is the planner's top plan since it weighs guard bands on
a mast above weak WiFi (before, its top plan packed all four sectors edge to edge).
Scores: today 7 critical / 19 warning; this plan 0 critical / 3 warning (N–S 10 MHz apart; same
channel at other sites: COL W with the EOC omni 13 km away, COL E with the MMC omni 6.2 km away).
Today two radios are partly outside the 5650–5925 ham band: COL W 5920/20 (to 5930) and the
COL ↔ MMC backbone 5660/40 (from 5640).

- [ ] Start a maintenance window for COL, MMC and EOC (Diagnostics → Maintenance, RF peers
      included) so alarms and reports ignore the change.
- [ ] COL N on ch 183 (5915/20): w0gq-col-rkt-n with kf0jcl-qth-litebeam5ac.
- [ ] COL S on ch 177 (5885/20): w0gq-col-rkt-s with kf9mt-home-litebeam.
- [ ] COL W on ch 169 (5845/20): w0gq-col-rkt-w with ka0ies-square-one.
- [ ] COL E on ch 132 (5660/20): w0gq-col-rkt-e with kj0dx-qth-ub.
- [ ] COL ↔ MMC backbone on ch 142 (5710/40): w0gq-col-sq-2mmc and w0gq-mmc-sq-2col together
      (also brings it back inside the band: today 5640–5680).
- [ ] MMC ↔ EOC backbone on ch 159 (5795/40): w0gq-mmc-pb5ac-2eoc and w0gq-eoc-pb5ac-2mmc together
      (30 MHz down from 5825, 20 MHz clear of COL W, which the EOC dish looks toward past MMC; the
      EOC dish itself heard almost no WiFi).
- [ ] EOC omni w0gq-eoc-rkt-o13 to ch 169 (5845/20); MMC omni w0gq-mmc-rkt-o13 to ch 132 (5660/20).
- [ ] Turn off w0gq-col-sq-2bldg112 and w0gq-col-sq-2hiwt until their far ends exist.
- [ ] Move each client before (or with) its sector, so no link is stranded.
- [ ] Lower kf9mt-home-litebeam's TX power (near-far at Collins). Never Collins's.
- [ ] Fix ka0ies-square-one's azimuth setting: set to 135°, but Collins is at 43° from it (link is
      good, so it's the setting, not the aim). Set it to 43°.
- [ ] Re-save kf9mt-home-litebeam's location: its LQM places it at Collins (reports 16 m to rkt-s,
      really 2.29 km).
- [ ] Set kj0dx-qth-ub's azimuth (15° dish, unset): 257°, toward w0gq-col-rkt-e.
- [ ] Fix n9pno-dstar's longitude: 90.62 should be -90.62.
- [ ] Check w0gq-mmc-rkt-o13 and w0gq-eoc-rkt-o13 (omnis hearing nothing): channel and SSID.
- [ ] Afterwards: check RF Stats for an rkt-n ↔ rkt-s link or clients linked to the opposite
      sector, and compare before/after on the Changes tab.
- [ ] Trade-offs to watch: COL N and COL S are 10 MHz apart (back to back), COL E and the MMC omni
      share 5650–5670 with weak WiFi, COL N (5905–5925) is in the C-V2X band. Check Link Watch noise and
      the Site Matrix for Collins after the change.
- [ ] Rescan mmc-rkt-o13 (empty scan) and import all scans on the Diagnostics tab
      (WiFi survey → Import) so the app keeps them.

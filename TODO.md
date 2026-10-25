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

## Pending radio work (outside the app)

Channel plan for Collins + MMC + EOC together (Channel Map → Plan channels, Radios = the 12
infrastructure radios, "One channel per sector" on), 2026-10-20. Replaces the 2026-10-19 Collins-only
plan, which shared channels between opposite sectors. Today: 9 critical / 24 warning; with this plan
0 critical / 2 warning (Collins W shares 5875 with the MMC omni 6.2 km away, Collins E shares 5660 with
the EOC omni 13 km away). Each Collins sector gets its own channel, as AREDN's channel-planning guide
recommends. The critical left on the Channel Map is kf0jcl-qth vs kj0dx-qth-sq (not infrastructure).

- [ ] Start a maintenance window for COL, MMC and EOC (Diagnostics → Maintenance, RF peers
      included) so alarms and reports ignore the change.
- [ ] COL N on ch 183 (5915/20): w0gq-col-rkt-n with kf0jcl-qth-litebeam5ac.
- [ ] COL E on ch 132 (5660/20): w0gq-col-rkt-e with kj0dx-qth-ub.
- [ ] COL S on ch 155 (5775/20): w0gq-col-rkt-s with kf9mt-home-litebeam.
- [ ] COL W on ch 175 (5875/20): w0gq-col-rkt-w with ka0ies-square-one.
- [ ] COL ↔ MMC backbone on ch 142 (5710/40): w0gq-col-sq-2mmc and w0gq-mmc-sq-2col together
      (also gets it off TDWR: today 5640–5680).
- [ ] MMC ↔ EOC backbone stays on ch 165 (5825/40).
- [ ] EOC omni w0gq-eoc-rkt-o13 to ch 132 (5660/20); MMC omni w0gq-mmc-rkt-o13 to ch 175 (5875/20).
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
- [ ] Trade-offs to watch: COL S (5765–5785) is in busy UNII-3 WiFi, COL E and the EOC omni
      (5650–5670) touch the first 10 MHz above TDWR, COL N (5905–5925) is in the C-V2X band.
      Check Link Watch noise for those radios after the change.

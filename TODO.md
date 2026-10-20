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
- [ ] 5. **Health score + "what changed?"** Score each node/link 0–100 over time;
      clicking a drop lists new neighbors, reboots, config changes, noise jumps, route
      changes and starting-node outages in that window.

## Troubleshooting checks

- [ ] 6. **Hidden-node / airtime check per sector.** Clients that can't hear each other,
      combined with channel busy and retries; suggest RTS/CTS or fewer clients.
- [x] 7. **Noise-floor watch.** Noise (signal − SNR) per radio over time; alert on jumps
      of 5 dB or more; show the time-of-day pattern (WiFi follows when people are home).
- [x] 8. **One-way link alarm.** One end's LQ high and the other low, routing cost
      infinite, or one end knows the other only by MAC. Flag it continuously instead of
      only when someone runs a diagnosis.
- [ ] 9. **Route-change log.** Periodic traceroute along key paths (starting node to
      each site, MMC↔COL↔EOC); record hop count and path; alert on detours.
- [x] 10. **Starting-node health page.** Outages, latency, DNS resolution time and lost
      scans, to separate problems with this PC or its uplink from problems on the mesh.
- [ ] 11. **Power-cut advisor.** Predict receive levels at every neighbor after a
      proposed TX power cut, so nobody drops below the SNR they need. Never cut
      infrastructure power.
- [x] 12. **Firmware/settings drift report.** Mismatches in firmware, mode, SSID, width,
      distance setting and LQM settings, within a site and across each link.
- [ ] 13. **Weather correlation.** Overlay NWS rain, temperature and wind on SNR/LQ
      (rain fade, ice, wind-shifted antennas).
- [ ] 14. **Antenna aim check.** Compare each antenna's set azimuth with the bearings of
      the neighbors it hears best; flag misaims or unset azimuths.

## New ways to view the data

- [ ] 15. **Heatmap, node × hour.** Colored by reachability or LQ; shows whole sites
      dropping together and nightly patterns.
- [ ] 16. **Site neighbor matrix.** Every radio at a site against every radio it hears,
      with SNR/LQ per cell; one-way and lopsided pairs stand out.
- [ ] 17. **Link swimlanes.** One bar per link, colored good/flapping/down over time,
      with reboots and config changes marked.
- [ ] 18. **Path view.** Current and historical route between two nodes, each hop
      colored by link quality.
- [ ] 19. **Site dashboard.** Per-site page (COL, MMC, EOC): radios, spectrum chart,
      clients per sector, uplink paths, node health.
- [ ] 20. **Weakest-link / single-point-of-failure map.** Rank links whose loss splits
      the mesh, weighted by how many nodes are behind each one.

## Alerts and operations

- [x] 21. **Notifications.** Email, Discord, ntfy or APRS when an infrastructure node
      goes down, a sector loses a client, the starting node is lost, or a link score
      drops; respect maintenance windows.
- [x] 22. **Maintenance windows.** Planned work is excluded from reports and alarms.
- [ ] 23. **Notes on nodes and links.** For example "tilted 2° on 10/20" or "call
      KF9MT first", shown in diagnoses and on charts.
- [x] 24. **Cleanup of stale and junk entries.** Exclude or delete nodes that never
      answer (k9lrd-home, the fake "10" node, `mac-…` placeholders). Exclude the
      kc0wkp-ar750 legacy TUN clients from scanning.
- [ ] 25. **Weekly report.** Saved or emailed summary of the worst links, new problems,
      infrastructure verdicts and changes.

## Planning

- [ ] 26. **Link budget + terrain.** Free-space loss, antenna gains, Fresnel zone and
      elevation data give a predicted SNR to compare with the measured SNR
      (obstructions, misaims).
- [ ] 27. **Capacity view.** iPerf results with bit rate and channel busy give the spare
      capacity on each backbone link (MMC–COL, MMC–EOC).
- [ ] 28. **Scheduled iPerf.** Overnight runs on key links, trended over time.

## Pending radio work (outside the app)

Collins plan from the channel planner (Channel Map → Plan channels, site COL), 2026-10-19.
It brings Collins's own conflicts from 8 critical / 24 warning to 0 critical / 2 warning.

- [ ] Start a maintenance window for COL (Diagnostics → Maintenance, site COL, RF peers
      included) so alarms and reports ignore the change.
- [ ] N + S sectors on ch 132 (5660/20): w0gq-col-rkt-n with kf0jcl-qth-litebeam5ac,
      w0gq-col-rkt-s with kf9mt-home-litebeam. Opposite sectors share the channel (shared airtime).
- [ ] E + W sectors on ch 173 (5865/20): w0gq-col-rkt-e with kj0dx-qth-ub,
      w0gq-col-rkt-w with ka0ies-square-one.
- [ ] MMC ↔ COL link on ch 142 (5710/40): w0gq-col-sq-2mmc and w0gq-mmc-sq-2col together.
- [ ] Turn off w0gq-col-sq-2bldg112 and w0gq-col-sq-2hiwt until their far ends exist.
- [ ] Move each client before (or with) its sector, so no link is stranded.
- [ ] Lower kf9mt-home-litebeam's TX power (near-far at Collins). Never Collins's.
- [ ] Fix ka0ies-square-one's azimuth setting: set to 135°, but Collins is at 43° from it.
- [ ] Afterwards: check RF Stats for an rkt-n ↔ rkt-s link or clients linked to the opposite
      sector, and compare before/after on the Changes tab.
- [ ] Note: sharing a channel between opposite sectors is shared airtime. AREDN's channel-planning
      guide (Collocated Nodes) recommends a different channel per sector, because sharing can halve site
      throughput. At Collins alone there isn't room ("One channel per sector" in the planner gives 5 warnings
      and lands N/W on the MMC/EOC omni channels). Long term: plan COL + MMC + EOC together (moves the
      MMC↔EOC and MMC↔COL backbones) so each sector gets its own channel.
- [ ] Known leftovers: E/W are only 10 MHz from the EOC↔MMC dish (5805–5845, aimed toward
      Collins); fixing that means moving MMC radios. Not Collins: k0cta-130-51-138 vs
      w0gq-mmc-rkt-o13 and kf0jcl-qth vs kj0dx-qth-sq.

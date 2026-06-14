# AI Incident Reporter — Design Plan

Status: IMPLEMENTED (steps 1 + 2). See `incident_report.py` and
`GET /api/reports/incident/<node>`; surfaced via the "Generate Report" button on
the node page. The AI narrative (step 2) is opt-in behind
`INCIDENT_REPORT_AI_ENABLED` + `ANTHROPIC_API_KEY` and degrades to the
deterministic report when off/unavailable. This document remains the design
rationale; build-order section below is retained for history.

## Guiding principles

- **Offline / post-incident, never in the live outage path.** An outage is the
  worst time to depend on an external API call. The reporter runs on demand or
  on a schedule against data already stored locally / in CouchDB.
- **AI never decides status.** Per `CLAUDE.md`, distributed status is derived
  deterministically from observations and heartbeats. The reporter only
  *explains* what the deterministic layer already recorded.
- **The model summarizes; Python diagnoses.** All numeric thresholds, flap
  counts, asymmetry deltas, and reboot detection are computed in Python (already
  done). The model turns the assembled, pre-computed evidence into a readable
  narrative. It is given facts, not raw firehose.
- **No secrets in prompts.** Build the evidence bundle from the same fields we
  put in replicated observation docs; never include credentials or tokens.

## Why this is worth doing now (and wasn't before)

Before this change the only per-link signals were `quality` and `snr` at 60s
resolution — too thin for either a rule or a model to explain a flap. We now
store, per link/poll: LQM `blocked`/`blocked_reason`, `rev_snr`, full `raw_tracker`,
a structured `link_state_log`, node health (`uptime`/load/memory/`channel_busy`),
reboot events, asymmetry, and high-rate bidirectional `incident` samples. That is
enough evidence for a genuinely useful root-cause narrative.

## Data sources (all already persisted)

| Source | What it contributes |
| --- | --- |
| `link_state_log` | Flap timeline: up/down/blocked/unblocked transitions with reason |
| `link_history` (`sample_type` scan/incident) | Quality/SNR/ping/throughput trend, incident bursts |
| `node_health_history` | Uptime resets (reboots), load spikes, memory pressure, channel busy |
| `events` | Human-facing event stream (reboot, degraded, frequency change) |
| `get_link_flap_report()` | Pre-computed flaps/hour + dominant block reason |
| `get_link_asymmetry_report()` | Pre-computed SNR asymmetry per link |
| CouchDB observations | Same data across collectors (multi-site correlation) |

## Architecture

```
incident_report.py
  ├─ gather_evidence(link or node, window)   # pure Python, deterministic
  │     → EvidenceBundle (JSON): flap stats, state timeline, health timeline,
  │       asymmetry, incident ping summary, correlated events, reboot flags
  ├─ summarize(bundle)                        # the ONLY AI call
  │     → narrative text + structured findings
  └─ render(bundle, narrative)                # markdown / JSON for UI + API
```

Keep it a standalone module (mirroring `observations.py`'s separation) so it can
run from a CLI, a scheduled job, or an on-demand REST route without dragging in
Flask/Socket.IO.

### `gather_evidence` (deterministic, no AI)

Assembles a compact, pre-digested bundle so the model never sees raw rows:

- Flap summary from `get_link_flap_report(hours, node)`.
- Condensed state timeline (collapse runs; keep transitions + timestamps).
- Health summary: reboot count/times, max/median load, min free memory, max
  channel busy, % of polls degraded.
- Asymmetry: within-row and cross-direction SNR deltas.
- Incident-probe summary: forward vs reverse loss/latency (isolates direction).
- Correlation hints computed in Python, e.g. "every `down` within 30s of a
  `channel_busy` spike" or "flaps stop after a reboot" — these are the high-value
  facts; the model phrases them, it does not infer them.

### `summarize` (single AI call)

- **Provider:** Anthropic Claude API. Default model: a cost-effective current
  model (`claude-sonnet-4-6`) since this is structured-input summarization;
  allow override to a larger model for cross-site analysis. Make model + API key
  configurable via env (`ANTHROPIC_API_KEY`, `INCIDENT_REPORT_MODEL`).
- **Input:** the EvidenceBundle as JSON in the prompt. Small (KB), so no
  chunking/RAG needed.
- **Output:** request structured output — a short root-cause sentence, a ranked
  list of candidate causes with the supporting evidence field for each, and a
  recommended next action. Validate/parse before display.
- **Cost control:** one call per report; cache by `(target, window, latest
  state-log id)` so re-opening an unchanged report costs nothing. Enable prompt
  caching for the static instruction preamble.
- **Confirm before sending:** sending the bundle to an external API publishes it;
  gate the first send in a session behind explicit operator action.

### Failure handling

If the API key is absent or the call fails, return the deterministic bundle
rendered as plain markdown (the facts) with no narrative. The tool degrades to a
structured report, never an error page — and is fully useful offline.

## Surfacing

- **CLI:** `python incident_report.py --node w0gq-col --hours 24` → markdown.
- **REST (on demand):** `GET /api/reports/incident/<node>?hours=24`. Returns the
  bundle always; includes the narrative only when AI is enabled and authorized.
- **Scheduled (optional):** a daily job that runs only for `WATCHED_NODES` with
  flaps above a threshold, writing the report to disk / a CouchDB report doc.
  This keeps cost bounded and avoids any call during a live incident.

## Build order

1. `gather_evidence` + the `/api/reports/incident/<node>` route returning the
   bundle as JSON/markdown (no AI). Immediately useful on its own.
2. Add `summarize` behind an `INCIDENT_REPORT_AI_ENABLED` flag + API key.
3. Add caching and the optional scheduled job for watched nodes.

## Explicitly out of scope

- No AI in `scanner.py`, status derivation, or any scheduled scan path.
- No model-driven thresholds — all numeric judgment stays in Python so behavior
  is reproducible and testable offline.

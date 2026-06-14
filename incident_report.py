"""
Incident reporter for AREDN flap/outage root-cause analysis.

Two layers, strictly separated:

1. ``gather_evidence`` / ``deterministic_findings`` / ``render_markdown`` -- pure
   Python over already-stored data. Always works offline, no external calls.
2. ``summarize`` -- an optional single Claude API call that turns the evidence
   bundle into a plain-English narrative. Opt-in via config, never in the live
   scan/outage path, never used to decide status.

``build_report`` ties them together and degrades gracefully to the deterministic
report when AI is disabled or unavailable.
"""

import json
import logging

import config
import database
import link_health

logger = logging.getLogger(__name__)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _node_health_summary(node_name, hours):
    samples = database.get_node_health_history(node_name, hours=hours)
    summary = {
        'samples': len(samples),
        'reboots': 0,
        'degraded_polls': 0,
        'unreachable_polls': 0,
        'max_load1': None,
        'min_mem_free': None,
        'max_channel_busy': None,
        'latest_uptime_seconds': None,
    }
    prev_uptime = None
    loads, mems, busy = [], [], []
    for s in samples:
        if not s.get('reachable'):
            summary['unreachable_polls'] += 1
        if s.get('degraded'):
            summary['degraded_polls'] += 1
        up = s.get('uptime_seconds')
        if up is not None:
            if prev_uptime is not None and up + 30 < prev_uptime:
                summary['reboots'] += 1
            prev_uptime = up
            summary['latest_uptime_seconds'] = up
        if s.get('load1') is not None:
            loads.append(s['load1'])
        if s.get('mem_free') is not None:
            mems.append(s['mem_free'])
        if s.get('channel_busy') is not None:
            busy.append(s['channel_busy'])
    if loads:
        summary['max_load1'] = max(loads)
    if mems:
        summary['min_mem_free'] = min(mems)
    if busy:
        summary['max_channel_busy'] = max(busy)
    return summary


def _incident_summary(node_name, hours):
    """Aggregate incident probe loss/latency per direction (which way is bad)."""
    samples = database.get_incident_samples(node_name, hours=hours)
    by_dir = {}
    for s in samples:
        key = (s['source_node'], s['target_node'])
        agg = by_dir.setdefault(key, {'count': 0, 'loss_sum': 0.0, 'loss_n': 0,
                                      'lat_sum': 0.0, 'lat_n': 0})
        agg['count'] += 1
        if s.get('ping_loss') is not None:
            agg['loss_sum'] += s['ping_loss']
            agg['loss_n'] += 1
        if s.get('ping_avg') is not None:
            agg['lat_sum'] += s['ping_avg']
            agg['lat_n'] += 1
    directions = []
    for (src, tgt), agg in by_dir.items():
        directions.append({
            'source_node': src,
            'target_node': tgt,
            'samples': agg['count'],
            'avg_loss_pct': round(agg['loss_sum'] / agg['loss_n'], 1) if agg['loss_n'] else None,
            'avg_latency_ms': round(agg['lat_sum'] / agg['lat_n'], 1) if agg['lat_n'] else None,
        })
    directions.sort(key=lambda d: (d['avg_loss_pct'] or 0), reverse=True)
    return {'total_samples': len(samples), 'directions': directions}


def _condense_link_health(node_name, hours):
    """Keep only the diagnostic fields from per-link health for each peer."""
    condensed = []
    for h in link_health.analyze_node_links(node_name, hours=hours):
        condensed.append({
            'peer': h.get('peer'),
            'link_type': h.get('link_type'),
            'grade': (h.get('overall') or {}).get('grade'),
            'lqm_block': h.get('lqm_block'),
            'snr_asymmetry': h.get('snr_asymmetry'),
            'flapping': {
                'flap_count': (h.get('flapping') or {}).get('flap_count'),
                'cause_label': (h.get('flapping') or {}).get('cause_label'),
                'top_block_reason': (h.get('flapping') or {}).get('top_block_reason'),
            },
            'snr': (h.get('snr') or {}).get('snr'),
            'loss_avg': (h.get('ping') or {}).get('loss_avg'),
            'latency_avg': (h.get('ping') or {}).get('latency_avg'),
        })
    return condensed


def gather_evidence(node_name, hours=24):
    """Assemble a compact, pre-digested evidence bundle for a node (no AI)."""
    node = database.get_node(node_name)
    return {
        'node': node_name,
        'window_hours': hours,
        'node_exists': node is not None,
        'flap_report': database.get_link_flap_report(hours=hours, node=node_name),
        'link_health': _condense_link_health(node_name, hours),
        'node_health': _node_health_summary(node_name, hours),
        'incident_probes': _incident_summary(node_name, hours),
        'asymmetry': [
            a for a in database.get_link_asymmetry_report(min_delta=3.0)
            if a['source_node'] == node_name or a['target_node'] == node_name
        ],
    }


def deterministic_findings(bundle):
    """Rank candidate root causes from the evidence using plain rules."""
    findings = []
    health = bundle['node_health']

    if health['reboots']:
        findings.append({
            'cause': 'node_reboots',
            'severity': 'high',
            'evidence': f"{health['reboots']} reboot(s) detected in the window "
                        "(uptime reset). Flaps coinciding with these are the node "
                        "restarting, not the RF link.",
        })
    if health['unreachable_polls']:
        findings.append({
            'cause': 'node_unreachable',
            'severity': 'high',
            'evidence': f"{health['unreachable_polls']} poll(s) could not reach the node at all.",
        })
    if health['max_load1'] is not None and health['max_load1'] >= 3.0:
        findings.append({
            'cause': 'cpu_overload',
            'severity': 'medium',
            'evidence': f"Peak 1-min load {health['max_load1']} suggests CPU pressure "
                        "(can cause slow/degraded sysinfo and apparent drops).",
        })
    if health['max_channel_busy'] is not None and health['max_channel_busy'] >= 50:
        findings.append({
            'cause': 'channel_congestion',
            'severity': 'medium',
            'evidence': f"Channel busy peaked at {health['max_channel_busy']}% "
                        "(congestion/interference).",
        })

    for link in bundle['link_health']:
        block = link.get('lqm_block') or {}
        if block.get('blocked'):
            findings.append({
                'cause': 'lqm_blocked',
                'severity': 'high',
                'evidence': f"LQM is blocking {link['peer']} (reason: {block.get('reason')}). "
                            "This is AREDN itself tearing the link down.",
            })
        asym = link.get('snr_asymmetry') or {}
        if asym.get('rating') == 'poor':
            findings.append({
                'cause': 'snr_asymmetry',
                'severity': 'medium',
                'evidence': f"{link['peer']}: {asym.get('details')} -- one direction is much weaker.",
            })

    probes = bundle['incident_probes']
    for d in probes['directions']:
        if d.get('avg_loss_pct') is not None and d['avg_loss_pct'] >= 50:
            findings.append({
                'cause': 'directional_loss',
                'severity': 'high',
                'evidence': f"Incident probes show {d['avg_loss_pct']}% loss "
                            f"{d['source_node']} -> {d['target_node']} -- this direction is failing.",
            })

    if not findings:
        findings.append({
            'cause': 'insufficient_or_healthy',
            'severity': 'info',
            'evidence': 'No strong flap indicators in this window. Let the node poll '
                        'through a few flap cycles, or widen the window.',
        })

    sev_rank = {'high': 0, 'medium': 1, 'info': 2}
    findings.sort(key=lambda f: sev_rank.get(f['severity'], 3))
    return findings


def render_markdown(bundle, findings, narrative=None):
    """Render the evidence + findings (and optional AI narrative) as markdown."""
    lines = [f"# Incident report: {bundle['node']}",
             f"_Window: last {bundle['window_hours']}h_", ""]

    if narrative:
        lines += ["## Summary", narrative, ""]

    lines.append("## Candidate causes (deterministic)")
    for f in findings:
        lines.append(f"- **{f['cause']}** ({f['severity']}): {f['evidence']}")
    lines.append("")

    h = bundle['node_health']
    lines += [
        "## Node health",
        f"- Samples: {h['samples']}, reboots: {h['reboots']}, "
        f"degraded: {h['degraded_polls']}, unreachable: {h['unreachable_polls']}",
        f"- Peak load(1m): {h['max_load1']}, min free mem: {h['min_mem_free']} KB, "
        f"peak channel busy: {h['max_channel_busy']}%",
        "",
    ]

    if bundle['flap_report']:
        lines.append("## Flapping links")
        for l in bundle['flap_report']:
            lines.append(f"- {l['source_node']} -> {l['target_node']}: "
                         f"{l['downs']} downs ({l['flaps_per_hour']}/h), "
                         f"top reason: {l.get('top_block_reason') or 'n/a'}")
        lines.append("")

    probes = bundle['incident_probes']
    if probes['directions']:
        lines.append("## Incident probe directions")
        for d in probes['directions']:
            lines.append(f"- {d['source_node']} -> {d['target_node']}: "
                         f"loss {d['avg_loss_pct']}%, latency {d['avg_latency_ms']}ms "
                         f"({d['samples']} samples)")
        lines.append("")

    return "\n".join(lines)


def summarize(bundle, findings):
    """Optional single Claude call. Returns narrative text or None on any issue."""
    if not config.INCIDENT_REPORT_AI_ENABLED:
        return None
    if not config.ANTHROPIC_API_KEY:
        logger.info("Incident AI summary enabled but ANTHROPIC_API_KEY is unset")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; skipping AI summary")
        return None

    system = (
        "You are a network engineer assistant for an AREDN mesh. You are given a "
        "pre-computed JSON evidence bundle and deterministic findings for one node. "
        "Write a concise plain-English root-cause summary (3-6 sentences). Only use "
        "facts in the bundle. Do not invent metrics. Do not assign a status; the "
        "system decides status separately."
    )
    payload = json.dumps({'evidence': bundle, 'findings': findings}, default=str)
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=config.INCIDENT_REPORT_MODEL,
            max_tokens=600,
            system=system,
            messages=[{'role': 'user', 'content': payload}],
        )
        parts = [block.text for block in message.content if getattr(block, 'type', None) == 'text']
        return "\n".join(parts).strip() or None
    except Exception as exc:
        logger.warning("Incident AI summary failed: %s", exc)
        return None


def build_report(node_name, hours=24):
    """Full report: deterministic bundle + findings, optional AI narrative."""
    bundle = gather_evidence(node_name, hours=hours)
    findings = deterministic_findings(bundle)
    narrative = summarize(bundle, findings)
    return {
        'node': node_name,
        'window_hours': hours,
        'ai_enabled': config.INCIDENT_REPORT_AI_ENABLED,
        'ai_used': narrative is not None,
        'findings': findings,
        'evidence': bundle,
        'narrative': narrative,
        'markdown': render_markdown(bundle, findings, narrative),
    }

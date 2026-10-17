"""
Mesh Health Check for AREDN Network Monitor

Network-wide, deterministic problem finder over the legacy SQLite state. Where
link_health.py grades one node's links, this module looks across the whole
in-scope mesh for the problems that actually break RF meshes:

- Near-far / overpowering radios: one neighbor so loud (too close or too much
  TX power) that it desensitizes a receiver and masks weaker stations
- SNR imbalance between the two directions of a link
- TX/RX PHY-rate and throughput imbalance
- One-way links (A hears B, B doesn't hear A)
- Packet loss, latency and jitter
- VoIP stability (E-model MOS from ping data, call tests, streamed sessions)
- Flapping and LQM-blocked links, weak/unstable signal
- Noise floor, channel congestion, node reboots/overload/unreliable polling
- Hidden-node pairs, single points of failure, RF-isolated nodes

The scope honours operator scan boundaries: only nodes reachable from the scan
seed without crossing a boundary are diagnosed, so remote meshes don't clutter
a local-event report. Findings are suggestions from heuristics, not verdicts.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime

import config
import database
import link_health

logger = logging.getLogger(__name__)

# ============ Thresholds ============

# Near-far: a "hot" signal is strong enough to risk receiver desense. A weak
# neighbor at the same node is one LQM is likely to penalize or block.
HOT_SNR = 40.0            # dB
HOT_SIGNAL_DBM = -45.0    # dBm
OVERLOAD_SNR = 55.0       # dB - receiver overload risk on its own
OVERLOAD_SIGNAL_DBM = -35.0
WEAK_SNR = 15.0           # dB - AREDN LQM's default minimum SNR
NEAR_FAR_SPREAD = 25.0    # dB between hottest and weakest neighbor

SNR_GAP_INFO = 3.0
SNR_GAP_WARNING = 6.0
SNR_GAP_CRITICAL = 10.0
NOISE_GAP_INTERFERENCE = 6.0   # dB noisier on one end -> local interference

RATE_RATIO_WARNING = 0.5
RATE_RATIO_CRITICAL = 0.33
LOW_PHY_RATE_MBPS = 6.0
THROUGHPUT_RATIO_WARNING = 0.5

LOSS_WARNING = 1.0
LOSS_CRITICAL = 5.0
LATENCY_WARNING = 100.0
JITTER_WARNING = 20.0

MOS_WARNING = 3.6
MOS_CRITICAL = 3.1

FLAP_WARNING = 3
FLAP_CRITICAL = 8

SNR_SWING_WARNING = 15.0   # dB min->max over the window

NOISE_WARNING = -88.0
NOISE_CRITICAL = -80.0

CHANNEL_BUSY_WARNING = 50.0
CHANNEL_BUSY_CRITICAL = 75.0

LOAD_WARNING = 2.0
MEM_FREE_WARNING_PCT = 10.0
UNREACHABLE_WARNING_PCT = 20.0
DEGRADED_WARNING = 3

SPOF_WARNING_NODES = 3     # cut-off nodes for a single point of failure warning

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}

CATEGORY_LABELS = {
    'near_far': 'Near-far / overpowering radio',
    'snr_imbalance': 'SNR imbalance',
    'rate_imbalance': 'TX/RX imbalance',
    'one_way': 'One-way link',
    'packet_loss': 'Packet loss / latency',
    'voip': 'VoIP stability',
    'flapping': 'Link flapping',
    'blocked': 'LQM blocked link',
    'weak_signal': 'Weak / unstable signal',
    'interference': 'Noise / interference',
    'congestion': 'Channel congestion',
    'node_health': 'Node health',
    'hidden_node': 'Hidden node',
    'topology': 'Topology / single point of failure',
}

# AREDN LQM block reasons -> what to do about them.
BLOCK_REASON_HINTS = {
    'signal': 'SNR is below the LQM minimum; improve antenna aim/height or the path.',
    'distance': 'Neighbor is beyond the configured max distance; raise the LQM max distance or verify GPS/coordinates.',
    'quality': 'Too many retransmissions; look for interference, hidden nodes or a near-far problem at either end.',
    'retries': 'Too many retransmissions; look for interference, hidden nodes or a near-far problem at either end.',
    'latency': 'Link latency is too high; check for congestion or a saturated hop.',
    'dtd': 'A DtD (cable) path to this neighbor is preferred, so RF is blocked by design.',
    'dup': 'Duplicate path to the same neighbor; usually expected.',
    'user': 'Blocked manually by the node owner.',
}


def _num(value):
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_rf(link):
    return str(link.get('link_type') or '').upper() == 'RF'


def _low_quality(link, threshold):
    quality = link.get('quality')
    return quality is not None and quality < threshold


def _pair(a, b):
    return tuple(sorted((a, b)))


class _Findings:
    def __init__(self):
        self.items = []

    def add(self, severity, category, title, nodes, detail, recommendation, evidence=None):
        self.items.append({
            'severity': severity,
            'category': category,
            'category_label': CATEGORY_LABELS.get(category, category),
            'title': title,
            'nodes': list(nodes),
            'subject': ' ↔ '.join(nodes) if len(nodes) == 2 else ', '.join(nodes[:4]),
            'detail': detail,
            'recommendation': recommendation,
            'evidence': evidence or {},
        })


# ============ Scope ============

def find_seed_node(nodes):
    """Best guess at the scan seed: the active node discovered at depth 0."""
    active = [n for n in nodes if n.get('is_active')]
    for n in active:
        if n.get('scan_depth') == 0:
            return n['name']
    start = database.get_setting('starting_node', config.STARTING_NODE) or ''
    host = start.replace('http://', '').replace('https://', '').split('/')[0].split(':')[0].lower()
    host = host.replace('.local.mesh', '')
    for n in active:
        if n['name'] == host or (n.get('ip') and n['ip'] == host):
            return n['name']
    return None


def compute_scope(nodes, links, boundaries):
    """Nodes reachable from the seed over current links without crossing a
    scan boundary or supernode. Mirrors the scanner's traversal rules."""
    by_name = {n['name']: n for n in nodes}
    seed = find_seed_node(nodes)
    adjacency = defaultdict(set)
    for link in links:
        adjacency[link['source_node']].add(link['target_node'])
        adjacency[link['target_node']].add(link['source_node'])

    if seed is None:
        scope = {n['name'] for n in nodes
                 if n.get('is_active') and boundaries.get(n['name']) != 'exclude'}
        return seed, scope

    scope = {seed}
    queue = deque([seed])
    while queue:
        name = queue.popleft()
        node = by_name.get(name) or {}
        if name != seed and (name in boundaries or node.get('is_supernode')):
            continue
        for peer in adjacency[name]:
            if peer in scope or boundaries.get(peer) == 'exclude':
                continue
            peer_node = by_name.get(peer)
            # Only expand through nodes this collector actually polls; a
            # link-only endpoint is a leaf.
            scope.add(peer)
            if peer_node and peer_node.get('is_active'):
                queue.append(peer)
    return seed, scope


# ============ Checks ============

def _check_near_far(f, rf_by_source, key_nodes, scope):
    hot_at = defaultdict(list)   # hot transmitter -> [(receiver, snr, signal)]
    for receiver, links in rf_by_source.items():
        if receiver not in scope:
            continue
        current = [l for l in links if l.get('status') == 'good']
        measured = [(l, _num(l.get('snr')), _num(l.get('signal'))) for l in current]
        measured = [m for m in measured if m[1] is not None]
        if not measured:
            continue

        hot = [m for m in measured
               if m[1] >= HOT_SNR or (m[2] is not None and m[2] >= HOT_SIGNAL_DBM)]
        for link, snr, signal in hot:
            hot_at[link['target_node']].append((receiver, snr, signal))

        if not hot or len(measured) < 2:
            continue
        loudest = max(hot, key=lambda m: m[1])
        weak = [m for m in measured
                if m[0] is not loudest[0]
                and (m[1] < WEAK_SNR or m[0].get('blocked') or _low_quality(m[0], config.QUALITY_POOR))
                and loudest[1] - m[1] >= NEAR_FAR_SPREAD]
        if not weak:
            continue

        loud_name = loudest[0]['target_node']
        weak_names = [m[0]['target_node'] for m in weak]
        any_blocked = any(m[0].get('blocked') for m in weak)
        severity = 'critical' if (any_blocked or receiver in key_nodes or len(weak) >= 2) else 'warning'
        weak_txt = ', '.join(f"{m[0]['target_node']} {m[1]:.0f} dB" + (' (blocked)' if m[0].get('blocked') else '')
                             for m in weak[:5])
        f.add(
            severity, 'near_far',
            f'{loud_name} may be drowning out weaker neighbors at {receiver}',
            [receiver, loud_name] + weak_names,
            f'{receiver} hears {loud_name} at {loudest[1]:.0f} dB SNR'
            + (f' ({loudest[2]:.0f} dBm)' if loudest[2] is not None else '')
            + f' but its other neighbors only at {weak_txt}. A very strong close-in signal can '
            'desensitize the receiver and dominate airtime, so weaker stations get penalized or '
            'blocked by LQM and the mesh loses paths through ' + receiver + '.'
            + (' ' + receiver + ' is a key node (single point of failure).' if receiver in key_nodes else ''),
            f'Reduce TX power on {loud_name}, increase physical separation or antenna isolation '
            f'between {loud_name} and {receiver} (vertical separation, aim away, shielding), or move '
            f'one of them to another channel/band. Re-check the weak neighbors after the change.',
            {'receiver': receiver, 'hot': loud_name, 'hot_snr': loudest[1], 'hot_signal': loudest[2],
             'weak': [{'node': m[0]['target_node'], 'snr': m[1], 'blocked': bool(m[0].get('blocked'))} for m in weak]}
        )

    # A transmitter that is "hot" at several receivers is likely running too
    # much power for the deployment, not just sited badly relative to one node.
    for tx, hits in hot_at.items():
        overload = [h for h in hits if h[1] >= OVERLOAD_SNR or (h[2] is not None and h[2] >= OVERLOAD_SIGNAL_DBM)]
        if len(hits) >= 2 or overload:
            receivers = ', '.join(f'{r} {s:.0f} dB' for r, s, _ in hits[:6])
            f.add(
                'warning', 'near_far',
                f'{tx} is very loud at {len(hits)} node(s)',
                [tx] + [h[0] for h in hits],
                f'{tx} is received at {receivers}. '
                + ('Signals this strong risk overloading the receiver front end. ' if overload else '')
                + 'An over-powered or too-close radio raises everyone else\'s effective noise and '
                'can keep neighbors from hearing more distant key nodes.',
                f'Lower TX power on {tx} until its strongest neighbor is around 25-35 dB SNR, or '
                'relocate/re-aim it. Close-in event radios often need only a few dBm.',
                {'transmitter': tx, 'receivers': [{'node': r, 'snr': s, 'signal': g} for r, s, g in hits]}
            )


def _check_link_pairs(f, rf_index, node_noise, scope, polled_scope):
    """Per RF pair: SNR imbalance, rate imbalance, one-way, weak signal, blocks."""
    seen_pairs = set()
    for (src, tgt), link in rf_index.items():
        if src not in scope:
            continue
        pair = _pair(src, tgt)
        reverse = rf_index.get((tgt, src))
        current = link.get('status') == 'good'

        # ---- one-way: A hears B but B (polled, in scope) doesn't list A
        if current and tgt in polled_scope and (reverse is None or reverse.get('status') != 'good'):
            f.add(
                'warning', 'one_way',
                f'One-way RF link: {src} hears {tgt}, but {tgt} does not hear {src}',
                [src, tgt],
                f'{src} reports {tgt} at {link.get("snr") or "?"} dB SNR, but {tgt}\'s own neighbor '
                f'list has no current RF entry for {src}. Traffic can only flow well in one direction.',
                f'Check TX power and antenna on {src} (its signal is not reaching {tgt}), and look for '
                f'interference or a near-far problem at {tgt}. Compare noise floors on both ends.',
                {'snr': _num(link.get('snr'))}
            )

        if not current:
            continue

        # ---- LQM blocked
        if link.get('blocked'):
            reason = (link.get('blocked_reason') or 'unspecified')
            hint = next((v for k, v in BLOCK_REASON_HINTS.items() if k in reason.lower()), None)
            f.add(
                'warning', 'blocked',
                f'LQM is blocking {src} → {tgt} ({reason})',
                [src, tgt],
                f'{src}\'s Link Quality Manager has blocked its link to {tgt}: {reason}.',
                hint or 'Inspect both radios\' LQM pages to see why the link fails the LQM thresholds.',
                {'reason': reason, 'snr': _num(link.get('snr')), 'quality': link.get('quality')}
            )

        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        snr = _num(link.get('snr'))              # src hears tgt
        rev_snr = _num(link.get('rev_snr'))      # tgt hears src (as reported to src)
        if reverse is not None and reverse.get('status') == 'good' and _num(reverse.get('snr')) is not None:
            rev_snr = _num(reverse.get('snr'))  # tgt's own measurement wins

        # ---- SNR imbalance
        if snr is not None and rev_snr is not None:
            gap = abs(snr - rev_snr)
            if gap >= SNR_GAP_INFO:
                if snr > rev_snr:
                    strong_rx, weak_rx, weak_tx = src, tgt, src
                else:
                    strong_rx, weak_rx, weak_tx = tgt, src, tgt
                severity = 'critical' if gap >= SNR_GAP_CRITICAL else 'warning' if gap >= SNR_GAP_WARNING else 'info'
                n_weak = node_noise.get(weak_rx)
                n_strong = node_noise.get(strong_rx)
                if n_weak is not None and n_strong is not None and n_weak - n_strong >= NOISE_GAP_INTERFERENCE:
                    cause = (f'{weak_rx}\'s noise floor ({n_weak:.0f} dBm) is {n_weak - n_strong:.0f} dB higher '
                             f'than {strong_rx}\'s, so local interference at {weak_rx} is the likely cause.')
                    fix = (f'Hunt the interference at {weak_rx} (co-located radios, other Part 15 gear, a '
                           f'near-far neighbor); try another channel or add antenna isolation.')
                else:
                    cause = (f'Noise floors are similar, so the usual cause is {weak_tx} transmitting with less '
                             f'power or antenna gain than {strong_rx}, or a one-sided obstruction/aim problem.')
                    fix = (f'Equalize TX power (raise {weak_tx} or lower {strong_rx}), and check antenna aim, '
                           f'gain and feedline on {weak_tx}.')
                f.add(
                    severity, 'snr_imbalance',
                    f'{gap:.0f} dB SNR imbalance on {src} ↔ {tgt}',
                    [src, tgt],
                    f'{strong_rx} hears its peer at {max(snr, rev_snr):.0f} dB but {weak_rx} hears {strong_rx} '
                    f'at only {min(snr, rev_snr):.0f} dB. ' + cause
                    + ' The link runs at the rate the weaker direction supports, and TCP ACKs on the weak '
                    'side cap throughput.',
                    fix,
                    {'snr': snr, 'rev_snr': rev_snr, 'gap': round(gap, 1),
                     'weak_receiver': weak_rx, 'noise_weak': n_weak, 'noise_strong': n_strong}
                )

        # ---- PHY rate imbalance / low rate
        tx = _num(link.get('tx_rate'))
        rx = _num(link.get('rx_rate'))
        if tx and rx and tx > 0 and rx > 0:
            ratio = min(tx, rx) / max(tx, rx)
            if ratio < RATE_RATIO_WARNING:
                slow = 'transmit (TX)' if tx < rx else 'receive (RX)'
                f.add(
                    'critical' if ratio < RATE_RATIO_CRITICAL else 'warning', 'rate_imbalance',
                    f'TX/RX rate imbalance on {src} → {tgt} ({tx:g} vs {rx:g} Mbps)',
                    [src, tgt],
                    f'{src} transmits to {tgt} at {tx:g} Mbps and receives at {rx:g} Mbps (ratio {ratio:.2f}). '
                    f'The {slow} direction is negotiating a much lower modulation rate, typically from '
                    'one-sided interference, a hidden node, or a power/antenna mismatch.',
                    'Compare SNR and noise in both directions; fix the weak direction first (see any SNR '
                    'imbalance finding for this pair).',
                    {'tx_rate': tx, 'rx_rate': rx, 'ratio': round(ratio, 2)}
                )
            elif max(tx, rx) < LOW_PHY_RATE_MBPS:
                f.add(
                    'warning', 'rate_imbalance',
                    f'Very low PHY rate on {src} ↔ {tgt} ({max(tx, rx):g} Mbps)',
                    [src, tgt],
                    f'Both directions are below {LOW_PHY_RATE_MBPS:g} Mbps, so the radios are using the most '
                    'robust (slowest) modulation. Airtime per packet is high, which hurts every node on the channel.',
                    'Improve the path (height, aim, gain) or reduce noise; consider a narrower channel '
                    'width only if SNR is the limit.',
                    {'tx_rate': tx, 'rx_rate': rx}
                )

        # ---- weak signal / low quality
        if snr is not None and snr < link_health.SNR_MARGINAL and not link.get('blocked'):
            f.add(
                'critical' if snr < 6 else 'warning', 'weak_signal',
                f'Weak signal on {src} ← {tgt} ({snr:.0f} dB SNR)',
                [src, tgt],
                f'{src} hears {tgt} at {snr:.0f} dB SNR, below the {link_health.SNR_MARGINAL} dB marginal '
                'level. Expect retransmissions, low rates and intermittent drops.',
                'Improve antenna height/aim/gain or find a better relay path. If a much louder neighbor '
                f'exists at {src}, check for a near-far problem.',
                {'snr': snr}
            )
        quality = link.get('quality')
        if quality is not None and quality < config.QUALITY_POOR and not link.get('blocked'):
            f.add(
                'warning', 'weak_signal',
                f'Low link quality on {src} → {tgt} ({quality}%)',
                [src, tgt],
                f'LQM quality is {quality}%, meaning many frames need retransmission.',
                'Look for interference, hidden nodes or a near-far neighbor at either end.',
                {'quality': quality}
            )


def _check_throughput(f, rf_stats, scope):
    for link in rf_stats:
        if link['source_node'] not in scope:
            continue
        tx = _num(link.get('throughput_tx'))
        rx = _num(link.get('throughput_rx'))
        if tx and rx and tx > 0 and rx > 0:
            ratio = min(tx, rx) / max(tx, rx)
            if ratio < THROUGHPUT_RATIO_WARNING:
                src, tgt = link['source_node'], link['target_node']
                f.add(
                    'warning', 'rate_imbalance',
                    f'Uni-directional throughput on {src} ↔ {tgt} ({tx:.1f} vs {rx:.1f} Mbps)',
                    [src, tgt],
                    f'The last iPerf test measured {tx:.1f} Mbps one way and {rx:.1f} Mbps the other '
                    f'(ratio {ratio:.2f}).',
                    'Fix the slower direction: check its SNR, noise and PHY rate, and whether a hidden '
                    'node is colliding with it.',
                    {'throughput_tx': tx, 'throughput_rx': rx, 'ratio': round(ratio, 2)}
                )


def _check_ping(f, ping_aggs, scope):
    for row in ping_aggs:
        src, tgt = row['source_node'], row['target_node']
        if src not in scope:
            continue
        loss = _num(row.get('loss_avg'))
        latency = _num(row.get('latency_avg'))
        jitter = _num(row.get('jitter_avg'))
        samples = row.get('samples') or 0
        evidence = {'samples': samples, 'loss_avg': loss, 'latency_avg': latency,
                    'jitter_avg': jitter, 'total_loss_samples': row.get('total_loss_samples')}

        problems = []
        severity = None
        if loss is not None and loss >= LOSS_WARNING:
            problems.append(f'{loss:.1f}% average packet loss')
            severity = 'critical' if loss >= LOSS_CRITICAL else 'warning'
        if latency is not None and latency >= LATENCY_WARNING:
            problems.append(f'{latency:.0f} ms average latency')
            severity = severity or 'warning'
        if jitter is not None and jitter >= JITTER_WARNING:
            problems.append(f'{jitter:.0f} ms jitter')
            severity = severity or 'warning'
        if problems:
            f.add(
                severity, 'packet_loss',
                f'{", ".join(problems).capitalize()} on {src} → {tgt}',
                [src, tgt],
                f'Over {samples} ping sample(s): ' + '; '.join(problems) + '. '
                + (f'{row["total_loss_samples"]} sample(s) lost every packet, which points at drops rather '
                   'than congestion. ' if row.get('total_loss_samples') else ''),
                'Correlate with flapping, SNR and channel-busy findings on this hop. Steady loss with good '
                'SNR suggests interference or collisions (hidden node); latency with low loss suggests congestion.',
                evidence
            )

        mos = link_health.compute_mos(latency, jitter, loss) if latency is not None and loss is not None else None
        if mos is not None and mos < MOS_WARNING:
            f.add(
                'critical' if mos < MOS_CRITICAL else 'warning', 'voip',
                f'Estimated VoIP MOS {mos:.2f} on {src} → {tgt}',
                [src, tgt],
                f'E-model estimate from ping data (latency {latency:.0f} ms, jitter {jitter or 0:.0f} ms, '
                f'loss {loss:.1f}%) is {link_health.mos_label(mos)}. Calls crossing this hop will sound choppy '
                'or drop.',
                'Resolve the loss/jitter on this hop, or route voice around it. Run a streamed VoIP session '
                'across it to confirm under a real call load.',
                dict(evidence, mos=mos)
            )


def _check_voip_history(f, tests, sessions, scope):
    for t in tests:
        src, tgt = t['source_node'], t['target_node']
        if src not in scope and tgt not in scope:
            continue
        mos = _num(t.get('mos'))
        if mos is not None and mos < MOS_WARNING:
            f.add(
                'critical' if mos < MOS_CRITICAL else 'warning', 'voip',
                f'VoIP call test {src} → {tgt} scored MOS {mos:.2f}',
                [src, tgt],
                f'Call test at {t.get("ran_at")} ({t.get("codec")}): latency {t.get("latency_ms")} ms, '
                f'jitter {t.get("jitter_ms")} ms, loss {t.get("loss_pct")}%.'
                + (f' Worst segment: {t["worst_segment"]}.' if t.get('worst_segment') else '')
                + (' Path MTU problem detected.' if t.get('mtu_warning') else ''),
                'Fix the worst segment first; if an MTU warning is present, lower the MTU on tunnel paths.',
                {k: t.get(k) for k in ('codec', 'mos', 'latency_ms', 'jitter_ms', 'loss_pct', 'worst_segment')}
            )
    for s in sessions:
        samples = s.get('samples') or 0
        if not samples:
            continue
        bad_pct = 100.0 * (s.get('bad') or 0) / samples
        warn_pct = 100.0 * (s.get('warn') or 0) / samples
        route_changes = s.get('route_changes') or 0
        if bad_pct < 5 and warn_pct < 10 and not route_changes:
            continue
        label = s.get('source') or 'collector'
        target = s.get('target') or s.get('remote_host') or '?'
        severity = 'critical' if bad_pct >= 5 else 'warning'
        f.add(
            severity, 'voip',
            f'Unstable streamed VoIP session #{s["id"]} ({label} → {target})',
            [label, target],
            f'{bad_pct:.0f}% of {samples} samples were BAD and {warn_pct:.0f}% WARN; '
            f'{route_changes} route change(s) during the call. Peak loss '
            f'{s.get("fwd_loss_max") or 0:.1f}% forward / {s.get("rev_loss_max") or 0:.1f}% reverse.',
            'Open the session report on the VOIP tab to see which hop or route change lined up with the '
            'bad samples. Route changes mid-call usually mean a flapping link along the path.',
            {k: s.get(k) for k in ('id', 'samples', 'bad', 'warn', 'route_changes', 'fwd_loss_max', 'rev_loss_max')}
        )


def _check_flaps(f, flaps, scope, hours):
    for row in flaps:
        src, tgt = row['source_node'], row['target_node']
        if src not in scope:
            continue
        downs = (row.get('node_reported_downs') or 0) + (row.get('blocks') or 0)
        if downs < FLAP_WARNING:
            continue
        reason = row.get('top_block_reason')
        hint = next((v for k, v in BLOCK_REASON_HINTS.items() if reason and k in reason.lower()), None)
        f.add(
            'critical' if downs >= FLAP_CRITICAL else 'warning', 'flapping',
            f'{src} → {tgt} flapped {downs} time(s) in {hours}h',
            [src, tgt],
            f'{row.get("node_reported_downs") or 0} node-reported drop(s) and {row.get("blocks") or 0} LQM '
            f'block(s); last change {row.get("last_change")}.'
            + (f' Most common block reason: {reason}.' if reason else ''),
            hint or 'Flapping usually follows SNR imbalance, a marginal signal, or a near-far neighbor; '
            'check those findings for this pair. Every flap forces route re-convergence across the mesh.',
            {k: row.get(k) for k in ('transitions', 'node_reported_downs', 'blocks', 'top_block_reason', 'last_change')}
        )


def _check_snr_swing(f, spreads, scope):
    for row in spreads:
        src, tgt = row['source_node'], row['target_node']
        if src not in scope or (row.get('samples') or 0) < 5:
            continue
        lo, hi = _num(row.get('snr_min')), _num(row.get('snr_max'))
        if lo is None or hi is None or hi - lo < SNR_SWING_WARNING:
            continue
        f.add(
            'warning', 'weak_signal',
            f'Unstable SNR on {src} ← {tgt} ({lo:.0f}-{hi:.0f} dB)',
            [src, tgt],
            f'SNR swung {hi - lo:.0f} dB over {row["samples"]} samples (avg {_num(row.get("snr_avg")) or 0:.0f} dB). '
            'Large swings come from fading/multipath, intermittent obstructions (vehicles, foliage, people at '
            'an event), a loose connector, or a radio that keeps changing power.',
            'Check mounts and connectors, raise antennas above crowds/vehicles, and avoid paths that graze '
            'obstacles.',
            {'snr_min': lo, 'snr_max': hi, 'samples': row['samples']}
        )


def _check_nodes(f, nodes_by_name, scope, node_noise, health, reboots, hours):
    for name in sorted(scope):
        node = nodes_by_name.get(name)
        if not node:
            continue

        noise = node_noise.get(name)
        if noise is not None and noise >= NOISE_WARNING:
            f.add(
                'critical' if noise >= NOISE_CRITICAL else 'warning', 'interference',
                f'Elevated noise floor at {name} ({noise:.0f} dBm)',
                [name],
                f'The noise floor on {name}\'s RF links is {noise:.0f} dBm (quiet is below '
                f'{link_health.NOISE_FLOOR_NORMAL} dBm). Every link into {name} loses that much margin.',
                'Look for co-located transmitters, other Wi-Fi/Part 15 gear on the same channel, or a '
                'very close mesh radio; try a different channel or add physical separation.',
                {'noise': noise}
            )

        busy = _num(node.get('channel_busy'))
        if busy is not None and busy >= CHANNEL_BUSY_WARNING:
            f.add(
                'critical' if busy >= CHANNEL_BUSY_CRITICAL else 'warning', 'congestion',
                f'Channel {busy:.0f}% busy at {name}',
                [name],
                f'{name} reports its channel busy {busy:.0f}% of the time. Above ~50% voice and '
                'interactive traffic see queueing delay and collisions.',
                'Reduce airtime: move bulk traffic (video, file transfer) off this channel, raise low PHY '
                'rates by improving weak links, or split the area onto another channel.',
                {'channel_busy': busy}
            )

        n_reboots = reboots.get(name, 0)
        if n_reboots:
            f.add(
                'critical' if n_reboots >= 3 else 'warning' if n_reboots == 2 else 'info', 'node_health',
                f'{name} rebooted {n_reboots} time(s) in {hours}h',
                [name],
                'Unexpected reboots drop every link through the node and force routing re-convergence.',
                'Check power (PoE injector, battery/voltage sag, cable length), heat, and the node\'s '
                'support log for watchdog or out-of-memory resets.',
                {'reboots': n_reboots}
            )

        mem_free, mem_total = _num(node.get('mem_free')), _num(node.get('mem_total'))
        if mem_free is not None and mem_total:
            pct = 100.0 * mem_free / mem_total
            if pct < MEM_FREE_WARNING_PCT:
                f.add(
                    'warning', 'node_health', f'Low free memory on {name} ({pct:.0f}%)', [name],
                    f'{name} has {pct:.0f}% memory free. Low memory leads to slow sysinfo, dropped services '
                    'and watchdog reboots.',
                    'Remove unused packages/services or reboot during a quiet period; consider newer hardware.',
                    {'mem_free': mem_free, 'mem_total': mem_total}
                )

        h = health.get(name)
        if h and (h.get('samples') or 0) >= 3:
            unreachable_pct = 100.0 * (h.get('unreachable') or 0) / h['samples']
            if unreachable_pct >= UNREACHABLE_WARNING_PCT:
                f.add(
                    'critical' if unreachable_pct >= 50 else 'warning', 'node_health',
                    f'{name} failed {unreachable_pct:.0f}% of polls',
                    [name],
                    f'{h.get("unreachable")} of {h["samples"]} scanner polls to {name} failed in the window. '
                    'This measures the path from this collector, not necessarily the node itself.',
                    'Check the hops between the collector and this node for flapping or loss; run the node '
                    'troubleshooter to see if neighbors can still reach it.',
                    {k: h.get(k) for k in ('samples', 'unreachable', 'degraded')}
                )
            if (h.get('degraded') or 0) >= DEGRADED_WARNING:
                f.add(
                    'warning', 'node_health', f'{name} was degraded {h["degraded"]} time(s)', [name],
                    f'Full sysinfo timed out {h["degraded"]} time(s) while bare sysinfo answered - the node is up '
                    'but overloaded or the path to it is lossy.',
                    'Check node load/memory and loss on the path to it.',
                    {'degraded': h.get('degraded'), 'load1_max': h.get('load1_max')}
                )
            load = _num(h.get('load1_max'))
            if load is not None and load >= LOAD_WARNING:
                f.add(
                    'info', 'node_health', f'High CPU load on {name} (peak {load:.1f})', [name],
                    f'1-minute load peaked at {load:.1f}. Busy nodes answer slowly and can drop routing packets.',
                    'Check for heavy services, many tunnels, or a broadcast storm on the LAN side.',
                    {'load1_max': load}
                )


def _check_hidden_nodes(f, rf_neighbors, scope, polled_scope, degraded_nodes):
    for hub in sorted(scope):
        neighbors = sorted(n for n in rf_neighbors.get(hub, ()) if n in polled_scope)
        if len(neighbors) < 2:
            continue
        hidden = []
        for i, a in enumerate(neighbors):
            for b in neighbors[i + 1:]:
                if b not in rf_neighbors.get(a, ()) and a not in rf_neighbors.get(b, ()):
                    hidden.append((a, b))
        if not hidden:
            continue
        degraded = hub in degraded_nodes
        if not degraded:
            # Hidden pairs are normal around a hilltop relay; only call them out
            # when the hub's links are actually suffering.
            continue
        pairs = '; '.join(f'{a} / {b}' for a, b in hidden[:5])
        f.add(
            'warning', 'hidden_node',
            f'{len(hidden)} hidden-node pair(s) around {hub}',
            [hub] + sorted({n for p in hidden for n in p}),
            f'{hub} hears these neighbor pairs, but they cannot hear each other: {pairs}. They transmit over '
            f'each other at {hub}, causing collisions, and {hub} is already showing loss/low quality.',
            f'Give the hidden stations a path to each other (raise or re-site one), reduce how many '
            f'stations share {hub}\'s channel, or move a group to a different channel.',
            {'hub': hub, 'pairs': hidden}
        )


def _articulation_impact(seed, adjacency, scope, polled):
    """For each node, how many in-scope nodes become unreachable from the seed
    if it fails. Brute force BFS is fine at mesh sizes."""
    if seed is None:
        return {}

    def reachable(skip):
        seen = {seed}
        q = deque([seed])
        while q:
            n = q.popleft()
            for p in adjacency.get(n, ()):
                if p != skip and p not in seen and p in scope:
                    seen.add(p)
                    q.append(p)
        return seen

    baseline = reachable(None)
    impact = {}
    for node in baseline:
        if node == seed or len(adjacency.get(node, ())) < 2:
            continue
        # Only count polled nodes; non-routable LQM-only endpoints would
        # otherwise make nearly every node look like a single point of failure.
        lost = (baseline - reachable(node) - {node}) & polled
        if lost:
            impact[node] = sorted(lost)
    return impact


def _check_topology(f, impact, nodes_by_name, scope, rf_neighbors):
    for node, lost in sorted(impact.items(), key=lambda kv: -len(kv[1])):
        f.add(
            'warning' if len(lost) >= SPOF_WARNING_NODES else 'info', 'topology',
            f'{node} is a single point of failure for {len(lost)} node(s)',
            [node] + lost[:10],
            f'If {node} goes down, {len(lost)} node(s) lose their only path to the rest of the mesh: '
            + ', '.join(lost[:8]) + ('...' if len(lost) > 8 else '') + '.',
            f'Add a redundant RF or DtD path that bypasses {node}, and give {node} reliable power. Protect '
            'it from near-far interference since everything behind it depends on it.',
            {'cut_off': lost}
        )
    for name in sorted(scope):
        node = nodes_by_name.get(name)
        if not node or not node.get('is_active'):
            continue
        has_rf = bool(node.get('rf_channel') or node.get('rf_frequency'))
        if has_rf and not rf_neighbors.get(name):
            f.add(
                'info', 'topology', f'{name} has RF enabled but no RF neighbors', [name],
                f'{name} is on {node.get("rf_frequency") or ""} channel {node.get("rf_channel") or "?"} '
                'and reachable only by tunnel/DtD.',
                'If it should be part of the RF mesh, check that its channel, width and SSID match its '
                'intended neighbors and that its antenna has a path to them.',
                {'rf_channel': node.get('rf_channel'), 'rf_frequency': node.get('rf_frequency')}
            )


# ============ Entry point ============

def run_health_check(hours=24):
    """Diagnose the in-scope mesh and return a findings report."""
    hours = max(1, min(int(hours or 24), 24 * 30))
    nodes = database.get_all_nodes()
    nodes_by_name = {n['name']: n for n in nodes}
    boundaries = database.get_scan_boundaries()
    all_links = database.get_active_links()
    current_links = [l for l in all_links if l.get('status') == 'good']

    seed, scope = compute_scope(nodes, current_links, boundaries)
    polled_scope = {n for n in scope if (nodes_by_name.get(n) or {}).get('is_active')}

    rf_links = [l for l in all_links if _is_rf(l)]
    rf_index = {(l['source_node'], l['target_node']): l for l in rf_links}
    rf_by_source = defaultdict(list)
    rf_neighbors = defaultdict(set)
    node_noise = {}
    for l in rf_links:
        rf_by_source[l['source_node']].append(l)
        if l.get('status') == 'good':
            rf_neighbors[l['source_node']].add(l['target_node'])
            rf_neighbors[l['target_node']].add(l['source_node'])
            noise = _num(l.get('noise'))
            if noise is not None:
                node_noise[l['source_node']] = max(noise, node_noise.get(l['source_node'], noise))

    adjacency = defaultdict(set)
    for l in current_links:
        if l['source_node'] in scope and l['target_node'] in scope:
            adjacency[l['source_node']].add(l['target_node'])
            adjacency[l['target_node']].add(l['source_node'])
    impact = _articulation_impact(seed, adjacency, scope, polled_scope)
    key_nodes = set(impact)

    ping_aggs = database.get_link_ping_aggregates(hours)
    health = database.get_node_health_aggregates(hours)

    # A hub "suffers" when any of its RF links is lossy or low quality.
    degraded_nodes = set()
    for row in ping_aggs:
        if (_num(row.get('loss_avg')) or 0) >= LOSS_WARNING:
            degraded_nodes.update((row['source_node'], row['target_node']))
    for l in rf_links:
        if l.get('status') == 'good' and _low_quality(l, 70):
            degraded_nodes.add(l['source_node'])

    f = _Findings()
    _check_near_far(f, rf_by_source, key_nodes, scope)
    _check_link_pairs(f, rf_index, node_noise, scope, polled_scope)
    _check_throughput(f, database.get_rf_links_with_latest_stats(), scope)
    _check_ping(f, ping_aggs, scope)
    _check_voip_history(f, database.get_recent_voip_tests(hours),
                        database.get_recent_voip_session_quality(hours), scope)
    _check_flaps(f, database.get_link_flap_report(hours=hours, min_transitions=FLAP_WARNING), scope, hours)
    _check_snr_swing(f, database.get_link_snr_spread(hours), scope)
    _check_nodes(f, nodes_by_name, scope, node_noise, health,
                 database.get_event_counts(database.EVENT_NODE_REBOOT, hours), hours)
    _check_hidden_nodes(f, rf_neighbors, scope, polled_scope, degraded_nodes)
    _check_topology(f, impact, nodes_by_name, scope, rf_neighbors)

    findings = sorted(f.items, key=lambda x: (SEVERITY_ORDER.get(x['severity'], 9), x['category'], x['subject']))
    by_severity = defaultdict(int)
    by_category = defaultdict(int)
    for item in findings:
        by_severity[item['severity']] += 1
        by_category[item['category']] += 1

    out_of_scope = sorted(n['name'] for n in nodes if n.get('is_active') and n['name'] not in scope)
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'hours': hours,
        'scope': {
            'seed': seed,
            'nodes_in_scope': len(polled_scope),
            'endpoints_in_scope': len(scope),
            'boundaries': boundaries,
            'out_of_scope_active': out_of_scope,
        },
        'key_nodes': sorted(key_nodes, key=lambda n: -len(impact[n])),
        'summary': {
            'total': len(findings),
            'by_severity': dict(by_severity),
            'by_category': {k: {'count': v, 'label': CATEGORY_LABELS.get(k, k)} for k, v in by_category.items()},
        },
        'categories': CATEGORY_LABELS,
        'findings': findings,
    }

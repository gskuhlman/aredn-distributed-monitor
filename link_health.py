"""
Link Health Analysis Module for AREDN Network Monitor

Computes derived health metrics per link from collected data:
- VoIP MOS score (ITU-T E-model)
- RF stability (quality/SNR variance)
- Link flapping score
- Interference indicators (noise floor)
- SNR health classification
- TX/RX rate asymmetry
- Overall link health grade
"""

import math
import logging
from datetime import datetime, timedelta
import database

logger = logging.getLogger(__name__)

# ============ Thresholds ============

SNR_GOOD = 20       # dB - solid RF link
SNR_MARGINAL = 12   # dB - usable but degraded
# Below SNR_MARGINAL is poor

NOISE_FLOOR_NORMAL = -95   # dBm - typical quiet environment
NOISE_FLOOR_ELEVATED = -88 # dBm - noticeable interference

QUALITY_GOOD = 85   # % - healthy link
QUALITY_POOR = 50   # % - degraded

JITTER_GOOD = 5.0    # ms
JITTER_MARGINAL = 20.0  # ms

LATENCY_GOOD = 30.0    # ms
LATENCY_MARGINAL = 100.0  # ms

LOSS_GOOD = 1.0       # %
LOSS_MARGINAL = 5.0   # %

# One-way delay budget (ITU-T G.114) for interactive voice.
G114_DELAY_GOOD = 150.0    # ms one-way - users don't notice
G114_DELAY_MAX = 400.0     # ms one-way - upper bound for acceptable calls

# Codec models: on-the-wire bitrate (incl. IP/UDP/RTP overhead @ ~20ms ptime) and
# E-model equipment impairment (Ie) + packet-loss robustness (Bpl) from ITU-T G.113.
# 'mixed' is deliberately conservative: G.711 bitrate for capacity (worst case) and
# G.729 Ie/Bpl for MOS (least loss-tolerant) so we never over-promise.
CODECS = {
    'g711':  {'label': 'G.711',  'bitrate_kbps': 87, 'ie': 0,  'bpl': 4.3},
    'g729':  {'label': 'G.729',  'bitrate_kbps': 31, 'ie': 11, 'bpl': 19.0},
    'opus':  {'label': 'Opus',   'bitrate_kbps': 45, 'ie': 1,  'bpl': 30.0},
    'mixed': {'label': 'Mixed',  'bitrate_kbps': 87, 'ie': 11, 'bpl': 19.0},
}

FLAP_WINDOW_HOURS = 24
FLAP_THRESHOLD_WARNING = 3   # state changes in window
FLAP_THRESHOLD_CRITICAL = 8

RATE_ASYMMETRY_THRESHOLD = 0.5  # ratio - flag if min/max rate < 0.5


# ============ Rating helpers ============

def _rate(value, good_threshold, marginal_threshold, lower_is_better=True):
    """Return 'good', 'marginal', or 'poor' for a numeric value."""
    if value is None:
        return 'unknown'
    if lower_is_better:
        if value <= good_threshold:
            return 'good'
        if value <= marginal_threshold:
            return 'marginal'
        return 'poor'
    else:
        if value >= good_threshold:
            return 'good'
        if value >= marginal_threshold:
            return 'marginal'
        return 'poor'


def _safe_float(value, default=None):
    """Convert to float or return default."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ============ VoIP MOS Score ============

def codec_params(name):
    """Return the codec model dict, resolving unknown names to 'mixed'."""
    return CODECS.get((name or 'mixed').lower(), CODECS['mixed'])


def compute_mos(latency_ms, jitter_ms, loss_pct, codec=None):
    """
    Estimate Mean Opinion Score using a simplified ITU-T E-model (G.107).

    ``codec=None`` keeps the original loss model (back-compatible for existing
    callers). When a codec name is given, the packet-loss term uses the E-model
    equipment-impairment form with that codec's Ie/Bpl, so a loss-tolerant codec
    (Opus) scores higher than a sensitive one (G.729) at the same loss.

    Returns a score from 1.0 (unusable) to 4.5 (excellent).
    """
    if latency_ms is None or loss_pct is None:
        return None

    jitter = jitter_ms if jitter_ms is not None else 0.0

    # Effective latency includes jitter buffer estimate
    effective_latency = latency_ms + (jitter * 2) + 10  # 10ms codec delay

    # R-factor calculation (simplified E-model)
    r = 93.2 - (effective_latency / 40.0)

    if codec is None:
        # Original flat loss model.
        if loss_pct > 0:
            r -= 2.5 * loss_pct
            r -= 0.1 * (loss_pct ** 1.5)
    else:
        # Codec-aware effective equipment impairment (ITU-T G.107):
        #   Ie_eff = Ie + (95 - Ie) * Ppl / (Ppl/BurstR + Bpl)   (BurstR=1, random loss)
        params = codec_params(codec)
        ie, bpl = params['ie'], params['bpl']
        r -= ie
        if loss_pct > 0:
            r -= (95 - ie) * loss_pct / (loss_pct + bpl)

    # Jitter penalty beyond what's covered by effective latency
    if jitter > 10:
        r -= (jitter - 10) * 0.2

    # Clamp R-factor to valid range
    r = max(0, min(93.2, r))

    # Convert R-factor to MOS (ITU-T G.107 Annex B)
    if r < 0:
        mos = 1.0
    elif r > 100:
        mos = 4.5
    else:
        mos = 1.0 + 0.035 * r + r * (r - 60) * (100 - r) * 7e-6

    return round(max(1.0, min(4.5, mos)), 2)


def g114_delay_rating(one_way_ms):
    """Rate one-way mouth-to-ear delay against the ITU-T G.114 budget."""
    if one_way_ms is None:
        return {'rating': 'unknown', 'one_way_ms': None,
                'budget_ms': G114_DELAY_GOOD, 'max_ms': G114_DELAY_MAX, 'label': 'No data'}
    if one_way_ms <= G114_DELAY_GOOD:
        rating, label = 'good', 'Within budget'
    elif one_way_ms <= G114_DELAY_MAX:
        rating, label = 'marginal', 'Noticeable delay'
    else:
        rating, label = 'poor', 'Exceeds acceptable delay'
    return {'rating': rating, 'one_way_ms': round(one_way_ms, 1),
            'budget_ms': G114_DELAY_GOOD, 'max_ms': G114_DELAY_MAX, 'label': label}


def mos_rating(mos):
    """Classify a MOS score."""
    if mos is None:
        return 'unknown'
    if mos >= 4.0:
        return 'good'       # Satisfied users
    if mos >= 3.5:
        return 'marginal'   # Some users dissatisfied
    return 'poor'            # Many users dissatisfied


def mos_label(mos):
    """Human-readable label for a MOS score."""
    if mos is None:
        return 'No data'
    if mos >= 4.3:
        return 'Excellent'
    if mos >= 4.0:
        return 'Good'
    if mos >= 3.6:
        return 'Fair'
    if mos >= 3.1:
        return 'Poor'
    return 'Bad'


# ============ RF Stability ============

def compute_rf_stability(quality_history):
    """
    Compute RF stability metrics from quality/SNR history.

    Returns dict with stddev, trend, and dip counts.
    """
    quality_values = [
        row['quality'] for row in quality_history
        if row.get('quality') is not None
    ]
    snr_values = [
        row['snr'] for row in quality_history
        if row.get('snr') is not None
    ]

    result = {
        'quality_stddev': None,
        'quality_trend': None,
        'quality_min': None,
        'quality_avg': None,
        'quality_dips_below_50': 0,
        'quality_dips_below_85': 0,
        'snr_stddev': None,
        'snr_trend': None,
        'snr_min': None,
        'snr_avg': None,
        'sample_count': len(quality_values),
        'rating': 'unknown'
    }

    if len(quality_values) >= 2:
        avg = sum(quality_values) / len(quality_values)
        variance = sum((v - avg) ** 2 for v in quality_values) / len(quality_values)
        result['quality_stddev'] = round(math.sqrt(variance), 2)
        result['quality_avg'] = round(avg, 1)
        result['quality_min'] = min(quality_values)
        result['quality_dips_below_50'] = sum(1 for v in quality_values if v < 50)
        result['quality_dips_below_85'] = sum(1 for v in quality_values if v < 85)

        # Simple trend: compare first half avg to second half avg
        mid = len(quality_values) // 2
        first_avg = sum(quality_values[:mid]) / mid if mid > 0 else avg
        second_avg = sum(quality_values[mid:]) / (len(quality_values) - mid)
        diff = second_avg - first_avg
        if diff > 5:
            result['quality_trend'] = 'improving'
        elif diff < -5:
            result['quality_trend'] = 'degrading'
        else:
            result['quality_trend'] = 'stable'

    if len(snr_values) >= 2:
        avg = sum(snr_values) / len(snr_values)
        variance = sum((v - avg) ** 2 for v in snr_values) / len(snr_values)
        result['snr_stddev'] = round(math.sqrt(variance), 2)
        result['snr_avg'] = round(avg, 1)
        result['snr_min'] = min(snr_values)

        mid = len(snr_values) // 2
        first_avg = sum(snr_values[:mid]) / mid if mid > 0 else avg
        second_avg = sum(snr_values[mid:]) / (len(snr_values) - mid)
        diff = second_avg - first_avg
        if diff > 2:
            result['snr_trend'] = 'improving'
        elif diff < -2:
            result['snr_trend'] = 'degrading'
        else:
            result['snr_trend'] = 'stable'

    # Rate stability
    q_std = result['quality_stddev']
    if q_std is not None:
        if q_std <= 3 and result['quality_dips_below_50'] == 0:
            result['rating'] = 'good'
        elif q_std <= 10 and result['quality_dips_below_50'] <= 2:
            result['rating'] = 'marginal'
        else:
            result['rating'] = 'poor'

    return result


# ============ Link Flapping ============

# Seconds within which a link drop and a node offline event are considered
# to be caused by the same underlying outage.
_COINCIDENCE_WINDOW = 120


def compute_flap_score(node_name, link_peer, link_type='',
                       hours=FLAP_WINDOW_HOURS):
    """
    Count and classify link state transitions in a time window.

    Distinguishes three cases:
      - node_flaps:  link drops that coincide with the source or peer node
                     going offline (node reboot / unreachable).
      - link_flaps:  link drops with no corresponding node outage — the link
                     itself is unstable.
      - total_flaps: sum of both.

    For DTD (wired) links, all drops are attributed to the node because a
    physical ethernet cable does not independently flap.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        events = database.get_node_observed_events(node_name, limit=2000)
    except Exception:
        return _flap_result(0, 0, hours, link_type)

    # Also grab events for the peer node so we can detect peer-side outages.
    try:
        peer_events = database.get_node_observed_events(link_peer, limit=2000)
    except Exception:
        peer_events = []

    # Collect timestamps of node_offline / node_online events for both sides.
    node_offline_times = set()
    for ev in events:
        ts = ev.get('timestamp', '')
        if ts < cutoff:
            continue
        if ev.get('event_type') in ('node_offline', 'node_online'):
            node_name_in_ev = ev.get('node_name', '')
            if node_name_in_ev in (node_name, link_peer):
                node_offline_times.add(ts)

    for ev in peer_events:
        ts = ev.get('timestamp', '')
        if ts < cutoff:
            continue
        if ev.get('event_type') in ('node_offline', 'node_online'):
            node_offline_times.add(ts)

    # Collect link drop/restore events for this specific peer.
    link_event_times = []
    for ev in events:
        ts = ev.get('timestamp', '')
        if ts < cutoff:
            continue
        etype = ev.get('event_type', '')
        details = ev.get('details', '')
        if etype in ('link_dropped', 'link_restored') and link_peer in details:
            link_event_times.append(ts)

    is_wired = link_type.upper() in ('DTD', 'XLINK')
    total = len(link_event_times)

    if is_wired:
        # Wired links don't independently flap — attribute everything to node.
        node_flaps = total
        link_flaps = 0
    else:
        # Classify each link event: does it fall near a node outage event?
        node_flaps = 0
        link_flaps = 0
        for lt in link_event_times:
            if _near_any(lt, node_offline_times, _COINCIDENCE_WINDOW):
                node_flaps += 1
            else:
                link_flaps += 1

    return _flap_result(node_flaps, link_flaps, hours, link_type)


def _near_any(timestamp_str, timestamp_set, window_seconds):
    """Return True if timestamp_str is within window_seconds of any entry."""
    try:
        ts = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return False
    for other_str in timestamp_set:
        try:
            other = datetime.strptime(other_str, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            continue
        if abs((ts - other).total_seconds()) <= window_seconds:
            return True
    return False


def _flap_result(node_flaps, link_flaps, hours, link_type):
    """Build the flapping result dict with a human-readable cause label."""
    total = node_flaps + link_flaps

    # Rating is based on total state changes regardless of cause
    if total >= FLAP_THRESHOLD_CRITICAL:
        rating = 'poor'
    elif total >= FLAP_THRESHOLD_WARNING:
        rating = 'marginal'
    else:
        rating = 'good'

    # Cause: what to show the operator
    is_wired = link_type.upper() in ('DTD', 'XLINK')
    if total == 0:
        cause = 'none'
        cause_label = 'Stable'
    elif is_wired:
        cause = 'node'
        cause_label = 'Node outage'
    elif link_flaps == 0:
        cause = 'node'
        cause_label = 'Node outage'
    elif node_flaps == 0:
        cause = 'link'
        cause_label = 'RF instability'
    else:
        cause = 'mixed'
        cause_label = f'{link_flaps} link / {node_flaps} node'

    return {
        'flap_count': total,
        'node_flaps': node_flaps,
        'link_flaps': link_flaps,
        'cause': cause,
        'cause_label': cause_label,
        'window_hours': hours,
        'rating': rating
    }


# ============ Interference / Noise ============

def analyze_noise(link, quality_history=None):
    """
    Analyze noise floor for interference indicators.

    Uses current link noise and optionally historical trend.
    """
    noise_val = _safe_float(link.get('noise'))
    signal_val = _safe_float(link.get('signal'))

    result = {
        'noise_floor': noise_val,
        'signal_level': signal_val,
        'noise_rating': 'unknown',
        'noise_trend': None,
        'interference_likely': False,
        'details': None
    }

    if noise_val is not None:
        if noise_val >= NOISE_FLOOR_ELEVATED:
            result['noise_rating'] = 'poor'
            result['interference_likely'] = True
            result['details'] = f'Noise floor {noise_val} dBm is elevated (threshold: {NOISE_FLOOR_ELEVATED} dBm)'
        elif noise_val >= NOISE_FLOOR_NORMAL:
            result['noise_rating'] = 'marginal'
            result['details'] = f'Noise floor {noise_val} dBm is above typical quiet level'
        else:
            result['noise_rating'] = 'good'
            result['details'] = f'Noise floor {noise_val} dBm is normal'

    return result


# ============ SNR Health ============

def classify_snr(link):
    """Classify current SNR health."""
    snr = _safe_float(link.get('snr'))
    if snr is None:
        return {'snr': None, 'rating': 'unknown', 'details': 'No SNR data'}
    if snr >= SNR_GOOD:
        return {'snr': snr, 'rating': 'good', 'details': f'SNR {snr} dB is strong'}
    if snr >= SNR_MARGINAL:
        return {'snr': snr, 'rating': 'marginal', 'details': f'SNR {snr} dB is marginal (below {SNR_GOOD} dB)'}
    return {'snr': snr, 'rating': 'poor', 'details': f'SNR {snr} dB is poor (below {SNR_MARGINAL} dB)'}


# ============ LQM Block State ============

def analyze_block(link):
    """Surface LQM's own block decision for this link.

    A blocked link is the most direct flap explanation AREDN gives us, so it is
    rated 'poor' whenever set, with the reason carried through for display.
    """
    blocked = link.get('blocked')
    reason = link.get('blocked_reason')
    if blocked is None:
        return {'blocked': None, 'reason': None, 'rating': 'unknown'}
    if blocked:
        return {'blocked': True, 'reason': reason or 'unspecified', 'rating': 'poor'}
    return {'blocked': False, 'reason': None, 'rating': 'good'}


# ============ SNR Asymmetry (rev_snr) ============

SNR_ASYMMETRY_MARGINAL = 3.0   # dB gap worth noting
SNR_ASYMMETRY_POOR = 6.0       # dB gap that commonly drives flapping


def analyze_snr_asymmetry(link):
    """Compare our RX SNR against the neighbor's RX SNR (rev_snr).

    "We hear them fine but they barely hear us" is a classic flapping cause and
    is invisible in a single SNR number.
    """
    snr = _safe_float(link.get('snr'))
    rev_snr = _safe_float(link.get('rev_snr'))
    result = {'snr': snr, 'rev_snr': rev_snr, 'delta': None, 'rating': 'unknown', 'details': None}
    if snr is None or rev_snr is None:
        return result
    delta = round(abs(snr - rev_snr), 1)
    result['delta'] = delta
    if delta >= SNR_ASYMMETRY_POOR:
        result['rating'] = 'poor'
        result['details'] = f'SNR {snr} dB vs neighbor {rev_snr} dB ({delta} dB gap)'
    elif delta >= SNR_ASYMMETRY_MARGINAL:
        result['rating'] = 'marginal'
        result['details'] = f'{delta} dB SNR gap between directions'
    else:
        result['rating'] = 'good'
        result['details'] = 'Balanced SNR in both directions'
    return result


# ============ TX/RX Rate Asymmetry ============

def analyze_rate_asymmetry(link):
    """Check for significant TX/RX rate asymmetry."""
    tx = _safe_float(link.get('tx_rate'))
    rx = _safe_float(link.get('rx_rate'))

    result = {
        'tx_rate': tx,
        'rx_rate': rx,
        'ratio': None,
        'rating': 'unknown',
        'details': None
    }

    if tx is not None and rx is not None and tx > 0 and rx > 0:
        ratio = min(tx, rx) / max(tx, rx)
        result['ratio'] = round(ratio, 2)

        if ratio >= 0.8:
            result['rating'] = 'good'
            result['details'] = 'TX/RX rates are balanced'
        elif ratio >= RATE_ASYMMETRY_THRESHOLD:
            result['rating'] = 'marginal'
            result['details'] = f'Moderate TX/RX asymmetry (ratio {ratio:.2f})'
        else:
            result['rating'] = 'poor'
            result['details'] = f'Significant TX/RX asymmetry (ratio {ratio:.2f}) - possible hidden node or interference'

    return result


# ============ Latency/Jitter/Loss ratings ============

def analyze_ping_metrics(ping_history):
    """Compute aggregate ping metrics from recent history."""
    avg_values = [row['ping_avg'] for row in ping_history if row.get('ping_avg') is not None]
    loss_values = [row['ping_loss'] for row in ping_history if row.get('ping_loss') is not None]
    jitter_values = [row['jitter'] for row in ping_history if row.get('jitter') is not None]

    result = {
        'latency_avg': None,
        'latency_rating': 'unknown',
        'loss_avg': None,
        'loss_rating': 'unknown',
        'jitter_avg': None,
        'jitter_rating': 'unknown',
        'sample_count': len(avg_values)
    }

    if avg_values:
        avg = sum(avg_values) / len(avg_values)
        result['latency_avg'] = round(avg, 2)
        result['latency_rating'] = _rate(avg, LATENCY_GOOD, LATENCY_MARGINAL, lower_is_better=True)

    if loss_values:
        avg = sum(loss_values) / len(loss_values)
        result['loss_avg'] = round(avg, 2)
        result['loss_rating'] = _rate(avg, LOSS_GOOD, LOSS_MARGINAL, lower_is_better=True)

    if jitter_values:
        avg = sum(jitter_values) / len(jitter_values)
        result['jitter_avg'] = round(avg, 2)
        result['jitter_rating'] = _rate(avg, JITTER_GOOD, JITTER_MARGINAL, lower_is_better=True)

    return result


# ============ Overall Grade ============

_GRADE_WEIGHTS = {
    'good': 3,
    'marginal': 2,
    'poor': 1,
    'unknown': 0
}

_METRIC_WEIGHTS = {
    'latency': 3,
    'loss': 4,
    'jitter': 2,
    'snr': 3,
    'noise': 2,
    'stability': 3,
    'flapping': 4,
    'asymmetry': 1,
    'lqm_block': 4,
    'snr_asymmetry': 3,
}


def compute_overall_grade(metrics):
    """
    Compute a weighted overall grade from individual metric ratings.

    Returns a dict with letter grade (A-F), numeric score, and rating.
    """
    weighted_sum = 0
    weight_total = 0

    rating_map = {
        'latency': metrics.get('ping', {}).get('latency_rating'),
        'loss': metrics.get('ping', {}).get('loss_rating'),
        'jitter': metrics.get('ping', {}).get('jitter_rating'),
        'snr': metrics.get('snr', {}).get('rating'),
        'noise': metrics.get('noise', {}).get('noise_rating'),
        'stability': metrics.get('stability', {}).get('rating'),
        'flapping': metrics.get('flapping', {}).get('rating'),
        'asymmetry': metrics.get('rate_asymmetry', {}).get('rating'),
        'lqm_block': metrics.get('lqm_block', {}).get('rating'),
        'snr_asymmetry': metrics.get('snr_asymmetry', {}).get('rating'),
    }

    for metric_name, rating in rating_map.items():
        if rating and rating != 'unknown':
            weight = _METRIC_WEIGHTS.get(metric_name, 1)
            weighted_sum += _GRADE_WEIGHTS[rating] * weight
            weight_total += weight

    if weight_total == 0:
        return {'grade': '?', 'score': None, 'rating': 'unknown'}

    score = weighted_sum / weight_total  # 1.0 to 3.0

    if score >= 2.7:
        grade, rating = 'A', 'good'
    elif score >= 2.3:
        grade, rating = 'B', 'good'
    elif score >= 1.8:
        grade, rating = 'C', 'marginal'
    elif score >= 1.3:
        grade, rating = 'D', 'poor'
    else:
        grade, rating = 'F', 'poor'

    return {
        'grade': grade,
        'score': round(score, 2),
        'rating': rating
    }


# ============ Main entry point ============

def analyze_link(node_name, link, quality_history, ping_history):
    """
    Run full health analysis for one link.

    Args:
        node_name: the node whose page we're analyzing from
        link: current link dict from database
        quality_history: list of link_history rows for quality/SNR
        ping_history: list of link_history rows with ping data

    Returns:
        dict with all health metrics and overall grade
    """
    peer = link['target_node'] if link['source_node'] == node_name else link['source_node']

    # Filter history to this specific link
    link_quality = [
        row for row in quality_history
        if (row['source_node'] == link['source_node'] and row['target_node'] == link['target_node'])
        or (row['source_node'] == link['target_node'] and row['target_node'] == link['source_node'])
    ]
    link_ping = [
        row for row in ping_history
        if (row['source_node'] == link['source_node'] and row['target_node'] == link['target_node'])
        or (row['source_node'] == link['target_node'] and row['target_node'] == link['source_node'])
    ]

    # Compute individual metrics
    link_type = link.get('link_type', '')
    ping_metrics = analyze_ping_metrics(link_ping)
    snr_health = classify_snr(link)
    noise_analysis = analyze_noise(link, link_quality)
    stability = compute_rf_stability(link_quality)
    flapping = compute_flap_score(node_name, peer, link_type=link_type)
    asymmetry = analyze_rate_asymmetry(link)
    block_state = analyze_block(link)
    snr_asymmetry = analyze_snr_asymmetry(link)

    # Attach the dominant LQM block reason from the structured state log so the
    # flap column can show *why* a link is flapping, not just how often.
    try:
        flap_summary = database.get_pair_flap_summary(node_name, peer)
        flapping['top_block_reason'] = flap_summary.get('top_block_reason')
        # logged_downs reflects node-reported peer flaps only; scanner-to-node
        # losses are surfaced separately so they are not read as RF instability.
        flapping['logged_downs'] = flap_summary.get('node_reported_downs')
        flapping['scanner_unreachable'] = flap_summary.get('scanner_unreachable')
        flapping['inferred_downs'] = flap_summary.get('inferred_downs')
    except Exception:
        flapping['top_block_reason'] = None

    # Compute MOS from latest ping averages
    mos = compute_mos(
        ping_metrics['latency_avg'],
        ping_metrics['jitter_avg'],
        ping_metrics['loss_avg']
    )

    metrics = {
        'peer': peer,
        'link_type': link.get('link_type', ''),
        'ping': ping_metrics,
        'mos': {
            'score': mos,
            'rating': mos_rating(mos),
            'label': mos_label(mos)
        },
        'snr': snr_health,
        'noise': noise_analysis,
        'stability': stability,
        'flapping': flapping,
        'rate_asymmetry': asymmetry,
        'lqm_block': block_state,
        'snr_asymmetry': snr_asymmetry,
    }

    metrics['overall'] = compute_overall_grade(metrics)

    return metrics


def _deduplicate_links(node_name, links):
    """Merge directional link pairs into one entry per peer.

    Links are stored as source→target, so A→B and B→A are separate rows.
    For each peer, prefer the row where *node_name* is the source (the local
    LQM perspective) and fill gaps from the reverse direction.
    """
    by_peer = {}
    for link in links:
        if link.get('status') == 'removed':
            continue
        peer = link['target_node'] if link['source_node'] == node_name else link['source_node']
        is_local = link['source_node'] == node_name
        entry = by_peer.setdefault(peer, {'local': None, 'remote': None})
        if is_local:
            entry['local'] = link
        else:
            entry['remote'] = link

    merged = []
    for peer, pair in by_peer.items():
        primary = pair['local'] or pair['remote']
        secondary = pair['local'] and pair['remote'] and pair['remote']
        if not secondary:
            merged.append(primary)
            continue
        result = dict(primary)
        for key in ('snr', 'signal', 'noise', 'tx_rate', 'rx_rate', 'distance',
                     'mac_address', 'canonical_ip', 'identity_status',
                     'routability_status', 'lqm_status_message', 'rev_snr'):
            if result.get(key) is None:
                result[key] = secondary.get(key)
        # A link blocked in either direction is blocked.
        if secondary.get('blocked'):
            result['blocked'] = secondary.get('blocked')
            if result.get('blocked_reason') is None:
                result['blocked_reason'] = secondary.get('blocked_reason')
        merged.append(result)
    return merged


def analyze_node_links(node_name, hours=24):
    """
    Compute health analysis for all links of a node.

    Returns a list of health metric dicts, one per peer (deduplicated).
    """
    links = database.get_node_all_links(node_name)
    if not links:
        return []

    deduped = _deduplicate_links(node_name, links)
    quality_history = database.get_node_history(node_name, hours=hours)
    ping_history = database.get_node_ping_history(node_name, hours=hours)

    results = []
    for link in deduped:
        try:
            analysis = analyze_link(node_name, link, quality_history, ping_history)
            results.append(analysis)
        except Exception as exc:
            logger.warning("Health analysis failed for %s -> %s: %s",
                           node_name, link.get('target_node'), exc)

    return results

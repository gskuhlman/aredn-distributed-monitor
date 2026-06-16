"""
VOIP call-quality diagnostics for the mixed wireguard/RF/DTD mesh.

Composes existing per-link probes into END-TO-END call diagnostics:
- end-to-end MOS between two endpoints (ping A->B via A's CGI),
- per-segment attribution (which wireguard/RF/DTD hop is the bottleneck),
- concurrent-call capacity (iperf vs codec bitrate),
- path-MTU / fragmentation check (collector->node DF sweep).

All probes are live and run ON DEMAND from the VOIP tab, never on the scan path.
Mirrors troubleshoot.py: synchronous, resilient (one failing probe still returns
the rest), read/active over existing rf_stats + link_health primitives.
"""

import math
import logging
from concurrent.futures import ThreadPoolExecutor

import config
import database
import rf_stats
import link_health

logger = logging.getLogger(__name__)

_LINK_TYPE_BUCKET = {
    'RF': 'rf', 'DTD': 'wired', 'XLINK': 'wired',
    'WIREGUARD': 'wireguard', 'WG': 'wireguard',
    'TUN': 'wireguard', 'TUNNEL': 'wireguard', 'VTUN': 'wireguard',
}


def _is_ip(value):
    parts = str(value or '').split('.')
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _ping_ok(result):
    return bool(result) and (result.get('loss') if result.get('loss') is not None else 100) < 100


def _addr(token):
    """Resolve a token (node name OR a hand-entered IP/hostname) to an address.

    Known nodes resolve to their canonical IP; a literal IP / dotted host /
    *.local.mesh is used as-is so the user can target devices with no service entry.
    """
    if not token:
        return None
    ip = database.get_canonical_ip_for_node(token)
    if ip:
        return ip
    t = str(token).strip()
    if _is_ip(t) or t.endswith('.local.mesh') or '.' in t:
        return t
    return None


def list_endpoints():
    """VOIP endpoints (phones/PBX) with a reachability hint."""
    endpoints = database.get_voip_endpoints()
    for e in endpoints:
        e['reachable_hint'] = bool(e.get('is_active') == 1)
    return endpoints


def ping_all_endpoints(count=2, timeout=2, max_workers=8):
    """Ping every VOIP device IP from the collector (concurrently) for a quick
    reachability sweep. Returns one result per device.

    Uses eventlet's GreenPool when available: ping_node offloads the blocking
    ping subprocess via eventlet.tpool, and nesting tpool inside a
    concurrent.futures ThreadPoolExecutor under eventlet's monkey-patched
    threading deadlocks — GreenPool composes with tpool correctly.
    """
    endpoints = database.get_voip_endpoints()
    if not endpoints:
        return []

    codec = _resolve_codec(config.VOIP_CODEC)

    def probe(e):
        ip = e.get('device_ip')
        base = {'node': e['node'], 'device': e['device'], 'type': e['type'], 'ip': ip}
        if not ip:
            base.update({'reachable': False, 'avg': None, 'jitter': None,
                         'loss': None, 'mos': None, 'mos_rating': 'unknown'})
            return base
        try:
            res = rf_stats.ping_node(ip, count=count, timeout=timeout)
        except Exception as exc:
            logger.warning("VOIP ping of %s failed: %s", ip, exc)
            res = None
        ok = _ping_ok(res)
        avg = (res or {}).get('avg')
        jitter = (res or {}).get('jitter')
        loss = (res or {}).get('loss')
        mos = link_health.compute_mos(avg, jitter, loss, codec=codec) if ok else None
        base.update({'reachable': ok, 'avg': avg, 'jitter': jitter, 'loss': loss,
                     'mos': mos, 'mos_rating': link_health.mos_rating(mos)})
        return base

    workers = max(1, min(max_workers, len(endpoints)))
    try:
        import eventlet
        pool = eventlet.GreenPool(workers)
        return list(pool.imap(probe, endpoints))
    except ImportError:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(probe, endpoints))


def _resolve_codec(codec):
    name = (codec or config.VOIP_CODEC or 'mixed').lower()
    return name if name in link_health.CODECS else 'mixed'


def end_to_end_quality(a, b, codec):
    """True cumulative call quality A->B (ping B from A's node), codec-aware MOS."""
    a_ip = _addr(a)
    b_ip = _addr(b)
    target = b_ip or b
    origin = 'node'
    if a_ip:
        res = rf_stats.ping_via_aredn(target, source_node_ip=a_ip)
    elif b_ip:
        # No address for A: fall back to a collector-origin ping (NOT the true A->B path).
        origin = 'collector'
        res = rf_stats.ping_node(b_ip)
    else:
        return {'reachable': False, 'origin': origin, 'note': 'No address for source or target.'}

    if not _ping_ok(res):
        return {'reachable': False, 'origin': origin,
                'note': 'No response across the path (100% loss).'}

    avg = res.get('avg')
    jitter = res.get('jitter')
    loss = res.get('loss')
    mos = link_health.compute_mos(avg, jitter, loss, codec=codec)
    return {
        'reachable': True, 'origin': origin,
        'latency_ms': avg, 'jitter_ms': jitter, 'loss_pct': loss,
        'one_way_ms': round(avg / 2, 1) if avg is not None else None,
        'mos': mos, 'mos_label': link_health.mos_label(mos), 'mos_rating': link_health.mos_rating(mos),
    }


def segment_attribution(a, b):
    """Trace A->B and tag each hop segment wireguard/RF/DTD; pick the worst."""
    a_ip = _addr(a)
    b_ip = _addr(b)
    target = b_ip or b
    origin = 'node'
    if a_ip:
        tr = rf_stats.traceroute_via_aredn(target, source_node_ip=a_ip)
    elif b_ip:
        origin = 'collector'
        tr = rf_stats.traceroute_local(b_ip)
    else:
        return {'status': 'failed', 'note': 'No address to trace from/to.'}
    if not tr or not tr.get('hops'):
        return {'status': 'failed', 'origin': origin, 'note': 'Traceroute returned no hops.'}

    ip_map = database.get_ip_name_map()

    def hop_name(host, ip):
        if ip and ip in ip_map:
            return ip_map[ip]
        if host and not _is_ip(host):
            return host
        return ip or host

    # Prepend source node so the first real hop forms segment A -> hop1.
    points = [{'name': a, 'ip': a_ip, 'ms': 0.0}]
    for h in tr['hops']:
        points.append({'name': hop_name(h.get('host'), h.get('ip')),
                       'ip': h.get('ip'), 'ms': h.get('ms'), 'timeout': h.get('timeout')})

    segments = []
    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        link = database.get_link(p0['name'], p1['name']) or database.get_link(p1['name'], p0['name'])
        link_type = (link or {}).get('link_type')
        bucket = _LINK_TYPE_BUCKET.get((link_type or '').upper(), 'unknown')
        delta = None
        if p0.get('ms') is not None and p1.get('ms') is not None:
            delta = round(max(0.0, p1['ms'] - p0['ms']), 1)
        segments.append({
            'from': p0['name'], 'to': p1['name'],
            'link_type': link_type, 'bucket': bucket,
            'rtt_delta_ms': delta, 'timeout': bool(p1.get('timeout')),
            'label': f"{p0['name']} → {p1['name']}",
        })

    # Worst segment: timeouts/loss first (prefer the variable media), else slowest hop.
    bucket_rank = {'rf': 0, 'wireguard': 1, 'wired': 2, 'unknown': 3}

    def worst_key(s):
        return (0 if s['timeout'] else 1, bucket_rank.get(s['bucket'], 3),
                -(s['rtt_delta_ms'] or 0))
    timeouts = [s for s in segments if s['timeout']]
    if timeouts:
        worst = sorted(timeouts, key=worst_key)[0]
        worst['reason'] = 'no response past this hop'
    elif segments:
        worst = max(segments, key=lambda s: (s['rtt_delta_ms'] or 0,
                                             -bucket_rank.get(s['bucket'], 3)))
        worst['reason'] = 'highest added latency'
    else:
        worst = None

    return {'status': 'ok', 'origin': origin, 'hop_count': len(tr['hops']),
            'segments': segments, 'worst': worst}


def concurrent_capacity(a, b, codec):
    """Estimate simultaneous calls supported between A and B from an iperf run."""
    a_ip = _addr(a)
    b_ip = _addr(b)
    if not b_ip:
        return {'status': 'failed', 'note': 'No target IP for iperf.'}
    # AREDN's iperf3 (/cgi-bin/iperf) requires a NODE NAME as the server -- it
    # rejects bare IPs ("no such server"). Resolve the target to its AREDN node;
    # a phone/IP with no node can't host an iperf3 server, so skip cleanly.
    target_name = b if database.get_node(b) else database.get_node_name_by_ip(b_ip)
    if not target_name:
        return {'status': 'skipped',
                'note': f'Capacity needs iperf3 between AREDN nodes; "{b}" is a device/IP with no AREDN node to run an iperf3 server.'}
    res = rf_stats.run_iperf_test(b_ip, source_node_ip=a_ip,
                                  source_node_name=a, target_node_name=target_name)
    if not res or res.get('tx_mbps') is None:
        return {'status': 'failed',
                'note': f'iperf3 returned no result for {a} -> {target_name}.'}
    tx = res['tx_mbps']
    usable = tx * config.VOIP_CAPACITY_HEADROOM
    per_call_kbps = config.VOIP_CODEC_KBPS.get(codec, config.VOIP_CODEC_KBPS['mixed'])
    max_calls = int(math.floor(usable * 1000 / per_call_kbps)) if per_call_kbps else None
    return {'status': 'ok', 'capacity_mbps': round(tx, 2), 'usable_mbps': round(usable, 2),
            'per_call_kbps': per_call_kbps, 'codec': codec, 'max_calls': max_calls}


def path_mtu(b):
    """Collector->node DF-bit MTU sweep (CGI ping cannot set DF, so collector-origin only)."""
    import platform
    b_ip = _addr(b)
    if not b_ip:
        return {'status': 'failed', 'note': 'No target IP for the MTU check.'}
    system = platform.system().lower()
    for payload in config.VOIP_MTU_PROBE_SIZES:
        if system == 'windows':
            cmd = ['ping', '-f', '-l', str(payload), '-n', '2', '-w', '2000', b_ip]
        else:
            cmd = ['ping', '-M', 'do', '-s', str(payload), '-c', '2', '-W', '2', b_ip]
        try:
            if rf_stats.USE_TPOOL:
                import eventlet.tpool
                result = eventlet.tpool.execute(rf_stats._run_subprocess, cmd, 8)
            else:
                result = rf_stats._run_subprocess(cmd, 8)
        except Exception as exc:
            logger.warning("MTU probe error for %s: %s", b_ip, exc)
            continue
        out = (result.stdout or '').lower()
        fragmented = ('fragment' in out or 'frag needed' in out or 'message too long' in out
                      or 'too long' in out)
        replied = ('ttl=' in out or 'bytes from' in out or 'time=' in out)
        if replied and not fragmented:
            mtu = payload + 28  # add IP(20) + ICMP(8) headers
            return {'status': 'ok', 'path_mtu': mtu, 'tested_to': b_ip,
                    'mtu_warning': mtu < config.VOIP_WG_SAFE_MTU,
                    'note': (f"Path MTU {mtu} is below the wireguard-safe {config.VOIP_WG_SAFE_MTU}; "
                             "SIP/RTP over UDP may fragment or black-hole — check tunnel MTU/MSS.")
                            if mtu < config.VOIP_WG_SAFE_MTU else None}
    return {'status': 'failed', 'tested_to': b_ip,
            'note': 'No DF-bit ping succeeded (node unreachable or all sizes fragment).'}


def run_call_quality(a, b, codec=None):
    """Orchestrate all VOIP probes for A->B; resilient + cached."""
    if not _addr(a):
        return {'error': f'Source "{a}" not found (unknown node and not an IP/host)'}
    codec = _resolve_codec(codec)
    result = {'source': a, 'target': b, 'codec': codec,
              'codec_label': link_health.codec_params(codec)['label']}

    try:
        result['end_to_end'] = end_to_end_quality(a, b, codec)
    except Exception as exc:
        logger.warning("end_to_end failed %s->%s: %s", a, b, exc)
        result['end_to_end'] = {'status': 'failed', 'note': str(exc)}

    e2e = result.get('end_to_end') or {}
    result['g114'] = link_health.g114_delay_rating(e2e.get('one_way_ms'))

    for key, fn in (('segments', lambda: segment_attribution(a, b)),
                    ('capacity', lambda: concurrent_capacity(a, b, codec)),
                    ('mtu', lambda: path_mtu(b))):
        try:
            result[key] = fn()
        except Exception as exc:
            logger.warning("%s probe failed %s->%s: %s", key, a, b, exc)
            result[key] = {'status': 'failed', 'note': str(exc)}

    try:
        database.save_voip_test(a, b, codec, result)
    except Exception as exc:
        logger.warning("save_voip_test failed: %s", exc)
    return result

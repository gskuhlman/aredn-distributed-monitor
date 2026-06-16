"""
Active troubleshooter for a single node.

Unlike incident_report (read-only over stored data), this module *runs live
diagnostics* — pings from the collector, a traceroute, and neighbor-relayed pings
— to investigate each detected problem and recommend a next step. It is invoked
on demand from the node page's "Troubleshoot" button, never on the scan path.
"""

import logging

import database
import rf_stats
import incident_report

logger = logging.getLogger(__name__)

# Cap live link probes so a node with many problem links can't flood the mesh.
MAX_LINK_PROBES = 5


def _pick_reachable_neighbor(node_name):
    """An active (recently-polled) neighbor with a live link to node_name."""
    for link in database.get_node_links(node_name):  # non-removed links
        if link.get('status') != 'good':
            continue
        peer = link['target_node'] if link['source_node'] == node_name else link['source_node']
        peer_node = database.get_node(peer)
        if peer_node and peer_node.get('is_active') == 1 and peer_node.get('ip'):
            return peer_node
    return None


def _ping_ok(result):
    return bool(result) and (result.get('loss') if result.get('loss') is not None else 100) < 100


def _diagnose_reachability(node_name, node_ip):
    """Live check: can the collector reach the node? If not, where does it break,
    and can a neighbor still reach it (node-down vs collector-route-broken)?"""
    checks = []
    if not node_ip:
        checks.append({'action': 'Ping from collector', 'result': 'skipped — no IP on record'})
        return False, checks, 'No IP recorded; run a scan or check the node record.'

    ping = rf_stats.ping_node(node_ip, count=3, timeout=2)
    reachable = _ping_ok(ping)
    checks.append({
        'action': f'Ping {node_ip} from collector',
        'result': (f"reachable, avg {ping.get('avg')} ms" if reachable else 'NO RESPONSE (100% loss)')
    })
    if reachable:
        return True, checks, 'Reachable from the collector — no routing action needed.'

    # Where does the collector's path die?
    tr = rf_stats.traceroute_local(node_ip)
    if tr and tr.get('hops'):
        last = tr['hops'][-1]
        where = last.get('host') or last.get('ip') or '?'
        checks.append({'action': 'Traceroute from collector',
                       'result': f"path reaches {len(tr['hops'])} hop(s); ends at {where}"})

    # Can a neighbor that hears it still route to it?
    neighbor = _pick_reachable_neighbor(node_name)
    if neighbor:
        relay = rf_stats.ping_via_aredn(node_ip, source_node_ip=neighbor['ip'])
        if _ping_ok(relay):
            checks.append({'action': f"Ask neighbor {neighbor['name']} to ping it",
                           'result': 'neighbor CAN reach it'})
            return False, checks, (f"The node is up — {neighbor['name']} can reach it, but the "
                                   "collector's route to it is broken. Fix the collector→node path "
                                   "(see the traceroute hop above), or add a collector nearer this node.")
        checks.append({'action': f"Ask neighbor {neighbor['name']} to ping it",
                       'result': 'neighbor also CANNOT reach it'})
        return False, checks, ("A neighbor that hears it on RF also cannot route to it — the node is "
                               "likely down or wedged. Check power/RF at the node.")
    return False, checks, ('Collector cannot reach it and no active neighbor is available to relay a '
                           'ping. Check the node and the path to it.')


def troubleshoot_node(node_name, hours=24):
    """Detect every problem and actively probe to localize/confirm each."""
    node = database.get_node(node_name)
    if not node:
        return {'node': node_name, 'error': 'Node not found'}
    node_ip = node.get('ip')

    bundle = incident_report.gather_evidence(node_name, hours=hours)
    findings = incident_report.deterministic_findings(bundle)

    steps = []

    # 1. Always actively verify reachability first.
    reachable, checks, recommendation = _diagnose_reachability(node_name, node_ip)
    steps.append({
        'problem': 'reachability',
        'severity': 'info' if reachable else 'high',
        'detail': 'Live check of whether the collector (and a neighbor) can reach the node.',
        'checks': checks,
        'recommendation': recommendation,
    })

    # 2. Walk the detected problems; actively probe link-level ones.
    link_probes = 0
    reachability_causes = {'node_down', 'collector_cannot_poll', 'node_unreachable'}
    for f in findings:
        if f['cause'] in reachability_causes or f['cause'] == 'no_problems_detected':
            continue  # reachability handled above; skip the "all clear" placeholder
        step = {
            'problem': f['cause'],
            'severity': f['severity'],
            'detail': f['evidence'],
            'checks': [],
            'recommendation': f.get('recommendation'),
        }
        target = f.get('target')
        probe_causes = {'lqm_blocked', 'snr_asymmetry', 'high_loss', 'directional_loss'}
        if target and node_ip and f['cause'] in probe_causes and link_probes < MAX_LINK_PROBES:
            link_probes += 1
            peer = database.get_node(target)
            peer_addr = (peer.get('ip') if peer else None) or target
            # Ask THIS node to ping the peer — confirms the current state of the link.
            res = rf_stats.ping_via_aredn(peer_addr, source_node_ip=node_ip)
            if res is None:
                step['checks'].append({'action': f'Ping {target} from {node_name}',
                                       'result': 'probe failed (node unreachable or peer unknown)'})
            elif _ping_ok(res):
                step['checks'].append({'action': f'Ping {target} from {node_name}',
                                       'result': f"reachable now — loss {res.get('loss')}%, avg {res.get('avg')} ms"})
            else:
                step['checks'].append({'action': f'Ping {target} from {node_name}',
                                       'result': '100% loss right now'})
        steps.append(step)

    problem_count = sum(1 for s in steps if s['severity'] in ('high', 'medium'))
    return {
        'node': node_name,
        'window_hours': hours,
        'reachable_from_collector': reachable,
        'problem_count': problem_count,
        'steps': steps,
    }

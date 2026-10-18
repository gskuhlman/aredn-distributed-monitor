"""
Starting-node (gateway) reachability gate.

The configured starting node is this collector's way onto the AREDN mesh. When
it does not answer, a failed probe to any other node most likely means this
collector is off the network, not that the target is down. Probes therefore
check here before starting, and again before recording a failure; when the
gateway is unreachable they abort and discard the result instead.

The answer is cached for a few seconds so a burst of concurrent probe failures
costs one gateway request, not one each.
"""

import logging
import threading
import time

import requests

import config
import database

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15
# Max age when re-checking after a probe failure: fresh enough that a failure
# seen now is judged against the gateway's state now.
FAILURE_RECHECK_SECONDS = 5

_lock = threading.Lock()
_state = {'checked_at': 0.0, 'ok': True}


def starting_node_host():
    """Host (name or IP) of the configured starting node."""
    url = database.get_setting('starting_node', config.STARTING_NODE) or ''
    return url.replace('http://', '').replace('https://', '').split('/')[0]


def unreachable_message():
    return (f"Starting node {starting_node_host()} unreachable -- this collector appears "
            "to be off the AREDN network; result discarded")


def record(ok):
    """Record a gateway reachability result observed elsewhere (e.g. a scan)."""
    with _lock:
        _state['checked_at'] = time.monotonic()
        _state['ok'] = bool(ok)


def probe(host=None):
    """Fetch the starting node's bare sysinfo (cheap, and answers even when the
    node is too loaded for the full LQM/hosts/services request)."""
    host = host or starting_node_host()
    try:
        response = requests.get(f"http://{host}/cgi-bin/sysinfo.json",
                                timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
        response.json()
        ok = True
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Starting node %s unreachable: %s", host, exc)
        ok = False
    record(ok)
    return ok


def is_reachable(max_age=CACHE_SECONDS):
    """Whether the starting node answered within the last `max_age` seconds,
    probing it if the cached answer is older than that."""
    with _lock:
        if time.monotonic() - _state['checked_at'] <= max_age:
            return _state['ok']
    return probe()


def failure_is_ours():
    """Call after a probe failure: True when the starting node is unreachable,
    meaning the failure should be discarded rather than recorded."""
    return not is_reachable(max_age=FAILURE_RECHECK_SECONDS)

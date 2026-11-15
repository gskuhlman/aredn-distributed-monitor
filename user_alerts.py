"""
Alerts to people (users.py), next to the operator notifications in notify.py.

Service alerts: each advertised service alerts its node's owner (the callsign
in the node name) by default. A user can turn their own alerts for a service
off, or ask for alerts from any other service; an admin can add or remove
anyone. SQLite `service_alert_recipients` (node, service, callsign, enabled)
holds only the changes from that default.

Infrastructure team: users an admin puts on the team get alerts for events on
infrastructure nodes (either end of a link), in the groups they pick (nodes,
links, radio, services, phones, routing, incidents) at or above their
minimum severity. Recoveries follow the problems they were sent.

Delivery: the user's e-mail (through the operator's SMTP server, Notifications
page) and/or ntfy topic (the operator's ntfy server), with notify.Dispatcher's
pipeline per user: maintenance windows, suppressions and open incidents
(one alarm for a cut-off site, not one per node), global mute and quiet
hours, flap hold, cooldown and digest. Silenced / ignored services raise no
events at all (service_check.py). One event reaches a user once even when
both a service subscription and the team would send it.
"""

import json
import logging
import queue
import re
import threading
import time

import database
import notify
import users

logger = logging.getLogger(__name__)

SERVICE_EVENTS = ('service_down', 'service_restored')
INFRA_GROUPS = {
    'nodes': ('node_offline', 'node_online', 'node_reboot', 'node_degraded', 'node_clock_skew', 'node_clock_ok',
              'resource_alarm', 'resource_cleared', 'rf_path_lost', 'rf_path_restored'),
    'links': ('link_dropped', 'link_restored', 'one_way_link', 'one_way_link_cleared', 'mcs_drop', 'mcs_restored',
              'link_alert', 'link_alert_cleared', 'link_underperforming', 'link_underperforming_cleared',
              'link_degrading', 'link_degrading_cleared', 'tunnel_degraded', 'tunnel_restored',
              'radio_chain_alarm', 'radio_chain_cleared', 'radio_retries_high', 'radio_retries_cleared',
              'radio_reconnects', 'radio_reconnects_cleared'),
    'radio': ('radio_interference', 'radio_interference_cleared', 'radio_rx_errors', 'radio_rx_errors_cleared',
              'radio_phy_resets', 'radio_phy_resets_cleared', 'radio_fw_crash', 'radio_fw_crash_cleared',
              'radio_distance_short', 'radio_distance_ok', 'noise_jump', 'noise_cleared', 'noise_source',
              'frequency_change', 'radio_config_change', 'ethernet_problem', 'ethernet_cleared'),
    'services': SERVICE_EVENTS,
    'phones': ('phone_unreachable', 'phone_restored', 'pbx_phone_unregistered', 'pbx_phone_registered',
               'pbx_unreachable', 'pbx_restored', 'call_quality_poor', 'call_quality_ok'),
    'routing': ('route_detour', 'route_restored', 'route_flapping', 'route_steady', 'route_count_drop',
                'route_count_restored', 'routing_error', 'routing_restored'),
    'incidents': ('incident_opened', 'incident_resolved'),
}
GROUP_OF = {t: g for g, types in INFRA_GROUPS.items() for t in types}
GROUP_LABELS = {'nodes': 'Nodes up/down, reboots, resources', 'links': 'Links (RF, tunnels, one-way, rate)',
                'radio': 'Radio (interference, errors, noise, channel)', 'services': 'Services',
                'phones': 'Phones, PBX, call quality', 'routing': 'Routes and routing errors',
                'incidents': 'Incidents (any node)'}

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    users.ensure_tables()
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_alert_recipients (
                node_name TEXT NOT NULL,
                service_name TEXT NOT NULL,
                callsign TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                set_by TEXT,
                set_at DATETIME,
                PRIMARY KEY (node_name, service_name, callsign)
            )
        ''')
    _tables_ready = True


# ------------------------------------------------------------ recipients --

def recipients(node, service, conn=None):
    """[{'callsign', 'owner', 'enabled'}] for one service: the owner (unless
    turned off) plus everyone added; turned-off rows included with enabled False."""
    ensure_tables()
    owner = users.callsign_of(node)
    rows = {}

    def read(c):
        for r in c.execute('SELECT callsign, enabled FROM service_alert_recipients WHERE node_name = ? AND service_name = ?',
                           (node, service)):
            rows[r[0]] = bool(r[1])
    if conn is not None:
        read(conn)
    else:
        with database.get_connection() as c:
            read(c)
    out = []
    if owner:
        out.append({'callsign': owner, 'owner': True, 'enabled': rows.pop(owner, True)})
    out += [{'callsign': cs, 'owner': False, 'enabled': on} for cs, on in sorted(rows.items())]
    return out


def node_recipients(node):
    """{service: recipients} for every service advertised by one node."""
    ensure_tables()
    with database.get_connection() as conn:
        names = sorted({r[0] for r in conn.execute('SELECT name FROM services WHERE node_name = ?', (node,))})
        return {n: recipients(node, n, conn) for n in names}


def set_recipient(node, service, callsign, enabled, by=None):
    """Turn one person's alerts for one service on or off. Returns (recipients, error)."""
    ensure_tables()
    cs = users.normalize(callsign)
    if not cs:
        return None, 'Not a callsign'
    if not users.get(cs):
        return None, f'{cs.upper()} has no account'
    with database.get_connection() as conn:
        if not conn.execute('SELECT 1 FROM services WHERE node_name = ? AND name = ?', (node, service)).fetchone():
            return None, 'No such service on that node'
        owner = users.callsign_of(node)
        if cs != owner and not enabled:
            conn.execute('DELETE FROM service_alert_recipients WHERE node_name = ? AND service_name = ? AND callsign = ?',
                         (node, service, cs))
        elif cs == owner and enabled:
            conn.execute('DELETE FROM service_alert_recipients WHERE node_name = ? AND service_name = ? AND callsign = ?',
                         (node, service, cs))   # back to the default
        else:
            conn.execute('''INSERT OR REPLACE INTO service_alert_recipients
                            (node_name, service_name, callsign, enabled, set_by, set_at)
                            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))''',
                         (node, service, cs, 1 if enabled else 0, by))
        return recipients(node, service, conn), None


def my_services(callsign):
    """Services that alert this user: [{node, service, owner, enabled}]."""
    ensure_tables()
    cs = (callsign or '').lower()
    out = {}
    with database.get_connection() as conn:
        for r in conn.execute('SELECT DISTINCT node_name, name FROM services ORDER BY node_name, name'):
            if users.callsign_of(r[0]) == cs:
                out[(r[0], r[1])] = {'node': r[0], 'service': r[1], 'owner': True, 'enabled': True}
        for r in conn.execute('SELECT node_name, service_name, enabled FROM service_alert_recipients WHERE callsign = ?', (cs,)):
            key = (r[0], r[1])
            if key in out:
                out[key]['enabled'] = bool(r[2])
            elif r[2]:
                out[key] = {'node': r[0], 'service': r[1], 'owner': False, 'enabled': True}
    return sorted(out.values(), key=lambda x: (x['node'], x['service']))


# ----------------------------------------------------------------- routing --

def service_of(event, conn=None):
    """The service a service_down / service_restored event is about (by the
    names its node advertises), or None."""
    node, details = event.get('node_name'), event.get('details') or ''
    if not node or not details.startswith('Service '):
        return None

    def names(c):
        return [r[0] for r in c.execute('SELECT DISTINCT name FROM services WHERE node_name = ?', (node,))]
    found = names(conn) if conn is not None else None
    if found is None:
        with database.get_connection() as c:
            found = names(c)
    hits = [n for n in found if details.startswith(f'Service {n} (')]
    if hits:
        return max(hits, key=len)
    m = re.match(r'^Service (.+?) \(', details)
    return m.group(1) if m else None


def _deliverable(u):
    return u and u.get('alerts_enabled') and (u.get('email') or u.get('ntfy_topic'))


def targets(event, people=None, infra=None):
    """[(callsign, rule)] this event goes to. `people`: {callsign: user};
    `infra`: infrastructure node names."""
    etype = event.get('event_type') or ''
    if people is None:
        people = {u['callsign']: users.raw(u['callsign']) for u in users.list_users()}
    people = {cs: u for cs, u in people.items() if _deliverable(u)}
    if not people:
        return []
    out, seen = [], set()
    if etype in SERVICE_EVENTS:
        service = service_of(event)
        if service:
            for r in recipients(event['node_name'], service):
                if r['enabled'] and r['callsign'] in people:
                    out.append((r['callsign'], {'id': 'svc', 'name': f"Service alerts for {r['callsign'].upper()}",
                                                'channels': []}))
                    seen.add(r['callsign'])
    group = GROUP_OF.get(etype)
    if group:
        if infra is None:
            infra = set(database.get_infrastructure_node_names())
        node = (event.get('node_name') or '').lower()
        _, peer = notify.event_subject(event)
        on_infra = node in infra or (peer is not None and peer in infra) or group == 'incidents'
        sev = notify.SEVERITY_RANK.get(event.get('severity') or 'info', 1)
        recovery = etype in notify.PROBLEM_OF
        for cs, u in people.items():
            if cs in seen or not u.get('infra_team') or group not in (u.get('infra_groups') or []) or not on_infra:
                continue
            if not recovery and sev < notify.SEVERITY_RANK.get(u.get('infra_min_severity') or 'warning', 2):
                continue
            out.append((cs, {'id': 'infra', 'name': f"Infrastructure team ({cs.upper()})", 'channels': []}))
    return out


def user_config(u, base=None):
    """notify config for one user: their e-mail (operator's SMTP) and ntfy topic."""
    cfg = json.loads(json.dumps(base or notify.get_config(raw=True)))
    smtp = cfg['channels']['email']
    nt = cfg['channels']['ntfy']
    cfg['enabled'] = True
    cfg['escalation'] = {**cfg.get('escalation', {}), 'enabled': False}
    cfg['channels'] = {
        'email': {**smtp, 'enabled': bool(u.get('email') and smtp.get('host')), 'to': u.get('email') or ''},
        'ntfy': {**nt, 'enabled': bool(u.get('ntfy_topic')), 'topic': u.get('ntfy_topic') or '',
                 'server': nt.get('server') or 'https://ntfy.sh'},
        'discord': {'enabled': False}, 'webhook': {'enabled': False},
    }
    return cfg


def send_test(callsign):
    """Send a test to this user's destinations. {channel: 'sent' | error}."""
    u = users.raw(callsign)
    cfg = user_config(u or {})
    out = {}
    for ch in ('email', 'ntfy'):
        if not cfg['channels'][ch]['enabled']:
            continue
        try:
            notify.send(ch, 'AREDN Monitor test alert',
                        f'Test alert for {callsign.upper()}. Your {ch} alerts work.', cfg=cfg)
            out[ch] = 'sent'
        except notify.SendError as exc:
            out[ch] = str(exc)
    if not out:
        if u and u.get('email') and not notify.get_config()['channels']['email'].get('host'):
            return {'error': 'E-mail alerts need the SMTP server set up on the Notifications page; add an ntfy topic meanwhile.'}
        return {'error': 'Add an e-mail address or an ntfy topic first.'}
    return out


# ------------------------------------------------------------------ worker --

_queue = queue.Queue(maxsize=5000)
_state = {'running': False, 'people': {}, 'infra': set(), 'loaded': 0.0, 'dispatchers': {}, 'dropped': 0}


def _listener(event):
    try:
        if event.get('event_type') in GROUP_OF:
            _queue.put_nowait(event)
    except queue.Full:
        _state['dropped'] += 1
    except Exception:
        pass


def _refresh():
    if time.time() - _state['loaded'] < 30:
        return
    _state['people'] = {u['callsign']: users.raw(u['callsign']) for u in users.list_users()}
    try:
        _state['infra'] = set(database.get_infrastructure_node_names())
    except Exception:
        _state['infra'] = set()
    _state['loaded'] = time.time()


def _dispatcher(cs):
    d = _state['dispatchers'].get(cs)
    if d is None:
        d = _state['dispatchers'][cs] = notify.Dispatcher(acks=False)
    return d


def process(event):
    """Route one event to its people's pipelines."""
    _refresh()
    base = notify.get_config(raw=True)
    for cs, rule in targets(event, _state['people'], _state['infra']):
        u = _state['people'].get(cs)
        try:
            _dispatcher(cs)._handle(rule, event, user_config(u, base))
        except Exception as exc:
            logger.warning('User alert for %s failed: %s', cs, type(exc).__name__)


def _loop():
    while True:
        try:
            try:
                event = _queue.get(timeout=1.0)
            except queue.Empty:
                event = None
            if event is not None:
                process(event)
            if _state['dispatchers']:
                base = notify.get_config(raw=True)
                for cs, d in list(_state['dispatchers'].items()):
                    u = _state['people'].get(cs)
                    if u:
                        d.tick(user_config(u, base))
        except Exception as exc:
            logger.error('User alert worker error: %s', type(exc).__name__)
            time.sleep(1)


def install(start_task=None):
    ensure_tables()
    database.add_event_listener(_listener)
    if _state['running']:
        return
    _state['running'] = True
    if start_task:
        start_task(_loop)
    else:
        threading.Thread(target=_loop, name='user-alerts', daemon=True).start()

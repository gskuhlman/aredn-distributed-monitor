"""
Read-only summary boards (TODO #60, #77, #89).

agencies()  "Agencies board" (/board/agencies): one tile per served agency
            (EOC, hospital ...). Agencies come from the `agency_board`
            setting, a JSON list of {name, site?, nodes?}; without it every
            infrastructure site is one agency. A tile: nodes up / total,
            service checks up / total (with the down ones named), Route Watch
            paths ending at the agency (normal / changed / detour / failing),
            uptime this month (uptime.node_uptime from the 1st), active
            maintenance and open incidents touching it, and an overall state
            ok / degraded / down.
ticker()    "What's happening" lines for the Status page and the wall: open
            incidents, active maintenance, and the newest warning / critical
            events (recoveries included so a line reads "back"), newest first.
"""

import json
from datetime import datetime, timedelta

import database
import local_time

TS_FMT = '%Y-%m-%d %H:%M:%S'
SETTING = 'agency_board'


def agencies_config():
    raw = database.get_setting(SETTING)
    try:
        items = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        items = None
    if items:
        return [{'name': str(a.get('name') or a.get('site') or 'Agency'), 'site': (a.get('site') or '').upper() or None,
                 'nodes': sorted({str(n).strip().lower() for n in (a.get('nodes') or []) if str(n).strip()})}
                for a in items if isinstance(a, dict)]
    names = database.get_site_names()
    sites = sorted({s for s in database.get_infrastructure_nodes().values() if s})
    return [{'name': names.get(s, s), 'site': s, 'nodes': []} for s in sites]


def save_config(items):
    clean = []
    for a in items or []:
        if not isinstance(a, dict) or not (a.get('name') or a.get('site')):
            continue
        nodes = a.get('nodes') or []
        if isinstance(nodes, str):
            nodes = nodes.replace(',', ' ').split()
        clean.append({'name': str(a.get('name') or a.get('site')).strip()[:60],
                      'site': (a.get('site') or '').strip().upper() or None,
                      'nodes': sorted({str(n).strip().lower() for n in nodes if str(n).strip()})})
    database.set_setting(SETTING, json.dumps(clean) if clean else '')
    return agencies_config()


def _members(agency, infra):
    out = set(agency['nodes'])
    if agency['site']:
        out |= {n for n, s in infra.items() if (s or '').upper() == agency['site']}
    return sorted(out)


def agencies(now=None):
    import maintenance
    import route_watch
    import service_check
    import uptime
    now = now or datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    infra = database.get_infrastructure_nodes()
    config_items = agencies_config()
    all_members = sorted({n for a in config_items for n in _members(a, infra)})
    with database.get_connection() as conn:
        nodes = {r['name']: dict(r) for r in conn.execute('SELECT name, is_active, last_seen FROM nodes')}
        up_month = uptime.node_uptime(month_start.strftime(TS_FMT), now.strftime(TS_FMT), all_members, conn)
    try:
        services = service_check.report()['services']
    except Exception:
        services = []
    try:
        paths = route_watch.list_report(hours=24)['paths']
    except Exception:
        paths = []
    try:
        windows = maintenance.active_windows(now)
    except Exception:
        windows = []
    try:
        import incidents
        open_incidents = incidents.list_incidents(status='active', days=31)['incidents']
    except Exception:
        open_incidents = []
    tiles = []
    for a in config_items:
        members = _members(a, infra)
        mset = set(members)
        up = [n for n in members if (nodes.get(n) or {}).get('is_active')]
        svc = [s for s in services if s['node_name'] in mset and s.get('state') not in ('off', 'ignored')]
        svc_up = [s for s in svc if s['state'] == 'up']
        svc_down = [f"{s['service_name']} ({s['node_name']})" for s in svc if s['state'] in ('alarm', 'down')]
        p = [x for x in paths if x.get('enabled') and x['target'] in mset]
        path_counts = {}
        for x in p:
            st = 'failing' if x.get('last') and not x['last']['ok'] else x['status']
            path_counts[st] = path_counts.get(st, 0) + 1
        ups = [u['uptime_pct'] for n, u in up_month.items() if n in mset and u.get('uptime_pct') is not None]
        maint = [w for w in windows if w.get('scope') == 'all' or mset & set(w.get('covered_nodes') or [])]
        inc = [i for i in open_incidents
               if (a['site'] and (i.get('site') or '').upper() == a['site']) or mset & set(i.get('nodes') or [])]
        if members and not up:
            state = 'down'
        elif len(up) < len(members) or svc_down or path_counts.get('failing') or path_counts.get('detour') or inc:
            state = 'degraded'
        else:
            state = 'ok'
        tiles.append({'name': a['name'], 'site': a['site'], 'members': members, 'state': state,
                      'nodes_up': len(up), 'nodes_total': len(members),
                      'nodes_down': [n for n in members if n not in up],
                      'services_up': len(svc_up), 'services_total': len(svc), 'services_down': svc_down,
                      'paths': path_counts, 'paths_total': len(p),
                      'uptime_month_pct': round(sum(ups) / len(ups), 2) if ups else None,
                      'maintenance': [{'id': w['id'], 'note': w.get('note'), 'end': w.get('end_time')} for w in maint],
                      'incidents': [{'id': i['id'], 'title': i['title'], 'status': i['status']} for i in inc]})
    return {'generated_at': now.strftime(TS_FMT), 'month_start': month_start.strftime(TS_FMT), 'agencies': tiles,
            'configured': bool(database.get_setting(SETTING))}


def ticker(limit=20, hours=24, now=None):
    now = now or datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    items = []
    try:
        import incidents
        for i in incidents.list_incidents(status='active', days=31)['incidents']:
            items.append({'t': i.get('updated_at') or i['opened_at'], 'kind': 'incident', 'severity': i.get('severity')
                          or 'warning', 'text': f"Incident: {i['title']} ({i['status']})", 'id': i['id']})
    except Exception:
        pass
    try:
        import maintenance
        for w in maintenance.active_windows(now):
            what = w.get('site') or ', '.join((w.get('covered_nodes') or [])[:3]) or 'all nodes'
            items.append({'t': w.get('start_time'), 'kind': 'maintenance', 'severity': 'info',
                          'text': f"Maintenance on {what} until {str(w.get('end_time') or '')[11:16]} {local_time.abbr(w.get('end_time'))}"
                                  + (f": {w['note']}" if w.get('note') else ''), 'id': w['id']})
    except Exception:
        pass
    with database.get_connection() as conn:
        rows = conn.execute('''SELECT id, timestamp, event_type, node_name, details, severity FROM events
                               WHERE timestamp >= ? AND (severity IN ('warning', 'critical')
                                  OR event_type LIKE '%restored' OR event_type LIKE '%cleared'
                                  OR event_type IN ('node_online', 'incident_resolved', 'gateway_restored'))
                               ORDER BY timestamp DESC LIMIT ?''', (since, limit * 2)).fetchall()
    for r in rows:
        node = r['node_name']
        text = r['details'] or r['event_type'].replace('_', ' ')
        if node and node not in text:
            text = f'{node}: {text}'
        items.append({'t': r['timestamp'], 'kind': 'event', 'severity': r['severity'] or 'info', 'text': text,
                      'event_type': r['event_type'], 'node': node, 'id': r['id']})
    pinned = [i for i in items if i['kind'] != 'event']
    events = [i for i in items if i['kind'] == 'event'][:max(0, limit - len(pinned))]
    return {'generated_at': now.strftime(TS_FMT), 'items': pinned + events, 'hours': hours}

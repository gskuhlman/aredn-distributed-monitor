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
            ok / degraded / down. `details` per agency (shown under the
            tiles): every node (down since, cause from unreachable.py),
            service check (state, since, error) and path (failing since,
            failures in 24 h, where the trace stopped, why) with a
            plain-language `problems` list.
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
# Service-check states that come from a real probe; the rest (no link
# advertised, UDP, outside the mesh ...) are listed as not checked.
CHECKED_STATES = ('up', 'down', 'alarm', 'node_down')


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
        s_rows = _service_rows([s for s in services if s['node_name'] in mset], now)
        svc = [s for s in s_rows if s['checked']]
        svc_up = [s for s in svc if s['state'] == 'up']
        svc_down = [f"{s['service']} ({s['node']})" for s in svc if s['state'] in ('alarm', 'down')]
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
                      'incidents': [{'id': i['id'], 'title': i['title'], 'status': i['status']} for i in inc],
                      'details': _details(members, nodes, s_rows, p, now, paths)})
    return {'generated_at': now.strftime(TS_FMT), 'month_start': month_start.strftime(TS_FMT), 'agencies': tiles,
            'configured': bool(database.get_setting(SETTING))}


def _minutes(since, now):
    try:
        return max(0, int((now - datetime.strptime(str(since)[:19], TS_FMT)).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def duration(minutes):
    """'3 d 4 h', '2 h 5 min', '12 min'."""
    if minutes is None:
        return ''
    d, rem = divmod(int(minutes), 1440)
    h, m = divmod(rem, 60)
    return f'{d} d {h} h' if d else f'{h} h {m} min' if h else f'{m} min'


def _node_rows(members, nodes, now):
    import unreachable
    rows = []
    for n in members:
        node = nodes.get(n)
        if node and node.get('is_active'):
            rows.append({'name': n, 'up': True, 'last_seen': node.get('last_seen')})
            continue
        last = (node or {}).get('last_seen')
        mins = _minutes(last, now) if last else None
        row = {'name': n, 'up': False, 'last_seen': last, 'down_minutes': mins, 'down_for': duration(mins),
               'cause': None, 'cause_label': 'Not polled' if not node else 'Not answering', 'detail': '', 'evidence': ''}
        try:
            c = unreachable.node_cause(n, hours=max(24, (mins or 0) // 60 + 1))
        except Exception:
            c = None
        if c:
            ev = c.get('evidence')
            row.update(cause=c['cause'], cause_label=c['cause_label'], detail=c['detail'],
                       evidence='; '.join(ev) if isinstance(ev, list) else (ev or ''),
                       fail_reasons=c.get('fail_reasons'), reported_by=c.get('reported_by'))
        elif not node:
            row['detail'] = 'Listed for this agency but never polled by the scanner (no such node, or not reachable from here).'
        rows.append(row)
    return rows


def _phones_by_ip():
    try:
        import phones
        phones.ensure_tables()
        with database.get_connection() as conn:
            return {r['ip']: dict(r) for r in conn.execute(
                'SELECT ip, ok, alarm, fail_count, since, last_check, last_ok, last_avg, last_loss, checks, oks '
                'FROM phones WHERE enabled = 1 AND ignored = 0 AND ip IS NOT NULL')}
    except Exception:
        return {}


def _phone_row(s, ph, now):
    """A phone service the service check can't probe, judged by the Phones page pings instead."""
    if ph.get('ok') is None:
        return 'unchecked', None, 'Phone: not pinged yet.'
    if ph.get('ok'):
        avg = f" ({round(ph['last_avg'])} ms)" if ph.get('last_avg') is not None else ''
        return 'up', None, f"Phone answers ping{avg} (Phones page, last {str(ph.get('last_check') or '')[11:16]})."
    mins = _minutes(ph.get('since'), now) if ph.get('since') else None
    return ('alarm' if ph.get('alarm') else 'down'), mins, (
        f"Phone not answering ping ({ph.get('fail_count') or 0} failed checks in a row; last answered "
        f"{ph.get('last_ok') or 'never'}).")


def _service_rows(services, now):
    import re
    rows = []
    by_ip = None
    for s in services:
        state = s.get('state')
        if state not in CHECKED_STATES:
            m = re.search(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', f"{s.get('service_name') or ''} {s.get('link') or ''}")
            by_ip = _phones_by_ip() if by_ip is None else by_ip
            if m and m.group(1) in by_ip:
                st, mins, text = _phone_row(s, by_ip[m.group(1)], now)
                rows.append({'node': s['node_name'], 'service': s['service_name'], 'state': st,
                             'checked': st != 'unchecked', 'problem': st in ('alarm', 'down'),
                             'since': by_ip[m.group(1)].get('since'), 'down_for': duration(mins),
                             'availability_pct': None, 'last_check': by_ip[m.group(1)].get('last_check'),
                             'text': text, 'via': 'phone ping'})
                continue
        checked = state in CHECKED_STATES
        mins = _minutes(s.get('since'), now) if state != 'up' and s.get('since') else None
        if state in ('alarm', 'down'):
            text = (f"Not answering on {s.get('link') or 'its advertised link'}"
                    + (f" ({s['last_error']})" if s.get('last_error') else '') + ' while its node answers the scanner.')
        elif state == 'node_down':
            text = 'Its node is down, so the service is too.'
        elif state == 'up':
            text = (f"Answering" + (f" in {round(s['last_ms'])} ms" if s.get('last_ms') is not None else '') + '.')
        else:
            text = 'Not checked: ' + {'no link advertised': 'the node advertises it without a link to probe.'}.get(
                s.get('last_error'), (s.get('last_error') or 'this kind of service is not probed') + '.')
        rows.append({'node': s['node_name'], 'service': s['service_name'], 'state': state, 'checked': checked,
                     'problem': state in ('alarm', 'down', 'node_down'), 'since': s.get('since'),
                     'down_for': duration(mins), 'availability_pct': s.get('availability_pct'),
                     'last_check': s.get('last_check'), 'text': text})
    return rows


def _last_hop(hops_json):
    try:
        hops = [h for h in json.loads(hops_json or '[]') if h.get('name') and not h.get('timeout')]
    except (TypeError, ValueError):
        return None
    return hops[-1]['name'] if hops else None


def _path_rows(paths, now, all_paths=()):
    rows = []
    targets_by_source = {}
    for o in all_paths:
        if o.get('enabled'):
            targets_by_source.setdefault(o.get('source') or '', set()).add(o['target'])
    since_24 = (now - timedelta(hours=24)).strftime(TS_FMT)
    with database.get_connection() as conn:
        for x in paths:
            last = x.get('last') or {}
            hist = conn.execute('SELECT id, timestamp, ok, reason, hops FROM route_history WHERE path_id = ? '
                                'AND timestamp >= ? ORDER BY id', (x['id'], since_24)).fetchall()
            fails = sum(1 for r in hist if not r['ok'])
            last_ok = next((r['timestamp'] for r in reversed(hist) if r['ok']), None)
            failing = bool(last) and not last.get('ok')
            failing_since = None
            if failing:
                streak = [r for r in hist if not last_ok or r['timestamp'] > last_ok]
                failing_since = streak[0]['timestamp'] if streak else last.get('timestamp')
            src = x.get('source') or 'the starting node'
            answered = [h for h in (last.get('hops') or []) if h.get('name') and not h.get('timeout')]
            route = ' → '.join([src] + [h['name'] for h in answered])
            base = x.get('baseline') if isinstance(x.get('baseline'), list) else []
            usual = ' → '.join([src] + [h for h in base if h and h != '*']) if base else ''
            if failing and last.get('reason') == 'no_answer':
                why = f"{src} did not answer the traceroute request (the node or its web page is not reachable from here)."
            elif failing:
                stop = answered[-1]['name'] if answered else None
                why = (f"The trace from {src} stopped at {stop} after {len(answered)} hop(s) without reaching {x['target']}."
                       if stop else f"The trace from {src} got no answer from any hop toward {x['target']}.")
                siblings = targets_by_source.get(x.get('source') or '', set()) - {x['target']}
                mixed = sum(1 for r in hist if not r['ok'] and _last_hop(r['hops']) in siblings)
                if stop in siblings:
                    why += (f" But {stop} is the end of another watched path traced from the same node at the same "
                            "time: this is that trace's answer, mixed up by the node's traceroute page, not a "
                            "network fault. The monitor now runs one trace at a time per node.")
                if mixed:
                    why += f" {mixed} of the {fails} failed traces in 24 h were mixed-up answers like this"
                    why += f"; {fails - mixed} look real." if fails > mixed else '.'
                elif fails < len(hist):
                    why += f" It reached it in {len(hist) - fails} of {len(hist)} traces in 24 h, so it comes and goes."
            elif x.get('status') in ('detour', 'changed'):
                why = 'Reaching it, but not on the usual route: ' + ('; '.join(x.get('reasons') or []) or 'route differs') + '.'
            else:
                why = 'Reaching it on the usual route.'
            rows.append({'id': x['id'], 'label': x.get('label'), 'source': x.get('source'), 'target': x['target'],
                         'role': x.get('role'), 'status': 'failing' if failing else x.get('status'),
                         'problem': failing or x.get('status') == 'detour', 'failing_since': failing_since,
                         'down_for': duration(_minutes(failing_since, now)) if failing_since else '',
                         'last_trace': last.get('timestamp'), 'last_ok': last_ok, 'fails_24h': fails,
                         'runs_24h': len(hist), 'route_now': route, 'usual_route': usual,
                         'reasons': x.get('reasons') or [], 'flapping': x.get('flapping'), 'text': why})
    return rows


def _details(members, nodes, s_rows, paths, now, all_paths=()):
    n_rows = _node_rows(members, nodes, now)
    p_rows = _path_rows(paths, now, all_paths)
    problems = []
    for r in n_rows:
        if not r['up']:
            problems.append(f"Node {r['name']} down" + (f" for {r['down_for']}" if r['down_for'] else '')
                            + f": {r['cause_label']}.")
    for r in s_rows:
        if r['problem']:
            problems.append(f"Service {r['service']} on {r['node']}" + (f" down for {r['down_for']}" if r['down_for'] else ' down')
                            + f": {r['text']}")
    for r in p_rows:
        if r['problem']:
            head = f"Path {r['label'] or r['target']} {r['status']}" + (f" for {r['down_for']}" if r['down_for'] else '')
            problems.append(f"{head}: {r['text']}")
    return {'problems': problems, 'nodes': n_rows, 'services': s_rows, 'paths': p_rows}


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

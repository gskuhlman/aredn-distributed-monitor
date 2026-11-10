"""Club status page: infrastructure radios and the nodes RF-linked to them,
plus the VoIP endpoints, as one self-contained HTML fragment for pasting into
a club website CMS (/export/club-status).

Inline styles only (CMS editors usually strip <style> and <script>), no
external resources, and everything escaped. Read-only. Mesh IP addresses are
left out unless asked for (`include_ips`), since the page is meant to be public.
"""

import html
import re
from datetime import datetime, timedelta

import database
import hygiene
import local_time
import maintenance
import phones
import unreachable as unreachable_causes
import uptime

TS_FMT = '%Y-%m-%d %H:%M:%S'
UP_WITHIN = timedelta(minutes=30)
IP_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')
MODE_LABEL = {'adhoc': 'Mesh', 'ptp': 'PtP AP', 'ptmp': 'PtMP AP', 'sta': 'Station'}

# Status -> (label, text colour, background)
STATUS = {
    'up': ('Up', '#116329', '#dafbe1'),
    'down': ('Down', '#a40e26', '#ffebe9'),
    'maintenance': ('Maintenance', '#7d4e00', '#fff8c5'),
    'stale': ('Not checked', '#57606a', '#eaeef2'),
}
PHONE_STATUS = {'up': 'up', 'down': 'down', 'alarm': 'down', 'node_down': 'down', 'unchecked': 'stale'}


def _ts(v):
    try:
        return datetime.strptime(str(v)[:19], TS_FMT)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_uptime(seconds):
    s = _num(seconds)
    if s is None or s < 0:
        return None
    d, rem = divmod(int(s), 86400)
    h, rem = divmod(rem, 3600)
    if d:
        return f'{d} d {h} h'
    return f'{h} h {rem // 60} m'


def _radio(n):
    freq = _num(n.get('rf_frequency'))
    off = str(n.get('rf_status') or '').lower() == 'off'
    width = _num(n.get('rf_chanbw'))
    return {
        'radio_off': off,
        'frequency': int(freq) if freq else None,
        'channel': n.get('rf_channel'),
        'width': int(width) if width else None,
        'mode': MODE_LABEL.get(str(n.get('rf_mode') or '').lower(), n.get('rf_mode')),
        'ssid': n.get('rf_ssid'),
    }


def _status(n, as_of, covered):
    if n['name'] in covered:
        return 'maintenance'
    seen = _ts(n.get('last_seen'))
    if n.get('is_active') and seen and as_of - seen <= UP_WITHIN:
        return 'up'
    return 'down' if seen else 'stale'


def _rf_clients(conn, infra):
    """{client: [{via, snr, quality}]} for current RF links touching an
    infrastructure radio (ghost trackers: radio off / cabled left out)."""
    try:
        import ghost_trackers
        ghosts = ghost_trackers.excluded_unordered(conn)
    except Exception:
        ghosts = set()
    out = {}
    rows = conn.execute("SELECT source_node, target_node, snr, quality, status FROM links "
                        "WHERE UPPER(link_type) = 'RF' AND status != 'removed'").fetchall()
    for src, tgt, snr, q, status in rows:
        if tuple(sorted((src, tgt))) in ghosts:
            continue
        for a, b in ((src, tgt), (tgt, src)):
            if a in infra and b not in infra:
                out.setdefault(b, {})
                cur = out[b].get(a)
                # Keep the client-side view when both ends report the link.
                if cur is None or src == b:
                    out[b][a] = {'via': a, 'snr': snr, 'quality': q, 'status': status}
    return {k: sorted(v.values(), key=lambda x: -(x['snr'] or 0)) for k, v in out.items()}


def _availability(start, end, names, conn):
    up = uptime.node_uptime(start, end, names, conn)
    try:
        causes = unreachable_causes.classify(start, end, names, conn)
    except Exception:
        causes = {}
    out = {}
    for name, u in up.items():
        pct = u.get('uptime_pct')
        if (causes.get(name) or {}).get('explained'):
            pct = None
        out[name] = {'pct': pct, 'reboots': u.get('reboots'), 'down_minutes': u.get('down_minutes')}
    return out


def _phone_name(p, include_ips):
    name = p.get('name') or p.get('host_name') or ''
    if not include_ips:
        name = IP_RE.sub('', name)
        name = re.sub(r'\bDirect IP\b\s*:?|\bport\s*\d+', ' ', name, flags=re.I)
        name = re.sub(r'\s+', ' ', name).strip(' -:()')
        if not name or name == (p.get('extension') or ''):
            name = p.get('host_name') or name or 'Phone'
    return name


def build(days=30, include_ips=False, now=None):
    now = now or datetime.now()
    end = now.strftime(TS_FMT)
    start = (now - timedelta(days=days)).strftime(TS_FMT)
    day = (now - timedelta(days=1)).strftime(TS_FMT)
    site_names = database.get_site_names()
    with database.get_connection() as conn:
        infra = {r[0]: r[1] for r in conn.execute('SELECT node_name, site FROM infrastructure_nodes')}
        clients = _rf_clients(conn, set(infra))
        names = set(infra) | set(clients)
        nodes = {r['name']: dict(r) for r in conn.execute('SELECT * FROM nodes')}
        names &= set(nodes)
        as_of = _ts(conn.execute('SELECT MAX(last_seen) FROM nodes').fetchone()[0]) or now
        avail = _availability(start, end, names, conn)
        avail_day = _availability(day, end, names, conn)
    covered = set()
    try:
        for w in maintenance.active_windows(now):
            covered.update(w.get('covered_nodes') or [])
        upcoming = [w for w in maintenance.list_windows(days=1, now=now)
                    if w.get('status') in ('active', 'grace', 'upcoming')
                    and (_ts(w.get('start_time')) or now) <= now + timedelta(days=7)]
    except Exception:
        upcoming = []
    hyg = {}
    try:
        hyg = {r['name']: r for r in hygiene.report(include_inactive=True)['nodes'] if r['name'] in names}
    except Exception:
        pass

    def row(name):
        n = nodes[name]
        h = hyg.get(name) or {}
        a = avail.get(name) or {}
        return {
            'name': name, 'model': n.get('model'), 'firmware': n.get('firmware_version'),
            'description': n.get('description'), **_radio(n),
            'status': _status(n, as_of, covered), 'last_seen': n.get('last_seen'),
            'uptime': fmt_uptime(n.get('uptime_seconds')) if n.get('is_active') else None,
            'available_pct': a.get('pct'), 'available_24h_pct': (avail_day.get(name) or {}).get('pct'),
            'reboots': a.get('reboots'),
            'hygiene': h.get('score'), 'fixes': h.get('fixes') or [],
            'antenna': n.get('antenna_desc'), 'azimuth': n.get('antenna_azimuth'),
        }

    sites = {}
    for name, code in infra.items():
        if name not in nodes:
            continue
        r = row(name)
        r['rf_clients'] = sum(1 for c in clients.values() for l in c if l['via'] == name)
        sites.setdefault(code or '?', []).append(r)
    site_list = [{'code': code, 'name': site_names.get(code, code),
                  'radios': sorted(rs, key=lambda r: r['name'])} for code, rs in sorted(sites.items())]
    client_rows = []
    for name in sorted(n for n in clients if n in nodes):
        r = row(name)
        r['links'] = clients[name]
        client_rows.append(r)

    ph = []
    try:
        rep = phones.report(hours=24 * days, now=now)
        for p in rep['phones']:
            if p.get('ignored') or not p.get('enabled'):
                continue
            ph.append({'name': _phone_name(p, include_ips), 'extension': p.get('extension'), 'kind': p.get('kind'),
                       'ip': p.get('ip') if include_ips else None, 'node': p.get('attached_node'),
                       'status': PHONE_STATUS.get(p.get('state'), 'stale'),
                       'answered_pct': p.get('window_pct'), 'median_ms': p.get('window_median_ms'),
                       'last_check': p.get('last_check')})
    except Exception:
        pass
    ph.sort(key=lambda p: (p['kind'] != 'pbx', str(p['extension'] or 'zzz'), p['name']))

    try:
        import incidents
        open_incidents = [{'title': i['title'], 'opened_at': i['opened_at'], 'status': i['status']}
                          for i in incidents.list_incidents(status='active', days=31)['incidents']]
    except Exception:
        open_incidents = []

    radios = [r for s in site_list for r in s['radios']]
    pcts = [r['available_pct'] for r in radios + client_rows if r['available_pct'] is not None]
    scores = [r['hygiene'] for r in radios + client_rows if r['hygiene'] is not None]
    return {
        'generated_at': now.strftime('%Y-%m-%d %H:%M'), 'as_of': as_of.strftime('%Y-%m-%d %H:%M'),
        'days': days, 'include_ips': include_ips,
        'sites': site_list, 'clients': client_rows, 'phones': ph,
        'incidents': open_incidents,
        'maintenance': [{'note': w.get('note'), 'start': w.get('start_time'), 'end': w.get('end_time'),
                         'status': w.get('status'), 'nodes': len(w.get('covered_nodes') or [])} for w in upcoming],
        'summary': {
            'radios': len(radios), 'radios_up': sum(1 for r in radios if r['status'] == 'up'),
            'clients': len(client_rows), 'clients_up': sum(1 for r in client_rows if r['status'] == 'up'),
            'phones': len(ph), 'phones_up': sum(1 for p in ph if p['status'] == 'up'),
            'available_pct': round(sum(pcts) / len(pcts), 1) if pcts else None,
            'hygiene': round(sum(scores) / len(scores)) if scores else None,
        },
    }


# --- HTML ------------------------------------------------------------------

E = html.escape
FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
TABLE = 'border-collapse:collapse;width:100%;font-size:13px;min-width:640px'
TH = 'text-align:left;padding:6px 8px;border-bottom:2px solid #d0d7de;background:#f6f8fa;white-space:nowrap'
TD = 'padding:6px 8px;border-bottom:1px solid #eaeef2;vertical-align:top'
MUTED = 'color:#57606a'


def _badge(status):
    label, fg, bg = STATUS.get(status, STATUS['stale'])
    return (f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;'
            f'font-weight:600;color:{fg};background:{bg}">{label}</span>')


def _pct(v, good=99.0, fair=95.0):
    if v is None:
        return f'<span style="{MUTED}">–</span>'
    colour = '#116329' if v >= good else '#7d4e00' if v >= fair else '#a40e26'
    return f'<span style="color:{colour};font-weight:600">{v:.1f}%</span>'


def _score(v):
    if v is None:
        return f'<span style="{MUTED}">–</span>'
    colour = '#116329' if v >= 85 else '#7d4e00' if v >= 60 else '#a40e26'
    return f'<span style="color:{colour};font-weight:600">{v}</span>'


def _channel(r):
    if r['radio_off']:
        return f'<span style="{MUTED}">Radio off</span>'
    if not r['frequency']:
        return f'<span style="{MUTED}">–</span>'
    ch = f' <span style="{MUTED}">(ch {E(str(r["channel"]))})</span>' if r.get('channel') else ''
    return f'{r["frequency"]} MHz{ch}'


def _table(headers, rows):
    head = ''.join(f'<th style="{TH}">{h}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td style="{TD}">{c}</td>' for c in cells) + '</tr>' for cells in rows)
    return (f'<div style="overflow-x:auto;margin:0 0 16px"><table style="{TABLE}">'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def _tile(label, value, sub=''):
    return (f'<div style="flex:1 1 150px;border:1px solid #d0d7de;border-radius:8px;padding:10px 12px;background:#fff">'
            f'<div style="font-size:12px;{MUTED}">{label}</div>'
            f'<div style="font-size:22px;font-weight:700;margin-top:2px">{value}</div>'
            f'<div style="font-size:12px;{MUTED}">{sub}</div></div>')


def _h(text, level=3):
    size = {2: 20, 3: 16}[level]
    return f'<h{level} style="font-size:{size}px;margin:20px 0 8px">{text}</h{level}>'


def render(d, title='Mesh Network Status', hygiene_details=True, voip=True):
    s = d['summary']
    out = [f'<div style="{FONT};color:#1f2328;line-height:1.4;max-width:1200px">',
           f'<h2 style="font-size:22px;margin:0 0 4px">{E(title)}</h2>',
           f'<p style="margin:0 0 12px;font-size:13px;{MUTED}">Generated {E(local_time.stamp(d["generated_at"]))} '
           f'from the AREDN network monitor (last scan {E(local_time.stamp(d["as_of"]))}). All times are '
           f'{E(local_time.label(d["generated_at"]))}. Availability is the share of the last '
           f'{d["days"]} days each node was running and answering the monitor, planned maintenance left out.</p>']

    tiles = [_tile('Infrastructure radios up', f'{s["radios_up"]} / {s["radios"]}'),
             _tile('RF-connected nodes up', f'{s["clients_up"]} / {s["clients"]}'),
             _tile(f'Average availability ({d["days"]} d)',
                   f'{s["available_pct"]:.1f}%' if s['available_pct'] is not None else '–'),
             _tile('Average setup score', s['hygiene'] if s['hygiene'] is not None else '–', 'out of 100')]
    if voip:
        tiles.append(_tile('Phones answering', f'{s["phones_up"]} / {s["phones"]}'))
    out.append(f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px">{"".join(tiles)}</div>')

    notices = [f'<li><b>Incident:</b> {E(i["title"])} <span style="{MUTED}">(since {E(local_time.stamp(i["opened_at"]))})</span></li>'
               for i in d['incidents']]
    notices += [f'<li><b>{"Maintenance now" if m["status"] in ("active", "grace") else "Planned maintenance"}:</b> '
                f'{E(m["start"][:16])} – {E(m["end"][11:16] if m["start"][:10] == m["end"][:10] else m["end"][:16])} '
                f'{local_time.abbr(m["end"])}'
                f'{": " + E(m["note"]) if m.get("note") else ""} <span style="{MUTED}">({m["nodes"]} nodes)</span></li>'
                for m in d['maintenance']]
    if notices:
        out.append('<div style="border:1px solid #d4a72c;background:#fff8c5;border-radius:8px;padding:8px 12px;'
                   f'margin:0 0 12px;font-size:13px"><ul style="margin:0;padding-left:18px">{"".join(notices)}</ul></div>')

    out.append(_h('Infrastructure sites', 2))
    for site in d['sites']:
        up = sum(1 for r in site['radios'] if r['status'] == 'up')
        out.append(_h(f'{E(site["name"])} <span style="font-weight:400;font-size:13px;{MUTED}">'
                      f'({E(site["code"])}, {up} of {len(site["radios"])} up)</span>'))
        out.append(_table(
            ['Radio', 'Status', 'Frequency', 'Width', 'Mode', 'RF clients', 'Up for',
             f'Available {d["days"]} d', '24 h', 'Setup score', 'Hardware / firmware'],
            [[f'<b>{E(r["name"])}</b>' + (f'<br><span style="font-size:12px;{MUTED}">{E(r["antenna"])}</span>'
                                          if r.get('antenna') else ''),
              _badge(r['status']), _channel(r), f'{r["width"]} MHz' if r['width'] and not r['radio_off'] else '–',
              E(r['mode'] or '–'), r['rf_clients'], E(r['uptime'] or '–'),
              _pct(r['available_pct']), _pct(r['available_24h_pct']), _score(r['hygiene']),
              f'{E(r["model"] or "–")}<br><span style="font-size:12px;{MUTED}">{E(r["firmware"] or "")}</span>']
             for r in site['radios']]))

    out.append(_h('Nodes connected over RF', 2))
    out.append(f'<p style="margin:0 0 8px;font-size:13px;{MUTED}">Every node with a current RF link to an '
               'infrastructure radio. SNR is the signal-to-noise ratio of that link in dB (higher is better; '
               'about 15 dB is the minimum AREDN uses).</p>')
    out.append(_table(
        ['Node', 'Status', 'Connected to', 'SNR', 'Link quality', 'Frequency', 'Width', 'Up for',
         f'Available {d["days"]} d', 'Setup score', 'Hardware / firmware'],
        [[f'<b>{E(r["name"])}</b>', _badge(r['status']),
          '<br>'.join(E(l['via']) for l in r['links']),
          '<br>'.join(f'{l["snr"]} dB' if l['snr'] is not None else '–' for l in r['links']),
          '<br>'.join(f'{l["quality"]}%' if l['quality'] is not None else '–' for l in r['links']),
          _channel(r), f'{r["width"]} MHz' if r['width'] and not r['radio_off'] else '–',
          E(r['uptime'] or '–'), _pct(r['available_pct']), _score(r['hygiene']),
          f'{E(r["model"] or "–")}<br><span style="font-size:12px;{MUTED}">{E(r["firmware"] or "")}</span>']
         for r in d['clients']]))

    if voip and d['phones']:
        out.append(_h('VoIP phones and PBXs', 2))
        out.append(f'<p style="margin:0 0 8px;font-size:13px;{MUTED}">Each endpoint is pinged from the monitor '
                   f'after every scan; "Answered" is the share of those checks over the last {d["days"]} days.</p>')
        heads = ['Endpoint', 'Extension', 'Type', 'Status', 'On node', f'Answered {d["days"]} d', 'Typical ping']
        if d['include_ips']:
            heads.insert(1, 'IP')
        rows = []
        for p in d['phones']:
            cells = [E(p['name']), E(p['extension'] or '–'), 'PBX' if p['kind'] == 'pbx' else 'Phone',
                     _badge(p['status']), E(p['node'] or '–'), _pct(p['answered_pct'], 98, 90),
                     f'{p["median_ms"]:.0f} ms' if p['median_ms'] is not None else '–']
            if d['include_ips']:
                cells.insert(1, E(p['ip'] or ''))
            rows.append(cells)
        out.append(_table(heads, rows))

    if hygiene_details:
        nodes = [r for s_ in d['sites'] for r in s_['radios']] + d['clients']
        todo = sorted((r for r in nodes if r['fixes']), key=lambda r: (r['hygiene'] or 0, r['name']))
        out.append(_h('Node setup checklist', 2))
        out.append(f'<p style="margin:0 0 8px;font-size:13px;{MUTED}">The setup score (0–100) checks that a node '
                   'has its location, current firmware, a description, a way to reach the owner, a callsign name, '
                   'antenna azimuth, model and height, its clock on Central time, and that the monitor could read '
                   'it in the last day. '
                   'Owners: these are the fixes that would raise your score.</p>')
        if todo:
            items = []
            for r in todo:
                fixes = ''.join(f'<li>{E(f)}</li>' for f in r['fixes'])
                items.append(f'<details style="border-bottom:1px solid #eaeef2;padding:4px 0">'
                             f'<summary style="cursor:pointer"><b>{E(r["name"])}</b> &ndash; score {_score(r["hygiene"])}'
                             f' <span style="{MUTED}">({len(r["fixes"])} to fix)</span></summary>'
                             f'<ul style="margin:4px 0 6px;padding-left:20px;font-size:13px">{fixes}</ul></details>')
            out.append(f'<div style="font-size:14px;margin:0 0 16px">{"".join(items)}</div>')
        else:
            out.append('<p style="font-size:13px">Every node passes all checks.</p>')

    out.append(f'<p style="font-size:12px;{MUTED};margin:16px 0 0">Status: Up = answered the monitor within '
               '30 minutes of the last scan; Down = not answering; Maintenance = planned work. '
               'Up for = time since the node last restarted.</p>')
    out.append('</div>')
    return '\n'.join(out)

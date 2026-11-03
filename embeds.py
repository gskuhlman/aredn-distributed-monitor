"""
Embeddable panels (TODO #72), like meshviewer's nodeInfos: small read-only
pages and images other sites can show -- the club website in an <iframe>,
a Grafana "Text"/"HTML" panel, a forum post badge.

  /embed/node/<name>            node card: up/down, uptime, polls answered
                                (24 h), firmware/model, RF links with SNR /
                                LQ and a 24 h quality sparkline per link;
  /embed/site/<code>            site card: each radio's state, channel and
                                RF clients;
  /embed/node/<name>/badge.svg  "name | up 12d" badge (green/amber/red);
  /embed/site/<code>/badge.svg  "COL | 7/8 up" badge.
Every page links back to the full page here and, when GRAFANA_URL is set, to
the Grafana dashboard. Pages refresh themselves (?refresh=seconds, default
120, 0 = off) and take ?theme=dark. Nothing is written.
"""

from datetime import datetime, timedelta
from html import escape

import config
import database
import site_dashboard

TS_FMT = '%Y-%m-%d %H:%M:%S'


def _age(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds >= 86400:
        return f'{seconds // 86400}d'
    if seconds >= 3600:
        return f'{seconds // 3600}h'
    return f'{max(1, seconds // 60)}m'


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _state(row, now):
    seen = row.get('last_seen')
    if not row.get('is_active') or not seen:
        return 'down'
    try:
        age = (now - datetime.strptime(seen[:19], TS_FMT)).total_seconds()
    except ValueError:
        return 'down'
    return 'up' if age <= max(3 * int(config.POLL_INTERVAL or 180), 900) else 'stale'


def node_card(name, hours=24):
    name = (name or '').strip().lower()
    now = datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        row = conn.execute('SELECT * FROM nodes WHERE name = ?', (name,)).fetchone()
        if not row:
            return None
        node = dict(row)
        polls = conn.execute('''SELECT COUNT(*), SUM(CASE WHEN reachable THEN 1 ELSE 0 END)
                                FROM node_health_history WHERE node_name = ? AND timestamp >= ?''',
                             (name, since)).fetchone()
        links = [dict(r) for r in conn.execute('''
            SELECT target_node, link_type, quality, snr, rev_snr, status FROM links
            WHERE source_node = ? AND status != 'removed' ORDER BY link_type, target_node''', (name,))]
        for l in links:
            if (l['link_type'] or '').upper() != 'RF':
                continue
            l['series'] = [r[0] for r in conn.execute('''
                SELECT AVG(COALESCE(quality, lq)) FROM link_history
                WHERE source_node = ? AND target_node = ? AND timestamp >= ?
                GROUP BY strftime('%Y-%m-%d %H', timestamp) ORDER BY MIN(timestamp)''',
                (name, l['target_node'], since))]
    infra = database.get_infrastructure_nodes()
    return {'kind': 'node', 'name': name, 'state': _state(node, now), 'uptime': _age(node.get('uptime_seconds')),
            'model': node.get('model'), 'firmware': node.get('firmware_version'),
            'description': node.get('description'), 'site': infra.get(name),
            'frequency': _f(node.get('rf_frequency')), 'chanbw': _f(node.get('rf_chanbw')),
            'polls': polls[0] or 0, 'answered': polls[1] or 0,
            'availability': round(100 * (polls[1] or 0) / polls[0], 1) if polls[0] else None,
            'rf': [l for l in links if (l['link_type'] or '').upper() == 'RF'],
            'other': [l for l in links if (l['link_type'] or '').upper() != 'RF'],
            'last_seen': node.get('last_seen'), 'generated_at': now.strftime(TS_FMT)}


def site_card(code):
    d = site_dashboard.build(code, hours=24)
    if d is None:
        return None
    now = datetime.now()
    radios = []
    for r in d['radios']:
        clients = next((s.get('clients') for s in d.get('sectors') or [] if s.get('name') == r['name']), None)
        radios.append({'name': r['name'], 'state': _state(r, now), 'frequency': _f(r.get('frequency')),
                       'chanbw': _f(r.get('chanbw')), 'channel': r.get('channel'), 'mode': r.get('mode'),
                       'uptime': _age(r.get('uptime_seconds')), 'rf_status': r.get('rf_status'),
                       'clients': len(clients) if isinstance(clients, list) else None})
    return {'kind': 'site', 'code': d['code'], 'name': d['name'], 'radios': radios,
            'up': sum(1 for r in radios if r['state'] == 'up'), 'generated_at': now.strftime(TS_FMT)}


def _spark(values, w=90, h=18):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ''
    step = w / (len(values) - 1)
    pts = ' '.join(f'{i * step:.1f},{h - (v or 0) / 100 * (h - 2) - 1:.1f}' for i, v in enumerate(values)
                   if v is not None)
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')


COLORS = {'up': '#1e8449', 'stale': '#b9770e', 'down': '#c0392b'}


def badge(label, value, state):
    """Shields-style SVG badge."""
    lw, vw = 7 * len(label) + 12, 7 * len(value) + 12
    color = COLORS.get(state, '#7f8c8d')
    label, value = escape(label), escape(value)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + vw}" height="20" role="img" '
            f'aria-label="{label}: {value}"><title>{label}: {value}</title>'
            f'<rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" text-anchor="middle">'
            f'<text x="{lw / 2}" y="14">{label}</text><text x="{lw + vw / 2}" y="14">{value}</text></g></svg>')


def node_badge(name):
    c = node_card(name)
    if not c:
        return badge(name or '?', 'unknown', None)
    value = (f"up {c['uptime']}" if c['state'] == 'up' and c['uptime'] else c['state'])
    return badge(c['name'], value, c['state'])


def site_badge(code):
    c = site_card(code)
    if not c:
        return badge((code or '?').upper(), 'unknown', None)
    n = len(c['radios'])
    state = 'up' if c['up'] == n else 'down' if c['up'] == 0 else 'stale'
    return badge(c['code'], f"{c['up']}/{n} up", state)


def render(card, base_url, refresh=120, theme='light'):
    """Self-contained HTML (inline CSS, no scripts) for an iframe."""
    dark = theme == 'dark'
    css = f'''
      :root {{ --bg: {'#1c2128' if dark else '#ffffff'}; --fg: {'#e6edf3' if dark else '#1f2933'};
               --muted: {'#8b949e' if dark else '#6b7785'}; --line: {'#30363d' if dark else '#e3e7eb'}; }}
      body {{ margin: 0; padding: 10px 12px; background: var(--bg); color: var(--fg);
              font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }}
      h1 {{ font-size: 15px; margin: 0 0 4px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
      .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
      .muted {{ color: var(--muted); font-size: 12px; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 6px; }}
      td, th {{ text-align: left; padding: 3px 6px 3px 0; border-bottom: 1px solid var(--line); font-size: 12px; }}
      th {{ color: var(--muted); font-weight: 500; }}
      .spark {{ color: #2e86c1; vertical-align: middle; }}
      a {{ color: #2e86c1; text-decoration: none; }}
      footer {{ margin-top: 6px; }}'''
    dot = lambda s: f'<span class="dot" style="background:{COLORS.get(s, "#7f8c8d")}" title="{escape(s or "")}"></span>'
    if card['kind'] == 'node':
        full = f"{base_url}nodes/{escape(card['name'])}"
        ch = (f" · {card['frequency']:g} MHz / {card['chanbw']:g} MHz" if card['frequency'] and card['chanbw'] else '')
        body = f'''<h1>{dot(card['state'])}{escape(card['name'])}
            <span class="muted">{escape(card['state'])}{(' · up ' + card['uptime']) if card['uptime'] and card['state'] == 'up' else ''}</span></h1>
          <div class="muted">{escape(card['model'] or '')}{' · ' + escape(card['firmware']) if card['firmware'] else ''}{escape(ch)}
            {f" · {card['availability']}% of polls answered (24 h)" if card['availability'] is not None else ''}</div>
          {f'<div class="muted">{escape(card["description"])}</div>' if card['description'] else ''}
          {('<table><tr><th>RF link</th><th>SNR</th><th>LQ</th><th>24 h quality</th></tr>' + ''.join(
              f"<tr><td>{dot('up' if l['status'] == 'good' else 'down')} {escape(l['target_node'])}</td>"
              f"<td>{escape(str(l['snr'] if l['snr'] is not None else '-'))}</td>"
              f"<td>{escape(str(l['quality'] if l['quality'] is not None else '-'))}</td>"
              f"<td>{_spark(l.get('series') or [])}</td></tr>" for l in card['rf']) + '</table>') if card['rf'] else ''}
          {f"<div class='muted'>Also {len(card['other'])} DtD / tunnel link(s).</div>" if card['other'] else ''}'''
    else:
        full = f"{base_url}sites/{escape(card['code'])}"
        rows = []
        for r in card['radios']:
            span = ' ({:g}/{:g})'.format(r['frequency'], r['chanbw']) if r['frequency'] and r['chanbw'] else ''
            clients = ' · {} client(s)'.format(r['clients']) if r['clients'] else ''
            rows.append('<tr><td>{} {}</td><td>{}{}</td><td>{}{}</td><td>{}</td></tr>'.format(
                dot(r['state']), escape(r['name']), escape(str(r['channel'] or '-')), span,
                escape(r['mode'] or '-'), clients, escape(r['uptime'] or '-')))
        body = (f'<h1>{escape(card["name"] or card["code"])} <span class="muted">{card["up"]}/'
                f'{len(card["radios"])} radios up</span></h1>'
                '<table><tr><th>Radio</th><th>Channel</th><th>Mode</th><th>Uptime</th></tr>' + ''.join(rows) + '</table>')
    grafana = (f' · <a href="{escape(config.GRAFANA_URL)}" target="_blank" rel="noopener">Grafana</a>'
               if config.GRAFANA_URL else '')
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ''
    title = card.get('name') or card.get('code')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{escape(str(title))}</title><style>{css}</style></head><body>{body}
<footer class="muted">Updated {escape(card['generated_at'][11:16])} · <a href="{full}" target="_blank" rel="noopener">Details</a>{grafana}</footer>
</body></html>'''

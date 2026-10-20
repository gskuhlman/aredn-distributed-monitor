"""
Antenna aim check: compares each directional antenna's configured azimuth
(and elevation, when antenna heights exist) with the bearings of the RF
neighbours it actually hears, and checks the locations the bearings depend on.

For every located radio with a directional antenna (azimuth set and a beam
narrower than an omni, or an antenna description that says so) it looks at
its RF neighbours (current links plus link history over LOOKBACK_HOURS, with
SNR in each direction), the bearing and distance to each, the strongest peer
and the SNR-weighted bearing of all of them, and flags:

- azimuth not set on a directional antenna (suggests the bearing to its
  strongest peer);
- a dish/panel whose peer sits outside its beam: with a good link the
  setting is probably wrong, with a weak link the antenna may really be
  misaimed;
- a sector whose clients sit outside its coverage, or closer to another
  sector's boresight on the same mast;
- location problems: a link distance the node reports that disagrees with
  the stored coordinates (the node's own location setting is stale), a
  longitude/latitude with the wrong sign, 0,0, and unrelated nodes sharing
  one placeholder position;
- downtilt: set elevation vs. the angle implied by both antenna heights and
  the distance (heights are above ground; terrain is not known).

Never recommends TX power changes. Read-only over SQLite; scoped like the
other reports and by scan boundaries.
"""

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

import channel_map as cm
import database
import link_diagnostics as ld
import mesh_diagnostics
import reports

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2, 'ok': 3}
LOOKBACK_HOURS = 72
AIM_MARGIN_DEG = 10.0       # beyond half the beamwidth before a peer counts as outside the beam
DISH_MAX_DEG = 45.0         # at or below: point-to-point dish/panel; above (and < 360): sector
GOOD_SNR_DB = 20            # a link this good can't be badly misaimed: the setting is wrong
POOR_SNR_DB = 12
DIST_TOLERANCE_M = 200      # reported vs. coordinate link distance
DIST_TOLERANCE_FRAC = 0.5
ELEVATION_MAX_KM = 20
EFFECTIVE_EARTH_M = 6371000 * 4 / 3   # radio horizon (k = 4/3)

VERDICT_LABELS = {
    'ok': 'Aimed at its peers',
    'unset': 'Azimuth not set',
    'setting_wrong': 'Azimuth setting looks wrong',
    'misaim': 'Possible misaim',
    'off_beam': 'Peer near the beam edge',
    'outside_sector': 'Clients outside the sector',
    'sector_edge': 'Client between sectors',
    'no_peers': 'No RF peers to compare',
}


def _add(findings, severity, category, title, detail, nodes=(), site=None,
         evidence=None, recommendation=None):
    findings.append({'severity': severity, 'category': category, 'title': title,
                     'detail': detail, 'nodes': list(nodes), 'site': site,
                     'evidence': evidence or [], 'recommendation': recommendation})


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _deg(v):
    return f'{v:.0f}°' if v is not None else '-'


def _km(m):
    return f'{m / 1000:.2f} km' if m >= 1000 else f'{m:.0f} m'


def _kind(r):
    """'dish' (point-to-point), 'sector', or None for an omni / unknown beam."""
    bw = r.get('beamwidth') or 360
    if bw >= 360 or (r.get('azimuth') is None and r.get('beamwidth_assumed')):
        return None
    return 'dish' if bw <= DISH_MAX_DEG else 'sector'


def _circular_mean(pairs):
    """Weighted mean bearing of [(bearing, weight)]."""
    x = sum(w * math.cos(math.radians(b)) for b, w in pairs)
    y = sum(w * math.sin(math.radians(b)) for b, w in pairs)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def _expected_elevation(h_self, h_peer, dist_m):
    """Elevation angle to the peer's antenna from both heights (flat ground,
    earth curvature with k = 4/3)."""
    drop = dist_m ** 2 / (2 * EFFECTIVE_EARTH_M)
    return math.degrees(math.atan2(h_peer - h_self - drop, dist_m))


def _history(conn, start):
    """{(a, b): {'snr', 'quality', 'samples'}}: what a hears from b (medians)."""
    acc = defaultdict(lambda: {'snr': [], 'quality': []})
    for src, tgt, snr, q in conn.execute('''
        SELECT source_node, target_node, snr, quality FROM link_history
        WHERE UPPER(link_type) = 'RF' AND sample_type = 'scan' AND timestamp >= ?
          AND target_node NOT LIKE 'mac-%'
    ''', (start,)):
        a = acc[(src, tgt)]
        if snr is not None and snr > 0:
            a['snr'].append(snr)
        if q is not None:
            a['quality'].append(q)
    return {k: {'snr': median(v['snr']) if v['snr'] else None,
                'quality': median(v['quality']) if v['quality'] else None,
                'samples': max(len(v['snr']), len(v['quality']))} for k, v in acc.items()}


def _links(conn):
    """{(src, tgt): row} for RF links that aren't removed (src's view)."""
    out = {}
    for r in conn.execute('''
        SELECT source_node, target_node, snr, rev_snr, quality, distance, status, last_seen
        FROM links WHERE UPPER(link_type) = 'RF' AND status != 'removed'
          AND target_node NOT LIKE 'mac-%'
    '''):
        out[(r['source_node'], r['target_node'])] = dict(r)
    return out


def _peers(name, radios, links, history):
    """RF peers of a radio with bearing, distance and SNR in each direction."""
    me = radios[name]
    names = {b for a, b in links if a == name} | {a for a, b in links if b == name} \
        | {b for a, b in history if a == name} | {a for a, b in history if b == name}
    out = []
    for peer in sorted(names):
        p = radios.get(peer)
        if not p or p['lat'] is None or me['lat'] is None:
            continue
        d = ld._haversine_m(me['lat'], me['lon'], p['lat'], p['lon'])
        fwd, rev = links.get((name, peer)) or {}, links.get((peer, name)) or {}
        h_me, h_peer = history.get((name, peer)) or {}, history.get((peer, name)) or {}
        heard = h_me.get('snr') or _num(fwd.get('snr')) or _num(rev.get('rev_snr'))
        heard_by_peer = h_peer.get('snr') or _num(rev.get('snr')) or _num(fwd.get('rev_snr'))
        snrs = [s for s in (heard, heard_by_peer) if s is not None]
        out.append({
            'name': peer, 'distance_m': round(d) if d is not None else None,
            'bearing': round(ld._bearing(me['lat'], me['lon'], p['lat'], p['lon']), 1) if d else None,
            'colocated': d is not None and d <= cm.SITE_METERS,
            'snr_heard': heard, 'snr_peer': heard_by_peer,
            'link_snr': max(snrs) if snrs else None, 'weak_snr': min(snrs) if snrs else None,
            'quality': h_me.get('quality') or _num(fwd.get('quality')),
            'current': bool(fwd or rev),
            'reported_m': _num(fwd.get('distance')), 'peer_reported_m': _num(rev.get('distance')),
            'is_infrastructure': p.get('is_infrastructure'),
        })
    return out


def _check_radio(r, peers, radios, ctx, findings):
    """Aim verdict and findings for one directional radio; returns the row."""
    name, kind, bw, az = r['name'], _kind(r), r['beamwidth'], r['azimuth']
    half = bw / 2 + AIM_MARGIN_DEG
    far = [p for p in peers if not p['colocated'] and p['bearing'] is not None]
    for p in far:
        p['offset'] = round(ld._angle_diff(p['bearing'], az), 1) if az is not None else None
        p['in_beam'] = p['offset'] is None or p['offset'] <= half
    ranked = sorted(far, key=lambda p: (p['link_snr'] is None, -(p['link_snr'] or 0), -(p['quality'] or 0)))
    main = ranked[0] if ranked else None
    best = _circular_mean([(p['bearing'], max(p['link_snr'] or 1, 1)) for p in far]) if far else None
    row = {
        'name': name, 'kind': kind, 'is_infrastructure': r.get('is_infrastructure'), 'site_code': r.get('site_code'),
        'antenna': r.get('antenna'), 'beamwidth': bw, 'beamwidth_assumed': r.get('beamwidth_assumed'),
        'azimuth': az, 'elevation': r.get('elevation'), 'height': r.get('height'),
        'peers': far, 'colocated_peers': [p['name'] for p in peers if p['colocated']],
        'main_peer': main['name'] if main else None, 'main_bearing': main['bearing'] if main else None,
        'main_snr': main['link_snr'] if main else None,
        'best_bearing': round(best, 1) if best is not None else None,
        'offset': None, 'verdict': 'ok', 'suggested_azimuth': None,
    }
    infra = ' (infrastructure)' if r.get('is_infrastructure') else ''
    site = r.get('site_code')

    if az is None:
        row['verdict'] = 'unset'
        if main:
            row['suggested_azimuth'] = round(main['bearing'])
        sev = 'warning' if (r.get('is_infrastructure') or kind == 'dish') and main else 'info'
        _add(findings, sev, 'azimuth_unset', f'{name}: azimuth not set{infra}',
             f"{r.get('antenna') or 'Directional antenna'} ({bw:.0f}° beam) has no azimuth, so the Channel Map, "
             'channel planner and conflict checks treat it as an omni.',
             [name], site,
             [f"{p['name']}: bearing {_deg(p['bearing'])}, {_km(p['distance_m'])}, SNR {p['link_snr'] or '?'} dB"
              for p in ranked[:4]],
             f"Set the azimuth to {row['suggested_azimuth']}° (bearing to {main['name']}, its strongest RF peer) "
             'in the node\'s location/antenna settings.' if main else
             'Set the azimuth in the node\'s location/antenna settings.')
        return row
    if not far:
        row['verdict'] = 'no_peers'
        return row

    if kind == 'dish':
        off = main['offset']
        row['offset'] = off
        snr = main['link_snr']
        if off > half:
            row['suggested_azimuth'] = round(main['bearing'])
            ev = [f"Set azimuth {_deg(az)}, bearing to {main['name']} {_deg(main['bearing'])} "
                  f"({off:.0f}° off, beam ±{bw / 2:.0f}°), {_km(main['distance_m'])}",
                  f"SNR: {name} hears {main['snr_heard'] or '?'} dB, {main['name']} hears {main['snr_peer'] or '?'} dB"]
            if snr is not None and snr >= GOOD_SNR_DB:
                row['verdict'] = 'setting_wrong'
                _add(findings, 'warning', 'azimuth_setting', f'{name}: azimuth setting looks wrong{infra}',
                     f"Its RF link to {main['name']} is good, but {main['name']} is {off:.0f}° outside where the "
                     'azimuth says the antenna points. A link this strong means the antenna really faces it: the '
                     'stored azimuth is probably wrong, not the aim.', [name, main['name']], site, ev,
                     f"Change the azimuth setting to about {row['suggested_azimuth']}°.")
            elif snr is not None and snr < POOR_SNR_DB:
                row['verdict'] = 'misaim'
                _add(findings, 'critical' if r.get('is_infrastructure') else 'warning', 'misaim',
                     f'{name}: peer outside the beam and a weak link{infra}',
                     f"{main['name']} is {off:.0f}° off the antenna's azimuth and the link is weak "
                     f"(SNR {snr} dB): the antenna may be physically misaimed.", [name, main['name']], site, ev,
                     f"Check the physical aim toward {row['suggested_azimuth']}° and that the azimuth setting matches.")
            else:
                row['verdict'] = 'off_beam'
                _add(findings, 'info', 'azimuth_setting', f'{name}: peer outside the set beam',
                     f"{main['name']} is {off:.0f}° off the azimuth setting; the link is fair (SNR {snr or '?'} dB).",
                     [name, main['name']], site, ev,
                     f"Check the azimuth setting (about {row['suggested_azimuth']}°) and the physical aim.")
        elif snr is not None and snr < POOR_SNR_DB and main['distance_m'] and main['distance_m'] < 5000:
            row['verdict'] = 'off_beam'
            _add(findings, 'info', 'weak_in_beam', f'{name}: weak link with the peer inside the beam',
                 f"{main['name']} is inside the beam ({off:.0f}° off) but SNR is only {snr} dB at "
                 f"{_km(main['distance_m'])}: check fine aim, obstructions and the far end's aim.",
                 [name, main['name']], site)
        _check_elevation(r, main, row, findings)
        return row

    # Sector: every RF client should sit inside its coverage, and nearer its
    # boresight than any other sector's on the same mast.
    outside, edges = [], []
    mates = [m for m in ctx['mast_mates'].get(name, ()) if _kind(radios[m]) == 'sector'
             and radios[m]['azimuth'] is not None]
    for p in far:
        others = [(ld._angle_diff(p['bearing'], radios[m]['azimuth']), m) for m in mates]
        closer = min(others) if others else None
        if not p['in_beam']:
            outside.append((p, closer))
        elif closer and closer[0] < p['offset']:
            edges.append((p, closer))
    if best is not None:
        row['offset'] = round(ld._angle_diff(best, az), 1)
    if outside:
        row['verdict'] = 'outside_sector'
        ev = []
        for p, closer in outside:
            line = (f"{p['name']}: bearing {_deg(p['bearing'])}, {p['offset']:.0f}° off (coverage ±{bw / 2:.0f}°), "
                    f"SNR {p['link_snr'] or '?'} dB")
            if closer:
                line += f"; {closer[0]:.0f}° off {closer[1]}"
            ev.append(line)
        good = all((p['link_snr'] or 0) >= GOOD_SNR_DB for p, _ in outside)
        _add(findings, 'warning' if not good else 'info', 'sector_coverage',
             f'{name}: {len(outside)} client(s) outside the sector{infra}',
             'RF clients sit outside the coverage the azimuth setting describes. '
             + ('Their links are good, so the azimuth setting may be off, or they are served by a side lobe.'
                if good else 'Weak links outside the beam are served by side lobes.'),
             [name] + [p['name'] for p, _ in outside], site, ev,
             'Check the sector azimuth setting; a client nearer another sector\'s boresight may do better linked '
             'to that sector (keep its channel plan in mind).')
    if edges:
        if row['verdict'] == 'ok':
            row['verdict'] = 'sector_edge'
        _add(findings, 'info', 'sector_edge', f'{name}: client(s) nearer another sector\'s boresight',
             'These clients link to this sector but sit closer to the boresight of another sector on the same '
             'mast, at the edge between the two. Fine while both work; if those sectors share a channel the '
             'client hears both.', [name] + [p['name'] for p, _ in edges], site,
             [f"{p['name']}: {p['offset']:.0f}° off {name}, {c[0]:.0f}° off {c[1]}" for p, c in edges])
    if row['offset'] is not None and row['offset'] > bw / 2 and len(far) > 1 and not outside:
        _add(findings, 'info', 'sector_aim', f'{name}: clients cluster away from the azimuth',
             f"The SNR-weighted bearing of its clients is {_deg(best)}, {row['offset']:.0f}° from the set "
             f'{_deg(az)}.', [name], site)
    return row


def _check_elevation(r, peer, row, findings):
    el, h, h_peer = r.get('elevation'), r.get('height'), peer.get('height')
    if el is None or h is None or h_peer is None or not peer['distance_m'] \
            or peer['distance_m'] > ELEVATION_MAX_KM * 1000:
        return
    expected = _expected_elevation(h, h_peer, peer['distance_m'])
    row['expected_elevation'] = round(expected, 1)
    limit = max(r['beamwidth'] / 2, 3.0)
    if abs(el - expected) > limit:
        _add(findings, 'info', 'downtilt', f"{r['name']}: elevation {el:g}° vs. about {expected:.1f}° expected",
             f"From the antenna heights ({h:g} m here, {h_peer:g} m at {peer['name']}) and "
             f"{_km(peer['distance_m'])}, the antenna should tilt about {expected:.1f}°; the setting says {el:g}°. "
             'Heights are above ground and terrain is not included, so treat this as a hint.',
             [r['name'], peer['name']], r.get('site_code'),
             recommendation='Check the elevation setting (and the physical tilt if the link is weak).')


def _check_locations(radios, names, links, findings):
    """Coordinates the bearings depend on: wrong signs, 0,0, shared
    placeholders, and link distances that disagree with the coordinates."""
    located = {n: radios[n] for n in names if radios.get(n) and radios[n]['lat'] is not None}
    if not located:
        return {}
    everyone = [r for r in radios.values() if r['lat'] is not None]
    mlat = median(r['lat'] for r in everyone)
    mlon = median(r['lon'] for r in everyone)
    bad = {}
    for n, r in sorted(located.items()):
        lat, lon = r['lat'], r['lon']
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            bad[n] = 'zero'
            _add(findings, 'warning', 'location', f'{n}: location is 0,0',
                 'The node has no real location set; bearings, distances and the map are meaningless for it.',
                 [n], recommendation='Set the node\'s location (Node setup → Location).')
        elif (lon > 0) != (mlon > 0) and abs(abs(lon) - abs(mlon)) < 30:
            bad[n] = 'lon_sign'
            _add(findings, 'warning', 'location', f'{n}: longitude sign looks wrong',
                 f'Longitude {lon:g} is on the other side of the globe from the rest of the mesh ({mlon:.2f}); '
                 f'it is probably {-lon:g}.', [n],
                 recommendation=f'Set the longitude to {-lon:g} in the node\'s location settings.')
        elif (lat > 0) != (mlat > 0) and abs(abs(lat) - abs(mlat)) < 30:
            bad[n] = 'lat_sign'
            _add(findings, 'warning', 'location', f'{n}: latitude sign looks wrong',
                 f'Latitude {lat:g} is in the other hemisphere from the rest of the mesh; it is probably {-lat:g}.',
                 [n], recommendation=f'Set the latitude to {-lat:g}.')

    # Unrelated nodes (different callsigns, no RF link) at one exact position.
    spots = defaultdict(list)
    for n, r in located.items():
        if n not in bad:
            spots[(round(r['lat'], 5), round(r['lon'], 5))].append(n)
    linked = {tuple(sorted(k)) for k in links}
    for (lat, lon), members in spots.items():
        calls = {m.split('-')[0] for m in members}
        if len(calls) > 1 and not any(tuple(sorted((a, b))) in linked
                                      for i, a in enumerate(members) for b in members[i + 1:]):
            _add(findings, 'info', 'location', f'{len(members)} unrelated nodes share one position',
                 f'{", ".join(sorted(members))} all sit at {lat}, {lon}. That is usually a default or copied '
                 'location, so their bearings and distances are not real.', sorted(members),
                 recommendation='Set each node\'s own location.')

    # Distance a node reports for a link (its LQM uses its own location
    # setting) vs. the stored coordinates of both ends.
    def close(rep, coord):
        return abs(rep - coord) <= max(DIST_TOLERANCE_M, DIST_TOLERANCE_FRAC * max(rep, coord))

    mismatches = defaultdict(list)   # src -> [(tgt, reported, coord, peer's report, peer agrees)]
    for (src, tgt), row in links.items():
        a, b = radios.get(src), radios.get(tgt)
        rep = _num(row.get('distance'))
        if not a or not b or a['lat'] is None or b['lat'] is None or rep is None \
                or (src not in located and tgt not in located):
            continue
        coord = ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])
        if close(rep, coord):
            continue
        back = _num((links.get((tgt, src)) or {}).get('distance'))
        mismatches[src].append((tgt, rep, coord, back, back is not None and close(back, coord)))
    reported = set()
    for src, items in sorted(mismatches.items()):
        evidence = [f'{src} → {t}: reported {_km(rep)}, coordinates {_km(c)}'
                    + (f', {t} reports {_km(back)}' if back is not None else '') for t, rep, c, back, _ in items]
        confirmed = [x for x in items if x[4]]
        if confirmed:
            # The far end agrees with the coordinates: src's own position (as
            # its link-quality manager sees it) is the odd one out.
            t, rep, c, back, _ = confirmed[0]
            _add(findings, 'warning', 'location', f'{src}: its own location setting looks stale',
                 f'{src} reports {_km(rep)} to {t}, but the coordinates (and {t}\'s own report, {_km(back)}) say '
                 f'{_km(c)}. {src}\'s link-quality manager is working from a different position than the one it '
                 'publishes' + ('; its other mismatched link distances follow from that.'
                                if len(items) > 1 else '.'),
                 [src], radios[src].get('site_code'), evidence,
                 f'Re-save {src}\'s location (Node setup → Location) and check it; reboot if it persists.')
            bad.setdefault(src, 'distance')
            continue
        for t, rep, c, back, _ in items:
            pair = tuple(sorted((src, t)))
            if pair in reported or t in mismatches and any(x[4] for x in mismatches[t]):
                continue
            reported.add(pair)
            _add(findings, 'warning', 'location', f'{src} ↔ {t}: link distance disagrees with the coordinates',
                 f'{src} reports {_km(rep)}' + (f', {t} reports {_km(back)}' if back is not None else '')
                 + f', but the stored coordinates are {_km(c)} apart. One end\'s location is wrong.',
                 [src, t], radios[src].get('site_code'),
                 [e for e in evidence if f'→ {t}:' in e], 'Check both nodes\' location settings.')
            bad.setdefault(src, 'distance')
    return bad


def run_report(scope='all', node=None):
    """Aim report for the in-scope radios, or for one radio (node=name,
    ignoring scope) for the node page."""
    now = datetime.now()
    start = (now - timedelta(hours=LOOKBACK_HOURS)).strftime(ld.TS_FMT)
    with database.get_connection() as conn:
        radios, _ = cm._load(conn)
        links = _links(conn)
        history = _history(conn, start)
        if node:
            names = {node.lower()} & set(radios)
        else:
            nodes = database.get_all_nodes()
            boundaries = database.get_scan_boundaries()
            current = [l for l in database.get_active_links() if l.get('status') == 'good']
            _, in_scope = mesh_diagnostics.compute_scope(nodes, current, boundaries)
            allowed = reports.resolve_scope(conn, scope or 'all')
            names = (in_scope if allowed is None else in_scope & allowed) & set(radios)

    # Directional radios on the same mast, for the sector-edge check.
    located = [n for n, r in radios.items() if r['lat'] is not None]
    mast_mates = defaultdict(list)
    for i, a in enumerate(located):
        for b in located[i + 1:]:
            d = ld._haversine_m(radios[a]['lat'], radios[a]['lon'], radios[b]['lat'], radios[b]['lon'])
            if d is not None and d <= cm.SITE_METERS:
                mast_mates[a].append(b)
                mast_mates[b].append(a)
    ctx = {'mast_mates': mast_mates}

    findings = []
    bad_location = _check_locations(radios, names, links, findings)
    rows = []
    for name in sorted(names):
        r = radios[name]
        if r['lat'] is None or not _kind(r):
            continue
        peers = _peers(name, radios, links, history)
        for p in peers:
            p['height'] = radios[p['name']].get('height')
        row = _check_radio(r, peers, radios, ctx, findings)
        row['location_problem'] = bad_location.get(name)
        row['verdict_label'] = VERDICT_LABELS[row['verdict']]
        rows.append(row)
    if node:
        findings = [f for f in findings if node.lower() in f['nodes']]
    findings.sort(key=lambda x: SEVERITY_ORDER[x['severity']])
    verdicts = Counter(r['verdict'] for r in rows)
    return {
        'generated_at': now.strftime(ld.TS_FMT),
        'scope': scope, 'node': node, 'lookback_hours': LOOKBACK_HOURS,
        'directional_count': len(rows),
        'counts': dict(Counter(f['severity'] for f in findings)),
        'verdicts': dict(verdicts),
        'findings': findings,
        'radios': rows,
        'notes': ['Bearings come from stored coordinates; a wrong location makes the aim look wrong too.',
                  'Elevation hints use antenna heights above ground only (terrain not included).'],
    }


def radio_aim(name):
    """Compact aim summary for one radio (node page / Nodes detail panel), or
    None for an omni or unlocated radio."""
    rep = run_report(node=name)
    row = rep['radios'][0] if rep['radios'] else None
    if not row and not rep['findings']:
        return None
    out = {'findings': rep['findings']}
    if row:
        out.update({k: row.get(k) for k in ('kind', 'azimuth', 'beamwidth', 'beamwidth_assumed', 'main_peer',
                                           'main_bearing', 'main_snr', 'best_bearing', 'offset', 'verdict',
                                           'verdict_label', 'suggested_azimuth', 'expected_elevation',
                                           'elevation', 'location_problem')})
    return out

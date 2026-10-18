"""
Infrastructure review: audit the radio settings of every infrastructure node.

Uses the radio configuration the scanner stores from each node's sysinfo
(mode, SSID, channel, width, antenna azimuth/beamwidth/height, LQM user
blocks), optionally refreshed live first, and checks:

- channel plans at each site (overlapping / adjacent channels on one mast)
- overlapping radios inside a directional antenna's beam at other sites
- azimuth settings vs. where each radio's RF neighbors actually are
- PtP/PtMP pairing (one AP, one station, matching SSID and width)
- firmware consistency, missing antenna data, RF off, user blocks
- reachability and near-far at infrastructure receivers
- a link-diagnosis verdict for every RF link that touches infrastructure

Read-only except that a refresh stores the fetched radio settings.
"""

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

import config
import database
import gateway
import link_diagnostics as ld

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2, 'ok': 3}
SITE_METERS = 150
BEAM_SEARCH_KM = 20.0
GUARD_MHZ = 10          # clear spectrum wanted between colocated radios
NEAR_GUARD_MHZ = 20
RELEASE_FW = re.compile(r'^\d+\.\d+\.\d+\.\d+$')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _add(findings, severity, category, title, detail, nodes=(), site=None,
         evidence=None, recommendation=None):
    findings.append({'severity': severity, 'category': category, 'title': title,
                     'detail': detail, 'nodes': list(nodes), 'site': site,
                     'evidence': evidence or [], 'recommendation': recommendation})


def _span(r):
    """Occupied spectrum (lo, hi) in MHz."""
    f, bw = r.get('frequency'), r.get('chanbw') or 20
    return (f - bw / 2, f + bw / 2) if f else None


def spectral_gap(a, b):
    """MHz of clear spectrum between two radios' channels (<0 = overlap)."""
    sa, sb = _span(a), _span(b)
    if not sa or not sb:
        return None
    return round(max(sb[0] - sa[1], sa[0] - sb[1]), 1)


def pair_relation(a, b):
    """Relation between two radios' channels. Same centre frequency with the
    same (or an unknown) width is a shared channel, not interference."""
    if a.get('frequency') and a.get('frequency') == b.get('frequency') \
            and (not a.get('chanbw') or not b.get('chanbw') or a['chanbw'] == b['chanbw']):
        return 'same'
    return gap_relation(spectral_gap(a, b))


def gap_relation(gap):
    if gap is None:
        return 'unknown'
    if gap < 0:
        return 'overlap'
    if gap < GUARD_MHZ:
        return 'adjacent'
    if gap < NEAR_GUARD_MHZ:
        return 'near'
    return 'clear'


def freq_to_channel(freq):
    """AREDN channel number for a centre/edge frequency in MHz (None if unknown band)."""
    if freq is None:
        return None
    if 2300 <= freq < 2500:
        ch = (freq - 2407) / 5
    elif 3300 <= freq < 3700 or 5000 <= freq < 6000:
        ch = (freq - (3000 if freq < 4000 else 5000)) / 5
    else:
        return None
    return int(ch) if ch == int(ch) else round(ch, 1)


def rf_span_info(node):
    """A node's channel as centre/width/low/high frequency and channel numbers."""
    f = _num(node.get('rf_frequency'))
    if not f:
        return None
    bw = _num(node.get('rf_chanbw'))
    lo, hi = _span({'frequency': f, 'chanbw': bw})
    return {'frequency': f, 'chanbw': bw, 'width_assumed': not bw, 'low': lo, 'high': hi,
            'channel': node.get('rf_channel') or freq_to_channel(f),
            'low_channel': freq_to_channel(lo), 'high_channel': freq_to_channel(hi),
            'rf_status': node.get('rf_status')}


def annotate_link_rf(node_name, links, node=None):
    """Add rf_span to each link: the peer's channel and how it relates to this node's."""
    node = node or database.get_node(node_name) or {}
    mine = rf_span_info(node)
    peers = {}
    for link in links:
        peer = link['target_node'] if link['source_node'] == node_name else link['source_node']
        if peer not in peers:
            peers[peer] = rf_span_info(database.get_node(peer) or {})
        info = dict(peers[peer]) if peers[peer] else None
        if info and mine:
            a = {'frequency': mine['frequency'], 'chanbw': mine['chanbw']}
            b = {'frequency': info['frequency'], 'chanbw': info['chanbw']}
            info['relation'] = pair_relation(a, b)
            info['gap_mhz'] = spectral_gap(a, b)
        link['peer_rf'] = info
    return mine


# ------------------------------------------------------------- refresh ----

def refresh_radio_configs(names):
    """Fetch sysinfo from each node and store its radio settings."""
    if not gateway.is_reachable():
        return {'error': gateway.unreachable_message()}
    ips = {n['name']: n.get('ip') for n in (database.get_node(x) for x in names) if n}

    def fetch(name):
        ip = ips.get(name)
        if not ip:
            return name, None, 'no IP address'
        try:
            resp = requests.get(f'http://{ip}/cgi-bin/sysinfo.json?lqm=1', timeout=config.REQUEST_TIMEOUT * 2)
            resp.raise_for_status()
            return name, resp.json(), None
        except (requests.RequestException, ValueError) as exc:
            return name, None, str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, names))
    errors = {}
    for name, data, err in results:
        if data:
            database.set_node_radio_config(name, database.radio_config_from_sysinfo(data))
        else:
            errors[name] = err
    if errors and gateway.failure_is_ours():
        return {'error': gateway.unreachable_message()}
    return {'refreshed': len(names) - len(errors), 'errors': errors}


# --------------------------------------------------------------- gather ---

def _radios(conn):
    rows = conn.execute('SELECT * FROM nodes').fetchall()
    out = {}
    for r in rows:
        r = dict(r)
        out[r['name']] = {
            'name': r['name'], 'ip': r.get('ip'), 'model': r.get('model'),
            'firmware': r.get('firmware_version'), 'is_active': r.get('is_active'),
            'last_seen': r.get('last_seen'), 'lat': r.get('lat'), 'lon': r.get('lon'),
            'frequency': _num(r.get('rf_frequency')), 'channel': r.get('rf_channel'),
            'chanbw': r.get('rf_chanbw'), 'rf_status': r.get('rf_status'), 'mode': r.get('rf_mode'),
            'ssid': r.get('rf_ssid'), 'azimuth': r.get('antenna_azimuth'),
            'elevation': r.get('antenna_elevation'), 'height': r.get('antenna_height'),
            'gain': r.get('antenna_gain'), 'beamwidth': r.get('antenna_beamwidth'),
            'antenna': r.get('antenna_desc'), 'user_blocks': r.get('lqm_user_blocks'),
            'config_at': r.get('radio_config_at'), 'description': r.get('description'),
        }
    return out


def _rf_on(r):
    return r.get('frequency') and (r.get('rf_status') in (None, 'on'))


def _dist(a, b):
    if None in (a.get('lat'), a.get('lon'), b.get('lat'), b.get('lon')):
        return None
    return ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])


def _role(mode):
    return ld.RF_MODE_ROLES.get((mode or '').lower())


# --------------------------------------------------------------- checks ---

def _check_sites(ctx, f):
    """Channel plan at each infrastructure site, using real channel widths."""
    for code, members in ctx['sites'].items():
        site = ctx['site_names'].get(code, code)
        radios = [r for r in members if _rf_on(r)]
        # Other radios physically at the site (not marked infrastructure).
        anchor = next((r for r in members if r.get('lat') is not None), None)
        if anchor:
            for r in ctx['radios'].values():
                if r['name'] not in ctx['infra'] and _rf_on(r) and (_dist(anchor, r) or 1e9) <= SITE_METERS:
                    radios.append(r)
        pairs = []
        for i, a in enumerate(radios):
            for b in radios[i + 1:]:
                gap = spectral_gap(a, b)
                rel = pair_relation(a, b)
                pairs.append({'a': a['name'], 'b': b['name'], 'gap_mhz': gap, 'relation': rel})
                if rel not in ('overlap', 'adjacent'):
                    continue  # 'same' channel radios share airtime on purpose
                apart = ''
                if a.get('azimuth') is not None and b.get('azimuth') is not None \
                        and (a.get('beamwidth') or 360) < 360 and (b.get('beamwidth') or 360) < 360:
                    apart = f" They point {ld._angle_diff(a['azimuth'], b['azimuth']):.0f}° apart."
                sev = 'critical' if rel == 'overlap' else 'warning'
                _add(f, sev, 'channel_plan',
                     f"{a['name']} and {b['name']} {'overlap' if rel == 'overlap' else 'have no guard band'} at {site}",
                     f"{a['name']} occupies {_span(a)[0]:.0f}–{_span(a)[1]:.0f} MHz and {b['name']} "
                     f"{_span(b)[0]:.0f}–{_span(b)[1]:.0f} MHz: "
                     + (f"{-gap:.0f} MHz overlap." if gap < 0 else f"only {gap:.0f} MHz between them.")
                     + apart + " Radios on one mast this close in frequency desensitize each other's receivers "
                     "whenever either transmits.",
                     [a['name'], b['name']], code,
                     [f"{r['name']}: ch {r.get('channel')} {r['frequency']:.0f} MHz, {r.get('chanbw') or '?'} MHz wide, "
                      f"{r.get('mode') or '?'} '{r.get('ssid') or '?'}'" for r in (a, b)],
                     f"Give colocated radios at least {GUARD_MHZ}–{NEAR_GUARD_MHZ} MHz of clear spectrum "
                     f"between channel edges (move a channel or narrow a width).")
        ctx['site_pairs'][code] = pairs


def _check_beams(ctx, f):
    """Overlapping radios at other sites inside an infrastructure antenna's beam."""
    for name in ctx['infra']:
        a = ctx['radios'].get(name)
        if not a or not _rf_on(a) or a.get('lat') is None:
            continue
        beam = a.get('beamwidth') or 360
        hits = []
        for b in ctx['radios'].values():
            if b['name'] == name or not _rf_on(b) or not b.get('is_active'):
                continue
            d = _dist(a, b)
            if d is None or d <= SITE_METERS or d > BEAM_SEARCH_KM * 1000:
                continue
            gap = spectral_gap(a, b)
            if gap is None or gap >= GUARD_MHZ or a['frequency'] == b['frequency']:
                continue
            if beam < 360:
                brg = ld._bearing(a['lat'], a['lon'], b['lat'], b['lon'])
                if a.get('azimuth') is None or ld._angle_diff(brg, a['azimuth']) > beam / 2 + 5:
                    continue
            toward = None
            if b.get('azimuth') is not None and (b.get('beamwidth') or 360) < 360:
                back = ld._bearing(b['lat'], b['lon'], a['lat'], a['lon'])
                toward = ld._angle_diff(back, b['azimuth']) <= b['beamwidth'] / 2
            hits.append((b, d, gap, toward))
        if not hits:
            continue
        hits.sort(key=lambda h: (h[2], h[1]))
        worst = any(g < 0 and (t is not False) for _, _, g, t in hits)
        _add(f, 'warning' if worst else 'info', 'beam_interference',
             f"{len(hits)} radio(s) on overlapping/adjacent channels in {name}'s "
             + ('coverage' if beam >= 360 else 'beam'),
             f"{name} ({a['frequency']:.0f} MHz, {a.get('chanbw') or '?'} MHz"
             + (f", aimed {a['azimuth']:.0f}° with a {beam:.0f}° beam" if beam < 360 else ', omni')
             + ") can hear these transmitters, which it cannot decode; they raise its noise floor.",
             [name] + [h[0]['name'] for h in hits], ctx['infra'].get(name),
             [f"{b['name']} {b['frequency']:.0f} MHz/{b.get('chanbw') or '20?'} MHz, "
              f"{'overlaps by ' + format(-g, '.0f') if g < 0 else 'only ' + format(g, '.0f') + ' MHz clear'}"
              f", {d / 1000:.1f} km"
              + ('' if t is None else (', aimed at it' if t else ', aimed away'))
              for b, d, g, t in hits[:8]],
             'Coordinate channels between sites that see each other, or narrow widths.')


def _check_pointing(ctx, f):
    for name in ctx['infra']:
        r = ctx['radios'].get(name)
        if not r or not _rf_on(r):
            continue
        beam = r.get('beamwidth')
        if beam and beam < 360 and r.get('azimuth') is None:
            _add(f, 'info', 'settings', f"{name} has no azimuth set",
                 f"A {beam:.0f}° antenna without an azimuth can't be checked for aim or interference.",
                 [name], ctx['infra'].get(name), recommendation='Set the azimuth on the node.')
            continue
        if not beam or beam >= 360 or r.get('lat') is None:
            continue
        outside = []
        for n in ctx['neighbors'].get(name, []):
            if n.get('bearing') is None or (n.get('distance_m') or 0) < ld.MIN_BEAM_LINK_M:
                continue
            off = ld._angle_diff(n['bearing'], r['azimuth'])
            if off > beam / 2:
                outside.append((n, off))
        if outside:
            narrow = beam < 60
            _add(f, 'warning' if narrow else 'info', 'pointing',
                 f"{name}'s azimuth setting doesn't cover {len(outside)} of its neighbors",
                 f"{name} is set to {r['azimuth']:.0f}° with a {beam:.0f}° beam"
                 + (f" ({r['antenna']})" if r.get('antenna') else '') + ". "
                 + ("For a narrow antenna this usually means the azimuth setting is wrong or the antenna "
                    "is mis-aimed." if narrow else
                    "Neighbors outside a sector are reached through its side lobes."),
                 [name] + [n['node'] for n, _ in outside], ctx['infra'].get(name),
                 [f"{n['node']} at {n['bearing']}° ({off:.0f}° off), {n['distance_m'] / 1000:.1f} km, "
                  f"SNR {n['snr'] if n['snr'] is not None else '?'} dB" for n, off in outside])


def _check_pairing(ctx, f):
    """PtP/PtMP radios: their RF partner must be the opposite role with the
    same SSID and width."""
    seen = set()
    for name in ctx['infra']:
        r = ctx['radios'].get(name)
        if not r or _role(r.get('mode')) not in ('station', 'access point'):
            continue
        for n in ctx['neighbors'].get(name, []):
            peer = ctx['radios'].get(n['node'])
            key = tuple(sorted((name, n['node'])))
            if not peer or key in seen:
                continue
            seen.add(key)
            problems = []
            ra, rb = _role(r.get('mode')), _role(peer.get('mode'))
            if rb and ra == rb:
                problems.append(f"both are {ra}s")
            elif rb == 'mesh':
                problems.append(f"{peer['name']} is in mesh mode")
            if r.get('ssid') and peer.get('ssid') and r['ssid'] != peer['ssid']:
                problems.append(f"SSID {r['ssid']} vs {peer['ssid']}")
            if r.get('chanbw') and peer.get('chanbw') and r['chanbw'] != peer['chanbw']:
                problems.append(f"width {r['chanbw']:.0f} vs {peer['chanbw']:.0f} MHz")
            if r.get('frequency') and peer.get('frequency') and r['frequency'] != peer['frequency']:
                problems.append(f"frequency {r['frequency']:.0f} vs {peer['frequency']:.0f} MHz")
            if problems:
                _add(f, 'critical', 'pairing', f"{name} and {peer['name']} are not configured as a pair",
                     '; '.join(problems) + '.', [name, peer['name']], ctx['infra'].get(name),
                     recommendation='One end PtP/PtMP access point, the other its station, same SSID/channel/width.')
            else:
                _add(f, 'ok', 'pairing', f"{name} ↔ {peer['name']} pairing is consistent",
                     f"{ra} / {rb or '?'}, SSID {r.get('ssid')}, {r.get('chanbw') or '?'} MHz.",
                     [name, peer['name']], ctx['infra'].get(name))


def _check_firmware(ctx, f):
    infra = [ctx['radios'][n] for n in ctx['infra'] if n in ctx['radios']]
    versions = {}
    for r in infra:
        if r.get('firmware'):
            versions.setdefault(r['firmware'], []).append(r['name'])
    if len(versions) > 1:
        _add(f, 'warning', 'firmware', f"Infrastructure runs {len(versions)} different firmware versions",
             "Mixed versions make behavior inconsistent across the backbone, especially when nightly "
             "builds change link modes or drivers.",
             [r['name'] for r in infra],
             evidence=[f"{v}: {', '.join(sorted(n))}" for v, n in sorted(versions.items())],
             recommendation='Standardize the infrastructure on one release.')
    nightly = [r['name'] for r in infra if r.get('firmware') and not RELEASE_FW.match(r['firmware'])]
    if nightly:
        _add(f, 'warning', 'firmware', f"{len(nightly)} infrastructure node(s) run nightly builds",
             'Nightly builds are development snapshots; infrastructure is usually safest on a release.',
             nightly, evidence=sorted(nightly),
             recommendation='Move infrastructure to the current release unless a nightly fix is needed.')


def _check_misc(ctx, f):
    stale = timedelta(seconds=ld._stale_after_seconds())
    now = datetime.now()
    for name, site in ctx['infra'].items():
        r = ctx['radios'].get(name)
        if not r:
            _add(f, 'info', 'reachability', f"{name} has only been seen as a neighbor",
                 'It is marked infrastructure but the scanner has never polled it directly (no IP), '
                 'so its settings cannot be reviewed.', [name], site)
            continue
        seen = ld._parse_ts(r.get('last_seen'))
        if not r.get('is_active') or not seen or now - seen > stale * 3:
            _add(f, 'critical', 'reachability', f"{name} is not being reached",
                 f"Last polled {r.get('last_seen') or 'never'}.", [name], site,
                 recommendation='Check power and its links.')
        if r.get('rf_status') == 'off':
            _add(f, 'info', 'settings', f"{name} has its mesh radio off",
                 'Fine for a router/DtD-only node; otherwise the radio is disabled.', [name], site)
        if r.get('lat') is None:
            _add(f, 'info', 'settings', f"{name} has no location set",
                 'Distance, beam and colocation checks need it.', [name], site,
                 recommendation='Set latitude/longitude on the node.')
        if _rf_on(r) and r.get('height') is None:
            _add(f, 'info', 'settings', f"{name} has no antenna height set",
                 'Height helps path planning.', [name], site)
        blocks = [m.strip().lower() for m in (r.get('user_blocks') or '').replace(';', ',').split(',') if m.strip()]
        for mac in blocks:
            who = ctx['mac_names'].get(ld._mac_key(mac))
            _add(f, 'info', 'settings', f"{name} blocks {who or mac}",
                 f"LQM user block on {mac}" + (f" ({who})" if who else '') +
                 '. Intended blocks are fine; a forgotten one silently removes a neighbor.',
                 [name] + ([who] if who else []), site,
                 recommendation='Confirm the block is still wanted.')


def _check_near_far(ctx, f):
    """A client too loud at an infrastructure receiver, crowding out the
    receiver's other neighbors: the client should turn its power down."""
    for name in ctx['infra']:
        nbrs = [n for n in ctx['neighbors'].get(name, []) if n.get('snr') is not None]
        for hot in [n for n in nbrs if n['hot'] and n['node'] not in ctx['infra']]:
            victims = [n for n in nbrs if n is not hot and n['poor'] and hot['snr'] - n['snr'] >= ld.NEAR_FAR_GAP]
            if not victims:
                continue
            cut = max(0, round(hot['snr'] - ld.TARGET_SNR))
            _add(f, 'warning', 'near_far', f"{hot['node']} may be drowning out other nodes at {name}",
                 f"{name} hears {hot['node']} at {hot['snr']:.0f} dB SNR while its other neighbors struggle.",
                 [name, hot['node']] + [v['node'] for v in victims], ctx['infra'].get(name),
                 [f"{v['node']}: SNR {v['snr']:.0f} dB, LQ {v['lq'] if v['lq'] is not None else '?'}%" for v in victims[:6]],
                 f"Ask {hot['node']} to lower its TX power by about {cut} dB; {name} is infrastructure and keeps its power.")


CHECKS = (_check_sites, _check_beams, _check_pointing, _check_pairing, _check_firmware,
          _check_misc, _check_near_far)


# ----------------------------------------------------------------- entry --

def run_review(refresh=False, link_hours=24):
    infra = database.get_infrastructure_nodes()
    refresh_result = refresh_radio_configs(sorted(infra)) if refresh and infra else None
    if refresh_result and refresh_result.get('error'):
        return {'error': refresh_result['error'], 'gateway_unreachable': True}

    now = datetime.now()
    stale_after = ld._stale_after_seconds()
    with database.get_connection() as conn:
        cur = conn.cursor()
        radios = _radios(conn)
        nodes_geo = {n: (r['lat'], r['lon']) for n, r in radios.items() if r.get('lat') is not None}
        neighbors = {n: ld._neighbors(cur, n, nodes_geo, stale_after, now) for n in infra}
        mac_names = {}
        for row in cur.execute('SELECT target_node, mac_address FROM links WHERE mac_address IS NOT NULL'):
            key = ld._mac_key(row[1])
            if key and not row[0].startswith('mac-'):
                mac_names.setdefault(key, row[0])
        rf_links = {tuple(sorted((r[0], r[1]))) for r in cur.execute('''
            SELECT source_node, target_node FROM links
            WHERE UPPER(link_type) = 'RF' AND status != 'removed' AND target_node NOT LIKE 'mac-%'
        ''') if r[0] in infra or r[1] in infra}

    sites = {}
    for name, code in infra.items():
        sites.setdefault(code or '?', []).append(radios.get(name) or {'name': name})
    ctx = {'infra': infra, 'radios': radios, 'neighbors': neighbors, 'mac_names': mac_names,
           'sites': sites, 'site_names': database.get_site_names(), 'site_pairs': {}}
    findings = []
    for check in CHECKS:
        check(ctx, findings)
    findings.sort(key=lambda x: SEVERITY_ORDER[x['severity']])

    links = []
    for a, b in sorted(rf_links):
        try:
            diag = ld.diagnose_pair(a, b, hours=link_hours)
        except Exception as exc:
            links.append({'a': a, 'b': b, 'severity': 'info', 'verdict': f'Diagnosis failed: {exc}'})
            continue
        counts = {}
        for x in diag['findings']:
            counts[x['severity']] = counts.get(x['severity'], 0) + 1
        links.append({'a': a, 'b': b, 'both_infrastructure': a in infra and b in infra,
                      'severity': diag['verdict']['severity'], 'verdict': diag['verdict']['text'],
                      'counts': counts})
    links.sort(key=lambda l: (SEVERITY_ORDER.get(l['severity'], 9), not l['both_infrastructure'], l['a']))

    site_out = []
    for code, members in sorted(sites.items()):
        names = {m['name'] for m in members}
        anchor = next((m for m in members if m.get('lat') is not None), None)
        others = [r for r in radios.values() if r['name'] not in names and anchor and _rf_on(r)
                  and (_dist(anchor, r) or 1e9) <= SITE_METERS]
        site_out.append({
            'code': code, 'name': ctx['site_names'].get(code, code),
            'radios': sorted(members + [dict(r, not_infrastructure=True) for r in others],
                             key=lambda r: (r.get('frequency') or 1e9, r['name'])),
            'pairs': ctx['site_pairs'].get(code, []),
        })

    counts = {}
    for x in findings:
        counts[x['severity']] = counts.get(x['severity'], 0) + 1
    return {
        'generated_at': now.strftime(ld.TS_FMT),
        'refresh': refresh_result,
        'infrastructure_count': len(infra),
        'counts': counts,
        'findings': findings,
        'sites': site_out,
        'links': links,
    }

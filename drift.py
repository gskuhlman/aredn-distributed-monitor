"""
Settings drift report: settings mismatches that cause problems, across each
RF link, within each site, and network-wide.

Uses the radio settings the scanner stores from each node's sysinfo (mode,
SSID, channel, width, LQM user blocks) plus firmware and model, and checks:

- RF links: width, SSID, frequency and mode pairing (access point <-> station
  or mesh <-> mesh), firmware major version and nightly vs. release
- sites (radios within 150 m of each other): mixed firmware, mesh radios with
  a width/SSID different from the site's other mesh radios, LQM user blocks
  that differ between sector radios
- network-wide: firmware versions against the newest release seen, and nodes
  whose settings have never been read

sysinfo does not report LQM distance/SNR thresholds or the radio distance
setting, so those cannot be compared. Read-only over SQLite; scoped like the
other reports and by scan boundaries.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import database
import link_diagnostics as ld
import mesh_diagnostics
import reports

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2, 'ok': 3}
SITE_METERS = 150
RELEASE_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)\.(\d+)$')
NIGHTLY_RE = re.compile(r'^(?:([a-z]+)-)?(\d{8})-[0-9a-f]+$', re.I)
RELEASE_FLOOR = (3, 24)      # releases at or below this are very old regardless
NIGHTLY_OLD_DAYS = 60
NIGHTLY_VERY_OLD_DAYS = 180
CONFIG_FRESH_HOURS = 24      # stored settings older than this may be out of date

FIRMWARE_CLASSES = ('current', 'behind', 'nightly', 'old', 'very_old', 'unknown')


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


def _role(mode):
    return ld.RF_MODE_ROLES.get((mode or '').lower())


def _rf_on(r):
    return bool(r.get('frequency')) and r.get('rf_status') in (None, 'on')


# ------------------------------------------------------------- firmware ---

def parse_firmware(version):
    """('release', (major, year, minor, patch)) | ('nightly', date, branch) | ('unknown',)."""
    v = (version or '').strip()
    m = RELEASE_RE.match(v)
    if m:
        return ('release', tuple(int(x) for x in m.groups()))
    m = NIGHTLY_RE.match(v)
    if m:
        try:
            return ('nightly', datetime.strptime(m.group(2), '%Y%m%d'), (m.group(1) or '').lower())
        except ValueError:
            pass
    return ('unknown',)


def classify_firmware(version, latest, now):
    """Firmware class and a short reason, against the newest release seen."""
    parsed = parse_firmware(version)
    if parsed[0] == 'release':
        v = parsed[1]
        if v[:2] <= RELEASE_FLOOR:
            return 'very_old', f"{v[0]}.{v[1]} release"
        if not latest or v >= latest:
            return 'current', 'newest release seen'
        if v[0] < latest[0] - 1 or v[1] < latest[1] - 1:
            return 'very_old', f"two or more releases behind {'.'.join(map(str, latest))}"
        if v[0] < latest[0] or v[1] < latest[1]:
            return 'old', f"previous release line ({latest[0]}.{latest[1]} is current)"
        return 'behind', f"older {latest[0]}.{latest[1]} point release"
    if parsed[0] == 'nightly':
        age = (now - parsed[1]).days
        if parsed[2]:
            return 'very_old', f"{parsed[2]} branch nightly from {parsed[1]:%Y-%m-%d}"
        if age > NIGHTLY_VERY_OLD_DAYS:
            return 'very_old', f"nightly {age} days old"
        if age > NIGHTLY_OLD_DAYS:
            return 'old', f"nightly {age} days old"
        return 'nightly', f"nightly from {parsed[1]:%Y-%m-%d}"
    return 'unknown', 'version not recognized' if version else 'not reported'


def _major(version):
    p = parse_firmware(version)
    return p[1][0] if p[0] == 'release' else None


# --------------------------------------------------------------- gather ---

def _radios(conn, names):
    out = {}
    for r in conn.execute('SELECT * FROM nodes'):
        r = dict(r)
        if r['name'] not in names:
            continue
        out[r['name']] = {
            'name': r['name'], 'model': r.get('model'), 'firmware': r.get('firmware_version'),
            'is_active': r.get('is_active'), 'last_seen': r.get('last_seen'),
            'lat': r.get('lat'), 'lon': r.get('lon'),
            'frequency': _num(r.get('rf_frequency')),
            'channel': r.get('rf_channel'), 'chanbw': r.get('rf_chanbw'), 'rf_status': r.get('rf_status'),
            'mode': r.get('rf_mode'), 'ssid': r.get('rf_ssid'),
            'user_blocks': (r.get('lqm_user_blocks') or '').strip() or None,
            'config_at': r.get('radio_config_at'),
        }
    return out


def _config_fresh(r, now):
    ts = ld._parse_ts(r.get('config_at'))
    return bool(ts and now - ts <= timedelta(hours=CONFIG_FRESH_HOURS))


def _sites(radios, infra, site_names):
    """Cluster located radios within SITE_METERS of each other."""
    located = [r for r in radios.values() if r.get('lat') is not None and r.get('lon') is not None]
    parent = {r['name']: r['name'] for r in located}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(located):
        for b in located[i + 1:]:
            if ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon']) <= SITE_METERS:
                parent[find(a['name'])] = find(b['name'])
    groups = defaultdict(list)
    for r in located:
        groups[find(r['name'])].append(r)

    sites = []
    for members in groups.values():
        if len(members) < 2:
            continue
        names = sorted(m['name'] for m in members)
        codes = Counter(infra[n] for n in names if infra.get(n))
        if codes:
            code = codes.most_common(1)[0][0]
            label = site_names.get(code, code)
        else:
            prefix = names[0]
            for n in names[1:]:
                while not n.startswith(prefix):
                    prefix = prefix[:-1]
            # Cut back to a whole name part (kd0cgn-prt0 -> kd0cgn).
            if prefix not in names and not prefix.endswith('-'):
                prefix = prefix[:prefix.rfind('-') + 1]
            prefix = prefix.rstrip('-')
            code = None
            label = prefix if len(prefix) >= 4 else f"near {names[0]}"
        sites.append({'code': code, 'name': label, 'members': sorted(members, key=lambda r: r['name'])})
    sites.sort(key=lambda s: (s['code'] is None, s['name']))
    return sites


def _band(r):
    f = r.get('frequency') or 0
    return '2.4 GHz' if f < 3000 else '3.4 GHz' if f < 4000 else '5 GHz'


def _majority(values):
    """Most common value when it covers at least two radios, else None."""
    counts = Counter(v for v in values if v not in (None, ''))
    if not counts:
        return None
    value, n = counts.most_common(1)[0]
    return value if n >= 2 else None


# --------------------------------------------------------------- checks ---

def _check_links(ctx, f):
    rows = []
    for a, b, distance in ctx['rf_pairs']:
        ra, rb = ctx['radios'].get(a), ctx['radios'].get(b)
        row = {'a': a, 'b': b, 'distance_km': round(distance / 1000, 2) if distance else None,
               'ends': [ctx['summary'](ra, a), ctx['summary'](rb, b)], 'problems': [], 'severity': 'ok'}
        rows.append(row)
        if not ra or not rb or not ra.get('config_at') or not rb.get('config_at'):
            row['severity'] = 'info'
            row['problems'].append('settings of one end were never read')
            continue
        hard, soft = [], []
        role_a, role_b = _role(ra.get('mode')), _role(rb.get('mode'))
        if role_a and role_b:
            pair = {role_a, role_b}
            if role_a == role_b and role_a != 'mesh':
                hard.append(f"both ends are {role_a}s")
            elif 'mesh' in pair and len(pair) > 1:
                hard.append(f"mode {ra.get('mode')} vs {rb.get('mode')} (mesh with a PtP/PtMP end)")
        if ra.get('ssid') and rb.get('ssid') and ra['ssid'] != rb['ssid']:
            hard.append(f"SSID {ra['ssid']} vs {rb['ssid']}")
        if ra.get('chanbw') and rb.get('chanbw') and ra['chanbw'] != rb['chanbw']:
            hard.append(f"width {ra['chanbw']:.0f} vs {rb['chanbw']:.0f} MHz")
        if ra.get('frequency') and rb.get('frequency') and ra['frequency'] != rb['frequency']:
            hard.append(f"frequency {ra['frequency']:.0f} vs {rb['frequency']:.0f} MHz")
        ma, mb = _major(ra.get('firmware')), _major(rb.get('firmware'))
        if ma is not None and mb is not None and ma != mb:
            soft.append(f"firmware {ra['firmware']} vs {rb['firmware']} (different major version)")
        nightly = [x['name'] for x in (ra, rb) if parse_firmware(x.get('firmware'))[0] == 'nightly']
        if len(nightly) == 1:
            row['problems'].append(f"{nightly[0]} runs a nightly build, the other end a release")

        fresh = _config_fresh(ra, ctx['now']) and _config_fresh(rb, ctx['now'])
        row['problems'] = hard + soft + row['problems']
        if hard:
            row['severity'] = 'critical' if fresh else 'warning'
            stale_note = '' if fresh else (' Stored settings are more than a day old for at least one end, '
                                           'so this may be out of date.')
            _add(f, row['severity'], 'link', f"{a} ↔ {b} settings don't match",
                 f"The two ends of this RF link disagree: {'; '.join(hard)}.{stale_note}",
                 [a, b], evidence=[ctx['describe'](ra), ctx['describe'](rb)],
                 recommendation='Mesh links need the same SSID, channel and width on both ends; a PtP/PtMP '
                                'link needs one access point and one station with the same SSID, channel and width.')
        elif soft:
            row['severity'] = 'warning'
            _add(f, 'warning', 'link', f"{a} ↔ {b} run different firmware generations",
                 '; '.join(soft) + '. Mixed major versions can differ in link handling and LQM behavior.',
                 [a, b], evidence=[ctx['describe'](ra), ctx['describe'](rb)],
                 recommendation='Upgrade the older end to the current release.')
        elif row['problems']:
            row['severity'] = 'info'
    rows.sort(key=lambda r: (SEVERITY_ORDER[r['severity']], r['a']))
    return rows


def _check_sites(ctx, f):
    out = []
    for site in ctx['sites']:
        members = site['members']
        mesh = [r for r in members if _rf_on(r) and _role(r.get('mode')) == 'mesh']
        # Mesh SSID/width norms per band: radios on different bands never link.
        by_band = defaultdict(list)
        for r in mesh:
            by_band[_band(r)].append(r)
        band_norms = {band: {'ssid': _majority(r.get('ssid') for r in rs),
                             'chanbw': _majority(r.get('chanbw') for r in rs)}
                      for band, rs in by_band.items()}
        norms = {'firmware': _majority(r.get('firmware') for r in members), 'bands': band_norms}
        rows = []
        for r in members:
            differs = []
            if norms['firmware'] and r.get('firmware') and r['firmware'] != norms['firmware']:
                differs.append('firmware')
            if r in mesh:
                for key in ('ssid', 'chanbw'):
                    norm = band_norms[_band(r)][key]
                    if norm and r.get(key) and r[key] != norm:
                        differs.append(key)
            rows.append(dict(ctx['summary'](r, r['name']), differs=differs))
        out.append({'code': site['code'], 'name': site['name'], 'norms': norms, 'radios': rows})

        firmware = defaultdict(list)
        for r in members:
            if r.get('firmware'):
                firmware[r['firmware']].append(r['name'])
        if len(firmware) > 1:
            majors = {_major(v) for v in firmware} - {None}
            severity = 'warning' if len(majors) > 1 or any(
                ctx['fw_class'].get(n) in ('old', 'very_old') for n in sum(firmware.values(), [])) else 'info'
            _add(f, severity, 'site', f"{site['name']}: {len(firmware)} firmware versions on one site",
                 'Radios on one mast are usually maintained together; mixed versions behave differently '
                 '(LQM, link modes, drivers).', sum(firmware.values(), []), site['code'],
                 [f"{v}: {', '.join(sorted(n))}" for v, n in sorted(firmware.items())],
                 'Bring the site to one release on the next visit.')

        for key, label in (('ssid', 'SSID'), ('chanbw', 'channel width')):
            for r in mesh:
                norm = band_norms[_band(r)][key]
                if not norm or not r.get(key) or r[key] == norm:
                    continue
                isolated = not ctx['rf_degree'].get(r['name'])
                value = f"{r[key]:.0f} MHz" if key == 'chanbw' else r[key]
                norm = f"{norm:.0f} MHz" if key == 'chanbw' else norm
                _add(f, 'warning' if isolated else 'info', 'site',
                     f"{r['name']} uses {label} {value}; other {_band(r)} mesh radios at {site['name']} use {norm}",
                     'A mesh radio only links with radios on the same SSID and width. Fine when deliberate '
                     '(a separate backbone or a different client group)' +
                     ('; this one has no current RF links, so it may be stranded.' if isolated else '.'),
                     [r['name']], site['code'], recommendation=f"Confirm {r['name']} is meant to differ.")

        blocks = {r['name']: r['user_blocks'] for r in mesh if r.get('user_blocks')}
        if blocks and len(mesh) > 1:
            _add(f, 'info', 'site', f"{site['name']}: LQM user blocks differ between sector radios",
                 'Some sector radios block neighbors and others don\'t. A block set on one sector is often '
                 'meant for all of them, and a forgotten one silently removes a neighbor.',
                 list(blocks), site['code'],
                 [f"{n}: {b}" for n, b in sorted(blocks.items())] +
                 [f"{r['name']}: no blocks" for r in mesh if r['name'] not in blocks],
                 'Confirm each block is still wanted and applied consistently.')
    return out


def _check_firmware(ctx, f):
    groups = defaultdict(list)
    for name, cls in ctx['fw_class'].items():
        groups[cls].append(name)
    latest = ctx['latest']
    latest_text = '.'.join(map(str, latest)) if latest else 'none'
    for cls, severity, title, rec in (
            ('very_old', 'critical', 'very old firmware', 'Upgrade these first; old releases lack current LQM and tunnel fixes.'),
            ('old', 'warning', 'firmware from the previous release line', f"Upgrade to {latest_text}."),
            ('behind', 'info', 'an older point release', f"Upgrade to {latest_text} when convenient.")):
        names = sorted(groups.get(cls, []))
        if not names:
            continue
        _add(f, severity, 'firmware', f"{len(names)} node(s) run {title}",
             f"Newest release seen on the mesh: {latest_text}.", names,
             evidence=[f"{n}: {ctx['radios'][n].get('firmware')} ({ctx['fw_reason'][n]})" for n in names[:25]] +
                      ([f"... and {len(names) - 25} more"] if len(names) > 25 else []),
             recommendation=rec)
    nightly = sorted(groups.get('nightly', []))
    if nightly:
        _add(f, 'info', 'firmware', f"{len(nightly)} node(s) run recent nightly builds",
             'Nightly builds are development snapshots; keep them current or move to a release.', nightly,
             evidence=[f"{n}: {ctx['radios'][n].get('firmware')}" for n in nightly])

    histogram = defaultdict(lambda: {'nodes': []})
    for name, r in ctx['radios'].items():
        v = r.get('firmware') or 'unknown'
        histogram[v]['nodes'].append(name)
        histogram[v]['class'] = ctx['fw_class'][name]
    order = {c: i for i, c in enumerate(FIRMWARE_CLASSES)}

    def sort_key(item):
        p = parse_firmware(item[0])
        rank = p[1] if p[0] == 'release' else (0,)
        return (order.get(item[1]['class'], 9), [-x for x in rank] if p[0] == 'release' else [0], item[0])

    return [{'version': v, 'class': h['class'], 'count': len(h['nodes']), 'nodes': sorted(h['nodes'])}
            for v, h in sorted(histogram.items(), key=sort_key)]


def _check_missing(ctx, f):
    never = sorted(n for n, r in ctx['radios'].items() if r.get('is_active') and not r.get('config_at'))
    if never:
        _add(f, 'info', 'missing', f"{len(never)} active node(s) have never reported radio settings",
             'Their settings can\'t be compared. Usually they haven\'t been polled since settings started '
             'being stored, or their firmware doesn\'t report the mesh radio.', never,
             evidence=never[:30], recommendation='Run a scan, or refresh settings from the Infrastructure Review.')
    no_width = sorted(n for n, r in ctx['radios'].items() if _rf_on(r) and r.get('config_at') and not r.get('chanbw'))
    if no_width:
        _add(f, 'info', 'missing', f"{len(no_width)} radio(s) report a frequency but no channel width",
             'Width is assumed 20 MHz for them in channel checks.', no_width, evidence=no_width)


# ----------------------------------------------------------------- entry --

def run_report(scope='all'):
    now = datetime.now()
    nodes = database.get_all_nodes()
    boundaries = database.get_scan_boundaries()
    current = [l for l in database.get_active_links() if l.get('status') == 'good']
    _, in_scope = mesh_diagnostics.compute_scope(nodes, current, boundaries)
    infra = database.get_infrastructure_nodes()

    with database.get_connection() as conn:
        allowed = reports.resolve_scope(conn, scope or 'all')
        names = in_scope if allowed is None else in_scope & allowed
        radios = _radios(conn, names)
        pairs = {}
        rf_degree = Counter()
        for src, tgt, dist in conn.execute('''
            SELECT source_node, target_node, distance FROM links
            WHERE UPPER(link_type) = 'RF' AND status = 'good' AND target_node NOT LIKE 'mac-%'
        '''):
            if src in radios:
                rf_degree[src] += 1
            if src in radios or tgt in radios:
                key = tuple(sorted((src, tgt)))
                pairs[key] = dist or pairs.get(key)

    releases = [p[1] for p in (parse_firmware(r.get('firmware')) for r in radios.values()) if p[0] == 'release']
    latest = max(releases) if releases else None
    fw_class, fw_reason = {}, {}
    for name, r in radios.items():
        fw_class[name], fw_reason[name] = classify_firmware(r.get('firmware'), latest, now)

    def summary(r, name):
        if not r:
            return {'name': name, 'known': False}
        return {'name': name, 'known': bool(r.get('config_at')), 'model': r.get('model'),
                'firmware': r.get('firmware'), 'firmware_class': fw_class.get(name),
                'mode': r.get('mode'), 'role': _role(r.get('mode')), 'ssid': r.get('ssid'),
                'chanbw': r.get('chanbw'), 'frequency': r.get('frequency'), 'channel': r.get('channel'),
                'rf_status': r.get('rf_status'), 'user_blocks': r.get('user_blocks'),
                'config_at': r.get('config_at'), 'is_infrastructure': name in infra}

    def describe(r):
        bits = [r.get('mode'), r.get('ssid'),
                f"{r['frequency']:.0f} MHz" if r.get('frequency') else None,
                f"{r['chanbw']:.0f} MHz wide" if r.get('chanbw') else None, r.get('firmware')]
        return f"{r['name']}: " + ', '.join(str(b) for b in bits if b) + \
            (f" (settings read {r['config_at']})" if r.get('config_at') else '')

    ctx = {'now': now, 'radios': radios, 'infra': infra, 'latest': latest,
           'fw_class': fw_class, 'fw_reason': fw_reason, 'rf_degree': rf_degree,
           'rf_pairs': [(a, b, d) for (a, b), d in sorted(pairs.items())],
           'sites': _sites(radios, infra, database.get_site_names()),
           'summary': summary, 'describe': describe}
    findings = []
    links = _check_links(ctx, findings)
    sites = _check_sites(ctx, findings)
    histogram = _check_firmware(ctx, findings)
    _check_missing(ctx, findings)
    findings.sort(key=lambda x: SEVERITY_ORDER[x['severity']])

    counts = Counter(x['severity'] for x in findings)
    return {
        'generated_at': now.strftime(ld.TS_FMT),
        'scope': scope,
        'node_count': len(radios),
        'latest_release': '.'.join(map(str, latest)) if latest else None,
        'counts': dict(counts),
        'findings': findings,
        'links': links,
        'sites': sites,
        'firmware': histogram,
        'notes': ['sysinfo does not report LQM distance/SNR thresholds or the radio distance setting, '
                  'so those are not compared.'],
    }

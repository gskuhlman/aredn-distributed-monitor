"""
Node hygiene score (Diagnostics tab "Node Hygiene", node page, new-node
welcome): per node, a 0-100 score from the settings an owner can fix, with a
"fix these" list, grouped by owner (the callsign at the start of the name).

Checks (points):
  location      location set (20) and plausible (10: aim_check location
                problems: wrong sign, 0,0, shared by unrelated callsigns,
                reported link distance disagrees);
  firmware      current release or recent nightly (20; older point release 12,
                previous line 6, very old 0) against the newest release seen
                (drift.classify_firmware);
  description   node description set (10) with a way to reach the owner
                (5: e-mail, phone or callsign in it);
  name          starts with a callsign (5);
  radio         for a node with its radio on: antenna azimuth set unless it
                is an omni (10), antenna described (5), antenna height (5);
  polled        answered the scanner in the last day (10).
Radio checks are skipped (points not counted) for nodes without a radio.
Read-only.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta

import database
import drift

TS_FMT = '%Y-%m-%d %H:%M:%S'
CALLSIGN = re.compile(r'^[a-z]{1,2}\d[a-z]{1,4}$', re.I)
CONTACT = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+|\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\b[a-z]{1,2}\d[a-z]{1,4}\b', re.I)
LOCATION_TEXT = {'zero': 'it is 0,0', 'lon_sign': 'the longitude sign is flipped',
                 'lat_sign': 'the latitude sign is flipped',
                 'distance': 'link distances reported by the node disagree with it'}
FW_POINTS = {'current': 20, 'nightly': 20, 'behind': 12, 'old': 6, 'very_old': 0, 'unknown': 0}


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def owner_of(name):
    first = (name or '').split('-')[0]
    return first.upper() if CALLSIGN.match(first) else '(no callsign)'


def _omni(n):
    bw = _num(n.get('antenna_beamwidth'))
    desc = (n.get('antenna_desc') or '').lower()
    return (bw is not None and bw >= 360) or 'omni' in desc or 'built-in' in desc and bw is None


def _location_problems():
    try:
        import aim_check
        r = aim_check.run_report(scope='all')
        return {row['name']: row['location_problem'] for row in r.get('radios', []) if row.get('location_problem')}
    except Exception:
        return {}


def score_node(n, latest, now, bad_location):
    checks = []

    def check(key, points, ok, fix, partial=None):
        got = points if ok else (partial or 0)
        checks.append({'key': key, 'points': points, 'got': got, 'ok': bool(ok), 'fix': None if ok else fix})

    lat, lon = _num(n.get('lat')), _num(n.get('lon'))
    located = lat is not None and lon is not None and (lat or lon)
    check('location', 20, located, 'Set the node location (Setup > Location, or the map on the node).')
    if located:
        problem = bad_location.get(n['name'])
        check('location_plausible', 10, not problem,
              f"Location looks wrong: {LOCATION_TEXT.get(problem, problem)} (see Antenna Aim).")
    cls, reason = drift.classify_firmware(n.get('firmware_version'), latest, now)
    check('firmware', 20, cls in ('current', 'nightly'),
          f"Update firmware ({n.get('firmware_version') or 'unknown'}: {reason}).", FW_POINTS.get(cls, 0))
    desc = (n.get('description') or '').strip()
    check('description', 10, bool(desc), 'Add a node description (what it is, where, who to contact).')
    check('contact', 5, bool(desc and CONTACT.search(desc)), 'Put a way to reach the owner in the description.')
    first = (n.get('name') or '').split('-')[0]
    check('name', 5, bool(CALLSIGN.match(first)), 'Start the node name with the owner\'s callsign.')
    radio_on = str(n.get('rf_status') or '').lower() != 'off' and _num(n.get('rf_frequency'))
    if radio_on:
        check('azimuth', 10, _omni(n) or _num(n.get('antenna_azimuth')) is not None,
              'Set the antenna azimuth (Setup > Antenna) so aim and conflict checks work.')
        check('antenna', 5, bool(n.get('antenna_desc')) or _num(n.get('antenna_beamwidth')) is not None,
              'Pick the antenna model (Setup > Antenna).')
        check('height', 5, (_num(n.get('antenna_height')) or 0) >= 2, 'Set the antenna height above ground.')
    seen = n.get('last_seen')
    polled = bool(n.get('is_active')) and bool(seen) and seen[:19] >= (now - timedelta(days=1)).strftime(TS_FMT)
    check('polled', 10, polled, 'The scanner could not read this node in the last day.')
    total = sum(c['points'] for c in checks)
    got = sum(c['got'] for c in checks)
    return {'name': n['name'], 'owner': owner_of(n['name']), 'score': round(100 * got / total) if total else None,
            'firmware': n.get('firmware_version'), 'firmware_class': cls, 'model': n.get('model'),
            'radio': bool(radio_on), 'checks': checks, 'fixes': [c['fix'] for c in checks if c['fix']]}


def _latest(nodes):
    rel = [drift.parse_firmware(n.get('firmware_version')) for n in nodes]
    rel = [p[1] for p in rel if p[0] == 'release']
    return max(rel) if rel else None


def report(node=None, include_inactive=False):
    now = datetime.now()
    nodes = database.get_all_nodes()
    latest = _latest(nodes)
    bad = _location_problems()
    rows = []
    for n in nodes:
        if node and n['name'] != node:
            continue
        if not node and (n['name'].startswith('mac-') or (not include_inactive and not n.get('is_active'))):
            continue
        rows.append(score_node(n, latest, now, bad))
    rows.sort(key=lambda r: (r['score'] if r['score'] is not None else 999, r['name']))
    owners = defaultdict(list)
    for r in rows:
        owners[r['owner']].append(r)
    owner_rows = sorted(({'owner': o, 'nodes': len(rs), 'avg_score': round(sum(r['score'] for r in rs) / len(rs)),
                          'fixes': sum(len(r['fixes']) for r in rs), 'names': [r['name'] for r in rs]}
                         for o, rs in owners.items()), key=lambda x: (x['avg_score'], x['owner']))
    return {'generated_at': now.strftime(TS_FMT), 'latest_release': '.'.join(map(str, latest)) if latest else None,
            'nodes': rows, 'owners': owner_rows,
            'average': round(sum(r['score'] for r in rows) / len(rows)) if rows else None}

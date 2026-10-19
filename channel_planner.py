"""
Channel planner: suggested channels for a site (or a list of radios), scored
with the same geometry and conflict rules as the Channel Map, then checked
with its what-if recompute. Nothing is stored and no radio is touched.

Radios that must share a channel form a group: a sector and the clients
RF-linked to it ("move clients with their sector"), or both ends of a PtP
link. Each group gets one centre frequency and width whose whole span sits
inside the allowed band. A plan's cost adds:

- channel conflicts with every other transmitting radio in range (mast
  neighbours, radios within CONFLICT_KM in either beam), weighted by the
  Channel Map severity (overlap/adjacent/near);
- same-channel reuse between groups that face each other (back-to-back
  sectors on one mast reuse a channel cheaply);
- infrastructure at another site aimed at the radio (either way): a backbone
  dish (narrow beam) aimed at it must not share its channel, and every such
  pair needs a 20 MHz guard band;
- busy WiFi / C-V2X / TDWR ranges as a noise risk, per radio;
- measured noise (signal - SNR from link history, scaled to 20 MHz) of the
  radio itself or radios on its mast, on the part of the band it covers;
- an RF link that would break because its far end isn't moving with it;
- a small cost per radio changed, so a good current channel is kept.

Point-to-point antennas (aimed, beam <= IDLE_MAX_BEAM_DEG) with no RF links
at all, e.g. a dish whose far end doesn't exist yet, are suggested off
instead of getting a channel (idle_off) and are left out of everyone else's
conflicts. Omnis and sectors stay on: they serve whoever shows up.

Search: greedy start plus coordinate descent from the current plan and from
seeded random starts; distinct local optima are ranked. The planner only
changes frequency and width; it never suggests TX power changes.
"""

import math
import random
import time
from datetime import datetime, timedelta
from statistics import median

import channel_map as cm
import database
import infra_audit as ia
import link_diagnostics as ld
import link_watch

DEFAULT_BAND = cm.HAM_5GHZ
STEP_MHZ = 5
RESTARTS = 40
BACK_TO_BACK_DEG = 150      # colocated antennas this far apart may share a channel
BEAM_GUARD_DEG = 30
MAX_ROUNDS = 20

SEVERITY_COST = {'critical': 100.0, 'warning': 25.0, 'info': 5.0}
LINK_BREAK_COST = 1000.0    # a group member's RF link to a radio left on another channel
CHANGE_COST = 2.0           # per radio whose frequency changes
WIDTH_CHANGE_COST = 3.0     # per radio whose width changes
NARROWER_COST = 25.0        # per radio per halving of its width (lost capacity)

# Other infrastructure sites: a backbone dish aimed at a radio must not share
# its channel, and infrastructure aimed either way needs a 20 MHz guard band.
NARROW_BEAM_DEG = 30        # at or below this an infrastructure antenna counts as a backbone dish
IDLE_MAX_BEAM_DEG = 45      # only aimed point-to-point antennas are suggested off when idle
INFRA_GUARD_MHZ = 20
INFRA_COST = {'same_dish': SEVERITY_COST['critical'], 'same': SEVERITY_COST['warning'],
              'overlap': SEVERITY_COST['critical'], 'adjacent': SEVERITY_COST['critical'],
              'near': SEVERITY_COST['warning']}

# Measured noise: dB above NOISE_REF_DBM (per 20 MHz) costs this much per radio.
NOISE_HOURS = 72
NOISE_REF_DBM = -100.0
NOISE_DB_COST = 3.0

# Noise risk per radio for a channel fully inside the range (scaled by the
# fraction of the channel that overlaps it).
NOISE_RANGES = [
    {'low': 5600, 'high': 5650, 'label': 'TDWR weather radar (5600–5650)', 'cost': 200.0},
    {'low': 5650, 'high': 5660, 'label': 'first 10 MHz above TDWR', 'cost': 4.0},
    {'low': 5490, 'high': 5730, 'label': 'WiFi ch 100–144 (DFS)', 'cost': 3.0},
    {'low': 5735, 'high': 5835, 'label': 'WiFi ch 149–165 (UNII-3, heavy)', 'cost': 20.0},
    {'low': 5835, 'high': 5895, 'label': 'WiFi 6E/7 UNII-4 (indoor, growing)', 'cost': 3.0},
    {'low': 5895, 'high': 5925, 'label': 'C-V2X (vehicles)', 'cost': 2.0},
]
RELATIONS = ('overlap', 'adjacent', 'near', 'same', 'clear')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _span(freq, width):
    return freq - width / 2, freq + width / 2


def _relation(c1, c2):
    """Spectral relation of two (freq, width) channels, as ia.pair_relation."""
    if c1 == c2:
        return 'same'
    lo1, hi1 = _span(*c1)
    lo2, hi2 = _span(*c2)
    return ia.gap_relation(max(lo2 - hi1, lo1 - hi2))


def _gap(c1, c2):
    lo1, hi1 = _span(*c1)
    lo2, hi2 = _span(*c2)
    return max(lo2 - hi1, lo1 - hi2)


def _geometry(a, b):
    """Distance and beam facts for a radio pair, or None when out of range."""
    d = ld._haversine_m(a['lat'], a['lon'], b['lat'], b['lon'])
    if d is None or d > cm.CONFLICT_KM * 1000:
        return None
    colocated = d <= cm.SITE_METERS
    a_sees, b_sees = cm._in_beam(a, b, d), cm._in_beam(b, a, d)
    # Infrastructure at two different sites with at least one aimed at the
    # other; 'dish' when the aimed one is a narrow-beam (backbone) antenna.
    infra_watch = dish_watch = False
    if not colocated and a.get('is_infrastructure') and b.get('is_infrastructure'):
        infra_watch = a_sees or b_sees
        dish_watch = any(sees and x.get('azimuth') is not None and (x['beamwidth'] or 360) <= NARROW_BEAM_DEG
                         for x, sees in ((a, a_sees), (b, b_sees)))
    back_to_back = False
    if colocated and a.get('azimuth') is not None and b.get('azimuth') is not None \
            and a['beamwidth'] < 360 and b['beamwidth'] < 360:
        # Opposite sectors (N/S, E/W) can reuse a channel; neighbours can't.
        back_to_back = ld._angle_diff(a['azimuth'], b['azimuth']) >= max(
            BACK_TO_BACK_DEG, (a['beamwidth'] + b['beamwidth']) / 2 + BEAM_GUARD_DEG)
    return {'d': d, 'colocated': colocated, 'a_sees': a_sees, 'b_sees': b_sees,
            'back_to_back': back_to_back, 'infra_watch': infra_watch, 'dish_watch': dish_watch}


def _infra_rule(rel, g):
    """Infrastructure-site rule that applies to a pair, or None."""
    if not g['infra_watch']:
        return None
    if rel == 'same':
        return 'same_dish' if g['dish_watch'] else 'same'
    return rel if rel in ('overlap', 'adjacent', 'near') else None


def _pair_cost(rel, g):
    """Cost of two radios (different groups) on channels with relation rel."""
    rule = _infra_rule(rel, g)
    return max(_base_pair_cost(rel, g), INFRA_COST[rule] if rule else 0.0)


def _base_pair_cost(rel, g):
    if rel == 'same':
        if g['colocated']:
            return 15.0 if g['back_to_back'] else 60.0
        mutual = g['a_sees'] and g['b_sees']
        km = g['d'] / 1000
        if mutual and km <= 8:
            return 15.0
        if (mutual and km <= 15) or ((g['a_sees'] or g['b_sees']) and km <= 8):
            return 5.0
        return 1.0 if (g['a_sees'] or g['b_sees']) and km <= 15 else 0.0
    if rel in ('overlap', 'adjacent', 'near'):
        sev = cm._severity(rel, g['colocated'], g['d'] / 1000, g['a_sees'], g['b_sees'])
        return SEVERITY_COST.get(sev, 0.0)
    return 0.0


def _noise_cost(chan, members):
    lo, hi = _span(*chan)
    cost, hits = 0.0, []
    for rng in NOISE_RANGES:
        overlap = min(hi, rng['high']) - max(lo, rng['low'])
        if overlap > 0:
            frac = overlap / (hi - lo)
            cost += rng['cost'] * frac * members
            hits.append((rng['label'], round(frac * 100)))
    return cost, hits


def _noise_samples(radios):
    """Median measured noise per radio over NOISE_HOURS, scaled to 20 MHz, on
    its current channel. Radios overlapping or adjacent to another radio on
    their own mast are skipped: their noise is mostly self-inflicted."""
    start = (datetime.now() - timedelta(hours=NOISE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
    with database.get_connection() as conn:
        buckets = link_watch._noise_buckets(conn, start)
    live = [r for r in radios if cm._transmitting(r)]
    out = []
    for r in live:
        samples = buckets.get(r['name'])
        if not samples or len(samples) < 6:
            continue
        chan = (r['frequency'], r['chanbw'] or 20.0)
        if any(o is not r and _relation(chan, (o['frequency'], o['chanbw'] or 20.0)) in ('same', 'overlap', 'adjacent')
               and (ld._haversine_m(r['lat'], r['lon'], o['lat'], o['lon']) or 1e9) <= cm.SITE_METERS
               for o in live):
            continue
        noise = median(v for _, v, _ in samples)
        lo, hi = _span(*chan)
        out.append({'name': r['name'], 'lat': r['lat'], 'lon': r['lon'], 'frequency': chan[0], 'chanbw': chan[1],
                    'low': lo, 'high': hi, 'noise': round(noise, 1),
                    'noise20': round(noise - 10 * math.log10(chan[1] / 20.0), 1), 'samples': len(samples)})
    return out


def _measured_noise(g, c):
    """(cost, worst sample) of channel c from noise measured by the group's
    radios or radios on their masts, on the part of the band c covers."""
    lo, hi = _span(*c)
    worst, ref = 0.0, None
    for smp in g['noise_refs']:
        overlap = min(hi, smp['high']) - max(lo, smp['low'])
        if overlap <= 0:
            continue
        excess = max(0.0, smp['noise20'] - NOISE_REF_DBM) * overlap / (hi - lo)
        if ref is None or excess > worst:
            worst, ref = excess, dict(smp, overlap_pct=round(overlap / (hi - lo) * 100))
    return NOISE_DB_COST * worst * len(g['members']), ref


def _groups(planned, radios, links, move_clients):
    """Union planned radios with the RF peers that must share their channel."""
    parent = {}

    def find(n):
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    members = set(planned)
    if move_clients:
        for l in links:
            if l['a'] in planned or l['b'] in planned:
                members.update((l['a'], l['b']))
    members = {n for n in members if n in radios}
    for n in members:
        find(n)
    for l in links:
        if l['a'] in members and l['b'] in members:
            parent[find(l['a'])] = find(l['b'])
    groups = {}
    for n in members:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values()]


def _candidates(band, widths):
    out = []
    for w in widths:
        f = STEP_MHZ * math.ceil((band[0] + w / 2) / STEP_MHZ)  # first 5 MHz centre that fits
        while f + w / 2 <= band[1]:
            if f - w / 2 >= band[0]:
                out.append((float(f), float(w)))
            f += STEP_MHZ
    return out


def plan(site=None, radios=None, band=None, widths=None, locked=None, move_clients=True,
         top=3, compare=None, seed=1, idle_off=True):
    """Ranked channel plans for a site code or radio list. widths: None =
    keep each group's width, a list for every group, or {name: [..]}."""
    started = time.time()
    base = cm.build_map()
    by_name = {r['name']: r for r in base['radios']}
    band = (float(band[0]), float(band[1])) if band else DEFAULT_BAND
    locked = {n.strip().lower() for n in (locked or []) if n and n.strip()}
    notes = []

    if site:
        code = site.strip().upper()
        planned = {r['name'] for r in base['radios'] if r.get('site_code') == code}
        for s in base['sites']:
            if code in s['codes']:
                planned.update(s['members'])
        if not planned:
            return {'error': f'No located radios at site {code}'}
    else:
        planned = {n.strip().lower() for n in (radios or []) if n and n.strip()}
        missing = sorted(n for n in planned if n not in by_name)
        if missing:
            notes.append(f"No location or not found (not planned): {', '.join(missing)}")
        planned = {n for n in planned if n in by_name}
        if not planned:
            return {'error': 'None of the radios were found with a location'}

    links = base['links']
    # Point-to-point antennas with no RF link at all: suggest turning them
    # off rather than giving them a channel (they only add noise for others).
    linked = {n for l in links for n in (l['a'], l['b'])}
    idle = []
    if idle_off:
        idle = sorted(n for n in planned if n not in linked and n not in locked
                      and cm._transmitting(by_name[n]) and by_name[n].get('azimuth') is not None
                      and (by_name[n]['beamwidth'] or 360) <= IDLE_MAX_BEAM_DEG)
        planned -= set(idle)
    groups = []
    for g in _groups(planned, by_name, links, move_clients):
        live = [n for n in g if cm._transmitting(by_name[n])]
        off = sorted(set(g) - set(live))
        if off:
            notes.append(f"Radio off or no channel (not planned): {', '.join(off)}")
        if not live:
            continue
        freqs = [by_name[n]['frequency'] for n in live]
        if not all(band[0] - 100 <= f <= band[1] + 100 for f in freqs):
            notes.append(f"Not in the planned band (left as is): {', '.join(live)}")
            continue
        cur_w = max((by_name[n]['chanbw'] or 20.0) for n in live)
        lead = next((n for n in live if n in planned), live[0])
        cur = (by_name[lead]['frequency'], cur_w)
        if isinstance(widths, dict):
            allowed = sorted({_num(w) for n in live for w in widths.get(n, []) if _num(w)}) or [cur_w]
        else:
            allowed = sorted({_num(w) for w in (widths or []) if _num(w)}) or [cur_w]
        groups.append({'members': live, 'lead': lead, 'current': cur,
                       'locked': bool(locked & set(live)),
                       'mismatch': len({(by_name[n]['frequency'], by_name[n]['chanbw']) for n in live}) > 1,
                       'cands': _candidates(band, allowed)})
    if not groups and not idle:
        return {'error': 'Nothing to plan', 'notes': notes}

    planned_names = {n for g in groups for n in g['members']}
    fixed = [r for r in base['radios'] if r['name'] not in planned_names and r['name'] not in idle
             and cm._transmitting(r) and r['recent'] and (r['band'] or '').startswith('5')]
    noise = _noise_samples([r for r in base['radios'] if r['name'] not in idle])
    for g in groups:
        if g['locked']:
            g['cands'] = [g['current']]
        elif not g['cands']:
            notes.append(f"No channel of the allowed widths fits the band for {', '.join(g['members'])}")
            g['cands'] = [g['current']]

    # Fixed-radio cost per group and candidate (other radios keep their channel).
    for g in groups:
        geo = []
        for f in fixed:
            for n in g['members']:
                gm = _geometry(by_name[n], f)
                if gm:
                    geo.append((n, f, gm))
        g['fixed_geo'] = geo
        peers = {}
        for l in links:
            for a, b in ((l['a'], l['b']), (l['b'], l['a'])):
                if a in g['members'] and b not in g['members']:
                    peers[b] = a
        g['link_peers'] = peers
        g['noise_refs'] = [smp for smp in noise if any(
            (ld._haversine_m(by_name[n]['lat'], by_name[n]['lon'], smp['lat'], smp['lon']) or 1e9) <= cm.SITE_METERS
            for n in g['members'])]
        g['cost'] = {c: _single_cost(g, c, by_name) for c in set(g['cands']) | {g['current']}}

    # Group-pair cost depends only on the spectral relation: precompute it.
    pair_w = {}
    for i, gi in enumerate(groups):
        for j in range(i + 1, len(groups)):
            gj = groups[j]
            w = dict.fromkeys(RELATIONS, 0.0)
            for a in gi['members']:
                for b in gj['members']:
                    gm = _geometry(by_name[a], by_name[b])
                    if gm:
                        for rel in RELATIONS:
                            w[rel] += _pair_cost(rel, gm)
            if any(w.values()):
                pair_w[(i, j)] = w
    neighbours = {i: [] for i in range(len(groups))}
    for (i, j), w in pair_w.items():
        neighbours[i].append((j, w))
        neighbours[j].append((i, w))

    def local_cost(i, c, assign):
        cost = groups[i]['cost'][c]
        for j, w in neighbours[i]:
            cost += w[_relation(c, assign[j])]
        return cost

    def total(assign):
        cost = sum(groups[i]['cost'][assign[i]] for i in range(len(groups)))
        for (i, j), w in pair_w.items():
            cost += w[_relation(assign[i], assign[j])]
        return cost

    def descend(assign):
        for _ in range(MAX_ROUNDS):
            improved = False
            for i, g in enumerate(groups):
                best = min(g['cands'], key=lambda c: (local_cost(i, c, assign), c != assign[i]))
                if local_cost(i, best, assign) < local_cost(i, assign[i], assign) - 1e-9:
                    assign[i] = best
                    improved = True
            if not improved:
                break
        return assign

    rng = random.Random(seed)
    starts = []
    current = [g['current'] for g in groups]
    # Start from the current plan, nudged into the band where it doesn't fit.
    starts.append([g['current'] if g['current'] in g['cands']
                   else min(g['cands'], key=lambda c: abs(c[0] - g['current'][0])) for g in groups])
    greedy = [None] * len(groups)
    order = sorted(range(len(groups)), key=lambda i: -len(neighbours[i]))
    for i in order:
        def gcost(c, i=i):
            cost = groups[i]['cost'][c]
            for j, w in neighbours[i]:
                if greedy[j] is not None:
                    cost += w[_relation(c, greedy[j])]
            return cost
        greedy[i] = min(groups[i]['cands'], key=gcost)
    starts.append(greedy)
    for _ in range(RESTARTS):
        starts.append([rng.choice(g['cands']) for g in groups])

    seen = {}
    for s in starts:
        a = descend(list(s))
        key = tuple(a)
        if key not in seen:
            seen[key] = total(a)
    def changes(assign):
        return sum(1 for i, g in enumerate(groups) for n in g['members']
                   if (by_name[n]['frequency'], by_name[n]['chanbw'] or 20.0) != assign[i])
    ranked = sorted(seen.items(), key=lambda kv: (round(kv[1], 1), changes(kv[0])))[:max(1, int(top or 3))]

    current_cost = total(current)
    result = {
        'generated_at': base['generated_at'], 'site': site.upper() if site else None,
        'band': {'low': band[0], 'high': band[1]}, 'move_clients': bool(move_clients),
        'groups': [{'members': g['members'], 'lead': g['lead'], 'locked': g['locked'],
                    'current': {'frequency': g['current'][0], 'chanbw': g['current'][1]},
                    'mismatch': g['mismatch'], 'candidates': len(g['cands'])} for g in groups],
        'notes': notes, 'searched': len(starts), 'distinct_optima': len(seen),
        'current': _describe(list(current), groups, by_name, fixed, pair_w, current_cost, current=True),
        'turn_off': [{'name': n, 'is_infrastructure': by_name[n].get('is_infrastructure'),
                      'frequency': by_name[n]['frequency'], 'chanbw': by_name[n]['chanbw'],
                      'reason': 'point-to-point antenna with no RF links: nothing to serve until its far '
                                'end exists, and while on it only adds noise for nearby radios'} for n in idle],
        'noise_samples': len(noise),
        'plans': [],
    }
    for assign, cost in ranked:
        p = _describe(list(assign), groups, by_name, fixed, pair_w, cost, idle=idle)
        p['what_if'] = _what_if(p['what_if_text'])
        result['plans'].append(p)
    if compare:
        result['compare'] = _evaluate_overrides(compare, groups, by_name, fixed, pair_w, total)
    result['elapsed_ms'] = round((time.time() - started) * 1000)
    return result


def _issues(i, c, assign, groups, by_name, fixed):
    """Remaining channel problems of group i on channel c."""
    out = []
    g = groups[i]
    for n, f, gm in g['fixed_geo']:
        rel = _relation(c, (f['frequency'], f['chanbw'] or 20.0))
        cost = _pair_cost(rel, gm)
        if cost:
            out.append({'a': n, 'b': f['name'], 'relation': rel, 'cost': cost, 'rule': _infra_rule(rel, gm),
                        'gap_mhz': _gap(c, (f['frequency'], f['chanbw'] or 20.0)),
                        'colocated': gm['colocated'], 'distance_m': round(gm['d'])})
    for j, other in enumerate(groups):
        if j == i:
            continue
        rel = _relation(c, assign[j])
        for a in g['members']:
            for b in other['members']:
                gm = _geometry(by_name[a], by_name[b])
                cost = _pair_cost(rel, gm) if gm else 0
                if cost and a < b:
                    out.append({'a': a, 'b': b, 'relation': rel, 'cost': cost,
                                'rule': _infra_rule(rel, gm), 'gap_mhz': _gap(c, assign[j]),
                                'colocated': gm['colocated'], 'distance_m': round(gm['d'])})
    for peer, member in g['link_peers'].items():
        p = by_name.get(peer)
        if p and p.get('frequency') and (p['frequency'], p['chanbw'] or 20.0) != c:
            out.append({'a': member, 'b': peer, 'relation': 'link_break', 'cost': LINK_BREAK_COST,
                        'gap_mhz': None, 'colocated': False, 'distance_m': None})
    return out


def _reasons(i, c, assign, groups, by_name, fixed, issues):
    g = groups[i]
    reasons = []
    if c == g['current']:
        reasons.append('keeps the current channel')
    if len(g['members']) > 1:
        reasons.append(f"shared by {', '.join(g['members'])} (sector and RF-linked clients move together)")
    if g['locked']:
        reasons.append('locked')
    for peer, member in sorted(g['link_peers'].items()):
        p = by_name.get(peer)
        if p and p.get('frequency') and (p['frequency'], p['chanbw'] or 20.0) == c:
            reasons.append(f'held on {peer}\'s channel: {member} has an RF link to it and {peer} is not in '
                           'the plan (turn on "move clients" or add it to move both)')
    if c[1] < g['current'][1]:
        reasons.append(f"narrower than now ({g['current'][1]:g} → {c[1]:g} MHz): less capacity")
    # Smallest guard band to every other radio on the same mast.
    gaps, shared = [], []
    for n, f, gm in g['fixed_geo']:
        if gm['colocated']:
            fc = (f['frequency'], f['chanbw'] or 20.0)
            if fc == c:
                shared.append((f['name'], gm['back_to_back']))
            else:
                gaps.append((_gap(c, fc), f['name']))
    for j, other in enumerate(groups):
        if j == i:
            continue
        geos = [_geometry(by_name[a], by_name[b]) for a in g['members'] for b in other['members']]
        colo = [gm for gm in geos if gm and gm['colocated']]
        if colo:
            if assign[j] == c:
                shared.append((other['lead'], all(gm['back_to_back'] for gm in colo)))
            else:
                gaps.append((_gap(c, assign[j]), other['lead']))
    for who, b2b in shared:
        reasons.append(f'reuses the channel of {who} on the mast' +
                       (' (antennas face opposite ways)' if b2b else ' (antennas overlap: shared airtime)'))
    if gaps:
        gmin, who = min(gaps)
        if gmin >= ia.GUARD_MHZ:
            reasons.append(f'{gmin:.0f} MHz or more clear of every other radio on the mast (closest: {who})')
        elif gmin >= 0:
            reasons.append(f'only {gmin:.0f} MHz guard band to {who} on the mast')
        else:
            reasons.append(f'overlaps {who} on the mast by {-gmin:.0f} MHz')
    infra = [x for x in issues if x.get('rule')]
    if not infra and any(gm['infra_watch'] for _, _, gm in g['fixed_geo']):
        reasons.append(f'{INFRA_GUARD_MHZ} MHz or more clear of infrastructure at other sites aimed this way')
    for x in infra:
        if x['rule'] == 'same_dish':
            reasons.append(f"same channel as backbone dish {x['b']} at another site, aimed at {x['a']}")
        else:
            reasons.append(f"less than {INFRA_GUARD_MHZ} MHz from infrastructure radio {x['b']} at another site "
                           f"({x['relation']}, {x['distance_m'] / 1000:.1f} km)")
    _, ref = _measured_noise(g, c)
    if ref:
        reasons.append(f"measured noise {ref['noise20']:g} dBm (per 20 MHz) at {ref['name']} on "
                       f"{ref['frequency']:g}/{ref['chanbw']:g}, covering {ref['overlap_pct']}% of this channel")
    hits = _noise_cost(c, 1)[1]
    if not hits:
        reasons.append('outside busy WiFi ranges')
    for label, pct in hits:
        reasons.append(f'{pct}% inside {label}')
    lo, hi = _span(*c)
    reasons.append(f'span {lo:.0f}–{hi:.0f} MHz inside the band')
    if not issues:
        reasons.append('no conflicts with radios in range')
    return reasons


def _describe(assign, groups, by_name, fixed, pair_w, cost, current=False, idle=()):
    radios, lines, counts = [], [], {'critical': 0, 'warning': 0, 'info': 0, 'same_channel': 0, 'link_break': 0}
    seen_issue = set()
    group_out = []
    for i, g in enumerate(groups):
        c = assign[i]
        issues = _issues(i, c, assign, groups, by_name, fixed)
        for iss in issues:
            key = (min(iss['a'], iss['b']), max(iss['a'], iss['b']), iss['relation'])
            if key in seen_issue:
                continue
            seen_issue.add(key)
            if iss['relation'] == 'same':
                counts['same_channel'] += 1
                if iss.get('rule'):
                    iss['severity'] = next(s for s, v in SEVERITY_COST.items() if v == iss['cost'])
                    counts[iss['severity']] += 1
            elif iss['relation'] == 'link_break':
                counts['link_break'] += 1
            else:
                sev = next((s for s, v in SEVERITY_COST.items() if v == iss['cost']), 'info')
                iss['severity'] = sev
                counts[sev] += 1
        lo, hi = _span(*c)
        for n in g['members']:
            r = by_name[n]
            old_w = r['chanbw'] or 20.0
            changed = r['frequency'] != c[0] or old_w != c[1]
            radios.append({
                'name': n, 'group': i, 'is_infrastructure': r.get('is_infrastructure'),
                'old': {'frequency': r['frequency'], 'chanbw': r['chanbw'], 'channel': ia.freq_to_channel(r['frequency']),
                        'low': r['low'], 'high': r['high']},
                'new': {'frequency': c[0], 'chanbw': c[1], 'channel': ia.freq_to_channel(c[0]),
                        'low': lo, 'high': hi, 'low_channel': ia.freq_to_channel(lo),
                        'high_channel': ia.freq_to_channel(hi)},
                'changed': changed,
            })
            if changed:
                lines.append(f"{n} {c[0]:g} {c[1]:g}")
        group_out.append({'members': g['members'], 'lead': g['lead'],
                          'frequency': c[0], 'chanbw': c[1], 'channel': ia.freq_to_channel(c[0]),
                          'reasons': _reasons(i, c, assign, groups, by_name, fixed, issues),
                          'issues': sorted(issues, key=lambda x: -x['cost'])})
    for n in idle:
        r = by_name[n]
        radios.append({'name': n, 'group': None, 'is_infrastructure': r.get('is_infrastructure'), 'off': True,
                       'old': {'frequency': r['frequency'], 'chanbw': r['chanbw'],
                               'channel': ia.freq_to_channel(r['frequency']), 'low': r['low'], 'high': r['high']},
                       'new': {'frequency': None, 'chanbw': None, 'channel': None, 'off': True},
                       'changed': True})
        lines.append(f'{n} off')
    return {'cost': round(cost, 1), 'counts': counts, 'radios': radios, 'groups': group_out,
            'changes': sum(1 for r in radios if r['changed']),
            'what_if_text': '\n'.join(lines) if not current else ''}


def _what_if(text):
    """Channel Map what-if delta for a plan (whole map, in memory only)."""
    if not text:
        return None
    m = cm.build_map(overrides=text)
    w = m.get('what_if') or {}
    return {'counts': m['counts'], 'baseline_counts': w.get('baseline_counts'),
            'resolved': [c['summary'] for c in w.get('resolved', [])],
            'introduced': [{'severity': c['severity'], 'summary': c['summary']} for c in w.get('introduced', [])],
            'changed': len(w.get('changed', []))}


def _evaluate_overrides(raw, groups, by_name, fixed, pair_w, total):
    """Score an operator's own plan ("name freq width" lines) with the same
    model. Groups take the override of any member; a group whose members get
    different channels is reported as split."""
    overrides = cm.parse_overrides(raw)
    assign, split = [], []
    for g in groups:
        chans = {(o['frequency'] or by_name[n]['frequency'], o['chanbw'] or g['current'][1])
                 for n in g['members'] if (o := overrides.get(n))}
        if len(chans) > 1:
            split.append(g['members'])
        c = sorted(chans)[0] if chans else g['current']
        if c not in g['cost']:
            g['cost'][c] = _single_cost(g, c, by_name)
        assign.append(c)
    cost = total(assign)
    out = _describe(assign, groups, by_name, fixed, pair_w, cost)
    out['split_groups'] = split
    out['what_if'] = _what_if(raw if isinstance(raw, str) else out['what_if_text'])
    lo, hi = DEFAULT_BAND
    out['outside_band'] = [r['name'] for r in out['radios']
                           if r['new']['low'] < lo or r['new']['high'] > hi]
    return out


def _single_cost(g, c, by_name):
    cost = 0.0
    for n, f, gm in g['fixed_geo']:
        cost += _pair_cost(_relation(c, (f['frequency'], f['chanbw'] or 20.0)), gm)
    for peer in g['link_peers']:
        p = by_name.get(peer)
        if p and p.get('frequency') and (p['frequency'], p['chanbw'] or 20.0) != c:
            cost += LINK_BREAK_COST
    cost += _noise_cost(c, len(g['members']))[0]
    cost += _measured_noise(g, c)[0]
    for n in g['members']:
        r = by_name[n]
        cost += CHANGE_COST if r['frequency'] != c[0] else 0
        old_w = r['chanbw'] or 20.0
        if old_w != c[1]:
            cost += WIDTH_CHANGE_COST
            if c[1] < old_w:
                cost += NARROWER_COST * math.log2(old_w / c[1])
    return cost

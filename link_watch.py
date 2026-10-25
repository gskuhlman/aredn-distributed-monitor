"""
Link Watch: continuous detectors that run after every committed scan.

- Noise-floor watch (#7): each radio's noise floor (signal - SNR from its own
  LQM trackers, promoted link_history columns) against a rolling baseline (the
  same hour of day when there are enough days of data, otherwise the median of
  the baseline window). Raises `noise_jump` when the noise sits >= NOISE_JUMP_DB
  above baseline for NOISE_JUMP_SCANS scans in a row; `noise_cleared` once it is
  back within NOISE_CLEAR_DB for as many scans.
- One-way link alarm (#8): an RF pair where one direction is healthy and the
  other is not (LQ/quality asymmetry, Babel cost infinite on one side while the
  RF is fine, one end tracking the other only by MAC, or one end not listing
  the other at all). Raises `one_way_link` after ONE_WAY_SCANS scans,
  escalates once to critical after ONE_WAY_ESCALATE_MINUTES, and raises
  `one_way_link_cleared` after as many healthy scans.

Events fire on state transitions only. State lives in the SQLite table
`link_watch_state` so restarts don't re-fire. Never run on aborted scans.

Maintenance windows (maintenance.py): while a radio or pair is in a window
(grace period included) nothing is raised, cleared or escalated and its
hysteresis counters are reset, so after the window it takes the usual number
of scans to alarm. A radio's noise baseline restarts after each window that
covered it (samples before the window's end are dropped), so a new channel's
noise floor is not compared with the old channel's.
"""

import json
import logging
from datetime import datetime, timedelta
from statistics import median

import config
import database
import link_diagnostics as ld
import maintenance

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
KIND_NOISE = 'noise'
KIND_ONE_WAY = 'one_way'
NOISE_SANE = (-125.0, -40.0)   # dBm; anything outside is a bad tracker sample


# ------------------------------------------------------------- state -----

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS link_watch_state (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                bad_count INTEGER NOT NULL DEFAULT 0,
                good_count INTEGER NOT NULL DEFAULT 0,
                since TEXT,
                escalated INTEGER NOT NULL DEFAULT 0,
                cleared_at TEXT,
                updated_at TEXT,
                detail TEXT,
                PRIMARY KEY (kind, key)
            )
        ''')
    _tables_ready = True


def _load_state(kind):
    ensure_tables()
    with database.get_connection() as conn:
        rows = conn.execute('SELECT * FROM link_watch_state WHERE kind = ?', (kind,)).fetchall()
    out = {}
    for r in rows:
        s = dict(r)
        try:
            s['detail'] = json.loads(s.get('detail') or '{}')
        except (TypeError, ValueError):
            s['detail'] = {}
        out[s['key']] = s
    return out


def _save_state(kind, states):
    with database.get_connection() as conn:
        conn.executemany('''
            INSERT INTO link_watch_state (kind, key, active, bad_count, good_count, since,
                                          escalated, cleared_at, updated_at, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET
                active = excluded.active, bad_count = excluded.bad_count,
                good_count = excluded.good_count, since = excluded.since,
                escalated = excluded.escalated, cleared_at = excluded.cleared_at,
                updated_at = excluded.updated_at, detail = excluded.detail
        ''', [(kind, k, s['active'], s['bad_count'], s['good_count'], s.get('since'),
               s.get('escalated', 0), s.get('cleared_at'), s.get('updated_at'),
               json.dumps(s.get('detail') or {})) for k, s in states.items()])


def _new_state(key):
    return {'key': key, 'active': 0, 'bad_count': 0, 'good_count': 0, 'since': None,
            'escalated': 0, 'cleared_at': None, 'updated_at': None, 'detail': {}}


def _emit(events, event_type, node, details, severity):
    database.log_event(event_type, node_name=node, details=details, severity=severity)
    events.append({'type': event_type, 'node': node, 'details': details, 'severity': severity})


def _step(state, bad, need, now_s):
    """Advance the hysteresis counters; return 'raise', 'clear' or None."""
    if bad is None:          # no data this scan: hold
        return None
    if bad:
        state['bad_count'] += 1
        state['good_count'] = 0
        if not state['active'] and state['bad_count'] >= need:
            state['active'] = 1
            state['since'] = now_s
            state['escalated'] = 0
            return 'raise'
    else:
        state['good_count'] += 1
        state['bad_count'] = 0
        if state['active'] and state['good_count'] >= need:
            state['active'] = 0
            state['cleared_at'] = now_s
            return 'clear'
    return None


# ------------------------------------------------------------- noise -----

def _noise_buckets(conn, start, node=None, end=None):
    """Per radio and per scan (minute bucket): average noise (signal - SNR)
    across its RF trackers. Returns {node: [(bucket_ts, noise, trackers)]}."""
    sql = '''
        SELECT source_node, substr(timestamp, 1, 16) AS bucket,
               AVG(signal - snr) AS noise, COUNT(*) AS n
        FROM link_history
        WHERE sample_type = 'scan' AND UPPER(link_type) = 'RF'
          AND signal IS NOT NULL AND snr IS NOT NULL AND snr > 0
          AND timestamp >= ?
          AND (signal - snr) BETWEEN ? AND ?
    '''
    args = [start, NOISE_SANE[0], NOISE_SANE[1]]
    if node:
        sql += ' AND source_node = ?'
        args.append(node)
    if end:
        sql += ' AND timestamp <= ?'
        args.append(end)
    sql += ' GROUP BY source_node, bucket ORDER BY bucket'
    out = {}
    for r in conn.execute(sql, args):
        out.setdefault(r['source_node'], []).append((r['bucket'] + ':00', round(r['noise'], 1), r['n']))
    return out


def _baseline(samples, now, current_ts=None):
    """Baseline noise for a radio from its earlier samples: median of the same
    hour of day if it has enough distinct days there, otherwise the median of
    the baseline window. Samples from the last NOISE_BASELINE_EXCLUDE_MINUTES
    are left out so a jump doesn't absorb itself."""
    cutoff = (now - timedelta(minutes=config.NOISE_BASELINE_EXCLUDE_MINUTES)).strftime(TS_FMT)
    earlier = [(ts, v) for ts, v, _ in samples if ts < cutoff]
    if len(earlier) < config.NOISE_MIN_BASELINE_SAMPLES:
        return None, 'insufficient', len(earlier)
    hour = (current_ts or now.strftime(TS_FMT))[11:13]
    same_hour = [(ts, v) for ts, v in earlier if ts[11:13] == hour]
    days = {ts[:10] for ts, _ in same_hour}
    if len(days) >= config.NOISE_HOUR_MIN_DAYS and len(same_hour) >= 6:
        return round(median(v for _, v in same_hour), 1), 'hour_of_day', len(same_hour)
    return round(median(v for _, v in earlier), 1), 'window', len(earlier)


def _current(samples, now, stale_after):
    if not samples:
        return None
    ts, v, n = samples[-1]
    seen = ld._parse_ts(ts)
    if not seen or (now - seen).total_seconds() > stale_after:
        return None
    return ts, v, n


def _baseline_samples(node, samples, now, windows):
    """Samples usable for the baseline: those after the end of the latest
    maintenance window that covered the radio (and none inside a window)."""
    if not windows or not samples:
        return samples
    intervals = maintenance.intervals_for(node, samples[0][0], now, windows)
    if not intervals:
        return samples
    ended = [b for _, b in intervals if b <= now]
    after = maintenance._fmt(max(ended)) if ended else None
    return [x for x in samples if (after is None or x[0] >= after)
            and not maintenance.ts_in_intervals(x[0], intervals)]


def _radio_status(samples, now, stale_after, base_samples=None):
    cur = _current(samples, now, stale_after)
    base, method, n_base = _baseline(samples if base_samples is None else base_samples,
                                     now, cur[0] if cur else None)
    delta = round(cur[1] - base, 1) if cur and base is not None else None
    return {'current': cur[1] if cur else None, 'current_at': cur[0] if cur else None,
            'trackers': cur[2] if cur else None, 'baseline': base, 'baseline_method': method,
            'baseline_samples': n_base, 'delta': delta}


def check_noise(now=None):
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    stale_after = ld._stale_after_seconds()
    start = (now - timedelta(hours=config.NOISE_BASELINE_HOURS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        buckets = _noise_buckets(conn, start, end=now_s)
    windows = maintenance.windows_between(start, now)
    states = _load_state(KIND_NOISE)
    events, touched = [], {}
    for node, samples in buckets.items():
        st = _radio_status(samples, now, stale_after, _baseline_samples(node, samples, now, windows))
        if st['current'] is None:
            continue
        state = states.get(node) or _new_state(node)
        w = maintenance.covering_window(node, now, windows=windows)
        if w:
            # In maintenance: hold the alarm state, restart the counters.
            state['bad_count'] = state['good_count'] = 0
            state['updated_at'] = now_s
            st['maintenance'] = maintenance.describe_window(w)
            state['detail'] = st
            touched[node] = state
            continue
        if st['delta'] is None:
            bad = None
        elif st['delta'] >= config.NOISE_JUMP_DB:
            bad = True
        elif st['delta'] <= config.NOISE_CLEAR_DB:
            bad = False
        else:
            # Between the thresholds: hold an alarm, but don't count toward one.
            bad = None if state['active'] else False
        action = _step(state, bad, config.NOISE_JUMP_SCANS, now_s)
        state['updated_at'] = now_s
        prev_peak = (state['detail'] or {}).get('peak')
        st['peak'] = (max(st['current'], prev_peak) if prev_peak is not None else st['current'])             if state['active'] else None
        state['detail'] = st
        if action == 'raise':
            _emit(events, database.EVENT_NOISE_JUMP, node,
                  f"Noise floor {st['current']:.0f} dBm is {st['delta']:+.1f} dB above its "
                  f"baseline {st['baseline']:.0f} dBm ({st['baseline_method'].replace('_', ' ')}, "
                  f"{config.NOISE_JUMP_SCANS}+ scans). New interference near this radio "
                  f"(WiFi, another transmitter) cuts every link's SNR.", 'warning')
        elif action == 'clear':
            _emit(events, database.EVENT_NOISE_CLEARED, node,
                  f"Noise floor back to {st['current']:.0f} dBm (baseline {st['baseline']:.0f} dBm, "
                  f"{st['delta']:+.1f} dB); elevated since {state['since']}.", 'info')
        touched[node] = state
    if touched:
        _save_state(KIND_NOISE, touched)
    return events


def noise_report(node=None, hours=72, days=7):
    """Per-radio noise status, or one radio's series, baseline and hour-of-day
    profile (average noise per hour of day over `days`)."""
    ensure_tables()
    now = datetime.now()
    stale_after = ld._stale_after_seconds()
    span = max(hours, days * 24, config.NOISE_BASELINE_HOURS)
    start = (now - timedelta(hours=span)).strftime(TS_FMT)
    with database.get_connection() as conn:
        buckets = _noise_buckets(conn, start, node.lower() if node else None)
    states = _load_state(KIND_NOISE)
    infra = set(database.get_infrastructure_node_names())
    windows = maintenance.windows_between(start, now)

    def status(name, samples):
        st = _radio_status(samples, now, stale_after, _baseline_samples(name, samples, now, windows))
        w = maintenance.covering_window(name, now, windows=windows)
        st['maintenance'] = maintenance.describe_window(w) if w else None
        s = states.get(name) or {}
        st.update({'node': name, 'is_infrastructure': name in infra,
                   'alarm': bool(s.get('active')), 'since': s.get('since') if s.get('active') else None,
                   'cleared_at': s.get('cleared_at'), 'samples': len(samples)})
        return st

    if not node:
        radios = [status(n, s) for n, s in buckets.items()]
        radios.sort(key=lambda r: (not r['alarm'], -(r['delta'] if r['delta'] is not None else -99), r['node']))
        return {'generated_at': now.strftime(TS_FMT), 'hours': hours,
                'jump_db': config.NOISE_JUMP_DB, 'clear_db': config.NOISE_CLEAR_DB, 'radios': radios}

    name = node.lower()
    samples = buckets.get(name, [])
    series_start = (now - timedelta(hours=hours)).strftime(TS_FMT)
    profile_start = (now - timedelta(days=days)).strftime(TS_FMT)
    by_hour = {}
    for ts, v, _ in samples:
        if ts >= profile_start:
            by_hour.setdefault(int(ts[11:13]), []).append(v)
    profile = [{'hour': h, 'avg': round(sum(by_hour[h]) / len(by_hour[h]), 1) if h in by_hour else None,
                'min': min(by_hour[h]) if h in by_hour else None,
                'max': max(by_hour[h]) if h in by_hour else None,
                'samples': len(by_hour.get(h, []))} for h in range(24)]
    return {'generated_at': now.strftime(TS_FMT), 'hours': hours, 'days': days,
            'jump_db': config.NOISE_JUMP_DB, 'clear_db': config.NOISE_CLEAR_DB,
            'status': status(name, samples),
            'series': [{'t': ts, 'noise': v, 'trackers': n} for ts, v, n in samples if ts >= series_start],
            'hour_profile': profile,
            'profile_days': len({ts[:10] for ts, _, _ in samples if ts >= profile_start})}


# ------------------------------------------------------------ one-way ----

def _quality(d):
    """How well this end hears the other: LQ (% of hellos heard) when known."""
    for key in ('lq', 'quality'):
        if d.get(key) is not None:
            return d[key]
    return None


def evaluate_pair(cur, a, b, now, stale_after, nodes_fresh):
    """Evaluate one RF pair. Returns None when it can't be judged (an end not
    polled recently, wired pair, user block), else {'one_way': bool, ...}."""
    start = (now - timedelta(minutes=30)).strftime(TS_FMT)
    macs = {n: set(ld._node(cur, n, start, now)['known_macs']) for n in (a, b)}
    da = ld._direction(cur, a, b, macs[b], start, now, stale_after)
    db = ld._direction(cur, b, a, macs[a], start, now, stale_after)
    if da.get('wired_link_type') or db.get('wired_link_type'):
        return None
    if not (nodes_fresh.get(a) and nodes_fresh.get(b)):
        return None   # an end we couldn't poll can't be judged "deaf"
    for d in (da, db):
        if 'user' in str(d.get('blocked_reason') or '').lower():
            return None

    def side(d):
        ok = d['found'] and d.get('fresh')
        return {'node': d['source'], 'hears': d['target'], 'listed': bool(ok),
                'by_mac': d.get('via_alias') if ok else None,
                'lq': d.get('lq') if ok else None, 'quality': d.get('quality') if ok else None,
                'snr': d.get('snr') if ok else None, 'signal': d.get('signal') if ok else None,
                'txcost': d.get('txcost') if ok else None, 'rxcost': d.get('rxcost') if ok else None,
                'tx_fail_pct': d.get('tx_fail_pct_recent') if ok else None}

    sa, sb = side(da), side(db)
    reasons, deaf = [], set()
    if sa['listed'] != sb['listed']:
        good, bad = (sa, sb) if sa['listed'] else (sb, sa)
        if (_quality(good) or 0) >= config.ONE_WAY_LOW_LQ:
            reasons.append(f"{bad['node']} does not list {bad['hears']} as a neighbor while "
                           f"{good['node']} hears it ({_quality(good):.0f}%)")
            deaf.add(bad['node'])
    if sa['listed'] and sb['listed']:
        qa, qb = _quality(sa), _quality(sb)
        if qa is not None and qb is not None:
            hi, lo = (sa, sb) if qa >= qb else (sb, sa)
            if _quality(hi) >= config.ONE_WAY_HIGH_LQ and _quality(lo) <= config.ONE_WAY_LOW_LQ:
                reasons.append(f"{lo['node']} hears {_quality(lo):.0f}% of {lo['hears']}'s hellos, "
                               f"{hi['node']} hears {_quality(hi):.0f}% the other way")
                deaf.add(lo['node'])
        snr_ok = all((s['snr'] or 0) >= config.ONE_WAY_SNR_OK for s in (sa, sb))
        for s in (sa, sb):
            inf = [k for k in ('txcost', 'rxcost') if (s[k] or 0) >= ld.BABEL_INFINITY]
            if inf and snr_ok:
                reasons.append(f"{s['node']}'s Babel {'/'.join(inf)} to {s['hears']} is infinite "
                               f"although SNR is {sa['snr']:.0f}/{sb['snr']:.0f} dB")
                deaf.add(s['node'])
        if bool(sa['by_mac']) != bool(sb['by_mac']):
            s = sa if sa['by_mac'] else sb
            reasons.append(f"{s['node']} knows {s['hears']} only by MAC ({s['by_mac']}): "
                           f"{s['hears']}'s announcements aren't reaching it")
            deaf.add(s['node'])
    return {'pair': [a, b], 'one_way': bool(reasons), 'reasons': reasons,
            'deaf': sorted(deaf), 'ends': [sa, sb]}


def _rf_pairs(cur, now, stale_after):
    """Named RF pairs with a current tracker on at least one side."""
    cutoff = (now - timedelta(seconds=stale_after)).strftime(TS_FMT)
    pairs = set()
    import ghost_trackers
    ghosts = ghost_trackers.excluded_unordered()
    for r in cur.execute('''
        SELECT source_node, target_node FROM links
        WHERE UPPER(link_type) = 'RF' AND status != 'removed' AND last_seen >= ?
          AND target_node NOT LIKE 'mac-%' AND source_node NOT LIKE 'mac-%'
    ''', (cutoff,)).fetchall():
        a, b = sorted((r[0], r[1]))
        if a != b and (a, b) not in ghosts:
            pairs.add((a, b))
    return sorted(pairs)


def _fresh_nodes(cur, now, stale_after):
    cutoff = (now - timedelta(seconds=stale_after)).strftime(TS_FMT)
    return {r[0]: True for r in cur.execute(
        'SELECT name FROM nodes WHERE last_seen >= ? AND is_active = 1', (cutoff,)).fetchall()}


def evaluate_all(now=None):
    now = now or datetime.now()
    stale_after = ld._stale_after_seconds()
    with database.get_connection() as conn:
        cur = conn.cursor()
        fresh = _fresh_nodes(cur, now, stale_after)
        results = {}
        for a, b in _rf_pairs(cur, now, stale_after):
            try:
                r = evaluate_pair(cur, a, b, now, stale_after, fresh)
            except Exception as exc:   # one odd pair must not stop the rest
                logger.debug("one-way evaluation failed for %s/%s: %s", a, b, exc)
                continue
            if r is not None:
                results[f'{a}|{b}'] = r
    return results


def _evidence(r):
    parts = []
    for s in r['ends']:
        if not s['listed']:
            parts.append(f"{s['node']}: no current tracker for {s['hears']}")
            continue
        bits = [f"LQ {s['lq']:.0f}%" if s['lq'] is not None else None,
                f"quality {s['quality']:.0f}%" if s['quality'] is not None else None,
                f"SNR {s['snr']:.0f} dB" if s['snr'] is not None else None,
                f"txcost {s['txcost']:.0f}" if s['txcost'] is not None else None,
                f"rxcost {s['rxcost']:.0f}" if s['rxcost'] is not None else None,
                f"via MAC {s['by_mac']}" if s['by_mac'] else None]
        parts.append(f"{s['node']} hearing {s['hears']}: " + ', '.join(b for b in bits if b))
    return '; '.join(parts)


def check_one_way(now=None):
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    results = evaluate_all(now)
    states = _load_state(KIND_ONE_WAY)
    infra = set(database.get_infrastructure_node_names())
    windows = maintenance.windows_between(now, now + timedelta(seconds=1))
    events, touched = [], {}
    for key, r in results.items():
        state = states.get(key) or _new_state(key)
        a, b = r['pair']
        w = maintenance.covering_window(a, now, b, windows=windows)
        if w:
            # In maintenance: no raise/clear/escalation; counters restart.
            state['bad_count'] = state['good_count'] = 0
            state['updated_at'] = now_s
            if r['one_way'] or state['active']:
                state['detail'] = dict(r, maintenance=maintenance.describe_window(w))
            touched[key] = state
            continue
        prev_deaf = (state.get('detail') or {}).get('deaf') or []
        action = _step(state, r['one_way'], config.ONE_WAY_SCANS, now_s)
        state['updated_at'] = now_s
        if r['one_way'] or state['active']:
            state['detail'] = r
        a, b = r['pair']
        # Event shape (shared with link_dropped and matched by notifications):
        # node_name = one end (the deaf one), details start "RF link to <peer>".
        node = (r['deaf'] or prev_deaf or [a])[0]
        peer = b if node == a else a
        touches_infra = bool({a, b} & infra)
        if action == 'raise':
            _emit(events, database.EVENT_ONE_WAY_LINK, node,
                  f"RF link to {peer}: one-way, " + ' | '.join(r['reasons'])
                  + f". Evidence: {_evidence(r)}",
                  'critical' if touches_infra else 'warning')
            state['escalated'] = 1 if touches_infra else 0
        elif action == 'clear':
            _emit(events, database.EVENT_ONE_WAY_LINK_CLEARED, node,
                  f"RF link to {peer}: works both ways again (one-way since {state['since']}). "
                  f"Evidence: {_evidence(r)}", 'info')
        elif state['active'] and not state['escalated']:
            since = ld._parse_ts(state['since'])
            if since and (now - since).total_seconds() >= config.ONE_WAY_ESCALATE_MINUTES * 60:
                state['escalated'] = 1
                _emit(events, database.EVENT_ONE_WAY_LINK, node,
                      f"RF link to {peer}: still one-way after "
                      f"{config.ONE_WAY_ESCALATE_MINUTES} min (since {state['since']}), "
                      + ' | '.join(r['reasons']), 'critical')
        touched[key] = state

    # A flagged pair that is no longer evaluated (link gone, an end not polled)
    # is cleared after ONE_WAY_FORGET_MINUTES rather than left active forever.
    for key, state in states.items():
        if key in results or not state['active']:
            continue
        a, b = key.split('|', 1)
        if maintenance.covering_window(a, now, b, windows=windows):
            continue
        seen = ld._parse_ts(state.get('updated_at'))
        if seen and (now - seen).total_seconds() >= config.ONE_WAY_FORGET_MINUTES * 60:
            a, b = key.split('|', 1)
            node = ((state.get('detail') or {}).get('deaf') or [a])[0]
            peer = b if node == a else a
            state['active'] = 0
            state['cleared_at'] = now_s
            state['updated_at'] = now_s
            _emit(events, database.EVENT_ONE_WAY_LINK_CLEARED, node,
                  f"RF link to {peer}: no longer seen as an RF pair; one-way alarm "
                  f"(since {state['since']}) closed without confirming a fix.", 'info')
            touched[key] = state
    if touched:
        _save_state(KIND_ONE_WAY, touched)
    return events


def one_way_report(hours=24):
    """Currently flagged pairs (with how long), live candidates this instant,
    and pairs cleared within `hours`."""
    ensure_tables()
    now = datetime.now()
    since = (now - timedelta(hours=hours)).strftime(TS_FMT)
    states = _load_state(KIND_ONE_WAY)
    infra = set(database.get_infrastructure_node_names())
    live = evaluate_all(now)

    def row(key, s=None, r=None):
        r = r or (s or {}).get('detail') or {}
        a, b = key.split('|', 1)
        return {'key': key, 'pair': [a, b], 'deaf': r.get('deaf', []), 'reasons': r.get('reasons', []),
                'ends': r.get('ends', []), 'since': (s or {}).get('since'),
                'cleared_at': (s or {}).get('cleared_at'), 'updated_at': (s or {}).get('updated_at'),
                'escalated': bool((s or {}).get('escalated')),
                'touches_infrastructure': bool({a, b} & infra)}

    active = [row(k, s, live.get(k) if live.get(k, {}).get('one_way') else None)
              for k, s in states.items() if s['active']]
    pending = [row(k, states.get(k), r) for k, r in live.items()
               if r['one_way'] and not (states.get(k) or {}).get('active')]
    cleared = [row(k, s) for k, s in states.items()
               if not s['active'] and s.get('cleared_at') and s['cleared_at'] >= since]
    for lst in (active, pending, cleared):
        lst.sort(key=lambda x: (not x['touches_infrastructure'], x['key']))
    return {'generated_at': now.strftime(TS_FMT), 'hours': hours, 'active': active,
            'pending': pending, 'cleared': cleared, 'evaluated_pairs': len(live),
            'thresholds': {'high_lq': config.ONE_WAY_HIGH_LQ, 'low_lq': config.ONE_WAY_LOW_LQ,
                           'snr_ok': config.ONE_WAY_SNR_OK, 'scans': config.ONE_WAY_SCANS}}


def pair_watch_findings(a, b):
    """Watch state for one pair and both ends' noise, for link_diagnostics."""
    ensure_tables()
    key = '|'.join(sorted((a, b)))
    one = _load_state(KIND_ONE_WAY).get(key)
    noise = _load_state(KIND_NOISE)
    return {'one_way': one if one and one['active'] else None,
            'noise': {n: noise[n] for n in (a, b) if n in noise and noise[n]['active']}}


# ------------------------------------------------------------- entry -----

def run_after_scan(now=None):
    """Called once after each committed (non-aborted) scan. Returns the event
    dicts raised, for real-time emit."""
    ensure_tables()
    events = []
    for check in (check_noise, check_one_way):
        try:
            events.extend(check(now))
        except Exception as exc:
            logger.error("Link watch %s failed: %s", check.__name__, exc)
    return events

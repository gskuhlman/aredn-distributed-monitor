"""
Likely TX power (or antenna) changes at nodes that don't report their power.

Most AREDN firmware doesn't put TX power in sysinfo.json, and nodes outside
our SSH reach have no saved config to read, so a neighbour turning its power
up or down is invisible except in the signal we hear from it. This looks for
a lasting step in one RF direction's hourly mean signal (link_history.signal,
what `source_node` hears from `target_node`) while the reverse direction
stays flat. A one-way step points at the transmitter (power / antenna aim at
the far end); a step both ways is a path or antenna change and is skipped.

Detected steps go straight into change_log as 'auto' power entries with
origin 'scan' (ref 'sigstep:<tx>:<rx>:<hour>', one per pair per STEP_GAP).
Read-only over link_history. Run hourly (install) and on demand (detect).
"""

import logging
from datetime import datetime, timedelta

import database

logger = logging.getLogger(__name__)

TS_FMT = '%Y-%m-%d %H:%M:%S'
HOUR_FMT = '%Y-%m-%d %H'
BEFORE_HOURS = 6        # stable hours before the transition hour
AFTER_HOURS = 3         # stable hours after it (so a step is reported ~3-4 h late)
MIN_STEP_DB = 3.0       # mean shift that counts as a step
MAX_SPREAD_DB = 1.5     # hourly means within each side stay inside this band
MAX_REVERSE_DB = 1.5    # the other direction moves less than this
MIN_SAMPLES = 3         # samples an hour needs to count
STEP_GAP = timedelta(hours=12)   # one entry per pair within this
LOOKBACK_HOURS = 36


def _hourly(conn, since):
    """{(rx, tx): {hour: mean signal}} for RF directions since `since`."""
    out = {}
    for r in conn.execute('''
            SELECT source_node, target_node, substr(timestamp, 1, 13) AS hour,
                   AVG(CAST(signal AS REAL)) AS sig, COUNT(*) AS n
            FROM link_history
            WHERE timestamp >= ? AND link_type = 'RF' AND signal IS NOT NULL AND signal != ''
              AND CAST(signal AS REAL) < 0
            GROUP BY source_node, target_node, hour''', (since,)):
        if r['n'] >= MIN_SAMPLES:
            out.setdefault((r['source_node'], r['target_node']), {})[r['hour']] = r['sig']
    return out


def _window(series, start, hours):
    vals = []
    for i in range(hours):
        v = series.get((start + timedelta(hours=i)).strftime(HOUR_FMT))
        if v is None:
            return None
        vals.append(v)
    return vals


def find_steps(hourly, now):
    """[{tx, rx, hour, before, after, delta, reverse}] one-way signal steps,
    one per pair. `hour` is the transition hour (datetime)."""
    last_full = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    first = last_full - timedelta(hours=LOOKBACK_HOURS)
    steps = {}
    for (rx, tx), series in hourly.items():
        h = first + timedelta(hours=BEFORE_HOURS)
        while h + timedelta(hours=AFTER_HOURS) <= last_full:
            before = _window(series, h - timedelta(hours=BEFORE_HOURS), BEFORE_HOURS)
            after = _window(series, h + timedelta(hours=1), AFTER_HOURS)
            h_next = h + timedelta(hours=1)
            if not before or not after:
                h = h_next
                continue
            b, a = sum(before) / len(before), sum(after) / len(after)
            if (abs(a - b) >= MIN_STEP_DB and max(before) - min(before) <= MAX_SPREAD_DB
                    and max(after) - min(after) <= MAX_SPREAD_DB):
                rev = hourly.get((tx, rx), {})
                rb = _window(rev, h - timedelta(hours=BEFORE_HOURS), BEFORE_HOURS)
                ra = _window(rev, h + timedelta(hours=1), AFTER_HOURS)
                reverse = (sum(ra) / len(ra) - sum(rb) / len(rb)) if rb and ra else None
                if reverse is None or abs(reverse) <= MAX_REVERSE_DB:
                    cand = {'tx': tx, 'rx': rx, 'hour': h, 'before': round(b, 1), 'after': round(a, 1),
                            'delta': round(a - b, 1), 'reverse': None if reverse is None else round(reverse, 1)}
                    steps.setdefault((tx, rx), []).append(cand)
            h = h_next
    # Neighbouring transition hours all see the same step; report the
    # earliest one showing (nearly) the full shift, which is when it happened.
    out = []
    for cands in steps.values():
        top = max(abs(c['delta']) for c in cands)
        out.append(min((c for c in cands if abs(c['delta']) >= 0.9 * top), key=lambda c: c['hour']))
    return out


def _describe(s):
    word = 'stronger' if s['delta'] > 0 else 'weaker'
    h0 = s['hour'].strftime('%H:00')
    h1 = (s['hour'] + timedelta(hours=1)).strftime('%H:00')
    rev = ('reverse direction not measured' if s['reverse'] is None
           else f"{s['tx']} hears {s['rx']} {s['reverse']:+.1f} dB (unchanged)")
    return (f"Likely TX power or antenna change at {s['tx']}: {s['rx']} hears it {abs(s['delta']):.1f} dB "
            f"{word} ({s['before']:.1f} -> {s['after']:.1f} dBm) from between {h0} and {h1}; {rev}. "
            f"Detected from signal, not a reported setting.")


def detect(now=None):
    """Find new steps and add them to change_log. Returns entries added."""
    now = now or datetime.now()
    since = (now - timedelta(hours=LOOKBACK_HOURS + BEFORE_HOURS + 2)).strftime(TS_FMT)
    added = 0
    with database.get_connection() as conn:
        for s in find_steps(_hourly(conn, since), now):
            prefix = f"sigstep:{s['tx']}:{s['rx']}:"
            near = False
            for r in conn.execute("SELECT ref FROM change_log WHERE ref LIKE ?", (prefix + '%',)):
                try:
                    prev = datetime.strptime(r['ref'][len(prefix):], HOUR_FMT)
                except ValueError:
                    continue
                if abs(prev - s['hour']) < STEP_GAP:
                    near = True
                    break
            if near:
                continue
            cur = conn.execute('''
                INSERT OR IGNORE INTO change_log
                    (timestamp, node_name, peer_node, category, description, source, origin, ref, created_at)
                VALUES (?, ?, ?, 'power', ?, 'auto', 'scan', ?, ?)
            ''', ((s['hour'] + timedelta(minutes=30)).strftime(TS_FMT), s['tx'], s['rx'], _describe(s),
                  prefix + s['hour'].strftime(HOUR_FMT), database.local_timestamp()))
            added += cur.rowcount
    return added


def _task():
    try:
        n = detect()
        if n:
            logger.info("Signal-step power changes added: %s", n)
    except Exception as exc:
        logger.error("Signal-step detection error: %s", exc)


def install(scheduler):
    scheduler.add_job(_task, 'cron', minute=7, id='power_steps', replace_existing=True)

"""
Per-link alerts (TODO #73): alert rules that watch one client link of a PtMP
sector (or any RF link) rather than the whole radio -- the gap multipoint
radios leave in node-level monitoring.

A rule names a direction, source -> target (how `source` hears `target`, as
in the links table), and any of: minimum SNR, minimum LQ quality, minimum
transmit rate (Mbps) or MCS from Prometheus station samples (prom_source),
a maximum number of node-reported drops within `flap_hours`, and whether a
missing link counts. Evaluated after every committed scan; a rule raises
after WATCH_SCANS bad evaluations in a row and clears after as many good
ones. Pairs in a maintenance window are not raised.

Events: `link_alert` (the rule's severity; node_name = source, details
`RF link to <target>: ...` like link_dropped, so notification rules and node
pages pick it up) and `link_alert_cleared` (info). SQLite
`link_alert_rules`, `link_alert_state`.
"""

import logging
from datetime import datetime, timedelta

import config
import database
import maintenance
import prom_source

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
WATCH_SCANS = 2
FIELDS = ('min_snr', 'min_quality', 'min_rate_mbps', 'min_mcs', 'max_flaps')
_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS link_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, target TEXT NOT NULL,
                min_snr REAL, min_quality REAL, min_rate_mbps REAL, min_mcs REAL,
                max_flaps INTEGER, flap_hours REAL NOT NULL DEFAULT 1,
                alert_missing INTEGER NOT NULL DEFAULT 1,
                severity TEXT NOT NULL DEFAULT 'warning',
                enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at DATETIME);
            CREATE TABLE IF NOT EXISTS link_alert_state (
                rule_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
                bad_count INTEGER NOT NULL DEFAULT 0, good_count INTEGER NOT NULL DEFAULT 0,
                since DATETIME, last_check DATETIME, detail TEXT, values_json TEXT);
        ''')
    _tables_ready = True


def _num(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def list_rules():
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''
            SELECT r.*, s.active, s.since, s.last_check, s.detail, s.values_json
            FROM link_alert_rules r LEFT JOIN link_alert_state s ON s.rule_id = r.id
            ORDER BY r.source, r.target''')]
    import json
    for r in rows:
        r['values'] = json.loads(r.pop('values_json') or '{}')
        r['active'] = bool(r['active'])
    return rows


def save_rule(data, rule_id=None):
    """Create or update a rule. Raises ValueError on bad input."""
    ensure_tables()
    source = (data.get('source') or '').strip().lower()
    target = (data.get('target') or '').strip().lower()
    if rule_id is None and (not source or not target or source == target):
        raise ValueError('source and target (two different nodes) are required')
    vals = {f: _num(data.get(f)) for f in FIELDS if f in data}
    if rule_id is None and not any(v is not None for v in vals.values()) and not data.get('alert_missing', True):
        raise ValueError('give at least one threshold')
    sev = data.get('severity') or 'warning'
    if sev not in ('info', 'warning', 'critical'):
        raise ValueError('severity must be info, warning or critical')
    with database.get_connection() as conn:
        if rule_id is None:
            cur = conn.execute('''INSERT INTO link_alert_rules (source, target, min_snr, min_quality, min_rate_mbps,
                                      min_mcs, max_flaps, flap_hours, alert_missing, severity, enabled, note, created_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                               (source, target, vals.get('min_snr'), vals.get('min_quality'), vals.get('min_rate_mbps'),
                                vals.get('min_mcs'), None if vals.get('max_flaps') is None else int(vals['max_flaps']),
                                _num(data.get('flap_hours')) or 1, 1 if data.get('alert_missing', True) else 0, sev,
                                1 if data.get('enabled', True) else 0, (data.get('note') or '').strip() or None,
                                datetime.now().strftime(TS_FMT)))
            return cur.lastrowid
        sets, params = [], []
        for f in FIELDS:
            if f in data:
                sets.append(f'{f} = ?')
                params.append(vals[f])
        for f in ('flap_hours',):
            if f in data:
                sets.append(f'{f} = ?')
                params.append(_num(data[f]) or 1)
        for f in ('alert_missing', 'enabled'):
            if f in data:
                sets.append(f'{f} = ?')
                params.append(1 if data[f] else 0)
        if 'severity' in data:
            sets.append('severity = ?')
            params.append(sev)
        if 'note' in data:
            sets.append('note = ?')
            params.append((data['note'] or '').strip() or None)
        if not sets:
            return rule_id
        n = conn.execute(f'UPDATE link_alert_rules SET {", ".join(sets)} WHERE id = ?', params + [rule_id]).rowcount
        if not n:
            raise LookupError('no such rule')
        if 'enabled' in data and not data['enabled']:
            conn.execute('DELETE FROM link_alert_state WHERE rule_id = ?', (rule_id,))
    return rule_id


def delete_rule(rule_id):
    ensure_tables()
    with database.get_connection() as conn:
        conn.execute('DELETE FROM link_alert_state WHERE rule_id = ?', (rule_id,))
        return conn.execute('DELETE FROM link_alert_rules WHERE id = ?', (rule_id,)).rowcount


def measure(conn, rule, now):
    """Current values for one rule's direction."""
    link = conn.execute('''SELECT quality, snr, status, last_seen, tx_rate FROM links
                           WHERE source_node = ? AND target_node = ?''', (rule['source'], rule['target'])).fetchone()
    vals = {'present': bool(link) and (link['status'] or 'good') == 'good'}
    if link:
        vals['snr'] = _num(link['snr'])
        vals['quality'] = _num(link['quality'])
        vals['lqm_rate_mbps'] = _num(link['tx_rate'])
    fresh = (now - timedelta(minutes=max(30, 3 * config.PROMETHEUS_POLL_MINUTES))).strftime(TS_FMT)
    try:
        prom_source.ensure_tables()
        sta = conn.execute('''SELECT tx_mcs, tx_rate FROM prom_station_samples WHERE node_name = ? AND peer = ?
                              AND timestamp >= ? ORDER BY timestamp DESC LIMIT 1''',
                           (rule['source'], rule['target'], fresh)).fetchone()
    except Exception:
        sta = None
    if sta:
        vals['mcs'] = sta['tx_mcs']
        vals['rate_mbps'] = round(sta['tx_rate'] / 1e6, 1) if sta['tx_rate'] else None
    since = (now - timedelta(hours=rule['flap_hours'] or 1)).strftime(TS_FMT)
    vals['flaps'] = conn.execute('''SELECT COUNT(*) FROM link_state_log WHERE timestamp >= ? AND state = 'down'
                                    AND COALESCE(origin, 'node_reported') = 'node_reported'
                                    AND ((source_node = ? AND target_node = ?) OR (source_node = ? AND target_node = ?))''',
                                 (since, rule['source'], rule['target'], rule['target'], rule['source'])).fetchone()[0]
    return vals


def problems(rule, vals):
    """Reasons the rule is breached (empty = fine)."""
    out = []
    if not vals['present']:
        if rule['alert_missing']:
            out.append('link not reported')
        return out
    checks = (('min_snr', 'snr', 'SNR', ' dB'), ('min_quality', 'quality', 'LQ', '%'),
              ('min_rate_mbps', 'rate_mbps', 'transmit rate', ' Mbps'), ('min_mcs', 'mcs', 'MCS', ''))
    for field, key, label, unit in checks:
        lim, v = rule.get(field), vals.get(key)
        if field == 'min_rate_mbps' and v is None:
            v = vals.get('lqm_rate_mbps')
        if lim is not None and v is not None and v < lim:
            out.append(f'{label} {round(v, 1):g}{unit} below {lim:g}{unit}')
    if rule.get('max_flaps') is not None and vals['flaps'] > rule['max_flaps']:
        out.append(f"{vals['flaps']} drops in {rule['flap_hours']:g} h (limit {rule['max_flaps']})")
    return out


def evaluate(now=None):
    """Check every enabled rule; returns logged events."""
    import json
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    events, rows = [], []
    with database.get_connection() as conn:
        rules = [dict(r) for r in conn.execute('SELECT * FROM link_alert_rules WHERE enabled = 1')]
        states = {r['rule_id']: dict(r) for r in conn.execute('SELECT * FROM link_alert_state')}
        for rule in rules:
            vals = measure(conn, rule, now)
            bad = problems(rule, vals)
            st = states.get(rule['id']) or {'active': 0, 'bad_count': 0, 'good_count': 0, 'since': None}
            head = f"RF link to {rule['target']}: "
            if bad:
                st['bad_count'] += 1
                st['good_count'] = 0
                if not st['active'] and st['bad_count'] >= WATCH_SCANS:
                    if maintenance.is_in_maintenance(rule['source'], peer=rule['target']):
                        st['bad_count'] = 0
                    else:
                        st['active'], st['since'] = 1, now_s
                        events.append(('link_alert', rule['source'], head + '; '.join(bad)
                                       + (f" ({rule['note']})" if rule['note'] else ''), rule['severity']))
            else:
                st['good_count'] += 1
                st['bad_count'] = 0
                if st['active'] and st['good_count'] >= WATCH_SCANS:
                    st['active'], st['since'] = 0, None
                    events.append(('link_alert_cleared', rule['source'], head + 'back within its alert limits', 'info'))
            rows.append((rule['id'], st['active'], st['bad_count'], st['good_count'], st['since'], now_s,
                         '; '.join(bad) or None, json.dumps(vals)))
        conn.executemany('INSERT OR REPLACE INTO link_alert_state VALUES (?,?,?,?,?,?,?,?)', rows)
    out = []
    for ev, node, text, sev in events:
        database.log_event(ev, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': ev, 'node_name': node, 'details': text, 'severity': sev})
    return out


def sector_clients(node):
    """RF peers of a radio (to offer one rule per client of a sector)."""
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute('''
            SELECT target_node AS peer, snr, quality, tx_rate FROM links
            WHERE source_node = ? AND UPPER(link_type) = 'RF' AND COALESCE(status, 'good') = 'good'
            ORDER BY target_node''', (node,))]

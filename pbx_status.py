"""
Asterisk PBX status over SSH (TODO #79): calls in progress and which phones
are registered, with an alarm when a phone drops off.

PBXs to check are a setting (`pbx_hosts`, edited on the VoIP & Phones page):
[{name, host, port, user}]. Every PBX_INTERVAL_MINUTES (gated by gateway.py,
like the other mesh checks) each PBX gets one SSH session with the same key,
pinned host keys and no-password options as ssh_collect.py (HostKeyAlias
`pbx-<name>`), running a fixed read-only script -- never anything typed by a
user -- of `asterisk -rx` commands:

  core show version, core show calls, core show channels concise,
  pjsip show endpoints (contacts included), sip show peers (chan_sip)

`asterisk -rx` needs the Asterisk control socket: log in as root, or as a user
in the asterisk group, or one allowed `sudo -n asterisk` (tried when the plain
call is refused). The private key never leaves its file.

Per phone (endpoint / peer): registered = a contact that isn't Unavail /
Unreachable (PJSIP) or a known host that isn't UNREACHABLE (chan_sip). A phone
that has been registered and then isn't for PBX_UNREACHABLE_RUNS checks in a
row raises `pbx_phone_unregistered` (warning), cleared by
`pbx_phone_registered`; phones never seen registered (spare extensions) don't
alarm. A PBX whose SSH or Asterisk doesn't answer for PBX_UNREACHABLE_RUNS
checks raises `pbx_unreachable` / `pbx_restored`. Failures while the starting
node is down are discarded. The events go to the node holding the phone's (or
PBX's) address when one does, else to the PBX name.

SQLite: `pbx_status` (per PBX: last check, error, version, call counts, the
channels in progress), `pbx_endpoints` (per phone: state, contact, RTT, alarm
counters) and `pbx_samples` (call / registration counts per check, kept
PBX_HISTORY_DAYS).
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta

import config
import database
import ssh_collect

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
MARK = ssh_collect.MARK
SETTING = 'pbx_hosts'
EVENT_PHONE_DOWN, EVENT_PHONE_UP = 'pbx_phone_unregistered', 'pbx_phone_registered'
EVENT_PBX_DOWN, EVENT_PBX_UP = 'pbx_unreachable', 'pbx_restored'

SCRIPT = f'''PATH="$PATH:/usr/sbin:/sbin:/usr/local/sbin"
ax() {{ out=$(asterisk -rx "$1" 2>&1)
  case "$out" in *"Unable to connect"*|*"ermission denied"*|*"not found"*)
    s=$(sudo -n asterisk -rx "$1" 2>/dev/null) && out="$s";; esac
  printf '%s\\n' "$out"; }}
echo "{MARK} version"; ax 'core show version'
echo "{MARK} calls"; ax 'core show calls'
echo "{MARK} channels"; ax 'core show channels concise'
echo "{MARK} endpoints"; ax 'pjsip show endpoints'
echo "{MARK} peers"; ax 'sip show peers'
echo "{MARK} end"
exit 0
'''

ERROR_HINTS = dict(ssh_collect.ERROR_HINTS, **{
    'auth': "the PBX refused the key: add the public key to that user's ~/.ssh/authorized_keys on the PBX",
    'asterisk': "logged in, but `asterisk -rx` was refused: use root, add the user to the asterisk group, "
                "or allow it `sudo -n asterisk`",
})
_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$')
_HOST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')
_USER = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
_ASTERISK_DOWN = re.compile(r'Unable to connect to remote asterisk|ermission denied|asterisk: (command )?not found', re.I)

_tables_ready = False


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with database.get_connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS pbx_status (
                name TEXT PRIMARY KEY, host TEXT, last_try TEXT, last_ok TEXT, error TEXT, error_kind TEXT,
                fails INTEGER DEFAULT 0, alarm INTEGER DEFAULT 0, since TEXT, version TEXT,
                active_calls INTEGER, active_channels INTEGER, calls_processed INTEGER, channels TEXT);
            CREATE TABLE IF NOT EXISTS pbx_endpoints (
                pbx TEXT NOT NULL, endpoint TEXT NOT NULL, tech TEXT, state TEXT, registered INTEGER,
                contact_ip TEXT, rtt_ms REAL, node_name TEXT, last_registered TEXT, last_ip TEXT,
                fail_count INTEGER DEFAULT 0, alarm INTEGER DEFAULT 0, since TEXT, updated TEXT,
                PRIMARY KEY (pbx, endpoint));
            CREATE TABLE IF NOT EXISTS pbx_samples (
                pbx TEXT NOT NULL, timestamp TEXT NOT NULL, active_calls INTEGER, registered INTEGER,
                unregistered INTEGER, PRIMARY KEY (pbx, timestamp));
        ''')
    _tables_ready = True


# ----------------------------------------------------------------- config --

def validate_host(entry):
    """Clean one {name, host, port, user} entry; raises ValueError. Strict,
    since host / port / user end up in ssh's argument list."""
    name = str(entry.get('name') or '').strip()
    host = str(entry.get('host') or '').strip()
    user = str(entry.get('user') or config.PBX_SSH_USER).strip()
    try:
        port = int(entry.get('port') or config.PBX_SSH_PORT)
    except (TypeError, ValueError):
        raise ValueError(f'{name or host}: port must be a number')
    if not _NAME.match(name):
        raise ValueError(f'"{name}": name may use letters, digits, . _ - (up to 40)')
    if not _HOST.match(host):
        raise ValueError(f'{name}: "{host}" is not a host name or IPv4 address')
    if not _USER.match(user):
        raise ValueError(f'{name}: "{user}" is not a valid user name')
    if not 1 <= port <= 65535:
        raise ValueError(f'{name}: port must be 1-65535')
    return {'name': name, 'host': host, 'port': port, 'user': user}


def get_hosts():
    try:
        raw = json.loads(database.get_setting(SETTING, '[]') or '[]')
    except (TypeError, ValueError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            out.append(validate_host(e))
        except (ValueError, AttributeError):
            continue
    return out


def save_hosts(entries):
    """Replace the PBX list; returns the cleaned list. Raises ValueError."""
    cleaned, names = [], set()
    for e in entries or []:
        h = validate_host(e)
        if h['name'].lower() in names:
            raise ValueError(f'{h["name"]}: listed twice')
        names.add(h['name'].lower())
        cleaned.append(h)
    database.set_setting(SETTING, json.dumps(cleaned))
    ensure_tables()
    with database.get_connection() as conn:
        keep = [h['name'] for h in cleaned]
        marks = ','.join('?' * len(keep)) or "''"
        for table, col in (('pbx_status', 'name'), ('pbx_endpoints', 'pbx'), ('pbx_samples', 'pbx')):
            conn.execute(f'DELETE FROM {table} WHERE {col} NOT IN ({marks})', keep)
    return cleaned


# ---------------------------------------------------------------- parsing --

def parse_calls(text):
    """{'active_channels', 'active_calls', 'calls_processed'} from `core show calls`
    (and the summary lines of `core show channels`)."""
    out = {'active_channels': None, 'active_calls': None, 'calls_processed': None}
    for key, pat in (('active_channels', r'(\d+) active channels?'), ('active_calls', r'(\d+) active calls?'),
                     ('calls_processed', r'(\d+) calls? processed')):
        m = re.search(pat, text or '')
        if m:
            out[key] = int(m.group(1))
    return out


def parse_channels(text):
    """`core show channels concise`: Channel!Context!Exten!Prio!State!App!Data!
    CallerID!Accountcode!PeerAccount!AMAFlags!Duration!Bridge!Uniqueid."""
    rows = []
    for line in (text or '').splitlines():
        f = line.strip().split('!')
        if len(f) < 8:
            continue
        duration = int(f[11]) if len(f) > 11 and f[11].isdigit() else None
        rows.append({'channel': f[0], 'context': f[1], 'exten': f[2], 'state': f[4], 'app': f[5],
                     'data': f[6], 'callerid': f[7], 'duration': duration,
                     'bridge': f[12] if len(f) > 12 and f[12] else None})
    return rows


_EP = re.compile(r'^\s*Endpoint:\s+(?P<name>[^\s/<]+)(?:/\S*)?\s+(?P<state>\S.*?)\s+\d+\s+of\s+(?:\d+|inf)\s*$')
_CONTACT = re.compile(r'^\s*Contact:\s+(?P<aor>[^\s/<]+)/(?P<uri>\S+)\s+(?P<hash>[0-9a-f]+)\s+(?P<status>\S+)'
                      r'(?:\s+(?P<rtt>[\d.]+|nan))?')
_DOWN_CONTACT = {'unavail', 'unreachable', 'removed'}


def contact_ip(uri):
    m = re.search(r'@(?:\[([0-9a-fA-F:.]+)\]|([A-Za-z0-9.-]+))', uri or '')
    return (m.group(1) or m.group(2)) if m else None


def _rtt(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # nan


def parse_pjsip_endpoints(text):
    """`pjsip show endpoints` -> [{name, tech, state, registered, contact_ip, rtt_ms}]."""
    eps, cur = [], None
    for line in (text or '').splitlines():
        m = _EP.match(line)
        if m:
            cur = {'name': m.group('name'), 'tech': 'PJSIP', 'state': m.group('state').strip(),
                   'registered': False, 'contact_ip': None, 'rtt_ms': None}
            eps.append(cur)
            continue
        m = _CONTACT.match(line)
        if m and cur is not None:
            up = m.group('status').lower() not in _DOWN_CONTACT
            if up and not cur['registered'] or cur['contact_ip'] is None:
                cur['contact_ip'] = contact_ip(m.group('uri'))
                cur['rtt_ms'] = _rtt(m.group('rtt'))
            cur['registered'] = cur['registered'] or up
    return eps


_PEER = re.compile(r'^(?P<name>[^\s/]+)(?:/\S*)?\s+(?P<host>\(Unspecified\)|\S+)\s.*?'
                   r'(?P<status>OK \((?P<ms>\d+) ms\)|LAGGED \((?P<lms>\d+) ms\)|UNREACHABLE|UNKNOWN|Unmonitored)')


def parse_sip_peers(text):
    """chan_sip `sip show peers` -> same shape as parse_pjsip_endpoints."""
    peers = []
    for line in (text or '').splitlines():
        if line.startswith('Name/username') or re.match(r'\d+ sip peers', line):
            continue
        m = _PEER.match(line)
        if not m:
            continue
        host = None if m.group('host') == '(Unspecified)' else m.group('host')
        status = m.group('status')
        ms = m.group('ms') or m.group('lms')
        peers.append({'name': m.group('name'), 'tech': 'SIP', 'state': status,
                      'registered': bool(host) and status != 'UNREACHABLE',
                      'contact_ip': host, 'rtt_ms': float(ms) if ms else None})
    return peers


def parse_output(out):
    """Sections of one session -> {ok, error_kind, error, version, calls..., channels, endpoints}."""
    sections = {kind: body for kind, _, body in ssh_collect.split_sections(out)}
    if 'end' not in sections:
        return {'ok': False}
    version = (sections.get('version') or '').strip()
    if not version or _ASTERISK_DOWN.search(version):
        return {'ok': False, 'error_kind': 'asterisk',
                'error': (version.splitlines() or ['no answer from asterisk -rx'])[-1][:300]}
    eps = parse_pjsip_endpoints(sections.get('endpoints'))
    names = {e['name'] for e in eps}
    eps += [p for p in parse_sip_peers(sections.get('peers')) if p['name'] not in names]
    calls = parse_calls((sections.get('calls') or '') + '\n' + (sections.get('channels') or ''))
    return dict(calls, ok=True, version=version.splitlines()[0][:200],
                channels=parse_channels(sections.get('channels')), endpoints=eps)


# -------------------------------------------------------------- collection --

def ssh_args(pbx):
    return [ssh_collect.ssh_exe(), '-T', '-p', str(pbx['port']), '-i', config.SSH_KEY_PATH,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={config.SSH_KNOWN_HOSTS}',
            '-o', f'HostKeyAlias=pbx-{pbx["name"]}', '-o', f'ConnectTimeout={config.SSH_CONNECT_TIMEOUT_SECONDS}',
            '-o', 'ServerAliveInterval=10', '-l', pbx['user'], '--', pbx['host'], 'sh -s']


def _run_ssh(pbx, script):
    """(returncode, stdout, stderr). Tests replace this."""
    return ssh_collect._run_ssh(pbx['name'], script, args=ssh_args(pbx))


def _node_ips(conn):
    return {r[0]: r[1] for r in conn.execute('SELECT name, ip FROM nodes WHERE ip IS NOT NULL')}


def _owner(ip, node_ips):
    import phones
    return phones.owner_node(ip, node_ips) if ip else None


def check(pbx, now=None):
    """One SSH session to one PBX; updates the tables. Returns {name, ok, events, ...}."""
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    try:
        code, out, err = _run_ssh(pbx, SCRIPT)
    except subprocess.TimeoutExpired:
        code, out, err = None, '', 'timed out'
    except OSError as exc:
        code, out, err = None, '', f'could not run ssh: {exc}'
    res = parse_output(out)
    with database.get_connection() as conn:
        row = conn.execute('SELECT * FROM pbx_status WHERE name = ?', (pbx['name'],)).fetchone()
        st = dict(row) if row else {'fails': 0, 'alarm': 0, 'since': None, 'last_ok': None}
        node_ips = _node_ips(conn)
    pbx_node = _owner(pbx['host'], node_ips) or pbx['name']
    events = []

    if not res['ok']:
        kind = res.get('error_kind') or ssh_collect.classify_error(err)
        if kind in ('timeout', 'dns', 'refused', 'error'):
            import gateway
            if gateway.failure_is_ours():
                return {'name': pbx['name'], 'ok': False, 'discarded': True, 'events': [],
                        'error': gateway.unreachable_message()}
        lines = [l.strip() for l in (err or '').splitlines()
                 if l.strip() and not l.startswith('Warning: Permanently added')]
        error = res.get('error') or (lines[-1] if lines else f'ssh exit {code}')[:300]
        fails = (st['fails'] or 0) + 1
        alarm, since = st['alarm'] or 0, st['since']
        if not alarm and fails >= config.PBX_UNREACHABLE_RUNS:
            alarm, since = 1, now_s
            events.append((EVENT_PBX_DOWN, pbx_node,
                           f"PBX {pbx['name']} not answering ({fails} checks in a row): {error}", 'warning'))
        with database.get_connection() as conn:
            conn.execute('''INSERT OR REPLACE INTO pbx_status (name, host, last_try, last_ok, error, error_kind, fails,
                                alarm, since, version, active_calls, active_channels, calls_processed, channels)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)''',
                         (pbx['name'], pbx['host'], now_s, st['last_ok'], error, kind, fails, alarm, since))
        return {'name': pbx['name'], 'ok': False, 'error': error, 'error_kind': kind,
                'hint': ERROR_HINTS.get(kind), 'events': _log(events, now_s)}

    if st['alarm']:
        events.append((EVENT_PBX_UP, pbx_node, f"PBX {pbx['name']} answering again", 'info'))
    eps = res['endpoints']
    with database.get_connection() as conn:
        old = {r['endpoint']: dict(r) for r in conn.execute('SELECT * FROM pbx_endpoints WHERE pbx = ?', (pbx['name'],))}
        for ep in eps:
            o = old.get(ep['name']) or {'last_registered': None, 'last_ip': None, 'fail_count': 0, 'alarm': 0,
                                        'since': None}
            fail_count, alarm, since = o['fail_count'] or 0, o['alarm'] or 0, o['since']
            last_reg, last_ip = o['last_registered'], o['last_ip']
            ip = ep['contact_ip'] or last_ip
            node = _owner(ip, node_ips)
            label = f"{ep['name']} ({ep['tech']}) on PBX {pbx['name']}"
            if ep['registered']:
                if alarm:
                    events.append((EVENT_PHONE_UP, node or pbx['name'],
                                   f"Phone {label} registered again" + (f" from {ep['contact_ip']}" if ep['contact_ip'] else ''),
                                   'info'))
                fail_count, alarm, since, last_reg = 0, 0, None, now_s
                last_ip = ep['contact_ip'] or last_ip
            elif last_reg:
                fail_count += 1
                if not alarm and fail_count >= config.PBX_UNREACHABLE_RUNS:
                    alarm, since = 1, now_s
                    where = f"; last from {last_ip}" + (f" on node {node}" if node else '') if last_ip else ''
                    events.append((EVENT_PHONE_DOWN, node or pbx['name'],
                                   f"Phone {label} not registered ({fail_count} checks in a row, state {ep['state']}){where}",
                                   'warning'))
            conn.execute('''INSERT OR REPLACE INTO pbx_endpoints (pbx, endpoint, tech, state, registered, contact_ip,
                                rtt_ms, node_name, last_registered, last_ip, fail_count, alarm, since, updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (pbx['name'], ep['name'], ep['tech'], ep['state'], int(ep['registered']), ep['contact_ip'],
                          ep['rtt_ms'], node, last_reg, last_ip, fail_count, alarm, since, now_s))
        gone = [n for n in old if n not in {e['name'] for e in eps}]
        conn.executemany('DELETE FROM pbx_endpoints WHERE pbx = ? AND endpoint = ?', [(pbx['name'], n) for n in gone])
        conn.execute('''INSERT OR REPLACE INTO pbx_status (name, host, last_try, last_ok, error, error_kind, fails, alarm,
                            since, version, active_calls, active_channels, calls_processed, channels)
                        VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, NULL, ?, ?, ?, ?, ?)''',
                     (pbx['name'], pbx['host'], now_s, now_s, res['version'], res['active_calls'],
                      res['active_channels'], res['calls_processed'], json.dumps(res['channels'])))
        reg = sum(1 for e in eps if e['registered'])
        conn.execute('INSERT OR REPLACE INTO pbx_samples VALUES (?, ?, ?, ?, ?)',
                     (pbx['name'], now_s, res['active_calls'], reg, len(eps) - reg))
        conn.execute('DELETE FROM pbx_samples WHERE timestamp < ?',
                     ((now - timedelta(days=config.PBX_HISTORY_DAYS)).strftime(TS_FMT),))
    return {'name': pbx['name'], 'ok': True, 'endpoints': len(eps), 'registered': reg,
            'active_calls': res['active_calls'], 'events': _log(events, now_s)}


def _log(events, now_s):
    out = []
    for kind, node, text, sev in events:
        database.log_event(kind, node, text, sev)
        out.append({'timestamp': now_s, 'event_type': kind, 'node_name': node, 'details': text, 'severity': sev})
    return out


def run(names=None, now=None):
    """Check every configured PBX (or `names`). Gated like the other mesh checks."""
    import gateway
    ensure_tables()
    hosts = [h for h in get_hosts() if not names or h['name'] in names]
    if not hosts:
        return {'results': [], 'events': [], 'aborted': 'no PBX configured'}
    if not ssh_collect.key_present():
        return {'results': [], 'events': [], 'aborted': f'No SSH key at {config.SSH_KEY_PATH}'}
    if not gateway.is_reachable():
        return {'results': [], 'events': [], 'aborted': gateway.unreachable_message()}
    results = [check(h, now=now) for h in hosts]
    return {'results': results, 'events': [e for r in results for e in r.get('events', [])], 'aborted': None}


def status():
    """Everything the VoIP page shows: config, per-PBX state, phones and calls."""
    ensure_tables()
    hosts = get_hosts()
    with database.get_connection() as conn:
        st = {r['name']: dict(r) for r in conn.execute('SELECT * FROM pbx_status')}
        eps = {}
        for r in conn.execute('SELECT * FROM pbx_endpoints ORDER BY pbx, endpoint'):
            eps.setdefault(r['pbx'], []).append(dict(r))
    pbxes = []
    for h in hosts:
        s = st.get(h['name']) or {}
        channels = json.loads(s['channels']) if s.get('channels') else []
        e = eps.get(h['name'], [])
        pbxes.append(dict(h, **{k: s.get(k) for k in ('last_try', 'last_ok', 'error', 'error_kind', 'fails', 'alarm',
                                                       'since', 'version', 'active_calls', 'active_channels',
                                                       'calls_processed')},
                          hint=ERROR_HINTS.get(s.get('error_kind')), channels=channels, endpoints=e,
                          registered=sum(1 for x in e if x['registered']),
                          unregistered_alarm=sum(1 for x in e if x['alarm'])))
    return {'enabled': config.PBX_STATUS_ENABLED, 'interval_minutes': config.PBX_INTERVAL_MINUTES,
            'key_present': ssh_collect.key_present(), 'public_key': ssh_collect.public_key(),
            'default_user': config.PBX_SSH_USER, 'default_port': config.PBX_SSH_PORT, 'pbxes': pbxes}

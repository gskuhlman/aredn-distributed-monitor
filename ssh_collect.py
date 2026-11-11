"""
Radio details over SSH from infrastructure nodes (Windows OpenSSH ssh.exe or
any OpenSSH client; no Python SSH library).

Key login only: `python ssh_collect.py --keygen` makes an ed25519 key pair in
SSH_KEY_PATH (no passphrase, since collection runs unattended) and prints the
public key, which the operator adds to each node's SSH keys (AREDN's SSH
listens on port 2222, user root). BatchMode and PasswordAuthentication=no mean
ssh never asks for or tries a password. Host keys are pinned on first contact
in SSH_KNOWN_HOSTS (StrictHostKeyChecking=accept-new); a changed key is
refused until the operator removes the old line. The private key never leaves
its file: not the database, settings, CouchDB, logs or any API (the API shows
only the public key).

Every SSH_INTERVAL_MINUTES (own APScheduler job, gated by gateway.py, one node
at a time) each infrastructure node (plus SSH_NODES) gets one session running
a fixed script on stdin -- never anything typed by a user:
  fast   `iw dev <if> station dump` and `iw dev <if> survey dump` per wireless
         interface -> SQLite `ssh_station_samples` (per station: signal per
         chain, tx/rx bitrate with MCS, streams and width, retries, failures)
         and `ssh_survey_samples` (the in-use channel's cumulative active /
         busy / receive / transmit time and noise; busy % from deltas);
         ath10k fw_reset_stats (firmware crashes / warm / cold restarts, kept
         by the driver: reading it never asks the firmware) and ath10k
         fw_stats (asks the firmware; a radio whose firmware doesn't answer
         within the driver's 1 s logs "failed to request fw stats: -145"
         each time -- in the node's syslog, which other operators read -- so
         after SSH_FW_STATS_BACKOFF_FAILS timeouts in a row it is retried
         only once every SSH_FW_STATS_RETRY_DAYS, on a slow run, until a read
         works again);
  slow   (every SSH_SLOW_HOURS, or on request) `iw phy`, ath10k fw_stats (not
         on a backed-off radio between its weekly retries) and
         ath9k ani/xmit/reset from debugfs when readable, the last 400 dmesg
         and 600 logread lines, the radio firmware (ath10k/ath11k version
         lines from dmesg; md5, size and whether the file was replaced in the
         overlay, `radiofw`) and `uci show` with every key / password /
         secret value replaced by ******** before it is stored -> SQLite
         `ssh_snapshots` (newest SSH_SNAPSHOT_KEEP per node and kind).
`iw scan` is never run: it takes the radio off its channel.

Alarms per (node, station), raised after 2 collections in a row and cleared
the same way, none raised in maintenance: `chain` -- the node hears the
station SSH_CHAIN_IMBALANCE_DB or more louder on one chain than the other,
and/or sends to it on one stream although the SNR (signal - survey noise) is
25 dB or more: on a 2x2 link usually one polarisation off (aim, connector,
cable). Events `radio_chain_alarm` (warning) / `radio_chain_cleared`
(`RF link to <peer>: ...`). Kernel / radio firmware errors in the logread
snapshot feed Mesh Health (`log_findings`).

Failures are recorded per node in `ssh_status` (auth = key not installed,
host_key = changed host key, timeout, dns, refused, algorithms, error); after
an auth or host-key failure a node is retried only every SSH_RETRY_HOURS
(manual collection always tries). Failures while the starting node is down
are discarded.
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from statistics import median

import config
import database
import link_diagnostics as ld
import maintenance

logger = logging.getLogger(__name__)
TS_FMT = '%Y-%m-%d %H:%M:%S'
MARK = '@@AREDNMON'
import iface_traffic  # noqa: E402  (after MARK: its script uses it)
EVENT_ALARM = 'radio_chain_alarm'
EVENT_CLEARED = 'radio_chain_cleared'
WATCH_RUNS = 2
ALARM_SAMPLES = 3
SINGLE_STREAM_SNR = 25
MIN_CHAIN_DBM = -95
REDACTED = '********'
# uci option names whose values are secrets (WiFi keys, tunnel passwords,
# WireGuard private keys, API tokens).
SECRET_OPTION = re.compile(r'(key|pass|psk|secret|token|priv|auth)', re.I)

FAST_SCRIPT = f'''export LC_ALL=C
echo "{MARK} sys"; echo "epoch $(date +%s)"; echo "load $(cat /proc/loadavg)"; echo "uptime $(cat /proc/uptime)"
echo "cores $(grep -c ^processor /proc/cpuinfo)"; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "{MARK} eth"
for i in /sys/class/net/eth* /sys/class/net/lan* /sys/class/net/wan*; do
  [ -e "$i/carrier" ] || continue
  echo "${{i##*/}} $(cat $i/speed 2>/dev/null || echo -) $(cat $i/duplex 2>/dev/null || echo -) $(cat $i/carrier 2>/dev/null || echo -) $(cat $i/carrier_changes 2>/dev/null || echo -) $(cat $i/statistics/rx_errors 2>/dev/null || echo -) $(cat $i/statistics/rx_crc_errors 2>/dev/null || echo -) $(cat $i/statistics/tx_errors 2>/dev/null || echo -)"
done
for d in $(iw dev 2>/dev/null | awk '$1=="Interface"{{print $2}}'); do
  echo "{MARK} station $d"; iw dev "$d" station dump 2>&1
  echo "{MARK} survey $d"; iw dev "$d" survey dump 2>&1
  echo "{MARK} info $d"; iw dev "$d" info 2>&1
done
for f in /sys/kernel/debug/ieee80211/*/ath10k/fw_reset_stats; do [ -r "$f" ] && {{ echo "{MARK} fw_reset $f"; cat "$f" 2>&1; }}; done
''' + iface_traffic.SCRIPT.format(mark=MARK)
# fw_stats (counters for receive errors, missing ACKs, PHY resets) is read
# every run unless the node is backed off (see the docstring), and kept as a
# snapshot on slow runs that read it.
FW_STATS_SCRIPT = f'''for f in /sys/kernel/debug/ieee80211/*/ath10k/fw_stats; do [ -r "$f" ] && {{ echo "{MARK} fw_stats $f"; cat "$f" 2>&1; }}; done
'''
# The shell would exit at the end of stdin anyway; `exit` ends the session
# right after the end marker so dropbear sees an orderly close.
END_SCRIPT = f'''echo "{MARK} end"
exit 0
'''
SLOW_SCRIPT = f'''echo "{MARK} phy"; iw phy 2>&1
for f in /sys/kernel/debug/ieee80211/*/ath9k/ani /sys/kernel/debug/ieee80211/*/ath9k/xmit /sys/kernel/debug/ieee80211/*/ath9k/reset; do [ -r "$f" ] && {{ echo "{MARK} ath9k $f"; cat "$f" 2>&1; }}; done
echo "{MARK} dmesg"; dmesg 2>&1 | tail -n 400
echo "{MARK} logread"; logread 2>&1 | tail -n 600
echo "{MARK} uci"; uci show 2>&1
echo "{MARK} radiofw"; dmesg 2>&1 | grep -E 'ath1[01]k.*(firmware ver| hw[0-9.]+ |CT firmware)' | tail -n 8
for f in /lib/firmware/ath10k/*/*/firmware-*.bin /lib/firmware/ath11k/*/*/*.bin; do [ -f "$f" ] && {{ md5sum "$f"; echo "size $f $(wc -c < "$f")"; [ -e "/overlay/upper$f" ] && echo "overlay $f"; }}; done
'''
SLOW_KINDS = ('phy', 'fw_stats', 'ath9k', 'dmesg', 'logread', 'uci', 'radiofw')

# (label, severity, pattern) for kernel / radio log lines.
LOG_PATTERNS = [
    ('radio firmware crash', 'warning', re.compile(r'firmware crashed|firmware.*(crash|halt)|ath10k.*(fw|firmware).*(error|fail)', re.I)),
    ('radio reset', 'info', re.compile(r'ath9k.*(reset|stuck|hang)|ath10k.*(restart|reset)', re.I)),
    ('out of memory', 'warning', re.compile(r'out of memory|oom-killer|invoked oom', re.I)),
    ('kernel oops', 'warning', re.compile(r'\boops\b|kernel panic|\bBUG:|call trace', re.I)),
    # Not bare 'watchdog': procd and AREDN's manager log '- watchdog -' and
    # 'hw_watchdog: Starting task' on every boot.
    ('watchdog / lockup', 'info', re.compile(r'\bwatchdog\b.*(reboot|reset|timeout|expired|bite|triggered)|soft lockup|hard lockup|hung task', re.I)),
    ('segfault', 'info', re.compile(r'segfault', re.I)),
    ('radar / DFS', 'info', re.compile(r'radar detected|\bDFS\b.*(radar|CAC)|\bCAC (started|finished|aborted)', re.I)),
    ('radio driver error', 'info', re.compile(r'ath1[01]k\S*.*\bfailed to\b', re.I)),
    ('link lost (deauth / beacon loss)', 'info',
     re.compile(r'deauthenticat|disassociat|beacon loss|CTRL-EVENT-DISCONNECTED|connection to AP .* lost', re.I)),
    ('Babel restart', 'info', re.compile(r'babeld.*\b(exit|terminat|restart|starting)', re.I)),
]
# Lines matched above that every node logs and that mean nothing: ath10k-ct
# messages on each association, and our own SSH logins (dropbear).
LOG_BENIGN = re.compile(r'failed to enable peer stats info: -95|pdev param 0 not supported|'
                        r'Firmware lacks feature flag|dropbear\[|'
                        # our own fw_stats read timing out (ssh_status.fw_stats_fails)
                        r'failed to request fw stats', re.I)

# Station columns added after the first release (ALTER TABLE on old databases).
STATION_EXTRA = (('connected_s', 'INTEGER'), ('beacon_loss', 'INTEGER'), ('rx_drop_misc', 'INTEGER'),
                 ('ack_signal', 'REAL'))
STATION_COLS = ('node_name', 'timestamp', 'iface', 'mac', 'peer', 'signal', 'chains', 'signal_avg', 'chains_avg',
                'tx_rate', 'tx_mcs', 'tx_nss', 'tx_width', 'tx_kind', 'rx_rate', 'rx_mcs', 'rx_nss', 'rx_width',
                'rx_kind', 'tx_packets', 'tx_retries', 'tx_failed', 'rx_packets', 'expected_mbps',
                'inactive_ms') + tuple(c for c, _ in STATION_EXTRA)
# ath10k fw_stats line label -> ssh_fw_samples column (first occurrence wins:
# 'MPDU queued' is the PDEV TX count, the VDEV block repeats it lower-case).
FW_FIELDS = {'Channel noise floor': 'noise_floor', 'Channel TX power': 'txpower_half_dbm',
             'PHY error count': 'phy_errors', 'RTS bad count': 'rts_bad', 'MPDUs delivered to HTT': 'rx_mpdus',
             'MPDUs delivered to stack': 'rx_mpdus_stack',
             'MPDU errors (FCS, MIC, ENC)': 'rx_mpdu_errors', 'MPDU queued': 'tx_mpdus',
             'MPDUs receive no ACK': 'mpdu_no_ack', 'ppdu aggr count': 'ppdu_aggr', 'ppdu noack': 'ppdu_noack',
             'Excessive retries': 'excessive_retries', 'PDEV resets': 'pdev_resets'}
# ath10k fw_reset_stats (driver counters since boot) -> ssh_fw_samples column.
FW_RESET_FIELDS = {'fw_crash_counter': 'fw_crashes', 'fw_warm_reset_counter': 'fw_warm_resets',
                   'fw_cold_reset_counter': 'fw_cold_resets'}

_lock = threading.Lock()
# Set on shutdown (app.py's SIGTERM handler): run() starts no further node, so
# the session in progress ends with its normal `exit` instead of being killed.
_stopping = threading.Event()


def stop():
    _stopping.set()


def busy():
    """True while a collection run holds the lock (a session may be open)."""
    return _lock.locked()


# ------------------------------------------------------------------ setup --

def ensure_tables():
    with database.get_connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS ssh_station_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, mac TEXT NOT NULL,
                peer TEXT, signal REAL, chains TEXT, signal_avg REAL, chains_avg TEXT,
                tx_rate REAL, tx_mcs INTEGER, tx_nss INTEGER, tx_width INTEGER, tx_kind TEXT,
                rx_rate REAL, rx_mcs INTEGER, rx_nss INTEGER, rx_width INTEGER, rx_kind TEXT,
                tx_packets INTEGER, tx_retries INTEGER, tx_failed INTEGER, rx_packets INTEGER,
                expected_mbps REAL, inactive_ms INTEGER,
                PRIMARY KEY (node_name, timestamp, iface, mac));
            CREATE INDEX IF NOT EXISTS idx_ssh_station_node_ts ON ssh_station_samples (node_name, timestamp);
            CREATE TABLE IF NOT EXISTS ssh_survey_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, frequency INTEGER NOT NULL,
                noise REAL, active_ms INTEGER, busy_ms INTEGER, rx_ms INTEGER, tx_ms INTEGER,
                PRIMARY KEY (node_name, timestamp, iface, frequency));
            CREATE TABLE IF NOT EXISTS ssh_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, node_name TEXT NOT NULL, timestamp DATETIME NOT NULL,
                kind TEXT NOT NULL, label TEXT, text TEXT);
            CREATE INDEX IF NOT EXISTS idx_ssh_snapshots_node ON ssh_snapshots (node_name, kind, timestamp);
            CREATE TABLE IF NOT EXISTS ssh_status (
                node_name TEXT PRIMARY KEY, last_try DATETIME, last_ok DATETIME, last_slow DATETIME,
                error TEXT, error_kind TEXT, fails INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ssh_alarm_state (
                kind TEXT NOT NULL, node_name TEXT NOT NULL, peer TEXT NOT NULL, active INTEGER DEFAULT 0,
                bad_count INTEGER DEFAULT 0, good_count INTEGER DEFAULT 0, since DATETIME, detail TEXT,
                PRIMARY KEY (kind, node_name, peer));
            CREATE TABLE IF NOT EXISTS ssh_node_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, load1 REAL, load5 REAL, load15 REAL,
                cores INTEGER, mem_total_kb INTEGER, mem_avail_kb INTEGER, uptime_s REAL, clock_offset_s REAL,
                PRIMARY KEY (node_name, timestamp));
            CREATE TABLE IF NOT EXISTS ssh_eth_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, speed INTEGER,
                duplex TEXT, carrier INTEGER, carrier_changes INTEGER, rx_errors INTEGER, rx_crc_errors INTEGER,
                tx_errors INTEGER, PRIMARY KEY (node_name, timestamp, iface));
            CREATE TABLE IF NOT EXISTS ssh_radio_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, iface TEXT NOT NULL, wiphy INTEGER,
                type TEXT, frequency INTEGER, width INTEGER, center1 INTEGER, txpower_dbm REAL,
                PRIMARY KEY (node_name, timestamp, iface));
            CREATE TABLE IF NOT EXISTS ssh_fw_samples (
                node_name TEXT NOT NULL, timestamp DATETIME NOT NULL, phy TEXT NOT NULL, noise_floor INTEGER,
                txpower_half_dbm INTEGER, phy_errors INTEGER, rts_bad INTEGER, rx_mpdus INTEGER,
                rx_mpdu_errors INTEGER, tx_mpdus INTEGER, mpdu_no_ack INTEGER, ppdu_aggr INTEGER,
                ppdu_noack INTEGER, excessive_retries INTEGER, pdev_resets INTEGER,
                PRIMARY KEY (node_name, timestamp, phy));
            CREATE TABLE IF NOT EXISTS ssh_settings (
                node_name TEXT PRIMARY KEY, timestamp DATETIME, radio TEXT, mode TEXT, channel TEXT,
                chanbw TEXT, htmode TEXT, country TEXT, distance_m INTEGER, txpower_set REAL,
                coverage_class INTEGER, coverage_m INTEGER, lqm TEXT);
        ''')
        have = {r[1] for r in conn.execute('PRAGMA table_info(ssh_station_samples)')}
        for col, kind in STATION_EXTRA:
            if col not in have:
                conn.execute(f'ALTER TABLE ssh_station_samples ADD COLUMN {col} {kind}')
        if 'rx_mpdus_stack' not in {r[1] for r in conn.execute('PRAGMA table_info(ssh_fw_samples)')}:
            conn.execute('ALTER TABLE ssh_fw_samples ADD COLUMN rx_mpdus_stack INTEGER')
        have = {r[1] for r in conn.execute('PRAGMA table_info(ssh_fw_samples)')}
        for col in FW_RESET_FIELDS.values():
            if col not in have:
                conn.execute(f'ALTER TABLE ssh_fw_samples ADD COLUMN {col} INTEGER')
        if 'severity' not in {r[1] for r in conn.execute('PRAGMA table_info(ssh_alarm_state)')}:
            conn.execute('ALTER TABLE ssh_alarm_state ADD COLUMN severity TEXT')
        cols = {r[1] for r in conn.execute('PRAGMA table_info(ssh_status)')}
        if 'fw_stats_fails' not in cols:
            conn.execute('ALTER TABLE ssh_status ADD COLUMN fw_stats_fails INTEGER DEFAULT 0')
        if 'fw_stats_tried' not in cols:
            conn.execute('ALTER TABLE ssh_status ADD COLUMN fw_stats_tried TEXT')


def _exe(name):
    win = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'OpenSSH', name + '.exe')
    return win if sys.platform == 'win32' and os.path.exists(win) else name


def ssh_exe():
    return config.SSH_EXE or _exe('ssh')


def key_present():
    return os.path.exists(config.SSH_KEY_PATH)


def enabled():
    return bool(config.SSH_COLLECT_ENABLED and key_present())


def public_key():
    try:
        with open(config.SSH_KEY_PATH + '.pub', encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return None


def keygen():
    """Make the key pair (refuses to overwrite one). Returns the public key."""
    if key_present():
        raise FileExistsError(f'{config.SSH_KEY_PATH} already exists')
    os.makedirs(os.path.dirname(config.SSH_KEY_PATH) or '.', exist_ok=True)
    subprocess.run([config.SSH_KEYGEN_EXE or _exe('ssh-keygen'), '-q', '-t', 'ed25519', '-N', '',
                    # AREDN may ignore an ed25519 key without a user@host comment.
                    '-C', f'aredn-monitor@{socket.gethostname()}', '-f', config.SSH_KEY_PATH], check=True, capture_output=True)
    return public_key()


def targets():
    names = set(database.get_infrastructure_nodes())
    names.update(n.strip() for n in (config.SSH_NODES or '').split(',') if n.strip())
    return sorted(names)


# -------------------------------------------------------------- transport --

def _ssh_args(node):
    return [ssh_exe(), '-T', '-p', str(config.SSH_PORT), '-i', config.SSH_KEY_PATH,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={config.SSH_KNOWN_HOSTS}',
            '-o', f'HostKeyAlias={node}', '-o', f'ConnectTimeout={config.SSH_CONNECT_TIMEOUT_SECONDS}',
            '-o', 'ServerAliveInterval=10',
            f'{config.SSH_USER}@{node}.local.mesh', 'sh -s']


def _subprocess(args, script):
    """Run ssh with stdin/stdout/stderr on temporary files, not pipes:
    subprocess.run's pipe readers are threads, which eventlet makes green, and
    in a tpool thread they never run -- a node that answers with more than a
    pipe buffer blocks ssh forever and the timeout never fires. Files in the
    ssh/ folder (uci output stays on this PC), deleted on close."""
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    folder = os.path.dirname(config.SSH_KNOWN_HOSTS) or '.'
    with tempfile.TemporaryFile(dir=folder) as fin, tempfile.TemporaryFile(dir=folder) as fout,             tempfile.TemporaryFile(dir=folder) as ferr:
        fin.write(script.encode())
        fin.seek(0)
        p = subprocess.Popen(args, stdin=fin, stdout=fout, stderr=ferr, **kwargs)
        try:
            p.wait(timeout=config.SSH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise
        fout.seek(0)
        ferr.seek(0)
        return subprocess.CompletedProcess(args, p.returncode, fout.read(), ferr.read())


def _run_ssh(node, script):
    """(returncode, stdout, stderr). Tests replace this. Runs in a real
    thread under eventlet (like rf_stats' ping) so the web app keeps going."""
    os.makedirs(os.path.dirname(config.SSH_KNOWN_HOSTS) or '.', exist_ok=True)
    try:
        import eventlet.patcher
        import eventlet.tpool
        green = eventlet.patcher.is_monkey_patched('thread')
    except ImportError:
        green = False
    args = _ssh_args(node)
    p = eventlet.tpool.execute(_subprocess, args, script) if green else _subprocess(args, script)
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


def classify_error(stderr):
    s = (stderr or '').lower()
    for kind, words in (('host_key', ('identification has changed', 'host key verification failed')),
                        ('auth', ('permission denied',)),
                        ('dns', ('could not resolve', 'name or service not known', 'no such host')),
                        ('refused', ('connection refused',)),
                        ('timeout', ('timed out', 'timeout')),
                        ('algorithms', ('no matching', 'unable to negotiate'))):
        if any(w in s for w in words):
            return kind
    return 'error'


ERROR_HINTS = {
    'auth': "the node refused the key: add the public key to the node's SSH keys",
    'host_key': "the node's host key changed (reinstall or a different device?): if expected, remove its line "
                "from the known_hosts file and collect again",
    'dns': 'the name does not resolve from this PC',
    'refused': 'nothing answers on the SSH port',
    'timeout': 'no answer in time',
    'algorithms': "the node's SSH server and this ssh.exe share no key or cipher type",
}


# ---------------------------------------------------------------- parsing --

def split_sections(text):
    """[(kind, arg, body)] from the MARK lines of a script's output."""
    out, cur = [], None
    for line in (text or '').splitlines():
        if line.startswith(MARK + ' '):
            parts = line[len(MARK) + 1:].split(' ', 1)
            cur = [parts[0], parts[1] if len(parts) > 1 else '', []]
            out.append(cur)
        elif cur is not None:
            cur[2].append(line)
    return [(k, a, '\n'.join(b)) for k, a, b in out]


def _signal(value):
    """'-68 [-66, -79] dBm' -> (-68.0, [-66.0, -79.0]). ath10k sometimes
    reports nonsense per-chain values ('-56 [90, 83]', '-54 [-27, 50]'); a
    chain at 0 dBm or above, or more than 10 dB louder than the combined
    signal, becomes None."""
    m = re.match(r'\s*(-?\d+)\s*(?:\[([^\]]*)\])?', value or '')
    if not m:
        return None, []
    total = float(m.group(1))
    chains = [float(x) for x in re.findall(r'-?\d+', m.group(2) or '')]
    return total, [c if c < 0 and c <= total + 10 else None for c in chains]


def rx_chains(phy_text):
    """Receive chains from `iw phy` ('Configured Antennas: TX 0x3 RX 0x3' ->
    2), the most of any radio on the node; None when not listed."""
    masks = re.findall(r'Configured Antennas:.*?RX 0x([0-9a-f]+)', phy_text or '', re.I) or \
        re.findall(r'Available Antennas:.*?RX 0x([0-9a-f]+)', phy_text or '', re.I)
    return max(bin(int(m, 16)).count('1') for m in masks) if masks else None


def parse_bitrate(value):
    """'54.0 MBit/s VHT-MCS 3 40MHz VHT-NSS 1' -> {rate, mcs, nss, width, kind}."""
    out = {'rate': None, 'mcs': None, 'nss': None, 'width': None, 'kind': None}
    m = re.match(r'\s*([\d.]+)\s*MBit/s(.*)', value or '')
    if not m:
        return out
    out['rate'], rest = float(m.group(1)), m.group(2)
    for kind in ('EHT', 'HE', 'VHT'):
        mm = re.search(kind + r'-MCS (\d+)', rest)
        if mm:
            nss = re.search(kind + r'-NSS (\d+)', rest)
            out.update(kind=kind, mcs=int(mm.group(1)), nss=int(nss.group(1)) if nss else 1)
            break
    else:
        mm = re.search(r'\bMCS (\d+)', rest)
        if mm:
            idx = int(mm.group(1))
            out.update(kind='HT', mcs=idx % 8, nss=idx // 8 + 1)
        else:
            out['kind'] = 'legacy'
    w = re.search(r'(\d+)MHz', rest)
    out['width'] = int(w.group(1)) if w else (20 if out['kind'] else None)
    return out


def _int(v):
    m = re.match(r'\s*(-?\d+)', v or '')
    return int(m.group(1)) if m else None


def parse_station_dump(text, iface=''):
    """Stations of `iw dev <if> station dump`: [{mac, signal, chains, ...}]."""
    out, cur = [], None
    for line in (text or '').splitlines():
        m = re.match(r'Station ([0-9a-fA-F:]{17})', line)
        if m:
            cur = {'mac': m.group(1).lower(), 'iface': iface}
            out.append(cur)
            continue
        if cur is None or ':' not in line:
            continue
        key, _, value = line.strip().partition(':')
        key = key.strip()
        if key == 'signal':
            cur['signal'], cur['chains'] = _signal(value)
        elif key == 'signal avg':
            cur['signal_avg'], cur['chains_avg'] = _signal(value)
        elif key in ('tx bitrate', 'rx bitrate'):
            b = parse_bitrate(value)
            d = key[:2]
            cur.update({f'{d}_rate': b['rate'], f'{d}_mcs': b['mcs'], f'{d}_nss': b['nss'],
                        f'{d}_width': b['width'], f'{d}_kind': b['kind']})
        elif key in ('tx packets', 'tx retries', 'tx failed', 'rx packets'):
            cur[key.replace(' ', '_')] = _int(value)
        elif key == 'inactive time':
            cur['inactive_ms'] = _int(value)
        elif key == 'expected throughput':
            mm = re.match(r'\s*([\d.]+)', value)
            cur['expected_mbps'] = float(mm.group(1)) if mm else None
        elif key == 'connected time':
            cur['connected_s'] = _int(value)
        elif key in ('beacon loss', 'rx drop misc'):
            cur[key.replace(' ', '_')] = _int(value)
        elif key == 'avg ack signal':
            cur['ack_signal'] = _int(value)
    return out


def parse_sys(text):
    """The `sys` section: node clock, load, uptime, cores and memory."""
    out = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            if parts[0] == 'epoch':
                out['epoch'] = int(parts[1])
            elif parts[0] == 'load' and len(parts) >= 4:
                out['load1'], out['load5'], out['load15'] = map(float, parts[1:4])
            elif parts[0] == 'uptime':
                out['uptime_s'] = float(parts[1])
            elif parts[0] == 'cores':
                out['cores'] = int(parts[1])
            elif parts[0] in ('MemTotal:', 'MemAvailable:'):
                out['mem_total_kb' if parts[0] == 'MemTotal:' else 'mem_avail_kb'] = int(parts[1])
        except ValueError:
            continue
    return out


def parse_eth(text):
    """The `eth` section: one line per wired port (name speed duplex carrier
    carrier_changes rx_errors rx_crc_errors tx_errors, '-' when unreadable)."""
    out = []
    num = lambda v: int(v) if re.fullmatch(r'-?\d+', v) else None
    for line in (text or '').splitlines():
        p = line.split()
        if len(p) != 8:
            continue
        speed = num(p[1])
        out.append({'iface': p[0], 'speed': speed if speed and speed > 0 else None,
                    'duplex': None if p[2] in ('-', 'unknown') else p[2], 'carrier': num(p[3]),
                    'carrier_changes': num(p[4]), 'rx_errors': num(p[5]), 'rx_crc_errors': num(p[6]),
                    'tx_errors': num(p[7])})
    return out


def parse_iw_info(text, iface=''):
    """`iw dev <if> info`: wiphy, type, channel, width, centre and TX power."""
    out = {'iface': iface, 'wiphy': None, 'type': None, 'frequency': None, 'width': None, 'center1': None,
           'txpower_dbm': None}
    for line in (text or '').splitlines():
        s = line.strip()
        m = re.match(r'wiphy (\d+)$', s)
        if m:
            out['wiphy'] = int(m.group(1))
        m = re.match(r'type (.+)$', s)
        if m:
            out['type'] = m.group(1)
        m = re.match(r'channel \d+ \((\d+) MHz\), width: (\d+) MHz(?:, center1: (\d+) MHz)?', s)
        if m:
            out['frequency'], out['width'] = int(m.group(1)), int(m.group(2))
            out['center1'] = int(m.group(3)) if m.group(3) else None
        m = re.match(r'txpower ([\d.]+) dBm', s)
        if m:
            out['txpower_dbm'] = float(m.group(1))
    return out


def parse_fw_stats(text):
    """Counters from ath10k fw_stats (FW_FIELDS); missing ones are None.
    'FCS bad count' is not used: a 16-bit counter stuck at 65535 on QCA4019."""
    out = dict.fromkeys(FW_FIELDS.values())
    for line in (text or '').splitlines():
        m = re.match(r'\s*(.+?)\s+(-?\d+)\s*$', line)
        if m and m.group(1) in FW_FIELDS and out[FW_FIELDS[m.group(1)]] is None:
            out[FW_FIELDS[m.group(1)]] = int(m.group(2))
    return out


def parse_fw_reset_stats(text):
    """{fw_crashes, fw_warm_resets, fw_cold_resets} from ath10k fw_reset_stats
    ('fw_crash_counter\t\t3'). A warm restart also happens on every normal
    interface restart (AREDN's daily mesh restart, a settings change)."""
    out = dict.fromkeys(FW_RESET_FIELDS.values())
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in FW_RESET_FIELDS and parts[1].isdigit():
            out[FW_RESET_FIELDS[parts[0]]] = int(parts[1])
    return out


def fw_stats_failed(text):
    """True when an fw_stats read gave no counters (the firmware didn't answer:
    'cat: can't open ...: Operation timed out')."""
    return not any(v is not None for v in parse_fw_stats(text).values())


def node_script(slow, fw_stats=True):
    """The fixed script for one session: the fast part, fw_stats when asked
    off (always on slow runs), the slow part on slow runs."""
    return FAST_SCRIPT + (FW_STATS_SCRIPT if fw_stats else '') + (SLOW_SCRIPT if slow else '') + END_SCRIPT


def parse_coverage(phy_text):
    """{wiphy index: (coverage class, metres)} from `iw phy`
    ('Coverage class: 13 (up to 5850m)'): the radio's ACK window."""
    out, cur = {}, None
    for line in (phy_text or '').splitlines():
        m = re.match(r'Wiphy phy(\d+)', line.strip())
        if m:
            cur = int(m.group(1))
        m = re.search(r'Coverage class: (\d+) \(up to (\d+)m\)', line)
        if m and cur is not None:
            out[cur] = (int(m.group(1)), int(m.group(2)))
    return out


def parse_uci_settings(text):
    """The mesh radio's settings from `uci show`: the wifi-iface on network
    'wifi'/'wifi0' and its wifi-device (channel, width, country, distance, TX power),
    plus the aredn LQM options. None when there is no mesh interface."""
    opts = {}
    for line in (text or '').splitlines():
        m = re.match(r"([\w@\[\]-]+)\.([\w@\[\]-]+)\.(\w+)='?(.*?)'?$", line.strip())
        if m:
            opts.setdefault((m.group(1), m.group(2)), {})[m.group(3)] = m.group(4)
    # network 'wifi' in PtP/PtMP/Station mode, 'wifi0' in Mesh mode
    iface = next((v for (cfg, sec), v in sorted(opts.items())
                  if cfg == 'wireless' and re.fullmatch(r'wifi\d*', v.get('network') or '')), None)
    if not iface:
        return None
    radio_name = iface.get('device') or 'radio0'
    radio = opts.get(('wireless', radio_name), {})
    lqm = next((v for (cfg, sec), v in opts.items() if cfg == 'aredn' and sec.startswith('@lqm')), {})
    num = lambda v: float(v) if v and re.fullmatch(r'-?[\d.]+', v) else None
    dist = num(radio.get('distance'))
    return {'radio': radio_name, 'mode': iface.get('mode'), 'channel': radio.get('channel'),
            'chanbw': radio.get('chanbw'), 'htmode': radio.get('htmode'), 'country': radio.get('country'),
            'distance_m': int(dist) if dist is not None else None, 'txpower_set': num(radio.get('txpower')),
            'lqm': lqm}


def parse_survey_dump(text, iface=''):
    """In-use channel entries of `iw dev <if> survey dump`."""
    out, cur = [], None
    for line in (text or '').splitlines():
        s = line.strip()
        if s.startswith('frequency:'):
            m = re.match(r'frequency:\s*(\d+)\s*MHz(.*)', s)
            cur = {'iface': iface, 'frequency': int(m.group(1)), 'in_use': 'in use' in m.group(2)} if m else None
            if cur:
                out.append(cur)
        elif cur is not None and ':' in s:
            key, _, value = s.partition(':')
            field = {'noise': 'noise', 'channel active time': 'active_ms', 'channel busy time': 'busy_ms',
                     'channel receive time': 'rx_ms', 'channel transmit time': 'tx_ms'}.get(key.strip())
            if field:
                cur[field] = _int(value)
    return [x for x in out if x['in_use']]


def redact_uci(text):
    """`uci show` with secret option values replaced."""
    lines = []
    for line in (text or '').splitlines():
        name, eq, _ = line.partition('=')
        option = name.rsplit('.', 1)[-1]
        lines.append(f'{name}={REDACTED}' if eq and '.' in name and SECRET_OPTION.search(option) else line)
    return '\n'.join(lines)


# ------------------------------------------------------------- collection --

def _peer_names(conn, node):
    names = {}
    for r in conn.execute('''SELECT target_node, mac_address FROM links WHERE source_node = ?
                             AND mac_address IS NOT NULL AND target_node NOT LIKE 'mac-%' ''', (node,)):
        key = ld._mac_key(r['mac_address'])
        if key:
            names[key] = r['target_node']
    return names


def _status(conn, node):
    r = conn.execute('SELECT * FROM ssh_status WHERE node_name = ?', (node,)).fetchone()
    return dict(r) if r else {'node_name': node, 'last_try': None, 'last_ok': None, 'last_slow': None,
                              'error': None, 'error_kind': None, 'fails': 0, 'fw_stats_fails': 0,
                              'fw_stats_tried': None}


STATUS_COLS = ('node_name', 'last_try', 'last_ok', 'last_slow', 'error', 'error_kind', 'fails', 'fw_stats_fails',
               'fw_stats_tried')


def _save_status(conn, st):
    conn.execute(f"INSERT OR REPLACE INTO ssh_status ({', '.join(STATUS_COLS)}) "
                 f"VALUES ({', '.join('?' * len(STATUS_COLS))})", [st.get(c) for c in STATUS_COLS])


def fw_stats_backed_off(st):
    return (st.get('fw_stats_fails') or 0) >= config.SSH_FW_STATS_BACKOFF_FAILS


def read_fw_stats(st, slow, now):
    """Whether this run reads fw_stats: always, unless backed off; a backed-off
    radio only on a slow run once every SSH_FW_STATS_RETRY_DAYS (each try that
    times out logs another -145 on the node)."""
    if not fw_stats_backed_off(st):
        return True
    tried = st.get('fw_stats_tried')
    return bool(slow) and (not tried or datetime.strptime(tried, TS_FMT)
                           <= now - timedelta(days=config.SSH_FW_STATS_RETRY_DAYS))


def _due(st, now, manual):
    if manual or st['error_kind'] not in ('auth', 'host_key') or not st['last_try']:
        return True
    return datetime.strptime(st['last_try'], TS_FMT) <= now - timedelta(hours=config.SSH_RETRY_HOURS)


def _slow_due(st, now):
    return not st['last_slow'] or datetime.strptime(st['last_slow'], TS_FMT) <= now - timedelta(hours=config.SSH_SLOW_HOURS)


def collect_node(node, slow=None, manual=False, now=None):
    """One SSH session to `node`. Returns {node, ok, stations, surveys,
    snapshots, error?, error_kind?, skipped?}."""
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    with database.get_connection() as conn:
        st = _status(conn, node)
    if not _due(st, now, manual):
        return {'node': node, 'ok': False, 'skipped': True, 'error_kind': st['error_kind'], 'error': st['error']}
    slow = _slow_due(st, now) if slow is None else slow
    fw_try = read_fw_stats(st, slow, now)
    try:
        code, out, err = _run_ssh(node, node_script(slow, fw_stats=fw_try))
    except subprocess.TimeoutExpired:
        code, out, err = None, '', 'timed out'
    except OSError as exc:
        code, out, err = None, '', f'could not run ssh: {exc}'
    sections = split_sections(out)
    if not sections or sections[-1][0] != 'end':
        kind = classify_error(err)
        if kind in ('timeout', 'dns', 'refused', 'error'):
            import gateway
            if gateway.failure_is_ours():
                return {'node': node, 'ok': False, 'discarded': True, 'error': gateway.unreachable_message()}
        lines = [l.strip() for l in (err or '').splitlines() if l.strip() and not l.startswith('Warning: Permanently added')]
        st.update(last_try=now_s, error=(lines[-1] if lines else f'ssh exit {code}')[:300], error_kind=kind, fails=(st['fails'] or 0) + 1)
        with database.get_connection() as conn:
            _save_status(conn, st)
        return {'node': node, 'ok': False, 'error': st['error'], 'error_kind': kind,
                'hint': ERROR_HINTS.get(kind)}
    received = time.time()
    stations, surveys, snaps, infos, fws, eth, sysinfo, uci = [], [], [], [], {}, [], {}, None
    fw_read = fw_ok = False
    for kind, arg, body in sections:
        if kind == 'station':
            stations += parse_station_dump(body, arg)
        elif kind == 'survey':
            surveys += parse_survey_dump(body, arg)
        elif kind == 'info':
            infos.append(parse_iw_info(body, arg))
        elif kind == 'sys':
            sysinfo = parse_sys(body)
        elif kind == 'eth':
            eth = parse_eth(body)
        elif kind in ('fw_stats', 'fw_reset'):
            m = re.search(r'/(phy\d+)/', arg or '')
            row = fws.setdefault(m.group(1) if m else (arg or 'phy'), {})
            if kind == 'fw_reset':
                row.update(parse_fw_reset_stats(body))
            else:
                fw_read = True
                fw_ok = fw_ok or not fw_stats_failed(body)
                row.update({k: v for k, v in parse_fw_stats(body).items() if v is not None})
        if kind == 'uci':
            uci = parse_uci_settings(body)
        if kind in SLOW_KINDS and (slow or kind != 'fw_stats'):
            snaps.append((kind, arg or None, redact_uci(body) if kind == 'uci' else body))
    for kind, _, body in snaps:
        if kind == 'logread':
            import syslog_rx
            syslog_rx.ingest_logread(node, body)
    with database.get_connection() as conn:
        _store_extras(conn, node, now_s, sysinfo, received, eth, infos, fws, uci, snaps)
        iface_traffic.store(conn, node, now_s, sections)
        names = _peer_names(conn, node)
        # ath10k lists four chain values on 2x2 radios (the SXTsq 5 ac: '-64 [-64, -65, -79, -79]');
        # keep the radio's receive chains (AREDN radios are 1x1 or 2x2 when its iw phy isn't known).
        phy = next((t for k, _, t in snaps if k == 'phy'), None)
        if phy is None:
            row = conn.execute('''SELECT text FROM ssh_snapshots WHERE node_name = ? AND kind = 'phy'
                                  ORDER BY timestamp DESC, id DESC LIMIT 1''', (node,)).fetchone()
            phy = row[0] if row else None
        n_chains = rx_chains(phy) or 2
        for s in stations:
            s['chains'] = (s.get('chains') or [])[:n_chains]
            s['chains_avg'] = (s.get('chains_avg') or [])[:n_chains]
            row = dict(s, node_name=node, timestamp=now_s, peer=names.get(ld._mac_key(s['mac'])),
                       chains=json.dumps(s.get('chains') or []), chains_avg=json.dumps(s.get('chains_avg') or []))
            conn.execute(f"INSERT OR REPLACE INTO ssh_station_samples ({', '.join(STATION_COLS)}) "
                         f"VALUES ({', '.join('?' * len(STATION_COLS))})", [row.get(c) for c in STATION_COLS])
        for v in surveys:
            conn.execute('INSERT OR REPLACE INTO ssh_survey_samples VALUES (?,?,?,?,?,?,?,?,?)',
                         (node, now_s, v['iface'], v['frequency'], v.get('noise'), v.get('active_ms'),
                          v.get('busy_ms'), v.get('rx_ms'), v.get('tx_ms')))
        for kind, label, text in snaps:
            conn.execute('INSERT INTO ssh_snapshots (node_name, timestamp, kind, label, text) VALUES (?,?,?,?,?)',
                         (node, now_s, kind, label, text))
            conn.execute('''DELETE FROM ssh_snapshots WHERE node_name = ? AND kind = ? AND COALESCE(label, '') = ?
                            AND id NOT IN (SELECT id FROM ssh_snapshots WHERE node_name = ? AND kind = ?
                                           AND COALESCE(label, '') = ? ORDER BY timestamp DESC, id DESC LIMIT ?)''',
                         (node, kind, label or '', node, kind, label or '', config.SSH_SNAPSHOT_KEEP))
        st.update(last_try=now_s, last_ok=now_s, error=None, error_kind=None, fails=0)
        if fw_read:
            st['fw_stats_fails'] = 0 if fw_ok else (st.get('fw_stats_fails') or 0) + 1
        if fw_try:
            st['fw_stats_tried'] = now_s
        if slow:
            st['last_slow'] = now_s
        _save_status(conn, st)
    return {'node': node, 'ok': True, 'stations': len(stations), 'surveys': len(surveys), 'snapshots': len(snaps)}


def _mesh_info(infos):
    """The mesh radio's `iw dev info`: the first interface on a channel that
    isn't an AP/VLAN (WDS) sub-interface."""
    on = [i for i in infos if i.get('frequency')]
    return next((i for i in on if i.get('type') != 'AP/VLAN'), on[0] if on else None)


def _store_extras(conn, node, now_s, sysinfo, received, eth, infos, fws, uci, snaps):
    """Node, Ethernet, radio, fw_stats samples and (slow runs) settings."""
    if sysinfo:
        offset = round(sysinfo['epoch'] - received, 1) if sysinfo.get('epoch') else None
        conn.execute('INSERT OR REPLACE INTO ssh_node_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (node, now_s, sysinfo.get('load1'), sysinfo.get('load5'), sysinfo.get('load15'),
                      sysinfo.get('cores'), sysinfo.get('mem_total_kb'), sysinfo.get('mem_avail_kb'),
                      sysinfo.get('uptime_s'), offset))
    for e in eth:
        conn.execute('INSERT OR REPLACE INTO ssh_eth_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (node, now_s, e['iface'], e['speed'], e['duplex'], e['carrier'], e['carrier_changes'],
                      e['rx_errors'], e['rx_crc_errors'], e['tx_errors']))
    for i in infos:
        conn.execute('INSERT OR REPLACE INTO ssh_radio_samples VALUES (?,?,?,?,?,?,?,?,?)',
                     (node, now_s, i['iface'], i['wiphy'], i['type'], i['frequency'], i['width'], i['center1'],
                      i['txpower_dbm']))
    cols = list(FW_FIELDS.values()) + list(FW_RESET_FIELDS.values())
    for phy, v in fws.items():
        if not any(v.get(c) is not None for c in cols):
            continue
        conn.execute(f"INSERT OR REPLACE INTO ssh_fw_samples (node_name, timestamp, phy, {', '.join(cols)}) "
                     f"VALUES ({', '.join('?' * (len(cols) + 3))})", [node, now_s, phy] + [v.get(c) for c in cols])
    phy = next((t for k, _, t in snaps if k == 'phy'), None)
    if not uci and conn.execute('SELECT 1 FROM ssh_settings WHERE node_name = ?', (node,)).fetchone() is None:
        # Never read: use the newest stored snapshots (uci is stored redacted,
        # which leaves the settings read here intact) instead of a slow run.
        stored = {r['kind']: r['text'] for r in conn.execute(
            '''SELECT kind, text FROM ssh_snapshots WHERE node_name = ? AND kind IN ('uci', 'phy')
               ORDER BY timestamp, id''', (node,))}
        uci = parse_uci_settings(stored.get('uci'))
        phy = phy or stored.get('phy')
    if uci:
        mesh = _mesh_info(infos)
        cov = parse_coverage(phy).get(mesh['wiphy']) if mesh and mesh.get('wiphy') is not None else None
        conn.execute('INSERT OR REPLACE INTO ssh_settings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (node, now_s, uci['radio'], uci['mode'], uci['channel'], uci['chanbw'], uci['htmode'],
                      uci['country'], uci['distance_m'], uci['txpower_set'], cov[0] if cov else None,
                      cov[1] if cov else None, json.dumps(uci['lqm'], sort_keys=True)))


def run(nodes=None, slow=None, manual=False):
    """Collect from `nodes` (default every target), then evaluate alarms.
    Skipped while another run is going or the starting node is down."""
    import gateway
    if not _lock.acquire(blocking=False):
        return {'busy': True, 'results': [], 'events': []}
    try:
        ensure_tables()
        if not key_present():
            return {'results': [], 'events': [], 'error': f'No SSH key at {config.SSH_KEY_PATH}'}
        if not gateway.is_reachable():
            return {'results': [], 'events': [], 'error': gateway.unreachable_message()}
        now = datetime.now()
        results = []
        for n in (nodes or targets()):
            if _stopping.is_set():
                return {'results': results, 'events': [], 'stopped': True}
            results.append(collect_node(n, slow=slow, manual=manual, now=now))
        events = evaluate(now)
        _prune(now)
        return {'results': results, 'events': events}
    finally:
        _lock.release()


def _prune(now):
    cutoff = (now - timedelta(hours=config.HISTORY_RETENTION_HOURS)).strftime(TS_FMT)
    with database.get_connection() as conn:
        for table in ('ssh_station_samples', 'ssh_survey_samples', 'ssh_node_samples', 'ssh_eth_samples',
                      'ssh_radio_samples', 'ssh_fw_samples'):
            conn.execute(f'DELETE FROM {table} WHERE timestamp < ?', (cutoff,))
        iface_traffic.cleanup(conn, cutoff)


def install(scheduler, job):
    if not config.SSH_COLLECT_ENABLED:
        return
    scheduler.add_job(job, 'interval', minutes=config.SSH_INTERVAL_MINUTES, id='ssh_collect',
                      replace_existing=True, next_run_time=datetime.now() + timedelta(minutes=2))


# ----------------------------------------------------------------- alarms --

def _noise(conn, node, since):
    vals = [r[0] for r in conn.execute('''SELECT noise FROM ssh_survey_samples WHERE node_name = ? AND timestamp >= ?
                                          AND noise IS NOT NULL''', (node, since))]
    return median(vals) if vals else None


def station_conditions(conn, now):
    """{(node, peer): detail} for stations with a chain imbalance or on one
    stream at a good SNR over their last ALARM_SAMPLES collections."""
    since = (now - timedelta(minutes=config.SSH_INTERVAL_MINUTES * (ALARM_SAMPLES + 1) + 5)).strftime(TS_FMT)
    rows = {}
    for r in conn.execute('''SELECT * FROM ssh_station_samples WHERE timestamp >= ?
                             ORDER BY timestamp DESC''', (since,)):
        rows.setdefault((r['node_name'], r['peer'] or r['mac']), []).append(dict(r))
    noise = {}
    out = {}
    for (node, peer), samples in rows.items():
        samples = samples[:ALARM_SAMPLES]
        if len(samples) < ALARM_SAMPLES:
            continue
        chains = [json.loads(s['chains_avg'] or s['chains'] or '[]') for s in samples]
        chains = [[c for c in ch if c is not None and MIN_CHAIN_DBM < c < 0] for ch in chains]
        problems = []
        if all(len(ch) >= 2 for ch in chains):
            gap = median(max(ch) - min(ch) for ch in chains)
            if gap >= config.SSH_CHAIN_IMBALANCE_DB:
                last = chains[0]
                problems.append(f"{node} hears it {gap:g} dB louder on one chain than the other "
                                f"({' / '.join(f'{c:g}' for c in last)} dBm)")
        # How the peer hears this node, from LQM (the peer's own tracker, else this
        # node's). ath10k's survey noise is a calibrated floor (-111 dBm on a Rocket
        # 5AC Lite), so signal - survey noise overstates SNR by 10+ dB: last resort.
        row = conn.execute('''SELECT rev_snr, snr FROM links WHERE source_node = ? AND target_node = ?
                              AND link_type = 'RF' ''', (node, peer)).fetchone()
        snr = next((float(v) for v in (row or ()) if v not in (None, '')), None)
        if snr is None:
            if node not in noise:
                noise[node] = _noise(conn, node, since)
            sig = median(s['signal'] for s in samples if s['signal'] is not None) if any(
                s['signal'] is not None for s in samples) else None
            snr = sig - noise[node] if sig is not None and noise[node] is not None else None
        nss = [s['tx_nss'] for s in samples if s['tx_nss'] is not None and s['tx_kind'] != 'legacy']
        if (len(nss) == len(samples) and median(nss) == 1 and any(len(ch) >= 2 for ch in chains)
                and snr is not None and snr >= SINGLE_STREAM_SNR):
            problems.append(f"{node} sends to it on one stream ({samples[0]['tx_rate']:g} Mbps, "
                            f"{samples[0]['tx_kind']}-MCS {samples[0]['tx_mcs']}) at {snr:g} dB SNR")
        if problems:
            out[(node, peer)] = (f"RF link to {peer}: " + '; '.join(problems)
                                 + '. On a 2x2 link this usually means one polarisation is off: check the aim and '
                                   'each chain (connectors, cables, water).')
    return out


def chain_conditions(conn, now):
    """The `chain` alarm for ssh_health: (bad, evaluated) keyed (node, peer)."""
    since = (now - timedelta(minutes=config.SSH_INTERVAL_MINUTES + 5)).strftime(TS_FMT)
    fresh = {(r[0], r[1] or r[2]) for r in conn.execute(
        'SELECT node_name, peer, mac FROM ssh_station_samples WHERE timestamp >= ?', (since,))}
    return {k: (v, 'warning') for k, v in station_conditions(conn, now).items()}, fresh


def evaluate(now=None):
    """Raise / clear every alarm kind of ssh_health.KINDS (after WATCH_RUNS
    collections in a row; none raised in maintenance). A subject not
    evaluated this time (not collected) keeps its state."""
    import ssh_health
    ensure_tables()
    now = now or datetime.now()
    now_s = now.strftime(TS_FMT)
    with database.get_connection() as conn:
        found = ssh_health.conditions(conn, now)
        state = {(r['kind'], r['node_name'], r['peer']): dict(r) for r in conn.execute('SELECT * FROM ssh_alarm_state')}
    events, rows = [], []
    for kind, (bad, evaluated) in found.items():
        raise_ev, clear_ev, clear_text = ssh_health.KINDS[kind]
        keys = set(bad) | {k[1:] for k in state if k[0] == kind}
        for key in keys:
            if key not in bad and key not in evaluated:
                continue
            st = state.get((kind,) + key) or {'active': 0, 'bad_count': 0, 'good_count': 0, 'since': None,
                                              'detail': None, 'severity': None}
            node, subject = key
            if key in bad:
                detail, severity = bad[key]
                st['bad_count'], st['good_count'], st['detail'], st['severity'] = st['bad_count'] + 1, 0, detail, severity
                if not st['active'] and st['bad_count'] >= WATCH_RUNS:
                    peer = subject if kind in ssh_health.LINK_KINDS else None
                    if maintenance.is_in_maintenance(node, peer=peer):
                        st['bad_count'] = 0
                    else:
                        st['active'], st['since'] = 1, now_s
                        events.append((raise_ev, node, detail, severity))
            else:
                st['good_count'], st['bad_count'] = st['good_count'] + 1, 0
                if st['active'] and st['good_count'] >= WATCH_RUNS:
                    prefix = f"RF link to {subject}: " if kind in ssh_health.LINK_KINDS else ''
                    events.append((clear_ev, node, f"{prefix}{clear_text} (since {st['since']})", 'info'))
                    st['active'], st['since'] = 0, None
            rows.append((kind, node, subject, st['active'], st['bad_count'], st['good_count'], st['since'],
                         st['detail'], st.get('severity')))
    with database.get_connection() as conn:
        conn.executemany('''INSERT OR REPLACE INTO ssh_alarm_state (kind, node_name, peer, active, bad_count,
                            good_count, since, detail, severity) VALUES (?,?,?,?,?,?,?,?,?)''', rows)
    for ev, node, text, sev in events:
        database.log_event(ev, node, text, sev)
    return [{'timestamp': now_s, 'event_type': ev, 'node_name': n, 'details': t, 'severity': s}
            for ev, n, t, s in events]


def active_alarms(node=None):
    ensure_tables()
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM ssh_alarm_state WHERE active = 1 ORDER BY since')]
    return [r for r in rows if node is None or r['node_name'] == node]


# ---------------------------------------------------------------- readers --

def _logread_time(line):
    m = re.match(r'\w{3} (\w{3})\s+(\d+) (\d\d:\d\d:\d\d) (\d{4})', line)
    if not m:
        return None
    try:
        return datetime.strptime(' '.join(m.groups()), '%b %d %H:%M:%S %Y')
    except ValueError:
        return None


def log_findings(hours=24, nodes=None):
    """Kernel / radio firmware errors in each node's newest logread snapshot
    within `hours`: [{node, label, severity, count, last, lines}]."""
    ensure_tables()
    since = datetime.now() - timedelta(hours=hours)
    out = []
    with database.get_connection() as conn:
        snaps = conn.execute('''SELECT s.node_name, s.text FROM ssh_snapshots s
                                JOIN (SELECT node_name, MAX(id) AS id FROM ssh_snapshots WHERE kind = 'logread'
                                      GROUP BY node_name) n ON n.id = s.id''').fetchall()
    for r in snaps:
        if nodes is not None and r['node_name'] not in nodes:
            continue
        found = {}
        for line in (r['text'] or '').splitlines():
            t = _logread_time(line)
            if t is None or t < since or LOG_BENIGN.search(line):
                continue
            for label, sev, pat in LOG_PATTERNS:
                if pat.search(line):
                    found.setdefault((label, sev), []).append(line.strip())
                    break
        for (label, sev), lines in found.items():
            out.append({'node': r['node_name'], 'label': label, 'severity': sev, 'count': len(lines),
                        'last': lines[-1][:300], 'lines': lines[-5:]})
    return out


def channel_busy(node, hours=1):
    """Busy / receive / transmit % of the in-use channel over the last
    `hours` from survey counter deltas (counter resets skipped), or None."""
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT * FROM ssh_survey_samples WHERE node_name = ? AND timestamp >= ?
                                                 ORDER BY iface, timestamp''', (node, since))]
    return _busy(rows)


def _busy(rows):
    tot = {'active': 0, 'busy': 0, 'rx': 0, 'tx': 0}
    prev = {}
    last = None
    for r in rows:
        p = prev.get(r['iface'])
        prev[r['iface']] = r
        if not p or p['frequency'] != r['frequency'] or None in (r['active_ms'], p['active_ms']):
            continue
        da = r['active_ms'] - p['active_ms']
        if da <= 0:
            continue  # counters reset (reboot, channel change)
        tot['active'] += da
        for k in ('busy', 'rx', 'tx'):
            a, b = r.get(f'{k}_ms'), p.get(f'{k}_ms')
            tot[k] += max(0, a - b) if a is not None and b is not None else 0
        last = r
    if not tot['active']:
        return None
    pct = lambda k: round(100 * tot[k] / tot['active'], 1)
    # Busy while neither receiving a frame nor transmitting: energy above the
    # clear-channel threshold the radio can't decode (non-WiFi sources, WiFi
    # partly off our channel, overlapping wider channels). Frames from other
    # networks it can decode count as receive time, so this is a floor.
    others = round(max(0.0, pct('busy') - pct('rx') - pct('tx')), 1)
    return {'busy_pct': pct('busy'), 'rx_pct': pct('rx'), 'tx_pct': pct('tx'), 'others_pct': others,
            'frequency': last['frequency'], 'noise': last['noise'], 'at': last['timestamp']}


def node_details(node, hours=24):
    """Node page: newest station rows, channel busy series, snapshots list,
    alarms and status."""
    ensure_tables()
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)
    with database.get_connection() as conn:
        st = _status(conn, node)
        newest = conn.execute('SELECT MAX(timestamp) FROM ssh_station_samples WHERE node_name = ?', (node,)).fetchone()[0]
        stations = [dict(r) for r in conn.execute('''SELECT * FROM ssh_station_samples WHERE node_name = ?
                                                     AND timestamp = ? ORDER BY peer, mac''', (node, newest))]
        series = {}
        for r in conn.execute('''SELECT timestamp, peer, mac, signal, chains_avg, chains, tx_rate, tx_nss
                                 FROM ssh_station_samples WHERE node_name = ? AND timestamp >= ?
                                 ORDER BY timestamp''', (node, since)):
            series.setdefault(r['peer'] or r['mac'], []).append(
                {'t': r['timestamp'], 'signal': r['signal'], 'chains': json.loads(r['chains_avg'] or r['chains'] or '[]'),
                 'tx_rate': r['tx_rate'], 'tx_nss': r['tx_nss']})
        survey = [dict(r) for r in conn.execute('''SELECT * FROM ssh_survey_samples WHERE node_name = ? AND timestamp >= ?
                                                   ORDER BY iface, timestamp''', (node, since))]
        snaps = [dict(r) for r in conn.execute('''SELECT id, timestamp, kind, label, LENGTH(text) AS size
                                                  FROM ssh_snapshots WHERE node_name = ?
                                                  ORDER BY timestamp DESC, kind''', (node,))]
    for s in stations:
        s['chains'] = json.loads(s['chains'] or '[]')
        s['chains_avg'] = json.loads(s['chains_avg'] or '[]')
    busy = []
    for i in range(1, len(survey)):
        b = _busy(survey[i - 1:i + 1])
        if b:
            busy.append({'t': survey[i]['timestamp'], **b})
    import ssh_health
    return {'enabled': enabled(), 'key_present': key_present(), 'targeted': node in targets(), 'status': st,
            'hint': ERROR_HINTS.get(st['error_kind']), 'as_of': newest, 'stations': stations, 'series': series,
            'busy': busy, 'busy_now': channel_busy(node), 'snapshots': snaps, 'alarms': active_alarms(node),
            'log_findings': log_findings(hours, {node}), **ssh_health.node_summary(node, hours)}


def snapshot(snap_id):
    ensure_tables()
    with database.get_connection() as conn:
        r = conn.execute('SELECT * FROM ssh_snapshots WHERE id = ?', (snap_id,)).fetchone()
    return dict(r) if r else None


def status():
    """Admin page: setup state and per-node status (public key only)."""
    ensure_tables()
    with database.get_connection() as conn:
        rows = {r['node_name']: dict(r) for r in conn.execute('SELECT * FROM ssh_status')}
    nodes = []
    for n in targets():
        r = rows.get(n) or {'node_name': n}
        r['hint'] = ERROR_HINTS.get(r.get('error_kind'))
        nodes.append(r)
    return {'enabled': enabled(), 'configured': bool(config.SSH_COLLECT_ENABLED), 'key_present': key_present(),
            'public_key': public_key(), 'key_path': config.SSH_KEY_PATH, 'known_hosts': config.SSH_KNOWN_HOSTS,
            'ssh_exe': ssh_exe(), 'port': config.SSH_PORT, 'user': config.SSH_USER,
            'interval_minutes': config.SSH_INTERVAL_MINUTES, 'slow_hours': config.SSH_SLOW_HOURS,
            'running': _lock.locked(), 'nodes': nodes, 'alarms': active_alarms()}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='SSH radio details from AREDN nodes')
    ap.add_argument('--keygen', action='store_true', help='make the key pair and print the public key')
    ap.add_argument('--pubkey', action='store_true', help='print the public key')
    ap.add_argument('--test', metavar='NODE', help='collect from one node now (fast and slow) and print the result')
    a = ap.parse_args()
    if a.keygen:
        print(keygen())
    elif a.pubkey:
        print(public_key() or f'No key at {config.SSH_KEY_PATH}: run with --keygen')
    elif a.test:
        print(json.dumps(collect_node(a.test, slow=True, manual=True), indent=2))
    else:
        ap.print_help()

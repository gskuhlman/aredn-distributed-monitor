"""
Shared VoIP probe protocol + AREDN LQM helpers.

Ported from the standalone ``voip_diag/aredn_voip_diag.py`` tool so the
distributed monitor and its remote agent speak the same wire protocol.

Contents:
- Binary UDP protocol (stream + echo), receiver statistics (StreamStats),
  RTT statistics (RttStats), and the send/receive endpoint (UdpEndpoint).
- AREDN 4.26.x sysinfo fetch + LQM/link_info merge (lqm_rows / link_rows).
- Traceroute text parsing (shared with rf_stats output shape).
- Live diagnose() heuristics (GOOD/WARN/BAD per 1-second sample).

Only the Python standard library is used here so ``agent.py`` (which imports
this module) stays stdlib-only and can be copied to any Windows/Linux host.
"""

import hmac
import json
import math
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_NAME = "AREDN Distributed Monitor VoIP Agent"
PROTO_VERSION = "1.0"

# Binary UDP protocol. No cross-host timestamp comparisons are made:
# the echoed sender_ns only ever round-trips back to the originating clock.
PACKET = struct.Struct("!4sIIQ")
MAGIC_STREAM = b"AVS1"
MAGIC_ECHO = b"AVE1"
MAGIC_ECHO_REPLY = b"AVR1"

DEFAULT_AGENT_PORT = 8765
DEFAULT_UDP_PORT = 8766
DEFAULT_PPS = 50.0               # voice-like 20 ms packet cadence
DEFAULT_PACKET_BYTES = 172       # ~160-byte audio + 12-byte RTP header
HTTP_TIMEOUT = 4.0


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def safe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ============ UDP statistics (receiver-side, own-clock only) ============


class StreamStats:
    """Direction-specific UDP receive statistics without cross-host clock assumptions."""

    def __init__(self, expected_interval=1.0 / DEFAULT_PPS):
        import contextlib
        self.lock = threading.Lock()
        self.expected_interval = expected_interval
        self._ctx = contextlib
        self.reset()

    def reset(self):
        with self.lock:
            self.session_id = None
            self.first_seq = None
            self.highest_seq = None
            self.received_unique = 0
            self.duplicates = 0
            self.out_of_order = 0
            self.missing = set()
            self.prev_arrival = None
            self.jitter_ewma = 0.0
            self.max_gap = 0.0
            self.late_packets = 0
            self.started = None
            self.last_arrival = None

    def set_interval(self, seconds):
        with self.lock:
            self.expected_interval = max(0.005, seconds)

    def observe(self, session_id, seq, arrival):
        with self.lock:
            if self.session_id != session_id:
                self.session_id = session_id
                self.first_seq = seq
                self.highest_seq = None
                self.received_unique = 0
                self.duplicates = 0
                self.out_of_order = 0
                self.missing.clear()
                self.prev_arrival = None
                self.jitter_ewma = 0.0
                self.max_gap = 0.0
                self.late_packets = 0
                self.started = arrival

            if self.highest_seq is None:
                self.highest_seq = seq
                self.first_seq = seq
                self.received_unique = 1
            elif seq > self.highest_seq:
                if seq > self.highest_seq + 1:
                    gap = seq - self.highest_seq - 1
                    if gap <= 5000:
                        self.missing.update(range(self.highest_seq + 1, seq))
                    else:
                        self.missing.update(range(seq - 5000, seq))
                self.highest_seq = seq
                self.received_unique += 1
            elif seq in self.missing:
                self.missing.remove(seq)
                self.received_unique += 1
                self.out_of_order += 1
            else:
                self.duplicates += 1

            if self.prev_arrival is not None:
                gap = arrival - self.prev_arrival
                dev = abs(gap - self.expected_interval)
                self.jitter_ewma += (dev - self.jitter_ewma) / 16.0
                self.max_gap = max(self.max_gap, gap)
                if gap > self.expected_interval * 2.5:
                    self.late_packets += 1
            self.prev_arrival = arrival
            self.last_arrival = arrival

    def snapshot(self):
        with self.lock:
            if self.first_seq is None or self.highest_seq is None:
                expected = 0
            else:
                expected = max(0, self.highest_seq - self.first_seq + 1)
            missing = len(self.missing)
            loss_pct = (100.0 * missing / expected) if expected else 0.0
            duration = ((self.last_arrival or time.monotonic()) - self.started) if self.started else 0.0
            return {
                "session_id": self.session_id,
                "expected": expected,
                "received": self.received_unique,
                "missing": missing,
                "loss_pct": loss_pct,
                "duplicates": self.duplicates,
                "out_of_order": self.out_of_order,
                "arrival_jitter_ms": self.jitter_ewma * 1000.0,
                "max_gap_ms": self.max_gap * 1000.0,
                "late_packets": self.late_packets,
                "duration_s": duration,
                "expected_interval_ms": self.expected_interval * 1000.0,
            }


class RttStats:
    def __init__(self):
        import collections
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=300)
        self.sent = 0
        self.replies = 0

    def reset(self):
        with self.lock:
            self.samples.clear()
            self.sent = 0
            self.replies = 0

    def sent_one(self):
        with self.lock:
            self.sent += 1

    def observe(self, ms):
        with self.lock:
            self.replies += 1
            self.samples.append(ms)

    def snapshot(self):
        with self.lock:
            vals = list(self.samples)
            loss_pct = 100.0 * max(0, self.sent - self.replies) / self.sent if self.sent else 0.0
            if vals:
                mean = sum(vals) / len(vals)
                variance = sum((x - mean) ** 2 for x in vals) / len(vals)
                jitter = math.sqrt(variance)
                return {
                    "rtt_ms": mean,
                    "rtt_min_ms": min(vals),
                    "rtt_max_ms": max(vals),
                    "rtt_jitter_ms": jitter,
                    "echo_sent": self.sent,
                    "echo_replies": self.replies,
                    "echo_loss_pct": loss_pct,
                }
            return {"rtt_ms": None, "rtt_min_ms": None, "rtt_max_ms": None, "rtt_jitter_ms": None,
                    "echo_sent": self.sent, "echo_replies": self.replies, "echo_loss_pct": loss_pct}


class UdpEndpoint:
    """UDP stream sender/receiver + echo sender/replier on one bound port."""

    def __init__(self, bind_port=DEFAULT_UDP_PORT):
        self.bind_port = bind_port
        self.rx_stats = StreamStats()
        self.rtt_stats = RttStats()
        self.stop_event = threading.Event()
        self.sock = None
        self.thread = None
        self.sender_stop = threading.Event()
        self.sender_thread = None
        self.echo_stop = threading.Event()
        self.echo_thread = None
        # Latest observed send rates (bits/s) for rx/tx directions, set externally.
        self.rx_bps = 0.0
        self.tx_bps = 0.0
        self._rx_bytes_window = 0
        self._rx_window_start = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.bind_port))
        self.sock.settimeout(0.5)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._receiver, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_sender()
        self.stop_echo()
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _receiver(self):
        import contextlib
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(2048) if self.sock else (b"", ("", 0))
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.monotonic()
            self._rx_bytes_window += len(data)
            if self._rx_window_start is None:
                self._rx_window_start = now
            else:
                w = now - self._rx_window_start
                if w >= 1.0:
                    self.rx_bps = self._rx_bytes_window * 8.0 / w
                    self._rx_bytes_window = 0
                    self._rx_window_start = now
            if len(data) < PACKET.size:
                continue
            try:
                magic, sid, seq, sender_ns = PACKET.unpack_from(data)
            except struct.error:
                continue
            if magic == MAGIC_STREAM:
                self.rx_stats.observe(sid, seq, now)
            elif magic == MAGIC_ECHO:
                try:
                    reply = PACKET.pack(MAGIC_ECHO_REPLY, sid, seq, sender_ns)
                    if self.sock:
                        self.sock.sendto(reply, addr)
                except OSError:
                    pass
            elif magic == MAGIC_ECHO_REPLY:
                rtt_ms = (time.monotonic_ns() - sender_ns) / 1_000_000.0
                if 0 <= rtt_ms < 120_000:
                    self.rtt_stats.observe(rtt_ms)

    def start_sender(self, target_host, target_port, pps, packet_bytes=DEFAULT_PACKET_BYTES, session_id=None):
        import uuid
        self.stop_sender()
        self.sender_stop.clear()
        self.rx_stats.set_interval(1.0 / max(1.0, pps))
        sid = session_id if session_id is not None else (uuid.uuid4().int & 0xFFFFFFFF)
        packet_bytes = int(clamp(packet_bytes, PACKET.size, 1400))
        padding = b"\x00" * (packet_bytes - PACKET.size)
        self.tx_bps = pps * packet_bytes * 8.0

        endpoint = self

        def run():
            interval = 1.0 / max(1.0, pps)
            seq = 0
            target = (target_host, target_port)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            next_send = time.perf_counter()
            try:
                while not endpoint.sender_stop.is_set():
                    packet = PACKET.pack(MAGIC_STREAM, sid, seq & 0xFFFFFFFF, time.monotonic_ns()) + padding
                    try:
                        s.sendto(packet, target)
                    except OSError:
                        pass
                    seq += 1
                    next_send += interval
                    delay = next_send - time.perf_counter()
                    if delay > 0:
                        endpoint.sender_stop.wait(delay)
                    elif delay < -1.0:
                        next_send = time.perf_counter()
            finally:
                s.close()

        self.sender_thread = threading.Thread(target=run, daemon=True)
        self.sender_thread.start()
        return sid

    def stop_sender(self):
        self.sender_stop.set()
        if self.sender_thread and self.sender_thread.is_alive():
            self.sender_thread.join(timeout=1.0)
        self.sender_thread = None

    def start_echo(self, target_host, target_port, interval_s=1.0):
        import uuid
        self.stop_echo()
        self.echo_stop.clear()
        sid = uuid.uuid4().int & 0xFFFFFFFF

        endpoint = self

        def run():
            seq = 0
            target = (target_host, target_port)
            # Send echo probes through the BOUND socket so the peer's replies
            # come back to it and are observed by _receiver (which computes RTT
            # from the echoed monotonic timestamp on this same clock).
            while not endpoint.echo_stop.is_set():
                endpoint.rtt_stats.sent_one()
                packet = PACKET.pack(MAGIC_ECHO, sid, seq & 0xFFFFFFFF, time.monotonic_ns())
                try:
                    if endpoint.sock:
                        endpoint.sock.sendto(packet, target)
                except OSError:
                    pass
                seq += 1
                endpoint.echo_stop.wait(interval_s)

        self.echo_thread = threading.Thread(target=run, daemon=True)
        self.echo_thread.start()

    def stop_echo(self):
        self.echo_stop.set()
        if self.echo_thread and self.echo_thread.is_alive():
            self.echo_thread.join(timeout=1.0)
        self.echo_thread = None


# ============ HTTP helpers (agent control) ============


def http_json(url, timeout=HTTP_TIMEOUT, headers=None):
    h = {"User-Agent": f"{APP_NAME}/{PROTO_VERSION}"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8", errors="replace"))


def http_post_json(url, payload, timeout=8.0, headers=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{PROTO_VERSION}"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def key_ok(supplied, expected):
    return bool(supplied) and bool(expected) and hmac.compare_digest(str(supplied), str(expected))


def discover_local_ip(remote_host, remote_port):
    """Best-effort: the local source IP the OS would use toward the remote."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((remote_host, remote_port))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


# ============ AREDN sysinfo + LQM merge ============


def normalize_base_url(value):
    import re
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "http://" + value
    return value.rstrip("/")


def fetch_aredn_sysinfo(node_url):
    """Fetch AREDN 4.26.x sysinfo (link_info + nodes + hosts + lqm)."""
    base = normalize_base_url(node_url)
    if not base:
        return {"ok": False, "error": "no node URL configured"}
    qs = urllib.parse.urlencode({"link_info": 1, "nodes": 1, "hosts": 1, "lqm": 1})
    candidates = [f"{base}/cgi-bin/sysinfo.json?{qs}"]
    parsed = urllib.parse.urlparse(base)
    if parsed.port is None:
        host = parsed.hostname or ""
        candidates.append(f"{parsed.scheme}://{host}:8080/cgi-bin/sysinfo.json?{qs}")
    errors = []
    for url in candidates:
        try:
            data = http_json(url)
            data["_fetched_from"] = url
            data["ok"] = True
            return data
        except Exception as e:
            errors.append(f"{url}: {e}")
    return {"ok": False, "error": " | ".join(errors)}


def fetch_hop_sysinfo(ip, timeout=1.5):
    """Best-effort short-timeout sysinfo lookup for a mesh hop IP."""
    qs = urllib.parse.urlencode({"link_info": 1, "nodes": 1, "lqm": 1})
    errors = []
    for base in (f"http://{ip}:8080", f"http://{ip}"):
        url = f"{base}/cgi-bin/sysinfo.json?{qs}"
        try:
            data = http_json(url, timeout=timeout)
            data["ok"] = True
            return data
        except Exception as e:
            errors.append(str(e))
    return {"ok": False, "error": " | ".join(errors)}


def _lqm_trackers(snapshot):
    lqm = snapshot.get("lqm") or {}
    info = lqm.get("info") if isinstance(lqm, dict) else {}
    trackers = info.get("trackers") if isinstance(info, dict) else {}
    return trackers if isinstance(trackers, dict) else {}


def _tracker_rtt_ms(track):
    v = safe_float(track.get("rtt"))
    return (v / 1000.0) if v is not None else None


def _tracker_bitrate_mbps(track, key):
    # nl80211 bitrate is reported in 100-kbit/s units by the LQM tracker.
    v = safe_float(track.get(key))
    return (v / 10.0) if v is not None else None


def lqm_rows(snapshot):
    """Normalize AREDN 4.26.x /tmp/lqm.info tracker records."""
    rows = []
    for mac, track in _lqm_trackers(snapshot).items():
        if not isinstance(track, dict):
            continue
        row = dict(track)
        row["mac"] = row.get("mac") or mac
        rx = safe_float(row.get("avg_lq"))
        if rx is None:
            rx = safe_float(row.get("lq"))
        row["rx_pct"] = rx
        row["rtt_ms"] = _tracker_rtt_ms(row)
        row["tx_mbps"] = _tracker_bitrate_mbps(row, "avg_tx_bitrate")
        if row["tx_mbps"] is None:
            row["tx_mbps"] = _tracker_bitrate_mbps(row, "tx_bitrate")
        row["rx_mbps"] = _tracker_bitrate_mbps(row, "avg_rx_bitrate")
        if row["rx_mbps"] is None:
            row["rx_mbps"] = _tracker_bitrate_mbps(row, "rx_bitrate")
        rows.append(row)
    return rows


def link_rows(snapshot):
    """Direct-link rows merging public link_info with full LQM trackers."""
    base_rows = []
    links = snapshot.get("link_info") or {}
    if isinstance(links, dict):
        for ip, info in links.items():
            if not isinstance(info, dict):
                continue
            row = dict(info)
            row["ip"] = row.get("ip") or ip
            sig = safe_float(row.get("signal"))
            noise = safe_float(row.get("noise"))
            if sig is not None and noise is not None and "snr" not in row:
                row["snr"] = sig - noise
            base_rows.append(row)
    elif isinstance(links, list):
        for info in links:
            if isinstance(info, dict):
                row = dict(info)
                sig = safe_float(row.get("signal"))
                noise = safe_float(row.get("noise"))
                if sig is not None and noise is not None and "snr" not in row:
                    row["snr"] = sig - noise
                base_rows.append(row)

    trackers = lqm_rows(snapshot)
    if not trackers:
        return base_rows

    idx = {}
    for tr in trackers:
        for key in ("ip", "canonical_ip", "hostname", "mac", "ipv6ll"):
            val = str(tr.get(key) or "").strip().lower()
            if val:
                idx[val] = tr

    merged = []
    used = set()
    for row in base_rows:
        match = None
        for key in ("ip", "canonical_ip", "hostname", "name", "mac", "ipv6ll"):
            val = str(row.get(key) or "").strip().lower()
            if val and val in idx:
                match = idx[val]
                break
        if match:
            m = dict(row)
            m.update(match)
            if row.get("noise") is not None:
                m["noise"] = row.get("noise")
            merged.append(m)
            used.add(str(match.get("mac") or ""))
        else:
            merged.append(row)

    for tr in trackers:
        ident = str(tr.get("mac") or "")
        if ident not in used:
            merged.append(dict(tr))
    return merged


# ============ Route helpers ============


def route_ips(route):
    return [str(h.get("ip")) for h in (route.get("hops") or []) if h.get("ip")]


def reciprocal_route(forward, reverse):
    a = route_ips(forward)
    b = route_ips(reverse)
    if not a or not b:
        return None
    return a == list(reversed(b))


def route_changed(old, new):
    a, b = route_ips(old), route_ips(new)
    return bool(a and b and a != b)


# ============ Live diagnosis heuristics ============


def diagnose(fwd, rev, rtt, asymmetric, changed):
    """Return (severity, concise diagnosis). Thresholds are heuristics."""
    f_loss = float(fwd.get("loss_pct") or 0.0)
    r_loss = float(rev.get("loss_pct") or 0.0)
    f_jit = float(fwd.get("arrival_jitter_ms") or 0.0)
    r_jit = float(rev.get("arrival_jitter_ms") or 0.0)
    rtt_jit = float(rtt.get("rtt_jitter_ms") or 0.0)
    rr = rtt.get("rtt_ms")

    severe_dirs = []
    if f_loss >= 3.0 or f_jit >= 30.0:
        severe_dirs.append("A->B")
    if r_loss >= 3.0 or r_jit >= 30.0:
        severe_dirs.append("B->A")
    warn_dirs = []
    if f_loss >= 1.0 or f_jit >= 20.0:
        warn_dirs.append("A->B")
    if r_loss >= 1.0 or r_jit >= 20.0:
        warn_dirs.append("B->A")

    if severe_dirs:
        reason = f"Severe transport impairment in {', '.join(severe_dirs)}"
        if changed:
            reason += "; route changed"
        return "BAD", reason
    if changed and (f_loss > 0.2 or r_loss > 0.2 or max(f_jit, r_jit, rtt_jit) > 8):
        return "BAD", "Route changed while packet timing/loss was degraded"
    if warn_dirs:
        return "WARN", f"Voice-risk packet loss/jitter in {', '.join(warn_dirs)}"
    if rr is not None and float(rr) > 250:
        return "WARN", f"High path RTT ({float(rr):.0f} ms)"
    if asymmetric is False:
        return "INFO", "Forward/reverse routes differ; transport currently looks clean"
    return "GOOD", "Transport currently looks clean"

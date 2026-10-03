"""
Two-ended / one-ended streamed VoIP session orchestration.

Ported from the standalone ``voip_diag`` tool and adapted to the dist_mon web
app (eventlet green threads + SQLite persistence + JSON API, not Tkinter).

Two modes:

- ``full``    — the controller (this host) and a remote dist_mon agent run the
    reciprocal voice-like UDP streams. Each side measures receive loss/jitter
    on its own clock; echo probes give true RTT. Routes are traced forward
    locally and reverse by asking the agent to traceroute back, refreshed on a
    cadence. This is the definitive diagnostic but needs agent.py running at
    the far end.

- ``one_ended`` — only the controller streams toward the target and runs echo
    probes. We still collect route change + LQM/RF + event marks, but there is
    no independent remote receiver, so B->A receive stats are unavailable.

A single global session runs at a time because the UDP probe port is owned by
one endpoint. All work happens in a daemon thread; the web request handlers
return immediately and poll session_status().
"""

import datetime as dt
import json
import logging
import threading
import time

import config
import database
import rf_stats
import voip_proto as vp

logger = logging.getLogger(__name__)

_session_lock = threading.Lock()
_current = None  # the one active VoipSession


def _iso_now():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class VoipSession:
    def __init__(self, mode, source_label, target_label, remote_host,
                 agent_key="", pps=config.VOIP_STREAM_PPS,
                 packet_bytes=config.VOIP_STREAM_PACKET_BYTES,
                 source_node_ip=None):
        self.mode = mode  # 'full' | 'one_ended'
        self.source_label = source_label
        self.target_label = target_label
        self.remote_host = remote_host
        self.agent_key = agent_key
        self.pps = float(pps)
        self.packet_bytes = int(packet_bytes)
        self.source_node_ip = source_node_ip

        self.started_at = _iso_now()
        self.started_mono = time.monotonic()
        self.stop_event = threading.Event()
        self.worker = None
        self.udp = vp.UdpEndpoint(config.VOIP_PROBE_UDP_PORT)
        self.force_route = threading.Event()

        self.forward = {}
        self.reverse = {}
        self.prev_forward = {}
        self.prev_reverse = {}
        self.route_changed_flag = False
        self.last_diag = ("IDLE", "Not sampling yet")
        self.last_sample = {}
        self.error = None
        self.done = False

        self.session_id = database.save_voip_session_start(
            mode=mode, source=source_label, target=target_label,
            remote_host=remote_host, pps=self.pps, packet_bytes=self.packet_bytes,
        )

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        self.udp.start()
        self.udp.rx_stats.set_interval(1.0 / self.pps)
        self.udp.rx_stats.reset()
        self.udp.rtt_stats.reset()

        # Controller always sends its stream toward the remote end.
        self.udp.start_sender(self.remote_host, config.VOIP_PROBE_UDP_PORT,
                              self.pps, self.packet_bytes)
        self.udp.start_echo(self.remote_host, config.VOIP_PROBE_UDP_PORT, 1.0)

        if self.mode == 'full':
            # Ask the agent to stream back at us, and reset its stats.
            local_ip = vp.discover_local_ip(self.remote_host, config.VOIP_AGENT_PORT)
            base = f"http://{self.remote_host}:{config.VOIP_AGENT_PORT}"
            headers = {"X-AREDN-Diag-Key": self.agent_key}
            try:
                vp.http_post_json(f"{base}/reset", {}, timeout=3.0, headers=headers)
                vp.http_post_json(f"{base}/stream/start", {
                    "target": local_ip,
                    "port": config.VOIP_PROBE_UDP_PORT,
                    "pps": self.pps,
                    "packet_bytes": self.packet_bytes,
                }, timeout=4.0, headers=headers)
            except Exception as exc:
                logger.warning("Remote agent stream start failed: %s", exc)
                self.error = f"reverse stream failed to start: {exc}"

        self.worker = threading.Thread(target=self._collector, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.udp.stop_sender()
        self.udp.stop_echo()
        if self.mode == 'full':
            try:
                base = f"http://{self.remote_host}:{config.VOIP_AGENT_PORT}"
                vp.http_post_json(f"{base}/stream/stop", {}, timeout=2.0,
                                  headers={"X-AREDN-Diag-Key": self.agent_key})
            except Exception:
                pass
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2.0)
        self.udp.stop()

        ended_at = _iso_now()
        report = self._build_report()
        database.save_voip_session_finish(self.session_id, ended_at, report)
        self.done = True

    # ---- collector loop ----------------------------------------------------

    def _collector(self):
        next_route = 0.0
        max_end = self.started_mono + config.VOIP_SESSION_MAX_SECONDS
        base = f"http://{self.remote_host}:{config.VOIP_AGENT_PORT}"
        headers = {"X-AREDN-Diag-Key": self.agent_key}

        while not self.stop_event.is_set() and time.monotonic() < max_end:
            now = time.monotonic()

            if self.force_route.is_set() or now >= next_route:
                self.force_route.clear()
                forced = self.force_route.is_set()
                try:
                    self._refresh_routes(base, headers)
                except Exception as exc:
                    logger.warning("route refresh failed: %s", exc)
                next_route = now + config.VOIP_SESSION_ROUTE_INTERVAL

            # 1-second sample: local RX (B->A) + RTT; remote RX (A->B) in full mode.
            local_rx = self.udp.rx_stats.snapshot()
            rtt = self.udp.rtt_stats.snapshot()
            remote_stats = {"rx": {}, "rtt": {}}
            if self.mode == 'full':
                try:
                    remote_stats = vp.http_json(f"{base}/stats", timeout=3.0, headers=headers)
                except Exception as exc:
                    remote_stats = {"rx": {}, "rtt": {}, "ok": False, "error": str(exc)}

            fwd_rx = remote_stats.get("rx") or {}
            rec = vp.reciprocal_route(self.forward, self.reverse)
            sev, text = vp.diagnose(fwd_rx, local_rx, rtt, rec, self.route_changed_flag)
            self.last_diag = (sev, text)
            sample = {
                "ts": _iso_now(),
                "fwd_loss": fwd_rx.get("loss_pct"),
                "fwd_jitter": fwd_rx.get("arrival_jitter_ms"),
                "fwd_late": fwd_rx.get("late_packets"),
                "rev_loss": local_rx.get("loss_pct"),
                "rev_jitter": local_rx.get("arrival_jitter_ms"),
                "rev_late": local_rx.get("late_packets"),
                "rtt": rtt.get("rtt_ms"),
                "rtt_jitter": rtt.get("rtt_jitter_ms"),
                "echo_loss": rtt.get("echo_loss_pct"),
                "route_asymmetric": None if rec is None else (0 if rec else 1),
                "route_changed": 1 if self.route_changed_flag else 0,
                "severity": sev,
                "diagnosis": text,
            }
            self.last_sample = sample
            database.save_voip_sample(self.session_id, sample)
            self.route_changed_flag = False
            self.stop_event.wait(1.0)

    def _refresh_routes(self, base, headers):
        # Forward: from the collector's vantage (or the chosen source node).
        if self.source_node_ip:
            fwd = rf_stats.traceroute_via_aredn(self.remote_host,
                                                source_node_ip=self.source_node_ip)
        else:
            fwd = rf_stats.traceroute_local(self.remote_host)
        fwd = fwd or {"hops": []}

        rev = {"hops": []}
        if self.mode == 'full':
            # Reverse: ask the agent to traceroute back toward us.
            local_ip = vp.discover_local_ip(self.remote_host, config.VOIP_AGENT_PORT)
            try:
                rev = vp.http_post_json(f"{base}/traceroute",
                                        {"target": local_ip}, timeout=35.0,
                                        headers=headers)
            except Exception as exc:
                logger.warning("agent traceroute failed: %s; falling back", exc)
                # Fall back to a collector-origin trace so we still show *something*.
                rev = rf_stats.traceroute_local(self.remote_host) or {"hops": []}
                rev["fallback"] = True
        else:
            rev = rf_stats.traceroute_local(self.remote_host) or {"hops": []}

        ch = (vp.route_changed(self.prev_forward, fwd)
              or vp.route_changed(self.prev_reverse, rev))
        self.route_changed_flag = self.route_changed_flag or ch
        self.prev_forward, self.prev_reverse = self.forward, self.reverse
        self.forward, self.reverse = fwd, rev

        ts = _iso_now()
        names = database.get_ip_name_map()
        database.save_voip_route(self.session_id, ts, 'A_TO_B', fwd, names)
        database.save_voip_route(self.session_id, ts, 'B_TO_A', rev, names)

    # ---- status / reporting ------------------------------------------------

    def snapshot(self):
        elapsed = time.monotonic() - self.started_mono
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "source": self.source_label,
            "target": self.target_label,
            "remote_host": self.remote_host,
            "pps": self.pps,
            "packet_bytes": self.packet_bytes,
            "started_at": self.started_at,
            "elapsed_s": round(elapsed, 1),
            "running": not self.done and not self.stop_event.is_set(),
            "error": self.error,
            "last_diag": {"severity": self.last_diag[0], "text": self.last_diag[1]},
            "last_sample": self.last_sample,
            "forward_hops": [h.get("ip") for h in (self.forward.get("hops") or [])],
            "reverse_hops": [h.get("ip") for h in (self.reverse.get("hops") or [])],
            "route_reciprocal": vp.reciprocal_route(self.forward, self.reverse),
        }

    def mark_event(self, tag, note=""):
        database.add_voip_event(self.session_id, tag, note)

    def _build_report(self):
        """Session correlation report against samples + events + routes."""
        samples = database.get_voip_samples(self.session_id)
        events = database.get_voip_events(self.session_id)
        routes = database.get_voip_routes(self.session_id)

        lines = [
            f"VoIP session diagnosis - {self.mode} mode",
            f"Generated: {_iso_now()}",
            f"Path: {self.source_label} -> {self.target_label} ({self.remote_host})",
            f"Stream: {self.pps:.0f} pps x {self.packet_bytes} bytes",
            "",
            "Note: loss/jitter thresholds are heuristics. One-way absolute delay is",
            "intentionally not reported unless endpoint clocks are synchronized.",
            "",
        ]
        if not samples:
            lines.append("No measurement samples were recorded.")
            return "\n".join(lines) + "\n"

        def maxv(key):
            vals = [float(r[key]) for r in samples if r.get(key) is not None]
            return max(vals) if vals else 0.0

        def avgv(key):
            vals = [float(r[key]) for r in samples if r.get(key) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        lines += [
            "SESSION SUMMARY",
            f"  Samples: {len(samples)}",
            f"  Worst A->B loss: {maxv('fwd_loss'):.2f}%",
            f"  Worst B->A loss: {maxv('rev_loss'):.2f}%",
            f"  Avg A->B arrival jitter: {avgv('fwd_jitter'):.2f} ms",
            f"  Avg B->A arrival jitter: {avgv('rev_jitter'):.2f} ms",
            f"  Worst RTT: {maxv('rtt'):.1f} ms",
            f"  Route-change samples: {sum(int(r['route_changed'] or 0) for r in samples)}",
            f"  Non-reciprocal-route samples: {sum(int(r['route_asymmetric'] or 0) for r in samples)}",
            "",
        ]

        if not events:
            lines += [
                "No audio-condition marks were recorded.",
                "During a call, mark GOOD/BAD/ONE-WAY/NO AUDIO so the analyzer has",
                "timestamps to correlate against the network measurements.",
                "",
            ]
        else:
            lines.append("EVENT CORRELATION (+/- 8 seconds)")
            for ev in events:
                try:
                    et = dt.datetime.fromisoformat(ev['ts'])
                except Exception:
                    continue
                window = []
                for r in samples:
                    try:
                        st = dt.datetime.fromisoformat(r['ts'])
                    except Exception:
                        continue
                    if abs((st - et).total_seconds()) <= 8.0:
                        window.append(r)
                lines.append(f"  {ev['ts']}  {ev['tag']}" + (f"  - {ev['note']}" if ev.get('note') else ""))
                if not window:
                    lines.append("    No measurement samples in correlation window.")
                    continue
                f_loss = max(float(r['fwd_loss'] or 0) for r in window)
                r_loss = max(float(r['rev_loss'] or 0) for r in window)
                f_jit = max(float(r['fwd_jitter'] or 0) for r in window)
                r_jit = max(float(r['rev_jitter'] or 0) for r in window)
                rtt = max(float(r['rtt'] or 0) for r in window)
                changed = any(int(r['route_changed'] or 0) for r in window)
                asym = any(int(r['route_asymmetric'] or 0) for r in window)
                lines.append(f"    max loss A->B {f_loss:.2f}% | B->A {r_loss:.2f}% | "
                             f"jitter {f_jit:.1f}/{r_jit:.1f} ms | RTT {rtt:.1f} ms")
                lines.append(f"    route change: {'YES' if changed else 'no'} | "
                             f"fwd/rev differ: {'YES' if asym else 'no/unknown'}")

                impaired_f = f_loss >= 1.0 or f_jit >= 20.0
                impaired_r = r_loss >= 1.0 or r_jit >= 20.0
                tag = str(ev['tag']).upper()
                if tag in ("BAD AUDIO", "NO AUDIO", "ONE-WAY AUDIO"):
                    if changed and (impaired_f or impaired_r):
                        assessment = ("Strong correlation with a route change plus transport "
                                      "degradation; routing/RF instability is a leading suspect.")
                    elif impaired_f and not impaired_r:
                        assessment = ("Directional impairment A->B; investigate forward RF/path "
                                      "links and route at this timestamp.")
                    elif impaired_r and not impaired_f:
                        assessment = ("Directional impairment B->A; investigate reverse RF/path "
                                      "links and route at this timestamp.")
                    elif impaired_f and impaired_r:
                        assessment = ("Both directions degraded; investigate shared RF links, "
                                      "contention, retries, or congestion.")
                    elif changed:
                        assessment = ("Audio problem coincided with a route change, but endpoint UDP "
                                      "probes stayed clean; inspect the actual RTP stream next.")
                    else:
                        assessment = ("No clear endpoint transport impairment captured; codec, PBX/"
                                      "conference processing, RTP handling, or jitter-buffer behavior "
                                      "moves higher on the list.")
                else:
                    assessment = "Reference good-audio interval for comparison."
                lines.append(f"    Assessment: {assessment}")
            lines.append("")

        if routes:
            lines.append(f"Route observations: {len(routes)} hop rows recorded.")
        lines.append(f"Raw data: voip session id {self.session_id}")
        return "\n".join(lines) + "\n"


# ---- module-level API used by app.py routes ---------------------------------


def start_session(mode, source_label, target_label, remote_host,
                  agent_key="", pps=config.VOIP_STREAM_PPS,
                  packet_bytes=config.VOIP_STREAM_PACKET_BYTES,
                  source_node_ip=None):
    """Start the single streamed session. Returns (session, error)."""
    global _current
    with _session_lock:
        if _current and not _current.done:
            return None, "A VoIP session is already running; stop it first."
        try:
            s = VoipSession(mode, source_label, target_label, remote_host,
                            agent_key=agent_key, pps=pps,
                            packet_bytes=packet_bytes, source_node_ip=source_node_ip)
            s.start()
        except Exception as exc:
            logger.exception("voip session start failed")
            return None, str(exc)
        _current = s
        return s, None


def stop_session():
    global _current
    with _session_lock:
        s = _current
    if not s:
        return None
    s.stop()
    return s.session_id


def current_session():
    return _current

#!/usr/bin/env python3
"""
AREDN Distributed Monitor - remote VoIP agent.

A tiny stdlib-only HTTP + UDP responder that runs on a Windows/Linux PC at the
FAR end of a VoIP path. The dist_mon web UI (End A) drives it over the mesh to
run a true two-ended voice-like stream test: the agent receives a UDP stream
from the controller, answers an echo stream so the controller can measure true
RTT, and launches its own stream back at the controller so each side measures
receive loss/jitter on its own clock (no NTP assumption).

Usage:
    python agent.py [--bind 0.0.0.0] [--port 8765] [--udp-port 8766] [--key KEY]

If --key is omitted a random 4-byte hex key is generated and printed; enter it
in the dist_mon VoIP session form so only sessions you authorize can drive
this agent. The control channel is not encrypted -- treat the key as a
per-test-session token, not a durable credential.

Files copied to the remote end: agent.py + voip_proto.py (that's all).
"""

import argparse
import http.server
import platform
import secrets
import socket

import voip_proto as vp


TRACERT_LINE = __import__("re").compile(r"^\s*(\d+)\s+(.+)$")
IP_RE = __import__("re").compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
MS_RE = __import__("re").compile(r"<?\s*(\d+)\s*ms", __import__("re").I)


def parse_traceroute_text(text):
    hops = []
    for raw_line in text.splitlines():
        line = raw_line.strip("\r\n")
        m = TRACERT_LINE.match(line)
        if not m:
            continue
        hop_no = int(m.group(1))
        tail = m.group(2)
        ips = IP_RE.findall(tail)
        ip = ips[-1] if ips else None
        times = [float(x) for x in MS_RE.findall(tail)]
        if "*" in tail and not ip:
            hops.append({"hop": hop_no, "ip": None, "rtt_ms": None, "timeout": True})
        elif ip:
            hops.append({
                "hop": hop_no,
                "ip": ip,
                "rtt_ms": (sum(times) / len(times)) if times else None,
                "timeout": False,
            })
    return hops


class AgentState:
    def __init__(self, key, udp_port):
        self.key = key.strip() or secrets.token_hex(4).upper()
        self.udp = vp.UdpEndpoint(udp_port)


class AgentHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DistMonVoipAgent/1.0"

    @property
    def state(self):
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        print(f"[agent] {self.client_address[0]} {fmt % args}")

    def _authorized(self):
        supplied = self.headers.get("X-AREDN-Diag-Key", "")
        return vp.key_ok(supplied, self.state.key)

    def _json(self, status, payload):
        import json
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        import json
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(min(n, 1_000_000)).decode("utf-8"))

    def do_GET(self):
        if not self._authorized():
            self._json(403, {"ok": False, "error": "invalid agent key"})
            return
        import urllib.parse
        p = urllib.parse.urlparse(self.path)
        if p.path == "/health":
            self._json(200, {
                "ok": True,
                "app": vp.APP_NAME,
                "proto": vp.PROTO_VERSION,
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "udp_port": self.state.udp.bind_port,
            })
        elif p.path == "/stats":
            self._json(200, {
                "ok": True,
                "rx": self.state.udp.rx_stats.snapshot(),
                "rtt": self.state.udp.rtt_stats.snapshot(),
                "rx_bps": self.state.udp.rx_bps,
                "tx_bps": self.state.udp.tx_bps,
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._json(403, {"ok": False, "error": "invalid agent key"})
            return
        import urllib.parse
        try:
            body = self._body()
            p = urllib.parse.urlparse(self.path)
            if p.path == "/stream/start":
                target = str(body.get("target") or self.client_address[0]).strip()
                port = int(body.get("port") or vp.DEFAULT_UDP_PORT)
                pps = float(body.get("pps") or vp.DEFAULT_PPS)
                packet_bytes = int(body.get("packet_bytes") or vp.DEFAULT_PACKET_BYTES)
                if not (1 <= port <= 65535) or not (1 <= pps <= 200) or not (vp.PACKET.size <= packet_bytes <= 1400):
                    self._json(400, {"ok": False, "error": "invalid stream parameters"})
                    return
                self.state.udp.rx_stats.set_interval(1.0 / pps)
                sid = self.state.udp.start_sender(target, port, pps, packet_bytes)
                self._json(200, {"ok": True, "session_id": sid, "target": target,
                                 "port": port, "pps": pps, "packet_bytes": packet_bytes})
            elif p.path == "/stream/stop":
                self.state.udp.stop_sender()
                self._json(200, {"ok": True})
            elif p.path == "/traceroute":
                import re
                import subprocess
                target = str(body.get("target") or "").strip()
                if not target or len(target) > 255 or not re.fullmatch(r"[A-Za-z0-9_.:\-]+", target):
                    self._json(400, {"ok": False, "error": "invalid target", "hops": []})
                    return
                import os
                if os.name == "nt":
                    cmd = ["tracert", "-d", "-w", "750", "-h", "20", target]
                else:
                    cmd = ["traceroute", "-n", "-w", "1", "-m", "20", target]
                try:
                    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30, errors="replace")
                    raw = (cp.stdout or "") + (cp.stderr or "")
                    self._json(200, {"ok": True, "hops": parse_traceroute_text(raw), "raw": raw[:4000]})
                except Exception as e:
                    self._json(200, {"ok": False, "error": str(e), "hops": []})
            elif p.path == "/reset":
                self.state.udp.rx_stats.reset()
                self.state.udp.rtt_stats.reset()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="AREDN dist_mon remote VoIP agent")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP control bind address")
    parser.add_argument("--port", type=int, default=vp.DEFAULT_AGENT_PORT, help="HTTP control port")
    parser.add_argument("--udp-port", type=int, default=vp.DEFAULT_UDP_PORT, help="UDP stream/echo port")
    parser.add_argument("--key", default="", help="Agent key (generated if omitted)")
    args = parser.parse_args()

    state = AgentState(args.key, args.udp_port)
    state.udp.start()
    server = http.server.ThreadingHTTPServer((args.bind, args.port), AgentHandler)
    server.state = state  # type: ignore[attr-defined]

    print(f"{vp.APP_NAME} (protocol {vp.PROTO_VERSION})")
    print(f"HTTP control: http://{args.bind}:{args.port}  UDP probe: {args.udp_port}")
    print(f"Agent key: {state.key}")
    print("Enter this key in the dist_mon VoIP session form. Ctrl+C stops the agent.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        state.udp.stop()


if __name__ == "__main__":
    main()

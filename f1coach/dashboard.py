from __future__ import annotations

import json
import socket
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from f1coach.adapters import F124Adapter, UnsupportedPacket
from f1coach.coach import LapCoach, ReferenceProfile
from f1coach.models import PacketId
from f1coach.track_metadata import FastF1CornerMetadata


STATIC_DIR = Path(__file__).with_name("static")


class TelemetryRuntime:
    def __init__(self, reference: ReferenceProfile | None = None) -> None:
        self.adapter = F124Adapter()
        self.coach = LapCoach(external_reference=reference, corner_metadata=FastF1CornerMetadata())
        self.packet_counts: Counter[str] = Counter()
        self.paused_packet_counts: Counter[str] = Counter()
        self.last_sender: tuple[str, int] | None = None
        self.paused = False
        self.lock = threading.Lock()

    def process_packet(self, packet: bytes, sender: tuple[str, int], show_errors: bool = False) -> list[str]:
        try:
            header = self.adapter.decode_header(packet)
            packet_name = packet_name_for(header.packet_id)
        except (UnsupportedPacket, ValueError) as exc:
            if show_errors:
                print(f"Ignored packet from {sender}: {exc}", file=sys.stderr)
            return []

        with self.lock:
            self.packet_counts[packet_name] += 1
            self.last_sender = sender
            if self.paused:
                self.paused_packet_counts[packet_name] += 1
                return []

        try:
            message = self.adapter.decode(packet)
        except (UnsupportedPacket, ValueError) as exc:
            if show_errors:
                print(f"Ignored packet from {sender}: {exc}", file=sys.stderr)
            return []

        with self.lock:
            if message is None:
                return []
            return self.coach.update(message)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_unlocked()

    def pause(self) -> dict[str, Any]:
        with self.lock:
            self.paused = True
            return self._snapshot_unlocked()

    def resume(self) -> dict[str, Any]:
        with self.lock:
            self.paused = False
            return self._snapshot_unlocked()

    def start_new_session(self) -> dict[str, Any]:
        with self.lock:
            notices = self.coach.start_new_session("Manual new session started.")
            self.coach._record_notices(notices)
            self.paused_packet_counts.clear()
            return self._snapshot_unlocked()

    def set_driving_goal(self, goal: str) -> dict[str, Any]:
        with self.lock:
            notices = self.coach.set_driving_goal(goal)
            self.coach._record_notices(notices)
            return self._snapshot_unlocked()

    def set_theoretical_best(self, lap_time_ms: int | None) -> dict[str, Any]:
        with self.lock:
            notices = self.coach.set_manual_theoretical_best(lap_time_ms)
            self.coach._record_notices(notices)
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        state = self.coach.snapshot()
        state["packets"] = dict(self.packet_counts)
        state["pausedPackets"] = dict(self.paused_packet_counts)
        state["lastSender"] = self.last_sender
        state["paused"] = self.paused
        return state


def run_udp_listener(
    runtime: TelemetryRuntime,
    bind: str,
    port: int,
    show_packets: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.settimeout(0.5)

    while stop_event is None or not stop_event.is_set():
        try:
            packet, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        notices = runtime.process_packet(packet, addr, show_errors=show_packets)
        for notice in notices:
            print(notice)


def serve_dashboard(runtime: TelemetryRuntime, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in {"/", "/index.html"}:
                self._send_static("text/html; charset=utf-8", "index.html")
            elif self.path == "/app.css":
                self._send_static("text/css; charset=utf-8", "app.css")
            elif self.path == "/app.js":
                self._send_static("application/javascript; charset=utf-8", "app.js")
            elif self.path == "/state":
                self._send("application/json; charset=utf-8", json.dumps(runtime.snapshot()))
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/control":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            action = body.get("action")
            if action == "pause":
                self._send("application/json; charset=utf-8", json.dumps(runtime.pause()))
            elif action == "resume":
                self._send("application/json; charset=utf-8", json.dumps(runtime.resume()))
            elif action == "new-session":
                self._send("application/json; charset=utf-8", json.dumps(runtime.start_new_session()))
            elif action == "set-goal":
                goal = str(body.get("goal", ""))
                try:
                    state = runtime.set_driving_goal(goal)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                self._send("application/json; charset=utf-8", json.dumps(state))
            elif action == "set-theoretical-best":
                try:
                    lap_time_ms = _manual_lap_time_ms(body.get("lapTimeMs"))
                    state = runtime.set_theoretical_best(lap_time_ms)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                self._send("application/json; charset=utf-8", json.dumps(state))
            else:
                self.send_error(400, "Unknown control action")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, content_type: str, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _send_static(self, content_type: str, filename: str) -> None:
            try:
                body = (STATIC_DIR / filename).read_text(encoding="utf-8")
            except OSError:
                self.send_error(500, f"Missing dashboard asset: {filename}")
                return
            self._send(content_type, body)

    return ThreadingHTTPServer((host, port), Handler)


def packet_name_for(packet_id: int) -> str:
    try:
        return PacketId(packet_id).name.lower()
    except ValueError:
        return f"unknown_{packet_id}"


def _manual_lap_time_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        lap_time_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid theoretical best time.") from exc
    if lap_time_ms <= 0:
        raise ValueError("Theoretical best must be greater than zero.")
    return lap_time_ms

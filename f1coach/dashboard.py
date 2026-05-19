from __future__ import annotations

import json
import socket
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
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

    def _snapshot_unlocked(self) -> dict[str, Any]:
        state = self.coach.snapshot()
        state["packets"] = dict(self.packet_counts)
        state["pausedPackets"] = dict(self.paused_packet_counts)
        state["lastSender"] = self.last_sender
        state["paused"] = self.paused
        state["diagnostics"] = self._diagnostics_unlocked(state)
        return state

    def _diagnostics_unlocked(self, state: dict[str, Any]) -> list[dict[str, str]]:
        packets = state.get("packets", {})
        session = state.get("session") or {}
        completed_laps = state.get("completedLaps") or []
        reference = state.get("reference")
        current = state.get("current") or {}
        track_map = state.get("trackMap") or {}
        insights = state.get("insights") or []
        corner_metadata = state.get("cornerMetadata") or {}

        diagnostics: list[dict[str, str]] = []
        if not packets:
            diagnostics.append(
                _diagnostic(
                    "waiting",
                    "Waiting for UDP packets",
                    "No F1 24 telemetry has reached this app yet.",
                    "Check UDP Broadcast Mode, port 20777, and local firewall permissions.",
                )
            )
            return diagnostics

        diagnostics.append(
            _diagnostic(
                "ok" if not state.get("paused") else "warning",
                "Telemetry link active" if not state.get("paused") else "Capture paused",
                f"{sum(int(value) for value in packets.values())} packets received.",
                "Resume capture when you are ready for new laps." if state.get("paused") else "",
            )
        )

        required_packets = {
            "lap_data": "lap timing and completed-lap detection",
            "car_telemetry": "speed, throttle, brake, steering, and gears",
            "motion": "track map positions",
            "car_status": "ERS, fuel, tyres, and status context",
            "motion_ex": "slip and rotation context",
        }
        for packet_name, purpose in required_packets.items():
            if packets.get(packet_name, 0) == 0:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        f"Missing {packet_name.replace('_', ' ')} packets",
                        f"The app needs these for {purpose}.",
                        "Confirm the game UDP format is set to 2024 and send rate is 60Hz.",
                    )
                )

        if not session.get("trackLengthM"):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "No session metadata yet",
                    "Track name and lap distance are unavailable until a session packet arrives.",
                    "Start or resume an on-track session in F1 24.",
                )
            )

        if not completed_laps:
            diagnostics.append(
                _diagnostic(
                    "waiting",
                    "No completed lap yet",
                    "The coach needs at least one finished lap before it can compare pace.",
                    "Cross the timing line once with capture running.",
                )
            )
        elif reference is None:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "No reference lap",
                    "A clean personal best or imported reference is required for corner-level coaching.",
                    "Complete a clean lap or load a reference JSON.",
                )
            )
        elif not reference.get("samples"):
            diagnostics.append(
                _diagnostic(
                    "info",
                    "Time-only target active",
                    "The theoretical best can score lap delta but has no input trace.",
                    "Corner advice will compare against your personal-best or imported telemetry trace.",
                )
            )
        elif not insights and len(completed_laps) >= 2:
            diagnostics.append(
                _diagnostic(
                    "ok",
                    "No large loss detected",
                    "No segment crossed the current time-loss threshold against the selected reference.",
                    "Use the lap profile and trace for smaller technique details.",
                )
            )

        if current.get("sample") is None and packets.get("lap_data", 0) and packets.get("car_telemetry", 0):
            diagnostics.append(
                _diagnostic(
                    "info",
                    "Waiting for timed-lap samples",
                    "Telemetry is arriving, but no current timed lap sample is available yet.",
                    "Start a flying lap rather than sitting in the garage or menus.",
                )
            )

        if not track_map.get("samples") and packets.get("motion", 0):
            diagnostics.append(
                _diagnostic(
                    "info",
                    "Map not built yet",
                    "Motion packets are arriving, but no completed lap has usable world positions yet.",
                    "Finish a lap while motion packets are active.",
                )
            )

        if corner_metadata.get("error"):
            diagnostics.append(
                _diagnostic(
                    "info",
                    "Official turn labels unavailable",
                    str(corner_metadata["error"]),
                    "Dynamic corner labels are still available from your telemetry trace.",
                )
            )

        return diagnostics[:7]


def run_udp_listener(
    runtime: TelemetryRuntime,
    bind: str,
    port: int,
    show_packets: bool = False,
    stop_event: threading.Event | None = None,
    sock: socket.socket | None = None,
) -> None:
    owns_socket = sock is None
    if sock is None:
        sock = open_udp_socket(bind, port)

    last_status_at = 0.0
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                packet, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            notices = runtime.process_packet(packet, addr, show_errors=show_packets)
            for notice in notices:
                print(notice)
            if show_packets:
                now = monotonic()
                if now - last_status_at >= 2.0:
                    last_status_at = now
                    print(_packet_status_line(runtime.snapshot().get("packets", {})))
    finally:
        if owns_socket:
            sock.close()


def open_udp_socket(bind: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        sock.settimeout(0.5)
    except OSError:
        sock.close()
        raise
    return sock


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


def _diagnostic(level: str, title: str, detail: str, action: str = "") -> dict[str, str]:
    return {
        "level": level,
        "title": title,
        "detail": detail,
        "action": action,
    }


def _packet_status_line(packets: dict[str, Any]) -> str:
    if not packets:
        return "Packets: none"
    entries = sorted(((name, int(count)) for name, count in packets.items()), key=lambda item: item[0])
    return "Packets: " + ", ".join(f"{name}={count}" for name, count in entries)

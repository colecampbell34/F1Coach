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
from urllib.parse import parse_qs, urlparse

from f1coach.adapters import F124Adapter, UnsupportedPacket
from f1coach.coach import LapCoach, ReferenceProfile
from f1coach.models import PacketId
from f1coach.track_metadata import FastF1CornerMetadata


STATIC_DIR = Path(__file__).with_name("static")


class TelemetryRuntime:
    def __init__(self, reference: ReferenceProfile | None = None) -> None:
        self.adapter = F124Adapter()
        self.coaches = {
            "lap": LapCoach(external_reference=reference, corner_metadata=FastF1CornerMetadata()),
            "race": LapCoach(external_reference=reference, corner_metadata=FastF1CornerMetadata(), driving_goal="race"),
        }
        self.packet_counts_by_mode: dict[str, Counter[str]] = {"lap": Counter(), "race": Counter()}
        self.paused_packet_counts_by_mode: dict[str, Counter[str]] = {"lap": Counter(), "race": Counter()}
        self.last_sender_by_mode: dict[str, tuple[str, int] | None] = {"lap": None, "race": None}
        self.active_mode = "lap"
        self.paused = False
        self.lock = threading.Lock()

    @property
    def coach(self) -> LapCoach:
        return self.coaches[self.active_mode]

    @property
    def packet_counts(self) -> Counter[str]:
        return self.packet_counts_by_mode[self.active_mode]

    @property
    def paused_packet_counts(self) -> Counter[str]:
        return self.paused_packet_counts_by_mode[self.active_mode]

    @property
    def last_sender(self) -> tuple[str, int] | None:
        return self.last_sender_by_mode[self.active_mode]

    def process_packet(self, packet: bytes, sender: tuple[str, int], show_errors: bool = False) -> list[str]:
        try:
            header = self.adapter.decode_header(packet)
            packet_name = packet_name_for(header.packet_id)
        except (UnsupportedPacket, ValueError) as exc:
            if show_errors:
                print(f"Ignored packet from {sender}: {exc}", file=sys.stderr)
            return []

        with self.lock:
            mode = self.active_mode
            self.packet_counts_by_mode[mode][packet_name] += 1
            self.last_sender_by_mode[mode] = sender
            if self.paused:
                self.paused_packet_counts_by_mode[mode][packet_name] += 1
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
            return self.coaches[mode].update(message)

    def snapshot(self, mode: str | None = None) -> dict[str, Any]:
        with self.lock:
            if mode is not None:
                self.active_mode = normalize_dashboard_mode(mode)
            return self._snapshot_unlocked(self.active_mode)

    def pause(self, mode: str | None = None) -> dict[str, Any]:
        with self.lock:
            if mode is not None:
                self.active_mode = normalize_dashboard_mode(mode)
            self.paused = True
            return self._snapshot_unlocked(self.active_mode)

    def resume(self, mode: str | None = None) -> dict[str, Any]:
        with self.lock:
            if mode is not None:
                self.active_mode = normalize_dashboard_mode(mode)
            self.paused = False
            return self._snapshot_unlocked(self.active_mode)

    def start_new_session(self, mode: str | None = None) -> dict[str, Any]:
        with self.lock:
            active_mode = normalize_dashboard_mode(mode) if mode is not None else self.active_mode
            self.active_mode = active_mode
            coach = self.coaches[active_mode]
            label = "race review" if active_mode == "race" else "lap review"
            notices = coach.start_new_session(f"Manual new {label} started.")
            coach._record_notices(notices)
            self.paused_packet_counts_by_mode[active_mode].clear()
            return self._snapshot_unlocked(active_mode)

    def set_driving_goal(self, goal: str, mode: str | None = None) -> dict[str, Any]:
        with self.lock:
            active_mode = normalize_dashboard_mode(mode) if mode is not None else self.active_mode
            self.active_mode = active_mode
            notices = self.coaches[active_mode].set_driving_goal(goal)
            self.coaches[active_mode]._record_notices(notices)
            return self._snapshot_unlocked(active_mode)

    def _snapshot_unlocked(self, mode: str) -> dict[str, Any]:
        state = self.coaches[mode].snapshot()
        state["dashboardMode"] = mode
        state["packets"] = dict(self.packet_counts_by_mode[mode])
        state["pausedPackets"] = dict(self.paused_packet_counts_by_mode[mode])
        state["lastSender"] = self.last_sender_by_mode[mode]
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
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_response(302)
                self.send_header("Location", "/lap-review")
                self.end_headers()
            elif parsed.path in {"/lap-review", "/race-overview", "/index.html"}:
                self._send_static("text/html; charset=utf-8", "index.html")
            elif parsed.path == "/app.css":
                self._send_static("text/css; charset=utf-8", "app.css")
            elif parsed.path == "/app.js":
                self._send_static("application/javascript; charset=utf-8", "app.js")
            elif parsed.path == "/state":
                mode = _mode_from_query(parsed.query)
                self._send("application/json; charset=utf-8", json.dumps(runtime.snapshot(mode)))
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/control":
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
            mode = normalize_dashboard_mode(str(body.get("mode", runtime.active_mode)))
            if action == "pause":
                self._send("application/json; charset=utf-8", json.dumps(runtime.pause(mode)))
            elif action == "resume":
                self._send("application/json; charset=utf-8", json.dumps(runtime.resume(mode)))
            elif action == "new-session":
                self._send("application/json; charset=utf-8", json.dumps(runtime.start_new_session(mode)))
            elif action == "set-goal":
                goal = str(body.get("goal", ""))
                try:
                    state = runtime.set_driving_goal(goal, mode)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                self._send("application/json; charset=utf-8", json.dumps(state))
            elif action == "set-dashboard-mode":
                self._send("application/json; charset=utf-8", json.dumps(runtime.snapshot(mode)))
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


def normalize_dashboard_mode(mode: str) -> str:
    normalized = mode.strip().lower().replace("_", "-")
    if normalized in {"lap", "lap-review", "single-lap", "single-lap-review"}:
        return "lap"
    if normalized in {"race", "race-overview", "full-race", "full-race-overview"}:
        return "race"
    return "lap"


def _mode_from_query(query: str) -> str:
    values = parse_qs(query).get("mode", ["lap"])
    return normalize_dashboard_mode(values[0])


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

from __future__ import annotations

import json
import socket
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from f1coach.adapters import F124Adapter, UnsupportedPacket
from f1coach.coach import LapCoach, ReferenceProfile
from f1coach.models import PacketId


class TelemetryRuntime:
    def __init__(self, reference: ReferenceProfile | None = None) -> None:
        self.adapter = F124Adapter()
        self.coach = LapCoach(external_reference=reference)
        self.packet_counts: Counter[str] = Counter()
        self.last_sender: tuple[str, int] | None = None
        self.lock = threading.Lock()

    def process_packet(self, packet: bytes, sender: tuple[str, int], show_errors: bool = False) -> list[str]:
        try:
            header = self.adapter.decode_header(packet)
            packet_name = packet_name_for(header.packet_id)
            message = self.adapter.decode(packet)
        except (UnsupportedPacket, ValueError) as exc:
            if show_errors:
                print(f"Ignored packet from {sender}: {exc}", file=sys.stderr)
            return []

        with self.lock:
            self.packet_counts[packet_name] += 1
            self.last_sender = sender
            if message is None:
                return []
            return self.coach.update(message)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = self.coach.snapshot()
            state["packets"] = dict(self.packet_counts)
            state["lastSender"] = self.last_sender
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
                self._send("text/html; charset=utf-8", INDEX_HTML)
            elif self.path == "/app.css":
                self._send("text/css; charset=utf-8", APP_CSS)
            elif self.path == "/app.js":
                self._send("application/javascript; charset=utf-8", APP_JS)
            elif self.path == "/state":
                self._send("application/json; charset=utf-8", json.dumps(runtime.snapshot()))
            else:
                self.send_error(404)

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

    return ThreadingHTTPServer((host, port), Handler)


def packet_name_for(packet_id: int) -> str:
    try:
        return PacketId(packet_id).name.lower()
    except ValueError:
        return f"unknown_{packet_id}"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>F1Coach</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <h1>F1Coach</h1>
      <p id="sessionLine">Waiting for telemetry</p>
    </div>
    <div class="status">
      <span id="packetStatus">0 packets</span>
      <span id="referenceStatus">No reference</span>
    </div>
  </header>

  <main class="shell">
    <section class="panel metrics">
      <div><span>Speed</span><strong id="speed">--</strong></div>
      <div><span>Gear</span><strong id="gear">--</strong></div>
      <div><span>Throttle</span><strong id="throttle">--</strong></div>
      <div><span>Brake</span><strong id="brake">--</strong></div>
      <div><span>Delta</span><strong id="delta">--</strong></div>
      <div><span>ERS</span><strong id="ers">--</strong></div>
    </section>

    <section class="layout">
      <div class="panel mapPanel">
        <div class="panelHeader">
          <h2>Track Map</h2>
          <span id="mapHint">World-position trace</span>
        </div>
        <canvas id="trackMap" width="900" height="560"></canvas>
      </div>

      <aside class="panel">
        <div class="panelHeader"><h2>Priority Areas</h2></div>
        <div id="insights" class="insights"></div>
      </aside>
    </section>

    <section class="panel">
      <div class="panelHeader"><h2>Telemetry Trace</h2><span>Speed, throttle, brake vs lap distance</span></div>
      <canvas id="trace" width="1200" height="300"></canvas>
    </section>

    <section class="layout bottom">
      <div class="panel">
        <div class="panelHeader"><h2>Lap History</h2></div>
        <table>
          <thead><tr><th>Lap</th><th>Time</th><th>S1</th><th>S2</th><th>S3</th><th>Status</th></tr></thead>
          <tbody id="lapTable"></tbody>
        </table>
      </div>
      <div class="panel">
        <div class="panelHeader"><h2>Coach Feed</h2></div>
        <div id="notices" class="feed"></div>
      </div>
    </section>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""


APP_CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0d10;
  --panel: #121820;
  --panel-2: #17202a;
  --line: #263544;
  --text: #edf3f8;
  --muted: #91a3b5;
  --green: #32d583;
  --red: #ff5c7a;
  --amber: #f5b84b;
  --blue: #5fb5ff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: center;
  padding: 18px 28px;
  border-bottom: 1px solid var(--line);
  background: #0d1218;
}
h1, h2, p { margin: 0; }
h1 { font-size: 24px; letter-spacing: 0; }
h2 { font-size: 15px; letter-spacing: 0; }
p, span, td, th { color: var(--muted); }
.status { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
.status span {
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
}
.shell { padding: 18px; display: grid; gap: 18px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.panelHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
}
.metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
}
.metrics div {
  padding: 14px 16px;
  border-right: 1px solid var(--line);
}
.metrics div:last-child { border-right: 0; }
.metrics span { display: block; font-size: 12px; }
.metrics strong { display: block; margin-top: 4px; font-size: 28px; letter-spacing: 0; }
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(320px, .8fr);
  gap: 18px;
}
.bottom { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
canvas { display: block; width: 100%; height: auto; background: #0b1117; }
.mapPanel canvas { min-height: 420px; }
.insights, .feed { padding: 12px; display: grid; gap: 10px; }
.insight {
  border: 1px solid var(--line);
  border-left: 4px solid var(--amber);
  border-radius: 6px;
  padding: 10px;
  background: var(--panel-2);
}
.insight.high { border-left-color: var(--red); }
.insight.low { border-left-color: var(--blue); }
.insight strong { display: block; margin-bottom: 4px; }
.feed div {
  padding: 9px 10px;
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--text);
}
table { width: 100%; border-collapse: collapse; }
th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  font-size: 13px;
}
th { color: var(--text); font-weight: 600; }
@media (max-width: 960px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .layout, .bottom { grid-template-columns: 1fr; }
}
"""


APP_JS = """
const stateUrl = "/state";
const mapCanvas = document.getElementById("trackMap");
const traceCanvas = document.getElementById("trace");

function fmtMs(ms) {
  if (ms === null || ms === undefined) return "--";
  const sign = ms > 0 ? "+" : ms < 0 ? "-" : "";
  const abs = Math.abs(ms);
  return sign + (abs / 1000).toFixed(2) + "s";
}

function pct(value) {
  if (value === null || value === undefined) return "--";
  return Math.round(value * 100) + "%";
}

function lapTime(ms) {
  if (!ms && ms !== 0) return "--";
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const x = String(ms % 1000).padStart(3, "0");
  return `${m}:${String(s).padStart(2, "0")}.${x}`;
}

async function refresh() {
  const response = await fetch(stateUrl, { cache: "no-store" });
  const state = await response.json();
  renderMetrics(state);
  renderInsights(state.insights || []);
  renderLaps(state.completedLaps || []);
  renderFeed(state.notices || []);
  renderMap(state);
  renderTrace(state);
}

function renderMetrics(state) {
  const sample = state.current.sample;
  const packets = Object.values(state.packets || {}).reduce((a, b) => a + b, 0);
  document.getElementById("packetStatus").textContent = `${packets} packets`;
  document.getElementById("sessionLine").textContent = state.session.trackLengthM
    ? `Track ${state.session.trackId}, ${state.session.trackLengthM} m`
    : "Waiting for session packet";
  document.getElementById("referenceStatus").textContent = state.reference
    ? `${state.reference.name} ${state.reference.lapTime}`
    : "No reference";

  document.getElementById("speed").textContent = sample ? `${sample.speedKmh}` : "--";
  document.getElementById("gear").textContent = sample ? `${sample.gear}` : "--";
  document.getElementById("throttle").textContent = sample ? pct(sample.throttle) : "--";
  document.getElementById("brake").textContent = sample ? pct(sample.brake) : "--";
  document.getElementById("ers").textContent = sample && sample.ersPercent !== null ? `${Math.round(sample.ersPercent)}%` : "--";

  let delta = "--";
  if (sample && state.reference && state.reference.samples.length) {
    const ref = nearestByDistance(state.reference.samples, sample.normalizedDistance);
    if (ref) delta = fmtMs(sample.lapTimeMs - ref.lapTimeMs);
  }
  document.getElementById("delta").textContent = delta;
}

function renderInsights(insights) {
  const target = document.getElementById("insights");
  target.innerHTML = "";
  if (!insights.length) {
    target.innerHTML = `<div class="insight low"><strong>No loss map yet</strong><span>Complete clean laps to build the reference.</span></div>`;
    return;
  }
  for (const item of insights) {
    const div = document.createElement("div");
    div.className = `insight ${item.severity}`;
    div.innerHTML = `<strong>${item.category} · ${fmtMs(item.time_delta_ms)}</strong><span>${item.detail}</span>`;
    target.appendChild(div);
  }
}

function renderLaps(laps) {
  const body = document.getElementById("lapTable");
  body.innerHTML = "";
  for (const lap of [...laps].reverse()) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${lap.lapNum}</td><td>${lap.lapTime}</td><td>${lapTime(lap.sector1Ms)}</td><td>${lapTime(lap.sector2Ms)}</td><td>${lapTime(lap.sector3Ms)}</td><td>${lap.invalid ? "Invalid" : "Clean"}</td>`;
    body.appendChild(row);
  }
}

function renderFeed(notices) {
  const feed = document.getElementById("notices");
  feed.innerHTML = "";
  for (const notice of [...notices].reverse()) {
    const div = document.createElement("div");
    div.textContent = notice;
    feed.appendChild(div);
  }
}

function renderMap(state) {
  const ctx = mapCanvas.getContext("2d");
  const w = mapCanvas.width;
  const h = mapCanvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b1117";
  ctx.fillRect(0, 0, w, h);

  const ref = (state.reference && state.reference.samples || []).filter(hasPos);
  const current = (state.current.samples || []).filter(hasPos);
  const points = ref.length ? ref : current;
  if (!points.length) {
    centerText(ctx, w, h, "Waiting for motion packets to draw the track map");
    return;
  }
  const bounds = getBounds([...ref, ...current]);
  drawPath(ctx, ref, bounds, "#425161", 5);
  for (const insight of state.insights || []) {
    const segment = ref.filter(s => s.normalizedDistance >= insight.start_pct / 100 && s.normalizedDistance <= insight.end_pct / 100);
    drawPath(ctx, segment, bounds, insight.severity === "high" ? "#ff5c7a" : "#f5b84b", 7);
  }
  drawPath(ctx, current, bounds, "#5fb5ff", 3);
  const latest = current[current.length - 1];
  if (latest) {
    const [x, y] = project(latest, bounds);
    ctx.fillStyle = "#32d583";
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fill();
  }
}

function renderTrace(state) {
  const ctx = traceCanvas.getContext("2d");
  const w = traceCanvas.width;
  const h = traceCanvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b1117";
  ctx.fillRect(0, 0, w, h);
  const samples = state.current.samples || [];
  const ref = state.reference ? state.reference.samples || [] : [];
  drawTrace(ctx, ref, w, h, "speedKmh", 360, "#425161");
  drawTrace(ctx, samples, w, h, "speedKmh", 360, "#5fb5ff");
  drawTrace(ctx, samples, w, h, "throttle", 1, "#32d583");
  drawTrace(ctx, samples, w, h, "brake", 1, "#ff5c7a");
}

function drawTrace(ctx, samples, w, h, key, max, color) {
  if (!samples.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = key === "speedKmh" ? 2 : 1.5;
  ctx.beginPath();
  samples.forEach((s, i) => {
    const x = 40 + s.normalizedDistance * (w - 70);
    const y = h - 24 - Math.max(0, Math.min(1, s[key] / max)) * (h - 48);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawPath(ctx, samples, bounds, color, width) {
  if (samples.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  samples.forEach((s, i) => {
    const [x, y] = project(s, bounds);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function getBounds(samples) {
  const xs = samples.map(s => s.worldPosition[0]);
  const ys = samples.map(s => s.worldPosition[2]);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
}

function project(sample, bounds) {
  const pad = 32;
  const sx = (mapCanvas.width - pad * 2) / Math.max(1, bounds.maxX - bounds.minX);
  const sy = (mapCanvas.height - pad * 2) / Math.max(1, bounds.maxY - bounds.minY);
  const scale = Math.min(sx, sy);
  const x = pad + (sample.worldPosition[0] - bounds.minX) * scale;
  const y = mapCanvas.height - pad - (sample.worldPosition[2] - bounds.minY) * scale;
  return [x, y];
}

function hasPos(sample) {
  return sample.worldPosition && sample.worldPosition.length === 3;
}

function nearestByDistance(samples, distance) {
  let best = null;
  let bestDiff = Infinity;
  for (const sample of samples) {
    const diff = Math.abs(sample.normalizedDistance - distance);
    if (diff < bestDiff) {
      best = sample;
      bestDiff = diff;
    }
  }
  return best;
}

function centerText(ctx, w, h, text) {
  ctx.fillStyle = "#91a3b5";
  ctx.font = "16px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(text, w / 2, h / 2);
}

setInterval(() => refresh().catch(console.error), 500);
refresh().catch(console.error);
"""

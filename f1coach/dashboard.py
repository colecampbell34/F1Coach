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
from f1coach.track_metadata import FastF1CornerMetadata


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
      <div class="goalToggle" role="group" aria-label="Coaching goal">
        <button data-goal="qualifying" type="button">Qualifying</button>
        <button data-goal="race" type="button">Race Pace</button>
      </div>
      <button id="pauseButton" type="button">Pause Listening</button>
      <button id="newSessionButton" type="button">New Session</button>
      <span id="packetStatus">0 packets</span>
      <span id="referenceStatus">No reference</span>
    </div>
  </header>

  <main class="shell">
    <section class="dashboardGrid">
      <div class="panel mapPanel">
        <div class="panelHeader">
          <h2>Selected Lap Delta Map</h2>
          <span id="mapHint">Select a completed lap</span>
        </div>
        <canvas id="trackMap" width="720" height="520"></canvas>
        <div class="mapLegend">
          <span><i class="gain"></i>Gain</span>
          <span><i class="neutral"></i>Even</span>
          <span><i class="loss"></i>Loss</span>
        </div>
      </div>

      <aside class="panel priorityPanel">
        <div class="panelHeader"><h2>Priority Areas</h2></div>
        <div id="insights" class="insights"></div>
        <div class="panelHeader subhead"><h2>Setup / Energy Trends</h2></div>
        <div id="setupInsights" class="feed compact"></div>
      </aside>

      <section class="panel inputTracePanel">
        <div class="panelHeader"><h2>Selected Lap Trace</h2><span>Throttle and brake vs lap distance</span></div>
        <canvas id="trace" width="760" height="120"></canvas>
      </section>

      <div class="panel lapPanel">
        <div class="panelHeader"><h2>Lap History</h2></div>
        <table>
          <thead><tr><th>Lap</th><th>Time</th><th>Delta</th><th>ERS</th><th>Status</th></tr></thead>
          <tbody id="lapTable"></tbody>
        </table>
      </div>
      <div class="panel feedPanel">
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
html { height: 100%; }
body {
  margin: 0;
  height: 100%;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
  background: #0d1218;
}
h1, h2, p { margin: 0; }
h1 { font-size: 20px; letter-spacing: 0; }
h2 { font-size: 15px; letter-spacing: 0; }
p, span, td, th { color: var(--muted); }
.status { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.goalToggle {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #091018;
}
.status span,
.status button {
  padding: 6px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--muted);
  font: inherit;
}
.status button {
  cursor: pointer;
  color: var(--text);
}
.status button:hover {
  border-color: var(--blue);
}
.status button.active {
  border-color: var(--amber);
  color: var(--amber);
}
.goalToggle button {
  border-color: transparent;
  background: transparent;
  border-radius: 6px;
}
.goalToggle button.active {
  background: var(--panel-2);
}
.shell {
  min-height: 0;
  padding: 10px;
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  gap: 10px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  min-height: 0;
}
.panelHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 38px;
  padding: 9px 12px;
  border-bottom: 1px solid var(--line);
}
.dashboardGrid {
  display: grid;
  min-height: 0;
  gap: 10px;
  grid-template-columns: minmax(320px, 1.02fr) minmax(360px, 1.08fr) minmax(320px, .9fr);
  grid-template-rows: 220px minmax(0, 1fr) 150px;
}
.mapPanel {
  display: grid;
  grid-column: 1;
  grid-row: 1 / span 2;
  grid-template-rows: auto minmax(0, 1fr) auto;
}
.priorityPanel {
  display: grid;
  grid-column: 2;
  grid-row: 1 / span 3;
  grid-template-rows: auto minmax(0, 1fr) auto minmax(82px, .46fr);
}
.inputTracePanel {
  display: grid;
  grid-column: 1;
  grid-row: 3;
  grid-template-rows: auto minmax(0, 1fr);
}
.lapPanel {
  grid-column: 3;
  grid-row: 1;
  overflow: auto;
}
.feedPanel {
  display: grid;
  grid-column: 3;
  grid-row: 2 / span 2;
  grid-template-rows: auto minmax(0, 1fr);
}
canvas { display: block; width: 100%; height: auto; background: #0b1117; }
.mapPanel canvas {
  height: 100%;
  min-height: 0;
}
.mapLegend {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: center;
  min-height: 30px;
  border-top: 1px solid var(--line);
  font-size: 12px;
}
.mapLegend span {
  display: inline-flex;
  gap: 5px;
  align-items: center;
}
.mapLegend i {
  display: inline-block;
  width: 22px;
  height: 4px;
  border-radius: 4px;
}
.mapLegend .gain { background: var(--green); }
.mapLegend .neutral { background: var(--amber); }
.mapLegend .loss { background: var(--red); }
.inputTracePanel canvas {
  height: 100%;
  min-height: 0;
}
.insights, .feed {
  min-height: 0;
  overflow: auto;
  padding: 8px;
  display: grid;
  align-content: start;
  gap: 8px;
}
.subhead { border-top: 1px solid var(--line); }
.insight {
  border: 1px solid var(--line);
  border-left: 4px solid var(--amber);
  border-radius: 6px;
  padding: 8px;
  background: var(--panel-2);
  cursor: pointer;
}
.insight.high { border-left-color: var(--red); }
.insight.low { border-left-color: var(--blue); }
.insight.selected {
  border-color: var(--blue);
  box-shadow: inset 0 0 0 1px var(--blue);
}
.insight strong { display: block; margin-bottom: 6px; color: var(--text); }
.insight span { display: block; line-height: 1.32; }
.insight .area { color: var(--muted); font-size: 12px; margin-bottom: 5px; }
.insight .action { color: var(--text); }
.insight .evidence { color: var(--blue); margin-top: 5px; font-size: 12px; }
.insight .setup { color: var(--amber); margin-top: 5px; font-size: 12px; }
.lapPanel table { min-width: 0; }
.lapPanel tr { cursor: pointer; }
.lapPanel tr.selected { background: #1b2a37; }
.lapPanel td, .lapPanel th { white-space: nowrap; }
.feed div {
  padding: 7px 8px;
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--text);
}
.feed.compact div { font-size: 13px; line-height: 1.4; }
table { width: 100%; border-collapse: collapse; }
th, td {
  padding: 7px 9px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  font-size: 13px;
}
th { color: var(--text); font-weight: 600; }
@media (max-width: 1100px) {
  body { overflow: auto; display: block; }
  .topbar { align-items: flex-start; flex-direction: column; }
  .shell { height: auto; grid-template-rows: auto; }
  .dashboardGrid { display: grid; grid-template-columns: 1fr; grid-template-rows: none; }
  .mapPanel, .priorityPanel, .inputTracePanel, .lapPanel, .feedPanel {
    grid-column: auto;
    grid-row: auto;
  }
  .mapPanel canvas { min-height: 300px; }
  .inputTracePanel canvas { min-height: 110px; }
}
"""


APP_JS = """
const stateUrl = "/state";
const mapCanvas = document.getElementById("trackMap");
const traceCanvas = document.getElementById("trace");
const pauseButton = document.getElementById("pauseButton");
const newSessionButton = document.getElementById("newSessionButton");
const goalButtons = Array.from(document.querySelectorAll("[data-goal]"));
let selectedLapNum = null;
let selectedInsightIndex = null;

function initControls() {
  goalButtons.forEach(button => {
    button.addEventListener("click", async () => {
      await sendControl("set-goal", { goal: button.dataset.goal });
      await refresh();
    });
  });
  pauseButton.addEventListener("click", async () => {
    const paused = pauseButton.dataset.paused === "true";
    await sendControl(paused ? "resume" : "pause");
    await refresh();
  });
  newSessionButton.addEventListener("click", async () => {
    const confirmed = window.confirm("Start a new session? This clears the current session laps, map, and ideal reference.");
    if (!confirmed) return;
    await sendControl("new-session");
    await refresh();
  });
}

async function sendControl(action, payload = {}) {
  const response = await fetch("/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  if (!response.ok) {
    throw new Error(`Control action failed: ${action}`);
  }
}

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

function fmtKj(value) {
  if (value === null || value === undefined) return "--";
  return `${Math.round(value)} kJ`;
}

function selectedLapFromState(state) {
  const laps = state.completedLaps || [];
  if (selectedLapNum === null && laps.length) {
    selectedLapNum = laps[laps.length - 1].lapNum;
  }
  return laps.find(lap => lap.lapNum === selectedLapNum) || laps[laps.length - 1] || null;
}

function shortSentence(text) {
  if (!text) return "";
  const sentence = String(text).split(/(?<=[.!?])\\s+/)[0] || String(text);
  return sentence.length > 150 ? sentence.slice(0, 147).trimEnd() + "..." : sentence;
}

function titleCase(text) {
  return String(text || "")
    .replace(/-/g, " ")
    .replace(/\\b\\w/g, c => c.toUpperCase());
}

function specificTip(item) {
  const category = item.category || "pace";
  const speedLoss = Math.round(Math.abs(item.speed_delta_kmh || 0));
  if (category === "braking") return `Release brake earlier here. You are ${speedLoss} km/h down.`;
  if (category === "throttle") return "Start throttle pickup earlier after rotation.";
  if (category === "minimum-speed") return `Carry more apex speed. You are ${speedLoss} km/h down.`;
  if (category === "steering") return "Reduce steering angle before adding speed.";
  if (category === "traction") return "Unwind steering before full throttle.";
  if (category === "ERS deployment") return "Use more ERS on this exit.";
  if (category === "ERS waste") return "Save ERS until the car can accelerate.";
  return "Review this speed trace first.";
}

async function refresh() {
  const response = await fetch(stateUrl, { cache: "no-store" });
  const state = await response.json();
  renderMetrics(state);
  renderInsights(state.insights || []);
  renderSetupInsights(state.setupInsights || []);
  renderLaps(state.completedLaps || []);
  renderFeed(state.notices || []);
  renderMap(state);
  renderTrace(state);
}

function renderMetrics(state) {
  const packets = Object.values(state.packets || {}).reduce((a, b) => a + b, 0);
  document.getElementById("packetStatus").textContent = `${packets} packets`;
  pauseButton.dataset.paused = state.paused ? "true" : "false";
  pauseButton.textContent = state.paused ? "Resume Listening" : "Pause Listening";
  pauseButton.classList.toggle("active", Boolean(state.paused));
  goalButtons.forEach(button => {
    button.classList.toggle("active", button.dataset.goal === state.drivingGoal);
  });
  const trackLabel = state.session.trackName || (
    state.session.trackId !== null && state.session.trackId !== undefined ? `Track ${state.session.trackId}` : "Track"
  );
  const goalLabel = state.drivingGoal === "race" ? "race pace" : "qualifying";
  document.getElementById("sessionLine").textContent = state.session.trackLengthM
    ? `${trackLabel}, ${state.session.trackLengthM} m · ${goalLabel}${state.paused ? " · paused" : ""}`
    : "Waiting for session packet";
  document.getElementById("referenceStatus").textContent = state.reference
    ? `${state.reference.name} ${state.reference.lapTime}${state.reference.segments && state.reference.segments.length ? ` · ${state.reference.segments.length} sectors` : ""}`
    : "No reference";
}

function renderInsights(insights) {
  const target = document.getElementById("insights");
  target.innerHTML = "";
  if (!insights.length) {
    target.innerHTML = `<div class="insight low"><strong>No loss map yet</strong><span>Complete clean laps to build the reference.</span></div>`;
    return;
  }
  for (const item of insights) {
    const index = insights.indexOf(item);
    const div = document.createElement("div");
    div.className = `insight ${item.severity}${selectedInsightIndex === index ? " selected" : ""}`;
    div.addEventListener("click", () => {
      selectedInsightIndex = selectedInsightIndex === index ? null : index;
      refresh().catch(console.error);
    });
    const area = item.area || `${Math.round(item.start_pct)}-${Math.round(item.end_pct)}% lap`;
    const action = specificTip(item);
    const setup = item.setup_hint ? `<span class="setup">${shortSentence(item.setup_hint)}</span>` : "";
    const source = item.reference_source ? `<span class="evidence">${item.reference_source}</span>` : "";
    div.innerHTML = `<strong>${titleCase(item.category)} · ${fmtMs(item.time_delta_ms)}</strong><span class="area">${area}</span><span class="action">${action}</span>${source}<span class="evidence">${item.evidence}</span>${setup}`;
    target.appendChild(div);
  }
}

function renderSetupInsights(items) {
  const target = document.getElementById("setupInsights");
  target.innerHTML = "";
  if (!items.length) {
    const div = document.createElement("div");
    div.textContent = "No repeated setup or ERS trend detected yet.";
    target.appendChild(div);
    return;
  }
  for (const item of items) {
    const div = document.createElement("div");
    div.textContent = item;
    target.appendChild(div);
  }
}

function renderLaps(laps) {
  const body = document.getElementById("lapTable");
  body.innerHTML = "";
  const selectable = laps.filter(lap => lap.samples && lap.samples.length);
  if (selectedLapNum === null && selectable.length) {
    selectedLapNum = selectable[selectable.length - 1].lapNum;
  }
  if (selectedLapNum !== null && selectable.length && !selectable.some(lap => lap.lapNum === selectedLapNum)) {
    selectedLapNum = selectable[selectable.length - 1].lapNum;
  }
  for (const lap of [...laps].reverse()) {
    const row = document.createElement("tr");
    row.classList.toggle("selected", lap.lapNum === selectedLapNum);
    row.addEventListener("click", () => {
      selectedLapNum = lap.lapNum;
      selectedInsightIndex = null;
      refresh().catch(console.error);
    });
    const status = lap.invalid ? "Invalid" : lap.gameInvalid ? "Clean (practice flag)" : "Clean";
    row.innerHTML = `<td>${lap.lapNum}</td><td>${lap.lapTime}</td><td>${fmtMs(lap.deltaToReferenceMs)}</td><td>${fmtKj(lap.ersUsedKj)}</td><td>${status}</td>`;
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

  const selectedLap = selectedLapFromState(state);
  const selectedSamples = orderedMapSamples(selectedLap && selectedLap.samples || []);
  const fallbackSamples = orderedMapSamples(state.trackMap && state.trackMap.samples || []);
  const ref = selectedSamples.length ? selectedSamples : fallbackSamples;
  const referenceSamples = orderedSamples(state.reference && state.reference.samples ? state.reference.samples : []);
  document.getElementById("mapHint").textContent = selectedLap
    ? `Lap ${selectedLap.lapNum} vs ${state.reference ? state.reference.name : "ideal lap pending"}`
    : "Complete a lap to review delta";
  if (!ref.length) {
    centerText(ctx, w, h, "Complete a clean lap with motion packets to lock the static map");
    return;
  }
  const bounds = getBounds(ref);
  drawGroupedPath(ctx, ref, bounds, "#425161", 5);
  if (selectedSamples.length >= 2 && referenceSamples.length) {
    drawDeltaPath(ctx, selectedSamples, referenceSamples, bounds);
  } else if (selectedSamples.length >= 2) {
    drawGroupedPath(ctx, selectedSamples, bounds, "#5fb5ff", 4);
  }
  const selectedInsight = (state.insights || [])[selectedInsightIndex];
  if (selectedInsight) {
    const segment = ref.filter(s => s.normalizedDistance >= selectedInsight.start_pct / 100 && s.normalizedDistance <= selectedInsight.end_pct / 100);
    drawGroupedPath(ctx, segment, bounds, "#ffffff", 10);
    drawGroupedPath(ctx, segment, bounds, "#5fb5ff", 6);
  }
}

function drawDeltaPath(ctx, samples, referenceSamples, bounds) {
  if (samples.length < 2) return;
  ctx.lineWidth = 6;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const group of sampleGroups(samples)) {
    for (let i = 1; i < group.length; i += 1) {
      const previous = group[i - 1];
      const current = group[i];
      const refPreviousMs = timeAtDistance(referenceSamples, previous.normalizedDistance);
      const refCurrentMs = timeAtDistance(referenceSamples, current.normalizedDistance);
      if (refPreviousMs === null || refCurrentMs === null) continue;
      const lapSegmentMs = current.lapTimeMs - previous.lapTimeMs;
      const refSegmentMs = refCurrentMs - refPreviousMs;
      if (lapSegmentMs <= 0 || refSegmentMs <= 0) continue;
      ctx.strokeStyle = deltaColor(lapSegmentMs - refSegmentMs);
      const [x1, y1] = project(previous, bounds);
      const [x2, y2] = project(current, bounds);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
  }
}

function deltaColor(deltaMs) {
  if (deltaMs <= -50) return "#32d583";
  if (deltaMs >= 80) return "#ff5c7a";
  return "#f5b84b";
}

function renderTrace(state) {
  const ctx = traceCanvas.getContext("2d");
  const w = traceCanvas.width;
  const h = traceCanvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b1117";
  ctx.fillRect(0, 0, w, h);
  const selectedLap = selectedLapFromState(state);
  const samples = selectedLap && selectedLap.samples ? selectedLap.samples : [];
  drawTraceGrid(ctx, w, h);
  drawTrace(ctx, samples, w, h, "throttle", 1, "#32d583");
  drawTrace(ctx, samples, w, h, "brake", 1, "#ff5c7a");
}

function drawTraceGrid(ctx, w, h) {
  ctx.strokeStyle = "#263544";
  ctx.lineWidth = 1;
  for (const pct of [0.25, 0.5, 0.75]) {
    const y = h - 18 - pct * (h - 36);
    ctx.beginPath();
    ctx.moveTo(32, y);
    ctx.lineTo(w - 24, y);
    ctx.stroke();
  }
}

function drawTrace(ctx, samples, w, h, key, max, color) {
  samples = orderedSamples(samples);
  if (!samples.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  samples.forEach((s, i) => {
    const x = 40 + s.normalizedDistance * (w - 70);
    const y = h - 24 - Math.max(0, Math.min(1, s[key] / max)) * (h - 48);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawGroupedPath(ctx, samples, bounds, color, width) {
  for (const group of sampleGroups(samples)) {
    drawPath(ctx, group, bounds, color, width);
  }
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
  samples = orderedMapSamples(samples);
  const xs = samples.map(s => s.worldPosition[0]);
  const ys = samples.map(s => s.worldPosition[2]);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
}

function project(sample, bounds) {
  const pad = 32;
  const spanX = Math.max(1, bounds.maxX - bounds.minX);
  const spanY = Math.max(1, bounds.maxY - bounds.minY);
  const sx = (mapCanvas.width - pad * 2) / spanX;
  const sy = (mapCanvas.height - pad * 2) / spanY;
  const scale = Math.min(sx, sy);
  const offsetX = (mapCanvas.width - spanX * scale) / 2;
  const offsetY = (mapCanvas.height - spanY * scale) / 2;
  const x = offsetX + (sample.worldPosition[0] - bounds.minX) * scale;
  const y = offsetY + (sample.worldPosition[2] - bounds.minY) * scale;
  return [x, y];
}

function hasPos(sample) {
  return sample.worldPosition && sample.worldPosition.length === 3;
}

function orderedMapSamples(samples) {
  return orderedSamples(samples).filter(hasPos);
}

function orderedSamples(samples) {
  const byBucket = new Map();
  for (const sample of samples || []) {
    if (sample.normalizedDistance === null || sample.normalizedDistance === undefined) continue;
    if (sample.normalizedDistance < 0 || sample.normalizedDistance > 1) continue;
    const bucket = Math.round(sample.normalizedDistance * 1000);
    byBucket.set(bucket, sample);
  }
  return Array.from(byBucket.values()).sort((a, b) => a.normalizedDistance - b.normalizedDistance);
}

function sampleGroups(samples) {
  const ordered = orderedMapSamples(samples);
  const groups = [];
  let current = [];
  for (const sample of ordered) {
    const previous = current[current.length - 1];
    if (previous && sample.normalizedDistance - previous.normalizedDistance > 0.04) {
      if (current.length >= 2) groups.push(current);
      current = [];
    }
    current.push(sample);
  }
  if (current.length >= 2) groups.push(current);
  return groups;
}

function timeAtDistance(samples, distance) {
  const ordered = orderedSamples(samples);
  if (!ordered.length) return null;
  if (distance <= ordered[0].normalizedDistance) return ordered[0].lapTimeMs;
  for (let i = 1; i < ordered.length; i += 1) {
    const previous = ordered[i - 1];
    const current = ordered[i];
    if (previous.normalizedDistance <= distance && distance <= current.normalizedDistance) {
      const span = current.normalizedDistance - previous.normalizedDistance;
      if (span <= 0) return current.lapTimeMs;
      const ratio = (distance - previous.normalizedDistance) / span;
      return previous.lapTimeMs + ratio * (current.lapTimeMs - previous.lapTimeMs);
    }
  }
  return ordered[ordered.length - 1].lapTimeMs;
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

initControls();
setInterval(() => refresh().catch(console.error), 500);
refresh().catch(console.error);
"""

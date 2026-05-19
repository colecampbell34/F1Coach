const stateUrl = "/state";
const mapCanvas = document.getElementById("trackMap");
const traceCanvas = document.getElementById("trace");
const pauseButton = document.getElementById("pauseButton");
const newSessionButton = document.getElementById("newSessionButton");
const goalButtons = Array.from(document.querySelectorAll("[data-goal]"));
const appError = document.getElementById("appError");
let selectedLapNum = null;
let selectedInsightIndex = null;

function initControls() {
  goalButtons.forEach(button => {
    button.addEventListener("click", () => withUiError(async () => {
      await sendControl("set-goal", { goal: button.dataset.goal });
      await refresh();
    }));
  });
  pauseButton.addEventListener("click", () => withUiError(async () => {
    const paused = pauseButton.dataset.paused === "true";
    await sendControl(paused ? "resume" : "pause");
    await refresh();
  }));
  newSessionButton.addEventListener("click", () => withUiError(async () => {
    const confirmed = window.confirm("Start a new session? This clears session laps, map data, and the theoretical best.");
    if (!confirmed) return;
    selectedLapNum = null;
    selectedInsightIndex = null;
    await sendControl("new-session");
    await refresh();
  }));
}

async function sendControl(action, payload = {}) {
  const response = await fetch("/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(cleanErrorText(detail) || `Control action failed: ${action}`);
  }
}

async function refresh() {
  const response = await fetch(stateUrl, { cache: "no-store" });
  if (!response.ok) throw new Error(`State request failed: ${response.status}`);
  const state = await response.json();
  const selectedLap = selectedLapFromState(state);
  const selectedInsights = selectedLapInsights(state, selectedLap);
  if (selectedInsightIndex !== null && selectedInsightIndex >= selectedInsights.length) selectedInsightIndex = null;
  clearAppError();
  renderSession(state);
  renderReviewHero(state, selectedLap, selectedInsights);
  renderPacketMix(state);
  renderDiagnostics(state.diagnostics || []);
  renderInsights(selectedInsights);
  renderSetupInsights(state.setupInsights || []);
  renderLaps(state.completedLaps || []);
  renderFeed(state.notices || []);
  renderMap(state, selectedLap, selectedInsights);
  renderTrace(state, selectedLap, selectedInsights);
}

function renderSession(state) {
  const packets = packetTotal(state);
  const session = state.session || {};
  const trackLabel = session.trackName || (
    session.trackId !== null && session.trackId !== undefined ? `Track ${session.trackId}` : "Unknown track"
  );
  const goalLabel = state.drivingGoal === "race" ? "Race review" : "Quali review";
  document.getElementById("sessionLine").textContent = session.trackLengthM
    ? `${trackLabel} · ${session.trackLengthM} m · ${goalLabel}`
    : `${goalLabel} · waiting for F1 24`;
  document.getElementById("packetStatus").textContent = `${packets.toLocaleString()} packets captured`;
  document.getElementById("senderStatus").textContent = state.lastSender
    ? `Source ${state.lastSender[0]}:${state.lastSender[1]}`
    : "No sender";
  document.getElementById("referenceStatus").textContent = state.reference
    ? `${state.reference.name} · ${state.reference.lapTime}`
    : "No reference built";

  const lamp = document.getElementById("connectionLamp");
  lamp.classList.toggle("connected", packets > 0 && !state.paused);
  lamp.classList.toggle("paused", Boolean(state.paused));
  document.getElementById("connectionLabel").textContent = state.paused ? "Capture paused" : packets > 0 ? "Capturing telemetry" : "No packets";

  pauseButton.dataset.paused = state.paused ? "true" : "false";
  pauseButton.textContent = state.paused ? "Resume Capture" : "Pause Capture";
  pauseButton.classList.toggle("active", Boolean(state.paused));
  goalButtons.forEach(button => button.classList.toggle("active", button.dataset.goal === state.drivingGoal));
}

function renderReviewHero(state, selectedLap, insights) {
  const reference = state.reference;
  const referenceLabel = reference ? `${reference.name} ${reference.lapTime}` : "Set target";
  const bestDelta = reference && state.bestLap ? fmtMs(state.bestLap.lapTimeMs - reference.lapTimeMs) : null;
  const title = document.getElementById("selectedLapTitle");
  const meta = document.getElementById("selectedLapMeta");
  if (!selectedLap) {
    title.textContent = "No lap review yet";
    meta.textContent = "Finish a lap to unlock the map, trace, and focus cards.";
    setText("reviewLapTime", "--");
    setText("reviewDelta", "--");
    setText("reviewReference", bestDelta ? `${referenceLabel} (${bestDelta} vs PB)` : referenceLabel);
    setText("reviewStatus", "--");
    renderReviewProfile(null);
    return;
  }
  const insight = (insights || [])[0];
  const overviewNotes = selectedLap.overviewNotes || [];
  title.textContent = insight
    ? `Lap ${selectedLap.lapNum}: ${titleCase(insight.category)} at ${insight.area}`
    : `Lap ${selectedLap.lapNum} review`;
  meta.textContent = insight
    ? `${insight.detail} ${insight.recommendation}`
    : overviewNotes.length
      ? overviewNotes.join(" ")
      : "No major time-loss segment was detected against the current reference.";
  setText("reviewLapTime", selectedLap.lapTime);
  setText("reviewDelta", fmtMs(selectedLap.deltaToReferenceMs));
  setText("reviewReference", bestDelta ? `${referenceLabel} (${bestDelta} vs PB)` : referenceLabel);
  setText("reviewStatus", lapStatus(selectedLap));
  renderReviewProfile(selectedLap);
}

function renderReviewProfile(selectedLap) {
  const target = document.getElementById("reviewProfile");
  target.innerHTML = "";
  const overview = selectedLap && selectedLap.overview || {};
  if (!overview.sampleCount) return;
  const metrics = [
    ["Avg speed", fmtKmh(overview.avgSpeedKmh)],
    ["Top speed", fmtKmh(overview.topSpeedKmh)],
    ["Full throttle", fmtPct(overview.fullThrottlePct)],
    ["Braking", fmtPct(overview.brakingPct)],
    ["Overlap", fmtPct(overview.brakeThrottleOverlapPct)],
    ["Control", overview.controlScore === null || overview.controlScore === undefined ? "--" : `${overview.controlScore}/100`],
  ];
  for (const [label, value] of metrics) {
    const pill = document.createElement("div");
    pill.className = "profilePill";
    appendText(pill, "span", label);
    appendText(pill, "strong", value);
    target.appendChild(pill);
  }
}

function renderPacketMix(state) {
  const target = document.getElementById("packetMix");
  target.innerHTML = "";
  const entries = Object.entries(state.packets || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
  const max = Math.max(...entries.map(entry => entry[1]), 1);
  if (!entries.length) {
    target.appendChild(empty("Packet counters appear here once telemetry starts."));
    return;
  }
  for (const [name, count] of entries) {
    const row = document.createElement("div");
    row.className = "packetRow";
    const label = document.createElement("span");
    label.textContent = readablePacketName(name);
    const bar = document.createElement("div");
    bar.className = "packetBar";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(5, Math.round(count / max * 100))}%`;
    bar.appendChild(fill);
    const value = document.createElement("span");
    value.textContent = compactNumber(count);
    row.append(label, bar, value);
    target.appendChild(row);
  }
}

function renderDiagnostics(items) {
  const target = document.getElementById("diagnostics");
  target.innerHTML = "";
  if (!items.length) {
    target.appendChild(empty("Runtime checks are clear."));
    return;
  }
  for (const item of items) {
    const div = document.createElement("div");
    div.className = `diagnostic ${item.level || "info"}`;
    appendText(div, "strong", item.title || "Status");
    appendText(div, "span", item.detail || "");
    appendText(div, "small", item.action || "");
    target.appendChild(div);
  }
}

function renderInsights(insights) {
  const target = document.getElementById("insights");
  target.innerHTML = "";
  if (!insights.length) {
    target.appendChild(empty("No priority fixes yet. Clean completed laps will generate corner-level targets."));
    return;
  }
  insights.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `insight ${item.severity}${selectedInsightIndex === index ? " selected" : ""}`;
    button.addEventListener("click", () => {
      selectedInsightIndex = selectedInsightIndex === index ? null : index;
      refreshSafe();
    });
    appendText(button, "strong", `${titleCase(item.category)} · ${fmtMs(item.time_delta_ms)}`);
    appendText(button, "span", item.area || `${Math.round(item.start_pct)}-${Math.round(item.end_pct)}% lap`, "area");
    appendText(button, "span", item.recommendation || specificTip(item), "tip");
    appendText(button, "span", item.detail || "", "evidence");
    appendText(button, "span", item.evidence || "", "evidence");
    if (item.reference_source) appendText(button, "span", item.reference_source, "evidence");
    if (item.setup_hint) appendText(button, "span", shortSentence(item.setup_hint), "setup");
    target.appendChild(button);
  });
}

function renderSetupInsights(items) {
  const target = document.getElementById("setupInsights");
  target.innerHTML = "";
  if (!items.length) {
    target.appendChild(empty("Repeated ERS, traction, or balance patterns will appear after comparable laps."));
    return;
  }
  for (const item of items) {
    const div = document.createElement("div");
    div.textContent = item;
    target.appendChild(div);
  }
}

function renderLaps(laps) {
  const target = document.getElementById("lapList");
  target.innerHTML = "";
  const selectable = laps.filter(lap => lap.samples && lap.samples.length);
  if (selectedLapNum === null && selectable.length) selectedLapNum = selectable[selectable.length - 1].lapNum;
  if (selectedLapNum !== null && selectable.length && !selectable.some(lap => lap.lapNum === selectedLapNum)) {
    selectedLapNum = selectable[selectable.length - 1].lapNum;
  }
  if (!laps.length) {
    target.appendChild(empty("Completed laps will stack here after each crossing."));
    return;
  }
  for (const lap of [...laps].reverse()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `lapCard${lap.lapNum === selectedLapNum ? " selected" : ""}`;
    button.addEventListener("click", () => {
      selectedLapNum = lap.lapNum;
      selectedInsightIndex = null;
      refreshSafe();
    });
    const badge = document.createElement("span");
    badge.className = "lapBadge";
    badge.textContent = lap.lapNum;
    const copy = document.createElement("span");
    appendText(copy, "strong", lap.lapTime);
    appendText(copy, "small", lapMeta(lap));
    const ers = document.createElement("span");
    ers.className = "lapErs";
    ers.textContent = fmtErs(lap.ersUsedKj);
    const delta = document.createElement("span");
    delta.className = "lapDelta";
    delta.textContent = fmtMs(lap.deltaToReferenceMs);
    button.append(badge, copy, ers, delta);
    target.appendChild(button);
  }
}

function renderFeed(notices) {
  const feed = document.getElementById("notices");
  feed.innerHTML = "";
  if (!notices.length) {
    feed.appendChild(empty("Lap summaries and coach notes will appear here."));
    return;
  }
  for (const notice of [...notices].reverse()) {
    const div = document.createElement("div");
    div.textContent = notice;
    feed.appendChild(div);
  }
}

function renderMap(state, selectedLap, insights) {
  const ctx = mapCanvas.getContext("2d");
  const w = mapCanvas.width;
  const h = mapCanvas.height;
  clearCanvas(ctx, w, h);
  const selectedSamples = orderedMapSamples(selectedLap && selectedLap.samples || []);
  const fallbackSamples = orderedMapSamples(state.trackMap && state.trackMap.samples || []);
  const ref = selectedSamples.length ? selectedSamples : fallbackSamples;
  const referenceSamples = referenceTraceSamples(state);
  document.getElementById("mapHint").textContent = selectedLap
    ? `Lap ${selectedLap.lapNum} trace vs ${referenceSamples.length ? traceReferenceLabel(state) : "reference pending"}`
    : "Complete a lap with motion packets";
  if (ref.length < 2) {
    centerText(ctx, w, h, "No lap map available yet");
    return;
  }
  const bounds = getBounds(ref);
  drawGroupedPath(ctx, ref, bounds, "#2e2738", 11);
  drawGroupedPath(ctx, ref, bounds, "#111016", 7);
  if (selectedSamples.length >= 2 && referenceSamples.length) {
    drawDeltaPath(ctx, selectedSamples, referenceSamples, bounds);
  } else if (selectedSamples.length >= 2) {
    drawGroupedPath(ctx, selectedSamples, bounds, "#45d6ff", 5);
  } else {
    drawGroupedPath(ctx, ref, bounds, "#ffd166", 5);
  }
  const selectedInsight = (insights || [])[selectedInsightIndex];
  if (selectedInsight) {
    const segment = ref.filter(s => s.normalizedDistance >= selectedInsight.start_pct / 100 && s.normalizedDistance <= selectedInsight.end_pct / 100);
    drawGroupedPath(ctx, segment, bounds, "#fff7ee", 13);
    drawGroupedPath(ctx, segment, bounds, "#45d6ff", 8);
  }
}

function renderTrace(state, selectedLap, insights) {
  const ctx = traceCanvas.getContext("2d");
  const w = traceCanvas.width;
  const h = traceCanvas.height;
  clearCanvas(ctx, w, h);
  const samples = selectedLap && selectedLap.samples ? orderedSamples(selectedLap.samples) : [];
  const referenceSamples = referenceTraceSamples(state);
  document.getElementById("traceHint").textContent = selectedLap
    ? `Lap ${selectedLap.lapNum} speed, throttle, and brake by distance`
    : "Select a completed lap";
  drawTraceGrid(ctx, w, h);
  const selectedInsight = (insights || [])[selectedInsightIndex];
  if (selectedInsight) shadeTraceSegment(ctx, w, h, selectedInsight.start_pct / 100, selectedInsight.end_pct / 100);
  if (!samples.length) {
    centerText(ctx, w, h, "No input trace available yet");
    return;
  }
  if (referenceSamples.length) {
    drawSpeedTrace(ctx, referenceSamples, w, h, "rgba(255,255,255,.25)", 1.5);
    drawTrace(ctx, referenceSamples, w, h, "throttle", 1, "rgba(63,240,154,.24)", 1);
    drawTrace(ctx, referenceSamples, w, h, "brake", 1, "rgba(255,54,94,.24)", 1);
  }
  drawSpeedTrace(ctx, samples, w, h, "#ffd166", 2.5);
  drawTrace(ctx, samples, w, h, "throttle", 1, "#3ff09a", 2);
  drawTrace(ctx, samples, w, h, "brake", 1, "#ff365e", 2);
}

function drawTraceGrid(ctx, w, h) {
  ctx.strokeStyle = "rgba(255,255,255,.10)";
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui";
  ctx.fillStyle = "#81788e";
  for (const pct of [0.25, 0.5, 0.75]) {
    const y = h - 24 - pct * (h - 52);
    ctx.beginPath();
    ctx.moveTo(42, y);
    ctx.lineTo(w - 24, y);
    ctx.stroke();
  }
  for (const pct of [0, .25, .5, .75, 1]) {
    const x = 42 + pct * (w - 70);
    ctx.fillText(`${Math.round(pct * 100)}%`, x - 8, h - 8);
  }
}

function drawSpeedTrace(ctx, samples, w, h, color, width) {
  const maxSpeed = Math.max(...samples.map(sample => sample.speedKmh || 0), 1);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  samples.forEach((s, i) => {
    const x = 42 + s.normalizedDistance * (w - 70);
    const y = h - 24 - Math.max(0, Math.min(1, (s.speedKmh || 0) / maxSpeed)) * (h - 52);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawTrace(ctx, samples, w, h, key, max, color, width) {
  if (!samples.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  samples.forEach((s, i) => {
    const raw = s[key];
    const x = 42 + s.normalizedDistance * (w - 70);
    const y = h - 24 - Math.max(0, Math.min(1, (raw || 0) / max)) * (h - 52);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function shadeTraceSegment(ctx, w, h, start, end) {
  const x = 42 + start * (w - 70);
  const width = Math.max(2, (end - start) * (w - 70));
  ctx.fillStyle = "rgba(69,214,255,.14)";
  ctx.fillRect(x, 8, width, h - 34);
}

function drawDeltaPath(ctx, samples, referenceSamples, bounds) {
  ctx.lineWidth = 7;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const group of sampleGroups(samples)) {
    for (let i = 1; i < group.length; i += 1) {
      const current = group[i];
      const previous = group[i - 1];
      const midpoint = (previous.normalizedDistance + current.normalizedDistance) / 2;
      const deltaMs = localSegmentDelta(samples, referenceSamples, midpoint);
      ctx.strokeStyle = deltaMs === null ? "#ffd166" : deltaColor(deltaMs);
      const [x1, y1] = project(previous, bounds);
      const [x2, y2] = project(current, bounds);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
  }
}

function drawGroupedPath(ctx, samples, bounds, color, width) {
  for (const group of sampleGroups(samples)) drawPath(ctx, group, bounds, color, width);
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

function selectedLapFromState(state) {
  const laps = state.completedLaps || [];
  const selectable = laps.filter(lap => lap.samples && lap.samples.length);
  if (selectedLapNum === null && selectable.length) selectedLapNum = selectable[selectable.length - 1].lapNum;
  return selectable.find(lap => lap.lapNum === selectedLapNum) || selectable[selectable.length - 1] || null;
}

function selectedLapInsights(state, selectedLap) {
  if (selectedLap && selectedLap.insights) return selectedLap.insights;
  return state.insights || [];
}

function referenceTraceSamples(state) {
  if (state.reference && state.reference.samples && state.reference.samples.length) {
    return orderedSamples(state.reference.samples);
  }
  return orderedSamples(state.trackMap && state.trackMap.samples || []);
}

function traceReferenceLabel(state) {
  if (state.reference && state.reference.samples && state.reference.samples.length) return state.reference.name || "reference";
  return state.trackMap && state.trackMap.source ? state.trackMap.source : "personal best";
}

function orderedSamples(samples) {
  const byBucket = new Map();
  for (const sample of samples || []) {
    if (sample.normalizedDistance === null || sample.normalizedDistance === undefined) continue;
    if (sample.normalizedDistance < 0 || sample.normalizedDistance > 1) continue;
    byBucket.set(Math.round(sample.normalizedDistance * 1000), sample);
  }
  return Array.from(byBucket.values()).sort((a, b) => a.normalizedDistance - b.normalizedDistance);
}

function orderedMapSamples(samples) {
  return orderedSamples(samples).filter(sample => sample.worldPosition && sample.worldPosition.length === 3);
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

function getBounds(samples) {
  const xs = samples.map(s => s.worldPosition[0]);
  const ys = samples.map(s => s.worldPosition[2]);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
}

function project(sample, bounds) {
  const pad = 34;
  const spanX = Math.max(1, bounds.maxX - bounds.minX);
  const spanY = Math.max(1, bounds.maxY - bounds.minY);
  const sx = (mapCanvas.width - pad * 2) / spanX;
  const sy = (mapCanvas.height - pad * 2) / spanY;
  const scale = Math.min(sx, sy);
  const offsetX = (mapCanvas.width - spanX * scale) / 2;
  const offsetY = (mapCanvas.height - spanY * scale) / 2;
  return [
    offsetX + (sample.worldPosition[0] - bounds.minX) * scale,
    offsetY + (sample.worldPosition[2] - bounds.minY) * scale,
  ];
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
      return previous.lapTimeMs + ((distance - previous.normalizedDistance) / span) * (current.lapTimeMs - previous.lapTimeMs);
    }
  }
  return ordered[ordered.length - 1].lapTimeMs;
}

function localSegmentDelta(samples, referenceSamples, midpoint) {
  const segmentCount = 30;
  const index = Math.max(0, Math.min(segmentCount - 1, Math.floor(midpoint * segmentCount)));
  const start = index / segmentCount;
  const end = (index + 1) / segmentCount;
  const lapStart = timeAtDistance(samples, start);
  const lapEnd = timeAtDistance(samples, end);
  if (lapStart === null || lapEnd === null) return null;
  const targetStart = timeAtDistance(referenceSamples, start);
  const targetEnd = timeAtDistance(referenceSamples, end);
  if (targetStart === null || targetEnd === null) return null;
  return (lapEnd - lapStart) - (targetEnd - targetStart);
}

function deltaColor(deltaMs) {
  if (deltaMs < -10) return "#3ff09a";
  if (deltaMs > 10) return "#ff365e";
  return "#ffd166";
}

function clearCanvas(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0d0b12";
  ctx.fillRect(0, 0, w, h);
}

function centerText(ctx, w, h, text) {
  ctx.fillStyle = "#b4aebd";
  ctx.font = "16px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(text, w / 2, h / 2);
}

function appendText(parent, tag, value, className = "") {
  if (!value) return;
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  parent.appendChild(node);
}

function empty(text) {
  const div = document.createElement("div");
  div.className = "emptyState";
  div.textContent = text;
  return div;
}

function setText(id, value) {
  document.getElementById(id).textContent = value === null || value === undefined ? "--" : value;
}

function packetTotal(state) {
  return Object.values(state.packets || {}).reduce((a, b) => a + b, 0);
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return "--";
  const sign = ms > 0 ? "+" : ms < 0 ? "-" : "";
  return sign + (Math.abs(ms) / 1000).toFixed(2) + "s";
}

function fmtErs(kj) {
  if (kj === null || kj === undefined) return "ERS --";
  if (kj >= 1000) return `ERS ${(kj / 1000).toFixed(1)} MJ`;
  return `ERS ${Math.round(kj)} kJ`;
}

function fmtFuel(lap) {
  if (lap.fuelKg !== null && lap.fuelKg !== undefined) return `Fuel ${Number(lap.fuelKg).toFixed(1)}kg`;
  if (lap.fuelRemainingLaps !== null && lap.fuelRemainingLaps !== undefined) {
    return `Fuel ${Number(lap.fuelRemainingLaps).toFixed(1)} laps`;
  }
  return "";
}

function fmtKmh(value) {
  if (value === null || value === undefined) return "--";
  return `${Math.round(value)} km/h`;
}

function fmtPct(value) {
  if (value === null || value === undefined) return "--";
  return `${Number(value).toFixed(1)}%`;
}

function titleCase(text) {
  return String(text || "")
    .replace(/-/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

function readablePacketName(text) {
  return titleCase(String(text || "").replace(/_/g, " "));
}

function compactNumber(value) {
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function shortSentence(text) {
  const sentence = String(text || "").split(/(?<=[.!?])\s+/)[0] || String(text || "");
  return sentence.length > 150 ? sentence.slice(0, 147).trimEnd() + "..." : sentence;
}

function lapStatus(lap) {
  if (!lap) return "--";
  if (lap.invalid) return "Invalid";
  if (lap.gameInvalid) return "Practice flag";
  return "Clean";
}

function lapMeta(lap) {
  const parts = [lapStatus(lap)];
  if (lap.tyreCompound) parts.push(lap.tyreCompound);
  const fuel = fmtFuel(lap);
  if (fuel) parts.push(fuel);
  return parts.join(" · ");
}

function specificTip(item) {
  const category = item.category || "pace";
  const speedLoss = Math.round(Math.abs(item.speed_delta_kmh || 0));
  if (category === "braking") return `Release brake earlier; speed is ${speedLoss} km/h down.`;
  if (category === "throttle") return "Rotate the car sooner so throttle can start earlier.";
  if (category === "minimum-speed") return `Protect apex speed; minimum speed is ${speedLoss} km/h down.`;
  if (category === "steering") return "Reduce steering scrub before asking for speed.";
  if (category === "traction") return "Unwind steering before full throttle.";
  if (category === "ERS deployment") return "Deploy more once the car is straight and traction is stable.";
  if (category === "ERS waste") return "Move ERS use out of braking or partial-throttle phases.";
  return "Review this section against the reference trace.";
}

function showAppError(message) {
  appError.textContent = message;
  appError.classList.add("visible");
}

function clearAppError() {
  appError.textContent = "";
  appError.classList.remove("visible");
}

async function withUiError(work) {
  try {
    clearAppError();
    await work();
  } catch (error) {
    showAppError(error && error.message ? error.message : String(error));
  }
}

function cleanErrorText(text) {
  return String(text || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function refreshSafe() {
  refresh().catch(error => {
    showAppError(error && error.message ? error.message : String(error));
  });
}

initControls();
setInterval(refreshSafe, 1500);
refreshSafe();

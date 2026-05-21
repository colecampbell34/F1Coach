const dashboardMode = window.location.pathname.includes("race-overview") ? "race" : "lap";
const stateUrl = `/state?mode=${encodeURIComponent(dashboardMode)}`;
const mapCanvas = document.getElementById("trackMap");
const traceCanvas = document.getElementById("trace");
const pauseButton = document.getElementById("pauseButton");
const newSessionButton = document.getElementById("newSessionButton");
const goalToggle = document.querySelector(".goalToggle");
const navLinks = {
  lap: document.getElementById("lapReviewLink"),
  race: document.getElementById("raceOverviewLink"),
};
const goalButtons = Array.from(document.querySelectorAll("[data-goal]"));
const dashboardViews = {
  lap: document.getElementById("lapView"),
  race: document.getElementById("raceView"),
};
const appError = document.getElementById("appError");
let selectedLapNum = null;
let selectedInsightIndex = null;
let selectedDashboardView = dashboardMode;
let lastState = null;

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
  switchDashboardView(selectedDashboardView);
  withUiError(async () => {
    await sendControl("set-dashboard-mode", { mode: selectedDashboardView });
    await refresh();
  });
}

async function sendControl(action, payload = {}) {
  const response = await fetch("/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, mode: selectedDashboardView, ...payload }),
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
  lastState = state;
  clearAppError();
  renderSession(state);
  renderActiveDashboard(state);
}

function renderActiveDashboard(state) {
  if (selectedDashboardView === "race") {
    renderRaceReview(state.raceReview || {});
    return;
  }
  const selectedLap = selectedLapFromState(state);
  const selectedInsights = selectedLapInsights(state, selectedLap);
  if (selectedInsightIndex !== null && selectedInsightIndex >= selectedInsights.length) selectedInsightIndex = null;
  renderReviewHero(state, selectedLap, selectedInsights);
  renderPacketMix(state);
  renderDiagnostics(state.diagnostics || []);
  renderInsights(selectedInsights);
  renderLaps(state.completedLaps || []);
  renderFeed(state.notices || []);
  renderMap(state, selectedLap, selectedInsights);
  renderTrace(state, selectedLap, selectedInsights);
}

function switchDashboardView(view) {
  selectedDashboardView = dashboardViews[view] ? view : "lap";
  for (const [name, element] of Object.entries(dashboardViews)) {
    if (!element) continue;
    element.hidden = name !== selectedDashboardView;
  }
  if (goalToggle) goalToggle.hidden = selectedDashboardView !== "lap";
  for (const [name, link] of Object.entries(navLinks)) {
    if (!link) continue;
    const active = name === selectedDashboardView;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  pauseButton.textContent = selectedDashboardView === "race" ? "Pause Race Capture" : "Pause Lap Capture";
  newSessionButton.textContent = selectedDashboardView === "race" ? "New Race Review" : "New Lap Session";
}

function renderSession(state) {
  const packets = packetTotal(state);
  const session = state.session || {};
  const trackLabel = session.trackName || (
    session.trackId !== null && session.trackId !== undefined ? `Track ${session.trackId}` : "Unknown track"
  );
  const goalLabel = selectedDashboardView === "race"
    ? "Full race overview"
    : state.drivingGoal === "race" ? "Race-pace lap review" : "Qualifying lap review";
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
  pauseButton.textContent = selectedDashboardView === "race"
    ? state.paused ? "Resume Race Capture" : "Pause Race Capture"
    : state.paused ? "Resume Lap Capture" : "Pause Lap Capture";
  pauseButton.classList.toggle("active", Boolean(state.paused));
  newSessionButton.textContent = selectedDashboardView === "race" ? "New Race Review" : "New Lap Session";
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
    renderLapPulse(null);
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
  renderLapPulse(selectedLap);
}

function renderReviewProfile(selectedLap) {
  const target = document.getElementById("reviewProfile");
  if (!target) return;
  target.innerHTML = "";
  const overview = selectedLap && selectedLap.overview || {};
  if (!overview.sampleCount) return;
  const metrics = [
    { label: "Avg speed", value: fmtKmh(overview.avgSpeedKmh), pct: Number(overview.avgSpeedKmh || 0) / 330 * 100, tone: "cyan" },
    { label: "Top speed", value: fmtKmh(overview.topSpeedKmh), pct: Number(overview.topSpeedKmh || 0) / 360 * 100, tone: "yellow" },
    { label: "Full throttle", value: fmtPct(overview.fullThrottlePct), pct: overview.fullThrottlePct, tone: "green" },
    { label: "Braking", value: fmtPct(overview.brakingPct), pct: overview.brakingPct, tone: "red" },
    { label: "Overlap", value: fmtPct(overview.brakeThrottleOverlapPct), pct: overview.brakeThrottleOverlapPct, tone: Number(overview.brakeThrottleOverlapPct || 0) >= 6 ? "red" : "cyan" },
    { label: "Control", value: overview.controlScore === null || overview.controlScore === undefined ? "--" : `${overview.controlScore}/100`, pct: overview.controlScore, tone: Number(overview.controlScore || 0) >= 82 ? "green" : Number(overview.controlScore || 0) >= 65 ? "yellow" : "red" },
    { label: "ERS used", value: fmtErs(selectedLap.ersUsedKj), pct: selectedLap.ersUsedKj === null || selectedLap.ersUsedKj === undefined ? 0 : Number(selectedLap.ersUsedKj) / 4000 * 100, tone: "violet" },
  ];
  for (const metric of metrics) {
    const pill = document.createElement("div");
    pill.className = `profilePill ${metric.tone || ""}`;
    pill.style.setProperty("--fill", `${clamp(Number(metric.pct || 0), 0, 100)}%`);
    appendText(pill, "span", metric.label);
    appendText(pill, "strong", metric.value);
    pill.appendChild(document.createElement("i"));
    target.appendChild(pill);
  }
}

function renderLapPulse(selectedLap) {
  const target = document.getElementById("lapPulseStrip");
  if (!target) return;
  target.innerHTML = "";
  if (!selectedLap) {
    target.appendChild(empty("Lap telemetry pulse appears after a completed lap."));
    return;
  }
  const total = Number(selectedLap.lapTimeMs || 0);
  const sectors = [
    ["S1", selectedLap.sector1Ms],
    ["S2", selectedLap.sector2Ms],
    ["S3", selectedLap.sector3Ms],
  ];
  for (const [label, value] of sectors) {
    const pct = total > 0 ? Number(value || 0) / total * 100 : 0;
    target.appendChild(pulseTile(label, fmtSectorMs(value), pct, "neutral"));
  }
  const overview = selectedLap.overview || {};
  const delta = Number(selectedLap.deltaToReferenceMs);
  target.appendChild(pulseTile("Delta", fmtMs(selectedLap.deltaToReferenceMs), Number.isFinite(delta) ? Math.min(100, Math.abs(delta) / 3000 * 100) : 0, delta <= 0 ? "gain" : "loss"));
  target.appendChild(pulseTile("Control", overview.controlScore === null || overview.controlScore === undefined ? "--" : `${overview.controlScore}/100`, overview.controlScore, Number(overview.controlScore || 0) >= 82 ? "gain" : Number(overview.controlScore || 0) >= 65 ? "neutral" : "loss"));
  target.appendChild(pulseTile("Commit", fmtPct(overview.fullThrottlePct), overview.fullThrottlePct, "gain"));
}

function pulseTile(label, value, pct, tone) {
  const tile = document.createElement("div");
  tile.className = `pulseTile ${tone || "neutral"}`;
  tile.style.setProperty("--fill", `${clamp(Number(pct || 0), 0, 100)}%`);
  appendText(tile, "span", label);
  appendText(tile, "strong", value);
  tile.appendChild(document.createElement("i"));
  return tile;
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
  if (!target) return;
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

function renderRaceReview(review) {
  const summary = review.summary || {};
  const ranking = review.powerRanking || {};
  const ring = document.getElementById("racePowerRing");
  if (ring) ring.style.setProperty("--score", `${clamp(Number(ranking.score || 0) * 10, 0, 100)}%`);
  setText("racePowerScore", ranking.score === null || ranking.score === undefined ? "--" : Number(ranking.score).toFixed(1));
  setText("racePowerLabel", ranking.label || "No rating");
  setText("racePowerMeta", [ranking.confidence, ranking.explanation].filter(Boolean).join(" · "));
  setText("raceTotalTime", summary.raceTime || "--");
  setText("raceClassified", summary.totalLaps ? `${summary.scoredLaps || 0}/${summary.totalLaps} scored` : "--");
  setText("raceBestLap", summary.bestLap ? `L${summary.bestLap.lapNum} ${summary.bestLap.lapTime}` : "--");
  setText("raceAverageLap", summary.averageLapTime || "--");
  setText("raceConsistency", summary.consistency || "--");
  setText("raceTrend", summary.trend || "--");

  renderRaceStatusStrip(review);
  renderPowerFactors(review.factors || []);
  renderRaceCharts(review.trends || {});
  renderRaceFunStats(review.funStats || []);
  renderRaceRisks(review.riskRegister || []);
  renderRaceStandouts(review.standoutLaps || []);
  renderRacePlan(review.recommendations || []);
  renderRaceLapRibbon(review.lapTable || [], summary);
  renderRaceLapTable(review.lapTable || []);
}

function renderRaceStatusStrip(review) {
  const target = document.getElementById("raceStatusStrip");
  if (!target) return;
  target.innerHTML = "";
  const summary = review.summary || {};
  const ranking = review.powerRanking || {};
  const expected = summary.expectedLaps || summary.totalLaps || null;
  const completion = expected ? `${summary.totalLaps || 0}/${expected}` : `${summary.totalLaps || 0}`;
  const status = summary.raceComplete ? "Checkered flag" : summary.lapsRemaining === 0 ? "Distance covered" : "In progress";
  const scored = summary.scoredPct === undefined || summary.scoredPct === null ? "--" : `${Number(summary.scoredPct).toFixed(0)}%`;
  target.appendChild(statusChip("Status", status, summary.raceComplete ? "gain" : "neutral"));
  target.appendChild(statusChip("Distance", `${completion} laps`, summary.raceComplete ? "gain" : "cyan"));
  target.appendChild(statusChip("Scored", scored, Number(summary.scoredPct || 0) >= 75 ? "gain" : "neutral"));
  target.appendChild(statusChip("Pace", fmtMs(summary.averageDeltaToBestMs), "yellow"));
  target.appendChild(statusChip("Rating", ranking.score === null || ranking.score === undefined ? "--" : `${Number(ranking.score).toFixed(1)}/10`, "violet"));
}

function statusChip(label, value, tone) {
  const chip = document.createElement("div");
  chip.className = `statusChip ${tone || "neutral"}`;
  appendText(chip, "span", label);
  appendText(chip, "strong", value || "--");
  return chip;
}

function renderRaceCharts(trends) {
  renderRacePaceTrend(trends.pace || []);
  renderRacePositionTrend(trends.position || []);
}

function renderRacePaceTrend(points) {
  const canvas = document.getElementById("racePaceChart");
  if (!canvas) return;
  const { ctx, w, h } = canvasContext(canvas);
  clearCanvas(ctx, w, h);
  const series = (points || []).filter(point => Number.isFinite(Number(point.deltaToBestMs)));
  if (!series.length) {
    centerText(ctx, w, h, "Pace trend appears after representative laps");
    return;
  }
  const plot = chartPlot(w, h);
  drawChartGrid(ctx, w, h, plot);
  const maxDelta = Math.max(0.25, ...series.map(point => Math.max(0, Number(point.deltaToBestMs) / 1000)));
  const lapMin = Math.min(...series.map(point => Number(point.lapNum)));
  const lapMax = Math.max(...series.map(point => Number(point.lapNum)));
  const xFor = point => plot.left + lapProgress(point.lapNum, lapMin, lapMax) * plot.width;
  const yFor = point => plot.top + Math.max(0, Number(point.deltaToBestMs) / 1000) / maxDelta * plot.height;
  drawTrendArea(ctx, series, xFor, yFor, plot, "rgba(255,209,102,.14)");
  drawTrendLine(ctx, series, xFor, yFor, "#ffd166", 2.5);
  for (const point of series) {
    drawTrendDot(ctx, xFor(point), yFor(point), point.rankingEligible ? "#3ff09a" : "#81788e", point.rankingEligible ? 3.5 : 2.5);
  }
  drawChartCaption(ctx, plot, `0 to +${maxDelta.toFixed(1)}s vs race best`, `L${lapMin} to L${lapMax}`);
}

function renderRacePositionTrend(points) {
  const canvas = document.getElementById("racePositionChart");
  if (!canvas) return;
  const { ctx, w, h } = canvasContext(canvas);
  clearCanvas(ctx, w, h);
  const series = (points || []).filter(point => Number.isFinite(Number(point.position)));
  if (!series.length) {
    centerText(ctx, w, h, "Position history appears when F1 24 reports race position");
    return;
  }
  const plot = chartPlot(w, h);
  drawChartGrid(ctx, w, h, plot);
  const bestPos = Math.min(...series.map(point => Number(point.position)));
  const worstPos = Math.max(...series.map(point => Number(point.position)));
  const posSpan = Math.max(1, worstPos - bestPos);
  const lapMin = Math.min(...series.map(point => Number(point.lapNum)));
  const lapMax = Math.max(...series.map(point => Number(point.lapNum)));
  const xFor = point => plot.left + lapProgress(point.lapNum, lapMin, lapMax) * plot.width;
  const yFor = point => plot.top + (Number(point.position) - bestPos) / posSpan * plot.height;
  drawTrendArea(ctx, series, xFor, yFor, plot, "rgba(69,214,255,.12)");
  drawTrendLine(ctx, series, xFor, yFor, "#45d6ff", 2.5);
  for (const point of series) {
    drawTrendDot(ctx, xFor(point), yFor(point), point.rankingEligible ? "#45d6ff" : "#81788e", point.rankingEligible ? 3.5 : 2.5);
  }
  drawChartCaption(ctx, plot, `Best P${bestPos} · worst P${worstPos}`, `L${lapMin} to L${lapMax}`);
}

function drawTrendArea(ctx, series, xFor, yFor, plot, color) {
  if (series.length < 2) return;
  ctx.fillStyle = color;
  ctx.beginPath();
  series.forEach((point, index) => {
    const x = xFor(point);
    const y = yFor(point);
    if (index === 0) ctx.moveTo(x, plot.top + plot.height);
    ctx.lineTo(x, y);
  });
  ctx.lineTo(xFor(series[series.length - 1]), plot.top + plot.height);
  ctx.closePath();
  ctx.fill();
}

function drawTrendLine(ctx, series, xFor, yFor, color, width) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  series.forEach((point, index) => {
    const x = xFor(point);
    const y = yFor(point);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawTrendDot(ctx, x, y, color, radius) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
}

function drawChartGrid(ctx, w, h, plot) {
  ctx.strokeStyle = "rgba(255,255,255,.10)";
  ctx.lineWidth = 1;
  for (const pct of [0, 0.25, 0.5, 0.75, 1]) {
    const y = plot.top + pct * plot.height;
    ctx.beginPath();
    ctx.moveTo(plot.left, y);
    ctx.lineTo(w - plot.right, y);
    ctx.stroke();
  }
  for (const pct of [0, 0.25, 0.5, 0.75, 1]) {
    const x = plot.left + pct * plot.width;
    ctx.beginPath();
    ctx.moveTo(x, plot.top);
    ctx.lineTo(x, h - plot.bottom);
    ctx.stroke();
  }
}

function drawChartCaption(ctx, plot, leftText, rightText) {
  ctx.fillStyle = "#b4aebd";
  ctx.font = "11px system-ui";
  ctx.textAlign = "left";
  ctx.fillText(leftText, plot.left, 14);
  ctx.textAlign = "right";
  ctx.fillText(rightText, plot.left + plot.width, plot.top + plot.height + 22);
  ctx.textAlign = "start";
}

function chartPlot(w, h) {
  const left = 42;
  const right = 24;
  const top = 22;
  const bottom = 32;
  return { left, right, top, bottom, width: w - left - right, height: h - top - bottom };
}

function lapProgress(lapNum, lapMin, lapMax) {
  const span = Math.max(1, Number(lapMax) - Number(lapMin));
  return (Number(lapNum) - Number(lapMin)) / span;
}

function renderPowerFactors(factors) {
  const target = document.getElementById("powerFactors");
  target.innerHTML = "";
  if (!factors.length) {
    target.appendChild(empty("Race ranking factors appear after completed laps."));
    return;
  }
  for (const factor of factors) {
    const card = document.createElement("div");
    card.className = "factorCard";
    card.style.setProperty("--score", `${clamp(Number(factor.score || 0) * 10, 0, 100)}%`);
    const gauge = document.createElement("div");
    gauge.className = "factorGauge";
    gauge.style.setProperty("--score", `${clamp(Number(factor.score || 0) * 10, 0, 100)}%`);
    appendText(gauge, "strong", fmtScore(factor.score));
    appendText(gauge, "span", `${Math.round(Number(factor.weight || 0) * 100)}% wt`);
    const body = document.createElement("div");
    body.className = "factorBody";
    const top = document.createElement("div");
    top.className = "factorTop";
    appendText(top, "strong", factor.name || "Factor");
    appendText(top, "span", `${fmtScore(factor.score)}/10`, "factorScore");
    const bar = document.createElement("div");
    bar.className = "scoreBar";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(0, Math.min(100, Number(factor.score || 0) * 10))}%`;
    bar.appendChild(fill);
    const evidence = document.createElement("div");
    evidence.className = "factorEvidence";
    (factor.evidence || []).forEach(item => appendText(evidence, "span", item));
    body.append(top, bar);
    appendText(body, "p", factor.detail || "");
    if (evidence.childElementCount) body.appendChild(evidence);
    card.append(gauge, body);
    target.appendChild(card);
  }
}

function renderRaceFunStats(items) {
  const target = document.getElementById("raceFunStats");
  if (!target) return;
  target.innerHTML = "";
  if (!items.length) {
    target.appendChild(empty("Position changes, ERS spend, fuel burn, and race signals appear after a race."));
    return;
  }
  for (const item of items) {
    const card = document.createElement("div");
    card.className = `funStat ${item.tone || "neutral"}`;
    appendText(card, "span", item.label || "Stat");
    appendText(card, "strong", item.value || "--");
    appendText(card, "small", item.detail || "");
    target.appendChild(card);
  }
}

function renderRacePhases(phases) {
  const target = document.getElementById("racePhases");
  target.innerHTML = "";
  if (!phases.length) {
    target.appendChild(empty("Opening, middle, and closing phase splits appear after clean laps."));
    return;
  }
  const maxDelta = Math.max(400, ...phases.map(phase => Math.max(0, Number(phase.deltaToRaceBestMs || 0))));
  for (const phase of phases) {
    const card = document.createElement("div");
    const delta = Number(phase.deltaToRaceBestMs || 0);
    card.className = `phaseCard ${delta <= 500 ? "gain" : delta >= 1500 ? "loss" : "neutral"}`;
    card.style.setProperty("--phase", `${clamp(Math.max(0, delta) / maxDelta * 100, 8, 100)}%`);
    const header = document.createElement("div");
    appendText(header, "strong", phase.name || "Phase");
    appendText(header, "span", `Laps ${phase.lapRange || "--"}`);
    const track = document.createElement("div");
    track.className = "phaseTrack";
    track.appendChild(document.createElement("i"));
    const metrics = document.createElement("div");
    metrics.className = "miniMetrics";
    metrics.append(metricPill("Avg", phase.averageLapTime || "--"));
    metrics.append(metricPill("Best", phase.bestLapTime ? `L${phase.bestLapNum} ${phase.bestLapTime}` : "--"));
    metrics.append(metricPill("Vs best", fmtMs(phase.deltaToRaceBestMs)));
    metrics.append(metricPill("Control", phase.averageControlScore === null || phase.averageControlScore === undefined ? "--" : `${phase.averageControlScore}/100`));
    appendText(card, "p", phase.note || "");
    card.prepend(header, track, metrics);
    target.appendChild(card);
  }
}

function renderRaceSectors(sectors) {
  const target = document.getElementById("raceSectors");
  target.innerHTML = "";
  if (!sectors.length) {
    target.appendChild(empty("Sector trends need clean laps with sector timing."));
    return;
  }
  const maxSpread = Math.max(1, ...sectors.map(sector => Number(sector.spreadMs || 0)));
  for (const sector of sectors) {
    const row = document.createElement("div");
    const spread = Number(sector.spreadMs || 0);
    row.className = `sectorRow ${spread <= 500 ? "gain" : spread >= 1800 ? "loss" : "neutral"}`;
    row.style.setProperty("--spread", `${clamp(spread / maxSpread * 100, 5, 100)}%`);
    appendText(row, "strong", sector.sector || "Sector");
    const bar = document.createElement("div");
    bar.className = "sectorBar";
    bar.appendChild(document.createElement("i"));
    row.appendChild(bar);
    appendText(row, "span", `Best L${sector.bestLapNum} ${sector.best || "--"}`);
    appendText(row, "span", `Avg ${sector.average || "--"}`);
    appendText(row, "span", `Spread ${sector.spread || "--"}`);
    appendText(row, "span", `Δ Ref ${fmtMs(sector.deltaToReferenceMs)}`);
    target.appendChild(row);
  }
}

function renderRaceRisks(risks) {
  const target = document.getElementById("raceRisks");
  target.innerHTML = "";
  if (!risks.length) {
    target.appendChild(empty("Race risks appear once the stint has enough telemetry."));
    return;
  }
  for (const risk of risks) {
    const card = document.createElement("div");
    card.className = `riskCard ${risk.severity || "low"}`;
    appendText(card, "strong", risk.title || "Risk");
    appendText(card, "span", risk.detail || "");
    appendText(card, "small", risk.action || "");
    target.appendChild(card);
  }
}

function renderRaceStandouts(items) {
  const target = document.getElementById("raceStandouts");
  target.innerHTML = "";
  if (!items.length) {
    target.appendChild(empty("Best, cleanest, and most representative laps appear here."));
    return;
  }
  for (const item of items) {
    const card = document.createElement("div");
    card.className = `standoutCard ${item.tone || "neutral"}`;
    appendText(card, "strong", `${item.title || "Moment"} · L${item.lapNum}`);
    appendText(card, "span", item.metric || "--", "metric");
    appendText(card, "small", item.detail || "");
    target.appendChild(card);
  }
}

function renderRacePlan(items) {
  const target = document.getElementById("racePlan");
  target.innerHTML = "";
  if (!items.length) {
    target.appendChild(empty("A next-run plan appears after the race review has enough data."));
    return;
  }
  items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "planStep";
    appendText(row, "strong", String(index + 1));
    appendText(row, "span", item);
    target.appendChild(row);
  });
}

function renderRaceLapRibbon(rows, summary) {
  const target = document.getElementById("raceLapRibbon");
  if (!target) return;
  target.innerHTML = "";
  if (!rows.length) {
    target.appendChild(empty("Lap delta ribbon appears after the first completed race lap."));
    return;
  }
  const finiteDeltas = rows
    .map(row => Math.max(0, Number(row.deltaToBestMs || 0)))
    .filter(value => Number.isFinite(value));
  const maxDelta = Math.max(500, ...finiteDeltas);
  rows.forEach(row => {
    const delta = Number(row.deltaToBestMs || 0);
    const control = Number(row.controlScore || 0);
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = `raceLapTile ${row.status === "Invalid" ? "invalid" : row.rankingEligible === false ? "excluded" : delta <= 500 ? "gain" : delta >= 1800 ? "loss" : "neutral"}`;
    tile.style.setProperty("--delta", `${clamp(Math.max(0, delta) / maxDelta * 100, 4, 100)}%`);
    tile.style.setProperty("--control", `${clamp(control, 0, 100)}%`);
    tile.title = `Lap ${row.lapNum}: ${row.lapTime || "--"} · ${row.note || row.status || ""}`;
    appendText(tile, "span", `L${row.lapNum}`);
    appendText(tile, "strong", fmtMs(row.deltaToBestMs));
    appendText(tile, "small", [row.position ? `P${row.position}` : "", row.lapTime || ""].filter(Boolean).join(" · "));
    const bars = document.createElement("i");
    tile.appendChild(bars);
    target.appendChild(tile);
  });
  const expected = summary && summary.expectedLaps;
  if (expected && rows.length < expected) {
    for (let lapNum = rows.length + 1; lapNum <= expected; lapNum += 1) {
      const tile = document.createElement("div");
      tile.className = "raceLapTile pending";
      appendText(tile, "span", `L${lapNum}`);
      appendText(tile, "strong", "--");
      appendText(tile, "small", "pending");
      target.appendChild(tile);
    }
  }
}

function renderRaceLapTable(rows) {
  const target = document.getElementById("raceLapTable");
  target.innerHTML = "";
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 9;
    cell.appendChild(empty("Full race lap data appears after completed laps."));
    row.appendChild(cell);
    target.appendChild(row);
    return;
  }
  for (const lap of rows) {
    const row = document.createElement("tr");
    row.className = lap.status === "Invalid" ? "invalid" : lap.rankingEligible === false ? "excluded" : "";
    appendCell(row, `L${lap.lapNum}`);
    appendCell(row, lap.position ? `P${lap.position}` : "--");
    appendCell(row, lap.lapTime || "--");
    appendCell(row, fmtMs(lap.deltaToBestMs));
    appendCell(row, fmtMs(lap.deltaToReferenceMs));
    appendCell(row, `${lap.sector1 || "--"} / ${lap.sector2 || "--"} / ${lap.sector3 || "--"}`);
    appendCell(row, lap.controlScore === null || lap.controlScore === undefined ? "--" : `${lap.controlScore}/100`);
    appendCell(row, raceTyreFuel(lap));
    appendCell(row, lap.note || lap.status || "--");
    target.appendChild(row);
  }
}

function renderMap(state, selectedLap, insights) {
  const { ctx, w, h } = canvasContext(mapCanvas);
  clearCanvas(ctx, w, h);
  const selectedTraceSamples = orderedTraceSamples(selectedLap && selectedLap.samples || []);
  const selectedSamples = stableMapSamples(selectedLap && selectedLap.samples || []);
  const fallbackSamples = stableMapSamples(state.trackMap && state.trackMap.samples || []);
  const ref = selectedSamples.length ? selectedSamples : fallbackSamples;
  const referenceSamples = referenceTraceSamples(state);
  const selectedInsight = (insights || [])[selectedInsightIndex];
  document.getElementById("mapHint").textContent = selectedLap
    ? `Lap ${selectedLap.lapNum} trace vs ${referenceSamples.length ? traceReferenceLabel(state) : "reference pending"}`
    : "Complete a lap with motion packets";
  if (ref.length < 2 && selectedTraceSamples.length < 2) {
    centerText(ctx, w, h, "No lap map available yet");
    return;
  }

  const groups = usableMapGroups(sampleGroups(ref));
  const worldSamples = groups.flat();
  if (mapPathIsUsable(groups)) {
    const bounds = getBounds(worldSamples);
    drawGroupedPath(ctx, worldSamples, bounds, "#2e2738", 11);
    drawGroupedPath(ctx, worldSamples, bounds, "#111016", 7);
    if (selectedSamples.length >= 2 && referenceSamples.length) {
      drawDeltaPath(ctx, selectedSamples, referenceSamples, bounds);
    } else if (selectedSamples.length >= 2) {
      drawGroupedPath(ctx, selectedSamples, bounds, "#45d6ff", 5);
    } else {
      drawGroupedPath(ctx, worldSamples, bounds, "#ffd166", 5);
    }
    if (selectedInsight) {
      const segment = worldSamples.filter(s => s.normalizedDistance >= selectedInsight.start_pct / 100 && s.normalizedDistance <= selectedInsight.end_pct / 100);
      drawGroupedPath(ctx, segment, bounds, "#fff7ee", 13);
      drawGroupedPath(ctx, segment, bounds, "#45d6ff", 8);
    }
    drawStartMarker(ctx, worldSamples, bounds);
    return;
  }

  document.getElementById("mapHint").textContent += " · schematic fallback";
  drawSchematicCircuit(ctx, w, h, selectedTraceSamples.length ? selectedTraceSamples : referenceSamples, referenceSamples, selectedInsight);
}

function renderTrace(state, selectedLap, insights) {
  const { ctx, w, h } = canvasContext(traceCanvas);
  clearCanvas(ctx, w, h);
  const samples = selectedLap && selectedLap.samples ? orderedTraceSamples(selectedLap.samples) : [];
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
    drawTracePoints(ctx, referenceSamples, w, h, "brake", 1, "rgba(255,54,94,.24)", 1.4, 0.04);
  }
  drawSpeedTrace(ctx, samples, w, h, "#ffd166", 2.5);
  drawTrace(ctx, samples, w, h, "throttle", 1, "#3ff09a", 2);
  drawTrace(ctx, samples, w, h, "brake", 1, "#ff365e", 2);
  drawTracePoints(ctx, samples, w, h, "brake", 1, "#ff365e", 2.2, 0.04);
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

function drawTracePoints(ctx, samples, w, h, key, max, color, radius, threshold = 0) {
  ctx.fillStyle = color;
  for (const s of samples) {
    const raw = Number(s[key] || 0);
    if (raw <= threshold) continue;
    const x = 42 + s.normalizedDistance * (w - 70);
    const y = h - 24 - Math.max(0, Math.min(1, raw / max)) * (h - 52);
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
  }
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

function drawStartMarker(ctx, samples, bounds) {
  const start = samples.reduce((best, sample) => (
    best === null || sample.normalizedDistance < best.normalizedDistance ? sample : best
  ), null);
  if (!start) return;
  const [x, y] = project(start, bounds);
  ctx.fillStyle = "#fff7ee";
  ctx.strokeStyle = "#111016";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
}

function drawSchematicCircuit(ctx, w, h, samples, referenceSamples, selectedInsight) {
  const ordered = orderedTraceSamples(samples);
  if (ordered.length < 2) {
    centerText(ctx, w, h, "No stable map samples yet");
    return;
  }
  drawSchematicPath(ctx, w, h, "#2e2738", 12);
  drawSchematicPath(ctx, w, h, "#111016", 8);
  const segments = 96;
  for (let index = 1; index <= segments; index += 1) {
    const start = (index - 1) / segments;
    const end = index / segments;
    const midpoint = (start + end) / 2;
    const deltaMs = referenceSamples.length ? localSegmentDelta(ordered, referenceSamples, midpoint) : null;
    ctx.strokeStyle = deltaMs === null ? "#45d6ff" : deltaColor(deltaMs);
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    const [x1, y1] = schematicPoint(start, w, h);
    const [x2, y2] = schematicPoint(end, w, h);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }
  if (selectedInsight) {
    ctx.strokeStyle = "#fff7ee";
    ctx.lineWidth = 12;
    ctx.lineCap = "round";
    const start = selectedInsight.start_pct / 100;
    const end = selectedInsight.end_pct / 100;
    for (let pct = start + 0.01; pct <= end + 0.001; pct += 0.01) {
      const [x1, y1] = schematicPoint(Math.max(start, pct - 0.01), w, h);
      const [x2, y2] = schematicPoint(Math.min(end, pct), w, h);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.strokeStyle = "#45d6ff";
    ctx.lineWidth = 7;
    for (let pct = start + 0.01; pct <= end + 0.001; pct += 0.01) {
      const [x1, y1] = schematicPoint(Math.max(start, pct - 0.01), w, h);
      const [x2, y2] = schematicPoint(Math.min(end, pct), w, h);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
  }
  const [sx, sy] = schematicPoint(0, w, h);
  ctx.fillStyle = "#fff7ee";
  ctx.beginPath();
  ctx.arc(sx, sy, 5, 0, Math.PI * 2);
  ctx.fill();
}

function drawSchematicPath(ctx, w, h, color, width) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  for (let index = 0; index <= 120; index += 1) {
    const [x, y] = schematicPoint(index / 120, w, h);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function schematicPoint(distance, w, h) {
  const pad = 52;
  const cx = w / 2;
  const cy = h / 2;
  const rx = Math.max(80, (w - pad * 2) / 2);
  const ry = Math.max(80, (h - pad * 2) / 2);
  const angle = -Math.PI / 2 + distance * Math.PI * 2;
  const wobble = 1 + Math.sin(angle * 3) * 0.08 + Math.cos(angle * 5) * 0.05;
  return [
    cx + Math.cos(angle) * rx * wobble,
    cy + Math.sin(angle) * ry * (1 + Math.cos(angle * 2) * 0.10),
  ];
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
    return orderedTraceSamples(state.reference.samples);
  }
  return orderedTraceSamples(state.trackMap && state.trackMap.samples || []);
}

function traceReferenceLabel(state) {
  if (state.reference && state.reference.samples && state.reference.samples.length) return state.reference.name || "reference";
  return state.trackMap && state.trackMap.source ? state.trackMap.source : "personal best";
}

function orderedSamples(samples) {
  return orderedTraceSamples(samples);
}

function orderedTraceSamples(samples) {
  return (samples || [])
    .filter(sample => (
      sample.normalizedDistance !== null
      && sample.normalizedDistance !== undefined
      && sample.normalizedDistance >= 0
      && sample.normalizedDistance <= 1
    ))
    .sort((a, b) => {
      const distance = a.normalizedDistance - b.normalizedDistance;
      if (distance !== 0) return distance;
      return (a.lapTimeMs || 0) - (b.lapTimeMs || 0);
    });
}

function orderedMapSamples(samples) {
  return orderedSamples(samples).filter(finiteMapSample);
}

function finiteMapSample(sample) {
  return sample.worldPosition
    && sample.worldPosition.length === 3
    && Number.isFinite(Number(sample.worldPosition[0]))
    && Number.isFinite(Number(sample.worldPosition[2]));
}

function stableMapSamples(samples) {
  const ordered = orderedMapSamples(samples);
  if (ordered.length < 8) return ordered;
  const nonZero = ordered.filter(sample => Math.hypot(Number(sample.worldPosition[0]), Number(sample.worldPosition[2])) > 0.001);
  return nonZero.length >= ordered.length * 0.85 ? nonZero : ordered;
}

function sampleGroups(samples) {
  const ordered = stableMapSamples(samples);
  const groups = [];
  let current = [];
  const bounds = ordered.length >= 2 ? getBounds(ordered) : null;
  const trackSpan = bounds ? Math.hypot(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY) : 0;
  const stepDistances = [];
  for (let index = 1; index < ordered.length; index += 1) {
    const previous = ordered[index - 1];
    const sample = ordered[index];
    const distanceGap = sample.normalizedDistance - previous.normalizedDistance;
    const spatialGap = mapPointDistance(previous, sample);
    if (distanceGap >= 0 && distanceGap <= 0.04 && spatialGap > 0) stepDistances.push(spatialGap);
  }
  const medianStep = median(stepDistances);
  const maxSpatialJump = Math.max(trackSpan * 0.16, medianStep * 12, 35);
  for (const sample of ordered) {
    const previous = current[current.length - 1];
    const distanceGap = previous ? sample.normalizedDistance - previous.normalizedDistance : 0;
    const spatialGap = previous ? mapPointDistance(previous, sample) : 0;
    if (previous && (distanceGap > 0.04 || distanceGap < -0.002 || spatialGap > maxSpatialJump)) {
      if (current.length >= 2) groups.push(current);
      current = [];
    }
    current.push(sample);
  }
  if (current.length >= 2) groups.push(current);
  return groups;
}

function mapPathIsUsable(groups) {
  if (!groups.length) return false;
  const largest = groups.reduce((best, group) => group.length > best.length ? group : best, groups[0]);
  if (largest.length < 8) return false;
  const coverage = largest[largest.length - 1].normalizedDistance - largest[0].normalizedDistance;
  return coverage >= 0.35 || groups.reduce((total, group) => total + group.length, 0) >= 24;
}

function usableMapGroups(groups) {
  if (!groups.length) return [];
  const largest = groups.reduce((best, group) => group.length > best.length ? group : best, groups[0]);
  const minLength = Math.max(3, Math.round(largest.length * 0.14));
  return groups.filter(group => group.length >= minLength);
}

function mapPointDistance(a, b) {
  return Math.hypot(
    Number(a.worldPosition[0]) - Number(b.worldPosition[0]),
    Number(a.worldPosition[2]) - Number(b.worldPosition[2]),
  );
}

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
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

function canvasContext(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width || canvas.clientWidth || canvas.width));
  const height = Math.max(1, Math.round(rect.height || canvas.clientHeight || canvas.height));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return { ctx: canvas.getContext("2d"), w: width, h: height };
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

function metricPill(label, value) {
  const pill = document.createElement("div");
  pill.className = "metricPill";
  appendText(pill, "span", label);
  appendText(pill, "strong", value);
  return pill;
}

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "--" : value;
  row.appendChild(cell);
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

function fmtSectorMs(ms) {
  if (ms === null || ms === undefined || Number(ms) <= 0) return "--";
  return (Number(ms) / 1000).toFixed(3) + "s";
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

function fmtScore(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(1);
}

function clamp(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

function raceTyreFuel(lap) {
  const parts = [];
  if (lap.tyreCompound) parts.push(lap.tyreCompound);
  if (lap.fuelKg !== null && lap.fuelKg !== undefined) parts.push(`${Number(lap.fuelKg).toFixed(1)}kg`);
  else if (lap.fuelRemainingLaps !== null && lap.fuelRemainingLaps !== undefined) parts.push(`${Number(lap.fuelRemainingLaps).toFixed(1)} laps`);
  if (lap.ersUsedKj !== null && lap.ersUsedKj !== undefined) parts.push(fmtErs(lap.ersUsedKj));
  return parts.join(" · ") || lap.status || "--";
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

// etoroNightwatch web dashboard — DESIGN.md §9 (web app), §4.5 (sleeve
// accounting), §1.4 (the managed set). No framework, no build step: this
// file is served as-is by Cloudflare Pages. All data comes from same-origin
// /api/* endpoints backed by functions/api/*.js, which hold the Worker read
// token server-side (see cloud/pages/README.md) — this file never sees or
// sends that token.
import { classifyStaleness, formatAge } from "./staleness.js";

// Fallback only: the snapshot's own per-position `is_managed` flag (set by
// the phone, per DESIGN.md §3) is authoritative when present. This mirrors
// the managed-set allowlist from DESIGN.md §1.4 purely for display when an
// older/minimal snapshot shape omits the flag (e.g. the current live stub
// `{"stub":true,"equity_usd":0}`).
const MANAGED_SYMBOLS = new Set(["UPRO", "SCHG"]);

const els = {
  envBadge: document.getElementById("env-badge"),
  asofDot: document.getElementById("asof-dot"),
  asofText: document.getElementById("asof-text"),
  refreshBtn: document.getElementById("refresh-btn"),
  refreshStatus: document.getElementById("refresh-status"),
  emptyState: document.getElementById("empty-state"),
  errorState: document.getElementById("error-state"),
  errorText: document.getElementById("error-text"),
  data: document.getElementById("data"),
  acctEquity: document.getElementById("acct-equity"),
  acctCash: document.getElementById("acct-cash"),
  acctAvail: document.getElementById("acct-avail"),
  sleeveAlloc: document.getElementById("sleeve-alloc"),
  sleeveCash: document.getElementById("sleeve-cash"),
  sleeveEquity: document.getElementById("sleeve-equity"),
  sleeveUpnl: document.getElementById("sleeve-upnl"),
  sleeveRpnl: document.getElementById("sleeve-rpnl"),
  sleeveBudget: document.getElementById("sleeve-budget"),
  sleeveSignal: document.getElementById("sleeve-signal"),
  positionsBody: document.getElementById("positions-body"),
};

function fmtUsd(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
}

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

function pnlClass(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
  return Number(v) >= 0 ? "pnl-pos" : "pnl-neg";
}

function isManaged(position) {
  if (typeof position.is_managed === "boolean") return position.is_managed;
  return MANAGED_SYMBOLS.has(String(position.symbol || "").toUpperCase());
}

function renderEnvBadge(snapshot) {
  document.getElementById("source-mode").textContent = snapshot?.stub === true
    ? "API FIXTURE · not live evidence"
    : snapshot ? "LIVE SOURCE · stored snapshot" : "NO SNAPSHOT · source unavailable";
  const raw = snapshot && (snapshot.environment ?? snapshot.mode ?? snapshot.account_environment);
  const env = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  els.envBadge.classList.remove("badge-demo", "badge-real", "badge-unknown");
  if (env === "real" || env === "live") {
    els.envBadge.textContent = "REAL";
    els.envBadge.classList.add("badge-real");
  } else if (env === "demo") {
    els.envBadge.textContent = "DEMO";
    els.envBadge.classList.add("badge-demo");
  } else {
    // Snapshot doesn't state the environment -> never assume. (DESIGN §6.F)
    els.envBadge.textContent = "UNKNOWN";
    els.envBadge.classList.add("badge-unknown");
  }
}

function renderAsOf(updatedAt) {
  const { level, ageMs } = classifyStaleness(updatedAt);
  els.asofDot.className = `dot dot-${level}`;
  if (level === "none") {
    els.asofText.textContent = "No snapshot yet";
  } else {
    const when = new Date(updatedAt).toLocaleString();
    els.asofText.textContent = `Cloud received ${when} (${formatAge(ageMs)}; ${level})`;
  }
}

function renderAccountAndSleeve(snapshot) {
  const account = snapshot.account || {};
  const sleeve = snapshot.sleeve || {};

  els.acctEquity.textContent = fmtUsd(account.total_equity_usd);
  els.acctCash.textContent = fmtUsd(account.total_cash_usd);
  els.acctAvail.textContent = fmtUsd(account.broker_available_cash_usd);

  els.sleeveAlloc.textContent = fmtUsd(sleeve.cash_reserve_usd);
  els.sleeveCash.textContent = fmtUsd(sleeve.sleeve_cash_usd);
  els.sleeveEquity.textContent = fmtUsd(sleeve.sleeve_equity_usd);

  els.sleeveUpnl.textContent = fmtUsd(sleeve.sleeve_unrealized_pnl_usd);
  els.sleeveUpnl.className = pnlClass(sleeve.sleeve_unrealized_pnl_usd);
  els.sleeveRpnl.textContent = fmtUsd(sleeve.sleeve_realized_pnl_usd);
  els.sleeveRpnl.className = pnlClass(sleeve.sleeve_realized_pnl_usd);

  const budgetBits = [];
  if (sleeve.budget_usd !== undefined) budgetBits.push(fmtUsd(sleeve.budget_usd));
  if (sleeve.order_size_usd !== undefined) budgetBits.push(`order ${fmtUsd(sleeve.order_size_usd)}`);
  els.sleeveBudget.textContent = budgetBits.length ? budgetBits.join(" / ") : "—";

  const signalBits = [];
  if (sleeve.cone_z !== undefined) signalBits.push(`z=${fmtNum(sleeve.cone_z)}`);
  if (sleeve.spy_drawdown !== undefined) signalBits.push(`dd=${fmtNum(sleeve.spy_drawdown == null ? null : sleeve.spy_drawdown * 100, 1)}%`);
  if (sleeve.tier !== undefined) signalBits.push(`tier ${sleeve.tier}`);
  els.sleeveSignal.textContent = signalBits.length ? signalBits.join(" ") : "—";
}

function renderPositions(snapshot) {
  const positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
  els.positionsBody.innerHTML = "";
  if (positions.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent = "No positions in this snapshot.";
    td.className = "hint";
    tr.appendChild(td);
    els.positionsBody.appendChild(tr);
    return;
  }

  for (const p of positions) {
    const managed = isManaged(p);
    const tr = document.createElement("tr");
    tr.className = managed ? "row-managed" : "";

    const cells = [
      p.symbol ?? "—",
      managed ? "managed" : "other",
      p.units !== undefined ? fmtNum(p.units, 4) : "—",
      p.entry_price_usd !== undefined ? fmtUsd(p.entry_price_usd) : "—",
      p.market_value_usd !== undefined ? fmtUsd(p.market_value_usd) : "—",
      p.unrealized_pnl_usd !== undefined ? fmtUsd(p.unrealized_pnl_usd) : "—",
      p.open_date ?? "—",
    ];
    cells.forEach((value, i) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (i === 5) td.className = pnlClass(p.unrealized_pnl_usd);
      tr.appendChild(td);
    });
    els.positionsBody.appendChild(tr);
  }
}

function execution(armed) {
  return armed === true ? "ARMED" : armed === false ? "DRY" : "Unknown";
}

function reasonText(reason) {
  return typeof reason === "string" ? Array.from(reason).slice(0, 300).join("") : "—";
}

function renderSessions(snapshot) {
  const session = snapshot.last_session || {};
  const decision = snapshot.last_decision || {};
  const set = (id, value) => { document.getElementById(id).textContent = value ?? "—"; };
  document.getElementById("session-empty").hidden = Boolean(snapshot.last_session);
  set("session-date", session.trading_date);
  set("session-environment", snapshot.environment);
  set("session-armed", execution(session.armed));
  set("session-state", session.state);
  set("session-preflight", `${session.preflight ?? "—"} / ${session.failed_gate ?? "—"}`);
  set("session-reason", reasonText(decision.reason));
  set("session-intents", decision.intents);
  set("session-seed", snapshot.seed?.status);
  const source = classifyStaleness(session.snapshot_taken_at);
  set("session-source", source.level === "none" ? "Unknown" :
    `${new Date(session.snapshot_taken_at).toLocaleString()} (${formatAge(source.ageMs)}; ${source.level})`);
  set("session-complete", session.snapshot_complete === true ? "Complete" :
    session.snapshot_complete === false ? "Incomplete — figures may be missing" : "Unknown");
  set("session-omitted", session.omitted_managed_positions);
  const body = document.getElementById("sessions-body");
  body.replaceChildren();
  const rows = Array.isArray(snapshot.recent_sessions) ? snapshot.recent_sessions.slice(0, 30) : [];
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No sessions yet";
    row.appendChild(cell);
    body.appendChild(row);
  }
  for (const session of rows) {
    const row = document.createElement("tr");
    for (const value of [session.trading_date, execution(session.armed), session.state,
      reasonText(session.reason), session.orders]) {
      const cell = document.createElement("td");
      cell.textContent = value ?? "—";
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}

function showState(name) {
  els.emptyState.hidden = name !== "empty";
  els.errorState.hidden = name !== "error";
  els.data.hidden = name !== "data";
}

async function loadSnapshot() {
  try {
    const res = await fetch("/api/snapshot");
    if (!res.ok) {
      let detail = "";
      try {
        detail = JSON.stringify(await res.json());
      } catch {
        // ignore
      }
      showState("error");
      els.errorText.textContent = `Could not load snapshot (HTTP ${res.status}). ${detail}`;
      renderAsOf(null);
      renderEnvBadge(null);
      return;
    }
    const body = await res.json();
    const snapshot = body.snapshot;
    const updatedAt = body.updated_at;

    renderAsOf(updatedAt);
    renderEnvBadge(snapshot);

    if (!snapshot) {
      showState("empty");
      return;
    }

    renderSessions(snapshot);
    renderAccountAndSleeve(snapshot);
    renderPositions(snapshot);
    showState("data");
  } catch (err) {
    showState("error");
    els.errorText.textContent = `Network error loading snapshot: ${err && err.message ? err.message : err}`;
    renderAsOf(null);
    renderEnvBadge(null);
  }
}

const pauseButton = document.getElementById("pause-demo-btn");
const controlStatus = document.getElementById("control-status");
const controlRecords = document.getElementById("control-records");
let control = null;
let sending = false;

function controlButtons() {
  const allowed = action => !sending && control?.environment === "demo" && control.writable === true &&
    Array.isArray(control.capabilities) && control.capabilities.includes(action);
  els.refreshBtn.disabled = !allowed("refresh_demo_broker");
  pauseButton.disabled = !allowed("pause_demo_orders");
}

async function loadControl() {
  try {
    const response = await fetch("/api/demo-control", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok || body.environment !== "demo" || !Array.isArray(body.records)) throw new Error();
    control = body;
    controlStatus.textContent = body.writable ? "Verified demo controls available." : "Demo controls unavailable for writes.";
    controlRecords.replaceChildren();
    for (const record of body.records) {
      const row = document.createElement("li");
      const confirmation = record.confirmation;
      const observed = confirmation?.brokerObservation?.observedAt;
      row.textContent = `${record.action} · ${record.requestId} · ${record.status} · delivery: ${record.delivery}` +
        ` · issued ${new Date(record.issuedAt).toISOString()} · deadline ${new Date(record.deadlineAt).toISOString()}` +
        (confirmation ? ` · device executed ${new Date(confirmation.executedAt).toISOString()}` +
          ` · cloud received ${new Date(confirmation.receivedAt).toISOString()}` : "") +
        (observed !== undefined ? ` · broker observed ${new Date(observed).toISOString()}` : "") +
        (confirmation?.pausePersisted === true ? " · durable pause and final guard confirmed" : "");
      controlRecords.appendChild(row);
    }
  } catch {
    control = null;
    controlStatus.textContent = "Demo control unavailable — status not verified; previous rows may be stale.";
  }
  controlButtons();
}

async function requestDemoControl(action) {
  if (sending || !control?.writable || !control.capabilities?.includes(action)) return;
  if (action === "pause_demo_orders" && !window.confirm("Pause new demo orders? Open and in-flight orders are not canceled. This interface cannot resume.")) return;
  sending = true;
  controlButtons();
  let requestId;
  try {
    requestId = crypto.randomUUID();
    els.refreshStatus.textContent = `Sending ${requestId}…`;
    const response = await fetch("/api/demo-control", { method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ requestId, action, deviceId: control.deviceId, environment: "demo" }) });
    const body = await response.json();
    els.refreshStatus.textContent = response.ok
      ? `${requestId}: ${body.status}; delivery ${body.delivery}. Reload status for the device receipt.`
      : `${requestId}: request rejected (HTTP ${response.status}). Reload status; do not infer device state.`;
  } catch {
    els.refreshStatus.textContent = `${requestId || "Request"}: send unconfirmed. Reload status before doing anything else; no automatic retry.`;
  } finally {
    await loadControl();
    sending = false;
    controlButtons();
  }
}

els.refreshBtn.addEventListener("click", () => requestDemoControl("refresh_demo_broker"));
pauseButton.addEventListener("click", () => requestDemoControl("pause_demo_orders"));
document.getElementById("control-reload-btn").addEventListener("click", loadControl);

loadSnapshot();
loadControl();

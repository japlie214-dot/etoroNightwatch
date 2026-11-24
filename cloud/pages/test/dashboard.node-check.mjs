// Dependency-free DOM smoke checks; run with node --test (Vitest suite remains separate).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { Script } from "node:vm";

const station = readFileSync(new URL("../public/station.html", import.meta.url), "utf8");

test("Telegram landing and synthetic station link both ways before scripts run", () => {
  assert.match(html, /<a[^>]+href="\/station\.html"[^>]*>Open synthetic station<\/a>/);
  assert.match(station, /<a[^>]+href="\/"[^>]*>Back to snapshot monitor<\/a>/);
  assert.match(html, /Latest stored upload, not a streaming feed/);
  assert.match(station, /SYNTHETIC FIXTURE/);
  assert.match(station, /Fictional prices, balances, lots and orders/);
});

test("station is self-contained and disconnected, with valid inline scripts", () => {
  assert.match(station, /connect-src 'none'/);
  assert.doesNotMatch(station, /<script\b[^>]*\bsrc\s*=|<link\b[^>]*\bhref\s*=/i);
  assert.doesNotMatch(station, /\/api\/|\bfetch\s*\(|\b(?:WebSocket|EventSource|XMLHttpRequest)\s*\(/);
  const scripts = [...station.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)]
    .filter(([, attributes]) => !/type="text\/plain"/.test(attributes));
  assert.equal(scripts.length, 6);
  for (const [, , code] of scripts) assert.doesNotThrow(() => new Script(code));
  assert.match(station, /TradingView Lightweight Charts™ v4\.2\.3/);
  assert.match(station, /id="tradingview-license"/);
});

test("public release has only the expected assets, not the snapshot fixture", () => {
  assert.deepEqual(readdirSync(new URL("../public/", import.meta.url)).sort(),
    ["app.js", "index.html", "staleness.js", "station.html", "style.css"]);
});

const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
const stub = JSON.parse(readFileSync(new URL("../stub-snapshot.json", import.meta.url), "utf8"));

class Element {
  children = [];
  hidden = false;
  className = "";
  classList = { add() {}, remove() {} };
  _text = "";
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text; }
  set innerHTML(value) { assert.equal(value, ""); this.children = []; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener() {}
}

async function render(snapshot, tag) {
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
  globalThis.document = {
    getElementById: id => { assert.ok(nodes.has(id), `missing element ${id}`); return nodes.get(id); },
    createElement: () => new Element(),
  };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ snapshot, updated_at: new Date().toISOString() }) });
  await import(`../public/app.js?test=${tag}`);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("error-state").hidden, true);
  return nodes;
}

function rowText(row) { return row.children.map(cell => cell.textContent); }

test("new stub fills session, account, sleeve, positions and history", async () => {
  const nodes = await render(stub, "stub");
  assert.equal(nodes.get("data").hidden, false);
  assert.equal(nodes.get("session-date").textContent, "2026-09-02");
  assert.equal(nodes.get("session-armed").textContent, "DRY");
  assert.equal(nodes.get("session-environment").textContent, "demo");
  assert.equal(nodes.get("session-state").textContent, "finished");
  assert.equal(nodes.get("session-intents").textContent, "0");
  assert.equal(nodes.get("session-seed").textContent, "adopted");
  assert.equal(nodes.get("session-complete").textContent, "Complete");
  assert.equal(nodes.get("acct-equity").textContent, (15234.56).toLocaleString(undefined, { style: "currency", currency: "USD" }));
  assert.equal(nodes.get("sleeve-alloc").textContent, (2000).toLocaleString(undefined, { style: "currency", currency: "USD" }));
  assert.equal(nodes.get("sleeve-rpnl").textContent, "—");
  assert.equal(nodes.get("positions-body").children.length, 3);
  assert.equal(nodes.get("sessions-body").children.length, 2);
  assert.equal(rowText(nodes.get("sessions-body").children[1])[1], "ARMED");
  assert.match(nodes.get("asof-text").textContent, /^Cloud received/);
  assert.doesNotMatch(nodes.get("session-source").textContent, /fresh\)/);
});

test("legacy probe payload shows unknown source and no sessions without errors", async () => {
  const nodes = await render({ probe: "legacy", device: {}, halt: false, recent_events: [], latest_checks: [] }, "legacy");
  assert.equal(nodes.get("session-source").textContent, "Unknown");
  assert.equal(nodes.get("session-complete").textContent, "Unknown");
  assert.equal(nodes.get("session-armed").textContent, "Unknown");
  assert.equal(nodes.get("acct-equity").textContent, "—");
  assert.equal(nodes.get("session-empty").hidden, false);
  assert.deepEqual(rowText(nodes.get("sessions-body").children[0]), ["No sessions yet"]);
});

test("incomplete or missing source cannot inherit fresh receipt, unknown numbers stay unknown", async () => {
  const value = { environment: "demo", last_session: { armed: true, snapshot_complete: false },
    sleeve: { spy_drawdown: null }, recent_sessions: [] };
  const nodes = await render(value, "incomplete");
  assert.match(nodes.get("asof-text").textContent, /fresh/);
  assert.equal(nodes.get("session-source").textContent, "Unknown");
  assert.match(nodes.get("session-complete").textContent, /^Incomplete/);
  assert.equal(nodes.get("session-armed").textContent, "ARMED");
  assert.equal(nodes.get("sleeve-signal").textContent, "dd=—%");
});

test("reasons are text-only and capped at 300 characters, history at 30 rows", async () => {
  const reason = '<img src=x onerror="throw Error()">' + "日😀 ".repeat(200);
  const value = { last_session: {}, last_decision: { reason },
    recent_sessions: Array.from({ length: 31 }, () => ({ reason, orders: null })) };
  const nodes = await render(value, "reason");
  assert.equal(Array.from(nodes.get("session-reason").textContent).length, 300);
  assert.equal(nodes.get("sessions-body").children.length, 30);
  for (const row of nodes.get("sessions-body").children) {
    assert.equal(Array.from(rowText(row)[3]).length, 300);
    assert.equal(rowText(row)[4], "—");
  }
});

test("API fixtures are labeled as fixtures, not live evidence", async () => {
  const nodes = await render({ stub: true, environment: "demo" }, "api-fixture");
  assert.equal(nodes.get("source-mode").textContent, "API FIXTURE · not live evidence");
  assert.equal(nodes.get("env-badge").textContent, "DEMO");
});

test("live-source labeling never promises fresh data and empty has no fixture fallback", async () => {
  const stored = await render({ environment: "demo" }, "source-mode");
  assert.equal(stored.get("source-mode").textContent, "LIVE SOURCE · stored snapshot");
  const empty = await render(null, "empty-source");
  assert.equal(empty.get("source-mode").textContent, "NO SNAPSHOT · source unavailable");
  assert.equal(empty.get("empty-state").hidden, false);
  assert.equal(empty.get("data").hidden, true);
});

const usd = (n) => Number(n).toLocaleString(undefined, { style: "currency", currency: "USD" });
const budgetBase = { evaluated: true, session_matches: true, trading_date: "2026-10-02",
  evaluated_at: new Date().toISOString(), state_budget_year: 2025, target_year: 2026, history_year: 2025,
  sample_count: 3, sample_mean_usd: 20000, sleeve_equity_usd: 9000, core_fraction: 0.4,
  rounding_step_usd: 20, sizing_floor_usd: 10, order_divisor: 20, budget_usd: 8000,
  order_size_usd: 400, cash_target_usd: 2800,
  sampling: { status: "recorded", trading_date: "2026-10-02", reason: null, value_usd: 1234.5 } };

test("budget panel: stub shows an observed rollover with the coverage caveat", async () => {
  const nodes = await render(stub, "budget-stub");
  assert.match(nodes.get("budget-source").textContent, /^Rollover: prior-year observed SCHG mean/);
  assert.match(nodes.get("budget-samples").textContent, /3 observed, mean .*Observed history; coverage not established/);
  assert.equal(nodes.get("budget-value").textContent, usd(1200));
  assert.equal(nodes.get("budget-years").textContent, "2026 / 2025");
  assert.match(nodes.get("budget-status").textContent, /^Evaluated in the latest session/);
  assert.equal(nodes.get("budget-sampling").textContent, `Recorded ${usd(1234.5)} (2026-09-02)`);
});

for (const [path, sourceText, samplesText] of [
  ["bootstrap", /^Bootstrap: first budget/, /available, not used for this budget$/],
  ["retained", /^Retained: kept for the year; any history did not drive it/, /available, not used for this budget$/],
  ["missing_history_fallback", /^Rollover FALLBACK: no prior-year history/, /fallback warning: no history recorded$/],
]) {
  test(`budget panel: ${path} is labeled as the engine stored it, even when history exists`, async () => {
    const nodes = await render({ environment: "demo", last_session: {},
      annual_budget: { ...budgetBase, source_path: path, sample_count: path === "missing_history_fallback" ? 0 : 5 } }, `budget-${path}`);
    assert.match(nodes.get("budget-source").textContent, sourceText);
    assert.match(nodes.get("budget-samples").textContent, samplesText);
    assert.doesNotMatch(nodes.get("budget-samples").textContent, /coverage not established/);
  });
}

test("budget panel: older payloads and missing evaluations say not evaluated, never a value", async () => {
  for (const [snapshot, tag, status] of [
    [{ environment: "demo", last_session: {} }, "no-section", /no budget evidence/],
    [{ environment: "demo", annual_budget: { evaluated: false, sampling: { status: "not_evaluated" } } }, "unevaluated", /no stored budget evaluation/],
  ]) {
    const nodes = await render(snapshot, `budget-${tag}`);
    assert.match(nodes.get("budget-status").textContent, status);
    for (const id of ["budget-source", "budget-years", "budget-samples", "budget-equity", "budget-rule",
      "budget-value", "budget-order", "budget-cash"]) assert.equal(nodes.get(id).textContent, "—");
    assert.equal(nodes.get("budget-time").textContent, "Unknown");
    assert.equal(nodes.get("budget-sampling").textContent, "Not evaluated");
  }
});

test("budget panel: a verdict from an older session is named, not passed off as current", async () => {
  const nodes = await render({ environment: "demo", last_session: {}, annual_budget: {
    ...budgetBase, source_path: "retained", session_matches: false, trading_date: "2026-09-29",
    sampling: { status: "not_evaluated", trading_date: "2026-10-02" } } }, "budget-older");
  assert.match(nodes.get("budget-status").textContent, /from 2026-09-29; the latest session did not evaluate/);
  assert.equal(nodes.get("budget-sampling").textContent, "Not evaluated");
});

test("budget panel: skipped sampling shows its reason and no value; unknown reasons stay generic", async () => {
  const skipped = await render({ environment: "demo", annual_budget: { evaluated: false,
    sampling: { status: "skipped", reason: "snapshot_failed", trading_date: "2026-10-02", value_usd: null } } }, "budget-skip");
  assert.equal(skipped.get("budget-sampling").textContent, "Skipped: snapshot failed (2026-10-02)");
  const odd = await render({ environment: "demo", annual_budget: { evaluated: false,
    sampling: { status: "skipped", reason: "<img src=x onerror=1>", trading_date: "2026-10-02" } } }, "budget-odd");
  assert.equal(odd.get("budget-sampling").textContent, "Skipped: reason unavailable (2026-10-02)");
});

test("budget panel: unknown source path and hostile text render as plain unknowns", async () => {
  const unknown = await render({ environment: "demo", annual_budget: { ...budgetBase, source_path: "magic" } }, "budget-magic");
  assert.match(unknown.get("budget-status").textContent, /no stored budget evaluation/);
  const hostile = await render({ environment: "demo", annual_budget: { ...budgetBase, source_path: "retained",
    trading_date: "<script>x</script>" + "日".repeat(100), session_matches: false } }, "budget-hostile");
  assert.ok(Array.from(hostile.get("budget-status").textContent).length < 140);
});

test("budget panel: the page contains no recompute of the rule", () => {
  const app = readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  const block = app.slice(app.indexOf("function renderBudget"), app.indexOf("function showState"));
  assert.doesNotMatch(block, /Math\.floor|\*\s*b\.|core_fraction\s*\*|\/ ?20/);
});

if (process.env.NIGHTWATCH_TEST_EXPORT) {
  test("render the actual seeded SQLite export", async () => {
    const value = JSON.parse(readFileSync(process.env.NIGHTWATCH_TEST_EXPORT, "utf8"));
    const nodes = await render(value, "seeded");
    assert.equal(nodes.get("session-environment").textContent, "demo");
    assert.equal(nodes.get("acct-equity").textContent, (15000).toLocaleString(undefined, { style: "currency", currency: "USD" }));
    assert.equal(nodes.get("positions-body").children.length, 2);
    assert.equal(nodes.get("sessions-body").children.length, 1);
  });
}

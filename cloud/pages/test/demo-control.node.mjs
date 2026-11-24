import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { onRequest as proxy } from "../functions/api/demo-control.js";
import { onRequest as protect } from "../functions/_middleware.js";

const origin = "https://monitor.example.test";
const request = (method = "GET", headers = {}) => new Request(origin + "/api/demo-control", {
  method, headers: { origin, "sec-fetch-site": "same-origin", "content-type": "application/json", ...headers },
  body: method === "POST" ? JSON.stringify({ requestId: "synthetic" }) : undefined,
});

test("dedicated Pages proxy forwards only fixed path/method/body and never injects snapshot authority", async () => {
  for (const method of ["GET", "POST"]) {
    let seen;
    const result = await proxy({ request: request(method, { authorization: "synthetic-session" }), env: {
      WEB_READ_TOKEN: "synthetic-read-token",
      DEMO_CONTROL_WORKER: { async fetch(req) {
        seen = req;
        return Response.json({ status: "pending" }, { status: 202, headers: { "set-cookie": "not-forwarded" } });
      } },
    } });
    assert.equal(new URL(seen.url).pathname, "/demo-control");
    assert.equal(seen.method, method);
    assert.equal(seen.headers.get("authorization"), "synthetic-session");
    assert.equal(seen.headers.has("x-web-read-token"), false);
    if (method === "POST") assert.deepEqual(await seen.json(), { requestId: "synthetic" });
    assert.equal(result.status, 202);
    assert.equal(result.headers.get("cache-control"), "no-store");
    assert.equal(result.headers.has("set-cookie"), false);
  }
});

test("Pages rejects cross-origin, missing metadata and unsupported methods without invoking service", async () => {
  let calls = 0;
  const env = { DEMO_CONTROL_WORKER: { fetch() { calls++; throw new Error(); } } };
  for (const headers of [{ origin: "https://other.example" }, { origin: "" },
    { "sec-fetch-site": "" }, { "sec-fetch-site": "cross-site" }, { "content-type": "text/plain" }]) {
    assert.equal((await proxy({ request: request("POST", headers), env })).status, 403);
  }
  assert.equal((await proxy({ request: request("DELETE"), env })).status, 405);
  assert.equal(calls, 0);
});

test("missing proxy binding and transport failures are explicit and do not expose diagnostics", async () => {
  assert.equal((await proxy({ request: request(), env: { WEB_READ_TOKEN: "synthetic-reader" } })).status, 503);
  const result = await proxy({ request: request(), env: { DEMO_CONTROL_WORKER: {
    fetch() { throw new Error("private synthetic diagnostic"); },
  } } });
  assert.equal(result.status, 503);
  assert.doesNotMatch(await result.text(), /private/);
});

test("monitor middleware fails closed for all aliases/assets/APIs and rejects forged identity headers", async () => {
  let served = 0;
  const next = async () => { served++; return new Response("protected synthetic monitor"); };
  for (const path of ["/", "/index", "/index.html", "/station", "/station.html", "/app.js", "/api/snapshot", "/api/demo-control"]) {
    const req = new Request(origin + path, { headers: { "x-operator-authorized": "true", "cf-access-authenticated-user-email": "synthetic" } });
    assert.equal((await protect({ request: req, env: {}, next })).status, 503);
    assert.equal((await protect({ request: req, env: { DEMO_CONTROL_AUTH: {
      fetch: async () => new Response(null, { status: 403 }),
    } }, next })).status, 403);
  }
  assert.equal(served, 0);
  for (const authority of [{ operatorReadAuthorized: true }, { operatorWriteAuthorized: true }]) {
    const response = await protect({ request: request(), env: { DEMO_CONTROL_AUTH: {
      fetch: async () => Response.json(authority),
    } }, next });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
  assert.equal(served, 2);
  for (const fetch of [async () => Response.json({ operatorReadAuthorized: "true" }),
    async () => { throw new Error("private diagnostic"); }]) {
    assert.ok([403, 503].includes((await protect({ request: request(), env: { DEMO_CONTROL_AUTH: { fetch } }, next })).status));
  }
  assert.equal(served, 2);
});

const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
class Element {
  children = [];
  listeners = {};
  textContent = "";
  hidden = false;
  disabled = false;
  className = "";
  classList = { add() {}, remove() {} };
  set innerHTML(value) { assert.equal(value, ""); this.children = []; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
const requestId = "12345678-1234-4234-8234-123456789abc";
const issuedAt = 1_000_000;
const pending = { requestId, action: "refresh_demo_broker", deviceId: "synthetic-device", environment: "demo",
  issuedAt, deadlineAt: issuedAt + 120000, confirmation: null, delivery: "sent", status: "pending" };

async function dashboard(tag, { writable = true, unavailable = false, records = [], rejectSend = false } = {}) {
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const sent = [];
  const state = { writable, unavailable, records, rejectSend };
  globalThis.document = { getElementById: id => { assert.ok(nodes.has(id), id); return nodes.get(id); },
    createElement: () => new Element() };
  globalThis.window = { confirm: () => true };
  globalThis.fetch = async (url, init) => {
    if (url === "/api/snapshot") return Response.json({ snapshot: null, updated_at: null });
    assert.equal(url, "/api/demo-control");
    if (init?.method === "POST") {
      sent.push(JSON.parse(init.body));
      if (state.rejectSend) throw new Error("synthetic offline");
      state.records = [{ ...pending, ...sent.at(-1) }];
      return Response.json(state.records[0], { status: 202 });
    }
    return state.unavailable ? Response.json({ error: "unavailable" }, { status: 503 }) : Response.json({
      deviceId: "synthetic-device", environment: "demo", writable: state.writable,
      capabilities: ["refresh_demo_broker", "pause_demo_orders"], records: state.records,
    });
  };
  await import(`../public/app.js?demo-control-test=${tag}`);
  await new Promise(resolve => setImmediate(resolve));
  return { nodes, sent, state };
}

test("monitor controls remain disabled for unavailable backend or read-only operator", async () => {
  for (const options of [{ unavailable: true }, { writable: false }]) {
    const f = await dashboard(JSON.stringify(options), options);
    assert.equal(f.nodes.get("refresh-btn").disabled, true);
    assert.equal(f.nodes.get("pause-demo-btn").disabled, true);
    assert.match(f.nodes.get("control-status").textContent, /unavailable/);
    await f.nodes.get("refresh-btn").listeners.click();
    assert.equal(f.sent.length, 0);
  }
});

test("actual monitor sends constrained UUID demo requests and renders pending, expiry, applied evidence after reopen", async () => {
  const f = await dashboard("send");
  assert.equal(f.nodes.get("refresh-btn").disabled, false);
  await f.nodes.get("refresh-btn").listeners.click();
  assert.equal(f.sent.length, 1);
  assert.match(f.sent[0].requestId, /^[0-9a-f-]{36}$/);
  assert.deepEqual(Object.keys(f.sent[0]).sort(), ["action", "deviceId", "environment", "requestId"]);
  assert.equal(f.sent[0].action, "refresh_demo_broker");
  assert.equal(f.sent[0].environment, "demo");
  assert.match(f.nodes.get("control-records").children[0].textContent, /pending/);
  await f.nodes.get("pause-demo-btn").listeners.click();
  assert.equal(f.sent[1].action, "pause_demo_orders");
  const expired = await dashboard("expired", { records: [{ ...pending, status: "expired_unconfirmed" }] });
  assert.match(expired.nodes.get("control-records").children[0].textContent, /expired_unconfirmed/);
  const applied = await dashboard("applied", { records: [{ ...pending, status: "applied", confirmation: {
    executedAt: issuedAt + 1000, receivedAt: issuedAt + 2000,
    brokerObservation: { observedAt: issuedAt + 500, account: true, positions: true, openOrders: true },
  } }] });
  const text = applied.nodes.get("control-records").children[0].textContent;
  assert.match(text, /applied/);
  assert.match(text, /device executed/);
  assert.match(text, /cloud received/);
  assert.match(text, /broker observed/);
  assert.match(text, new RegExp(requestId));
});

test("pause requires confirmation and ambiguous browser send never retries automatically", async () => {
  const f = await dashboard("ambiguous", { rejectSend: true });
  globalThis.window.confirm = () => false;
  await f.nodes.get("pause-demo-btn").listeners.click();
  assert.equal(f.sent.length, 0);
  await f.nodes.get("refresh-btn").listeners.click();
  assert.equal(f.sent.length, 1);
  assert.match(f.nodes.get("refresh-status").textContent, /send unconfirmed/);
  assert.match(f.nodes.get("refresh-status").textContent, /no automatic retry/);
  assert.equal(f.sent[0].action, "refresh_demo_broker");
});

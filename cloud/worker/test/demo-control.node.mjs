import test from "node:test";
import assert from "node:assert/strict";
import worker, { DemoControlStore } from "../src/index.js";

const origin = "https://monitor.example.test";
const start = 1_000_000;
const actions = ["refresh_demo_broker", "pause_demo_orders"];
const id = "12345678-1234-4234-8234-123456789abc";
const payload = (action = actions[0]) => ({ requestId: id, action, deviceId: "synthetic-device", environment: "demo" });
const copy = value => structuredClone(value);

// Serialized transactions with rollback and structured-clone persistence, not a KV mock.
class Storage {
  values = new Map();
  queue = Promise.resolve();
  fail = false;
  transaction(fn) {
    const run = this.queue.then(async () => {
      if (this.fail) throw new Error("synthetic storage failure");
      const draft = copy(this.values);
      const result = await fn({ get: async key => copy(draft.get(key)),
        put: async (key, value) => { draft.set(key, copy(value)); } });
      this.values = draft;
      return result;
    });
    this.queue = run.catch(() => {});
    return run;
  }
}

function fixture() {
  let now = start;
  const storage = new Storage();
  const sent = [];
  const authority = { operatorWriteAuthorized: true, operatorReadAuthorized: true,
    environment: "demo", deviceId: "synthetic-device", capabilities: [...actions] };
  const proof = { authenticated: true, deviceId: "synthetic-device", environment: "demo",
    issuedAt: start, deadlineAt: start + 120000,
    brokerObservation: { observedAt: start + 500, account: true, positions: true, openOrders: true },
    pausePersisted: true, finalSubmissionGuard: true };
  const env = {
    DEMO_CONTROL_ORIGIN: origin,
    DEMO_CONTROL_AUTH: { async fetch(request) {
      // Synthetic credentials are verified by this injected service, never by input flags.
      const credential = request.headers.get("authorization");
      if (!["synthetic-operator", "synthetic-reader", "synthetic-device"].includes(credential)) return new Response(null, { status: 403 });
      if (request.url.endsWith("/ack")) {
        if (credential !== "synthetic-device") return new Response(null, { status: 403 });
        await request.json(); // Real verifier must authenticate/bind this entire signed body.
        return Response.json({ ...authority, operatorWriteAuthorized: false, operatorReadAuthorized: false, proof });
      }
      return Response.json({ ...authority, operatorWriteAuthorized: credential === "synthetic-operator" && authority.operatorWriteAuthorized });
    } },
    DEMO_CONTROL_DISPATCH: { async fetch(request) { sent.push(await request.json()); return new Response(null, { status: 202 }); } },
  };
  const reload = () => new DemoControlStore({ storage }, env, { now: () => now });
  let store = reload();
  env.DEMO_CONTROL_STORE = { idFromName: name => name, get: () => store };
  const request = (method = "POST", body = payload(), path = "/demo-control", headers = {}) => new Request(origin + path, {
    method, headers: { authorization: "synthetic-operator", origin, "sec-fetch-site": "same-origin",
      "content-type": "application/json", ...headers }, body: method === "GET" ? undefined : JSON.stringify(body),
  });
  const call = async (...args) => worker.fetch(request(...args), env, {});
  const ack = (body = { ...payload(), executedAt: start + 1000 }) => call("POST", body, "/demo-control/ack", { authorization: "synthetic-device" });
  return { env, storage, sent, authority, proof, call, ack, request,
    setNow: value => { now = value; }, reload: () => { store = reload(); } };
}

for (const action of actions) {
  test(`${action}: durable request/status/receipt reload, strict envelope and late reporting`, async () => {
    const f = fixture();
    const result = await f.call("POST", payload(action));
    assert.equal(result.status, 202);
    assert.equal(result.headers.get("cache-control"), "no-store");
    assert.equal((await result.json()).status, "pending");
    assert.deepEqual(f.sent, [{ version: 1, ...payload(action), issuedAt: start, deadlineAt: start + 120000 }]);
    f.reload();
    f.setNow(start + 120000);
    let status = await (await f.call("GET")).json();
    assert.equal(status.records[0].status, "expired_unconfirmed");
    assert.equal(status.records[0].confirmation, null);
    const accepted = await f.ack({ ...payload(action), executedAt: start + 1000 });
    assert.equal(accepted.status, 200);
    const applied = await accepted.json();
    assert.equal(applied.status, "applied");
    assert.equal(applied.confirmation.receivedAt, start + 120000);
    f.reload();
    status = await (await f.call("GET")).json();
    assert.equal(status.records[0].status, "applied");
    assert.equal(f.sent.length, 1);
  });
}

test("concurrent, case-variant and restarted duplicate requests dispatch exactly once", async () => {
  const f = fixture();
  const results = await Promise.all(Array.from({ length: 8 }, (_, i) => f.call("POST", {
    ...payload(), requestId: i % 2 ? id.toUpperCase() : id,
  })));
  assert.equal(results.filter(r => r.status === 202).length, 1);
  assert.equal(results.filter(r => r.status === 409).length, 7);
  f.reload();
  f.setNow(start + 200000);
  assert.equal((await f.call()).status, 409);
  assert.equal(f.sent.length, 1);
});

test("simultaneous duplicate receipts are immutable and atomic", async () => {
  const f = fixture();
  await f.call();
  f.setNow(start + 2000);
  const responses = await Promise.all([f.ack(), f.ack(), f.ack()]);
  assert.deepEqual(responses.map(r => r.status).sort(), [200, 409, 409]);
  f.reload();
  assert.equal((await f.ack()).status, 409);
});

test("reader token, arbitrary identity headers and payload flags never grant writes or device proof", async () => {
  const f = fixture();
  assert.equal((await f.call("POST", payload(), "/demo-control", { authorization: "synthetic-reader" })).status, 403);
  assert.equal((await f.call("POST", { ...payload(), operatorWriteAuthorized: true }, "/demo-control", {
    authorization: "", "x-web-read-token": "synthetic-reader", "x-operator-authorized": "true",
  })).status, 403);
  assert.equal((await f.call("GET", null, "/demo-control", { authorization: "" })).status, 403);
  await f.call();
  f.setNow(start + 2000);
  assert.equal((await f.call("POST", { ...payload(), executedAt: start + 1000, authenticated: true,
    pausePersisted: true, finalSubmissionGuard: true }, "/demo-control/ack")).status, 403);
  assert.equal(f.sent.length, 1);
});

test("cross-origin, absent Origin, wrong Fetch Metadata and non-JSON writes are rejected before dispatch", async () => {
  const f = fixture();
  for (const headers of [{ origin: "https://evil.example" }, { origin: "null" }, { origin: "" },
    { "sec-fetch-site": "cross-site" }, { "sec-fetch-site": "same-site" }, { "content-type": "text/plain" }]) {
    assert.equal((await f.call("POST", payload(), "/demo-control", headers)).status, 403);
  }
  assert.equal(f.sent.length, 0);
});

test("only known UUID/demo/action/capability combinations reach dispatch", async () => {
  const f = fixture();
  for (const patch of [{ requestId: "bad" }, { environment: "real" }, { environment: "unknown" },
    { deviceId: "other" }, { action: "resume" }, { action: "run_session" }, { command: "halt" },
    { issuedAt: start }, { operatorWriteAuthorized: true }]) {
    assert.equal((await f.call("POST", { ...payload(), ...patch })).status, 400);
  }
  f.authority.capabilities = [];
  assert.equal((await f.call()).status, 400);
  assert.equal((await (await f.call("GET")).json()).writable, false);
  assert.equal(f.sent.length, 0);
});

test("malformed and oversized bodies are rejected without dispatch", async () => {
  const f = fixture();
  const malformed = new Request(origin + "/demo-control", { method: "POST",
    headers: f.request().headers, body: "{" });
  assert.equal((await worker.fetch(malformed, f.env, {})).status, 400);
  assert.equal((await f.call("POST", { ...payload(), blob: "x".repeat(9000) })).status, 413);
  assert.equal(f.sent.length, 0);
});

test("missing bindings, verifier failure, missing capability and storage failure fail closed", async () => {
  for (const binding of ["DEMO_CONTROL_STORE", "DEMO_CONTROL_AUTH", "DEMO_CONTROL_DISPATCH", "DEMO_CONTROL_ORIGIN"]) {
    const f = fixture();
    delete f.env[binding];
    assert.ok([403, 503].includes((await f.call()).status));
    assert.equal(f.sent.length, 0);
  }
  for (const auth of [async () => { throw new Error("private diagnostic"); },
    async () => new Response("private diagnostic", { status: 500 }),
    async () => Response.json({ operatorWriteAuthorized: true })]) {
    const f = fixture();
    f.env.DEMO_CONTROL_AUTH.fetch = auth;
    const response = await f.call();
    assert.equal(response.status, 503);
    assert.doesNotMatch(await response.text(), /private diagnostic/);
    assert.equal(f.sent.length, 0);
  }
  const f = fixture();
  f.storage.fail = true;
  assert.equal((await f.call()).status, 503);
  assert.equal(f.sent.length, 0);
});

test("definite send rejection and ambiguous transport failure survive reload without resend or application", async () => {
  for (const ambiguous of [false, true]) {
    const f = fixture();
    f.env.DEMO_CONTROL_DISPATCH.fetch = async () => {
      if (ambiguous) throw new Error("synthetic disconnect after possible delivery");
      return new Response(null, { status: 503 });
    };
    const body = await (await f.call()).json();
    assert.equal(body.delivery, ambiguous ? "unconfirmed" : "send_failed");
    assert.equal(body.status, "pending");
    f.reload();
    assert.equal((await f.call()).status, 409);
    f.setNow(start + 120000);
    assert.equal((await (await f.call("GET")).json()).records[0].status, "expired_unconfirmed");
  }
});

test("crash after reservation leaves unconfirmed tombstone; expiration before dispatch never sends", async () => {
  const f = fixture();
  // First transaction persists reservation; the next clock read simulates a stalled worker.
  let reads = 0;
  const store = new DemoControlStore({ storage: f.storage }, f.env, {
    now: () => ++reads === 1 ? start : start + 120000,
  });
  f.env.DEMO_CONTROL_STORE.get = () => store;
  const body = await (await f.call()).json();
  assert.equal(body.delivery, "unconfirmed");
  assert.equal(body.status, "expired_unconfirmed");
  assert.equal(f.sent.length, 0);
  assert.equal((await f.call()).status, 409);
});

test("failed final persistence preserves the reservation and never redispatches after restart", async () => {
  const f = fixture();
  f.env.DEMO_CONTROL_DISPATCH.fetch = async request => {
    f.sent.push(await request.json());
    f.storage.fail = true;
    return new Response(null, { status: 202 });
  };
  assert.equal((await f.call()).status, 503);
  f.storage.fail = false;
  f.reload();
  const status = await (await f.call("GET")).json();
  assert.equal(status.records[0].delivery, "unconfirmed");
  assert.equal(status.records[0].status, "pending");
  assert.equal((await f.call()).status, 409);
  assert.equal(f.sent.length, 1);
});

test("ack racing dispatch completion is never overwritten by send state", async () => {
  const f = fixture();
  f.env.DEMO_CONTROL_DISPATCH.fetch = async () => {
    f.setNow(start + 2000);
    assert.equal((await f.ack()).status, 200);
    return new Response(null, { status: 202 });
  };
  const body = await (await f.call()).json();
  assert.equal(body.status, "applied");
  assert.equal(body.delivery, "sent");
});

test("unknown, mismatched, stale/future and incomplete device evidence is rejected", async () => {
  const f = fixture();
  f.setNow(start + 2000);
  assert.equal((await f.ack()).status, 404);
  f.setNow(start);
  await f.call();
  f.setNow(start + 2000);
  for (const patch of [{ deviceId: "other" }, { environment: "real" }, { action: actions[1] },
    { executedAt: start - 1 }, { executedAt: start + 120000 }, { executedAt: start + 3000 }]) {
    assert.equal((await f.ack({ ...payload(), executedAt: start + 1000, ...patch })).status, 400);
  }
  for (const patch of [{ authenticated: false }, { deviceId: "other" }, { environment: "real" },
    { issuedAt: undefined }, { issuedAt: start + 1 }, { deadlineAt: start + 120001 },
    { brokerObservation: { ...f.proof.brokerObservation, openOrders: false } },
    { brokerObservation: { ...f.proof.brokerObservation, observedAt: start } }]) {
    const saved = copy(f.proof);
    Object.assign(f.proof, patch);
    assert.equal((await f.ack()).status, 400);
    Object.assign(f.proof, saved);
  }
  assert.equal((await (await f.call("GET")).json()).records[0].status, "pending");
});

test("pause claims in payload cannot replace verified durable/guard evidence", async () => {
  const f = fixture();
  await f.call("POST", payload(actions[1]));
  f.setNow(start + 2000);
  f.proof.pausePersisted = false;
  assert.equal((await f.ack({ ...payload(actions[1]), executedAt: start + 1000,
    pausePersisted: true, finalSubmissionGuard: true })).status, 400);
  f.proof.pausePersisted = true;
  f.proof.finalSubmissionGuard = false;
  assert.equal((await f.ack({ ...payload(actions[1]), executedAt: start + 1000 })).status, 400);
});

test("status is device-scoped and both actions survive independent latest pointers", async () => {
  const f = fixture();
  await f.call();
  const otherId = "22345678-1234-4234-8234-123456789abc";
  await f.call("POST", { ...payload(actions[1]), requestId: otherId });
  assert.equal((await (await f.call("GET")).json()).records.length, 2);
  f.authority.deviceId = "other-device";
  assert.deepEqual((await (await f.call("GET")).json()).records, []);
  assert.equal((await f.call("GET", null, `/demo-control?requestId=${id}`)).status, 403);
  assert.equal((await f.call("GET", null, "/demo-control?requestId=bad")).status, 400);
});

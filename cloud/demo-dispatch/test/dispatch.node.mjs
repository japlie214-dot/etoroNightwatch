import test from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { createDispatcher, dispatchHmacHex, dispatchSigningText, validEnvelope } from "../src/index.js";

const envelope = { version: 1, requestId: "12345678-1234-4234-8234-123456789abc", action: "pause_demo_orders",
  deviceId: "z10-demo", environment: "demo", issuedAt: 1000000, deadlineAt: 1120000 };
const kv = (token) => ({ get: async (k) => (k === "device_token" ? token : null) });
const post = (body) => new Request("https://demo-control.internal/dispatch", { method: "POST", body: JSON.stringify(body) });

test("signing text and HMAC match the phone's canonical bytes (independent node:crypto)", async () => {
  const expectedText = "nightwatch.demo-control.dispatch.v1\nversion=1\nrequestId=12345678-1234-4234-8234-123456789abc\n" +
    "action=pause_demo_orders\ndeviceId=z10-demo\nenvironment=demo\nissuedAt=1000000\ndeadlineAt=1120000\n";
  assert.equal(dispatchSigningText(envelope), expectedText);
  assert.equal(await dispatchHmacHex("synthetic-only", envelope),
    createHmac("sha256", "synthetic-only").update(expectedText).digest("hex"));
});

test("sends one data-only push with the exact compact envelope and its hmac", async () => {
  const sent = [];
  const d = createDispatcher({ now: () => 1001000, push: async (_env, args) => { sent.push(args); return { ok: true }; } });
  const res = await d.fetch(post(envelope), { COMMAND_HMAC_SECRET: "synthetic-only", NIGHTWATCH_SNAPSHOTS: kv("tok") });
  assert.equal(res.status, 200);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].token, "tok");
  assert.equal(sent[0].data.command, "demo_control");
  assert.deepEqual(JSON.parse(sent[0].data.envelope), envelope);
  assert.equal(sent[0].data.hmac, await dispatchHmacHex("synthetic-only", envelope));
});

test("refuses bad, extra-field, expired, non-demo and unavailable requests without pushing", async () => {
  let pushes = 0;
  const d = createDispatcher({ now: () => 1001000, push: async () => { pushes++; } });
  const env = { COMMAND_HMAC_SECRET: "s", NIGHTWATCH_SNAPSHOTS: kv("tok") };
  for (const bad of [{ ...envelope, extra: 1 }, { ...envelope, environment: "real" }, { ...envelope, action: "resume" },
    { ...envelope, deadlineAt: 1120001 }, { ...envelope, requestId: envelope.requestId.toUpperCase() }]) {
    assert.equal(validEnvelope(bad), false);
    assert.equal((await d.fetch(post(bad), env)).status, 400);
  }
  assert.equal((await createDispatcher({ now: () => 1120000, push: async () => { pushes++; } }).fetch(post(envelope), env)).status, 409);
  assert.equal((await d.fetch(post(envelope), { NIGHTWATCH_SNAPSHOTS: kv("tok") })).status, 503);
  assert.equal((await d.fetch(post(envelope), { COMMAND_HMAC_SECRET: "s", NIGHTWATCH_SNAPSHOTS: kv(null) })).status, 409);
  assert.equal((await d.fetch(new Request("https://x/dispatch", { method: "GET" }), env)).status, 404);
  assert.equal(pushes, 0);
});

test("push failure is a non-2xx, never a success", async () => {
  const d = createDispatcher({ now: () => 1001000, push: async () => { throw new Error("boom"); } });
  const res = await d.fetch(post(envelope), { COMMAND_HMAC_SECRET: "s", NIGHTWATCH_SNAPSHOTS: kv("tok") });
  assert.equal(res.status, 502);
});

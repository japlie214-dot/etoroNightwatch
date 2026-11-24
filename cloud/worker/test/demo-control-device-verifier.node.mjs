import test, { before } from "node:test";
import assert from "node:assert/strict";
import { deviceReceiptSigningBytes, verifyDeviceReceipt, createDeviceReceiptVerifier } from "../src/demo-control-device-verifier.js";
import { DemoControlStore } from "../src/demo-control.js";
import { base64UrlEncode, base64UrlDecodeToBytes } from "../src/utils.js";

const issuedAt = 1_000_000;
const requestId = "12345678-1234-4234-8234-123456789abc";
const actions = ["refresh_demo_broker", "pause_demo_orders"];
const algorithm = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
let keys;
let otherKeys;
before(async () => {
  keys = await crypto.subtle.generateKey(algorithm, false, ["sign", "verify"]);
  otherKeys = await crypto.subtle.generateKey(algorithm, false, ["sign", "verify"]);
});
const payload = (action = actions[0]) => ({ version: 1, keyId: "synthetic-pin-v1", requestId,
  action, deviceId: "synthetic-device", environment: "demo", issuedAt, deadlineAt: issuedAt + 120000,
  executedAt: issuedAt + 1000, ...(action === actions[0] ? {
    brokerObservation: { observedAt: issuedAt + 500, account: true, positions: true, openOrders: true },
  } : { pausePersisted: true, finalSubmissionGuard: true }) });
const makePins = () => new Map([["synthetic-pin-v1", { publicKey: keys.publicKey, deviceId: "synthetic-device",
  environment: "demo", capabilities: [...actions], revoked: false }]]);
const signed = async (value = payload(), key = keys.privateKey) => ({ ...value,
  signature: base64UrlEncode(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, deviceReceiptSigningBytes(value))) });
const verify = (receipt, pins = makePins(), now = issuedAt + 2000) => verifyDeviceReceipt(receipt, { pins, now });
const req = (body, { path = "/demo-control/ack", method = "POST", type = "application/json" } = {}) => new Request("https://synthetic.example" + path, {
  method, headers: { "content-type": type }, body: method === "GET" ? undefined : typeof body === "string" ? body : JSON.stringify(body),
});

test("Kotlin canonical-byte vectors are exact fixed-order ASCII UTF-8 with one final LF", () => {
  const prefix = "nightwatch.demo-control.receipt.v1\nalg=RS256\nkeyId=synthetic-pin-v1\n" +
    `requestId=${requestId}\naction=`;
  const suffix = "\ndeviceId=synthetic-device\nenvironment=demo\nissuedAt=1000000\ndeadlineAt=1120000\nexecutedAt=1001000\n";
  assert.equal(new TextDecoder().decode(deviceReceiptSigningBytes(payload())), prefix + actions[0] + suffix +
    "observedAt=1000500\naccount=true\npositions=true\nopenOrders=true\n");
  assert.equal(new TextDecoder().decode(deviceReceiptSigningBytes(payload(actions[1]))), prefix + actions[1] + suffix +
    "pausePersisted=true\nfinalSubmissionGuard=true\n");
  const reordered = Object.fromEntries(Object.entries(payload()).reverse());
  reordered.brokerObservation = Object.fromEntries(Object.entries(reordered.brokerObservation).reverse());
  assert.deepEqual(deviceReceiptSigningBytes(reordered), deviceReceiptSigningBytes(payload()));
});

for (const action of actions) {
  test(`${action}: ephemeral-key signature yields only device proof, never operator authority`, async () => {
    const receipt = await signed(payload(action));
    const authority = await verify(receipt);
    assert.equal(authority.operatorReadAuthorized, false);
    assert.equal(authority.operatorWriteAuthorized, false);
    assert.equal(authority.proof.authenticated, true);
    assert.equal(authority.proof.issuedAt, issuedAt);
    assert.equal(authority.proof.deadlineAt, issuedAt + 120000);
    assert.equal(authority.deviceId, receipt.deviceId);
    assert.deepEqual(authority.capabilities, [action]);
    assert.deepEqual(await verify(JSON.parse(JSON.stringify(receipt))), authority);
  });

  test(`${action}: signature binds every labeled field, evidence value and final newline`, async () => {
    const receipt = await signed(payload(action));
    const bytes = deviceReceiptSigningBytes(payload(action));
    const signature = base64UrlDecodeToBytes(receipt.signature);
    const lines = new TextDecoder().decode(bytes).split("\n");
    for (let i = 0; i < lines.length - 1; i++) {
      const changed = [...lines];
      changed[i] += "x";
      assert.equal(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", keys.publicKey, signature,
        new TextEncoder().encode(changed.join("\n"))), false, lines[i]);
    }
    for (const changed of [bytes.slice(0, -1), new TextEncoder().encode(new TextDecoder().decode(bytes) + "\n")]) {
      assert.equal(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", keys.publicKey, signature, changed), false);
    }
  });
}

test("changed identity/times, swapped evidence and copied signatures never verify", async () => {
  const receipt = await signed();
  const changed = [
    { requestId: "22345678-1234-4234-8234-123456789abc" }, { deviceId: "other-device" },
    { keyId: "other-pin" }, { environment: "real" }, { executedAt: issuedAt + 1001 },
    { issuedAt: issuedAt + 1, deadlineAt: issuedAt + 120001 },
    { brokerObservation: { ...receipt.brokerObservation, observedAt: issuedAt + 501 } },
  ];
  for (const patch of changed) await assert.rejects(verify({ ...receipt, ...patch }));
  const another = await signed({ ...payload(), brokerObservation: { ...payload().brokerObservation, observedAt: issuedAt + 700 } });
  await assert.rejects(verify({ ...receipt, brokerObservation: another.brokerObservation }));
  const pause = await signed(payload(actions[1]));
  await assert.rejects(verify({ ...pause, pausePersisted: false }));
  await assert.rejects(verify({ ...pause, finalSubmissionGuard: false }));
  await assert.rejects(verify({ ...receipt, signature: pause.signature }));
  await assert.rejects(verify(await signed(payload(), otherKeys.privateKey)));
});

test("missing, revoked, reassigned, unsupported, private and wrong-algorithm pins fail closed", async () => {
  const receipt = await signed();
  await assert.rejects(verifyDeviceReceipt(receipt));
  await assert.rejects(verify(receipt, new Map()));
  await assert.rejects(verify(receipt, { "synthetic-pin-v1": {} }));
  const ec = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const weak = await crypto.subtle.generateKey({ ...algorithm, modulusLength: 1024 }, false, ["sign", "verify"]);
  for (const patch of [{ revoked: true }, { revoked: undefined }, { deviceId: "other-device" },
    { environment: "real" }, { environment: undefined }, { capabilities: [] }, { capabilities: ["resume"] },
    { publicKey: keys.privateKey }, { publicKey: otherKeys.publicKey }, { publicKey: ec.publicKey },
    { publicKey: weak.publicKey }, { publicKey: "not-a-public-CryptoKey" }]) {
    const pins = makePins();
    Object.assign(pins.get("synthetic-pin-v1"), patch);
    await assert.rejects(verify(receipt, pins));
  }
  const changingPins = makePins();
  const inFlight = verify(receipt, changingPins);
  changingPins.get("synthetic-pin-v1").revoked = true;
  await assert.rejects(inFlight);
  const replacedPins = makePins();
  const replacedInFlight = verify(receipt, replacedPins);
  replacedPins.set("synthetic-pin-v1", { ...replacedPins.get("synthetic-pin-v1") });
  await assert.rejects(replacedInFlight);
  const pins = makePins();
  await verify(receipt, pins);
  pins.get("synthetic-pin-v1").revoked = true;
  await assert.rejects(verify(receipt, pins));
  pins.delete("synthetic-pin-v1");
  await assert.rejects(verify(receipt, pins));
});

test("unrecognized/trailing fields, algorithm selection and unsupported schema/action/environment are rejected", async () => {
  const receipt = await signed();
  for (const patch of [{ version: 2 }, { action: "resume" }, { action: "run_session" },
    { action: "cancel" }, { action: "close" }, { environment: "unknown" }, { environment: "real" },
    { receivedAt: issuedAt + 2000 }, { alg: "none" }, { publicKey: "caller-provided" },
    { authenticated: true }, { operatorWriteAuthorized: true }, { capabilities: actions },
    { requestId: requestId.toUpperCase() }, { deviceId: "device\ninjected=true" }, { keyId: "pin=other" },
    { pausePersisted: true }, { trailing: {} }, { signature: undefined }]) {
    await assert.rejects(verify({ ...receipt, ...patch }));
  }
  await assert.rejects(verify({ ...receipt, brokerObservation: { ...receipt.brokerObservation, rawBrokerRecords: [] } }));
  await assert.rejects(verify({ ...receipt, brokerObservation: { ...receipt.brokerObservation, positions: "true" } }));
  await assert.rejects(verify({ ...receipt, brokerObservation: null }));
  const unsigned = { ...receipt };
  delete unsigned.signature;
  await assert.rejects(verify(unsigned));
});

test("strict unpadded base64url rejects padding, trailing bytes/bits, truncation and invalid signatures", async () => {
  const receipt = await signed();
  const invalid = ["", "not-a-signature", receipt.signature + "=", receipt.signature + "A", receipt.signature.slice(1),
    " " + receipt.signature, receipt.signature + "\n", receipt.signature.replace(/./, "!"),
    base64UrlEncode(new Uint8Array(256))];
  // The final character has only two significant bits for a 256-byte RSA signature.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  invalid.push(receipt.signature.slice(0, -1) + alphabet[alphabet.indexOf(receipt.signature.at(-1)) | 1]);
  for (const signature of invalid) await assert.rejects(verify({ ...receipt, signature }));
});

test("valid signatures still cannot establish partial refresh or non-durable/unguarded pause", async () => {
  for (const field of ["account", "positions", "openOrders"]) {
    const value = payload();
    value.brokerObservation[field] = false;
    await assert.rejects(verify(await signed(value)));
  }
  for (const observedAt of [issuedAt - 1, issuedAt, issuedAt + 1001]) {
    const value = payload();
    value.brokerObservation.observedAt = observedAt;
    await assert.rejects(verify(await signed(value)));
  }
  for (const field of ["pausePersisted", "finalSubmissionGuard"]) {
    const value = payload(actions[1]);
    value[field] = false;
    await assert.rejects(verify(await signed(value)));
  }
});

test("safe integer times, server-now checks and strict execution deadline; timely late reports remain valid", async () => {
  for (const invalid of [undefined, null, "1000000", NaN, Infinity, -1, -0, 1.5, Number.MAX_SAFE_INTEGER]) {
    for (const field of ["issuedAt", "deadlineAt", "executedAt"]) {
      assert.throws(() => deviceReceiptSigningBytes({ ...payload(), [field]: invalid }));
    }
  }
  for (const executedAt of [issuedAt - 1, issuedAt + 120000, issuedAt + 120001]) {
    await assert.rejects(verify(await signed({ ...payload(), executedAt }), makePins(), issuedAt + 200000));
  }
  await assert.rejects(verify(await signed(), makePins(), issuedAt + 999));
  for (const now of [NaN, Infinity, -1, "1002000"]) await assert.rejects(verify(await signed(), makePins(), now));
  assert.equal((await verify(await signed(), makePins(), issuedAt + 200000)).proof.authenticated, true);
});

test("HTTP adapter rejects duplicate keys, BOM, whitespace/trailing bytes, malformed/oversized JSON and bad content type", async () => {
  const adapter = createDeviceReceiptVerifier({ pins: makePins(), now: () => issuedAt + 2000 });
  const receipt = await signed();
  const raw = JSON.stringify(receipt);
  assert.equal((await adapter.fetch(req(raw))).status, 200);
  const reversed = JSON.stringify(Object.fromEntries(Object.entries(receipt).reverse()));
  assert.equal((await adapter.fetch(req(reversed))).status, 200);
  for (const body of [raw + "\n", raw + "{}", " " + raw, "﻿" + raw, "{", "null", "[]",
    raw.replace('"version":1', '"version":1,"version":1'),
    raw.replace('"keyId"', '"key\\u0049d"'), raw.replace('"issuedAt":1000000', '"issuedAt":1e6')]) {
    assert.equal((await adapter.fetch(req(body))).status, 403);
  }
  assert.equal((await adapter.fetch(req({ ...receipt, rawBrokerRecords: "x".repeat(9000) }))).status, 413);
  assert.equal((await adapter.fetch(req(raw, { type: "text/plain" }))).status, 403);
});

test("inactive adapter has no operator route and missing pins are explicitly unavailable", async () => {
  const adapter = createDeviceReceiptVerifier({ pins: makePins(), now: () => issuedAt + 2000 });
  for (const options of [{ path: "/demo-control" }, { path: "/command" }, { method: "GET" }]) {
    assert.equal((await adapter.fetch(req({}, options))).status, 403);
  }
  for (const pins of [undefined, new Map(), {}]) {
    const result = await createDeviceReceiptVerifier({ pins }).fetch(req(await signed()));
    assert.equal(result.status, 503);
    assert.equal(result.headers.get("cache-control"), "no-store");
    assert.doesNotMatch(await result.text(), /synthetic-pin|signature/);
  }
});

test("actual verifier/atomic-store integration rejects device-selected issue times then accepts correlated evidence", async () => {
  const pins = makePins();
  const adapter = createDeviceReceiptVerifier({ pins, now: () => issuedAt + 2000 });
  const values = new Map();
  const storage = { async transaction(fn) {
    const draft = structuredClone(values);
    const result = await fn({ get: async key => structuredClone(draft.get(key)),
      put: async (key, value) => draft.set(key, structuredClone(value)) });
    values.clear();
    for (const [key, value] of draft) values.set(key, value);
    return result;
  } };
  let now = issuedAt;
  const env = { DEMO_CONTROL_ORIGIN: "https://synthetic.example",
    DEMO_CONTROL_AUTH: { fetch(request) {
      if (new URL(request.url).pathname === "/demo-control/ack") return adapter.fetch(request);
      return Response.json({ operatorWriteAuthorized: true, operatorReadAuthorized: true,
        deviceId: "synthetic-device", environment: "demo", capabilities: actions });
    } },
    DEMO_CONTROL_DISPATCH: { fetch: async () => new Response(null, { status: 202 }) },
  };
  const store = new DemoControlStore({ storage }, env, { now: () => now });
  const request = { requestId, action: actions[0], deviceId: "synthetic-device", environment: "demo" };
  assert.equal((await store.fetch(new Request("https://synthetic.example/demo-control", {
    method: "POST", headers: { origin: env.DEMO_CONTROL_ORIGIN, "sec-fetch-site": "same-origin", "content-type": "application/json" },
    body: JSON.stringify(request),
  }))).status, 202);
  now = issuedAt + 2000;
  const shifted = await signed({ ...payload(), issuedAt: issuedAt + 1, deadlineAt: issuedAt + 120001 });
  assert.equal((await adapter.fetch(req(shifted))).status, 200); // Cryptographically authentic, but not the server's issued request.
  assert.equal((await store.fetch(req(shifted))).status, 400);
  const valid = await signed();
  const result = await store.fetch(req(valid));
  assert.equal(result.status, 200);
  const applied = await result.json();
  assert.equal(applied.status, "applied");
  assert.equal(applied.issuedAt, issuedAt);
  assert.equal(applied.confirmation.brokerObservation.observedAt, issuedAt + 500);
  assert.equal(applied.confirmation.receivedAt, now);
  assert.equal((await store.fetch(req(valid))).status, 409);
});

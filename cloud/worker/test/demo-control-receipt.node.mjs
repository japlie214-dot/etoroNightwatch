import test from "node:test";
import assert from "node:assert/strict";
import { createRequest, applyReceipt, requestStatus } from "../src/demo-control-receipt.js";

const issuedAt = 1_000_000;
const actions = ["refresh_demo_broker", "pause_demo_orders"];
const reload = (value) => JSON.parse(JSON.stringify(value));
const payload = (action) => ({
  requestId: "12345678-1234-4234-8234-123456789abc", action,
  deviceId: "synthetic-demo-device", environment: "demo",
});
const authority = () => ({
  operatorWriteAuthorized: true, deviceId: "synthetic-demo-device",
  environment: "demo", capabilities: [...actions],
});
const receipt = (action) => ({ ...payload(action), executedAt: issuedAt + 1000 });
const proof = () => ({
  authenticated: true, deviceId: "synthetic-demo-device", environment: "demo",
  brokerObservation: { observedAt: issuedAt + 500, account: true, positions: true, openOrders: true },
  pausePersisted: true, finalSubmissionGuard: true,
});

for (const action of actions) {
  test(`${action}: requested, offline expiry and reload preserve identity without claiming application`, () => {
    const record = reload(createRequest(payload(action), authority(), issuedAt));
    assert.equal(record.requestId, payload(action).requestId);
    assert.equal(record.deadlineAt, issuedAt + 120_000);
    assert.equal(requestStatus(record, issuedAt), "pending");
    assert.equal(requestStatus(record, record.deadlineAt - 1), "pending");
    assert.equal(requestStatus(record, record.deadlineAt), "expired_unconfirmed");
    assert.equal(requestStatus(reload(record), record.deadlineAt + 999), "expired_unconfirmed");
    assert.equal(record.confirmation, null);
  });

  test(`${action}: server clock and minimal record ignore client authority, times and raw data`, () => {
    const record = createRequest({ ...payload(action), issuedAt: issuedAt + 9999,
      deadlineAt: Infinity, confirmation: { executedAt: issuedAt }, rawBrokerRecords: ["private"] },
    { ...authority(), capabilities: [action] }, issuedAt);
    assert.equal(record.issuedAt, issuedAt);
    assert.equal(record.deadlineAt, issuedAt + 120_000);
    assert.equal(record.confirmation, null);
    assert.equal(JSON.stringify(record).includes("private"), false);
    const applied = applyReceipt(record, receipt(action), proof(), issuedAt + 2000);
    assert.equal(requestStatus(reload(applied), issuedAt + 2000), "applied");
    assert.equal(requestStatus(reload(applied), record.deadlineAt), "applied");
    assert.equal(record.confirmation, null);
  });

  test(`${action}: timely execution may be reported late; accepted terminal is immutable`, () => {
    const record = createRequest(payload(action), authority(), issuedAt);
    const before = reload(record);
    const applied = applyReceipt(reload(record), receipt(action), proof(), record.deadlineAt + 5000);
    assert.deepEqual(record, before);
    assert.equal(requestStatus(reload(applied), record.deadlineAt + 6000), "applied");
    assert.equal(applied.confirmation.executedAt, issuedAt + 1000);
    assert.equal(applied.confirmation.receivedAt, record.deadlineAt + 5000);
    const terminal = reload(applied);
    assert.throws(() => applyReceipt(applied, receipt(action), proof(), record.deadlineAt + 6000));
    assert.deepEqual(applied, terminal);
  });

  test(`${action}: request rejects unauthorized, non-demo, unsupported and malformed inputs`, () => {
    const badPayloads = [
      { environment: "real" }, { environment: "unknown" }, { environment: undefined },
      { deviceId: "other" }, { deviceId: "" }, { requestId: "not-a-uuid" },
      { requestId: undefined }, { action: "resume" }, { action: "status" },
      { action: "cancel" }, { action: "close" }, { action: "manual_order" },
    ];
    for (const patch of badPayloads) {
      assert.throws(() => createRequest({ ...payload(action), ...patch }, authority(), issuedAt));
    }
    for (const patch of [
      { operatorWriteAuthorized: false }, { operatorWriteAuthorized: undefined },
      { operatorWriteAuthorized: "true" }, { environment: "real" },
      { deviceId: "other" }, { capabilities: [] }, { capabilities: undefined },
      { capabilities: [actions.find((value) => value !== action)] },
    ]) {
      assert.throws(() => createRequest(payload(action), { ...authority(), ...patch }, issuedAt));
    }
    assert.throws(() => createRequest({ ...payload(action), operatorWriteAuthorized: true,
      capabilities: actions }, { readAuthorized: true }, issuedAt));
    assert.throws(() => createRequest(null, authority(), issuedAt));
    assert.throws(() => createRequest(payload(action), null, issuedAt));
  });

  test(`${action}: forged, mismatched, stale and invalid receipts do not change pending state`, () => {
    const record = createRequest(payload(action), authority(), issuedAt);
    for (const patch of [
      { requestId: "22345678-1234-4234-8234-123456789abc" },
      { action: actions.find((value) => value !== action) },
      { deviceId: "other" }, { environment: "real" }, { environment: undefined },
      { executedAt: issuedAt - 1 }, { executedAt: record.deadlineAt },
      { executedAt: record.deadlineAt + 1 }, { executedAt: NaN },
      { executedAt: Infinity }, { executedAt: "1001000" }, { executedAt: undefined },
    ]) {
      assert.throws(() => applyReceipt(record, { ...receipt(action), ...patch }, proof(), record.deadlineAt + 5000));
      assert.equal(record.confirmation, null);
    }
    for (const patch of [
      { authenticated: false }, { authenticated: undefined }, { authenticated: "true" },
      { deviceId: "other" }, { environment: "real" }, { environment: undefined },
    ]) {
      assert.throws(() => applyReceipt(record, receipt(action), { ...proof(), ...patch }, issuedAt + 2000));
    }
    assert.throws(() => applyReceipt(record, { ...receipt(action), ...proof() }, {}, issuedAt + 2000));
    assert.throws(() => applyReceipt(record, receipt(action), proof(), issuedAt + 999));
    assert.throws(() => applyReceipt(record, null, proof(), issuedAt + 2000));
    assert.equal(requestStatus(record, issuedAt + 2000), "pending");
  });

  test(`${action}: invalid clocks and malformed stored records fail closed`, () => {
    const record = createRequest(payload(action), authority(), issuedAt);
    for (const now of [NaN, Infinity, -1, "1000000", undefined, Number.MAX_VALUE]) {
      assert.throws(() => createRequest(payload(action), authority(), now));
      assert.throws(() => requestStatus(record, now));
      assert.throws(() => applyReceipt(record, receipt(action), proof(), now));
    }
    assert.throws(() => requestStatus(record, issuedAt - 1));
    for (const malformed of [null, {}, { ...record, environment: "real" },
      { ...record, deadlineAt: record.deadlineAt + 1 }, { ...record, confirmation: {} }]) {
      assert.throws(() => requestStatus(malformed, issuedAt + 2000));
      assert.throws(() => applyReceipt(malformed, receipt(action), proof(), issuedAt + 2000));
    }
  });
}

test("refresh requires fresh complete broker observation, not receipt/snapshot time", () => {
  const action = actions[0];
  const record = createRequest(payload(action), authority(), issuedAt);
  for (const brokerObservation of [undefined, {},
    { ...proof().brokerObservation, account: false },
    { ...proof().brokerObservation, positions: false },
    { ...proof().brokerObservation, openOrders: false },
    { ...proof().brokerObservation, account: "true" },
    { ...proof().brokerObservation, observedAt: issuedAt - 1 },
    { ...proof().brokerObservation, observedAt: issuedAt },
    { ...proof().brokerObservation, observedAt: issuedAt + 1001 },
    { ...proof().brokerObservation, observedAt: Infinity },
  ]) {
    assert.throws(() => applyReceipt(record, receipt(action), { ...proof(), brokerObservation }, issuedAt + 2000));
  }
  const applied = applyReceipt(record, { ...receipt(action), rawBrokerRecords: ["private"] }, proof(), issuedAt + 2000);
  assert.equal(applied.confirmation.brokerObservation.observedAt, issuedAt + 500);
  assert.equal(JSON.stringify(applied).includes("private"), false);
  assert.equal(applied.confirmation.receivedAt === applied.confirmation.brokerObservation.observedAt, false);
});

test("pause requires independent durable pause and final submission guard proof", () => {
  const action = actions[1];
  const record = createRequest(payload(action), authority(), issuedAt);
  for (const patch of [
    { pausePersisted: false }, { pausePersisted: undefined }, { pausePersisted: "true" },
    { finalSubmissionGuard: false }, { finalSubmissionGuard: undefined },
  ]) {
    assert.throws(() => applyReceipt(record, { ...receipt(action), pausePersisted: true,
      finalSubmissionGuard: true }, { ...proof(), ...patch }, issuedAt + 2000));
  }
  const applied = reload(applyReceipt(record, receipt(action), proof(), issuedAt + 2000));
  assert.equal(applied.confirmation.pausePersisted, true);
  assert.equal(applied.confirmation.finalSubmissionGuard, true);
  assert.equal(applied.confirmation.brokerObservation, undefined);
});

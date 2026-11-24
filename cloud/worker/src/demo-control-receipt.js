// Pure receipt bookkeeping only: no dispatch, broker query, pause enforcement or store.
const ACTIONS = new Set(["refresh_demo_broker", "pause_demo_orders"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DEADLINE_MS = 120_000;

function requireCondition(condition, message) {
  if (!condition) throw new Error(message);
}

// Timestamps are non-negative integer Unix milliseconds, safe through deadline arithmetic.
function checkTime(value) {
  requireCondition(Number.isSafeInteger(value) && value >= 0 &&
    value <= Number.MAX_SAFE_INTEGER - DEADLINE_MS, "Invalid timestamp");
}

function checkIdentity(value) {
  requireCondition(value && typeof value.requestId === "string" && UUID.test(value.requestId) &&
    ACTIONS.has(value.action) && typeof value.deviceId === "string" && value.deviceId.trim() !== "" &&
    value.environment === "demo", "Invalid demo request identity");
}

function checkConfirmation(record, confirmation, now) {
  requireCondition(confirmation && typeof confirmation === "object", "Invalid confirmation");
  checkTime(confirmation.executedAt);
  checkTime(confirmation.receivedAt);
  requireCondition(confirmation.executedAt >= record.issuedAt &&
    confirmation.executedAt < record.deadlineAt &&
    confirmation.receivedAt >= confirmation.executedAt && confirmation.receivedAt <= now,
  "Execution must be timely; receipt cannot be from the future");
  if (record.action === "refresh_demo_broker") {
    const observation = confirmation.brokerObservation;
    requireCondition(observation && observation.account === true && observation.positions === true &&
      observation.openOrders === true, "Complete broker observation required");
    checkTime(observation.observedAt);
    requireCondition(observation.observedAt > record.issuedAt &&
      observation.observedAt <= confirmation.executedAt, "Fresh broker observation required");
  } else {
    requireCondition(confirmation.pausePersisted === true && confirmation.finalSubmissionGuard === true,
      "Durable pause and final submission guard proof required");
  }
}

function checkRecord(record, now) {
  checkTime(now);
  checkIdentity(record);
  checkTime(record.issuedAt);
  requireCondition(record.deadlineAt === record.issuedAt + DEADLINE_MS && now >= record.issuedAt,
    "Invalid deadline or future request");
  requireCondition(record.confirmation === null || typeof record.confirmation === "object",
    "Invalid stored confirmation");
  if (record.confirmation !== null) checkConfirmation(record, record.confirmation, now);
}

/** authority is server-owned, independently verified write/device capability context, never payload. */
export function createRequest(payload, authority, now) {
  checkTime(now);
  checkIdentity(payload);
  requireCondition(authority?.operatorWriteAuthorized === true && authority.environment === "demo" &&
    authority.deviceId === payload.deviceId && Array.isArray(authority.capabilities) &&
    authority.capabilities.includes(payload.action), "Verified operator write authority and demo capability required");
  return {
    requestId: payload.requestId, action: payload.action, deviceId: payload.deviceId,
    environment: "demo", issuedAt: now, deadlineAt: now + DEADLINE_MS, confirmation: null,
  };
}

/** Read a trusted stored record; expiry means unknown outcome, never evidence of non-execution. */
export function requestStatus(record, now) {
  checkRecord(record, now);
  if (record.confirmation !== null) return "applied";
  return now >= record.deadlineAt ? "expired_unconfirmed" : "pending";
}

/**
 * proof is trusted adapter output after authenticating the receipt AND its bound evidence.
 * These flags are not authentication themselves. Never populate proof from request/receipt JSON.
 * The caller must atomically persist by request ID to prevent concurrent duplicate transitions.
 */
export function applyReceipt(record, receipt, proof, now) {
  checkRecord(record, now);
  requireCondition(record.confirmation === null, "Terminal receipt is immutable; duplicate rejected");
  checkIdentity(receipt);
  requireCondition(proof?.authenticated === true && proof.deviceId === record.deviceId &&
    proof.environment === "demo", "Authenticated demo device proof required");
  for (const key of ["requestId", "action", "deviceId", "environment"]) {
    requireCondition(receipt[key] === record[key], "Receipt identity mismatch");
  }
  const confirmation = { executedAt: receipt.executedAt, receivedAt: now };
  if (record.action === "refresh_demo_broker") {
    const observation = proof.brokerObservation;
    confirmation.brokerObservation = {
      observedAt: observation?.observedAt, account: observation?.account,
      positions: observation?.positions, openOrders: observation?.openOrders,
    };
  } else {
    confirmation.pausePersisted = proof.pausePersisted;
    confirmation.finalSubmissionGuard = proof.finalSubmissionGuard;
  }
  checkConfirmation(record, confirmation, now);
  return {
    requestId: record.requestId, action: record.action, deviceId: record.deviceId,
    environment: "demo", issuedAt: record.issuedAt, deadlineAt: record.deadlineAt, confirmation,
  };
}

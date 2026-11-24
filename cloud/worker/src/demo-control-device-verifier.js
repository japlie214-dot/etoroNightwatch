import { applyReceipt } from "./demo-control-receipt.js";
import { readDemoControlBody } from "./demo-control.js";
import { base64UrlDecodeToBytes, base64UrlEncode, json } from "./utils.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TOKEN = /^[A-Za-z0-9._:-]{1,128}$/;
const BASE_FIELDS = ["version", "keyId", "requestId", "action", "deviceId", "environment",
  "issuedAt", "deadlineAt", "executedAt"];

function requireValid(condition) {
  if (!condition) throw new Error("Invalid signed demo receipt");
}

function exactFields(value, fields) {
  requireValid(value && typeof value === "object" && !Array.isArray(value));
  const keys = Object.keys(value);
  requireValid(keys.length === fields.length && keys.every(key => fields.includes(key)));
}

function timestamp(value) {
  requireValid(Number.isSafeInteger(value) && value >= 0 && !Object.is(value, -0) &&
    value <= Number.MAX_SAFE_INTEGER - 120000);
}

/** Fixed-order, labeled UTF-8/ASCII lines, including a final LF; never sign transport JSON. */
export function deviceReceiptSigningBytes(payload) {
  const refresh = payload?.action === "refresh_demo_broker";
  requireValid(refresh || payload?.action === "pause_demo_orders");
  exactFields(payload, [...BASE_FIELDS, ...(refresh ? ["brokerObservation"] : ["pausePersisted", "finalSubmissionGuard"])]);
  requireValid(payload.version === 1 && typeof payload.keyId === "string" && TOKEN.test(payload.keyId) &&
    typeof payload.requestId === "string" && UUID.test(payload.requestId) &&
    typeof payload.deviceId === "string" && TOKEN.test(payload.deviceId) && payload.environment === "demo");
  for (const field of ["issuedAt", "deadlineAt", "executedAt"]) timestamp(payload[field]);
  requireValid(payload.deadlineAt === payload.issuedAt + 120000);
  const lines = ["nightwatch.demo-control.receipt.v1", "alg=RS256",
    ...BASE_FIELDS.filter(field => field !== "version").map(field => `${field}=${payload[field]}`)];
  if (refresh) {
    const observation = payload.brokerObservation;
    exactFields(observation, ["observedAt", "account", "positions", "openOrders"]);
    timestamp(observation.observedAt);
    for (const field of ["account", "positions", "openOrders"]) requireValid(typeof observation[field] === "boolean");
    lines.push(...["observedAt", "account", "positions", "openOrders"].map(field => `${field}=${observation[field]}`));
  } else {
    for (const field of ["pausePersisted", "finalSubmissionGuard"]) {
      requireValid(typeof payload[field] === "boolean");
      lines.push(`${field}=${payload[field]}`);
    }
  }
  return new TextEncoder().encode(lines.join("\n") + "\n");
}

/**
 * pins is server-owned Map<keyId, { publicKey: CryptoKey, deviceId, environment,
 * capabilities, revoked: false }>. No key, certificate or authority is accepted from wire JSON.
 * Re-read pins each call so revocation takes effect without a cached verification grant.
 */
export async function verifyDeviceReceipt(envelope, { pins, now = Date.now() } = {}) {
  requireValid(envelope && typeof envelope === "object" && !Array.isArray(envelope));
  requireValid(Object.hasOwn(envelope, "signature"));
  const { signature, ...payload } = envelope;
  const bytes = deviceReceiptSigningBytes(payload);
  requireValid(pins instanceof Map && pins.has(payload.keyId));
  const pin = pins.get(payload.keyId);
  const key = pin?.publicKey;
  requireValid(pin?.revoked === false && pin.environment === "demo" && pin.deviceId === payload.deviceId &&
    Array.isArray(pin.capabilities) && pin.capabilities.includes(payload.action) && key?.type === "public" &&
    key.algorithm?.name === "RSASSA-PKCS1-v1_5" && key.algorithm.hash?.name === "SHA-256" &&
    key.algorithm.modulusLength >= 2048 && key.usages.includes("verify"));
  requireValid(typeof signature === "string" && /^[A-Za-z0-9_-]+$/.test(signature));
  const signatureBytes = base64UrlDecodeToBytes(signature);
  requireValid(signatureBytes.byteLength === Math.ceil(key.algorithm.modulusLength / 8) &&
    base64UrlEncode(signatureBytes) === signature);
  requireValid(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, signatureBytes, bytes));
  requireValid(pins.get(payload.keyId) === pin && pin.revoked === false && pin.publicKey === key &&
    pin.deviceId === payload.deviceId && pin.environment === "demo" && pin.capabilities.includes(payload.action));

  const proof = { authenticated: true, deviceId: pin.deviceId, environment: "demo",
    issuedAt: payload.issuedAt, deadlineAt: payload.deadlineAt };
  if (payload.action === "refresh_demo_broker") proof.brokerObservation = { ...payload.brokerObservation };
  else {
    proof.pausePersisted = payload.pausePersisted;
    proof.finalSubmissionGuard = payload.finalSubmissionGuard;
  }
  // Validate complete evidence and timely execution, but signed issue time is not authority:
  // DemoControlStore must match proof.issuedAt/deadlineAt to its server-created stored record.
  applyReceipt({ requestId: payload.requestId, action: payload.action, deviceId: pin.deviceId,
    environment: "demo", issuedAt: payload.issuedAt, deadlineAt: payload.deadlineAt, confirmation: null },
  payload, proof, now);
  return { deviceId: pin.deviceId, environment: "demo", capabilities: [payload.action],
    operatorReadAuthorized: false, operatorWriteAuthorized: false, proof };
}

/** Inactive AUTH receipt adapter; compose with independent operator auth, never a write grant. */
export function createDeviceReceiptVerifier({ pins, now = Date.now } = {}) {
  return {
    async fetch(request) {
      const reply = (body, status) => json(body, { status, headers: { "cache-control": "no-store" } });
      if (request.method !== "POST" || new URL(request.url).pathname !== "/demo-control/ack") {
        return reply({ error: "device receipt route required" }, 403);
      }
      if (!(pins instanceof Map) || pins.size === 0) return reply({ error: "device receipt verification unavailable" }, 503);
      try {
        requireValid(request.headers.get("content-type")?.split(";")[0].trim() === "application/json");
        const { body, bytes } = await readDemoControlBody(request);
        // Reject duplicate keys, escaped aliases, whitespace/BOM and all trailing bytes.
        requireValid(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes) === JSON.stringify(body));
        const authority = await verifyDeviceReceipt(body, { pins, now: now() });
        return reply(authority, 200);
      } catch (error) {
        return reply({ error: "invalid signed demo receipt" }, error?.status === 413 ? 413 : 403);
      }
    },
  };
}

// DEMO_CONTROL_DISPATCH: one POST /dispatch per request, HMAC-bound to the exact envelope the
// phone verifies (android DemoControl.dispatchSigningBytes). Never resends: the caller treats
// any non-2xx as send_failed and an exception as unconfirmed.
import { sendPush, FcmSendError } from "../../worker/src/fcm.js";
import { json } from "../../worker/src/utils.js";

const ACTIONS = ["refresh_demo_broker", "pause_demo_orders"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TOKEN = /^[A-Za-z0-9._:-]{1,128}$/;
const FIELDS = ["version", "requestId", "action", "deviceId", "environment", "issuedAt", "deadlineAt"];
const reply = (body, status) => json(body, { status, headers: { "cache-control": "no-store" } });

export function validEnvelope(e) {
  return Boolean(e) && typeof e === "object" && !Array.isArray(e) &&
    Object.keys(e).length === FIELDS.length && FIELDS.every(k => Object.hasOwn(e, k)) &&
    e.version === 1 && UUID.test(e.requestId) && ACTIONS.includes(e.action) &&
    TOKEN.test(e.deviceId) && e.environment === "demo" &&
    Number.isSafeInteger(e.issuedAt) && e.issuedAt >= 0 && e.deadlineAt === e.issuedAt + 120000;
}

export function dispatchSigningText(e) {
  return ["nightwatch.demo-control.dispatch.v1", "version=1", `requestId=${e.requestId}`,
    `action=${e.action}`, `deviceId=${e.deviceId}`, "environment=demo",
    `issuedAt=${e.issuedAt}`, `deadlineAt=${e.deadlineAt}`].join("\n") + "\n";
}

export async function dispatchHmacHex(secret, e) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(dispatchSigningText(e)));
  return [...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export function createDispatcher({ now = Date.now, push = sendPush } = {}) {
  return {
    async fetch(request, env) {
      if (request.method !== "POST" || new URL(request.url).pathname !== "/dispatch") {
        return reply({ error: "not found" }, 404);
      }
      let envelope;
      try { envelope = await request.json(); } catch { return reply({ error: "invalid envelope" }, 400); }
      if (!validEnvelope(envelope)) return reply({ error: "invalid envelope" }, 400);
      if (now() >= envelope.deadlineAt) return reply({ error: "expired" }, 409);
      if (!env.COMMAND_HMAC_SECRET || !env.NIGHTWATCH_SNAPSHOTS) return reply({ error: "unavailable" }, 503);
      const token = await env.NIGHTWATCH_SNAPSHOTS.get("device_token");
      if (!token) return reply({ error: "no device token" }, 409);
      const hmac = await dispatchHmacHex(env.COMMAND_HMAC_SECRET, envelope);
      const compact = JSON.stringify(Object.fromEntries(FIELDS.map(k => [k, envelope[k]])));
      try {
        await push(env, { token, title: "Nightwatch: demo control", body: envelope.action,
          data: { command: "demo_control", envelope: compact, hmac } });
        return reply({ ok: true }, 200);
      } catch (error) {
        return reply({ error: error instanceof FcmSendError ? "fcm send failed" : "unavailable" }, 502);
      }
    },
  };
}

export default createDispatcher();

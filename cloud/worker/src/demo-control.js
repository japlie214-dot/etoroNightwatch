import { createRequest, applyReceipt, requestStatus } from "./demo-control-receipt.js";
import { json } from "./utils.js";

const ACTIONS = ["refresh_demo_broker", "pause_demo_orders"];
const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const unavailable = () => reply({ error: "demo control unavailable" }, 503);
const reply = (body, status = 200) => json(body, { status, headers: { "cache-control": "no-store" } });
class Rejection extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function sameOrigin(request, origin) {
  return origin && request.headers.get("origin") === origin &&
    request.headers.get("sec-fetch-site") === "same-origin" &&
    request.headers.get("content-type")?.split(";")[0].trim() === "application/json";
}

export async function readDemoControlBody(request) {
  const reader = request.body?.getReader();
  if (!reader) throw new Rejection(400, "JSON body required");
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 8192) {
      await reader.cancel();
      throw new Rejection(413, "demo control body too large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try {
    const body = JSON.parse(new TextDecoder().decode(bytes));
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error();
    return { body, bytes };
  } catch { throw new Rejection(400, "JSON object required"); }
}

export async function routeDemoControl(request, env) {
  if (!env.DEMO_CONTROL_STORE?.idFromName || !env.DEMO_CONTROL_STORE?.get) return unavailable();
  try {
    const id = env.DEMO_CONTROL_STORE.idFromName("demo-control-v1");
    return await env.DEMO_CONTROL_STORE.get(id).fetch(request);
  } catch { return unavailable(); }
}

/**
 * Dedicated Durable Object, never snapshot KV. Permanent UUID records are replay tombstones.
 * AUTH is a trusted service binding, not an HTTP identity header: it must verify credentials,
 * audience, expiry, operator ACL/device capability and (for ack) the exact signed body evidence.
 * DISPATCH must sign/bind the full envelope below, never translate it to legacy /command.
 */
export class DemoControlStore {
  constructor(ctx, env, { now = Date.now } = {}) {
    this.storage = ctx.storage;
    this.env = env;
    this.now = now;
  }

  view(entry) {
    return { ...entry.record, delivery: entry.delivery, status: requestStatus(entry.record, this.now()) };
  }

  async fetch(request) {
    try { return await this.handle(request); }
    catch (error) {
      return error instanceof Rejection ? reply({ error: error.message }, error.status) : unavailable();
    }
  }

  async handle(request) {
    const url = new URL(request.url);
    const ack = url.pathname === "/demo-control/ack";
    if (!(url.pathname === "/demo-control" && ["GET", "POST"].includes(request.method)) &&
        !(ack && request.method === "POST")) return reply({ error: "not found" }, 404);
    if (!this.env.DEMO_CONTROL_AUTH?.fetch || !this.storage?.transaction) return unavailable();
    if (request.method === "POST" && !ack && !sameOrigin(request, this.env.DEMO_CONTROL_ORIGIN)) {
      throw new Rejection(403, "same-origin JSON request required");
    }
    // Bound before forwarding to the verifier; its response is never echoed to the browser.
    const input = request.method === "POST" ? await readDemoControlBody(request) : null;
    const body = input?.body;
    // Preserve exact receipt bytes for verification; cloning a tee before limiting can stall cancellation.
    const authRequest = input ? new Request(request, { body: input.bytes }) : request;
    const authResponse = await this.env.DEMO_CONTROL_AUTH.fetch(authRequest);
    if ([401, 403].includes(authResponse.status)) throw new Rejection(403, "demo control forbidden");
    if (!authResponse.ok) return unavailable();
    const authority = await authResponse.json();
    if (authority.environment !== "demo" || typeof authority.deviceId !== "string" ||
        !authority.deviceId.trim() || !Array.isArray(authority.capabilities)) return unavailable();

    if (request.method === "GET") {
      if (authority.operatorReadAuthorized !== true && authority.operatorWriteAuthorized !== true) {
        throw new Rejection(403, "operator read authority required");
      }
      const requestId = url.searchParams.get("requestId");
      if (requestId !== null && !ID.test(requestId)) throw new Rejection(400, "UUID required");
      const entries = await this.storage.transaction(async (tx) => {
        const ids = requestId ? [requestId.toLowerCase()] : (await Promise.all(ACTIONS.map(action =>
          tx.get(`latest:${authority.deviceId}:${action}`)))).filter(Boolean);
        return (await Promise.all(ids.map(id => tx.get(`request:${id}`)))).filter(Boolean);
      });
      if (entries.some(entry => entry.record.deviceId !== authority.deviceId)) {
        throw new Rejection(403, "device mismatch");
      }
      return reply({ deviceId: authority.deviceId, environment: "demo",
        capabilities: ACTIONS.filter(action => authority.capabilities.includes(action)),
        writable: authority.operatorWriteAuthorized === true && ACTIONS.some(action => authority.capabilities.includes(action)) &&
          Boolean(this.env.DEMO_CONTROL_DISPATCH?.fetch) && Boolean(this.env.DEMO_CONTROL_ORIGIN),
        records: entries.map(entry => this.view(entry)) });
    }

    if (typeof body.requestId !== "string" || !ID.test(body.requestId)) throw new Rejection(400, "UUID required");
    body.requestId = body.requestId.toLowerCase();
    const key = `request:${body.requestId}`;
    if (ack) {
      // Proof comes only from AUTH's verification of this exact receipt, never from body flags.
      const entry = await this.storage.transaction(async (tx) => {
        const current = await tx.get(key);
        if (!current) throw new Rejection(404, "unknown request");
        if (authority.deviceId !== current.record.deviceId || !authority.capabilities.includes(current.record.action)) {
          throw new Rejection(403, "device capability mismatch");
        }
        if (current.record.confirmation) throw new Rejection(409, "duplicate receipt");
        if (authority.proof?.issuedAt !== current.record.issuedAt ||
            authority.proof?.deadlineAt !== current.record.deadlineAt) {
          throw new Rejection(400, "receipt request times mismatch");
        }
        let record;
        try { record = applyReceipt(current.record, body, authority.proof, this.now()); }
        catch { throw new Rejection(400, "invalid demo receipt"); }
        const next = { ...current, record };
        await tx.put(key, next);
        return next;
      });
      return reply(this.view(entry));
    }

    if (authority.operatorWriteAuthorized !== true) throw new Rejection(403, "operator write authority required");
    if (!this.env.DEMO_CONTROL_DISPATCH?.fetch) return unavailable();
    if (Object.keys(body).some(key => !["requestId", "action", "deviceId", "environment"].includes(key))) {
      throw new Rejection(400, "unexpected request fields");
    }
    let record;
    try { record = createRequest(body, authority, this.now()); }
    catch { throw new Rejection(400, "invalid or unsupported demo request"); }
    await this.storage.transaction(async (tx) => {
      if (await tx.get(key)) throw new Rejection(409, "duplicate request");
      await tx.put(key, { record, delivery: "unconfirmed" });
      await tx.put(`latest:${record.deviceId}:${record.action}`, record.requestId);
    });
    // Reserve before I/O. A crash or ambiguous transport result must never trigger a resend.
    let delivery = "unconfirmed";
    if (this.now() < record.deadlineAt) {
      try {
        const result = await this.env.DEMO_CONTROL_DISPATCH.fetch(new Request("https://demo-control.internal/dispatch", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ version: 1, requestId: record.requestId, action: record.action,
            deviceId: record.deviceId, environment: record.environment,
            issuedAt: record.issuedAt, deadlineAt: record.deadlineAt }),
        }));
        delivery = result.ok ? "sent" : "send_failed";
      } catch { /* Transport failure is ambiguous, not proof of non-execution. */ }
    }
    const entry = await this.storage.transaction(async (tx) => {
      const current = await tx.get(key);
      const next = { ...current, delivery };
      await tx.put(key, next);
      return next;
    });
    return reply(this.view(entry), 202);
  }
}

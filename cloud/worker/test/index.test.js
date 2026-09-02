import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import worker from "../src/index.js";
import { _resetTokenCacheForTests } from "../src/fcm.js";

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

async function makeServiceAccountJson() {
  const keyPair = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"]
  );
  const pkcs8 = await crypto.subtle.exportKey("pkcs8", keyPair.privateKey);
  const lines = bufToBase64(pkcs8).match(/.{1,64}/g).join("\n");
  const pem = `-----BEGIN PRIVATE KEY-----\n${lines}\n-----END PRIVATE KEY-----\n`;
  return JSON.stringify({ client_email: "nightwatch@test.iam.gserviceaccount.com", private_key: pem });
}

/** Minimal in-memory stand-in for the Workers KV API surface this code uses. */
class FakeKv {
  constructor() {
    this.store = new Map();
  }
  async get(key) {
    return this.store.has(key) ? this.store.get(key) : null;
  }
  async put(key, value) {
    this.store.set(key, value);
  }
  async delete(key) {
    this.store.delete(key);
  }
}

function baseEnv(overrides = {}) {
  return {
    NIGHTWATCH_KV: new FakeKv(),
    PHONE_PUSH_SECRET: "phone-secret",
    WEB_READ_TOKEN: "web-token",
    COMMAND_HMAC_SECRET: "command-secret",
    FIREBASE_PROJECT_ID: "test-project",
    ...overrides,
  };
}

function req(method, path, { headers = {}, body } = {}) {
  return new Request(`https://worker.example${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

describe("GET /health", () => {
  it("reports every secret absent, and never leaks values, when nothing is configured", async () => {
    const env = { NIGHTWATCH_KV: undefined };
    const res = await worker.fetch(req("GET", "/health"), env, {});
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.ready).toBe(false);
    expect(body.kv_bound).toBe(false);
    expect(body.secrets_present).toEqual({
      FIREBASE_SERVICE_ACCOUNT_JSON: false,
      PHONE_PUSH_SECRET: false,
      WEB_READ_TOKEN: false,
      COMMAND_HMAC_SECRET: false,
    });
    expect(body.vars_present).toEqual({ FIREBASE_PROJECT_ID: false });
    expect(JSON.stringify(body)).not.toMatch(/phone-secret|web-token|command-secret/);
  });

  it("reports ready:true and only booleans once everything is configured", async () => {
    const env = baseEnv({ FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson() });
    const res = await worker.fetch(req("GET", "/health"), env, {});
    const body = await res.json();
    expect(body.ready).toBe(true);
    expect(Object.values(body.secrets_present).every((v) => v === true)).toBe(true);
    expect(JSON.stringify(body)).not.toContain(env.FIREBASE_SERVICE_ACCOUNT_JSON);
  });
});

describe("auth rejection on every authenticated route", () => {
  it("POST /snapshot without the phone secret -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("POST", "/snapshot", { body: { equity: 100 } }), env, {});
    expect(res.status).toBe(401);
  });

  it("POST /snapshot with the wrong phone secret -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(
      req("POST", "/snapshot", { headers: { "x-phone-push-secret": "nope" }, body: { equity: 100 } }),
      env,
      {}
    );
    expect(res.status).toBe(401);
  });

  it("GET /snapshot without the web token -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("GET", "/snapshot"), env, {});
    expect(res.status).toBe(401);
  });

  it("POST /refresh without the web token -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("POST", "/refresh"), env, {});
    expect(res.status).toBe(401);
  });

  it("POST /command without the web token -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("POST", "/command", { body: { command: "status" } }), env, {});
    expect(res.status).toBe(401);
  });

  it("POST /device-token without the phone secret -> 401", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("POST", "/device-token", { body: { token: "abc" } }), env, {});
    expect(res.status).toBe(401);
  });
});

describe("snapshot round trip", () => {
  it("a pushed snapshot is readable back with an updated_at timestamp", async () => {
    const env = baseEnv();
    const snapshot = { equity: 12345.67, cash: 100, positions: [{ symbol: "UPRO", units: 10 }] };

    const postRes = await worker.fetch(
      req("POST", "/snapshot", { headers: { "x-phone-push-secret": "phone-secret" }, body: snapshot }),
      env,
      {}
    );
    expect(postRes.status).toBe(200);
    const postBody = await postRes.json();
    expect(postBody.ok).toBe(true);
    expect(typeof postBody.updated_at).toBe("string");

    const getRes = await worker.fetch(req("GET", "/snapshot", { headers: { "x-web-read-token": "web-token" } }), env, {});
    expect(getRes.status).toBe(200);
    const getBody = await getRes.json();
    expect(getBody.snapshot).toEqual(snapshot);
    expect(getBody.updated_at).toBe(postBody.updated_at);
  });

  it("GET /snapshot before anything was ever pushed returns null, not an error", async () => {
    const env = baseEnv();
    const res = await worker.fetch(req("GET", "/snapshot", { headers: { "x-web-read-token": "web-token" } }), env, {});
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.snapshot).toBeNull();
  });

  it("rejects an oversized snapshot body", async () => {
    const env = baseEnv();
    const huge = { blob: "x".repeat(300 * 1024) };
    const res = await worker.fetch(
      req("POST", "/snapshot", { headers: { "x-phone-push-secret": "phone-secret" }, body: huge }),
      env,
      {}
    );
    expect(res.status).toBe(413);
  });
});

describe("POST /command", () => {
  it("rejects a command outside the allowed set before ever touching the device token", async () => {
    const env = baseEnv(); // no device token registered at all
    const res = await worker.fetch(
      req("POST", "/command", { headers: { "x-web-read-token": "web-token" }, body: { command: "arm_live" } }),
      env,
      {}
    );
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toMatch(/not allowed/);
  });

  it("rejects every command not in the read-only + halt/resume set", async () => {
    const env = baseEnv();
    for (const bad of ["trade", "buy_upro", "config_set", "shell"]) {
      const res = await worker.fetch(
        req("POST", "/command", { headers: { "x-web-read-token": "web-token" }, body: { command: bad } }),
        env,
        {}
      );
      expect(res.status).toBe(400);
    }
  });

  it("returns 409 for an allowed command when no device token is registered", async () => {
    const env = baseEnv();
    const res = await worker.fetch(
      req("POST", "/command", { headers: { "x-web-read-token": "web-token" }, body: { command: "status" } }),
      env,
      {}
    );
    expect(res.status).toBe(409);
  });

  it("sends an allowed command via FCM and increments the counter", async () => {
    _resetTokenCacheForTests();
    const env = baseEnv({ FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson() });
    await env.NIGHTWATCH_KV.put("device_token", "device-abc");

    const fetchMock = vi.fn(async (url) => {
      if (String(url).includes("oauth2.googleapis.com")) {
        return { ok: true, json: async () => ({ access_token: "tok", expires_in: 3600 }) };
      }
      return { ok: true, json: async () => ({ name: "projects/test-project/messages/1" }) };
    });
    vi.stubGlobal("fetch", fetchMock);

    try {
      const res = await worker.fetch(
        req("POST", "/command", { headers: { "x-web-read-token": "web-token" }, body: { command: "/halt" } }),
        env,
        {}
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.ok).toBe(true);
      expect(body.command).toBe("halt");
      expect(body.counter).toBe(1);
      expect(await env.NIGHTWATCH_KV.get("command_counter")).toBe("1");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("POST /refresh", () => {
  it("clears a stale device token from KV on an UNREGISTERED FCM error", async () => {
    _resetTokenCacheForTests();
    const env = baseEnv({ FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson() });
    await env.NIGHTWATCH_KV.put("device_token", "dead-token");

    const fetchMock = vi.fn(async (url) => {
      if (String(url).includes("oauth2.googleapis.com")) {
        return { ok: true, json: async () => ({ access_token: "tok", expires_in: 3600 }) };
      }
      return {
        ok: false,
        status: 404,
        json: async () => ({
          error: {
            code: 404,
            status: "NOT_FOUND",
            message: "gone",
            details: [{ "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", errorCode: "UNREGISTERED" }],
          },
        }),
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    try {
      const res = await worker.fetch(req("POST", "/refresh", { headers: { "x-web-read-token": "web-token" } }), env, {});
      expect(res.status).toBe(502);
      expect(await env.NIGHTWATCH_KV.get("device_token")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("POST /device-token", () => {
  it("stores the token in KV", async () => {
    const env = baseEnv();
    const res = await worker.fetch(
      req("POST", "/device-token", { headers: { "x-phone-push-secret": "phone-secret" }, body: { token: "new-token-xyz" } }),
      env,
      {}
    );
    expect(res.status).toBe(200);
    expect(await env.NIGHTWATCH_KV.get("device_token")).toBe("new-token-xyz");
  });
});

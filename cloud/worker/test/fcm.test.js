import { describe, it, expect, beforeEach } from "vitest";
import { getAccessToken, sendPush, isStaleTokenError, FcmConfigError, FcmSendError, _resetTokenCacheForTests } from "../src/fcm.js";

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

function fakeFetchSequence(responses) {
  let i = 0;
  return async (url, init) => {
    const r = responses[Math.min(i, responses.length - 1)];
    i++;
    return {
      ok: r.ok,
      status: r.status,
      json: async () => r.json,
      text: async () => JSON.stringify(r.json ?? {}),
    };
  };
}

describe("getAccessToken", () => {
  beforeEach(() => _resetTokenCacheForTests());

  it("exchanges the signed JWT for an access token and caches it", async () => {
    const env = { FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson() };
    let calls = 0;
    const fetchImpl = async (url, init) => {
      calls++;
      expect(url).toBe("https://oauth2.googleapis.com/token");
      expect(init.method).toBe("POST");
      expect(init.body).toMatch(/grant_type=urn%3Aietf/);
      return { ok: true, json: async () => ({ access_token: "tok-1", expires_in: 3600 }) };
    };
    const token1 = await getAccessToken(env, fetchImpl);
    const token2 = await getAccessToken(env, fetchImpl);
    expect(token1).toBe("tok-1");
    expect(token2).toBe("tok-1");
    expect(calls).toBe(1); // second call served from cache
  });

  it("throws FcmConfigError when the secret is missing", async () => {
    await expect(getAccessToken({}, async () => ({ ok: true, json: async () => ({}) }))).rejects.toThrow(FcmConfigError);
  });

  it("throws FcmConfigError when the secret is not valid JSON", async () => {
    await expect(
      getAccessToken({ FIREBASE_SERVICE_ACCOUNT_JSON: "not-json" }, async () => ({ ok: true, json: async () => ({}) }))
    ).rejects.toThrow(FcmConfigError);
  });
});

describe("sendPush", () => {
  beforeEach(() => _resetTokenCacheForTests());

  it("sends a high-priority push with a visible notification block", async () => {
    const env = {
      FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson(),
      FIREBASE_PROJECT_ID: "test-project",
    };
    const fetchImpl = fakeFetchSequence([
      { ok: true, json: { access_token: "tok-1", expires_in: 3600 } },
      { ok: true, json: { name: "projects/test-project/messages/abc123" } },
    ]);
    const captured = [];
    const wrappedFetch = async (url, init) => {
      captured.push({ url, init });
      return fetchImpl(url, init);
    };

    const result = await sendPush(
      env,
      { token: "device-token-1", title: "t", body: "b", data: { command: "refresh" } },
      wrappedFetch
    );

    expect(result.ok).toBe(true);
    expect(result.name).toBe("projects/test-project/messages/abc123");

    const sendCall = captured[1];
    expect(sendCall.url).toBe("https://fcm.googleapis.com/v1/projects/test-project/messages:send");
    expect(sendCall.init.headers.authorization).toBe("Bearer tok-1");
    const payload = JSON.parse(sendCall.init.body);
    expect(payload.message.android.priority).toBe("high");
    expect(payload.message.notification).toEqual({ title: "t", body: "b" });
    expect(payload.message.data).toEqual({ command: "refresh" });
  });

  it("surfaces UNREGISTERED as a recognizable stale-token error", async () => {
    const env = { FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson(), FIREBASE_PROJECT_ID: "p" };
    const fetchImpl = fakeFetchSequence([
      { ok: true, json: { access_token: "tok-1", expires_in: 3600 } },
      {
        ok: false,
        status: 404,
        json: {
          error: {
            code: 404,
            message: "Requested entity was not found.",
            status: "NOT_FOUND",
            details: [{ "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", errorCode: "UNREGISTERED" }],
          },
        },
      },
    ]);

    let caught;
    try {
      await sendPush(env, { token: "dead-token", title: "t", body: "b" }, fetchImpl);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(FcmSendError);
    expect(caught.errorCode).toBe("UNREGISTERED");
    expect(isStaleTokenError(caught.errorCode)).toBe(true);
  });

  it("surfaces SENDER_ID_MISMATCH distinctly and it is not treated as stale-token", async () => {
    const env = { FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson(), FIREBASE_PROJECT_ID: "p" };
    const fetchImpl = fakeFetchSequence([
      { ok: true, json: { access_token: "tok-1", expires_in: 3600 } },
      {
        ok: false,
        status: 403,
        json: {
          error: {
            code: 403,
            message: "Mismatched sender.",
            status: "PERMISSION_DENIED",
            details: [{ "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", errorCode: "SENDER_ID_MISMATCH" }],
          },
        },
      },
    ]);

    let caught;
    try {
      await sendPush(env, { token: "some-token", title: "t", body: "b" }, fetchImpl);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(FcmSendError);
    expect(caught.errorCode).toBe("SENDER_ID_MISMATCH");
    expect(isStaleTokenError(caught.errorCode)).toBe(false);
  });

  it("throws FcmConfigError when FIREBASE_PROJECT_ID is missing", async () => {
    const env = { FIREBASE_SERVICE_ACCOUNT_JSON: await makeServiceAccountJson() };
    await expect(sendPush(env, { token: "t", title: "a", body: "b" }, async () => ({ ok: true, json: async () => ({}) }))).rejects.toThrow(
      FcmConfigError
    );
  });
});

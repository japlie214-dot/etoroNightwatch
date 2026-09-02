// FCM HTTP v1 client.
//
// Legacy FCM server keys are gone (deprecated 2023-06-20, shut down from
// 2024-07-22 — https://firebase.google.com/support/faq#fcm-23-deprecation).
// The only path now is HTTP v1:
//   POST https://fcm.googleapis.com/v1/projects/<project_id>/messages:send
//   Authorization: Bearer <OAuth 2.0 access token>
// (https://firebase.google.com/docs/cloud-messaging/send-message,
//  https://firebase.google.com/docs/cloud-messaging/migrate-v1)
//
// The access token is minted from the service-account JSON via the
// standard Google OAuth2 service-account (JWT-bearer) flow: sign a JWT
// (jwt.js), POST it to https://oauth2.googleapis.com/token, get back a
// bearer token valid for `expires_in` seconds (Google's default: 3600).
// https://developers.google.com/identity/protocols/oauth2/service-account
//
// High-priority messages must always carry a visible `notification`
// block, per design note and Google's own guidance: an app that keeps
// sending high-priority pushes with no visible UI for ~7 days gets its
// high-priority sending privilege throttled/demoted
// (https://firebase.google.com/docs/cloud-messaging/concept-options —
// "message priority" / delivery guidance). We build every push here with
// both `android.priority` and a `notification` block for that reason —
// see DESIGN.md §8.1 (the on-device demotion canary reads
// RemoteMessage.getPriority()).

import { signServiceAccountJwt } from "./jwt.js";

const FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging";
const TOKEN_URI = "https://oauth2.googleapis.com/token";
// Refresh this many seconds before the token's real expiry, so a request
// in flight never races an about-to-expire token.
const TOKEN_SAFETY_MARGIN_SECONDS = 300;

// Module-scope cache. A warm Worker isolate can serve many requests, so
// caching here avoids re-signing a JWT and round-tripping to Google's
// token endpoint on every single push — the token is valid for ~1 hour.
// This is *not* guaranteed to survive between requests (isolates can be
// evicted at any time), so it is a pure optimization, never relied on for
// correctness: getAccessToken() always re-mints if the cache is empty or
// stale.
let cachedToken = null; // { accessToken: string, expiresAtMs: number }

export function _resetTokenCacheForTests() {
  cachedToken = null;
}

function parseServiceAccount(env) {
  const raw = env.FIREBASE_SERVICE_ACCOUNT_JSON;
  if (!raw) {
    throw new FcmConfigError("FIREBASE_SERVICE_ACCOUNT_JSON secret is not set");
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    throw new FcmConfigError(`FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: ${err.message}`);
  }
}

export class FcmConfigError extends Error {}
export class FcmSendError extends Error {
  constructor(message, { status, errorCode, body } = {}) {
    super(message);
    this.status = status;
    this.errorCode = errorCode; // e.g. "UNREGISTERED", "SENDER_ID_MISMATCH"
    this.body = body;
  }
}

/**
 * Returns a valid OAuth2 access token for the FCM HTTP v1 API, minting
 * and caching a new one if needed.
 * @param {object} env Worker environment bindings/secrets.
 * @param {typeof fetch} [fetchImpl] injectable for tests.
 */
export async function getAccessToken(env, fetchImpl = fetch) {
  const now = Date.now();
  if (cachedToken && cachedToken.expiresAtMs - TOKEN_SAFETY_MARGIN_SECONDS * 1000 > now) {
    return cachedToken.accessToken;
  }

  const serviceAccount = parseServiceAccount(env);
  const assertion = await signServiceAccountJwt(serviceAccount, FCM_SCOPE);

  const body = new URLSearchParams({
    grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
    assertion,
  });

  const res = await fetchImpl(TOKEN_URI, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new FcmConfigError(`OAuth token exchange failed (${res.status}): ${text}`);
  }

  const json = await res.json();
  if (!json.access_token) {
    throw new FcmConfigError("OAuth token exchange response missing access_token");
  }
  const expiresInSeconds = json.expires_in ?? 3600;
  cachedToken = {
    accessToken: json.access_token,
    expiresAtMs: Date.now() + expiresInSeconds * 1000,
  };
  return cachedToken.accessToken;
}

/** FCM `data` payload values must all be strings. */
function stringifyData(data) {
  const out = {};
  for (const [k, v] of Object.entries(data || {})) {
    out[k] = typeof v === "string" ? v : JSON.stringify(v);
  }
  return out;
}

/**
 * Sends a high-priority (by default) FCM push carrying both a visible
 * notification and a data payload.
 *
 * @param {object} env
 * @param {object} opts
 * @param {string} opts.token FCM registration token of the target device.
 * @param {string} opts.title notification title.
 * @param {string} opts.body notification body.
 * @param {object} [opts.data] string-valued data payload, e.g. {command: "refresh"}.
 * @param {"high"|"normal"} [opts.priority]
 * @param {typeof fetch} [fetchImpl] injectable for tests.
 * @returns {Promise<{ok: true, name: string}>}
 * @throws {FcmSendError} with `.errorCode` set for known FCM error codes.
 */
export async function sendPush(env, { token, title, body, data, priority = "high" }, fetchImpl = fetch) {
  if (!token) throw new FcmSendError("no device token to send to", { errorCode: "NO_TOKEN" });
  if (!env.FIREBASE_PROJECT_ID) {
    throw new FcmConfigError("FIREBASE_PROJECT_ID var is not set");
  }

  const accessToken = await getAccessToken(env, fetchImpl);
  const url = `https://fcm.googleapis.com/v1/projects/${env.FIREBASE_PROJECT_ID}/messages:send`;

  const payload = {
    message: {
      token,
      android: {
        priority: priority === "high" ? "high" : "normal",
      },
      notification: { title, body },
      data: stringifyData(data),
    },
  };

  const res = await fetchImpl(url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${accessToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  const responseJson = await res.json().catch(() => ({}));

  if (!res.ok) {
    // Error shape per https://firebase.google.com/docs/cloud-messaging/error-codes:
    // { error: { code, message, status, details: [{ "@type": "...FcmError", errorCode }] } }
    const details = responseJson?.error?.details || [];
    const fcmDetail = details.find((d) => typeof d.errorCode === "string");
    const errorCode = fcmDetail?.errorCode || responseJson?.error?.status || "UNKNOWN";
    throw new FcmSendError(responseJson?.error?.message || `FCM send failed (${res.status})`, {
      status: res.status,
      errorCode,
      body: responseJson,
    });
  }

  return { ok: true, name: responseJson.name };
}

/** True for error codes that mean the device token is dead and should be dropped from KV. */
export function isStaleTokenError(errorCode) {
  return errorCode === "UNREGISTERED" || errorCode === "NOT_FOUND";
}

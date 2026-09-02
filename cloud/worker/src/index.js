// Cloudflare Worker for etoroNightwatch — DESIGN.md §9 (web app / Worker
// architecture), §7 (host integration / redundant cron trigger), §8
// (Telegram / FCM inbound command channel), §6.F/§6.I (safety).
//
// Routes (all require auth except /health):
//   POST /snapshot      phone -> Worker   (PHONE_PUSH_SECRET)
//   GET  /snapshot       web app <- Worker (WEB_READ_TOKEN)
//   POST /refresh        web app -> Worker -> FCM push to phone (WEB_READ_TOKEN)
//   POST /command         web/curl -> Worker -> FCM push to phone (WEB_READ_TOKEN)
//   POST /device-token   phone -> Worker   (PHONE_PUSH_SECRET)
//   GET  /health         unauthenticated, boolean secret presence only
//
// scheduled() is the redundant daily cron trigger described in DESIGN.md
// §7.2 / §9.1: independent of the phone's own on-device `setAlarmClock()`,
// cross-checking it rather than replacing it.

import { json, unauthorized, badRequest, checkSecret, nowIso } from "./utils.js";
import { sendPush, isStaleTokenError, FcmConfigError, FcmSendError } from "./fcm.js";
import { ALLOWED_COMMANDS, isAllowedCommand, normalizeCommand, hmacCommand } from "./commands.js";
import { shouldFireSessionTrigger, tradingDateKey, isHalfDay } from "./holidays.js";

const MAX_SNAPSHOT_BODY_BYTES = 256 * 1024; // 256 KiB is generous for a portfolio snapshot JSON
const MAX_COMMAND_BODY_BYTES = 4 * 1024;

const KV_KEYS = {
  snapshot: "snapshot",
  deviceToken: "device_token",
  commandCounter: "command_counter",
};

const REQUIRED_SECRETS = [
  "FIREBASE_SERVICE_ACCOUNT_JSON",
  "PHONE_PUSH_SECRET",
  "WEB_READ_TOKEN",
  "COMMAND_HMAC_SECRET",
];
const REQUIRED_VARS = ["FIREBASE_PROJECT_ID"];

async function readBodyWithLimit(request, maxBytes) {
  const contentLength = request.headers.get("content-length");
  if (contentLength && Number(contentLength) > maxBytes) {
    return { tooLarge: true };
  }
  const buf = await request.arrayBuffer();
  if (buf.byteLength > maxBytes) return { tooLarge: true };
  const text = new TextDecoder().decode(buf);
  try {
    return { data: JSON.parse(text) };
  } catch {
    return { invalidJson: true };
  }
}

function requireKv(env) {
  if (!env.NIGHTWATCH_KV) {
    throw new Error("NIGHTWATCH_KV binding is not configured (see wrangler.toml)");
  }
  return env.NIGHTWATCH_KV;
}

// --- /health -----------------------------------------------------------
// Unauthenticated by design: this is the first diagnostic the user hits,
// including before any secret has been set. It must NEVER reveal a
// secret's value — only whether it is present. (DESIGN.md instructs this
// explicitly.)
function handleHealth(env) {
  const secrets = {};
  for (const name of REQUIRED_SECRETS) secrets[name] = Boolean(env[name] && String(env[name]).length > 0);
  const vars = {};
  for (const name of REQUIRED_VARS) vars[name] = Boolean(env[name] && String(env[name]).length > 0);
  const kvBound = Boolean(env.NIGHTWATCH_KV);
  const allSecretsPresent = Object.values(secrets).every(Boolean) && Object.values(vars).every(Boolean);
  return json({
    ok: true,
    service: "etoro-nightwatch-worker",
    time: nowIso(),
    kv_bound: kvBound,
    secrets_present: secrets,
    vars_present: vars,
    ready: allSecretsPresent && kvBound,
  });
}

// --- POST /snapshot ------------------------------------------------------
async function handleSnapshotPost(request, env) {
  if (!(await checkSecret(request, "x-phone-push-secret", env.PHONE_PUSH_SECRET))) {
    return unauthorized("missing or invalid x-phone-push-secret");
  }
  const { data, tooLarge, invalidJson } = await readBodyWithLimit(request, MAX_SNAPSHOT_BODY_BYTES);
  if (tooLarge) return json({ error: "snapshot body too large" }, { status: 413 });
  if (invalidJson) return badRequest("snapshot body is not valid JSON");
  if (!data || typeof data !== "object") return badRequest("snapshot body must be a JSON object");

  const kv = requireKv(env);
  const record = { snapshot: data, updated_at: nowIso() };
  await kv.put(KV_KEYS.snapshot, JSON.stringify(record));
  return json({ ok: true, updated_at: record.updated_at });
}

// --- GET /snapshot ---------------------------------------------------------
async function handleSnapshotGet(request, env) {
  if (!(await checkSecret(request, "x-web-read-token", env.WEB_READ_TOKEN))) {
    return unauthorized("missing or invalid x-web-read-token");
  }
  const kv = requireKv(env);
  const raw = await kv.get(KV_KEYS.snapshot);
  if (!raw) return json({ snapshot: null, updated_at: null });
  const record = JSON.parse(raw);
  return json(record);
}

// --- POST /device-token ---------------------------------------------------
async function handleDeviceToken(request, env) {
  if (!(await checkSecret(request, "x-phone-push-secret", env.PHONE_PUSH_SECRET))) {
    return unauthorized("missing or invalid x-phone-push-secret");
  }
  const { data, tooLarge, invalidJson } = await readBodyWithLimit(request, MAX_COMMAND_BODY_BYTES);
  if (tooLarge) return json({ error: "body too large" }, { status: 413 });
  if (invalidJson) return badRequest("body is not valid JSON");
  const token = data?.token;
  if (!token || typeof token !== "string") return badRequest('body must be {"token": "<fcm token>"}');

  const kv = requireKv(env);
  await kv.put(KV_KEYS.deviceToken, token);
  return json({ ok: true, updated_at: nowIso() });
}

async function getDeviceTokenOrError(kv) {
  const token = await kv.get(KV_KEYS.deviceToken);
  if (!token) {
    return { error: json({ error: "no device token registered yet (phone has not called POST /device-token)" }, { status: 409 }) };
  }
  return { token };
}

/** Sends a push and, on a stale-token FCM error, clears it from KV (fcm.js contract). */
async function sendPushAndHandleStaleToken(env, kv, pushArgs) {
  try {
    return { result: await sendPush(env, pushArgs) };
  } catch (err) {
    if (err instanceof FcmSendError) {
      if (isStaleTokenError(err.errorCode)) {
        await kv.delete(KV_KEYS.deviceToken);
      }
      return {
        error: json(
          { error: "fcm send failed", errorCode: err.errorCode, message: err.message },
          { status: err.errorCode === "SENDER_ID_MISMATCH" ? 502 : 502 }
        ),
      };
    }
    if (err instanceof FcmConfigError) {
      return { error: json({ error: "fcm not configured", message: err.message }, { status: 500 }) };
    }
    throw err;
  }
}

// --- POST /refresh --------------------------------------------------------
async function handleRefresh(request, env) {
  if (!(await checkSecret(request, "x-web-read-token", env.WEB_READ_TOKEN))) {
    return unauthorized("missing or invalid x-web-read-token");
  }
  const kv = requireKv(env);
  const { token, error } = await getDeviceTokenOrError(kv);
  if (error) return error;

  const { result, error: sendError } = await sendPushAndHandleStaleToken(env, kv, {
    token,
    title: "Nightwatch: refresh requested",
    body: "The web dashboard asked for a fresh snapshot.",
    data: { command: "refresh" },
    priority: "high",
  });
  if (sendError) return sendError;
  return json({ ok: true, fcm: result });
}

// --- POST /command --------------------------------------------------------
async function handleCommand(request, env) {
  if (!(await checkSecret(request, "x-web-read-token", env.WEB_READ_TOKEN))) {
    return unauthorized("missing or invalid x-web-read-token");
  }
  if (!env.COMMAND_HMAC_SECRET) {
    return json({ error: "COMMAND_HMAC_SECRET is not configured" }, { status: 500 });
  }
  const { data, tooLarge, invalidJson } = await readBodyWithLimit(request, MAX_COMMAND_BODY_BYTES);
  if (tooLarge) return json({ error: "body too large" }, { status: 413 });
  if (invalidJson) return badRequest("body is not valid JSON");

  const rawCommand = data?.command;
  if (!isAllowedCommand(rawCommand)) {
    // Reject anything outside DESIGN.md §8.2's read-only + halt/resume set
    // right here — an unknown command must never reach the phone.
    return json(
      { error: "command not allowed", allowed: [...ALLOWED_COMMANDS] },
      { status: 400 }
    );
  }
  const command = normalizeCommand(rawCommand);

  const kv = requireKv(env);
  const currentCounterRaw = await kv.get(KV_KEYS.commandCounter);
  const nextCounter = (currentCounterRaw ? parseInt(currentCounterRaw, 10) : 0) + 1;
  const hmac = await hmacCommand(env.COMMAND_HMAC_SECRET, command, nextCounter);

  const { token, error } = await getDeviceTokenOrError(kv);
  if (error) return error;

  const { result, error: sendError } = await sendPushAndHandleStaleToken(env, kv, {
    token,
    title: `Nightwatch: /${command}`,
    body: `Command "${command}" was sent from the web command channel.`,
    data: { command, counter: String(nextCounter), hmac },
    priority: "high",
  });
  if (sendError) return sendError;

  // Only persist the incremented counter once the push actually went out,
  // so a failed send doesn't burn a counter value the phone never saw.
  await kv.put(KV_KEYS.commandCounter, String(nextCounter));
  return json({ ok: true, command, counter: nextCounter, fcm: result });
}

const ROUTES = {
  "GET /health": (request, env) => handleHealth(env),
  "POST /snapshot": handleSnapshotPost,
  "GET /snapshot": handleSnapshotGet,
  "POST /device-token": handleDeviceToken,
  "POST /refresh": handleRefresh,
  "POST /command": handleCommand,
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    const handler = ROUTES[key];
    if (!handler) return json({ error: "not found" }, { status: 404 });
    try {
      return await handler(request, env, ctx);
    } catch (err) {
      // Never leak internals (stack traces, secret material) to the caller.
      console.error("unhandled error", err && err.stack ? err.stack : err);
      return json({ error: "internal error" }, { status: 500 });
    }
  },

  /**
   * Redundant daily cron trigger (DESIGN.md §7.2, §9.1, §11 step 8).
   *
   * DST TRAP, explained: Cloudflare cron triggers fire on a fixed *UTC*
   * schedule (wrangler.toml can only express UTC times). "30 minutes
   * before the US equity close" is 15:30 America/New_York, which is
   * 20:30 UTC under EST (UTC-5) but 19:30 UTC under EDT (UTC-4) — a
   * moving target from the cron string's point of view. Rather than try
   * to maintain two different cron entries and swap them twice a year (or
   * worse, hardcode one offset and silently misfire for half the year),
   * wrangler.toml schedules this handler to run every 5 minutes across a
   * UTC window wide enough to contain both the EST and EDT instants for
   * both the normal 15:30 ET target and the 12:30 ET half-day target (see
   * holidays.js), and *this function* does the real "is it actually
   * 15:30 (or 12:30) America/New_York right now" check via
   * shouldFireSessionTrigger(), which resolves the correct EST/EDT offset
   * itself using Intl.DateTimeFormat + the runtime's IANA tz database —
   * never a hand-computed offset.
   *
   * This trigger is independent of, and a cross-check on, the phone's own
   * on-device `setAlarmClock()` trigger (DESIGN.md §7.2) — it does not
   * replace it. Firing twice (once from the phone's own alarm, once from
   * this cron) is safe: the session is idempotent per trading date
   * (`sessions.trading_date` is UNIQUE, DESIGN.md §6.I), so a duplicate
   * `run_session` push just causes the phone to no-op the second time.
   * Multiple 5-minute cron ticks landing after the target on the same day
   * are also safe/deliberately deduped below via a KV flag, so at most
   * one push per trading date is sent from this path.
   */
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    const kv = requireKv(env);
    const dateKey = tradingDateKey(now);
    const firedKey = `cron_fired:${dateKey}`;
    const alreadyFired = Boolean(await kv.get(firedKey));

    if (!shouldFireSessionTrigger(now, alreadyFired)) return;

    const { token } = await getDeviceTokenOrError(kv);
    if (!token) {
      console.error(`scheduled: no device token registered, cannot send run_session push for ${dateKey}`);
      return;
    }

    try {
      await sendPush(env, {
        token,
        title: "Nightwatch: scheduled session",
        body: isHalfDay(now)
          ? "Redundant cron trigger (half-day schedule)."
          : "Redundant cron trigger (normal schedule).",
        data: { command: "run_session", trading_date: dateKey },
        priority: "high",
      });
      // Mark fired only on success, with a generous TTL (a few days) so a
      // stale flag can never accumulate forever, but comfortably outlives
      // one trading day even across a Worker restart.
      await kv.put(firedKey, "1", { expirationTtl: 60 * 60 * 24 * 3 });
    } catch (err) {
      if (err instanceof FcmSendError && isStaleTokenError(err.errorCode)) {
        await kv.delete(KV_KEYS.deviceToken);
      }
      console.error(`scheduled: FCM push failed for ${dateKey}`, err && err.stack ? err.stack : err);
      // Deliberately do not set the fired flag on failure, so the next
      // 5-minute tick retries.
    }
  },
};

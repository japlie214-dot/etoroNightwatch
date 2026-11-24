// T075: dead-phone alert. The phone-side watchdog only runs while the phone is alive, so the
// Worker (which is not the phone) checks that a session pushed a fresh snapshot, and tells the
// Boss by Telegram if not. Pure decision functions here; index.js does the I/O.
import { isTradingDay, targetFireTime, tradingDateKey } from "./holidays.js";

const MINUTES_AFTER_TARGET = 75; // 16:45 ET on a normal day, 13:45 on a half day
const WINDOW_MINUTES = 60;       // keep retrying a failed send for an hour, then give up
const FRESH_WITHIN_MINUTES = MINUTES_AFTER_TARGET + 30; // snapshot must postdate the session window start

function etMinutes(now) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit",
  }).formatToParts(now);
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  return (get("hour") % 24) * 60 + get("minute");
}

/** True on the cron ticks where a trading day's session snapshot should already have arrived. */
export function deadPhoneCheckDue(now, alreadyAlerted) {
  if (alreadyAlerted || !isTradingDay(now)) return false;
  const [h, m] = targetFireTime(now).split(":").map(Number);
  const start = h * 60 + m + MINUTES_AFTER_TARGET;
  const minutes = etMinutes(now);
  return minutes >= start && minutes < start + WINDOW_MINUTES;
}

/** A missing, unparsable or too-old snapshot means the phone did not report this session. */
export function snapshotMissedSession(updatedAt, now) {
  const t = typeof updatedAt === "string" ? Date.parse(updatedAt) : NaN;
  if (!Number.isFinite(t)) return true;
  return t < now.getTime() - FRESH_WITHIN_MINUTES * 60 * 1000;
}

export function deadPhoneMessage(now) {
  return `Nightwatch: no snapshot from the phone for ${tradingDateKey(now)}. The phone may be off, ` +
    "offline or the app stopped. Open the app on Z10.";
}

export async function sendTelegram(env, text, fetchImpl = fetch) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return "not_configured";
  const res = await fetchImpl(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
  return res.ok ? "sent" : "failed";
}

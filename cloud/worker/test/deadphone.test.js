import { describe, it, expect } from "vitest";
import { deadPhoneCheckDue, snapshotMissedSession, sendTelegram } from "../src/deadphone.js";

describe("deadPhoneCheckDue", () => {
  it("is due from 16:45 ET on a normal day (EST 21:45Z, EDT 20:45Z), not before", () => {
    expect(deadPhoneCheckDue(new Date("2026-01-20T21:40:00Z"), false)).toBe(false);
    expect(deadPhoneCheckDue(new Date("2026-01-20T21:45:00Z"), false)).toBe(true);
    expect(deadPhoneCheckDue(new Date("2026-07-20T20:40:00Z"), false)).toBe(false);
    expect(deadPhoneCheckDue(new Date("2026-07-20T20:45:00Z"), false)).toBe(true);
  });
  it("uses 13:45 ET on a half day and stops retrying after an hour", () => {
    expect(deadPhoneCheckDue(new Date("2026-11-27T18:45:00Z"), false)).toBe(true);
    expect(deadPhoneCheckDue(new Date("2026-11-27T19:50:00Z"), false)).toBe(false);
  });
  it("never on a weekend, a holiday, or once alerted", () => {
    expect(deadPhoneCheckDue(new Date("2026-01-24T21:45:00Z"), false)).toBe(false);
    expect(deadPhoneCheckDue(new Date("2026-07-03T20:45:00Z"), false)).toBe(false);
    expect(deadPhoneCheckDue(new Date("2026-01-20T21:45:00Z"), true)).toBe(false);
  });
});

describe("snapshotMissedSession", () => {
  const now = new Date("2026-01-20T21:45:00Z");
  it("flags missing, garbage and old snapshots", () => {
    expect(snapshotMissedSession(null, now)).toBe(true);
    expect(snapshotMissedSession("nope", now)).toBe(true);
    expect(snapshotMissedSession("2026-01-20T19:00:00Z", now)).toBe(true);
  });
  it("accepts a snapshot from the session window", () => {
    expect(snapshotMissedSession("2026-01-20T20:40:00Z", now)).toBe(false);
  });
});

describe("sendTelegram", () => {
  it("does nothing without both secrets", async () => {
    expect(await sendTelegram({}, "x", () => { throw new Error("no"); })).toBe("not_configured");
  });
  it("reports sent or failed from the response", async () => {
    const env = { TELEGRAM_BOT_TOKEN: "t", TELEGRAM_CHAT_ID: "c" };
    expect(await sendTelegram(env, "x", async () => ({ ok: true }))).toBe("sent");
    expect(await sendTelegram(env, "x", async () => ({ ok: false }))).toBe("failed");
  });
});

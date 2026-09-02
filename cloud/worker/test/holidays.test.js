import { describe, it, expect } from "vitest";
import { isTradingDay, isHalfDay, targetFireTime, shouldFireSessionTrigger, tradingDateKey } from "../src/holidays.js";

describe("isTradingDay / isHalfDay", () => {
  it("flags a known full holiday as not a trading day", () => {
    // Independence Day observed, 2026-07-03 (Friday)
    expect(isTradingDay(new Date("2026-07-03T18:00:00Z"))).toBe(false);
  });

  it("flags a weekend as not a trading day", () => {
    // 2026-01-24 is a Saturday
    expect(isTradingDay(new Date("2026-01-24T18:00:00Z"))).toBe(false);
  });

  it("flags an ordinary weekday as a trading day", () => {
    // 2026-01-20 is a Tuesday, not a holiday
    expect(isTradingDay(new Date("2026-01-20T18:00:00Z"))).toBe(true);
  });

  it("flags the day after Thanksgiving 2026 as a half day", () => {
    expect(isHalfDay(new Date("2026-11-27T18:00:00Z"))).toBe(true);
    expect(targetFireTime(new Date("2026-11-27T18:00:00Z"))).toBe("12:30");
  });

  it("uses the normal 15:30 target on an ordinary day", () => {
    expect(targetFireTime(new Date("2026-01-20T18:00:00Z"))).toBe("15:30");
  });
});

describe("shouldFireSessionTrigger — DST and edge cases", () => {
  it("fires at 15:30 ET on an EST date (UTC-5): 2026-01-20 20:30Z", () => {
    const before = new Date("2026-01-20T20:25:00Z");
    const atTarget = new Date("2026-01-20T20:30:00Z");
    expect(shouldFireSessionTrigger(before, false)).toBe(false);
    expect(shouldFireSessionTrigger(atTarget, false)).toBe(true);
  });

  it("fires at 15:30 ET on an EDT date (UTC-4): 2026-07-20 19:30Z", () => {
    // 2026-07-20 is a Monday, not a holiday.
    const before = new Date("2026-07-20T19:25:00Z");
    const atTarget = new Date("2026-07-20T19:30:00Z");
    expect(shouldFireSessionTrigger(before, false)).toBe(false);
    expect(shouldFireSessionTrigger(atTarget, false)).toBe(true);
  });

  it("never fires on a weekend even if wall-clock time matches", () => {
    // 2026-01-24 Saturday, 20:30Z = 15:30 ET
    expect(shouldFireSessionTrigger(new Date("2026-01-24T20:30:00Z"), false)).toBe(false);
  });

  it("never fires on a full holiday", () => {
    // 2026-07-03 Friday, observed Independence Day, 20:30Z = 15:30 ET
    expect(shouldFireSessionTrigger(new Date("2026-07-03T20:30:00Z"), false)).toBe(false);
  });

  it("fires at the half-day target (12:30 ET) instead of 15:30 on a half day", () => {
    // 2026-11-27 (day after Thanksgiving), EST => 12:30 ET = 17:30Z
    expect(shouldFireSessionTrigger(new Date("2026-11-27T17:25:00Z"), false)).toBe(false);
    expect(shouldFireSessionTrigger(new Date("2026-11-27T17:30:00Z"), false)).toBe(true);
    // The normal 15:30 ET / 20:30Z target must NOT be the trigger point on a half day —
    // by then alreadyFiredToday would be true in real operation, but even in isolation
    // the target function itself must report the half-day time, not the normal one.
    expect(targetFireTime(new Date("2026-11-27T20:30:00Z"))).toBe("12:30");
  });

  it("does not re-fire once already fired today", () => {
    expect(shouldFireSessionTrigger(new Date("2026-01-20T20:30:00Z"), true)).toBe(false);
  });
});

describe("tradingDateKey", () => {
  it("returns the ET calendar date even when UTC date differs", () => {
    // 2026-01-20T02:00Z is still 2026-01-19 21:00 ET (EST, UTC-5)
    expect(tradingDateKey(new Date("2026-01-20T02:00:00Z"))).toBe("2026-01-19");
  });
});

import { describe, it, expect } from "vitest";
import { classifyStaleness, formatAge, STALENESS_THRESHOLDS_MS } from "../public/staleness.js";

const NOW = new Date("2026-09-03T12:00:00Z");

describe("classifyStaleness", () => {
  it("is 'none' when there is no updated_at (no snapshot pushed yet)", () => {
    expect(classifyStaleness(null, NOW)).toEqual({ level: "none", ageMs: null });
    expect(classifyStaleness(undefined, NOW)).toEqual({ level: "none", ageMs: null });
  });

  it("is 'none' for an unparseable timestamp rather than crashing", () => {
    expect(classifyStaleness("not-a-date", NOW).level).toBe("none");
  });

  it("is 'fresh' (green) under 1 hour old", () => {
    const updatedAt = new Date(NOW.getTime() - 5 * 60 * 1000).toISOString();
    expect(classifyStaleness(updatedAt, NOW).level).toBe("fresh");
  });

  it("is exactly at the fresh/stale boundary -> stale (>= 1 hour is amber, not green)", () => {
    const updatedAt = new Date(NOW.getTime() - STALENESS_THRESHOLDS_MS.fresh).toISOString();
    expect(classifyStaleness(updatedAt, NOW).level).toBe("stale");
  });

  it("is 'stale' (amber) between 1 hour and 1 day old", () => {
    const updatedAt = new Date(NOW.getTime() - 5 * 60 * 60 * 1000).toISOString();
    expect(classifyStaleness(updatedAt, NOW).level).toBe("stale");
  });

  it("is 'very-stale' (red) beyond 1 day old", () => {
    const updatedAt = new Date(NOW.getTime() - 25 * 60 * 60 * 1000).toISOString();
    expect(classifyStaleness(updatedAt, NOW).level).toBe("very-stale");
  });

  it("treats a future timestamp (clock skew) as fresh, not negative age", () => {
    const updatedAt = new Date(NOW.getTime() + 60 * 1000).toISOString();
    const { level, ageMs } = classifyStaleness(updatedAt, NOW);
    expect(level).toBe("fresh");
    expect(ageMs).toBe(0);
  });
});

describe("formatAge", () => {
  it("handles null/undefined", () => {
    expect(formatAge(null)).toBe("unknown");
    expect(formatAge(undefined)).toBe("unknown");
  });

  it("formats seconds, minutes, hours, days", () => {
    expect(formatAge(30 * 1000)).toBe("30s ago");
    expect(formatAge(5 * 60 * 1000)).toBe("5m ago");
    expect(formatAge(3 * 60 * 60 * 1000)).toBe("3h ago");
    expect(formatAge(2 * 24 * 60 * 60 * 1000)).toBe("2d ago");
  });
});

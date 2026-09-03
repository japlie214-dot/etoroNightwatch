import { describe, it, expect } from "vitest";
import { ALLOWED_COMMANDS, isAllowedCommand, normalizeCommand, hmacCommand } from "../src/commands.js";

describe("command allowlist", () => {
  it("matches the read-only + halt/resume set from DESIGN.md §8.2", () => {
    expect([...ALLOWED_COMMANDS].sort()).toEqual(
      // Must stay in step with SUPPORTED_COMMANDS in
      // app/src/main/python/nightwatch/notify/commands.py and DESIGN.md §8.2.
      [
        // Trading/reporting commands (DESIGN 8.2)
        "halt", "help", "logs", "next", "pnl", "positions", "resume", "status", "today",
        // Probe/diagnostic commands (android-probe v2)
        "ping", "runchecks", "schedule", "selftest",
      ].sort()
    );
  });

  it("accepts commands with or without a leading slash, case-insensitively", () => {
    expect(isAllowedCommand("status")).toBe(true);
    expect(isAllowedCommand("/status")).toBe(true);
    expect(isAllowedCommand("STATUS")).toBe(true);
    expect(isAllowedCommand(" /Status ")).toBe(true);
  });

  it("rejects anything outside the allowed set, including dangerous-sounding ones", () => {
    for (const bad of ["arm", "trade", "buy", "config", "shell", "rm -rf /", "", null, undefined, 123]) {
      expect(isAllowedCommand(bad)).toBe(false);
    }
  });

  it("normalizeCommand strips slash and lowercases", () => {
    expect(normalizeCommand("/Halt")).toBe("halt");
  });
});

describe("hmacCommand", () => {
  it("produces a deterministic hex digest for the same inputs", async () => {
    const a = await hmacCommand("secret-1", "status", 1);
    const b = await hmacCommand("secret-1", "status", 1);
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });

  it("changes when the counter changes (replay protection input)", async () => {
    const a = await hmacCommand("secret-1", "status", 1);
    const b = await hmacCommand("secret-1", "status", 2);
    expect(a).not.toBe(b);
  });

  it("changes when the secret changes", async () => {
    const a = await hmacCommand("secret-1", "status", 1);
    const b = await hmacCommand("secret-2", "status", 1);
    expect(a).not.toBe(b);
  });
});

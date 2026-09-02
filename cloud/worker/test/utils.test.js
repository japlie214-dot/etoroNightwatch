import { describe, it, expect } from "vitest";
import { timingSafeEqualStrings, checkSecret, base64UrlEncodeString } from "../src/utils.js";

describe("timingSafeEqualStrings", () => {
  it("returns true for identical strings", async () => {
    expect(await timingSafeEqualStrings("abc123", "abc123")).toBe(true);
  });

  it("returns false for different strings of the same length", async () => {
    expect(await timingSafeEqualStrings("abc123", "abc124")).toBe(false);
  });

  it("returns false for different-length strings", async () => {
    expect(await timingSafeEqualStrings("short", "a-lot-longer-string")).toBe(false);
  });

  it("returns false for non-string inputs", async () => {
    expect(await timingSafeEqualStrings(null, "abc")).toBe(false);
    expect(await timingSafeEqualStrings(undefined, undefined)).toBe(false);
  });
});

describe("checkSecret", () => {
  function fakeRequest(headers) {
    return { headers: new Headers(headers) };
  }

  it("rejects when expected secret is not configured", async () => {
    const req = fakeRequest({ "x-test": "anything" });
    expect(await checkSecret(req, "x-test", undefined)).toBe(false);
    expect(await checkSecret(req, "x-test", "")).toBe(false);
  });

  it("rejects when header missing", async () => {
    const req = fakeRequest({});
    expect(await checkSecret(req, "x-test", "expected")).toBe(false);
  });

  it("accepts a matching header", async () => {
    const req = fakeRequest({ "x-test": "expected" });
    expect(await checkSecret(req, "x-test", "expected")).toBe(true);
  });

  it("rejects a mismatching header", async () => {
    const req = fakeRequest({ "x-test": "wrong" });
    expect(await checkSecret(req, "x-test", "expected")).toBe(false);
  });
});

describe("base64UrlEncodeString", () => {
  it("produces URL-safe base64 with no padding", () => {
    const encoded = base64UrlEncodeString("hello?world>>>");
    expect(encoded).not.toMatch(/[+/=]/);
  });
});

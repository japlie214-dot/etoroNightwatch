import { describe, it, expect, vi } from "vitest";
import { proxyToWorker, resolveConfig, ALLOWED_WORKER_PATHS, DEFAULT_WORKER_BASE_URL } from "../functions/_shared/proxy.js";
import { onRequestGet as snapshotGet } from "../functions/api/snapshot.js";
import { onRequestPost as refreshPost } from "../functions/api/refresh.js";

function fakeUpstream(status, body, headers = { "content-type": "application/json; charset=utf-8" }) {
  return async (url, init) => {
    fakeUpstream.lastUrl = url;
    fakeUpstream.lastInit = init;
    return new Response(JSON.stringify(body), { status, headers });
  };
}

describe("resolveConfig", () => {
  it("reports WEB_READ_TOKEN missing and falls back to the default worker URL", () => {
    const { workerBaseUrl, missing } = resolveConfig({});
    expect(workerBaseUrl).toBe(DEFAULT_WORKER_BASE_URL);
    expect(missing).toEqual(["WEB_READ_TOKEN"]);
  });

  it("reports nothing missing once WEB_READ_TOKEN is set", () => {
    const { missing } = resolveConfig({ WEB_READ_TOKEN: "t" });
    expect(missing).toEqual([]);
  });

  it("honors an explicit WORKER_BASE_URL override", () => {
    const { workerBaseUrl } = resolveConfig({ WEB_READ_TOKEN: "t", WORKER_BASE_URL: "https://example.test" });
    expect(workerBaseUrl).toBe("https://example.test");
  });
});

describe("proxyToWorker: header injection and token secrecy", () => {
  it("injects x-web-read-token server-side on the outgoing request", async () => {
    const fetchImpl = fakeUpstream(200, { snapshot: { foo: "bar" }, updated_at: "2026-01-01T00:00:00Z" });
    const env = { WEB_READ_TOKEN: "super-secret-token", WORKER_BASE_URL: "https://worker.example" };

    const res = await proxyToWorker({ env, path: "/snapshot", method: "GET", fetchImpl });

    expect(fakeUpstream.lastUrl).toBe("https://worker.example/snapshot");
    expect(fakeUpstream.lastInit.headers["x-web-read-token"]).toBe("super-secret-token");
    expect(res.status).toBe(200);
  });

  it("never leaks the token into the response body or headers sent back to the caller", async () => {
    const fetchImpl = fakeUpstream(200, { snapshot: { equity: 1 }, updated_at: "2026-01-01T00:00:00Z" });
    const env = { WEB_READ_TOKEN: "super-secret-token", WORKER_BASE_URL: "https://worker.example" };

    const res = await proxyToWorker({ env, path: "/snapshot", method: "GET", fetchImpl });
    const text = await res.text();
    const headerDump = JSON.stringify([...res.headers.entries()]);

    expect(text).not.toContain("super-secret-token");
    expect(headerDump).not.toContain("super-secret-token");
  });

  it("passes through the upstream status and body unchanged (e.g. a 409 no-device-token)", async () => {
    const fetchImpl = fakeUpstream(409, { error: "no device token registered yet (phone has not called POST /device-token)" });
    const env = { WEB_READ_TOKEN: "t", WORKER_BASE_URL: "https://worker.example" };

    const res = await proxyToWorker({ env, path: "/refresh", method: "POST", fetchImpl });
    const body = await res.json();

    expect(res.status).toBe(409);
    expect(body.error).toMatch(/no device token/);
  });
});

describe("proxyToWorker: path allowlist", () => {
  it("has exactly the two intended worker paths allowlisted", () => {
    expect([...ALLOWED_WORKER_PATHS].sort()).toEqual(["/refresh", "/snapshot"]);
  });

  it("rejects any path outside the allowlist without calling fetch", async () => {
    const fetchImpl = vi.fn();
    const env = { WEB_READ_TOKEN: "t", WORKER_BASE_URL: "https://worker.example" };

    const res = await proxyToWorker({ env, path: "/device-token", method: "POST", fetchImpl });

    expect(res.status).toBe(400);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects an arbitrary/unexpected path (open-proxy guard)", async () => {
    const fetchImpl = vi.fn();
    const env = { WEB_READ_TOKEN: "t" };

    const res = await proxyToWorker({ env, path: "/../secrets", method: "GET", fetchImpl });

    expect(res.status).toBe(400);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("proxyToWorker: unconfigured secret behaviour", () => {
  it("returns a structured 500 naming what's missing, without ever calling fetch", async () => {
    const fetchImpl = vi.fn();
    const res = await proxyToWorker({ env: {}, path: "/snapshot", method: "GET", fetchImpl });
    const body = await res.json();

    expect(res.status).toBe(500);
    expect(body.missing).toEqual(["WEB_READ_TOKEN"]);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("WEB_READ_TOKEN alone is sufficient config (WORKER_BASE_URL has a default) and never echoes the token", async () => {
    const fetchImpl = fakeUpstream(200, { snapshot: null, updated_at: null });
    const res = await proxyToWorker({
      env: { WEB_READ_TOKEN: "super-secret-token" },
      path: "/snapshot",
      method: "GET",
      fetchImpl,
    });
    expect(res.status).toBe(200);
    expect(fakeUpstream.lastUrl).toBe(`${DEFAULT_WORKER_BASE_URL}/snapshot`);
    const text = await res.text();
    expect(text).not.toContain("super-secret-token");
  });
});

describe("functions/api/snapshot.js and functions/api/refresh.js route wiring", () => {
  it("snapshot.js onRequestGet forwards to GET /snapshot", async () => {
    const fetchImpl = fakeUpstream(200, { snapshot: null, updated_at: null });
    const originalFetch = globalThis.fetch;
    globalThis.fetch = fetchImpl;
    try {
      const res = await snapshotGet({ env: { WEB_READ_TOKEN: "t", WORKER_BASE_URL: "https://worker.example" } });
      expect(res.status).toBe(200);
      expect(fakeUpstream.lastUrl).toBe("https://worker.example/snapshot");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("refresh.js onRequestPost forwards to POST /refresh", async () => {
    const fetchImpl = fakeUpstream(200, { ok: true, fcm: {} });
    const originalFetch = globalThis.fetch;
    globalThis.fetch = fetchImpl;
    try {
      const res = await refreshPost({ env: { WEB_READ_TOKEN: "t", WORKER_BASE_URL: "https://worker.example" } });
      expect(res.status).toBe(200);
      expect(fakeUpstream.lastUrl).toBe("https://worker.example/refresh");
      expect(fakeUpstream.lastInit.method).toBe("POST");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

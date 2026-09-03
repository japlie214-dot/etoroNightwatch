// GET /api/snapshot -> Worker GET /snapshot, with x-web-read-token injected
// server-side (see ../_shared/proxy.js). The browser never sees the token.
import { proxyToWorker } from "../_shared/proxy.js";

export async function onRequestGet({ env }) {
  return proxyToWorker({ env, path: "/snapshot", method: "GET" });
}

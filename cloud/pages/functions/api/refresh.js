// POST /api/refresh -> Worker POST /refresh, with x-web-read-token injected
// server-side (see ../_shared/proxy.js). The Worker fires an FCM push at
// the phone; until a real device is registered, the Worker replies 409
// ("no device token registered yet") which is passed straight through for
// the UI to render as the expected current state, not an error.
import { proxyToWorker } from "../_shared/proxy.js";

export async function onRequestPost({ env }) {
  return proxyToWorker({ env, path: "/refresh", method: "POST" });
}

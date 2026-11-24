// DEMO_CONTROL_AUTH: verifies (a) a Cloudflare Access JWT + operator allowlist for browser
// calls, or (b) the phone's RS256 receipt signature for POST /demo-control/ack. It never
// trusts identity headers, query strings or JSON flags, and fails closed (401/403/503).
import { createDeviceReceiptVerifier } from "../../worker/src/demo-control-device-verifier.js";
import { base64UrlDecodeToBytes, json } from "../../worker/src/utils.js";

export const ACTIONS = ["refresh_demo_broker", "pause_demo_orders"];
const reply = (body, status) => json(body, { status, headers: { "cache-control": "no-store" } });
const CERT_TTL_MS = 10 * 60 * 1000;
let certCache = { at: 0, domain: "", keys: null };

async function accessKeys(env, fetchImpl, now) {
  if (certCache.keys && certCache.domain === env.ACCESS_TEAM_DOMAIN && now - certCache.at < CERT_TTL_MS) {
    return certCache.keys;
  }
  const res = await fetchImpl(`https://${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error("certs unavailable");
  const { keys } = await res.json();
  if (!Array.isArray(keys) || keys.length === 0) throw new Error("certs unavailable");
  certCache = { at: now, domain: env.ACCESS_TEAM_DOMAIN, keys };
  return keys;
}

export class Denied extends Error {}

/** Returns the verified Access email (lowercase) or throws Denied. */
export async function verifyAccessJwt(token, env, { now = Date.now(), fetchImpl = fetch } = {}) {
  const parts = typeof token === "string" ? token.split(".") : [];
  if (parts.length !== 3) throw new Denied();
  let header, claims;
  try {
    header = JSON.parse(new TextDecoder().decode(base64UrlDecodeToBytes(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(base64UrlDecodeToBytes(parts[1])));
  } catch { throw new Denied(); }
  if (header.alg !== "RS256" || typeof header.kid !== "string") throw new Denied();
  const jwk = (await accessKeys(env, fetchImpl, now)).find(k => k.kid === header.kid && k.kty === "RSA");
  if (!jwk) throw new Denied();
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64UrlDecodeToBytes(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  const seconds = Math.floor(now / 1000);
  if (!ok || !env.ACCESS_AUD || !aud.includes(env.ACCESS_AUD) ||
      claims.iss !== `https://${env.ACCESS_TEAM_DOMAIN}` ||
      !Number.isFinite(claims.exp) || claims.exp <= seconds ||
      (claims.nbf !== undefined && !(claims.nbf <= seconds + 60)) ||
      typeof claims.email !== "string") throw new Denied();
  return claims.email.toLowerCase();
}

function operators(env) {
  return String(env.OPERATOR_EMAILS || "").split(",").map(s => s.trim().toLowerCase())
    .filter(s => s && !s.startsWith("replace_with"));
}

async function devicePins(env) {
  const spki = String(env.DEVICE_PUBKEY_SPKI || "");
  if (!spki || spki.startsWith("REPLACE_WITH")) return new Map();
  const publicKey = await crypto.subtle.importKey("spki", Uint8Array.from(atob(spki), c => c.charCodeAt(0)),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  return new Map([[env.DEVICE_KEY_ID, { publicKey, deviceId: env.DEVICE_ID, environment: "demo",
    capabilities: [...ACTIONS], revoked: env.DEVICE_KEY_REVOKED === "true" }]]);
}

export function createAuth({ now = Date.now, fetchImpl = fetch } = {}) {
  return {
    async fetch(request, env) {
      try {
        const url = new URL(request.url);
        if (url.pathname === "/demo-control/ack") {
          return await createDeviceReceiptVerifier({ pins: await devicePins(env), now }).fetch(request);
        }
        const token = request.headers.get("cf-access-jwt-assertion");
        if (!token) return reply({ error: "authentication required" }, 401);
        let email;
        try { email = await verifyAccessJwt(token, env, { now: now(), fetchImpl }); }
        catch (error) {
          return error instanceof Denied ? reply({ error: "forbidden" }, 403) : reply({ error: "unavailable" }, 503);
        }
        if (!operators(env).includes(email)) return reply({ error: "forbidden" }, 403);
        return reply({ deviceId: env.DEVICE_ID, environment: "demo", capabilities: [...ACTIONS],
          operatorReadAuthorized: true, operatorWriteAuthorized: true }, 200);
      } catch { return reply({ error: "unavailable" }, 503); }
    },
  };
}

export default createAuth();

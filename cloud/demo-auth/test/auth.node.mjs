import test from "node:test";
import assert from "node:assert/strict";
import { createAuth } from "../src/index.js";
import { base64UrlEncode } from "../../worker/src/utils.js";

const TEAM = "team.example.cloudflareaccess.com";
const AUD = "synthetic-aud";
const env = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, DEVICE_ID: "z10-demo", DEVICE_KEY_ID: "k1",
  DEVICE_PUBKEY_SPKI: "REPLACE_WITH_PUBLIC_SPKI", OPERATOR_EMAILS: "Boss@example.com" };
const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid: "kid1" };
const fetchImpl = async () => new Response(JSON.stringify({ keys: [jwk] }));
const enc = (o) => base64UrlEncode(new TextEncoder().encode(JSON.stringify(o)));
async function token(claims, { key = pair.privateKey, kid = "kid1" } = {}) {
  const head = enc({ alg: "RS256", kid });
  const body = enc({ aud: [AUD], iss: `https://${TEAM}`, exp: 2000, email: "boss@example.com", ...claims });
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${base64UrlEncode(sig)}`;
}
const call = async (jwt, e = env) => {
  const headers = jwt ? { "cf-access-jwt-assertion": jwt } : {};
  return createAuth({ now: () => 1_000_000, fetchImpl }).fetch(new Request("https://p.dev/demo-control", { headers }), e);
};

test("valid Access JWT from an allowed operator is granted read and write", async () => {
  const res = await call(await token({}));
  assert.equal(res.status, 200);
  const a = await res.json();
  assert.equal(a.operatorWriteAuthorized, true);
  assert.equal(a.operatorReadAuthorized, true);
  assert.equal(a.environment, "demo");
  assert.equal(a.deviceId, "z10-demo");
  assert.deepEqual(a.capabilities, ["refresh_demo_broker", "pause_demo_orders"]);
});

test("missing token is 401; every other defect is 403 and grants nothing", async () => {
  assert.equal((await call(null)).status, 401);
  const other = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const cases = [
    await token({ aud: ["other"] }), await token({ iss: "https://evil.example" }), await token({ exp: 900 }),
    await token({ email: "stranger@example.com" }), await token({ email: undefined }),
    await token({}, { key: other.privateKey }), await token({}, { kid: "nope" }), "garbage", "a.b.c",
  ];
  for (const t of cases) assert.equal((await call(t)).status, 403);
});

test("an empty or placeholder operator list denies everyone; no ACL means no write", async () => {
  for (const operators of ["", "REPLACE_WITH_OPERATOR_EMAIL", undefined]) {
    assert.equal((await call(await token({}), { ...env, OPERATOR_EMAILS: operators })).status, 403);
  }
});

test("cert endpoint failure is unavailable, never allowed", async () => {
  const res = await createAuth({ now: () => 1_000_000, fetchImpl: async () => new Response("no", { status: 500 }) })
    .fetch(new Request("https://p.dev/", { headers: { "cf-access-jwt-assertion": await token({}) } }),
      { ...env, ACCESS_TEAM_DOMAIN: "fresh.example.cloudflareaccess.com", ACCESS_AUD: AUD });
  assert.equal(res.status, 503);
});

test("ack without a pinned device key is unavailable (503), not accepted", async () => {
  const res = await createAuth({ now: () => 1_000_000, fetchImpl }).fetch(new Request("https://w.dev/demo-control/ack", {
    method: "POST", headers: { "content-type": "application/json" }, body: "{}" }), env);
  assert.equal(res.status, 503);
});

// Small shared helpers. No framework — kept deliberately minimal per the
// "no build step beyond wrangler" instruction.

/** JSON response helper. */
export function json(body, init = {}) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "content-type": "application/json; charset=utf-8", ...(init.headers || {}) },
  });
}

export function unauthorized(reason = "unauthorized") {
  return json({ error: reason }, { status: 401 });
}

export function badRequest(reason = "bad request") {
  return json({ error: reason }, { status: 400 });
}

/**
 * Constant-time string comparison for Workers.
 *
 * Cloudflare exposes a non-standard `crypto.subtle.timingSafeEqual`, but it
 * (a) requires equal-length buffers or it throws, which itself leaks a
 * length signal if you don't guard it, and (b) isn't available outside the
 * Workers runtime, so it would not work under plain-Node vitest. Instead we
 * hash both inputs to a fixed-length SHA-256 digest first (this removes any
 * length or content dependence on comparison time from the *inputs*) and
 * then compare the two digests with a branchless XOR-accumulate loop that
 * always inspects all 32 bytes. This works identically under the Workers
 * runtime and under Node's Web Crypto implementation, so the same code path
 * is exercised in tests and in production.
 *
 * Reference: https://developers.cloudflare.com/workers/examples/protect-against-timing-attacks/
 */
export async function timingSafeEqualStrings(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const va = new Uint8Array(da);
  const vb = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < va.length; i++) diff |= va[i] ^ vb[i];
  // Also fold in a comparison of the *original* lengths, but do so via a
  // constant-time-ish accumulation rather than an early-return `if`, so a
  // length mismatch doesn't short-circuit before the digest loop above.
  diff |= a.length === b.length ? 0 : 1;
  return diff === 0;
}

/** Checks a bearer/shared-secret header against an expected value from env. constant-time. */
export async function checkSecret(request, headerName, expected) {
  if (!expected) return false; // secret not configured -> never authorizes
  const provided = request.headers.get(headerName);
  if (!provided) return false;
  return timingSafeEqualStrings(provided, expected);
}

export function base64UrlEncode(bytes) {
  let binary = "";
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < view.length; i++) binary += String.fromCharCode(view[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function base64UrlEncodeString(str) {
  return base64UrlEncode(new TextEncoder().encode(str));
}

export function base64UrlDecodeToBytes(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64url.length + 3) % 4);
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** ISO 8601 UTC timestamp, second precision. */
export function nowIso() {
  return new Date().toISOString();
}

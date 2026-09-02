// Signs a Google service-account JWT assertion for the OAuth2
// jwt-bearer flow, using the Web Crypto API only (no Node `crypto`
// module — Workers don't have it, and we want the exact same code path
// under `wrangler dev`, production, and vitest).
//
// Claim shape and token endpoint verified against Google's own docs:
// https://developers.google.com/identity/protocols/oauth2/service-account
//   iss   = service account client_email
//   scope = space-delimited OAuth scopes requested
//   aud   = "https://oauth2.googleapis.com/token" (fixed)
//   iat   = issued-at, seconds since epoch
//   exp   = expiry, seconds since epoch, max iat+3600
//
// The signed assertion is later POSTed to the token endpoint as
// grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt>
// (see fcm.js) to obtain a bearer access token for FCM HTTP v1.

import { base64UrlEncodeString, base64UrlEncode } from "./utils.js";

const TOKEN_URI = "https://oauth2.googleapis.com/token";
const JWT_LIFETIME_SECONDS = 3600; // Google's documented maximum

/**
 * Parses a PEM-encoded PKCS#8 private key (as found in a downloaded
 * service-account JSON's `private_key` field) into a CryptoKey usable
 * with RSASSA-PKCS1-v1_5 / SHA-256, which is what Google's JWT signing
 * requires (`alg: RS256`).
 */
async function importPrivateKey(pem) {
  const stripped = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  // The PEM body is standard base64 (uses '+' and '/'), not base64url —
  // decode it as plain base64 into raw DER bytes for importKey.
  return crypto.subtle.importKey(
    "pkcs8",
    derFromStandardBase64(stripped),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
}

function derFromStandardBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

/**
 * Builds and signs a service-account JWT assertion.
 * @param {{client_email: string, private_key: string}} serviceAccount
 * @param {string} scope space-delimited OAuth scope string
 * @param {number} [nowSeconds] injectable for tests
 */
export async function signServiceAccountJwt(serviceAccount, scope, nowSeconds) {
  if (!serviceAccount || !serviceAccount.client_email || !serviceAccount.private_key) {
    throw new Error("service account JSON missing client_email/private_key");
  }
  const iat = nowSeconds ?? Math.floor(Date.now() / 1000);
  const exp = iat + JWT_LIFETIME_SECONDS;

  const header = { alg: "RS256", typ: "JWT" };
  const claims = {
    iss: serviceAccount.client_email,
    scope,
    aud: TOKEN_URI,
    iat,
    exp,
  };

  const encodedHeader = base64UrlEncodeString(JSON.stringify(header));
  const encodedClaims = base64UrlEncodeString(JSON.stringify(claims));
  const signingInput = `${encodedHeader}.${encodedClaims}`;

  const key = await importPrivateKey(serviceAccount.private_key);
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(signingInput)
  );

  return `${signingInput}.${base64UrlEncode(new Uint8Array(signature))}`;
}

export { TOKEN_URI };

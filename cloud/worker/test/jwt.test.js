import { describe, it, expect, beforeAll } from "vitest";
import { signServiceAccountJwt } from "../src/jwt.js";

function toPem(base64, label) {
  const lines = base64.match(/.{1,64}/g).join("\n");
  return `-----BEGIN ${label}-----\n${lines}\n-----END ${label}-----\n`;
}

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function b64urlToJson(seg) {
  const b64 = seg.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((seg.length + 3) % 4);
  return JSON.parse(atob(b64));
}

describe("signServiceAccountJwt", () => {
  let keyPair;
  let privateKeyPem;

  beforeAll(async () => {
    // A throwaway RSA keypair generated just for this test — never a real
    // credential, so it's fine to generate one at test time.
    keyPair = await crypto.subtle.generateKey(
      { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
      true,
      ["sign", "verify"]
    );
    const pkcs8 = await crypto.subtle.exportKey("pkcs8", keyPair.privateKey);
    privateKeyPem = toPem(bufToBase64(pkcs8), "PRIVATE KEY");
  });

  it("produces a well-formed three-part JWT with correct header and claims", async () => {
    const serviceAccount = { client_email: "nightwatch@test-project.iam.gserviceaccount.com", private_key: privateKeyPem };
    const nowSeconds = 1_700_000_000;
    const jwt = await signServiceAccountJwt(serviceAccount, "https://www.googleapis.com/auth/firebase.messaging", nowSeconds);

    const parts = jwt.split(".");
    expect(parts).toHaveLength(3);

    const header = b64urlToJson(parts[0]);
    expect(header).toEqual({ alg: "RS256", typ: "JWT" });

    const claims = b64urlToJson(parts[1]);
    expect(claims.iss).toBe("nightwatch@test-project.iam.gserviceaccount.com");
    expect(claims.scope).toBe("https://www.googleapis.com/auth/firebase.messaging");
    expect(claims.aud).toBe("https://oauth2.googleapis.com/token");
    expect(claims.iat).toBe(nowSeconds);
    expect(claims.exp).toBe(nowSeconds + 3600); // Google's documented max lifetime
  });

  it("produces a signature verifiable with the matching public key", async () => {
    const serviceAccount = { client_email: "nightwatch@test-project.iam.gserviceaccount.com", private_key: privateKeyPem };
    const jwt = await signServiceAccountJwt(serviceAccount, "scope-x");
    const [headerSeg, claimsSeg, sigSeg] = jwt.split(".");

    const sigBytes = Uint8Array.from(atob(sigSeg.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((sigSeg.length + 3) % 4)), (c) =>
      c.charCodeAt(0)
    );
    const signingInput = new TextEncoder().encode(`${headerSeg}.${claimsSeg}`);

    const isValid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", keyPair.publicKey, sigBytes, signingInput);
    expect(isValid).toBe(true);
  });

  it("rejects a service account missing required fields", async () => {
    await expect(signServiceAccountJwt({}, "scope")).rejects.toThrow(/client_email|private_key/);
  });
});

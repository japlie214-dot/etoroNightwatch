import { json } from "./_shared/proxy.js";

// Fail closed for assets and APIs, including Pages aliases/SPA fallbacks. The verifier
// must validate a signed visitor session/JWT and operator ACL, never an identity header.
export async function onRequest({ request, env, next }) {
  const deny = (error, status) => json({ error }, { status, headers: { "cache-control": "no-store" } });
  if (!env.DEMO_CONTROL_AUTH?.fetch) return deny("protected monitor unavailable", 503);
  try {
    const response = await env.DEMO_CONTROL_AUTH.fetch(request.clone());
    if ([401, 403].includes(response.status)) return deny("operator authentication required", 403);
    if (!response.ok) return deny("protected monitor unavailable", 503);
    const authority = await response.json();
    if (authority.operatorReadAuthorized !== true && authority.operatorWriteAuthorized !== true) {
      return deny("operator authentication required", 403);
    }
    const result = await next();
    const headers = new Headers(result.headers);
    headers.set("cache-control", "no-store");
    return new Response(result.body, { status: result.status, statusText: result.statusText, headers });
  } catch { return deny("protected monitor unavailable", 503); }
}

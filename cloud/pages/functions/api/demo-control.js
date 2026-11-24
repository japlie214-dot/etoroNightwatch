import { json } from "../_shared/proxy.js";

// Only this dedicated service binding; never forward browser-selected paths or read tokens.
export async function onRequest({ request, env }) {
  const reply = (body, status) => json(body, { status, headers: { "cache-control": "no-store" } });
  if (!["GET", "POST"].includes(request.method)) return reply({ error: "method not allowed" }, 405);
  if (request.method === "POST" && (request.headers.get("origin") !== new URL(request.url).origin ||
      request.headers.get("sec-fetch-site") !== "same-origin" ||
      request.headers.get("content-type")?.split(";")[0].trim() !== "application/json")) {
    return reply({ error: "same-origin JSON request required" }, 403);
  }
  if (!env.DEMO_CONTROL_WORKER?.fetch) return reply({ error: "demo control unavailable" }, 503);
  const url = new URL(request.url);
  url.pathname = "/demo-control";
  try {
    const upstream = await env.DEMO_CONTROL_WORKER.fetch(new Request(url, request));
    return new Response(upstream.body, { status: upstream.status,
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
  } catch { return reply({ error: "demo control unavailable" }, 503); }
}

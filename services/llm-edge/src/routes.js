// Route authorization for llm-edge.
//
// The edge injects the platform's gateway key into whatever it forwards, so
// the set of forwardable requests is the set of things a session may do with
// that key. Only inference-shaped routes qualify. Anything else a gateway
// happens to expose under the same key — LiteLLM's /key/*, /user/*, /spend/*,
// /model/* management API, OpenAI-compatible path-routed model endpoints
// that would sidestep the per-session model allowlist — is refused here, so
// the key's privilege never has to be relied on as the boundary.
//
// Kept free of imports so it can be unit-tested without the AWS SDK.

const ROUTES = new Map([
  // Anthropic Messages API: the only place inference is requested. A body is
  // mandatory so the model check in server.js always has something to check.
  ["POST /v1/messages", { body: "required" }],
  ["POST /v1/messages/count_tokens", { body: "required" }],
  // Model discovery: bodyless, read-only.
  ["GET /v1/models", { body: "forbidden" }],
]);

// Normalize the request target and decide whether it may be forwarded.
// Returns {ok: true, pathname, search, body} or {ok: false, status, message}.
export function authorizeRoute(method, rawUrl) {
  let url;
  try {
    // A relative-form target is the only shape a proxy should ever see; a
    // base is needed for parsing and is discarded. Absolute-form targets
    // ("GET http://other-host/…") would carry their own host — refuse them.
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(String(rawUrl))) throw new Error("absolute target");
    url = new URL(String(rawUrl), "http://edge.invalid");
  } catch {
    return { ok: false, status: 400, message: "malformed request target" };
  }
  // Compare the path as sent, not after normalization: WHATWG parsing already
  // collapses "." / ".." segments, so anything that changed under it (or that
  // encodes a slash) was trying to be something other than what it says.
  const rawPath = String(rawUrl).split(/[?#]/)[0];
  if (rawPath !== url.pathname || /%2f/i.test(rawPath) || rawPath.includes("//")) {
    return { ok: false, status: 400, message: "request path is not in canonical form" };
  }
  const rule = ROUTES.get(`${String(method || "").toUpperCase()} ${url.pathname}`);
  if (!rule) {
    return { ok: false, status: 404, message: `${method} ${url.pathname} is not served by this endpoint` };
  }
  return { ok: true, pathname: url.pathname, search: url.search, body: rule.body };
}

// Request headers forwarded upstream. Everything else the client sent is
// dropped rather than relayed under the platform key: hop-by-hop headers,
// its own credentials, and any gateway-specific control header it may have
// learnt about. Exact names plus two prefixes the Anthropic SDKs use.
const FORWARD_HEADERS = new Set([
  "content-type",
  "accept",
  "user-agent",
  "anthropic-version",
  "anthropic-beta",
  "x-app",
]);
const FORWARD_PREFIXES = ["x-stainless-", "anthropic-"];

export function forwardableHeader(name) {
  const n = String(name).toLowerCase();
  if (FORWARD_HEADERS.has(n)) return true;
  return FORWARD_PREFIXES.some((p) => n.startsWith(p));
}

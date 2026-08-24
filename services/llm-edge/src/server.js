// llm-edge: the platform-side hop that holds the LLM gateway key.
//
// Why this service exists
// -----------------------
// A Dev Workbench session hands the user a real terminal, as root, inside the
// session's microVM. Anything present in that container is reachable: process
// environment, files, process memory, and the execution role's credentials
// from the metadata endpoint. The platform already applies that reasoning to
// workspace S3 access (see backend/app/services/workspace_credentials_service.py)
// — the kernel role has no workspaces/* grant, and the backend mints
// session-scoped credentials instead.
//
// The LLM gateway key used to be the exception: the kernel read it from
// Secrets Manager at startup and exported it as ANTHROPIC_AUTH_TOKEN, so
// `env` was enough to walk away with a long-lived, platform-wide credential.
// This service closes that: the key lives only in this task's role, and a
// session presents a short-lived, session-scoped token that is worthless
// anywhere else — the listener in front of this service is internal to the
// VPC and has no public route.
//
// What this service does per request
//   1. Authenticate the caller as a live session (token hash, constant-time).
//   2. Re-read that session's entitlements SERVER-SIDE. Nothing the container
//      sends about routing is trusted: upstream base URL, the gateway secret
//      name and the permitted model list all come from the token item the
//      backend wrote.
//   3. Inject the real gateway key and stream the response straight through.
//   4. Emit a structured usage line for cost attribution.
//
// Implementation note: raw node:http on purpose. Express/Fastify response
// pipelines buffer, and compression middleware buffers by definition — either
// one turns token-by-token SSE into a single blob delivered at the end.

import http from "node:http";
import https from "node:https";
import crypto from "node:crypto";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand } from "@aws-sdk/lib-dynamodb";
import {
  SecretsManagerClient,
  GetSecretValueCommand,
} from "@aws-sdk/client-secrets-manager";

const PORT = Number(process.env.PORT || 8080);
const REGION = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "us-east-1";
const TABLE = process.env.PLATFORM_TABLE;
// Partition holding one item per live session, written by the backend when it
// mints the session's token. Mirrors the WSTOKEN layout so the lookup is a
// get_item: this table is shared (sessions, channels, ledger, audit), and a
// filtered scan reads one 1 MB page of *unfiltered* data, so past that size
// the matching session silently stops being found.
const TOKEN_PK = "LLMTOKEN";

// A request body has to be buffered to validate `model` before anything is
// forwarded. Cap it so a malformed or hostile body can't exhaust the task.
const MAX_BODY_BYTES = 32 * 1024 * 1024;
// Enough to hold the final SSE frames, which is where usage totals arrive.
const USAGE_TAIL_BYTES = 8192;
// Re-reading the secret on every request would put Secrets Manager in the hot
// path of every model call; re-reading it periodically keeps rotation working
// without that cost.
const SECRET_TTL_MS = 5 * 60 * 1000;

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
const sm = new SecretsManagerClient({ region: REGION });

const secretCache = new Map(); // secretName -> { key, fetchedAt }

function log(fields) {
  process.stdout.write(JSON.stringify({ ts: new Date().toISOString(), ...fields }) + "\n");
}

function sha256(v) {
  return crypto.createHash("sha256").update(v, "utf8").digest("hex");
}

// Constant-time compare that also tolerates length mismatch (timingSafeEqual
// throws on differing lengths, which would itself leak length).
function sameDigest(a, b) {
  const ba = Buffer.from(String(a), "utf8");
  const bb = Buffer.from(String(b), "utf8");
  if (ba.length !== bb.length) return false;
  return crypto.timingSafeEqual(ba, bb);
}

async function loadSession(sessionId, token) {
  if (!sessionId || !token) return null;
  let item;
  try {
    const resp = await ddb.send(
      new GetCommand({ TableName: TABLE, Key: { PK: TOKEN_PK, SK: `RSID#${sessionId}` } }),
    );
    item = resp.Item;
  } catch (e) {
    log({ level: "error", msg: "token lookup failed", error: e.message });
    return null;
  }
  if (!item || !item.token_sha256) return null;
  if (!sameDigest(item.token_sha256, sha256(token))) return null;
  // Fail closed on an expired grant. The container renews through the
  // backend's refresh endpoint on the same cadence as workspace credentials.
  if (Number(item.expires_at || 0) * 1000 < Date.now()) return null;
  return item;
}

async function gatewayKey(secretName) {
  const hit = secretCache.get(secretName);
  if (hit && Date.now() - hit.fetchedAt < SECRET_TTL_MS) return hit.key;
  const resp = await sm.send(new GetSecretValueCommand({ SecretId: secretName }));
  const raw = String(resp.SecretString || "").trim();
  let key = raw;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.api_key) key = parsed.api_key;
  } catch {
    /* a plain-string secret is also valid */
  }
  secretCache.set(secretName, { key, fetchedAt: Date.now() });
  return key;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on("data", (c) => {
      total += c.length;
      if (total > MAX_BODY_BYTES) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function deny(res, status, message) {
  const body = JSON.stringify({ type: "error", error: { type: "forbidden", message } });
  res.writeHead(status, { "content-type": "application/json" });
  res.end(body);
}

// Hop-by-hop headers must not be forwarded, and the caller's own auth header
// is replaced rather than passed along.
const DROP_REQUEST_HEADERS = new Set([
  "authorization",
  "x-api-key",
  "host",
  "connection",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
  "proxy-authorization",
  "content-length",
  "x-platform-session-id",
  // gzip on a streamed response forces the compressor to buffer; ask upstream
  // for identity so every SSE frame reaches the client as it is produced.
  "accept-encoding",
]);
const DROP_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "transfer-encoding",
  "content-encoding",
  "content-length",
]);

async function handleProxy(req, res, sessionItem, body) {
  const baseUrl = String(sessionItem.upstream_base_url || "").replace(/\/+$/, "");
  const secretName = String(sessionItem.gateway_secret_name || "");
  if (!baseUrl || !secretName) {
    return deny(res, 503, "session has no gateway routing configured");
  }

  // Model authorization. The allowlist was resolved by the backend from the
  // platform model control plane and written onto the token item, so a caller
  // cannot widen it by editing the request.
  //
  // Bodyless requests (a GET for model discovery, say) carry no model to
  // authorize and are forwarded as-is. A request that *has* a body must name a
  // permitted model: that is where inference is actually requested.
  const allowed = Array.isArray(sessionItem.allowed_models) ? sessionItem.allowed_models : [];
  let requested = "";
  if (body.length > 0) {
    try {
      const parsed = JSON.parse(body.toString("utf8"));
      requested = String(parsed.model || "");
    } catch {
      return deny(res, 400, "request body is not valid JSON");
    }
    if (!requested) return deny(res, 400, "request does not name a model");
  }
  if (requested && !allowed.includes(requested)) {
    log({
      level: "warn",
      msg: "model not permitted for session",
      session: sessionItem.runtime_session_id,
      user: sessionItem.user,
      requested,
    });
    return deny(res, 403, `model '${requested}' is not permitted for this session`);
  }

  let key;
  try {
    key = await gatewayKey(secretName);
  } catch (e) {
    log({ level: "error", msg: "gateway secret read failed", secret: secretName, error: e.message });
    return deny(res, 503, "gateway credential unavailable");
  }

  const target = new URL(baseUrl + req.url);
  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (!DROP_REQUEST_HEADERS.has(k.toLowerCase())) headers[k] = v;
  }
  headers["authorization"] = `Bearer ${key}`;
  headers["accept-encoding"] = "identity";
  headers["content-length"] = String(body.length);

  const client = target.protocol === "http:" ? http : https;
  const started = Date.now();
  let tail = "";

  const upstream = client.request(
    target,
    { method: req.method, headers, timeout: 15 * 60 * 1000 },
    (up) => {
      const out = {};
      for (const [k, v] of Object.entries(up.headers)) {
        if (!DROP_RESPONSE_HEADERS.has(k.toLowerCase())) out[k] = v;
      }
      res.writeHead(up.statusCode || 502, out);
      // Nothing between this write and the socket: no framework, no
      // compression, no aggregation. Usage accounting reads a rolling tail so
      // it never delays a frame.
      up.on("data", (chunk) => {
        res.write(chunk);
        tail = (tail + chunk.toString("utf8")).slice(-USAGE_TAIL_BYTES);
      });
      up.on("end", () => {
        res.end();
        log({
          level: "info",
          msg: "llm request",
          session: sessionItem.runtime_session_id,
          user: sessionItem.user,
          team: sessionItem.team || "",
          model: requested,
          status: up.statusCode,
          duration_ms: Date.now() - started,
          usage: extractUsage(tail),
        });
      });
      up.on("error", (e) => {
        log({ level: "error", msg: "upstream stream error", error: e.message });
        res.destroy();
      });
    },
  );

  upstream.on("timeout", () => upstream.destroy(new Error("upstream timeout")));
  upstream.on("error", (e) => {
    log({ level: "error", msg: "upstream request failed", error: e.message });
    if (!res.headersSent) deny(res, 502, "upstream gateway unreachable");
    else res.destroy();
  });
  upstream.end(body);
}

// Token totals arrive in the last SSE frames (message_delta / message_stop for
// the Anthropic wire format, or the final chunk for OpenAI-compatible ones).
// Scan the tail for the last usage object rather than parsing the whole stream.
function extractUsage(tail) {
  const out = {};
  const re = /"usage"\s*:\s*(\{[^{}]*\})/g;
  let m;
  let last = null;
  while ((m = re.exec(tail)) !== null) last = m[1];
  if (!last) return out;
  try {
    const u = JSON.parse(last);
    for (const k of [
      "input_tokens",
      "output_tokens",
      "cache_creation_input_tokens",
      "cache_read_input_tokens",
      "prompt_tokens",
      "completion_tokens",
      "total_tokens",
    ]) {
      if (typeof u[k] === "number") out[k] = u[k];
    }
  } catch {
    /* a truncated tail is not worth reporting on */
  }
  return out;
}

const server = http.createServer(async (req, res) => {
  if (req.url === "/healthz") {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
    return;
  }
  if (!TABLE) {
    return deny(res, 500, "PLATFORM_TABLE is not configured");
  }

  const sessionId = String(req.headers["x-platform-session-id"] || "");
  const auth = String(req.headers["authorization"] || "");
  const token = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";

  const sessionItem = await loadSession(sessionId, token);
  if (!sessionItem) {
    // One response for unknown session, bad token and expired grant alike.
    log({ level: "warn", msg: "rejected unauthenticated call", session: sessionId });
    return deny(res, 401, "invalid or expired session credential");
  }

  let body;
  try {
    body = await readBody(req);
  } catch (e) {
    return deny(res, 413, e.message);
  }

  try {
    await handleProxy(req, res, sessionItem, body);
  } catch (e) {
    log({ level: "error", msg: "proxy failed", error: e.message });
    if (!res.headersSent) deny(res, 500, "internal error");
  }
});

// Long model responses must not be cut by the server itself; the listener in
// front sets its own idle timeout.
server.requestTimeout = 0;
server.headersTimeout = 65_000;
server.timeout = 0;
server.keepAliveTimeout = 75_000;

server.listen(PORT, () => log({ level: "info", msg: `llm-edge listening on ${PORT}` }));

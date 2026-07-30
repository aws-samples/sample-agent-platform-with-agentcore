/**
 * AgentCore Runtime contract server for the interactive Claude Code kernel.
 *
 * Owns port 8080 (the single port AgentCore routes to) and provides:
 *   GET  /ping         — health check (Healthy / HealthyBusy)
 *   POST /invocations  — warmup + session-ID capture (AgentCore injects the
 *                        runtime session ID as an HTTP header here)
 *   WS   /ws           — bidirectional bridge to a local ttyd web terminal
 *
 * The browser connects to /ws through a SigV4 pre-signed WSS URL minted by the
 * backend; this process relays frames to ttyd (loopback port 7681), which
 * attaches each client to a persistent tmux session whose login shell
 * auto-starts Claude Code — disconnects detach instead of killing the process.
 */
const http = require("http");
const fs = require("fs");
const url = require("url");
const { spawn, execFile, execFileSync } = require("child_process");
const WebSocket = require("ws");

const PORT = 8080;
const TTYD_PORT = 7681;
const SESSION_ID_FILE = "/tmp/.runtime-session-id";
const STARTUP_LOG = "/tmp/.startup-log";
const SYNC_INTERVAL_MS = 30_000;

let ttydReady = false;
let activeConnections = 0;
let lastActivityTime = Math.floor(Date.now() / 1000);
let runtimeSessionId = null;
let lastSyncTime = 0;

// ---------------------------------------------------------------------------
// Session-scoped workspace credentials.
//
// The container's execution role has NO access to workspaces/* — the backend
// assumes agent-platform-workspace-access with a session policy narrowed to
// this session's prefix and delivers the credentials in the warmup payload.
// They are written as an AWS shared-credentials profile that ONLY the
// workspace-sync `aws s3` calls use (via AWS_SHARED_CREDENTIALS_FILE), so the
// container's default credential chain — skills mount, Secrets Manager,
// mcp-proxy — stays on the execution role. Role chaining caps each grant at
// 1h; a refresh token (also from the warmup payload) renews them through the
// backend. WS_CREDS_NONE marks a legacy warmup that carried no credentials,
// so start.sh's restore doesn't wait for a file that will never appear.
// ---------------------------------------------------------------------------
const WS_CREDS_FILE = "/tmp/.aws-workspace-creds";
const WS_CREDS_NONE = "/tmp/.ws-creds-none";
let wsCredsExpiry = 0; // epoch ms
let wsCredsRefresh = null; // {url, token}

function saveWorkspaceCredentials(creds) {
  const ini = [
    "[workspace]",
    `aws_access_key_id = ${creds.access_key_id}`,
    `aws_secret_access_key = ${creds.secret_access_key}`,
    `aws_session_token = ${creds.session_token}`,
    "",
  ].join("\n");
  fs.writeFileSync(WS_CREDS_FILE, ini, { mode: 0o600 });
  wsCredsExpiry = Date.parse(creds.expiration) || Date.now() + 3600_000;
  console.log(`[creds] workspace credentials valid until ${creds.expiration}`);
}

function workspaceSyncEnv() {
  if (!fs.existsSync(WS_CREDS_FILE)) return process.env;
  return {
    ...process.env,
    AWS_SHARED_CREDENTIALS_FILE: WS_CREDS_FILE,
    AWS_PROFILE: "workspace",
    // Guard against a stray env override redirecting the profile lookup.
    AWS_SDK_LOAD_CONFIG: "0",
  };
}

async function refreshWorkspaceCredentials() {
  if (!wsCredsRefresh || !wsCredsRefresh.url || !runtimeSessionId) return;
  // Renew once within 20 minutes of expiry.
  if (wsCredsExpiry - Date.now() > 20 * 60_000) return;
  try {
    const resp = await fetch(wsCredsRefresh.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        runtime_session_id: runtimeSessionId,
        token: wsCredsRefresh.token,
      }),
    });
    if (!resp.ok) {
      console.log(`[creds] refresh failed: HTTP ${resp.status}`);
      return;
    }
    const body = await resp.json();
    if (body.workspace_credentials) saveWorkspaceCredentials(body.workspace_credentials);
  } catch (e) {
    console.log(`[creds] refresh error: ${e.message}`);
  }
}
setInterval(refreshWorkspaceCredentials, 5 * 60_000);

// ---------------------------------------------------------------------------
// Per-session model routing.
//
// The warmup payload may carry config.model — the same spec shape the
// headless kernel consumes (backend "bedrock" | "gateway", model,
// small_fast_model, base_url, secret_name). The interactive kernel can't set
// per-process env like the SDK does (Claude Code runs in a tmux login shell),
// so the spec is rendered into a shell env file that .bash_profile sources
// right before starting `claude`. Written synchronously in the warmup handler,
// which always completes before the backend mints the WSS URL — so it's in
// place before the first shell spawns. An empty/absent spec keeps the
// container defaults baked into the runtime environment.
// ---------------------------------------------------------------------------
const MODEL_ENV_FILE = "/tmp/.model-env";

// Single-quote shell escaping: safe for arbitrary values in `export X='…'`.
function shq(v) {
  return "'" + String(v).replace(/'/g, "'\\''") + "'";
}

function applyModelSpec(spec) {
  const backend = String(spec.backend || "");
  const lines = [];
  // /model picker aliases (opus/sonnet/haiku). The container bakes in
  // Bedrock profile IDs; a gateway session must replace them with names from
  // its own backend's catalog — and clear families the catalog lacks, so the
  // picker never offers an ID the gateway would reject.
  const aliasEnv = (aliases) => {
    for (const family of ["opus", "sonnet", "haiku"]) {
      const envName = `ANTHROPIC_DEFAULT_${family.toUpperCase()}_MODEL`;
      if (aliases && aliases[family]) lines.push(`export ${envName}=${shq(aliases[family])}`);
      else lines.push(`unset ${envName}`);
    }
  };
  if (backend === "bedrock") {
    lines.push("export CLAUDE_CODE_USE_BEDROCK=1");
    lines.push("unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN");
    if (spec.model) lines.push(`export ANTHROPIC_MODEL=${shq(spec.model)}`);
    if (spec.small_fast_model)
      lines.push(`export ANTHROPIC_SMALL_FAST_MODEL=${shq(spec.small_fast_model)}`);
    // keep the container's baked-in (Bedrock) alias steering
  } else if (backend === "gateway") {
    if (!spec.base_url) throw new Error("gateway spec missing base_url");
    const secretName = String(spec.secret_name || "agent-platform/llm-gateway-key");
    const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "us-east-1";
    // execFileSync (no shell); uses the container's default credential chain —
    // the execution role holds the read grant for the gateway-key secret.
    const raw = execFileSync(
      "aws",
      ["secretsmanager", "get-secret-value", "--secret-id", secretName,
       "--region", region, "--query", "SecretString", "--output", "text"],
      { timeout: 15_000 },
    ).toString().trim();
    let key = raw;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.api_key) key = parsed.api_key;
    } catch { /* raw string secret */ }
    lines.push("unset CLAUDE_CODE_USE_BEDROCK");
    lines.push(`export ANTHROPIC_BASE_URL=${shq(spec.base_url)}`);
    lines.push(`export ANTHROPIC_AUTH_TOKEN=${shq(key)}`);
    if (spec.model) lines.push(`export ANTHROPIC_MODEL=${shq(spec.model)}`);
    // a baked-in Bedrock haiku ID must not leak into gateway calls
    lines.push(
      `export ANTHROPIC_SMALL_FAST_MODEL=${shq(spec.small_fast_model || spec.model || "")}`,
    );
    aliasEnv(spec.alias_models);
  } else {
    return;
  }
  fs.writeFileSync(MODEL_ENV_FILE, lines.join("\n") + "\n", { mode: 0o600 });
  console.log(`[model] routing applied: ${backend}:${spec.model || "(backend default)"}`);
}

function startTtyd() {
  console.log(`[contract-server] starting ttyd on port ${TTYD_PORT}`);
  // Attach every client to one persistent tmux session instead of spawning a
  // shell per connection: Claude Code keeps running while the browser is
  // away, and reconnecting (or switching sessions and coming back) shows the
  // live screen — including in-flight work — until AgentCore expires the
  // runtime session.
  // tmux -u: force UTF-8 even if the environment's locale detection fails —
  // otherwise non-ASCII glyphs in the TUI are rewritten to `_`.
  const ttyd = spawn(
    "ttyd",
    ["-p", String(TTYD_PORT), "-W", "tmux", "-u", "new-session", "-A", "-s", "main"],
    {
      stdio: "inherit",
      env: { ...process.env, LANG: "C.UTF-8", LC_ALL: "C.UTF-8" },
    },
  );

  ttyd.on("error", (err) => {
    console.log(`[contract-server] ttyd failed to start: ${err.message}`);
  });

  ttyd.on("exit", (code) => {
    console.log(`[contract-server] ttyd exited (${code}), restarting…`);
    ttydReady = false;
    setTimeout(startTtyd, 2000);
  });

  setTimeout(() => {
    ttydReady = true;
    console.log("[contract-server] ttyd ready");
  }, 2000);
}

/**
 * Sync /workspace and Claude Code state to the session's S3 prefix.
 * Throttled; also invoked from /ping so periodic health checks keep the
 * workspace fresh even when the background loop in start.sh is starved.
 */
function triggerSync() {
  const now = Date.now();
  if (now - lastSyncTime < SYNC_INTERVAL_MS) return;
  if (!runtimeSessionId) return;

  const bucket = process.env.WORKSPACE_S3_BUCKET;
  if (!bucket) return;
  const prefix = process.env.WORKSPACE_S3_PREFIX || "workspaces";
  const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "us-east-1";
  lastSyncTime = now;

  // execFile (no shell): the session ID comes from a request header, so it
  // must never be interpolated into a shell string.
  const base = `s3://${bucket}/${prefix}/${runtimeSessionId}`;
  const syncArgs = (src, dst, extra) => ["s3", "sync", src, dst, "--quiet", "--region", region, ...extra];
  const excludes = ["node_modules/*", ".venv/*", "__pycache__/*", ".git/objects/*", "*.pyc", "cdk.out/*"]
    .flatMap((p) => ["--exclude", p]);
  const env = workspaceSyncEnv();
  execFile("aws", syncArgs("/workspace/", `${base}/`, excludes), { timeout: 20_000, env }, (err) => {
    if (err) return console.log(`[workspace] sync error: ${err.message}`);
    execFile(
      "aws",
      syncArgs("/root/.claude/", `${base}/.claude-home/`, ["--exclude", "*.lock"]),
      { timeout: 20_000, env },
      (err2) => {
        if (err2) console.log(`[workspace] sync error: ${err2.message}`);
        else console.log(`[workspace] synced at ${new Date().toISOString()}`);
      },
    );
  });
}

/**
 * Apply the session's ecosystem attachments (sent in the warmup payload):
 * write /workspace/.mcp.json for Claude Code and sync skill packages from S3
 * into /workspace/.claude/skills/. Runs before Claude Code starts on a cold
 * container (claude waits for /tmp/.restore-done; the first warmup is what
 * created this container).
 */
function applySessionConfig(config) {
  const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "us-east-1";
  try {
    const servers = {};
    for (const s of config.mcp_servers || []) {
      if (!s.name || !s.target) continue;
      if (s.kind === "agentcore-runtime") {
        // stdio → SigV4 proxy using this container's IAM role
        const endpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${encodeURIComponent(s.target)}/invocations?qualifier=DEFAULT`;
        servers[s.name] = {
          type: "stdio",
          command: "mcp-proxy-for-aws",
          args: [endpoint, "--service", "bedrock-agentcore", "--region", region],
        };
      } else if (s.kind === "url") {
        servers[s.name] = { type: "http", url: s.target };
      } else if (s.kind === "builtin") {
        // AgentCore built-in tools (code-interpreter / browser) wrapped as a
        // local stdio MCP server; sessions use this container's IAM role.
        servers[s.name] = {
          type: "stdio",
          command: "python3",
          args: ["/opt/platform/builtin_tools_mcp.py", s.target],
        };
      }
    }
    if (Object.keys(servers).length > 0) {
      fs.writeFileSync("/workspace/.mcp.json", JSON.stringify({ mcpServers: servers }, null, 2));
      console.log(`[config] wrote .mcp.json (${Object.keys(servers).join(", ")})`);
    }
    for (const sk of config.skills || []) {
      if (!sk.name || !sk.s3_uri) continue;
      // Defense-in-depth: config comes from the platform's own ecosystem
      // registry, but validate the S3 URI against a strict allowlist anyway,
      // so an adopter wiring in a different config source stays safe.
      if (!/^s3:\/\/[a-zA-Z0-9._/-]+$/.test(sk.s3_uri)) {
        console.log(`[config] skipping skill with invalid s3_uri: ${sk.name}`);
        continue;
      }
      const dest = `/workspace/.claude/skills/${sk.name.replace(/[^a-zA-Z0-9_-]/g, "")}/`;
      // dest is a fixed prefix plus a name stripped to [a-zA-Z0-9_-] — no dots
      // or slashes survive, so path traversal is impossible.
      // nosemgrep: detect-non-literal-fs-filename
      fs.mkdirSync(dest, { recursive: true });
      // execFile (no shell): arguments are passed as an array, never interpolated
      execFile(
        // nosemgrep: detect-child-process
        "aws",
        ["s3", "sync", sk.s3_uri, dest, "--quiet", "--region", region],
        { timeout: 30_000 },
        (err) => {
          if (err) console.log(`[config] skill sync failed (${sk.name}): ${err.message}`);
          else console.log(`[config] skill mounted: ${sk.name}`);
        },
      );
    }
  } catch (e) {
    console.log(`[config] apply failed: ${e.message}`);
  }
}

function flushStartupLog() {
  try {
    const startupLog = fs.readFileSync(STARTUP_LOG, "utf8").trim();
    if (startupLog) {
      for (const line of startupLog.split("\n")) if (line) console.log(line);
      fs.writeFileSync(STARTUP_LOG, "");
    }
  } catch {
    /* no startup log yet */
  }
}

function handleHttp(req, res) {
  if (req.method === "GET" && req.url === "/ping") {
    triggerSync();
    const status = activeConnections > 0 ? "HealthyBusy" : "Healthy";
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status }));
    return;
  }

  if (req.method === "POST" && req.url === "/invocations") {
    flushStartupLog();

    // AgentCore injects the runtime session ID on every invocation; the first
    // one tells this container which session identity (and S3 prefix) it owns.
    const headerSessionId = req.headers["x-amzn-bedrock-agentcore-runtime-session-id"];
    // The ID becomes part of this session's S3 prefix — accept only a safe
    // charset so a crafted header can't produce surprising object keys.
    if (headerSessionId && !runtimeSessionId && /^[a-zA-Z0-9._-]{1,256}$/.test(headerSessionId)) {
      runtimeSessionId = headerSessionId;
      fs.writeFileSync(SESSION_ID_FILE, runtimeSessionId);
      console.log(`[contract-server] runtime session ID: ${runtimeSessionId}`);
    }

    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      let payload;
      try {
        payload = JSON.parse(body);
      } catch {
        payload = {};
      }

      const action = payload.action || "";

      if (action === "warmup") {
        lastActivityTime = Math.floor(Date.now() / 1000);
        const cfg = payload.config || {};
        if (cfg.workspace_credentials) {
          try {
            saveWorkspaceCredentials(cfg.workspace_credentials);
          } catch (e) {
            console.log(`[creds] failed to save workspace credentials: ${e.message}`);
          }
          if (cfg.workspace_credentials_refresh) {
            wsCredsRefresh = cfg.workspace_credentials_refresh;
          }
        } else if (!fs.existsSync(WS_CREDS_FILE)) {
          // Legacy backend (no credential minting): unblock start.sh's
          // restore, which otherwise waits for the credentials file.
          fs.writeFileSync(WS_CREDS_NONE, "1");
        }
        if (cfg.model) {
          try {
            applyModelSpec(cfg.model);
          } catch (e) {
            // keep container defaults — a broken spec must not brick the shell
            console.log(`[model] failed to apply model spec: ${e.message}`);
          }
        }
        if (payload.config) applySessionConfig(payload.config);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            status: ttydReady ? "ready" : "starting",
            sessionId: runtimeSessionId,
          }),
        );
        return;
      }

      if (action === "status") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            status: ttydReady ? "ready" : "starting",
            activeConnections,
            lastActivity: lastActivityTime,
            uptime: process.uptime(),
          }),
        );
        return;
      }

      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: `Unknown action: ${action || "(none)"}` }));
    });
    return;
  }

  res.writeHead(404);
  res.end("Not Found");
}

// Plain HTTP on the single container port is intentional: AgentCore terminates
// TLS at the platform edge and routes to this loopback-facing port inside the
// microVM (see docs/architecture.md — Security notes). No plaintext traffic
// crosses the network boundary.
// nosemgrep: using-http-server
const server = http.createServer(handleHttp);
const wsBridgeServer = new WebSocket.Server({ noServer: true });

server.on("upgrade", (req, socket, head) => {
  const parsedUrl = url.parse(req.url, true);
  if (parsedUrl.pathname !== "/ws") {
    socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
    socket.destroy();
    return;
  }

  if (!ttydReady) {
    console.log("[ws-bridge] ttyd not ready — rejecting upgrade");
    socket.write("HTTP/1.1 503 Service Unavailable\r\n\r\n");
    socket.destroy();
    return;
  }

  wsBridgeServer.handleUpgrade(req, socket, head, (downstream) => {
    activeConnections++;
    lastActivityTime = Math.floor(Date.now() / 1000);
    console.log(`[ws-bridge] browser connected (${activeConnections} active)`);

    // ttyd speaks its own subprotocol; declare it or the handshake fails.
    const upstream = new WebSocket(`ws://127.0.0.1:${TTYD_PORT}`, ["tty"]);

    let upstreamOpen = false;
    const pendingMessages = [];

    upstream.on("open", () => {
      upstreamOpen = true;
      for (const msg of pendingMessages) upstream.send(msg);
      pendingMessages.length = 0;
    });

    downstream.on("message", (data) => {
      lastActivityTime = Math.floor(Date.now() / 1000);
      // Keepalive frames (single space) refresh activity but never reach the
      // terminal — otherwise the shell would receive stray spaces.
      if (typeof data === "string" && data === " ") return;
      if (data instanceof Buffer && data.length === 1 && data[0] === 0x20) return;
      if (upstreamOpen && upstream.readyState === WebSocket.OPEN) {
        upstream.send(data);
      } else {
        pendingMessages.push(data);
      }
    });

    upstream.on("message", (data) => {
      lastActivityTime = Math.floor(Date.now() / 1000);
      if (downstream.readyState === WebSocket.OPEN) downstream.send(data);
    });

    downstream.on("close", () => {
      activeConnections--;
      console.log(`[ws-bridge] browser disconnected (${activeConnections} active)`);
      if (upstream.readyState === WebSocket.OPEN) upstream.close();
    });
    downstream.on("error", (err) => {
      console.log(`[ws-bridge] downstream error: ${err.message}`);
      if (upstream.readyState === WebSocket.OPEN) upstream.close();
    });
    upstream.on("close", () => {
      if (downstream.readyState === WebSocket.OPEN) downstream.close();
    });
    upstream.on("error", (err) => {
      console.log(`[ws-bridge] upstream error: ${err.message}`);
      if (downstream.readyState === WebSocket.OPEN) downstream.close();
    });
  });
});

server.listen(PORT, () => {
  console.log(`[contract-server] HTTP + WS listening on ${PORT}`);
  startTtyd();
});

process.on("SIGTERM", () => {
  console.log("[contract-server] SIGTERM — exiting");
  process.exit(0);
});

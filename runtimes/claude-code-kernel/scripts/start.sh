#!/bin/bash
# Entrypoint for the interactive Claude Code kernel.
#
# Responsibilities:
#   1. Resolve model access (LLM gateway or Bedrock direct) — no baked-in secrets
#   2. Prepare the Claude Code workspace (permissions, onboarding state)
#   3. Optionally wire MCP tools hosted on another AgentCore Runtime
#   4. Start the contract-server (ttyd + WebSocket bridge on :8080)
#   5. Restore the session workspace from S3, then sync it back periodically
set -e

# ---------------------------------------------------------------------------
# 1. Model access
#
# Bedrock mode (set CLAUDE_CODE_USE_BEDROCK=1):
#   Claude Code calls Amazon Bedrock with the container's IAM role. Use
#   cross-region inference profiles (model IDs prefixed with `global.`).
#
# Gateway mode: nothing happens here, on purpose.
#   The session's user is root in this microVM, so a credential exported into
#   this environment is a credential they have. This script used to read the
#   LLM gateway key from Secrets Manager and export it as ANTHROPIC_AUTH_TOKEN,
#   which put a long-lived, platform-wide key one `env` away from anyone with
#   the terminal. The key now lives only in the llm-edge service; the container
#   receives a short-lived, session-scoped grant in the warmup payload, and
#   contract-server keeps it in memory behind a loopback shim rather than
#   putting it in the environment. Gateway routing is therefore always
#   per-session and can no longer be baked into the container.
# ---------------------------------------------------------------------------
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_DEFAULT_REGION="$AWS_REGION"

# Claude Code silently downgrades bypassPermissions when running as root
# unless it believes it's inside a sandbox. The AgentCore microVM *is* the
# sandbox here — without this, every MCP/tool call pops a permission dialog.
export IS_SANDBOX=1

if [ "$CLAUDE_CODE_USE_BEDROCK" = "1" ]; then
  echo "[start.sh] model access: Amazon Bedrock (IAM role, cross-region inference)"
elif [ -n "$ANTHROPIC_BASE_URL" ]; then
  # A gateway address baked into the container environment is now dead weight:
  # reaching the gateway requires a per-session grant that only arrives at
  # warmup. Clear it so a stale value can't send a Bedrock-routed session at an
  # endpoint it has no credential for.
  echo "[start.sh] NOTE: ignoring container-level ANTHROPIC_BASE_URL — gateway routing is per-session"
  unset ANTHROPIC_BASE_URL
  echo "[start.sh] model access: resolved per session at warmup"
else
  echo "[start.sh] model access: resolved per session at warmup"
fi

# ---------------------------------------------------------------------------
# 2. Workspace + Claude Code configuration
# ---------------------------------------------------------------------------
S3_BUCKET="${WORKSPACE_S3_BUCKET:?WORKSPACE_S3_BUCKET env var is required}"
S3_PREFIX="${WORKSPACE_S3_PREFIX:-workspaces}"
SYNC_INTERVAL="${WORKSPACE_SYNC_INTERVAL:-30}"
WORKSPACE="/workspace"

SESSION_ID_FILE="/tmp/.runtime-session-id"
STARTUP_LOG="/tmp/.startup-log"
echo -n "" > "$STARTUP_LOG"

# Session-scoped S3 credentials for workspace sync. The container's own role
# has no workspaces/* access; the backend delivers per-session credentials in
# the warmup payload and contract-server writes them to this profile file
# (and keeps them refreshed). Only the s3 sync/ls calls below use them —
# everything else stays on the execution role.
WS_CREDS_FILE="/tmp/.aws-workspace-creds"
WS_CREDS_NONE="/tmp/.ws-creds-none"

ws_aws() {
  if [ -f "$WS_CREDS_FILE" ]; then
    AWS_SHARED_CREDENTIALS_FILE="$WS_CREDS_FILE" AWS_PROFILE="workspace" aws "$@"
  else
    aws "$@"
  fi
}

log_startup() {
  local msg="[startup] $(date -u +%H:%M:%S) $1"
  echo "$msg" >> /proc/1/fd/1 2>/dev/null || echo "$msg"
  echo "$msg" >> "$STARTUP_LOG"
}

get_session_id() {
  if [ -f "$SESSION_ID_FILE" ]; then
    cat "$SESSION_ID_FILE"
  else
    echo "shared"
  fi
}

save_workspace() {
  local sid
  sid=$(get_session_id)
  # Never sync before the session identity is known — otherwise different
  # sessions would clobber a shared prefix.
  if [ "$sid" = "shared" ]; then
    return
  fi
  local s3_path="s3://${S3_BUCKET}/${S3_PREFIX}/${sid}"
  ws_aws s3 sync "${WORKSPACE}/" "${s3_path}/" \
    --quiet \
    --exclude "node_modules/*" \
    --exclude ".venv/*" \
    --exclude "__pycache__/*" \
    --exclude ".git/objects/*" \
    --exclude "*.pyc" \
    --exclude "cdk.out/*" \
    2>/dev/null || true
  # Claude Code state (conversation transcripts, project registry) lives in
  # /root/.claude — persist it so a dormant session resumes with history.
  ws_aws s3 sync /root/.claude/ "${s3_path}/.claude-home/" \
    --quiet --exclude "*.lock" 2>/dev/null || true
  echo "[workspace] saved to ${s3_path} at $(date -u +%H:%M:%S)" >> /proc/1/fd/1 2>/dev/null || true
}

background_sync() {
  while true; do
    sleep "$SYNC_INTERVAL"
    if [ -f "$SESSION_ID_FILE" ]; then
      save_workspace
    fi
  done
}

cleanup() {
  echo "[workspace] shutdown — final sync…" >> /proc/1/fd/1 2>/dev/null || true
  save_workspace
  kill "$NODE_PID" 2>/dev/null || true
  exit 0
}

trap cleanup SIGTERM SIGINT

mkdir -p "${WORKSPACE}/.claude" /root/.claude

# The terminal is the trust boundary here, not Claude Code's permission
# prompts: whoever reaches this shell already has full container access, so
# run Claude Code unattended-friendly inside the sandbox.
# NOTE: defaultMode must live under "permissions" — a top-level defaultMode is
# silently ignored and every MCP tool call would pop a permission dialog.
# skipDangerousModePermissionPrompt: newer Claude Code (2.1.x+) gates
# bypassPermissions behind a one-time "accept responsibility" dialog on launch;
# without this the auto-started `claude` sits at that dialog (and a stray Enter
# selects "No, exit", dropping the session to a bare shell).
cat > "${WORKSPACE}/.claude/settings.json" << 'SETTINGS'
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "WebFetch(*)",
      "WebSearch(*)"
    ]
  },
  "enableAllProjectMcpServers": true,
  "skipDangerousModePermissionPrompt": true
}
SETTINGS
cp "${WORKSPACE}/.claude/settings.json" /root/.claude/settings.json

# Pre-complete onboarding and pre-trust /workspace so every `claude` launch
# goes straight to the prompt (each WebSocket connect spawns a fresh shell,
# so without this the trust dialog would reappear on every reconnect).
cat > /root/.claude.json << 'CLAUDEJSON'
{
  "numStartups": 1,
  "hasCompletedOnboarding": true,
  "hasDismissedAnnouncement": true,
  "projects": {
    "/workspace": {
      "hasTrustDialogAccepted": true,
      "hasCompletedProjectOnboarding": true
    }
  }
}
CLAUDEJSON

cat > "${WORKSPACE}/CLAUDE.md" << 'CLAUDEMD'
# Cloud Workspace

This is a hosted Claude Code workspace running on Amazon Bedrock AgentCore.

- Files in /workspace persist to S3 and survive container restarts.
- Your conversation history is restored when you resume a dormant session.
- Runtime session ID: `cat /tmp/.runtime-session-id`
CLAUDEMD

# ---------------------------------------------------------------------------
# 3. Optional: MCP tools hosted on another AgentCore Runtime
#
# MCP_RUNTIME_ARN points at an AgentCore Runtime deployed with protocol=MCP.
# Claude Code reaches it through a local stdio→SigV4 proxy using this
# container's IAM role — no extra secrets required.
# ---------------------------------------------------------------------------
if [ -n "$MCP_RUNTIME_ARN" ]; then
  MCP_REGION="${MCP_RUNTIME_REGION:-$AWS_REGION}"
  ENCODED_ARN=$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['MCP_RUNTIME_ARN'],safe=''))")
  MCP_ENDPOINT="https://bedrock-agentcore.${MCP_REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT"
  cat > "${WORKSPACE}/.mcp.json" << MCPJSON
{
  "mcpServers": {
    "platform-tools": {
      "type": "stdio",
      "command": "mcp-proxy-for-aws",
      "args": ["${MCP_ENDPOINT}", "--service", "bedrock-agentcore", "--region", "${MCP_REGION}"]
    }
  }
}
MCPJSON
  log_startup "wired MCP tools → ${MCP_RUNTIME_ARN}"
fi

# ---------------------------------------------------------------------------
# 4. Auto-start Claude Code inside a persistent tmux session
#
# ttyd attaches every WebSocket client to tmux session "main" (see
# contract-server). Detaching does NOT kill the shell or Claude Code, so a
# browser disconnect / session switch keeps the conversation and any running
# task alive until AgentCore expires the runtime session.
# ---------------------------------------------------------------------------
cat > /root/.tmux.conf << 'TMUXCONF'
# Look like a plain terminal, not a multiplexer
set -g status off
# Follow the size of the most recently active client
setw -g aggressive-resize on
# The tmux window runs a login shell so .bash_profile auto-starts claude
set -g default-command "bash -l"
set -g history-limit 10000
set -sg escape-time 0
# Keep the tmux server alive even when no session exists yet
set -g exit-empty off
TMUXCONF

cat >> /root/.bash_profile << 'AUTOSTART'
source /root/.bashrc 2>/dev/null || true
if [ -z "$CLAUDE_STARTED" ] && [ -n "$PS1" ]; then
  export CLAUDE_STARTED=1
  # Wait for the S3 restore to finish so Claude Code sees the full workspace
  WAIT=0
  while [ ! -f /tmp/.restore-done ] && [ $WAIT -lt 30 ]; do
    sleep 1
    WAIT=$((WAIT + 1))
  done
  # Per-session model routing (written by contract-server from the warmup
  # payload's config.model). Overrides the container's baked-in model env for
  # this session's Claude Code only. Also applies on every reconnect, since
  # ttyd spawns a fresh login shell per WebSocket client.
  if [ -f /tmp/.model-env ]; then
    source /tmp/.model-env
  fi
  # ttyd spawns a new shell per WebSocket client, so a browser reconnect (or
  # a dormant-session resume) starts a fresh `claude` process. Continue the
  # previous conversation when transcripts exist — locally or restored from S3.
  if find /root/.claude/projects -name '*.jsonl' -size +0c 2>/dev/null | head -1 | grep -q .; then
    claude --continue || claude
  else
    claude
  fi
fi
AUTOSTART

# ---------------------------------------------------------------------------
# 5. Contract server + S3 restore + background sync
# ---------------------------------------------------------------------------
node /opt/contract-server/main.js &
NODE_PID=$!

(
  log_startup "waiting for session ID from AgentCore…"
  WAIT_COUNT=0
  while [ ! -f "$SESSION_ID_FILE" ] && [ $WAIT_COUNT -lt 60 ]; do
    sleep 1
    WAIT_COUNT=$((WAIT_COUNT + 1))
  done

  SESSION_ID=$(get_session_id)
  if [ "$SESSION_ID" = "shared" ]; then
    log_startup "WARNING: no session ID after 60s — skipping S3 restore"
  else
    S3_PATH="s3://${S3_BUCKET}/${S3_PREFIX}/${SESSION_ID}"
    log_startup "session: ${SESSION_ID}"
    # The restore needs the session-scoped credentials, which arrive in the
    # same warmup that delivered the session ID (contract-server writes the
    # profile file). Wait briefly; WS_CREDS_NONE marks a legacy backend that
    # sends none — then the container role is the only (legacy) option.
    CREDS_WAIT=0
    while [ ! -f "$WS_CREDS_FILE" ] && [ ! -f "$WS_CREDS_NONE" ] && [ $CREDS_WAIT -lt 15 ]; do
      sleep 1
      CREDS_WAIT=$((CREDS_WAIT + 1))
    done
    log_startup "restoring from ${S3_PATH}…"
    if ws_aws s3 ls "${S3_PATH}/" >/dev/null 2>&1; then
      # .mcp.json is owned by the warmup config (session attachments), not by
      # the restored snapshot — never let restore clobber it.
      ws_aws s3 sync "${S3_PATH}/" "${WORKSPACE}/" --quiet \
        --exclude ".claude/settings.json" --exclude ".claude-home/*" \
        --exclude ".mcp.json" 2>/dev/null || true
      if ws_aws s3 ls "${S3_PATH}/.claude-home/" >/dev/null 2>&1; then
        ws_aws s3 sync "${S3_PATH}/.claude-home/" /root/.claude/ --quiet \
          --exclude "settings.json" 2>/dev/null || true
        log_startup "Claude Code state restored"
      fi
      FILE_COUNT=$(find "${WORKSPACE}" -type f 2>/dev/null | wc -l)
      log_startup "restored ${FILE_COUNT} files"
    else
      log_startup "no prior data — fresh workspace"
    fi
  fi
  touch /tmp/.restore-done
) &

background_sync &

wait "$NODE_PID"
cleanup

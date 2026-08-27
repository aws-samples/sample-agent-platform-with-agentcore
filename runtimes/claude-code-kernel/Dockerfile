# Interactive Claude Code kernel for Amazon Bedrock AgentCore Runtime.
# AgentCore Runtime executes linux/arm64 images — build with:
#   docker buildx build --platform linux/arm64 -t claude-code-kernel .
# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: AgentCore Runtime health-checks via the /ping HTTP contract, not Docker HEALTHCHECK.
# checkov:skip=CKV_DOCKER_3: Runs as root inside a per-session AgentCore microVM; Claude Code requires root in-sandbox (IS_SANDBOX=1).

# UTF-8 locale is required for tmux: without it, tmux treats clients as
# non-UTF-8 and rewrites every non-ASCII glyph (Claude Code's logo, `·`, `❯`,
# spinners) to `_`, mangling the terminal UI.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# nosemgrep: remove-package-cache  (apt lists are removed via rm -rf below)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential wget unzip jq tmux \
    && rm -rf /var/lib/apt/lists/*

# ttyd: web terminal backend (loopback only; bridged by contract-server)
# nosemgrep: use-either-wget-or-curl  (wget fetches the ttyd binary; curl is used for the NodeSource script)
RUN wget -q https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.aarch64 -O /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd

# nosemgrep: set-pipefail  (trusted NodeSource setup script; build fails fast)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# awscli: S3 workspace sync + Secrets Manager reads
# mcp-proxy-for-aws: optional stdio→SigV4 proxy to MCP servers on AgentCore
# bedrock-agentcore + playwright: AgentCore built-in tools (Code Interpreter /
# Browser) wrapped as a local stdio MCP server; playwright only drives the
# remote cloud browser over CDP — no local browser binaries needed
RUN pip install --no-cache-dir awscli boto3 mcp mcp-proxy-for-aws bedrock-agentcore playwright

COPY scripts/builtin_tools_mcp.py /opt/platform/builtin_tools_mcp.py

# stdio → MCPHUB-HMAC-SHA256 signing proxy for mcp-hub attachments (workbench
# sessions sign as the shared dev-workbench Actor). Copies must stay identical
# to runtimes/agent-sdk-kernel/src/ — the signature algorithm is shared with
# the hub and must not drift per kernel.
COPY scripts/mcp_hub_proxy.py /opt/platform/mcp_hub_proxy.py
COPY scripts/mcphub_hmac.py /opt/platform/mcphub_hmac.py

COPY contract-server/ /opt/contract-server/
WORKDIR /opt/contract-server
RUN npm install --production

COPY scripts/start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

WORKDIR /workspace

EXPOSE 8080

CMD ["/opt/start.sh"]

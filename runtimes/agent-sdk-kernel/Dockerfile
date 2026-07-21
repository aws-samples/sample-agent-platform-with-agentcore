# Headless Claude Agent SDK kernel for Amazon Bedrock AgentCore Runtime.
# AgentCore Runtime executes linux/arm64 images — build with:
#   docker buildx build --platform linux/arm64 -t agent-sdk-kernel .
# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: AgentCore Runtime health-checks via the /ping HTTP contract, not Docker HEALTHCHECK.
# checkov:skip=CKV_DOCKER_3: Runs as root inside a per-session AgentCore microVM; the Claude Agent SDK spawns a Claude Code subprocess that expects root in-sandbox.

# nosemgrep: remove-package-cache  (apt lists are removed via rm -rf below)
RUN apt-get update && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*

# The Claude Agent SDK drives a Claude Code CLI subprocess — Node.js required
# nosemgrep: set-pipefail  (trusted NodeSource setup script; build fails fast)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

COPY requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt

COPY src/ /app/
# Same install path as the interactive kernel so registry entries with
# kind=builtin resolve identically in both hosting models.
RUN mkdir -p /opt/platform && cp /app/builtin_tools_mcp.py /opt/platform/
WORKDIR /app

EXPOSE 8080

CMD ["python", "main.py"]

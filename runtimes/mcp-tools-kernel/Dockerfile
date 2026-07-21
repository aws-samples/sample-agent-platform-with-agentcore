# Demo MCP server for Amazon Bedrock AgentCore Runtime (protocol = MCP).
# AgentCore routes MCP traffic to 0.0.0.0:8000/mcp — stateless streamable-HTTP.
# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: AgentCore Runtime health-checks via its MCP protocol contract, not Docker HEALTHCHECK.
# checkov:skip=CKV_DOCKER_3: Runs inside a per-session AgentCore microVM; demo MCP server, hardening left to the adopter.

RUN pip install --no-cache-dir "mcp>=1.10.0"

COPY src/server.py /app/server.py

EXPOSE 8000

CMD ["python", "/app/server.py"]

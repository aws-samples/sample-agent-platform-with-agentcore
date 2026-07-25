# Team-scoped backend API (MCP streamable-HTTP) with app-layer SSO authz.
# One image serves both teams; the TEAM env var selects team-a / team-b.
# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: health is the ALB target-group /health check, not Docker HEALTHCHECK.
# checkov:skip=CKV_DOCKER_3: demo backend behind a CloudFront-only ALB; hardening left to the adopter.

RUN pip install --no-cache-dir "mcp>=1.10.0" "PyJWT[crypto]>=2.8" "uvicorn>=0.30"

COPY src/server.py /app/server.py

EXPOSE 8000

CMD ["python", "/app/server.py"]

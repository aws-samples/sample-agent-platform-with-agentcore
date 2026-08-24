# llm-edge: holds the LLM gateway key so session containers never do.
#
# This image is the only place in the data path that can read the gateway
# secret, so it stays deliberately thin: no shell tooling beyond what Node
# needs, and the process runs unprivileged.
# nosemgrep: dockerfile-source-not-pinned
FROM node:22-slim
# checkov:skip=CKV_DOCKER_2: liveness is the ALB target-group /healthz check.

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force

COPY src/ ./src/

# Drop privileges: nothing here needs root, and the task role is the only
# credential in the container worth having.
USER node

EXPOSE 8080

CMD ["node", "src/server.js"]

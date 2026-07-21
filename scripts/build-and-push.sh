#!/bin/bash
# Build all container images (linux/arm64 — required by AgentCore Runtime)
# and push them to the ECR repositories created by PlatformStack.
#
# Usage: ./scripts/build-and-push.sh [image-tag]
set -euo pipefail

TAG="${1:-latest}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Registry: ${REGISTRY}  Tag: ${TAG}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

build() {
  local name="$1" dir="$2"
  local uri="${REGISTRY}/agent-platform/${name}:${TAG}"
  echo "==> ${name} (${dir})"
  docker buildx build --platform linux/arm64 -t "$uri" --push "$dir"
}

build claude-code-kernel "$ROOT/runtimes/claude-code-kernel"
build agent-sdk-kernel  "$ROOT/runtimes/agent-sdk-kernel"
build mcp-tools-kernel  "$ROOT/runtimes/mcp-tools-kernel"
build backend           "$ROOT/backend"

echo "All images pushed. Next: cdk deploy AgentPlatformRuntime"

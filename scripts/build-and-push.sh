#!/bin/bash
# Build all container images (linux/arm64 — required by AgentCore Runtime)
# and push them to the ECR repositories the foundation creates
# (agent-platform<name_suffix>/<image>).
#
# Usage: ./scripts/build-and-push.sh [image-tag]
#   PLATFORM_NAME_SUFFIX=-staging ./scripts/build-and-push.sh [image-tag]
#     pushes to the repositories of the environment with that name_suffix
#     ("" = production), the same variable deploy-frontend.sh reads.
#   OTEL_VARIANT=1 ./scripts/build-and-push.sh [image-tag]
#     additionally builds agent-sdk-kernel:<image-tag>-otel, the observability
#     variant (Dockerfile.otel: base image + ADOT + OpenInference). Opt-in; the
#     base image is unchanged. Deploy it with sdk_image_tag = "<image-tag>-otel"
#     and agent_observability = true.
set -euo pipefail

TAG="${1:-latest}"
SUFFIX="${PLATFORM_NAME_SUFFIX:-}"
# The allowed characters are spelled out: a glob range like a-z follows the
# locale's collation, and in en_US.UTF-8 (the macOS default) it admits
# uppercase letters too.
case "$SUFFIX" in
  *[!abcdefghijklmnopqrstuvwxyz0123456789-]*) echo "PLATFORM_NAME_SUFFIX must be lowercase alphanumerics/hyphens (the foundation's name_suffix)" >&2; exit 2 ;;
esac
OTEL_VARIANT="${OTEL_VARIANT:-0}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Registry: ${REGISTRY}  Tag: ${TAG}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

build() {
  local name="$1" dir="$2"
  local uri="${REGISTRY}/agent-platform${SUFFIX}/${name}:${TAG}"
  echo "==> ${name} (${dir})"
  docker buildx build --platform linux/arm64 -t "$uri" --push "$dir"
}

build_otel_variant() {
  local dir="$ROOT/runtimes/agent-sdk-kernel"
  local base="${REGISTRY}/agent-platform${SUFFIX}/agent-sdk-kernel:${TAG}"
  local uri="${base}-otel"
  echo "==> agent-sdk-kernel observability variant (${dir}/Dockerfile.otel)"
  docker buildx build --platform linux/arm64 -f "$dir/Dockerfile.otel" \
    --build-arg BASE_IMAGE="$base" -t "$uri" --push "$dir"
}

build claude-code-kernel "$ROOT/runtimes/claude-code-kernel"
build agent-sdk-kernel  "$ROOT/runtimes/agent-sdk-kernel"
if [ "$OTEL_VARIANT" = "1" ]; then build_otel_variant; fi
build mcp-tools-kernel  "$ROOT/runtimes/mcp-tools-kernel"
build backend           "$ROOT/backend"
# Only needed when the 'litellm' model backend is used (enable_llm_edge).
build llm-edge          "$ROOT/services/llm-edge"

echo "All images pushed. Next: terraform apply in terraform/workloads with these tags (see terraform/README.md, Deploy order)"

#!/bin/bash
# Build the team-auth demo images (linux/arm64) and push them to the ECR
# repositories created by PlatformStack.
#
# Usage: ./scripts/build-and-push-team-auth.sh [image-tag]
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

build keycloak "$ROOT/services/keycloak"
build team-api "$ROOT/services/team-api"

echo "Images pushed. Next: cdk deploy AgentPlatformTeamAuth"

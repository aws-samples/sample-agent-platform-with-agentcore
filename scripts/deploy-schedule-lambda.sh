#!/bin/bash
# Build and deploy the schedule-runner Lambda package.
#
# The portal module creates the function with placeholder code (mirroring how
# ECR images are pushed outside Terraform / CloudFormation); this script builds
# the real package — manylinux/arm64 wheels + the backend `app` package + the
# handler — and updates the function code in place.
#
# The function name comes from the foundation's facts document
# (/agent-platform<suffix>/foundation, portal.schedule_runner_function), the
# same source the acceptance checks use; PLATFORM_NAME_SUFFIX selects the
# environment ("" = production, "-staging" = staging). A CloudFormation
# deployment (the legacy CDK stacks) is tried when no facts document exists.
#
# Usage: ./scripts/deploy-schedule-lambda.sh
#        PLATFORM_NAME_SUFFIX=-staging ./scripts/deploy-schedule-lambda.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region)}"
PY="${PYTHON:-python3}"
SUFFIX="${PLATFORM_NAME_SUFFIX:-}"

FN=""
facts="$(aws ssm get-parameter --name "/agent-platform${SUFFIX}/foundation" --region "$REGION" \
  --query Parameter.Value --output text 2>/dev/null || true)"
if [ -n "$facts" ] && [ "$facts" != "None" ]; then
  FN="$(printf '%s' "$facts" | "$PY" -c 'import json, sys; d = json.load(sys.stdin); print(d.get("portal", {}).get("schedule_runner_function", ""))')"
  echo "facts: /agent-platform${SUFFIX}/foundation"
else
  FN=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ScheduleRunnerFunction'].OutputValue" --output text 2>/dev/null || true)
  echo "no facts document; using the CloudFormation stack AgentPlatformPortal"
fi
if [ -z "$FN" ] || [ "$FN" = "None" ]; then
  echo "schedule-runner function not found: deploy the foundation (terraform) or the AgentPlatformPortal stack first" >&2
  exit 1
fi

# DRY_RUN=1: resolve the target and stop (the deploy-scripts test uses this)
[ -z "${DRY_RUN:-}" ] || { echo "resolved function: $FN"; exit 0; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD" "$BUILD.zip"' EXIT
echo "Building package (arm64 wheels for the Lambda runtime)…"
"$PY" -m pip --version >/dev/null 2>&1 || { echo "$PY has no pip; set PYTHON to an interpreter with pip" >&2; exit 1; }
"$PY" -m pip install --quiet \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --target "$BUILD" \
  -r "$ROOT/backend/requirements-lambda.txt"
cp -R "$ROOT/backend/app" "$BUILD/app"
cp "$ROOT/backend/lambda/index.py" "$BUILD/index.py"
find "$BUILD" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
(cd "$BUILD" && zip -qr "$BUILD.zip" .)
echo "Package: $(du -h "$BUILD.zip" | cut -f1) — updating $FN"
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$BUILD.zip" --region "$REGION" --no-cli-pager >/dev/null
aws lambda wait function-updated --function-name "$FN" --region "$REGION"
echo "schedule-runner deployed."

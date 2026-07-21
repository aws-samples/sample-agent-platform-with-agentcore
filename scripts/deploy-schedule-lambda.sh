#!/bin/bash
# Build and deploy the schedule-runner Lambda package.
#
# The PortalStack creates the function with placeholder code (mirroring how
# ECR images are pushed outside CloudFormation); this script builds the real
# package — manylinux/arm64 wheels + the backend `app` package + the handler —
# and updates the function code in place.
#
# Usage: ./scripts/deploy-schedule-lambda.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region)}"

FN=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ScheduleRunnerFunction'].OutputValue" --output text)
if [ -z "$FN" ] || [ "$FN" = "None" ]; then
  echo "ScheduleRunnerFunction output not found — deploy AgentPlatformPortal first" >&2
  exit 1
fi

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD" "$BUILD.zip"' EXIT

echo "Building package (arm64 wheels for the Lambda runtime)…"
python3 -m pip install --quiet \
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

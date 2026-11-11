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
# CI runs it after every workloads apply (ci/codebuild/run.sh,
# publish_schedule_runner); by hand it is for a deployment without that CI.
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
  # The document must be the one of the environment SUFFIX names (its own
  # environment and name_suffix), however it was reached: a caller that checked
  # one read of the parameter cannot vouch for this one.
  FN="$(printf '%s' "$facts" | SUFFIX="$SUFFIX" "$PY" -c '
import json, os, sys
d = json.load(sys.stdin)
suffix = os.environ["SUFFIX"]
want = "production" if suffix == "" else suffix.lstrip("-")
env, got = d.get("environment"), d.get("name_suffix", "")
if env != want or got != suffix:
    sys.exit(f"refusing: /agent-platform{suffix}/foundation is the facts of {env!r} (name_suffix {got!r}), not {want!r}")
print(d.get("portal", {}).get("schedule_runner_function", ""))')" || exit 1
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
# From the hash-locked file (ci/lock/update.sh regenerates it from
# backend/requirements-lambda.txt): CI deploy builds run this with the deploy
# role, and the package then runs as the schedule-runner role, so a replaced
# upload on the index fails the install instead of shipping. No pip
# configuration can add another index (dependency confusion): --isolated drops
# the PIP_* variables and the user file, PIP_CONFIG_FILE=/dev/null every
# configuration file (global and site too, which --isolated still reads), and
# the index is named explicitly. ci/lock/check-pypi.py first confirms every pin
# is a pypi.org release and every hash one of its files (it refuses what it
# cannot read).
if ! confirm="$("$PY" -I "$ROOT/ci/lock/check-pypi.py" "$ROOT/ci/lock/schedule-runner.txt" 2>&1)"; then
  printf '%s\n' "$confirm" | grep -v '^ok ' >&2 || true
  echo "refusing: the lock is not confirmed against pypi.org" >&2
  exit 1
fi
PIP_CONFIG_FILE=/dev/null "$PY" -m pip install --quiet --isolated --require-hashes --index-url https://pypi.org/simple \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --target "$BUILD" \
  -r "$ROOT/ci/lock/schedule-runner.txt"
cp -R "$ROOT/backend/app" "$BUILD/app"
cp "$ROOT/backend/lambda/index.py" "$BUILD/index.py"
find "$BUILD" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# zip(1) is not on every build image; Python's zipfile writes the same archive
if command -v zip >/dev/null 2>&1; then
  (cd "$BUILD" && zip -qr "$BUILD.zip" .)
else
  (cd "$BUILD" && "$PY" -m zipfile -c "$BUILD.zip" ./*)
fi
echo "Package: $(du -h "$BUILD.zip" | cut -f1) — updating $FN"
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$BUILD.zip" --region "$REGION" --no-cli-pager >/dev/null
aws lambda wait function-updated --function-name "$FN" --region "$REGION"
echo "schedule-runner deployed."

#!/bin/bash
# Build and deploy the Lark Lambda package (lark-teammate sample): the
# lark-webhook function and, when the foundation has it, lark-memory.
#
# Same shape as deploy-schedule-lambda.sh: the workloads root creates the
# function with placeholder code; this script builds the real package
# (manylinux/arm64 wheels + the backend `app` package + the handlers) and
# updates the function code in place.
#
# The function names come from the foundation's facts document
# (/agent-platform<suffix>/foundation, lark_ingress.function_name and
# lark_ingress.memory_function_name); PLATFORM_NAME_SUFFIX selects the
# environment ("" = production, "-staging" = staging). The ingress is
# optional: no facts entry, no deploy.
#
# The operator runs it after enabling the ingress (samples/agents/lark-teammate
# README, "Connect a Lark app"); CI does not run it.
#
# Usage: ./scripts/deploy-lark-webhook.sh
#        PLATFORM_NAME_SUFFIX=-staging ./scripts/deploy-lark-webhook.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region)}"
PY="${PYTHON:-python3}"
SUFFIX="${PLATFORM_NAME_SUFFIX:-}"
LOCK="$ROOT/ci/lock/lark-webhook.txt"

facts="$(aws ssm get-parameter --name "/agent-platform${SUFFIX}/foundation" --region "$REGION" \
  --query Parameter.Value --output text 2>/dev/null || true)"
if [ -z "$facts" ] || [ "$facts" = "None" ]; then
  echo "no facts document /agent-platform${SUFFIX}/foundation: deploy the foundation first" >&2
  exit 1
fi
# The document must be the one of the environment SUFFIX names (its own
# environment and name_suffix): the code of one environment must not reach
# the function of another. Both function names are read in the same pass.
names="$(printf '%s' "$facts" | SUFFIX="$SUFFIX" "$PY" -c '
import json, os, sys
d = json.load(sys.stdin)
suffix = os.environ["SUFFIX"]
want = "production" if suffix == "" else suffix.lstrip("-")
env, got = d.get("environment"), d.get("name_suffix", "")
if env != want or got != suffix:
    sys.exit(f"refusing: /agent-platform{suffix}/foundation is the facts of {env!r} (name_suffix {got!r}), not {want!r}")
lark = d.get("lark_ingress") or {}
print(lark.get("function_name") or "")
print(lark.get("memory_function_name") or "")')" || exit 1
FN="$(printf '%s\n' "$names" | sed -n 1p)"
MEM_FN="$(printf '%s\n' "$names" | sed -n 2p)"
echo "facts: /agent-platform${SUFFIX}/foundation"
if [ -z "$FN" ]; then
  echo "the foundation has no Lark ingress (enable_lark_ingress) in /agent-platform${SUFFIX}/foundation" >&2
  exit 1
fi

# DRY_RUN=1: resolve the target and stop
[ -z "${DRY_RUN:-}" ] || { echo "resolved function: $FN${MEM_FN:+ and $MEM_FN}"; exit 0; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD" "$BUILD.zip"' EXIT
echo "Building package (arm64 wheels for the Lambda runtime)..."
"$PY" -m pip --version >/dev/null 2>&1 || { echo "$PY has no pip; set PYTHON to an interpreter with pip" >&2; exit 1; }
# From the hash-locked file (ci/lock/update.sh regenerates it from
# backend/requirements-lark-lambda.txt): this package faces the internet as
# the Lark ingress, so a replaced upload on the index fails the install
# instead of shipping. No pip configuration can add another index (dependency
# confusion): --isolated drops the PIP_* variables and the user file,
# PIP_CONFIG_FILE=/dev/null every configuration file (global and site too,
# which --isolated still reads), and the index is named explicitly.
# ci/lock/check-pypi.py first confirms every pin is a pypi.org release and
# every hash one of its files (it refuses what it cannot read).
if ! confirm="$("$PY" -I "$ROOT/ci/lock/check-pypi.py" "$LOCK" 2>&1)"; then
  printf '%s\n' "$confirm" | grep -v '^ok ' >&2 || true
  echo "refusing: the lock is not confirmed against pypi.org" >&2
  exit 1
fi
PIP_CONFIG_FILE=/dev/null "$PY" -m pip install --quiet --isolated --require-hashes --index-url https://pypi.org/simple \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --target "$BUILD" \
  -r "$LOCK"
cp -R "$ROOT/backend/app" "$BUILD/app"
cp "$ROOT/backend/lambda/lark_webhook.py" "$ROOT/backend/lambda/lark_memory.py" "$BUILD/"
find "$BUILD" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# zip(1) is not on every build image; Python's zipfile writes the same archive
if command -v zip >/dev/null 2>&1; then
  (cd "$BUILD" && zip -qr "$BUILD.zip" .)
else
  (cd "$BUILD" && "$PY" -m zipfile -c "$BUILD.zip" ./*)
fi
for f in $FN $MEM_FN; do
  echo "Package: $(du -h "$BUILD.zip" | cut -f1), updating $f"
  aws lambda update-function-code --function-name "$f" \
    --zip-file "fileb://$BUILD.zip" --region "$REGION" --no-cli-pager >/dev/null
  aws lambda wait function-updated --function-name "$f" --region "$REGION"
done
echo "lark functions deployed."

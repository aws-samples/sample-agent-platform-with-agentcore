#!/bin/bash
# Run every phase in order. Each script is idempotent, so this is safe to re-run
# and is the closest equivalent to `terraform apply` this port has.
#
# Ordering is NOT derived from anything — it is this list. Terraform builds a
# dependency graph and parallelises what it can; here the order is hand-encoded
# and everything is serial.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "### phase 1/7  network"        ; bash "$HERE/10-network.sh"
echo "### phase 2/7  platform"       ; bash "$HERE/20-platform.sh"
# The cluster needs no image, so it comes up while images may still be building.
echo "### phase 2b/7 eks"            ; bash "$HERE/25-eks.sh"
echo
echo "### images must exist in the -cli ECR repos before phase 3:"
echo "###   AgentCore validates image access at runtime-create time."
echo "###   Build/push them (arm64) then continue."
echo
echo "### phase 3/7  runtime"        ; bash "$HERE/30-runtime.sh"
# Only for the "litellm" model backend: it holds the gateway key so no kernel
# container ever receives one. A Bedrock-direct deployment has no such key and
# leaves ENABLE_LLM_EDGE unset, which also leaves PLATFORM_LLM_EDGE_URL empty in
# phase 6 — the backend then refuses gateway routing instead of falling back.
if [ "${ENABLE_LLM_EDGE:-0}" = "1" ]; then
  echo "### phase 3b/7 llm-edge"     ; bash "$HERE/35-llm-edge.sh"
else
  echo "### phase 3b/7 llm-edge      skipped (set ENABLE_LLM_EDGE=1 for the litellm backend)"
fi
echo "### phase 4/7  portal base"    ; bash "$HERE/40-portal-base.sh"
echo "### phase 5/7  portal app"     ; bash "$HERE/50-portal-app.sh"
echo "### phase 6/7  cloudfront+eks" ; bash "$HERE/60-cloudfront-eks.sh"
echo "### phase 7/7  service entry"  ; bash "$HERE/70-service-entry.sh"

. "$HERE/../lib/common.sh"; load
echo
echo "portal:      ${PORTAL_URL:-?}"
echo "service api: ${SERVICE_API_URL:-?}"
echo "state file:  $STATE_FILE"

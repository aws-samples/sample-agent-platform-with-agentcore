#!/bin/bash
# Run every phase in order. Each script is idempotent, so this is safe to re-run
# and is the closest equivalent to `terraform apply` this port has.
#
# Ordering is NOT derived from anything — it is this list. Terraform builds a
# dependency graph and parallelises what it can; here the order is hand-encoded
# and everything is serial.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "### phase 1/6  network"        ; bash "$HERE/10-network.sh"
echo "### phase 2/6  platform"       ; bash "$HERE/20-platform.sh"
echo
echo "### images must exist in the -cli ECR repos before phase 3:"
echo "###   AgentCore validates image access at runtime-create time."
echo "###   Build/push them (arm64) then continue."
echo
echo "### phase 3/6  runtime"        ; bash "$HERE/30-runtime.sh"
echo "### phase 4/6  portal base"    ; bash "$HERE/40-portal-base.sh"
echo "### phase 5/6  portal app"     ; bash "$HERE/50-portal-app.sh"
echo "### phase 6/6  cloudfront+ecs" ; bash "$HERE/60-cloudfront-ecs.sh"
echo "### phase 6b   service entry"  ; bash "$HERE/70-service-entry.sh"

. "$HERE/../lib/common.sh"; load
echo
echo "portal:      ${PORTAL_URL:-?}"
echo "service api: ${SERVICE_API_URL:-?}"
echo "state file:  $STATE_FILE"

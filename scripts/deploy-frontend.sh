#!/bin/bash
# Build the frontend and publish it to the portal bucket + CloudFront.
#
# Under the maintained pipeline this happens in the apply builds
# (ci/codebuild/publish-frontend.sh); this script is the workstation
# equivalent for a hand deployment. The bucket and distribution come from the
# foundation's facts document (/agent-platform<suffix>/foundation,
# portal.frontend_bucket_name / portal.distribution_id); PLATFORM_NAME_SUFFIX
# selects the environment ("" = production, "-staging" = staging). A
# CloudFormation deployment (the legacy CDK stacks) is tried when no facts
# document exists.
#
# Usage: ./scripts/deploy-frontend.sh
#        PLATFORM_NAME_SUFFIX=-staging ./scripts/deploy-frontend.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region)}"
PY="${PYTHON:-python3}"
SUFFIX="${PLATFORM_NAME_SUFFIX:-}"

BUCKET=""; DIST_ID=""
facts="$(aws ssm get-parameter --name "/agent-platform${SUFFIX}/foundation" --region "$REGION" \
  --query Parameter.Value --output text 2>/dev/null || true)"
if [ -n "$facts" ] && [ "$facts" != "None" ]; then
  BUCKET="$(printf '%s' "$facts" | "$PY" "$ROOT/scripts/foundation_facts.py" get /dev/stdin frontend_bucket_name)"
  DIST_ID="$(printf '%s' "$facts" | "$PY" "$ROOT/scripts/foundation_facts.py" get /dev/stdin distribution_id)"
  echo "facts: /agent-platform${SUFFIX}/foundation"
else
  BUCKET=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" --output text 2>/dev/null || true)
  DIST_ID=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text 2>/dev/null || true)
  echo "no facts document; using the CloudFormation stack AgentPlatformPortal"
fi
if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ] || [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
  echo "frontend bucket / distribution not found: the facts document predates the frontend keys (re-apply the foundation) or no portal is deployed" >&2
  exit 1
fi

echo "Bucket: $BUCKET  Distribution: $DIST_ID"
# DRY_RUN=1: resolve the targets and stop (the deploy-scripts test uses this)
[ -z "${DRY_RUN:-}" ] || exit 0

cd "$ROOT/frontend"
npm ci
npm run build

# hashed assets are immutable → cache them hard; index.html must always be
# revalidated or browsers keep loading a stale bundle after releases
aws s3 sync dist/assets/ "s3://${BUCKET}/assets/" --delete \
  --cache-control "public, max-age=31536000, immutable"
aws s3 sync dist/ "s3://${BUCKET}/" --exclude "assets/*" --delete \
  --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
echo "Frontend deployed."

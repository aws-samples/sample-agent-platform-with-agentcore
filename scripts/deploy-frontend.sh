#!/bin/bash
# Build the frontend and publish it to the PortalStack bucket + CloudFront.
#
# Usage: ./scripts/deploy-frontend.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region)}"

BUCKET=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" --output text)
DIST_ID=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)

echo "Bucket: $BUCKET  Distribution: $DIST_ID"

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

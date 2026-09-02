#!/bin/bash
# Phase 5 — CloudFront (OAC + SPA function + origin-verify header), then the
# backend Deployment on EKS. CloudFront comes first because the backend's CORS
# origin is its domain, which does not exist until the distribution does —
# terraform resolves that ordering from the graph; here it is script order.
. "$(dirname "$0")/../lib/common.sh"
load
: "${ALB_DNS:?run 40-portal-base.sh}"; : "${TASK_ROLE:?run 50-portal-app.sh}"
: "${SVC_SG:?run 40-portal-base.sh}"
need_tools kubectl helm

step "cloudfront + eks backend ($NAME)"

# ------------------------------------------------------------------ OAC
OAC_ID="$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='$NAME-frontend'].Id | [0]" --output text 2>/dev/null || echo None)"
if [ "$OAC_ID" = "None" ] || [ -z "$OAC_ID" ]; then
  OAC_ID="$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "Name=$NAME-frontend,Description=frontend OAC,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query 'OriginAccessControl.Id' --output text)"
  log "created OAC $OAC_ID"
else
  log "OAC exists $OAC_ID"
fi
save OAC_ID "$OAC_ID"

# ------------------------------------------------------------------ SPA function
FN_NAME="$NAME-spa-rewrite"
FN_ETAG="$(aws cloudfront describe-function --name "$FN_NAME" --query 'ETag' --output text 2>/dev/null || echo None)"
if [ "$FN_ETAG" = "None" ]; then
  cat > /tmp/spa.js <<'JS'
function handler(event) {
  var request = event.request;
  if (!request.uri.includes('.')) { request.uri = '/index.html'; }
  return request;
}
JS
  FN_ARN="$(aws cloudfront create-function --name "$FN_NAME" \
    --function-config "Comment=SPA rewrite,Runtime=cloudfront-js-2.0" \
    --function-code fileb:///tmp/spa.js --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)"
  ETAG="$(aws cloudfront describe-function --name "$FN_NAME" --query ETag --output text)"
  aws cloudfront publish-function --name "$FN_NAME" --if-match "$ETAG" >/dev/null
  log "created+published SPA function"
else
  FN_ARN="$(aws cloudfront describe-function --name "$FN_NAME" --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)"
  log "SPA function exists"
fi
save SPA_FN_ARN "$FN_ARN"

# managed policy ids (stable, but resolve them rather than hardcoding)
CACHE_OPT="$(aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingOptimized'].CachePolicy.Id | [0]" --output text)"
CACHE_DIS="$(aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingDisabled'].CachePolicy.Id | [0]" --output text)"
ORP_ALL="$(aws cloudfront list-origin-request-policies --type managed \
  --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='Managed-AllViewerExceptHostHeader'].OriginRequestPolicy.Id | [0]" --output text)"
log "cache policies: opt=$CACHE_OPT disabled=$CACHE_DIS orp=$ORP_ALL"

# ------------------------------------------------------------------ distribution
DIST_ID="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='$NAME'].Id | [0]" --output text 2>/dev/null || echo None)"
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  python3 - "$NAME" "$FRONTEND_BUCKET" "$AWS_REGION" "$ALB_DNS" "$OAC_ID" "$FN_ARN" \
           "$CACHE_OPT" "$CACHE_DIS" "$ORP_ALL" "$ORIGIN_VERIFY" > /tmp/dist.json <<'PY'
import json, sys
name, bucket, region, alb, oac, fn, copt, cdis, orp, verify = sys.argv[1:11]
api_behavior = {
    "TargetOriginId": "backend-alb",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 7,
        "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
        "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
    "CachePolicyId": cdis,
    "OriginRequestPolicyId": orp,
    "Compress": True,
}
cfg = {
  "CallerReference": f"{name}-1",
  "Comment": name,
  "Enabled": True,
  "DefaultRootObject": "index.html",
  "IsIPV6Enabled": True,
  "Origins": {"Quantity": 2, "Items": [
    {"Id": "frontend-s3",
     "DomainName": f"{bucket}.s3.{region}.amazonaws.com",
     "OriginAccessControlId": oac,
     "S3OriginConfig": {"OriginAccessIdentity": ""}},
    {"Id": "backend-alb", "DomainName": alb,
     # Proves to the ALB that the request came through THIS distribution; the
     # listener default-denies without it.
     "CustomHeaders": {"Quantity": 1, "Items": [
        {"HeaderName": "x-origin-verify", "HeaderValue": verify}]},
     "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443,
        "OriginProtocolPolicy": "http-only",
        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
        "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}},
  ]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "frontend-s3",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 2, "Items": ["GET","HEAD"],
                       "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
    "CachePolicyId": copt,
    "Compress": True,
    "FunctionAssociations": {"Quantity": 1, "Items": [
        {"EventType": "viewer-request", "FunctionARN": fn}]},
  },
  "CacheBehaviors": {"Quantity": 2, "Items": [
    dict(api_behavior, PathPattern="/api/*"),
    dict(api_behavior, PathPattern="/health"),
  ]},
  "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
  "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
}
json.dump({"DistributionConfig": cfg}, sys.stdout)
PY
  DIST_ID="$(aws cloudfront create-distribution --cli-input-json file:///tmp/dist.json \
    --query 'Distribution.Id' --output text)"
  log "created distribution $DIST_ID"
else
  log "distribution exists $DIST_ID"
fi
save DIST_ID "$DIST_ID"
DIST_DOMAIN="$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)"
DIST_ARN="arn:aws:cloudfront::$ACCOUNT_ID:distribution/$DIST_ID"
save DIST_DOMAIN "$DIST_DOMAIN"; save PORTAL_URL "https://$DIST_DOMAIN"

# frontend bucket policy: OAC read pinned to THIS distribution + TLS only
cat > /tmp/fe-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"AllowCloudFrontOAC","Effect":"Allow","Principal":{"Service":"cloudfront.amazonaws.com"},
  "Action":"s3:GetObject","Resource":"arn:aws:s3:::$FRONTEND_BUCKET/*",
  "Condition":{"StringEquals":{"AWS:SourceArn":"$DIST_ARN"}}},
 {"Sid":"EnforceTLS","Effect":"Deny","Principal":{"AWS":"*"},"Action":"s3:*",
  "Resource":["arn:aws:s3:::$FRONTEND_BUCKET","arn:aws:s3:::$FRONTEND_BUCKET/*"],
  "Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
aws s3api put-bucket-policy --bucket "$FRONTEND_BUCKET" --policy file:///tmp/fe-pol.json
log "frontend bucket policy pinned to $DIST_ID"

# ------------------------------------------------------------------ EKS backend
# One Deployment registered into both target groups (the ALB behind CloudFront
# and the NLB behind the private service-entry API), the shape the CLI port
# has always had; the terraform port splits the entry path into its own
# ENTRY_ONLY Deployment. Pods carry SVC_SG through a SecurityGroupPolicy and
# assume TASK_ROLE through IRSA.
BACKEND_IMAGE="${REGISTRY}/agent-platform${SUFFIX}/backend:${IMAGE_TAG}"
python3 - "$BACKEND_IMAGE" "$TASK_ROLE" "$TG_ALB" "$TG_NLB" > /tmp/backend-values.yaml <<PY
import json, sys
image, role, tg_alb, tg_nlb = sys.argv[1:5]
env = {
  "PLATFORM_AWS_REGION": "$AWS_REGION",
  "PLATFORM_DYNAMO_TABLE": "$TABLE",
  "PLATFORM_WORKSPACE_BUCKET": "$WORKSPACE_BUCKET",
  "PLATFORM_INTERACTIVE_RUNTIME_ARN": "$INTERACTIVE_RUNTIME_ARN",
  "PLATFORM_SDK_RUNTIME_ARN": "$SDK_RUNTIME_ARN",
  "PLATFORM_MCP_TOOLS_RUNTIME_ARN": "$MCP_TOOLS_RUNTIME_ARN",
  "PLATFORM_WORKSPACE_ACCESS_ROLE_ARN": "$WORKSPACE_ROLE_ARN",
  # Empty unless 35-llm-edge.sh ran. Empty means gateway model routing is
  # unavailable and the backend refuses it, rather than falling back to handing
  # the gateway key to a container.
  "PLATFORM_LLM_EDGE_URL": "${LLM_EDGE_URL:-}",
  # Scoped to this distribution, not "*" — matches the upstream hardening.
  "PLATFORM_CORS_ORIGINS": "https://$DIST_DOMAIN",
  "PLATFORM_COGNITO_POOL_ID": "$POOL_ID",
  "PLATFORM_COGNITO_CLIENT_ID": "$CLIENT_ID",
  "PLATFORM_SCHEDULER_GROUP": "$SCHED_GROUP",
  "PLATFORM_SCHEDULER_LAMBDA_ARN": "$LAMBDA_ARN",
  "PLATFORM_SCHEDULER_ROLE_ARN": "$SCHED_ROLE",
  "PLATFORM_SCHEDULER_DLQ_ARN": "$DLQ_ARN",
  "PLATFORM_SERVICE_ENTRY_SECRET_NAME": "$ENTRY_SECRET",
  "PLATFORM_PORTAL_API_URL": "https://$DIST_DOMAIN",
}
print(json.dumps({
  "name": "backend", "image": image, "replicas": 2, "port": 8000,
  "env": env,
  "serviceAccount": {"roleArn": role},
  "probe": {"path": "/health",
            "readiness": {"initialDelaySeconds": 5, "periodSeconds": 10, "failureThreshold": 3},
            "liveness": {"initialDelaySeconds": 30, "periodSeconds": 20, "failureThreshold": 3},
            "startup": {"enabled": False, "periodSeconds": 10, "failureThreshold": 30}},
  # ECS ran the backend at 0.5 vCPU / 1 GiB.
  "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
  "targetGroups": [{"arn": tg_alb, "port": 8000}, {"arn": tg_nlb, "port": 8000}],
}))
PY
workload_install backend portal /tmp/backend-values.yaml "$SVC_SG"
save BACKEND_NAMESPACE portal
save BACKEND_DEPLOYMENT backend

log "cloudfront+eks done — portal will be https://$DIST_DOMAIN"

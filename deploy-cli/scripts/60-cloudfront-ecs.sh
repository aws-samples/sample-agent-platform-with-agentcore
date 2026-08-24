#!/bin/bash
# Phase 5 — CloudFront (OAC + SPA function + origin-verify header), then the ECS
# cluster/task-definition/service. CloudFront comes first because the backend's
# CORS origin is its domain, which does not exist until the distribution does —
# terraform resolves that ordering from the graph; here it is script order.
. "$(dirname "$0")/../lib/common.sh"
load
: "${ALB_DNS:?run 40-portal-base.sh}"; : "${TASK_ROLE:?run 50-portal-app.sh}"
# Recompute rather than require it in state, so this phase is runnable standalone.
LOG_GROUP="${LOG_GROUP:-/ecs/agent-platform-backend${SUFFIX}}"

step "cloudfront + ecs ($NAME)"
PRIV0="${PRIVATE_SUBNETS%%,*}"; PRIV1="${PRIVATE_SUBNETS##*,}"

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

# ------------------------------------------------------------------ ECS
# AWSServiceRoleForECS is account-global and CloudFormation creates it implicitly,
# so the CDK and terraform paths never have to name it. On an account that has
# never run ECS in any region, create-service below fails with "Unable to assume
# the service linked role" instead. Needs iam:CreateServiceLinkedRole (§1.3).
if ! aws iam get-role --role-name AWSServiceRoleForECS >/dev/null 2>&1; then
  log "creating the ECS service-linked role (first ECS use in this account)"
  aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null 2>&1 || true
  # Creation is asynchronous: create-service keeps failing until ECS can assume it.
  retry_until "the ECS service-linked role to exist" 12 5 \
    aws iam get-role --role-name AWSServiceRoleForECS \
    || die "AWSServiceRoleForECS is missing. Have an admin run: aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com"
fi

CLUSTER="agent-platform${SUFFIX}"
aws ecs create-cluster --cluster-name "$CLUSTER" >/dev/null 2>&1 || true
save CLUSTER "$CLUSTER"

BACKEND_IMAGE="${REGISTRY}/agent-platform${SUFFIX}/backend:${IMAGE_TAG}"
SVC_API_ID_PLACEHOLDER="${SERVICE_API_ID:-}"
python3 - > /tmp/taskdef.json <<PY
import json, os
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
task = {
  "family": "agent-platform-backend${SUFFIX}",
  "cpu": "512", "memory": "1024",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "taskRoleArn": "$TASK_ROLE",
  "executionRoleArn": "$EXEC_ROLE",
  "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
  "containerDefinitions": [{
    "name": "backend",
    "image": "$BACKEND_IMAGE",
    "essential": True,
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [{"name": k, "value": v} for k, v in sorted(env.items())],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "$LOG_GROUP",
      "awslogs-region": "$AWS_REGION",
      "awslogs-stream-prefix": "backend"}},
  }],
}
print(json.dumps(task))
PY
TD_ARN="$(aws ecs register-task-definition --cli-input-json file:///tmp/taskdef.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
save TD_ARN "$TD_ARN"
log "registered task definition"

SVC="agent-platform-backend${SUFFIX}"
EXISTING="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SVC" \
  --query 'services[?status==`ACTIVE`].serviceName | [0]' --output text 2>/dev/null || echo None)"
if [ "$EXISTING" = "None" ] || [ -z "$EXISTING" ]; then
  aws ecs create-service --cluster "$CLUSTER" --service-name "$SVC" \
    --task-definition "$TD_ARN" --desired-count 2 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$PRIV0,$PRIV1],securityGroups=[$SVC_SG],assignPublicIp=DISABLED}" \
    --load-balancers "targetGroupArn=$TG_ALB,containerName=backend,containerPort=8000" \
                     "targetGroupArn=$TG_NLB,containerName=backend,containerPort=8000" \
    --deployment-configuration 'deploymentCircuitBreaker={enable=true,rollback=true}' >/dev/null
  log "created ecs service (2 tasks, circuit breaker on)"
else
  aws ecs update-service --cluster "$CLUSTER" --service "$SVC" --task-definition "$TD_ARN" >/dev/null
  log "updated ecs service to new task definition"
fi
save SERVICE "$SVC"

log "cloudfront+ecs done — portal will be https://$DIST_DOMAIN"

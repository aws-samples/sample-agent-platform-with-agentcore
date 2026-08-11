#!/bin/bash
# Phase 2 — platform: workspace bucket, logs bucket, DynamoDB, ECR repos,
# LLM-gateway secret. Mirrors terraform modules/platform (incl. the hardening
# from upstream 9e17d98: PITR, versioning, scan-on-push, access-log bucket).
. "$(dirname "$0")/../lib/common.sh"
load

step "platform ($NAME)"

# ------------------------------------------------------------------ buckets
bucket_ensure() {  # name
  local b="$1"
  if aws s3api head-bucket --bucket "$b" >/dev/null 2>&1; then
    log "bucket exists $b" >&2; return 0
  fi
  # us-east-1 must NOT be given a LocationConstraint; every other region must.
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$b" >/dev/null
  else
    aws s3api create-bucket --bucket "$b" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
  log "created bucket $b" >&2
}

harden_bucket() {  # name
  local b="$1"
  aws s3api put-public-access-block --bucket "$b" --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' >/dev/null
  aws s3api put-bucket-encryption --bucket "$b" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
}

bucket_ensure "$WORKSPACE_BUCKET"
harden_bucket "$WORKSPACE_BUCKET"
aws s3api put-bucket-versioning --bucket "$WORKSPACE_BUCKET" \
  --versioning-configuration Status=Enabled >/dev/null
log "workspace bucket ready (versioned, encrypted, private)"

# TLS-only policy on the workspace bucket
cat > /tmp/ws-policy.json <<JSON
{"Version":"2012-10-17","Statement":[{
  "Sid":"EnforceTLS","Effect":"Deny","Principal":{"AWS":"*"},"Action":"s3:*",
  "Resource":["arn:aws:s3:::$WORKSPACE_BUCKET","arn:aws:s3:::$WORKSPACE_BUCKET/*"],
  "Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
aws s3api put-bucket-policy --bucket "$WORKSPACE_BUCKET" --policy file:///tmp/ws-policy.json
save WORKSPACE_BUCKET "$WORKSPACE_BUCKET"

# ---- logs bucket (ALB access logs + CloudFront vended logs) ----
bucket_ensure "$LOGS_BUCKET"
harden_bucket "$LOGS_BUCKET"
# ALB log delivery writes with an ACL, so the bucket cannot be BucketOwnerEnforced
aws s3api put-bucket-ownership-controls --bucket "$LOGS_BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerPreferred}]' >/dev/null
aws s3api put-bucket-lifecycle-configuration --bucket "$LOGS_BUCKET" \
  --lifecycle-configuration '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":90}}]}' >/dev/null

# NB: the ALB statements use the logdelivery service principal (current model)
# rather than the per-region ELB account id, matching what upstream terraform does.
cat > /tmp/logs-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"AlbLogDelivery","Effect":"Allow",
  "Principal":{"Service":"logdelivery.elasticloadbalancing.amazonaws.com"},
  "Action":"s3:PutObject","Resource":"arn:aws:s3:::$LOGS_BUCKET/alb/*",
  "Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT_ID"}}},
 {"Sid":"AlbLogDeliveryAcl","Effect":"Allow",
  "Principal":{"Service":"logdelivery.elasticloadbalancing.amazonaws.com"},
  "Action":"s3:GetBucketAcl","Resource":"arn:aws:s3:::$LOGS_BUCKET"},
 {"Sid":"CloudFrontVendedLogsWrite","Effect":"Allow",
  "Principal":{"Service":"delivery.logs.amazonaws.com"},
  "Action":"s3:PutObject","Resource":"arn:aws:s3:::$LOGS_BUCKET/*",
  "Condition":{"StringEquals":{"s3:x-amz-acl":"bucket-owner-full-control","aws:SourceAccount":"$ACCOUNT_ID"}}},
 {"Sid":"CloudFrontVendedLogsAclCheck","Effect":"Allow",
  "Principal":{"Service":"delivery.logs.amazonaws.com"},
  "Action":["s3:GetBucketAcl","s3:ListBucket"],"Resource":"arn:aws:s3:::$LOGS_BUCKET",
  "Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT_ID"}}},
 {"Sid":"EnforceTLS","Effect":"Deny","Principal":{"AWS":"*"},"Action":"s3:*",
  "Resource":["arn:aws:s3:::$LOGS_BUCKET","arn:aws:s3:::$LOGS_BUCKET/*"],
  "Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
aws s3api put-bucket-policy --bucket "$LOGS_BUCKET" --policy file:///tmp/logs-policy.json
save LOGS_BUCKET "$LOGS_BUCKET"
log "logs bucket ready (90d expiry, delivery principals allowed)"

# ---- frontend bucket (CloudFront OAC reads it; policy set in the portal phase,
# which is where the distribution ARN it must be pinned to first exists) ----
bucket_ensure "$FRONTEND_BUCKET"
harden_bucket "$FRONTEND_BUCKET"
save FRONTEND_BUCKET "$FRONTEND_BUCKET"

# ------------------------------------------------------------------ dynamodb
if aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
  log "table exists $TABLE"
else
  aws dynamodb create-table --table-name "$TABLE" \
    --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
    --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --sse-specification Enabled=true >/dev/null
  log "creating table $TABLE"
  aws dynamodb wait table-exists --table-name "$TABLE"
fi
# PITR can only be set once the table is ACTIVE, and the call is not idempotent
# in the sense of being safe to blind-fire: it errors if already enabled.
aws dynamodb update-continuous-backups --table-name "$TABLE" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null 2>&1 \
  || log "PITR already enabled"
save TABLE "$TABLE"
log "table ready (PITR + SSE)"

# ------------------------------------------------------------------ ecr
for r in "${KERNEL_REPOS[@]}"; do
  repo="agent-platform${SUFFIX}/${r}"
  if aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
    log "ecr exists $repo"
  else
    aws ecr create-repository --repository-name "$repo" \
      --image-scanning-configuration scanOnPush=true >/dev/null
    log "created ecr $repo (scan-on-push)"
  fi
done
save ECR_PREFIX "agent-platform${SUFFIX}"

# ------------------------------------------------------------------ secrets
secret_ensure() {  # name json-value
  local n="$1" v="$2"
  if aws secretsmanager describe-secret --secret-id "$n" >/dev/null 2>&1; then
    log "secret exists $n" >&2
  else
    aws secretsmanager create-secret --name "$n" --secret-string "$v" >/dev/null
    log "created secret $n" >&2
  fi
}
# Placeholder only — the real key is set out of band, same as terraform's
# ignore_changes on the version.
secret_ensure "$LLM_SECRET" '{"api_key":"REPLACE_ME"}'
save LLM_SECRET "$LLM_SECRET"

log "platform done"

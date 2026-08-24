#!/bin/bash
# Tear down the CLI environment, in reverse dependency order.
#
# Deliberately NOT a mirror of the deploy scripts: several resources refuse to go
# while something references them, and a few need a disable-then-wait step that
# has no counterpart on the way up. Run with CONFIRM=yes.
#
# Order matters: ECS service (drain) -> ALB/NLB listeners+LBs -> target groups ->
# CloudFront (disable, wait, delete) -> API GW + VPC Link -> runtimes -> Lambda/
# scheduler -> Cognito -> buckets -> table -> ECR -> IAM -> NAT -> subnets -> VPC.
set -uo pipefail
. "$(dirname "$0")/../lib/common.sh"
load

[ "${CONFIRM:-no}" = "yes" ] || die "refusing to destroy without CONFIRM=yes"
step "DESTROY $NAME"

t() { "$@" >/dev/null 2>&1 && log "ok: $*" || log "skip/failed: $*"; }

# ---- ECS ----
if [ -n "${CLUSTER:-}" ] && [ -n "${SERVICE:-}" ]; then
  t aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0
  log "waiting for tasks to drain…"
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" 2>/dev/null || true
  t aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force
  t aws ecs delete-cluster --cluster "$CLUSTER"
fi

# ---- llm-edge ECS (its own cluster; absent on Bedrock-direct deployments) ----
if [ -n "${EDGE_CLUSTER:-}" ] && [ -n "${EDGE_SERVICE:-}" ]; then
  t aws ecs update-service --cluster "$EDGE_CLUSTER" --service "$EDGE_SERVICE" --desired-count 0
  log "waiting for llm-edge tasks to drain…"
  aws ecs wait services-stable --cluster "$EDGE_CLUSTER" --services "$EDGE_SERVICE" 2>/dev/null || true
  t aws ecs delete-service --cluster "$EDGE_CLUSTER" --service "$EDGE_SERVICE" --force
  t aws ecs delete-cluster --cluster "$EDGE_CLUSTER"
fi

# ---- load balancers (listeners die with the LB; target groups must follow it) ----
for arn in "${ALB_ARN:-}" "${NLB_ARN:-}" "${EDGE_ALB:-}"; do
  [ -n "$arn" ] && t aws elbv2 delete-load-balancer --load-balancer-arn "$arn"
done
sleep 25   # target groups stay "in use" briefly after the LB goes
for arn in "${TG_ALB:-}" "${TG_NLB:-}" "${EDGE_TG:-}"; do
  [ -n "$arn" ] && t aws elbv2 delete-target-group --target-group-arn "$arn"
done

# ---- CloudFront: must be disabled and fully deployed before it can be deleted ----
if [ -n "${DIST_ID:-}" ]; then
  ETAG="$(aws cloudfront get-distribution-config --id "$DIST_ID" --query ETag --output text 2>/dev/null || echo '')"
  if [ -n "$ETAG" ]; then
    aws cloudfront get-distribution-config --id "$DIST_ID" \
      --query 'DistributionConfig' > /tmp/d.json 2>/dev/null
    python3 -c "
import json;d=json.load(open('/tmp/d.json'));d['Enabled']=False
json.dump(d,open('/tmp/d.json','w'))"
    t aws cloudfront update-distribution --id "$DIST_ID" --distribution-config file:///tmp/d.json --if-match "$ETAG"
    log "waiting for distribution to finish disabling (can take ~5 min)…"
    aws cloudfront wait distribution-deployed --id "$DIST_ID" 2>/dev/null || true
    ETAG2="$(aws cloudfront get-distribution-config --id "$DIST_ID" --query ETag --output text 2>/dev/null || echo '')"
    t aws cloudfront delete-distribution --id "$DIST_ID" --if-match "$ETAG2"
  fi
fi
t aws cloudfront delete-function --name "$NAME-spa-rewrite" \
  --if-match "$(aws cloudfront describe-function --name "$NAME-spa-rewrite" --query ETag --output text 2>/dev/null || echo x)"
[ -n "${OAC_ID:-}" ] && t aws cloudfront delete-origin-access-control --id "$OAC_ID" \
  --if-match "$(aws cloudfront get-origin-access-control --id "$OAC_ID" --query ETag --output text 2>/dev/null || echo x)"

# ---- API GW + VPC link ----
[ -n "${SERVICE_API_ID:-}" ] && t aws apigateway delete-rest-api --rest-api-id "$SERVICE_API_ID"
[ -n "${VPCLINK_ID:-}" ] && t aws apigateway delete-vpc-link --vpc-link-id "$VPCLINK_ID"

# ---- AgentCore runtimes ----
for n in claude_code_kernel agent_sdk_kernel mcp_tools_kernel; do
  id="$(aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='${n}${RUNTIME_SUFFIX}'].agentRuntimeId | [0]" --output text 2>/dev/null)"
  [ -n "$id" ] && [ "$id" != "None" ] && t aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$id"
done

# ---- lambda / scheduler / dlq ----
[ -n "${SCHEDULE_FN:-}" ] && t aws lambda delete-function --function-name "$SCHEDULE_FN"
[ -n "${SCHED_GROUP:-}" ] && t aws scheduler delete-schedule-group --name "$SCHED_GROUP"
[ -n "${DLQ_URL:-}" ] && t aws sqs delete-queue --queue-url "$DLQ_URL"

# ---- cognito ----
[ -n "${POOL_ID:-}" ] && t aws cognito-idp delete-user-pool --user-pool-id "$POOL_ID"

# ---- buckets (must be emptied first; versioned buckets need versions purged) ----
for b in "${FRONTEND_BUCKET:-}" "${LOGS_BUCKET:-}" "${WORKSPACE_BUCKET:-}"; do
  [ -z "$b" ] && continue
  log "emptying $b"
  aws s3 rm "s3://$b" --recursive >/dev/null 2>&1 || true
  python3 - "$b" <<'PY' 2>/dev/null || true
import json,subprocess,sys
b=sys.argv[1]
for key in ("Versions","DeleteMarkers"):
    out=subprocess.run(["aws","s3api","list-object-versions","--bucket",b,
        "--query",f"{key}[].{{Key:Key,VersionId:VersionId}}","--output","json"],
        capture_output=True,text=True).stdout or "[]"
    items=json.loads(out) or []
    for i in range(0,len(items),900):
        batch={"Objects":items[i:i+900],"Quiet":True}
        subprocess.run(["aws","s3api","delete-objects","--bucket",b,
            "--delete",json.dumps(batch)],capture_output=True)
PY
  t aws s3api delete-bucket --bucket "$b"
done

# ---- table / ecr ----
[ -n "${TABLE:-}" ] && t aws dynamodb delete-table --table-name "$TABLE"
for r in "${KERNEL_REPOS[@]}" "${SERVICE_REPOS[@]}"; do
  t aws ecr delete-repository --repository-name "agent-platform${SUFFIX}/$r" --force
done

# ---- secrets ----
for s in "${LLM_SECRET:-}" "${ENTRY_SECRET:-}"; do
  [ -n "$s" ] && t aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery
done

# ---- IAM (inline policies must go first) ----
for r in "agent-platform-interactive-role${SUFFIX}" "agent-platform-sdk-role${SUFFIX}" \
         "agent-platform-mcp-tools-role${SUFFIX}" "agent-platform-workspace-access${SUFFIX}" \
         "agent-platform-backend-task${SUFFIX}" "agent-platform-backend-exec${SUFFIX}" \
         "agent-platform-schedule-runner${SUFFIX}" "agent-platform-scheduler${SUFFIX}" \
         "agent-platform-llm-edge${SUFFIX}" "agent-platform-llm-edge-exec${SUFFIX}"; do
  for p in $(aws iam list-role-policies --role-name "$r" --query 'PolicyNames[]' --output text 2>/dev/null); do
    t aws iam delete-role-policy --role-name "$r" --policy-name "$p"
  done
  t aws iam delete-role --role-name "$r"
done

# ---- network (NAT release is slow; the EIP cannot detach until it is gone) ----
[ -n "${NAT_ID:-}" ] && {
  t aws ec2 delete-nat-gateway --nat-gateway-id "$NAT_ID"
  log "waiting for nat to delete…"
  aws ec2 wait nat-gateway-deleted --nat-gateway-ids "$NAT_ID" 2>/dev/null || true
}
[ -n "${EIP_ALLOC:-}" ] && t aws ec2 release-address --allocation-id "$EIP_ALLOC"
[ -n "${IGW_ID:-}" ] && [ -n "${VPC_ID:-}" ] && {
  t aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
  t aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID"
}
for s in $(echo "${PUBLIC_SUBNETS:-},${PRIVATE_SUBNETS:-}" | tr ',' ' '); do
  [ -n "$s" ] && t aws ec2 delete-subnet --subnet-id "$s"
done
for rt in "${RT_PUB:-}" "${RT_PRIV:-}"; do
  [ -n "$rt" ] && t aws ec2 delete-route-table --route-table-id "$rt"
done
for sg in "${ALB_SG:-}" "${SVC_SG:-}" "${EDGE_ALB_SG:-}" "${EDGE_SVC_SG:-}" "${RUNTIME_SG:-}"; do
  [ -n "$sg" ] && t aws ec2 delete-security-group --group-id "$sg"
done
[ -n "${VPC_ID:-}" ] && t aws ec2 delete-vpc --vpc-id "$VPC_ID"

log "destroy pass complete — re-run once if anything reported 'in use'"

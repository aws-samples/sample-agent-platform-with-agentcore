#!/bin/bash
# Phase 4a — portal foundations: Cognito, ALB (+origin-verify secret), target
# groups and the security groups the backend pods will carry.
# Mirrors terraform modules/portal parts 1-3.
. "$(dirname "$0")/../lib/common.sh"
load
: "${VPC_ID:?run 10-network.sh}"; : "${SDK_RUNTIME_ARN:?run 30-runtime.sh}"
: "${CLUSTER_SG:?run 25-eks.sh}"

step "portal base ($NAME)"
PUB0="${PUBLIC_SUBNETS%%,*}"; PUB1="${PUBLIC_SUBNETS##*,}"
PRIV0="${PRIVATE_SUBNETS%%,*}"; PRIV1="${PRIVATE_SUBNETS##*,}"

# ------------------------------------------------------------------ cognito
POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 \
  --query "UserPools[?Name=='$NAME'].Id | [0]" --output text 2>/dev/null || echo None)"
if [ "$POOL_ID" = "None" ] || [ -z "$POOL_ID" ]; then
  POOL_ID="$(aws cognito-idp create-user-pool --pool-name "$NAME" \
    --admin-create-user-config 'AllowAdminCreateUserOnly=true' \
    --alias-attributes email \
    --auto-verified-attributes email \
    --policies 'PasswordPolicy={MinimumLength=12,RequireUppercase=false,RequireLowercase=false,RequireNumbers=false,RequireSymbols=false}' \
    --query 'UserPool.Id' --output text)"
  log "created user pool $POOL_ID"
else
  log "user pool exists $POOL_ID"
fi
save POOL_ID "$POOL_ID"

CLIENT_ID="$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 \
  --query "UserPoolClients[?ClientName=='PortalClient'].ClientId | [0]" --output text 2>/dev/null || echo None)"
if [ "$CLIENT_ID" = "None" ] || [ -z "$CLIENT_ID" ]; then
  CLIENT_ID="$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" \
    --client-name PortalClient \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --id-token-validity 12 --access-token-validity 12 \
    --token-validity-units 'IdToken=hours,AccessToken=hours,RefreshToken=days' \
    --query 'UserPoolClient.ClientId' --output text)"
  log "created pool client $CLIENT_ID"
else
  log "pool client exists $CLIENT_ID"
fi
save CLIENT_ID "$CLIENT_ID"

aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name platform-admin \
  --description "Agent Platform administrators" >/dev/null 2>&1 \
  || log "group platform-admin already exists"

# ------------------------------------------------------ origin-verify secret
# Same role as terraform's random_password.origin_verify: proves to the ALB that
# a request arrived through THIS distribution (the SG prefix list admits every
# CloudFront distribution, not just ours).
if [ -z "${ORIGIN_VERIFY:-}" ]; then
  ORIGIN_VERIFY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(36).replace("-","").replace("_","")[:48])')"
  save ORIGIN_VERIFY "$ORIGIN_VERIFY"
  log "generated origin-verify header value"
else
  log "origin-verify value reused from state"
fi

# ------------------------------------------------------------------ SGs
sg_ensure() {  # name description
  local n="$1" d="$2" id
  id="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$n" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"
  if [ "$id" = "None" ]; then
    id="$(aws ec2 create-security-group --group-name "$n" --description "$d" --vpc-id "$VPC_ID" \
          --query GroupId --output text)"
    log "created sg $n -> $id" >&2
  else
    log "sg exists $n -> $id" >&2
  fi
  echo "$id"
}
ALB_SG="$(sg_ensure "$NAME-portal-alb" "ALB - CloudFront origin-facing traffic only")"
# Carried by the backend pods (SecurityGroupPolicy), so these rules read exactly
# as they did for the ECS tasks.
SVC_SG="$(sg_ensure "$NAME-portal-service" "backend service")"
save ALB_SG "$ALB_SG"; save SVC_SG "$SVC_SG"

# CloudFront origin-facing managed prefix list
PL_ID="$(aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text)"
aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,PrefixListIds=[{PrefixListId=$PL_ID,Description=CloudFront origin-facing}]" \
  >/dev/null 2>&1 || log "alb ingress already present"
aws ec2 authorize-security-group-ingress --group-id "$SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8000,ToPort=8000,UserIdGroupPairs=[{GroupId=$ALB_SG,Description=from ALB}]" \
  >/dev/null 2>&1 || log "service ingress from alb already present"
aws ec2 authorize-security-group-ingress --group-id "$SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8000,ToPort=8000,IpRanges=[{CidrIp=$VPC_CIDR,Description=from service-entry NLB}]" \
  >/dev/null 2>&1 || log "service ingress from vpc already present"
# kubelet probes arrive from the node, which carries the cluster security group
# — the one ingress the ECS shape did not have: cluster nodes only, app port only.
aws ec2 authorize-security-group-ingress --group-id "$SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8000,ToPort=8000,UserIdGroupPairs=[{GroupId=$CLUSTER_SG,Description=kubelet probes from cluster nodes}]" \
  >/dev/null 2>&1 || log "service ingress from cluster sg already present"
# In strict enforcing mode a pod's traffic is judged by its own groups only, so
# CoreDNS (running with the cluster security group) has to admit it.
for proto in tcp udp; do
  aws ec2 authorize-security-group-ingress --group-id "$CLUSTER_SG" \
    --ip-permissions "IpProtocol=$proto,FromPort=53,ToPort=53,UserIdGroupPairs=[{GroupId=$SVC_SG,Description=DNS from portal pods}]" \
    >/dev/null 2>&1 || log "cluster sg dns/$proto from portal already present"
done

# ------------------------------------------------------------------ ALBs
alb_ensure() {  # name scheme subnets sg-or-empty type
  local n="$1" scheme="$2" subnets="$3" sg="$4" type="$5" arn extra=""
  arn="$(aws elbv2 describe-load-balancers --names "$n" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo None)"
  if [ "$arn" != "None" ] && [ -n "$arn" ]; then log "lb exists $n" >&2; echo "$arn"; return 0; fi
  [ -n "$sg" ] && extra="--security-groups $sg"
  arn="$(aws elbv2 create-load-balancer --name "$n" --type "$type" --scheme "$scheme" \
        --subnets $subnets $extra --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  log "created $type lb $n" >&2
  echo "$arn"
}
# ALB name has a 32-char limit — "agent-platform-cli-portal" fits, but the
# terraform names plus a longer suffix would not; worth knowing before renaming.
ALB_ARN="$(alb_ensure "${NAME}-portal" internet-facing "$PUB0 $PUB1" "$ALB_SG" application)"
NLB_ARN="$(alb_ensure "${NAME}-svc-entry" internal "$PRIV0 $PRIV1" "" network)"
save ALB_ARN "$ALB_ARN"; save NLB_ARN "$NLB_ARN"

ALB_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --query 'LoadBalancers[0].DNSName' --output text)"
NLB_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$NLB_ARN" --query 'LoadBalancers[0].DNSName' --output text)"
save ALB_DNS "$ALB_DNS"; save NLB_DNS "$NLB_DNS"

# access logs on the ALB (the bucket policy from phase 2 admits the delivery principal)
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$ALB_ARN" --attributes \
  "Key=access_logs.s3.enabled,Value=true" "Key=access_logs.s3.bucket,Value=$LOGS_BUCKET" \
  "Key=access_logs.s3.prefix,Value=alb" "Key=routing.http.drop_invalid_header_fields.enabled,Value=true" \
  >/dev/null && log "alb access logs enabled"

tg_ensure() {  # name protocol port target-type health-path
  local n="$1" proto="$2" port="$3" tt="$4" hp="$5" arn
  arn="$(aws elbv2 describe-target-groups --names "$n" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo None)"
  if [ "$arn" != "None" ] && [ -n "$arn" ]; then log "tg exists $n" >&2; echo "$arn"; return 0; fi
  arn="$(aws elbv2 create-target-group --name "$n" --protocol "$proto" --port "$port" \
        --vpc-id "$VPC_ID" --target-type "$tt" \
        --health-check-protocol HTTP --health-check-path "$hp" \
        --health-check-interval-seconds 30 --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
        --query 'TargetGroups[0].TargetGroupArn' --output text)"
  aws elbv2 modify-target-group-attributes --target-group-arn "$arn" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
  log "created tg $n" >&2
  echo "$arn"
}
TG_ALB="$(tg_ensure "${NAME}-backend" HTTP 8000 ip /health)"
TG_NLB="$(tg_ensure "${NAME}-svc-entry" TCP 8000 ip /health)"
aws elbv2 modify-target-group-attributes --target-group-arn "$TG_NLB" \
  --attributes Key=preserve_client_ip.enabled,Value=false >/dev/null 2>&1 || true
save TG_ALB "$TG_ALB"; save TG_NLB "$TG_NLB"

# ---- listeners: default-deny 403, forward only on the origin-verify header ----
L_ALB="$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || echo None)"
if [ "$L_ALB" = "None" ] || [ -z "$L_ALB" ]; then
  L_ALB="$(aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
    --default-actions 'Type=fixed-response,FixedResponseConfig={MessageBody="Direct origin access is not allowed.",StatusCode=403,ContentType=text/plain}' \
    --query 'Listeners[0].ListenerArn' --output text)"
  log "created alb listener (default-deny)"
else
  log "alb listener exists"
fi
save L_ALB "$L_ALB"

if ! aws elbv2 describe-rules --listener-arn "$L_ALB" --query 'Rules[?Priority==`100`]' --output text | grep -q .; then
  aws elbv2 create-rule --listener-arn "$L_ALB" --priority 100 \
    --conditions "Field=http-header,HttpHeaderConfig={HttpHeaderName=x-origin-verify,Values=[$ORIGIN_VERIFY]}" \
    --actions "Type=forward,TargetGroupArn=$TG_ALB" >/dev/null
  log "created origin-verify forward rule"
else
  log "origin-verify rule exists"
fi

L_NLB="$(aws elbv2 describe-listeners --load-balancer-arn "$NLB_ARN" --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || echo None)"
if [ "$L_NLB" = "None" ] || [ -z "$L_NLB" ]; then
  aws elbv2 create-listener --load-balancer-arn "$NLB_ARN" --protocol TCP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=$TG_NLB" >/dev/null
  log "created nlb listener"
else
  log "nlb listener exists"
fi

log "portal base done (alb=$ALB_DNS)"

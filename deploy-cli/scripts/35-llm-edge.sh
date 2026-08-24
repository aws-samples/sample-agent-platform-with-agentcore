#!/bin/bash
# Phase 3b — llm-edge: the platform-side hop that holds the LLM gateway key.
#
# Why this phase exists at all: a Dev Workbench session hands its user a root
# shell inside the session's microVM, and the headless kernel runs agent tools
# in a subprocess. Anything a kernel container can read, its users can read —
# including the execution role's credentials from the metadata endpoint. So the
# gateway key is not given to a kernel; it lives here, behind a listener with no
# route from outside the VPC, and kernels present a per-session grant the backend
# mints for them.
#
# Only needed for the "litellm" model backend. A Bedrock-direct deployment has
# no gateway key to protect and can skip this phase entirely (see the note at
# the end of 00-deploy-all.sh).
#
# Ordering: after 30-runtime (needs the VPC + runtime SG) and before
# 60-cloudfront-ecs, which bakes PLATFORM_LLM_EDGE_URL into the backend task
# definition.
. "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
load

step "llm-edge ($NAME)"

[ -n "${VPC_ID:-}" ]     || die "VPC_ID missing from state — run 10-network.sh first"
[ -n "${RUNTIME_SG:-}" ] || die "RUNTIME_SG missing from state — run 10-network.sh first"

TABLE_ARN="arn:aws:dynamodb:$AWS_REGION:$ACCOUNT_ID:table/$TABLE"
LLM_ARN="$(aws secretsmanager describe-secret --secret-id "$LLM_SECRET" --query ARN --output text)"
EDGE_LOG_GROUP="/ecs/agent-platform-llm-edge${SUFFIX}"

# ------------------------------------------------------------------ SGs
sg_ensure() {  # name description
  local n="$1" d="$2" id
  id="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$n" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"
  if [ "$id" = "None" ] || [ -z "$id" ]; then
    id="$(aws ec2 create-security-group --group-name "$n" --description "$d" --vpc-id "$VPC_ID" \
          --query GroupId --output text)"
    log "created sg $n" >&2
  else
    log "sg exists $n" >&2
  fi
  echo "$id"
}
EDGE_ALB_SG="$(sg_ensure "${NAME}-llm-edge-alb" "llm-edge listener: reachable only from runtime ENIs")"
EDGE_SVC_SG="$(sg_ensure "${NAME}-llm-edge-task" "llm-edge tasks: from its listener only")"
save EDGE_ALB_SG "$EDGE_ALB_SG"; save EDGE_SVC_SG "$EDGE_SVC_SG"

# Ingress is granted to the runtime security group, not to a CIDR. A CIDR rule
# here would quietly widen to "anything in the VPC" the first time someone
# re-used the subnet range.
aws ec2 authorize-security-group-ingress --group-id "$EDGE_ALB_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,UserIdGroupPairs=[{GroupId=$RUNTIME_SG,Description=model calls from session kernels}]" \
  >/dev/null 2>&1 || log "alb ingress from runtime sg already present"
aws ec2 authorize-security-group-ingress --group-id "$EDGE_SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$EDGE_ALB_SG,Description=from llm-edge listener}]" \
  >/dev/null 2>&1 || log "task ingress from alb sg already present"

# Egress: the upstream gateway is a public endpoint reached over the shared NAT,
# so this cannot be narrowed to an address the deployment owns. Restricted to
# 443; nothing inbound is ever opened to 0.0.0.0/0.
aws ec2 authorize-security-group-egress --group-id "$EDGE_SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=upstream gateway + AWS APIs}]" \
  >/dev/null 2>&1 || log "task egress 443 already present"
# The default allow-all egress rule is what the 443 rule is meant to replace.
aws ec2 revoke-security-group-egress --group-id "$EDGE_SVC_SG" \
  --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]" \
  >/dev/null 2>&1 || true

# ------------------------------------------------------------------ ALB
EDGE_ALB="$(aws elbv2 describe-load-balancers --names "${NAME}-llm-edge" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo None)"
if [ "$EDGE_ALB" = "None" ] || [ -z "$EDGE_ALB" ]; then
  EDGE_ALB="$(aws elbv2 create-load-balancer --name "${NAME}-llm-edge" --type application \
    --scheme internal --subnets "$PRIV0" "$PRIV1" --security-groups "$EDGE_ALB_SG" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  log "created internal alb ${NAME}-llm-edge"
else
  log "alb exists ${NAME}-llm-edge"
fi
save EDGE_ALB "$EDGE_ALB"

# 900s idle: a single model response can stream for many minutes and the 60s
# default would cut it mid-answer. This is also why the edge is not fronted by a
# managed SigV4 validator — VPC Lattice caps a connection at 10 minutes and API
# Gateway buffers responses, either of which truncates a long completion.
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$EDGE_ALB" --attributes \
  "Key=idle_timeout.timeout_seconds,Value=900" \
  "Key=routing.http.drop_invalid_header_fields.enabled,Value=true" \
  "Key=access_logs.s3.enabled,Value=true" \
  "Key=access_logs.s3.bucket,Value=$LOGS_BUCKET" \
  "Key=access_logs.s3.prefix,Value=llm-edge-alb" >/dev/null && log "alb attributes set (idle 900s, access logs)"

EDGE_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$EDGE_ALB" \
  --query 'LoadBalancers[0].DNSName' --output text)"
save EDGE_DNS "$EDGE_DNS"

EDGE_TG="$(aws elbv2 describe-target-groups --names "${NAME}-llm-edge" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo None)"
if [ "$EDGE_TG" = "None" ] || [ -z "$EDGE_TG" ]; then
  EDGE_TG="$(aws elbv2 create-target-group --name "${NAME}-llm-edge" --protocol HTTP --port 8080 \
    --vpc-id "$VPC_ID" --target-type ip \
    --health-check-protocol HTTP --health-check-path /healthz \
    --health-check-interval-seconds 15 --health-check-timeout-seconds 5 \
    --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)"
  aws elbv2 modify-target-group-attributes --target-group-arn "$EDGE_TG" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
  log "created tg ${NAME}-llm-edge"
else
  log "tg exists ${NAME}-llm-edge"
fi
save EDGE_TG "$EDGE_TG"

EDGE_L="$(aws elbv2 describe-listeners --load-balancer-arn "$EDGE_ALB" \
  --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || echo None)"
if [ "$EDGE_L" = "None" ] || [ -z "$EDGE_L" ]; then
  EDGE_L="$(aws elbv2 create-listener --load-balancer-arn "$EDGE_ALB" --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=$EDGE_TG" \
    --query 'Listeners[0].ListenerArn' --output text)"
  log "created listener (HTTP 80 -> 8080)"
else
  log "listener exists"
fi
save EDGE_L "$EDGE_L"

# ------------------------------------------------------------------ IAM
role_ensure() {  # name trust
  local rn="$1" trust="$2" arn
  arn="$(aws iam get-role --role-name "$rn" --query 'Role.Arn' --output text 2>/dev/null || echo None)"
  if [ "$arn" = "None" ]; then
    arn="$(aws iam create-role --role-name "$rn" --assume-role-policy-document "$trust" \
          --query 'Role.Arn' --output text)"
    log "created role $rn" >&2
  else
    log "role exists $rn" >&2
  fi
  echo "$arn"
}
ECS_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
EDGE_TASK_ROLE="$(role_ensure "agent-platform-llm-edge${SUFFIX}" "$ECS_TRUST")"
EDGE_EXEC_ROLE="$(role_ensure "agent-platform-llm-edge-exec${SUFFIX}" "$ECS_TRUST")"
save EDGE_TASK_ROLE "$EDGE_TASK_ROLE"; save EDGE_EXEC_ROLE "$EDGE_EXEC_ROLE"

put_json_policy() {  # role name file
  aws iam put-role-policy --role-name "$1" --policy-name "$2" --policy-document "file://$3"
  log "policy $2 -> $1"
}

# Inline and repo-scoped rather than the AWS managed execution policy: tighter,
# and 99-destroy.sh only removes inline policies, so an attached managed policy
# would leave the role undeletable.
cat > /tmp/llm-edge-exec-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"EcrAuth","Effect":"Allow","Action":"ecr:GetAuthorizationToken","Resource":"*"},
 {"Sid":"EcrPull","Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":"arn:aws:ecr:$AWS_REGION:$ACCOUNT_ID:repository/agent-platform${SUFFIX}/llm-edge"},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:$EDGE_LOG_GROUP:*"}]}
JSON
put_json_policy "agent-platform-llm-edge-exec${SUFFIX}" execution /tmp/llm-edge-exec-pol.json

# The whole point of the phase: this is the only principal in the model data
# path that can read the gateway key. GetItem (not Query, not Scan) is enough to
# authenticate a session, and keeps a bug here from reading the rest of a table
# that also holds sessions, channels, the ledger and the audit log.
cat > /tmp/llm-edge-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"GatewaySecret","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"$LLM_ARN"},
 {"Sid":"SessionTokenLookup","Effect":"Allow","Action":"dynamodb:GetItem","Resource":"$TABLE_ARN"}]}
JSON
put_json_policy "agent-platform-llm-edge${SUFFIX}" llm-edge /tmp/llm-edge-pol.json

# ------------------------------------------------------------------ ECS
aws logs create-log-group --log-group-name "$EDGE_LOG_GROUP" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "$EDGE_LOG_GROUP" --retention-in-days 30 >/dev/null 2>&1 || true
save EDGE_LOG_GROUP "$EDGE_LOG_GROUP"

# Its own cluster, matching the terraform port: the edge has a different blast
# radius from the control plane and is easier to reason about kept separate.
EDGE_CLUSTER="agent-platform-llm-edge${SUFFIX}"
aws ecs create-cluster --cluster-name "$EDGE_CLUSTER" >/dev/null 2>&1 || true
save EDGE_CLUSTER "$EDGE_CLUSTER"

EDGE_IMAGE="${REGISTRY}/agent-platform${SUFFIX}/llm-edge:${IMAGE_TAG}"
python3 - > /tmp/llm-edge-taskdef.json <<PY
import json
task = {
  "family": "agent-platform-llm-edge${SUFFIX}",
  "cpu": "512", "memory": "1024",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "taskRoleArn": "$EDGE_TASK_ROLE",
  "executionRoleArn": "$EDGE_EXEC_ROLE",
  "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
  "containerDefinitions": [{
    "name": "edge",
    "image": "$EDGE_IMAGE",
    "essential": True,
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    # The key is deliberately NOT injected here. The task role fetches it at
    # request time from the secret named on the session's grant, so a per-backend
    # secret override in the model control plane keeps working and the key never
    # becomes part of the task definition.
    "environment": [
      {"name": "PLATFORM_TABLE", "value": "$TABLE"},
      {"name": "AWS_REGION", "value": "$AWS_REGION"},
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "$EDGE_LOG_GROUP",
      "awslogs-region": "$AWS_REGION",
      "awslogs-stream-prefix": "edge"}},
  }],
}
print(json.dumps(task))
PY
EDGE_TD="$(aws ecs register-task-definition --cli-input-json file:///tmp/llm-edge-taskdef.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
save EDGE_TD "$EDGE_TD"
log "registered task definition"

EDGE_SVC="agent-platform-llm-edge${SUFFIX}"
EXISTING="$(aws ecs describe-services --cluster "$EDGE_CLUSTER" --services "$EDGE_SVC" \
  --query 'services[?status==`ACTIVE`].serviceName | [0]' --output text 2>/dev/null || echo None)"
if [ "$EXISTING" = "None" ] || [ -z "$EXISTING" ]; then
  # enableExecuteCommand is left off: ECS Exec would put a shell in the one
  # container that can read the gateway key. Debugging goes through logs.
  aws ecs create-service --cluster "$EDGE_CLUSTER" --service-name "$EDGE_SVC" \
    --task-definition "$EDGE_TD" --desired-count 2 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$PRIV0,$PRIV1],securityGroups=[$EDGE_SVC_SG],assignPublicIp=DISABLED}" \
    --load-balancers "targetGroupArn=$EDGE_TG,containerName=edge,containerPort=8080" \
    --deployment-configuration 'deploymentCircuitBreaker={enable=true,rollback=true}' >/dev/null
  log "created ecs service (2 tasks, circuit breaker on)"
else
  aws ecs update-service --cluster "$EDGE_CLUSTER" --service "$EDGE_SVC" \
    --task-definition "$EDGE_TD" >/dev/null
  log "updated ecs service to new task definition"
fi
save EDGE_SERVICE "$EDGE_SVC"

# Consumed by 60-cloudfront-ecs.sh as PLATFORM_LLM_EDGE_URL. Plain http on an
# internal listener: set up TLS with a certificate for a name you own if the
# prompt content on this leg needs it (see docs/deployment-aws-cli.md).
save LLM_EDGE_URL "http://$EDGE_DNS"

log "llm-edge done — $LLM_EDGE_URL"
log "the gateway key is now readable only by agent-platform-llm-edge${SUFFIX}"

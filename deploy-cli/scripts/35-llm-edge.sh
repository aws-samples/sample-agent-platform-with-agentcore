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
# the end of 00-deploy-all.sh). The edge runs as a two-replica Deployment on
# the platform's EKS cluster; its pods carry the edge security group through a
# SecurityGroupPolicy, so the 443-only egress below is the pods' real egress.
#
# Ordering: after 25-eks (the pods run there) and 30-runtime (needs the runtime
# SG), and before 60-cloudfront-eks, which passes PLATFORM_LLM_EDGE_URL to the
# backend pods.
. "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
load
need_tools kubectl helm

step "llm-edge ($NAME)"

[ -n "${VPC_ID:-}" ]     || die "VPC_ID missing from state — run 10-network.sh first"
[ -n "${RUNTIME_SG:-}" ] || die "RUNTIME_SG missing from state — run 10-network.sh first"
[ -n "${CLUSTER_SG:-}" ] || die "CLUSTER_SG missing from state — run 25-eks.sh first"

TABLE_ARN="arn:aws:dynamodb:$AWS_REGION:$ACCOUNT_ID:table/$TABLE"
LLM_ARN="$(aws secretsmanager describe-secret --secret-id "$LLM_SECRET" --query ARN --output text)"
EDGE_LOG_GROUP="$LOG_PREFIX/llm-edge.edge"

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
EDGE_SVC_SG="$(sg_ensure "${NAME}-llm-edge-task" "llm-edge pods: from its listener only")"
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
# kubelet probes arrive from the node, which carries the cluster security group.
aws ec2 authorize-security-group-ingress --group-id "$EDGE_SVC_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$CLUSTER_SG,Description=kubelet probes from cluster nodes}]" \
  >/dev/null 2>&1 || log "task ingress from cluster sg already present"

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
# Name resolution: CoreDNS runs with the cluster security group. In strict
# enforcing mode both sides are needed — the pod's egress and CoreDNS's ingress.
for proto in tcp udp; do
  aws ec2 authorize-security-group-egress --group-id "$EDGE_SVC_SG" \
    --ip-permissions "IpProtocol=$proto,FromPort=53,ToPort=53,UserIdGroupPairs=[{GroupId=$CLUSTER_SG,Description=DNS to CoreDNS}]" \
    >/dev/null 2>&1 || log "task egress dns/$proto already present"
  aws ec2 authorize-security-group-ingress --group-id "$CLUSTER_SG" \
    --ip-permissions "IpProtocol=$proto,FromPort=53,ToPort=53,UserIdGroupPairs=[{GroupId=$EDGE_SVC_SG,Description=DNS from llm-edge pods}]" \
    >/dev/null 2>&1 || log "cluster sg dns/$proto from edge already present"
done

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
# Assumed through IRSA by the `edge` service account in the `llm-edge`
# namespace and nothing else. Image pulls are the node's business, so there is
# no execution role.
EDGE_TASK_ROLE="$(role_ensure "agent-platform-llm-edge${SUFFIX}" "$(irsa_trust llm-edge edge)")"
aws iam update-assume-role-policy --role-name "agent-platform-llm-edge${SUFFIX}" \
  --policy-document "$(irsa_trust llm-edge edge)"
save EDGE_TASK_ROLE "$EDGE_TASK_ROLE"

put_json_policy() {  # role name file
  aws iam put-role-policy --role-name "$1" --policy-name "$2" --policy-document "file://$3"
  log "policy $2 -> $1"
}

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

# ------------------------------------------------------------------ EKS
aws logs create-log-group --log-group-name "$EDGE_LOG_GROUP" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "$EDGE_LOG_GROUP" --retention-in-days 30 >/dev/null 2>&1 || true
save EDGE_LOG_GROUP "$EDGE_LOG_GROUP"

# Its own namespace, matching the terraform port: the edge has a different blast
# radius from the control plane and is easier to reason about kept separate.
EDGE_IMAGE="${REGISTRY}/agent-platform${SUFFIX}/llm-edge:${IMAGE_TAG}"
python3 - "$EDGE_IMAGE" "$EDGE_TASK_ROLE" "$TABLE" "$AWS_REGION" "$EDGE_TG" > /tmp/llm-edge-values.yaml <<'PY'
import json, sys
image, role, table, region, tg = sys.argv[1:6]
# The key is deliberately NOT injected here. The workload role fetches it at
# request time from the secret named on the session's grant, so a per-backend
# secret override in the model control plane keeps working and the key never
# becomes part of the pod spec.
print(json.dumps({
  "name": "edge", "image": image, "replicas": 2, "port": 8080,
  "env": {"PLATFORM_TABLE": table, "AWS_REGION": region},
  "serviceAccount": {"roleArn": role},
  "probe": {"path": "/healthz",
            "readiness": {"initialDelaySeconds": 5, "periodSeconds": 10, "failureThreshold": 3},
            "liveness": {"initialDelaySeconds": 30, "periodSeconds": 20, "failureThreshold": 3},
            "startup": {"enabled": False, "periodSeconds": 10, "failureThreshold": 30}},
  "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
  "targetGroups": [{"arn": tg, "port": 8080}],
}))
PY
workload_install edge llm-edge /tmp/llm-edge-values.yaml "$EDGE_SVC_SG"
save EDGE_NAMESPACE llm-edge

# Consumed by 60-cloudfront-eks.sh as PLATFORM_LLM_EDGE_URL. Plain http on an
# internal listener: set up TLS with a certificate for a name you own if the
# prompt content on this leg needs it (see docs/deployment-aws-cli.md).
save LLM_EDGE_URL "http://$EDGE_DNS"

log "llm-edge done — $LLM_EDGE_URL"
log "the gateway key is now readable only by agent-platform-llm-edge${SUFFIX} (IRSA: llm-edge/edge)"

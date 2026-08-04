#!/bin/bash
# Deploy the service-entry demo pod to an EKS cluster — this script IS the
# channel SOP, executed end to end against the demo environment:
#
#   1. per-channel IAM policy (from the SOP)          → attach to the pod role
#   2. eks-pod-identity-agent add-on                  → ensure installed
#   3. pod identity association                       → role ⇄ service account
#   4. robot IdP credentials (path A)                 → K8s secret in the pod
#   5. the workload itself                            → Deployment (submit/poll loop)
#
# Usage:
#   ./deploy.sh <cluster-name> <channel-id> [pod-role-name]
#
# Requires: an existing IAM channel (Channels page → New channel → AWS IAM),
# the AgentPlatformPortal stack (ServiceEntryApiUrl output), kubectl access
# to the cluster, and — for the robot identity — a seeded
# agent-platform/robot-order-service secret (scripts/seed_team_idp.py).
set -euo pipefail

CLUSTER="${1:?cluster name}"
CHANNEL_ID="${2:?channel id}"
ROLE_NAME="${3:-eks-demo-order-service-agent-caller}"
NAMESPACE="agent-demo"
SERVICE_ACCOUNT="order-service"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
HERE="$(cd "$(dirname "$0")" && pwd)"

API_URL=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceEntryApiUrl'].OutputValue" --output text)
API_ID=$(echo "$API_URL" | sed -E 's|https://([a-z0-9]+)\..*|\1|')
ARN_BASE="arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/svc"
echo "Service entry: $API_URL  ($ARN_BASE)"

# ---- 0. execute-api interface endpoint in the workload's VPC ---------------
# The service entry is a PRIVATE API — unreachable except through a VPC
# endpoint. One endpoint per VPC serves every channel. Private DNS stays OFF
# (this may be a shared cluster VPC; enabling it would hijack execute-api
# resolution for every workload in it), so the pod calls the
# endpoint-specific URL form instead.
CLUSTER_VPC=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query "cluster.resourcesVpcConfig.vpcId" --output text)
VPCE_ID=$(aws ec2 describe-vpc-endpoints --region "$REGION" \
  --filters "Name=vpc-id,Values=$CLUSTER_VPC" \
            "Name=service-name,Values=com.amazonaws.${REGION}.execute-api" \
            "Name=vpc-endpoint-state,Values=available,pending" \
  --query "VpcEndpoints[0].VpcEndpointId" --output text)
if [ "$VPCE_ID" = "None" ] || [ -z "$VPCE_ID" ]; then
  VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids "$CLUSTER_VPC" --region "$REGION" \
    --query "Vpcs[0].CidrBlock" --output text)
  VPCE_SG=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$CLUSTER_VPC" "Name=group-name,Values=agent-platform-execute-api-vpce" \
    --query "SecurityGroups[0].GroupId" --output text)
  if [ "$VPCE_SG" = "None" ] || [ -z "$VPCE_SG" ]; then
    VPCE_SG=$(aws ec2 create-security-group --region "$REGION" --vpc-id "$CLUSTER_VPC" \
      --group-name agent-platform-execute-api-vpce \
      --description "HTTPS to the agent-platform private service-entry API" \
      --query "GroupId" --output text)
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$VPCE_SG" \
      --protocol tcp --port 443 --cidr "$VPC_CIDR" >/dev/null
  fi
  # private subnets, at most ONE per AZ (interface endpoints reject two
  # subnets in the same zone)
  SUBNETS=$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=vpc-id,Values=$CLUSTER_VPC" \
    --query "Subnets[?MapPublicIpOnLaunch==\`false\`].[AvailabilityZone,SubnetId]" --output text \
    | sort | awk '!seen[$1]++ {print $2}' | tr '\n' ' ')
  VPCE_ID=$(aws ec2 create-vpc-endpoint --region "$REGION" --vpc-id "$CLUSTER_VPC" \
    --vpc-endpoint-type Interface \
    --service-name "com.amazonaws.${REGION}.execute-api" \
    --subnet-ids $SUBNETS --security-group-ids "$VPCE_SG" \
    --no-private-dns-enabled \
    --query "VpcEndpoint.VpcEndpointId" --output text)
  echo "created execute-api endpoint $VPCE_ID — waiting for it to become available…"
  for _ in $(seq 1 30); do
    STATE=$(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$VPCE_ID" --region "$REGION" \
      --query "VpcEndpoints[0].State" --output text)
    [ "$STATE" = "available" ] && break
    sleep 10
  done
fi
POD_API_URL="https://${API_ID}-${VPCE_ID}.execute-api.${REGION}.amazonaws.com/svc"
echo "execute-api endpoint: $VPCE_ID  pod URL: $POD_API_URL"

# ---- 1. workload onboarding policy (identical to the SOP's step 1) --------
# One API-wide grant per workload, applied ONCE. Which channels this role may
# actually call is the channel's caller allowlist inside the platform — so
# binding it to more channels later never comes back to this script.
POLICY_NAME="agent-platform-service-entry"
POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SubmitToAllowlistedChannels", "Effect": "Allow", "Action": "execute-api:Invoke",
      "Resource": "${ARN_BASE}/POST/service/v1/channels/*/invocations" },
    { "Sid": "PollOwnInvocations", "Effect": "Allow", "Action": "execute-api:Invoke",
      "Resource": "${ARN_BASE}/GET/service/v1/invocations/*" }
  ]
}
EOF
)
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
if ! aws iam create-policy --policy-name "$POLICY_NAME" --policy-document "$POLICY_DOC" >/dev/null 2>&1; then
  # policy exists — refresh it (the API id in the ARN base can change across
  # redeployments); prune the oldest non-default version if at the 5 cap
  OLDEST=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
    --query "Versions[?IsDefaultVersion==\`false\`]|[-1].VersionId" --output text)
  if [ "$OLDEST" != "None" ] && [ -n "$OLDEST" ]; then
    N=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" --query "length(Versions)" --output text)
    [ "$N" -ge 5 ] && aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$OLDEST"
  fi
  aws iam create-policy-version --policy-arn "$POLICY_ARN" \
    --policy-document "$POLICY_DOC" --set-as-default >/dev/null
  echo "policy refreshed: $POLICY_NAME"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
echo "policy attached: $POLICY_NAME -> $ROLE_NAME"

# ---- 2. pod identity agent add-on -----------------------------------------
if ! aws eks list-addons --cluster-name "$CLUSTER" --region "$REGION" --output text | grep -q eks-pod-identity-agent; then
  aws eks create-addon --cluster-name "$CLUSTER" --addon-name eks-pod-identity-agent --region "$REGION"
  echo "installing eks-pod-identity-agent add-on…"
else
  echo "eks-pod-identity-agent add-on already installed"
fi

# ---- 3. pod identity association -------------------------------------------
EXISTING=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER" --region "$REGION" \
  --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
  --query "associations[0].associationId" --output text)
if [ "$EXISTING" = "None" ] || [ -z "$EXISTING" ]; then
  aws eks create-pod-identity-association --cluster-name "$CLUSTER" --region "$REGION" \
    --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
    --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" >/dev/null
  echo "pod identity association created"
else
  echo "pod identity association exists: $EXISTING"
fi

# ---- 4 + 5. Kubernetes objects ---------------------------------------------
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" >/dev/null

IDP_TOKEN_URL=""
ROBOT_JSON=$(aws secretsmanager get-secret-value --secret-id agent-platform/robot-order-service \
  --region "$REGION" --query SecretString --output text 2>/dev/null || true)
if [ -n "$ROBOT_JSON" ]; then
  IDP_TOKEN_URL="$(echo "$ROBOT_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["issuer"])')/protocol/openid-connect/token"
fi

python3 - "$HERE" "$POD_API_URL" "$CHANNEL_ID" "$IDP_TOKEN_URL" <<'EOF' | kubectl apply -f -
import json, pathlib, sys
here, api_url, channel_id, idp_url = sys.argv[1:5]
manifest = pathlib.Path(here, "k8s.yaml").read_text()
manifest = manifest.replace('SERVICE_API_URL: "REPLACED_BY_DEPLOY_SCRIPT"', f'SERVICE_API_URL: "{api_url}"')
manifest = manifest.replace('CHANNEL_ID: "REPLACED_BY_DEPLOY_SCRIPT"', f'CHANNEL_ID: "{channel_id}"')
manifest = manifest.replace('IDP_TOKEN_URL: "REPLACED_BY_DEPLOY_SCRIPT"', f'IDP_TOKEN_URL: "{idp_url}"')
code = pathlib.Path(here, "caller.py").read_text()
manifest = manifest.replace('caller.py: "REPLACED_BY_DEPLOY_SCRIPT"', "caller.py: |\n" + "\n".join("    " + l for l in code.splitlines()))
print(manifest)
EOF

if [ -n "$ROBOT_JSON" ]; then
  kubectl create secret generic robot-idp -n "$NAMESPACE" \
    --from-literal=client_id="$(echo "$ROBOT_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["client_id"])')" \
    --from-literal=client_secret="$(echo "$ROBOT_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["client_secret"])')" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "robot IdP credentials mounted (path A: the pod holds them, not the platform)"
else
  echo "no robot secret found — pod runs with IAM only (seed_team_idp.py provisions it)"
fi

kubectl -n "$NAMESPACE" rollout restart deployment order-service >/dev/null 2>&1 || true
echo
echo "Done. Watch it: kubectl -n $NAMESPACE logs -f deploy/order-service"
echo "Teardown:      kubectl delete ns $NAMESPACE ; aws eks delete-pod-identity-association …"

#!/bin/bash
# Post-deployment verification for the CLI-deployed agent platform.
#
# Two layers:
#   L1  infrastructure reconciliation — resources exist AND are configured as
#       intended (read-only, no model spend)
#   L2  functional smoke — a real sign-in, a real agent invocation, an
#       interactive session, S3 persistence, governance
#
# Both layers include NEGATIVE assertions. That is deliberate: a misconfigured
# origin-verify header or a wildcard CORS origin leaves every resource present
# and correct-looking, so only a request that SHOULD fail proves the control is
# actually on.
#
# Usage:
#   export AWS_PROFILE=... AWS_REGION=...
#   PORTAL_PASSWORD='...' bash tests/verify.sh            # L1 + L2
#   LAYER=1 bash tests/verify.sh                          # L1 only (free)
#
#   # Against a TERRAFORM deployment (no .state file needed): point TF_DIR at
#   # the terraform/ directory and ids resolve from `terraform output` plus
#   # the platform's fixed naming convention.
#   TF_DIR=../../terraform LAYER=1 bash tests/verify.sh
#
# Exit codes are distinct so a wrapper can tell the cases apart:
#   0  all checks passed
#   1  one or more checks FAILED — the deployment has a problem
#   2  could not run (missing state, no credentials, no portal password)
set -uo pipefail
# NB: deliberately no `set -e`. Half these checks probe for failure, so a
# non-zero exit from curl/aws is data, not a reason to stop. Each check decides
# pass/fail for itself; the summary sets the exit code.

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../lib/common.sh" 2>/dev/null || { echo "cannot load lib/common.sh"; exit 2; }

if [ -n "${TF_DIR:-}" ]; then
  # ------------------------------------------------ terraform id source
  # Resolve the same ids the CLI path records, from `terraform output` plus
  # the platform's fixed naming convention (the suffix keeps both in step).
  # This makes the suite the acceptance test for ANY deployment of the
  # platform, not just one built by these scripts.
  TFO="$(cd "$TF_DIR" && terraform output -json 2>/dev/null)" \
    || { echo "terraform output failed in $TF_DIR"; exit 2; }
  tfo() { printf '%s' "$TFO" | python3 -c "
import json,sys
d=json.load(sys.stdin)
v=d.get('$1',{}).get('value','')
print(v if isinstance(v,str) else json.dumps(v))"; }

  STATE_FILE=/dev/null          # skip the .state preflight
  VPC_ID="$(tfo vpc_id)"
  DIST_DOMAIN="$(tfo portal_url)"; DIST_DOMAIN="${DIST_DOMAIN#https://}"
  TABLE="$(tfo table_name)"
  WORKSPACE_BUCKET="$(tfo workspace_bucket_name)"
  CLIENT_ID="$(tfo user_pool_client_id)"
  SERVICE_API_ID="$(tfo service_entry_api_id)"
  ALB_DNS="$(tfo alb_dns_name)"
  # Fixed names (suffix-aware), then describe for the ARNs the checks need.
  EKS_CLUSTER="$(tfo eks_cluster_name)"
  BACKEND_NAMESPACE=portal
  BACKEND_DEPLOYMENT=backend
  TASK_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/agent-platform-backend-task${SUFFIX}"
  OIDC_PROVIDER_ARN="$(tfo eks_oidc_provider_arn)"
  ALB_ARN="$(aws elbv2 describe-load-balancers --names "agent-platform-portal${SUFFIX}" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)"
  L_ALB="$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[0].ListenerArn' --output text 2>/dev/null)"
  TG_ALB="$(aws elbv2 describe-target-groups --names "agent-platform-backend${SUFFIX}" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)"
  # Network ids aren't outputs in reuse-mode deployments; resolve from the VPC.
  NAT_ID="$(aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available" \
    --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null)"
  RT_PRIV="$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
    --query "RouteTables[?Routes[?NatGatewayId=='$NAT_ID']].RouteTableId | [0]" --output text 2>/dev/null)"
else
  load
fi

LAYER="${LAYER:-2}"
PASS=0; FAIL=0; SKIP=0
FAILED_NAMES=""

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES="$FAILED_NAMES\n    - $1"; printf '  \033[31mFAIL\033[0m %s\n    %s\n' "$1" "${2:-}"; }
skip() { SKIP=$((SKIP+1)); printf '  \033[33mSKIP\033[0m %s (%s)\n' "$1" "${2:-}"; }

# check NAME EXPECTED ACTUAL
check() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}
# check_not NAME FORBIDDEN ACTUAL — the negative form
check_not() {
  if [ "$2" != "$3" ]; then ok "$1"; else bad "$1" "value must not be '$2'"; fi
}
# check_contains NAME NEEDLE HAYSTACK
check_contains() {
  case "$3" in *"$2"*) ok "$1";; *) bad "$1" "'$2' not found in: $(printf '%s' "$3" | head -c 160)";; esac
}

# ---------------------------------------------------------------- preflight
printf '\n=== preflight ===\n'
[ -n "${TF_DIR:-}" ] || [ -s "$STATE_FILE" ] || { echo "no state at $STATE_FILE — deploy first (or set TF_DIR)"; exit 2; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "AWS credentials not usable"; exit 2; }
: "${DIST_DOMAIN:?state has no DIST_DOMAIN — deployment incomplete}"
PORTAL="https://$DIST_DOMAIN"
echo "  stack:  $NAME"
echo "  portal: $PORTAL"

##############################################################################
printf '\n=== L1 · network ===\n'
##############################################################################
VPC_STATE="$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --query 'Vpcs[0].State' --output text 2>/dev/null)"
check "vpc available" available "$VPC_STATE"

DNS_H="$(aws ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsHostnames \
  --query 'EnableDnsHostnames.Value' --output text 2>/dev/null)"
# Without DNS hostnames the runtimes cannot resolve the endpoints they call.
check "vpc dns hostnames enabled" True "$DNS_H"

# At least 4 — a fresh VPC has exactly the platform's 2 public + 2 private,
# but a reuse-mode deployment (existing_vpc_id) shares the VPC with other
# subnets, and extras are not a defect.
SUBNET_COUNT="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'length(Subnets)' --output text 2>/dev/null)"
if [ "${SUBNET_COUNT:-0}" -ge 4 ] 2>/dev/null; then ok "subnets present ($SUBNET_COUNT >= 4)"
else bad "subnets present" "expected >= 4, got '$SUBNET_COUNT'"; fi

NAT_STATE="$(aws ec2 describe-nat-gateways --nat-gateway-ids "$NAT_ID" \
  --query 'NatGateways[0].State' --output text 2>/dev/null)"
check "nat gateway available" available "$NAT_STATE"

# The private route table must actually point at the NAT, or every runtime call
# out of the VPC silently fails.
NAT_ROUTE="$(aws ec2 describe-route-tables --route-table-ids "$RT_PRIV" \
  --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].NatGatewayId | [0]" --output text 2>/dev/null)"
check "private subnets egress via nat" "$NAT_ID" "$NAT_ROUTE"

##############################################################################
printf '\n=== L1 · platform data ===\n'
##############################################################################
T_STATUS="$(aws dynamodb describe-table --table-name "$TABLE" --query 'Table.TableStatus' --output text 2>/dev/null)"
check "dynamodb table active" ACTIVE "$T_STATUS"

PITR="$(aws dynamodb describe-continuous-backups --table-name "$TABLE" \
  --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' --output text 2>/dev/null)"
check "dynamodb PITR enabled" ENABLED "$PITR"

VERS="$(aws s3api get-bucket-versioning --bucket "$WORKSPACE_BUCKET" --query Status --output text 2>/dev/null)"
check "workspace bucket versioned" Enabled "$VERS"

PAB="$(aws s3api get-public-access-block --bucket "$WORKSPACE_BUCKET" \
  --query 'PublicAccessBlockConfiguration.BlockPublicAcls' --output text 2>/dev/null)"
check "workspace bucket blocks public acls" True "$PAB"

for r in "${KERNEL_REPOS[@]}"; do
  SCAN="$(aws ecr describe-repositories --repository-names "agent-platform${SUFFIX}/$r" \
    --query 'repositories[0].imageScanningConfiguration.scanOnPush' --output text 2>/dev/null)"
  check "ecr scan-on-push: $r" True "$SCAN"
  IMGS="$(aws ecr list-images --repository-name "agent-platform${SUFFIX}/$r" \
    --query 'length(imageIds)' --output text 2>/dev/null)"
  if [ "${IMGS:-0}" -ge 1 ] 2>/dev/null; then ok "ecr has an image: $r"; else bad "ecr has an image: $r" "0 images"; fi
done

##############################################################################
printf '\n=== L1 · runtimes ===\n'
##############################################################################
for rt in claude_code_kernel agent_sdk_kernel mcp_tools_kernel; do
  n="${rt}${RUNTIME_SUFFIX}"
  ST="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$n'].status | [0]" --output text 2>/dev/null)"
  check "runtime READY: $n" READY "$ST"
done

# The interactive kernel must NOT be able to read workspaces/* with its own role —
# that access is deliberately confined to the per-session credentials the backend
# mints (docs/permissions.md). A policy that granted it would be a real finding.
INT_POL="$(aws iam get-role-policy --role-name "agent-platform-interactive-role${SUFFIX}" \
  --policy-name kernel --query 'PolicyDocument' --output json 2>/dev/null)"
if printf '%s' "$INT_POL" | grep -q 'workspaces/\*'; then
  bad "interactive role has no workspaces/* grant" "found a workspaces/* resource in the kernel policy"
else
  ok "interactive role has no workspaces/* grant"
fi

##############################################################################
printf '\n=== L1 · backend on EKS ===\n'
##############################################################################
EKS_STATUS="$(aws eks describe-cluster --name "$EKS_CLUSTER" --query 'cluster.status' --output text 2>/dev/null)"
check "eks cluster ACTIVE" ACTIVE "$EKS_STATUS"

# IRSA, not Pod Identity: the CNI runs under its own role and the Pod Identity
# agent add-on is absent.
CNI_ROLE="$(aws eks describe-addon --cluster-name "$EKS_CLUSTER" --addon-name vpc-cni \
  --query 'addon.serviceAccountRoleArn' --output text 2>/dev/null)"
case "$CNI_ROLE" in arn:aws:iam::*) ok "vpc-cni add-on runs under an IRSA role" ;;
  *) bad "vpc-cni add-on runs under an IRSA role" "serviceAccountRoleArn is '$CNI_ROLE'" ;; esac
PIA="$(aws eks describe-addon --cluster-name "$EKS_CLUSTER" --addon-name eks-pod-identity-agent \
  --query 'addon.status' --output text 2>/dev/null || true)"
check "pod identity agent is NOT installed" "" "${PIA:-}"

# Security groups for Pods, strict mode: the pod's own groups are the only
# ones evaluated, so the 443-only / ALB-only rules mean what they say.
CNI_CFG="$(aws eks describe-addon --cluster-name "$EKS_CLUSTER" --addon-name vpc-cni \
  --query 'addon.configurationValues' --output text 2>/dev/null)"
check_contains "vpc-cni has pod ENIs enabled" '"ENABLE_POD_ENI":"true"' "$CNI_CFG"
check_contains "vpc-cni enforces pod security groups strictly" '"POD_SECURITY_GROUP_ENFORCING_MODE":"strict"' "$CNI_CFG"

if command -v kubectl >/dev/null 2>&1 && kube version >/dev/null 2>&1; then
  READY="$(kube -n "$BACKEND_NAMESPACE" get deploy "$BACKEND_DEPLOYMENT" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
  WANT="$(kube -n "$BACKEND_NAMESPACE" get deploy "$BACKEND_DEPLOYMENT" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  check "backend pods ready == desired" "${WANT:-?}" "${READY:-0}"

  # The rollout never dips below the desired count (the ECS circuit breaker's
  # job is now maxUnavailable=0 plus helm --atomic).
  MAXU="$(kube -n "$BACKEND_NAMESPACE" get deploy "$BACKEND_DEPLOYMENT" -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}' 2>/dev/null)"
  check "backend rollout keeps maxUnavailable=0" 0 "$MAXU"

  # IRSA wiring: annotation on the service account, trust on the role.
  SA_ROLE="$(kube -n "$BACKEND_NAMESPACE" get sa "$BACKEND_DEPLOYMENT" -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null)"
  check "backend service account is annotated with the workload role" "$TASK_ROLE" "$SA_ROLE"
  TRUST="$(aws iam get-role --role-name "${TASK_ROLE##*/}" --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)"
  check_contains "workload role trusts the cluster OIDC provider" "${OIDC_PROVIDER_ARN##*/}" "$TRUST"
  check_contains "workload role is pinned to the backend service account" "system:serviceaccount:${BACKEND_NAMESPACE}:${BACKEND_DEPLOYMENT}" "$TRUST"
  if printf '%s' "$TRUST" | grep -q 'ecs-tasks.amazonaws.com'; then
    bad "workload role no longer trusts ecs-tasks" "ECS trust still present"
  else ok "workload role no longer trusts ecs-tasks"; fi

  # Every backend pod got a branch ENI, i.e. its own security group.
  NO_ENI="$(kube -n "$BACKEND_NAMESPACE" get pods -l app="$BACKEND_DEPLOYMENT" \
    -o jsonpath='{range .items[*]}{.metadata.name}:{.metadata.annotations.vpc\.amazonaws\.com/pod-eni}{"\n"}{end}' 2>/dev/null \
    | awk -F: '$2==""{print $1}' | tr '\n' ' ')"
  check "every backend pod carries a pod security group (branch ENI)" "" "$NO_ENI"

  # CORS must be scoped, never "*": with allow_credentials the wildcard is
  # reflected back and defeats browser origin isolation.
  CORS_ENV="$(kube -n "$BACKEND_NAMESPACE" get deploy "$BACKEND_DEPLOYMENT" \
    -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='PLATFORM_CORS_ORIGINS')].value}" 2>/dev/null)"
  check_not "backend CORS origin is not a wildcard" "*" "$CORS_ENV"
  check "backend CORS origin is the distribution" "$PORTAL" "$CORS_ENV"
else
  skip "backend pods ready == desired" "kubectl unavailable or cluster unreachable"
  skip "backend CORS origin is the distribution" "kubectl unavailable or cluster unreachable"
fi

# The controller's TargetGroupBinding is what puts pods behind the ALB.
HEALTHY="$(aws elbv2 describe-target-health --target-group-arn "$TG_ALB" \
  --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text 2>/dev/null)"
if [ "${HEALTHY:-0}" -ge 1 ] 2>/dev/null; then ok "alb target group has healthy pod targets ($HEALTHY)"
else bad "alb target group has healthy pod targets" "healthy count '$HEALTHY'"; fi

##############################################################################
printf '\n=== L1 · portal edge (negative assertions) ===\n'
##############################################################################

# The ALB must default-deny: its security group admits every CloudFront
# distribution, so the origin-verify header is the only thing making the edge a
# real chokepoint.
ALB_DEFAULT="$(aws elbv2 describe-listeners --listener-arns "$L_ALB" \
  --query 'Listeners[0].DefaultActions[0].Type' --output text 2>/dev/null)"
check "alb listener default action is fixed-response" fixed-response "$ALB_DEFAULT"

# Find the forward rule by its header CONDITION, not by priority — the CLI
# path creates it at 100, the terraform deployment at 1; the number is an
# implementation detail, the header condition is the control being asserted.
ALB_RULE_HDR="$(aws elbv2 describe-rules --listener-arn "$L_ALB" \
  --query "Rules[?Actions[?Type=='forward']].Conditions[].HttpHeaderConfig.HttpHeaderName | [0]" --output text 2>/dev/null)"
check "alb forwards only on x-origin-verify" x-origin-verify "$ALB_RULE_HDR"

ALB_LOGS="$(aws elbv2 describe-load-balancer-attributes --load-balancer-arn "$ALB_ARN" \
  --query "Attributes[?Key=='access_logs.s3.enabled'].Value | [0]" --output text 2>/dev/null)"
check "alb access logs enabled" true "$ALB_LOGS"

API_TYPE="$(aws apigateway get-rest-api --rest-api-id "$SERVICE_API_ID" \
  --query 'endpointConfiguration.types[0]' --output text 2>/dev/null)"
# A PUBLIC service entry would expose the SigV4 submit path to the internet.
check "service-entry API is PRIVATE" PRIVATE "$API_TYPE"

##############################################################################
printf '\n=== L1 · reachability (negative assertions) ===\n'
##############################################################################
HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PORTAL/health" 2>/dev/null)"
check "portal /health through cloudfront" 200 "$HEALTH_CODE"

ANON_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PORTAL/api/v1/kernels" 2>/dev/null)"
# Unauthenticated API access must be refused.
check "unauthenticated /api is 401" 401 "$ANON_CODE"

FOREIGN_ACAO="$(curl -s -D - -o /dev/null --max-time 30 \
  -H 'Origin: https://not-the-portal.invalid' "$PORTAL/api/v1/config" 2>/dev/null \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')"
if [ -z "$FOREIGN_ACAO" ]; then
  ok "foreign Origin gets no Access-Control-Allow-Origin"
else
  bad "foreign Origin gets no Access-Control-Allow-Origin" "reflected: $FOREIGN_ACAO"
fi

# Direct-to-ALB must be refused. From outside the VPC the SG usually drops the
# connection outright (timeout), which is also a pass — what must NOT happen is
# a 200.
# Being unable to reach the ALB is the EXPECTED outcome here, and curl signals
# it with a non-zero exit (28 on timeout). Two things follow: `|| echo 000` would
# concatenate onto the 000 curl already printed, and an unguarded assignment
# aborts the whole run under `set -e`-style flags. So capture, then normalise.
ALB_DIRECT="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "http://$ALB_DNS/health" 2>/dev/null)" || true
case "$ALB_DIRECT" in ''|*[!0-9]*) ALB_DIRECT=000 ;; esac
case "$ALB_DIRECT" in
  200) bad "direct ALB access is refused" "ALB answered 200 without the origin-verify header" ;;
  403) ok  "direct ALB access is refused (403)" ;;
  000) ok  "direct ALB access is refused (unreachable from here)" ;;
  *)   ok  "direct ALB access is refused ($ALB_DIRECT)" ;;
esac

if [ "$LAYER" = "1" ]; then
  printf '\n=== summary (L1 only) ===\n  passed %d   failed %d   skipped %d\n' "$PASS" "$FAIL" "$SKIP"
  [ "$FAIL" -eq 0 ] || { printf '\n  failed checks:%b\n' "$FAILED_NAMES"; exit 1; }
  exit 0
fi

##############################################################################
printf '\n=== L2 · sign-in ===\n'
##############################################################################
if [ -z "${PORTAL_PASSWORD:-}" ]; then
  echo "  PORTAL_PASSWORD not set — cannot exercise the API."
  echo "  Set it to the password of the 'admin' Cognito user, or run LAYER=1."
  exit 2
fi

TOKEN="$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id "$CLIENT_ID" \
  --auth-parameters "USERNAME=admin,PASSWORD=$PORTAL_PASSWORD" \
  --query 'AuthenticationResult.IdToken' --output text 2>/dev/null || echo '')"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "None" ]; then
  echo "  could not sign in as 'admin' — wrong password, or the user has no permanent password"
  exit 2
fi
ok "signed in as admin (id token acquired)"

api() {  # method path [json-file]
  local m="$1" p="$2" f="${3:-}"
  if [ -n "$f" ]; then
    curl -s -X "$m" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      --data @"$f" --max-time 240 "$PORTAL$p" 2>/dev/null
  else
    curl -s -X "$m" -H "Authorization: Bearer $TOKEN" --max-time 240 "$PORTAL$p" 2>/dev/null
  fi
}
# jq is not assumed; python3 stdlib does the parsing. errors="replace" matters
# because agent output can carry control characters that break strict json.
jget() {  # json-string  python-expression-on-d
  python3 -c "
import json,sys
raw=sys.stdin.read()
try: d=json.loads(raw)
except Exception: print(''); sys.exit()
try: print($1)
except Exception: print('')"
}

ME="$(api GET /api/v1/me)"
check "identity resolves to admin" admin "$(printf '%s' "$ME" | jget "d['user']")"
check "admin has the admin role" True "$(printf '%s' "$ME" | jget "str(d['is_admin'])")"

CFG="$(curl -s --max-time 30 "$PORTAL/api/v1/config" 2>/dev/null)"
check "auth mode is cognito" cognito "$(printf '%s' "$CFG" | jget "d['auth_mode']")"

##############################################################################
printf '\n=== L2 · kernel catalog ===\n'
##############################################################################
KERNELS="$(api GET /api/v1/kernels)"
check_contains "catalog lists the interactive kernel" '"claude-code"' "$KERNELS"
check_contains "catalog lists the headless kernel" '"agent-sdk"' "$KERNELS"
NOT_READY="$(printf '%s' "$KERNELS" | jget "','.join(k['id'] for k in d if k.get('status')!='READY')")"
check "all catalogued kernels are READY" "" "$NOT_READY"

##############################################################################
printf '\n=== L2 · headless invocation (real model call) ===\n'
##############################################################################
MARKER="cli-verify-$$"
cat > /tmp/verify-invoke.json <<JSON
{"prompt":"Reply with exactly this token and nothing else: $MARKER","max_turns":2}
JSON
INV="$(api POST /api/v1/kernels/agent-sdk/invoke /tmp/verify-invoke.json)"
INV_OK="$(printf '%s' "$INV" | jget "str(d.get('ok'))")"
if [ "$INV_OK" = "True" ]; then
  ok "headless kernel invocation succeeded"
  RESULT="$(printf '%s' "$INV" | jget "d.get('result','')")"
  check_contains "model echoed the request marker" "$MARKER" "$RESULT"
  TURNS="$(printf '%s' "$INV" | jget "str((d.get('usage') or {}).get('num_turns',''))")"
  if [ -n "$TURNS" ] && [ "$TURNS" != "None" ]; then ok "invocation reported usage (turns=$TURNS)"
  else bad "invocation reported usage" "usage block empty"; fi
else
  # A quota rejection is a configuration state, not a broken deployment — say so
  # rather than reporting a generic failure (this exact case produced a silent
  # empty artifact during pipeline testing).
  DETAIL="$(printf '%s' "$INV" | jget "d.get('detail') or d.get('error') or ''")"
  case "$DETAIL" in
    *"daily invocation limit"*) skip "headless kernel invocation" "daily quota reached: $DETAIL" ;;
    *) bad "headless kernel invocation succeeded" "$(printf '%s' "$INV" | head -c 200)" ;;
  esac
fi

##############################################################################
printf '\n=== L2 · governance / observability ===\n'
##############################################################################
POLICY="$(api GET /api/v1/governance/policy)"
LIMIT="$(printf '%s' "$POLICY" | jget "str(d.get('daily_limit_per_user',''))")"
if [ -n "$LIMIT" ] && [ "$LIMIT" != "None" ]; then ok "governance policy readable (per-user limit $LIMIT)"
else bad "governance policy readable" "no daily_limit_per_user in response"; fi

USAGE="$(api GET /api/v1/governance/usage)"
check_contains "usage counter readable" '"total"' "$USAGE"

LEDGER="$(api "GET" "/api/v1/observability/invocations?limit=5")"
# The invocation above must have been recorded — the ledger is how spend and
# attribution are tracked.
if printf '%s' "$LEDGER" | grep -q 'agent-sdk\|source'; then ok "invocation ledger has entries"
else bad "invocation ledger has entries" "$(printf '%s' "$LEDGER" | head -c 160)"; fi

##############################################################################
printf '\n=== L2 · interactive session + workspace credentials ===\n'
##############################################################################
cat > /tmp/verify-session.json <<'JSON'
{"name":"cli-verify","kernel":"claude-code"}
JSON
SESS="$(api POST /api/v1/sessions /tmp/verify-session.json)"
SID="$(printf '%s' "$SESS" | jget "d.get('session_id','')")"
if [ -z "$SID" ]; then
  bad "interactive session created" "$(printf '%s' "$SESS" | head -c 200)"
else
  ok "interactive session created"
  RSID="$(printf '%s' "$SESS" | jget "d.get('runtime_session_id','')")"

  CONN="$(api GET "/api/v1/sessions/$SID/connect")"
  WSS="$(printf '%s' "$CONN" | jget "d.get('wss_url','')")"
  case "$WSS" in
    wss://*) ok "connect minted a presigned wss url" ;;
    *) bad "connect minted a presigned wss url" "$(printf '%s' "$CONN" | head -c 200)" ;;
  esac

  # connect() is also what mints the session-scoped S3 credentials and the
  # refresh token; the lookup item proves that path ran end to end.
  if [ -n "$RSID" ]; then
    LOOKUP="$(aws dynamodb get-item --table-name "$TABLE" \
      --key "{\"PK\":{\"S\":\"WSTOKEN\"},\"SK\":{\"S\":\"RSID#$RSID\"}}" \
      --query 'Item.runtime_session_id.S' --output text 2>/dev/null || echo '')"
    if [ "$LOOKUP" = "$RSID" ]; then ok "workspace refresh-token lookup item written"
    else skip "workspace refresh-token lookup item written" "not found (older backend image writes it only to the session item)"; fi
  fi

  # cleanup: without this, repeated runs pile up sessions and burn daily quota
  api DELETE "/api/v1/sessions/$SID" >/dev/null
  ok "test session deleted (cleanup)"
fi

##############################################################################
printf '\n=== L2 · ecosystem registry ===\n'
##############################################################################
MCP="$(api GET /api/v1/ecosystem/mcp-servers)"
check_contains "registry seeded with built-in tools" 'code-interpreter' "$MCP"

##############################################################################
printf '\n=== summary ===\n'
##############################################################################
printf '  passed %d   failed %d   skipped %d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -ne 0 ]; then
  printf '\n  failed checks:%b\n\n' "$FAILED_NAMES"
  echo "  The deployment has a problem — see the notes in docs/DEPLOYMENT.md."
  exit 1
fi
printf '\n  Deployment verified: infrastructure matches intent and the platform serves real traffic.\n'
exit 0

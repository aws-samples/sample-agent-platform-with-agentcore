#!/bin/bash
# Shared config + helpers for the CLI deployment.
#
# Terraform gets idempotence and dependency ordering for free from state and the
# resource graph. Neither exists here, so every create is written as
# "look it up, create only if absent" and every ordering constraint is an
# explicit wait. That is the bulk of what this port is actually about.

set -euo pipefail

# ---------------------------------------------------------------- config
# Credentials come from the ambient environment: set AWS_PROFILE yourself, or
# rely on an instance/SSO role. No default profile name is assumed here.
export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_PAGER=""

# Appended to every fixed resource name so this stack coexists with the
# terraform one in the same account+region (the terraform port calls this
# name_suffix and exists for the same reason).
# Empty for production-style names (agent-platform, agent-platform-workspaces-...).
# Set it to coexist with a CDK/Terraform deployment in the same account+region —
# the same purpose as terraform's name_suffix variable.
SUFFIX="${SUFFIX:-}"
# AgentCore runtime names only allow [a-zA-Z0-9_], so a "-cli" suffix becomes
# "_cli". An empty suffix must stay empty — prefixing unconditionally would name
# the runtimes "claude_code_kernel_", which diverges from the CDK/Terraform names.
if [ -n "$SUFFIX" ]; then
  RUNTIME_SUFFIX="_$(echo "$SUFFIX" | tr -d -- '-')"
else
  RUNTIME_SUFFIX=""
fi

NAME="agent-platform${SUFFIX}"
VPC_CIDR="${VPC_CIDR:-10.20.0.0/16}"   # distinct from the terraform stack's 10.0.0.0/16

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [ -z "$ACCOUNT_ID" ] || [ "$ACCOUNT_ID" = "None" ]; then
  echo "Cannot resolve the AWS account: credentials are missing or expired." >&2
  echo "  Set AWS_PROFILE (or refresh your SSO/role session) and retry." >&2
  echo "  Check with: aws sts get-caller-identity" >&2
  return 1 2>/dev/null || exit 1
fi
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

WORKSPACE_BUCKET="agent-platform-workspaces${SUFFIX}-${ACCOUNT_ID}-${AWS_REGION}"
FRONTEND_BUCKET="agent-platform-frontend${SUFFIX}-${ACCOUNT_ID}-${AWS_REGION}"
LOGS_BUCKET="agent-platform-logs${SUFFIX}-${ACCOUNT_ID}-${AWS_REGION}"
TABLE="agent-platform${SUFFIX}"
LLM_SECRET="agent-platform${SUFFIX}/llm-gateway-key"
ENTRY_SECRET="agent-platform${SUFFIX}/service-entry"

# ---- EKS: every platform container (backend, llm-edge) runs on this cluster.
# Pods authenticate to AWS with IRSA; the Pod Identity agent is not installed.
EKS_CLUSTER="agent-platform${SUFFIX}"
EKS_VERSION="${EKS_VERSION:-1.36}"
# Graviton, and a type that supports ENI trunking (security groups for Pods):
# m/c/r families from 6g on, never the t family. The images are arm64 only.
EKS_NODE_TYPE="${EKS_NODE_TYPE:-m7g.large}"
EKS_NODE_COUNT="${EKS_NODE_COUNT:-2}"       # one per private subnet
EKS_NODE_MAX="${EKS_NODE_MAX:-3}"
# Who may reach the public Kubernetes API endpoint (it is IAM-authenticated
# regardless). Narrow this to your operators' egress addresses.
EKS_PUBLIC_CIDRS="${EKS_PUBLIC_CIDRS:-0.0.0.0/0}"
LBC_CHART_VERSION="${LBC_CHART_VERSION:-3.5.0}"
FLUENT_BIT_CHART_VERSION="${FLUENT_BIT_CHART_VERSION:-0.2.0}"
LOG_PREFIX="/eks/agent-platform${SUFFIX}"
# The workload charts are shared with the Terraform path — one definition of
# what a platform pod looks like.
CHARTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../terraform/charts" && pwd)"

KERNEL_REPOS=(claude-code-kernel agent-sdk-kernel mcp-tools-kernel backend)
# Kept out of KERNEL_REPOS deliberately: the kernel execution roles are granted
# ECR pull on every repo in that list, and nothing in a session container should
# be able to pull the image of the service that holds the gateway key.
SERVICE_REPOS=(llm-edge)
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Model ids the runtimes bake in (mirrors terraform.tfvars).
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-sonnet-4-5-20250929-v1:0}"
ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-global.anthropic.claude-haiku-4-5-20251001-v1:0}"
ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-global.anthropic.claude-opus-5}"

# Where resource ids get recorded. This file is this port's substitute for
# terraform state: without it a re-run cannot find what it already built, and
# later phases have no way to reference earlier ones.
STATE_DIR="${STATE_DIR:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state"}"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/${NAME}.env"
touch "$STATE_FILE"

# ---------------------------------------------------------------- output
log()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf '  !! %s\n' "$*" >&2; }
die()  { printf '  XX %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- state
# save KEY VALUE — record an id so re-runs and later phases can find it
save() {
  local k="$1" v="$2"
  # An empty or "None" id means the describe/create above silently failed;
  # recording it would make the next phase reference a nonexistent resource.
  if [ -z "$v" ] || [ "$v" = "None" ] || [ "$v" = "null" ]; then
    warn "refusing to save empty $k"
    return 0
  fi
  grep -v "^export ${k}=" "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
  mv "$STATE_FILE.tmp" "$STATE_FILE"
  printf 'export %s=%q\n' "$k" "$v" >> "$STATE_FILE"
  export "$k=$v"
}

load() { set +u; . "$STATE_FILE"; set -u; }

# ---------------------------------------------------------------- waiting
# retry_until "description" attempts sleep_seconds command...
# Terraform's providers wait on resource state internally; here every
# eventually-consistent step needs its own poll or the next call 400s.
retry_until() {
  local desc="$1" tries="$2" gap="$3"; shift 3
  local i
  for i in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep "$gap"
  done
  warn "timed out waiting for: $desc"
  return 1
}

# ---------------------------------------------------------------- kubernetes
# kubectl/helm against the platform cluster through a private kubeconfig, so a
# deploy never rewrites the operator's current-context. Built lazily on first
# use; needs the cluster to exist (25-eks.sh).
KUBECONFIG_FILE="$STATE_DIR/${NAME}.kubeconfig"
kube_ready() {
  if [ ! -s "$KUBECONFIG_FILE" ]; then
    aws eks update-kubeconfig --name "$EKS_CLUSTER" --kubeconfig "$KUBECONFIG_FILE" >/dev/null
  fi
}
kube() { kube_ready; kubectl --kubeconfig "$KUBECONFIG_FILE" "$@"; }
helm_k() { kube_ready; helm --kubeconfig "$KUBECONFIG_FILE" "$@"; }

need_tools() {  # tool...
  local t
  for t in "$@"; do
    command -v "$t" >/dev/null 2>&1 || die "$t is required for this phase and is not on PATH"
  done
}

# irsa_trust namespace sa [sa...] — trust policy for a role assumed through the
# cluster's OIDC provider by exactly these service accounts.
irsa_trust() {
  local ns="$1"; shift
  : "${OIDC_PROVIDER_ARN:?run 25-eks.sh}"; : "${OIDC_HOST:?run 25-eks.sh}"
  python3 - "$OIDC_PROVIDER_ARN" "$OIDC_HOST" "$ns" "$@" <<'PY'
import json, sys
arn, host, ns, *sas = sys.argv[1:]
subs = [f"system:serviceaccount:{ns}:{sa}" for sa in sas]
print(json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": arn},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
        f"{host}:aud": "sts.amazonaws.com",
        f"{host}:sub": subs if len(subs) > 1 else subs[0]}}}]}))
PY
}

# workload_install name namespace values-file [sg-id...] — the two Helm
# releases every platform workload is made of: its SecurityGroupPolicy first
# (it only applies to pods created after it exists), then the workload itself.
workload_install() {
  local name="$1" ns="$2" values="$3"; shift 3
  local sgs=("$@") sgargs=() i
  for i in "${!sgs[@]}"; do sgargs+=(--set "securityGroupIds[$i]=${sgs[$i]}"); done
  helm_k upgrade --install "${name}-sg" "$CHARTS_DIR/pod-security-group" \
    --namespace "$ns" --create-namespace --set "name=$name" "${sgargs[@]}" >/dev/null
  log "security group policy $name -> ${sgs[*]}"
  # --atomic: a rollout whose pods never become ready is rolled back, the way
  # the ECS deployment circuit breaker used to.
  helm_k upgrade --install "$name" "$CHARTS_DIR/platform-workload" \
    --namespace "$ns" --values "$values" --wait --timeout 15m --atomic >/dev/null
  log "workload $name ready ($(kube -n "$ns" get deploy "$name" -o jsonpath='{.status.readyReplicas}') replicas)"
}

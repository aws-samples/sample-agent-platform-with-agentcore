#!/usr/bin/env bash
# Deploy the pods of the AgentCore gateway entry demo and attach the hub to
# both gateways. Reads everything from `terraform output` (./terraform, applied
# first); writes kubeconfig contexts into a kubeconfig of its own
# (./.kubeconfig), never into ~/.kube/config.
#
#   ./deploy.sh            render, apply, wait for the pods, create targets + policies
#   ./deploy.sh --render   only render the manifests into ./.rendered
#   PYTHON=...             interpreter for k8s/render.py and scripts/ (default
#                          python3; needs PyYAML and a boto3 that models
#                          certificateConfigurations, >= 1.43)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HUB_SRC="$(cd "$HERE/../../hub" && pwd)"
TF="$HERE/terraform"
OUT="$HERE/.rendered"
export KUBECONFIG="$HERE/.kubeconfig"
PY="${PYTHON:-python3}"

"$PY" -c 'import yaml, boto3' 2>/dev/null || {
  echo "deploy.sh: $PY needs PyYAML and boto3 (pip install pyyaml boto3, or PYTHON=/path/to/python ./deploy.sh)" >&2
  exit 2
}

tf_json="$(mktemp)"
trap 'rm -f "$tf_json"' EXIT
terraform -chdir="$TF" output -json > "$tf_json"
tf_out() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["value"])' "$tf_json" "$1"; }

# render.py validates before it writes anything (see its docstring).
"$PY" "$HERE/k8s/render.py" "$tf_json" "$OUT"
[ "${1:-}" = "--render" ] && exit 0

REGION="$(tf_out region)"
HUB_CLUSTER="$(tf_out hub_cluster_name)"
CALLER_CLUSTER="$(tf_out caller_cluster_name)"
HUB_NS="$(tf_out hub_namespace)"
NS="$(tf_out caller_namespace)"
for cluster in "$HUB_CLUSTER" "$CALLER_CLUSTER"; do
  aws eks update-kubeconfig --region "$REGION" --name "$cluster" --alias "$cluster" >/dev/null
done

echo "== hub cluster ($HUB_CLUSTER): gateway-facing hub"
kubectl --context "$HUB_CLUSTER" apply -f "$OUT/hub.yaml"
kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" create configmap hub-code \
  --from-file="$HUB_SRC/app.py" --from-file="$HUB_SRC/entry_identity.py" \
  --from-file="$HUB_SRC/gateway_entry.py" \
  --from-file="$HUB_SRC/mcphub_hmac.py" --from-file="$HUB_SRC/requirements.txt" \
  --dry-run=client -o yaml | kubectl --context "$HUB_CLUSTER" apply -f -
kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" rollout restart deployment/hub
kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" rollout status deployment/hub --timeout=300s

echo "== caller cluster ($CALLER_CLUSTER): static-app, intruder"
# Pods are immutable: replace them.
kubectl --context "$CALLER_CLUSTER" -n "$NS" delete pod static-app intruder ws-probe --ignore-not-found --wait=true 2>/dev/null || true
kubectl --context "$CALLER_CLUSTER" apply -f "$OUT/callers.yaml"
kubectl --context "$CALLER_CLUSTER" -n "$NS" create configmap gw-client \
  --from-file="$HERE/client/gateway_mcp_client.py" \
  --dry-run=client -o yaml | kubectl --context "$CALLER_CLUSTER" apply -f -
kubectl --context "$CALLER_CLUSTER" -n "$NS" wait --for=condition=Ready pod/static-app pod/intruder pod/ws-probe --timeout=300s

echo "== gateway targets and Cedar policies"
"$PY" "$HERE/scripts/gateway_targets.py" "$tf_json"

echo "deployed; run ./verify.sh"

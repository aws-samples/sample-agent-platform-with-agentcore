#!/usr/bin/env bash
# Deploy the pods of the pod-to-pod IAM entry test into the two clusters the
# Terraform in ./terraform created. Reads everything from `terraform output`;
# writes kubeconfig contexts named <cluster> into a kubeconfig of its own
# (./.kubeconfig), never into ~/.kube/config.
#
#   ./deploy.sh            render, apply, wait for the pods
#   ./deploy.sh --render   only render the manifests into ./.rendered (reads terraform output)
#   PYTHON=...             interpreter for k8s/render.py (default python3; needs PyYAML)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HUB_SRC="$(cd "$HERE/../../hub" && pwd)"
TF="$HERE/terraform"
OUT="$HERE/.rendered"
export KUBECONFIG="$HERE/.kubeconfig"
PY="${PYTHON:-python3}"   # needs PyYAML (k8s/render.py)

tf_out() { terraform -chdir="$TF" output -raw "$1"; }

render() {
  # k8s/render.py validates before it writes anything: every variable known
  # and non-empty, the NLB subnet list non-empty and narrow, and the rendered
  # hub NetworkPolicy admitting exactly those subnets (an empty peer list
  # would admit every source - the trust anchor of `trust: network` gone).
  "$PY" -c 'import yaml' 2>/dev/null || {
    echo "deploy.sh: $PY has no PyYAML; k8s/render.py needs it to check the rendered NetworkPolicy." >&2
    echo "  pip install pyyaml   (or PYTHON=/path/to/python ./deploy.sh)" >&2
    exit 2
  }
  local tf_json
  tf_json="$(mktemp)"
  terraform -chdir="$TF" output -json > "$tf_json"
  "$PY" "$HERE/k8s/render.py" "$tf_json" "$OUT" || { rm -f "$tf_json"; exit 1; }
  REGION="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["region"]["value"])' "$tf_json")"
  FRONTEND_NAMESPACE="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["frontend_namespace"]["value"])' "$tf_json")"
  rm -f "$tf_json"
}

kube_context() {
  local cluster="$1" region="$2"
  aws eks update-kubeconfig --region "$region" --name "$cluster" --alias "$cluster" >/dev/null
}

render
[ "${1:-}" = "--render" ] && exit 0

HUB_CLUSTER="$(tf_out hub_cluster_name)"
FRONTEND_CLUSTER="$(tf_out frontend_cluster_name)"
kube_context "$HUB_CLUSTER" "$REGION"
kube_context "$FRONTEND_CLUSTER" "$REGION"

echo "== cluster B ($HUB_CLUSTER): hub"
kubectl --context "$HUB_CLUSTER" apply -f "$OUT/hub.yaml"
kubectl --context "$HUB_CLUSTER" -n mcp-hub create configmap hub-code \
  --from-file="$HUB_SRC/app.py" --from-file="$HUB_SRC/entry_identity.py" \
  --from-file="$HUB_SRC/mcphub_hmac.py" --from-file="$HUB_SRC/requirements.txt" \
  --dry-run=client -o yaml | kubectl --context "$HUB_CLUSTER" apply -f -
kubectl --context "$HUB_CLUSTER" -n mcp-hub rollout restart deployment/hub
kubectl --context "$HUB_CLUSTER" -n mcp-hub rollout status deployment/hub --timeout=300s

echo "== cluster A ($FRONTEND_CLUSTER): frontend + intruder"
kubectl --context "$FRONTEND_CLUSTER" apply -f "$OUT/frontend.yaml"
kubectl --context "$FRONTEND_CLUSTER" -n "$FRONTEND_NAMESPACE" create configmap frontend-client \
  --from-file="$HERE/client/sigv4_mcp_client.py" \
  --dry-run=client -o yaml | kubectl --context "$FRONTEND_CLUSTER" apply -f -
kubectl --context "$FRONTEND_CLUSTER" -n "$FRONTEND_NAMESPACE" wait --for=condition=Ready pod/frontend pod/intruder --timeout=300s

echo "deployed; run ./verify.sh"

#!/usr/bin/env bash
# Deploy Coder into the caller cluster and push the mcp-hub template.
# Run after `terraform apply` (../terraform, with coder.tf) and ../deploy.sh.
#
#   1. Keycloak: create or update the confidential client Coder uses
#      (keycloak_client.py), keep its secret in a Kubernetes Secret.
#   2. Database URL and owner password as Kubernetes Secrets (namespace coder).
#   3. Helm: the Coder server, Service on the NodePort the ALB targets.
#   4. Owner account (first run only) and the template, from inside the
#      server pod with its own coder binary.
#
# Nothing secret is printed or passed on a command line: every secret goes
# from one process into a Secret through a pipe or a file in a temp dir.
#
#   PYTHON=...   interpreter with boto3 (default python3)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TF="$ROOT/terraform"
export KUBECONFIG="$ROOT/.kubeconfig"
PY="${PYTHON:-python3}"
CHART_VERSION="2.38.8"
CODER_NS="coder"
CODER_OIDC_CLIENT="${CODER_OIDC_CLIENT:-coder-demo}"
# code-server 4.145.2, the multi-arch index digest.
WORKSPACE_IMAGE="docker.io/codercom/code-server:4.145.2@sha256:09565f9e07f5624554e295e8bab0e2fec4a7c533eb3cff459ed6596a4d3d7c1c"

"$PY" -c 'import boto3' 2>/dev/null || { echo "deploy.sh: $PY needs boto3 (PYTHON=/path/to/python)" >&2; exit 2; }

tmp="$(mktemp -d)"
# password_open=1 while this run has password sign-in open (to mint the deploy
# token): whatever ends the run then, the trap closes it again before leaving,
# so a failed mint never leaves an owner password sign-in behind.
password_open=0
on_exit() {
  rc=$?
  if [ "$password_open" = 1 ]; then
    echo "deploy.sh: closing password sign-in again after a failure" >&2
    helm_coder true || echo "deploy.sh: COULD NOT close password sign-in: run ./coder/deploy.sh again" >&2
  fi
  rm -rf "$tmp"
  exit "$rc"
}
trap on_exit EXIT
terraform -chdir="$TF" output -json > "$tmp/tf.json"
tf_out() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["value"])' "$tmp/tf.json" "$1"; }

REGION="$(tf_out region)"
CLUSTER="$(tf_out caller_cluster_name)"
CALLER_NS="$(tf_out caller_namespace)"
CODER_URL="$(tf_out coder_url)"
OIDC_ISSUER_URL="$(tf_out oidc_issuer_url)"
CODER_NODE_PORT="$(tf_out coder_node_port)"
DB_ENDPOINT="$(tf_out coder_db_endpoint)"
DB_SECRET="$(tf_out coder_db_secret_arn)"
case "$CODER_URL" in https://*.cloudfront.net) ;; *) echo "deploy.sh: unexpected coder_url $CODER_URL" >&2; exit 1 ;; esac

aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CLUSTER" >/dev/null
k() { kubectl --context "$CLUSTER" "$@"; }
k create namespace "$CODER_NS" --dry-run=client -o yaml | k apply -f - >/dev/null

echo "== Keycloak client $CODER_OIDC_CLIENT"
# keycloak_client.py applies the Secret coder-oidc itself (manifest on kubectl's
# stdin), so the client secret never passes through this script
CODER_OIDC_CLIENT="$CODER_OIDC_CLIENT" "$PY" "$HERE/keycloak_client.py" "$tmp/tf.json" "$CLUSTER" "$CODER_NS" >/dev/null

echo "== database and owner secrets"
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$DB_SECRET" \
  --query SecretString --output text > "$tmp/db-password"
# Generated without punctuation (coder.tf), so it goes into the URL as is.
printf 'postgres://coder:%s@%s/coder?sslmode=require' "$(cat "$tmp/db-password")" "$DB_ENDPOINT" > "$tmp/db-url"
k -n "$CODER_NS" create secret generic coder-db-url --from-file=url="$tmp/db-url" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
# The owner: created once, kept in the cluster only. Signs in with this
# password (owners always may); everybody else signs in with Keycloak.
if ! k -n "$CODER_NS" get secret coder-admin >/dev/null 2>&1; then
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(24), end="")' > "$tmp/admin-password"
  k -n "$CODER_NS" create secret generic coder-admin --from-file=password="$tmp/admin-password" >/dev/null
fi

# helm_coder <disable-password-auth>: install or upgrade the server, wait for
# the new pod, and set POD to it.
helm_coder() {
  sed -e "s#\${CODER_URL}#$CODER_URL#g" -e "s#\${OIDC_ISSUER_URL}#$OIDC_ISSUER_URL#g" \
      -e "s#\${CODER_OIDC_CLIENT}#$CODER_OIDC_CLIENT#g" -e "s#\${CODER_NODE_PORT}#$CODER_NODE_PORT#g" \
      -e "s#\${CALLER_NAMESPACE}#$CALLER_NS#g" -e "s#\${DISABLE_PASSWORD_AUTH}#$1#g" \
      "$HERE/values.yaml.tpl" > "$tmp/values.yaml"
  if grep -n '\${' "$tmp/values.yaml"; then echo "deploy.sh: unrendered variable in values.yaml" >&2; exit 1; fi
  helm --kube-context "$CLUSTER" upgrade --install coder coder \
    --repo https://helm.coder.com/v2 --version "$CHART_VERSION" \
    --namespace "$CODER_NS" --values "$tmp/values.yaml" --wait --timeout 10m >/dev/null
  # A changed Secret does not restart the pod by itself.
  k -n "$CODER_NS" rollout restart deployment/coder >/dev/null
  k -n "$CODER_NS" rollout status deployment/coder --timeout=300s >/dev/null
  # The new pod only: right after the restart the old one may still be terminating.
  POD="$(k -n "$CODER_NS" get pod -l app.kubernetes.io/name=coder --field-selector=status.phase=Running \
    -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1)"
  [ -n "$POD" ] || { echo "deploy.sh: no running Coder pod" >&2; exit 1; }
}
kx() { k -n "$CODER_NS" exec -i "$POD" -- "$@"; }

# The deploy token: an API token of the owner, kept in the cluster only.
# Getting it needs a password sign-in, and with password auth disabled Coder
# 2.38 refuses that to everyone, owners included. So the first run opens
# password sign-in, creates (or signs in) the owner, mints the token, and
# closes it again; later runs use the token.
# The token lives 7 days (Coder's maximum for an API token here); an expired
# one is minted again the same way.
token_ok() {
  k -n "$CODER_NS" get secret coder-deploy-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d > "$tmp/deploy-token" || return 1
  [ -s "$tmp/deploy-token" ] || return 1
  kx sh -c 'read -r t; curl -fsS -o /dev/null -H "Coder-Session-Token: $t" http://127.0.0.1:8080/api/v2/users/me' < "$tmp/deploy-token" 2>/dev/null
}
POD="$(k -n "$CODER_NS" get pod -l app.kubernetes.io/name=coder --field-selector=status.phase=Running \
  -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | head -1)"
if [ -z "$POD" ] || ! token_ok; then
  echo "== Coder server (chart $CHART_VERSION), password sign-in open to mint the deploy token"
  password_open=1
  helm_coder false
  kx sh -eu -c '
    export CODER_URL=http://127.0.0.1:8080 CODER_CONFIG_DIR=/tmp/coder-cli
    pw="$(cat /etc/coder-admin/password)"
    if [ "$(curl -s -o /dev/null -w %{http_code} "$CODER_URL/api/v2/users/first")" = 404 ]; then
      CODER_FIRST_USER_USERNAME=admin CODER_FIRST_USER_EMAIL=admin@coder.invalid CODER_FIRST_USER_PASSWORD="$pw" \
        CODER_FIRST_USER_FULL_NAME="Coder admin" CODER_FIRST_USER_TRIAL=false \
        /opt/coder login "$CODER_URL" >/dev/null
    fi
    tok="$(printf "{\"email\":\"admin@coder.invalid\",\"password\":\"%s\"}" "$pw" | curl -fsS -X POST \
      "$CODER_URL/api/v2/users/login" -H "Content-Type: application/json" --data-binary @- \
      | sed -n "s/.*\"session_token\":\"\([^\"]*\)\".*/\1/p")"
    [ -n "$tok" ] || { echo "cannot sign in as the owner" >&2; exit 1; }
    CODER_SESSION_TOKEN="$tok" /opt/coder tokens create --name "deploy-sh-$(date +%Y%m%d%H%M%S)" --lifetime 168h
    rm -rf /tmp/coder-cli
  ' > "$tmp/deploy-token"
  [ -s "$tmp/deploy-token" ] || { echo "deploy.sh: no deploy token" >&2; exit 1; }
  k -n "$CODER_NS" create secret generic coder-deploy-token --from-file=token="$tmp/deploy-token" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
fi
k -n "$CODER_NS" get secret coder-deploy-token -o jsonpath='{.data.token}' | base64 -d > "$tmp/deploy-token"

echo "== Coder server (chart $CHART_VERSION), Keycloak sign-in only"
helm_coder true
password_open=0

echo "== template mcp-hub"
kx sh -c 'rm -rf /tmp/mcp-hub && mkdir -p /tmp/mcp-hub'
k -n "$CODER_NS" cp "$HERE/template/main.tf" "$POD:/tmp/mcp-hub/main.tf" >/dev/null
k -n "$CODER_NS" cp "$HERE/template/startup.sh" "$POD:/tmp/mcp-hub/startup.sh" >/dev/null
# The token on stdin, never on a command line.
kx sh -eu -c "read -r t; CODER_URL=http://127.0.0.1:8080 CODER_SESSION_TOKEN=\$t /opt/coder templates push mcp-hub \
  --directory /tmp/mcp-hub --yes \
  --variable namespace=$CALLER_NS --variable service_account=coder \
  --variable coder_internal_url=http://coder.$CODER_NS.svc.cluster.local \
  --variable jwt_gateway_url=$(tf_out jwt_gateway_url) --variable region=$REGION \
  --variable claude_model_main=$(tf_out claude_model_main) --variable claude_model_fast=$(tf_out claude_model_fast) \
  --variable codex_model=$(tf_out codex_model) \
  --variable image=$WORKSPACE_IMAGE --message deploy.sh" < "$tmp/deploy-token"

echo "deployed: open $CODER_URL and sign in with Keycloak"

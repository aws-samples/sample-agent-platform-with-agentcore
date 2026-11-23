#!/usr/bin/env bash
# End-to-end checks of the AgentCore gateway entry demo, run from inside the
# pods. Exit code 0 only when every check passes. A probe that cannot run
# (kubectl exec fails, a pod is missing, no token) is a FAIL, never a pass by
# omission.
#
#   PYTHON=...          interpreter for scripts/user_token.py (needs boto3)
#   ALLOWED_USER=alice  a demo user in a team both the gateway and the hub allow
#   DENIED_USER=bob     a demo user in a team the gateway's Cedar policy does not allow
#   HUB_DENIED_USER=carol  a demo user the gateway lets through and the hub refuses
#                       (checked only when hub_allowed_teams is set)
#
# User tokens are sent from ws-probe, a pod on the workspace pool: the JWT
# gateway is reachable only from that subnet (terraform/endpoints.tf). The
# application checks run in static-app and intruder on the app pool. Group 9
# checks the other direction: a valid token or credential from the wrong
# subnet, or from outside the VPC, does not get in. A resource policy change
# takes up to 15 minutes, so run group 9 that long after an apply.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TF="$HERE/terraform"
export KUBECONFIG="$HERE/.kubeconfig"
PY="${PYTHON:-python3}"
# A probe that cannot run must say why. Without boto3 the token script fails
# on import, and "no token" would read as an IdP problem.
"$PY" -c 'import boto3' 2>/dev/null || { echo "verify.sh: $PY has no boto3 (scripts/user_token.py needs it); set PYTHON=/path/to/python"; exit 2; }
ALLOWED_USER="${ALLOWED_USER:-alice}"
DENIED_USER="${DENIED_USER:-bob}"
HUB_DENIED_USER="${HUB_DENIED_USER:-carol}"

tf_json="$(mktemp)"; tok_err="$(mktemp)"
trap 'rm -f "$tf_json" "$tok_err"' EXIT
terraform -chdir="$TF" output -json > "$tf_json" || { echo "terraform output failed"; exit 2; }
tf_out() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["value"])' "$tf_json" "$1"; }

REGION="$(tf_out region)"
HUB_CLUSTER="$(tf_out hub_cluster_name)"
CALLER_CLUSTER="$(tf_out caller_cluster_name)"
HUB_NS="$(tf_out hub_namespace)"
NS="$(tf_out caller_namespace)"
IAM_URL="$(tf_out iam_gateway_url)"
JWT_URL="$(tf_out jwt_gateway_url)"
ALLOWED_TEAMS="$(tf_out allowed_teams)"         # a Python list literal, e.g. ['team-a', 'team-c']
HUB_ALLOWED_TEAMS="$(tf_out hub_allowed_teams)" # [] when the hub adds no rule of its own
HUB_HOST="$(tf_out hub_endpoint | sed -E 's#^https://([^/]+)/.*#\1#')"
NODE_PORT="$(tf_out hub_node_port)"

pass=0 fail=0
check() { # name, condition-exit-code, detail
  if [ "$2" -eq 0 ]; then echo "  [PASS] $1${3:+ - $3}"; pass=$((pass + 1))
  else echo "  [FAIL] $1${3:+ - $3}"; fail=$((fail + 1)); fi
}
# field <result-json> <python expression over r>: exit 0 when the expression is true
field() { "$PY" -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2" 2>/dev/null; }
show() { "$PY" -c 'import json,sys; r=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2" 2>/dev/null; }
# client <pod> <auth> <url> [stdin]: runs the MCP client in a pod, prints its RESULT json ({} if none).
# The checks read the RESULT fields, not the exit code: "denied" is reported only when the gateway
# answered with a refusal, "error" for anything that merely failed (see the client's own test).
client() {
  local pod="$1" auth="$2" url="$3" out
  out="$(kubectl --context "$CALLER_CLUSTER" -n "$NS" exec -i "$pod" -- python /client/gateway_mcp_client.py --auth "$auth" "$url" 2>&1)"
  echo "$out" | sed -n 's/^{"step"/     {"step"/p' | cut -c1-220 >&2
  local result; result="$(echo "$out" | sed -n 's/^RESULT //p' | tail -1)"
  [ -n "$result" ] || result="{}"
  echo "$result"
}

echo "== 1. static-app pod (no user) -> IAM gateway -> hub"
r="$(client static-app sigv4 "$IAM_URL" </dev/null)"
field "$r" 'r.get("call") == "ok" and r.get("tools") == ["hub___hub__whoami"]'
check "SigV4 with the pod's IRSA role: the tool is listed and the call succeeds" $? "$(show "$r" 'r.get("call"), r.get("tools")')"
field "$r" 'r["whoami"]["actor"] == "agentcore-gateway" and r["whoami"]["user"] is None and r["whoami"]["caller_arn"] is None'
check "the hub sees the gateway, not the calling application (no caller ARN, no user)" $? "$(show "$r" 'r.get("whoami")')"

echo "== 2. intruder pod (same IAM grant, no Cedar permit) -> IAM gateway"
r="$(client intruder sigv4 "$IAM_URL" </dev/null)"
field "$r" 'r.get("initialize") == 200'
check "IAM admits the intruder to the gateway (it holds InvokeGateway)" $? "initialize $(show "$r" 'r.get("initialize")')"
field "$r" 'r.get("tools") == [] and r.get("call") == "denied" and r.get("whoami") is None'
check "Cedar permits it nothing: no tool listed, the call refused" $? "$(show "$r" 'r.get("call"), r.get("tools"), r.get("detail", "")[:120]')"

echo "== 3. signed-in user the gateway and the hub allow ($ALLOWED_USER) -> JWT gateway -> hub"
token="$("$PY" "$HERE/scripts/user_token.py" "$tf_json" "$ALLOWED_USER" 2>"$tok_err")"
[ -n "$token" ]; check "obtained an access token for $ALLOWED_USER" $? "$(tail -1 "$tok_err" | cut -c1-160)"
r="$(printf '%s\n' "$token" | client ws-probe bearer "$JWT_URL")"
field "$r" 'r.get("call") == "ok" and r.get("tools") == ["hub___hub__whoami"]'
check "the user's token: the tool is listed and the call succeeds" $? "$(show "$r" 'r.get("call"), r.get("tools")')"
field "$r" "r['whoami']['user'] == '$ALLOWED_USER' and set(r['whoami']['departments']) & set($ALLOWED_TEAMS)"
check "the hub still knows the user (token exchanged by the gateway)" $? "$(show "$r" 'r.get("whoami")')"

echo "== 4. signed-in user outside $ALLOWED_TEAMS ($DENIED_USER) -> JWT gateway"
token="$("$PY" "$HERE/scripts/user_token.py" "$tf_json" "$DENIED_USER" 2>"$tok_err")"
[ -n "$token" ]; check "obtained an access token for $DENIED_USER" $? "$(tail -1 "$tok_err" | cut -c1-160)"
r="$(printf '%s\n' "$token" | client ws-probe bearer "$JWT_URL")"
field "$r" 'r.get("initialize") == 200 and r.get("tools") == [] and r.get("call") == "denied" and r.get("whoami") is None'
check "a valid token of another team: no tool listed, the call refused by Cedar" $? "$(show "$r" 'r.get("call"), r.get("tools"), r.get("detail", "")[:120]')"

if [ "$HUB_ALLOWED_TEAMS" != "[]" ]; then
  echo "== 4b. signed-in user the gateway admits and the hub refuses ($HUB_DENIED_USER; hub allows $HUB_ALLOWED_TEAMS)"
  token="$("$PY" "$HERE/scripts/user_token.py" "$tf_json" "$HUB_DENIED_USER" 2>"$tok_err")"
  [ -n "$token" ]; check "obtained an access token for $HUB_DENIED_USER" $? "$(tail -1 "$tok_err" | cut -c1-160)"
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  r="$(printf '%s\n' "$token" | client ws-probe bearer "$JWT_URL")"
  # Cedar lets the user through: the tool is listed. The hub then refuses the
  # call with its own reason, not the gateway's policy enforcement.
  field "$r" 'r.get("tools") == ["hub___hub__whoami"] and r.get("call") != "ok" and "hub policy" in r.get("detail", "")'
  check "the gateway lists the tool, the hub refuses the call with its own rule" $? "$(show "$r" 'r.get("call"), r.get("tools"), r.get("detail", "")[:140]')"
  sleep 2
  logs="$(kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" logs deploy/hub --since-time="$since" 2>/dev/null)"
  echo "$logs" | grep -q "gateway entry refused tool hub__whoami: user=$HUB_DENIED_USER"
  check "the hub logged the refusal (the request did reach it)" $? "$(echo "$logs" | grep -c 'refused tool') refused line(s) since $since"
fi

echo "== 5. the wrong credential at each gateway"
r="$(client static-app none "$IAM_URL" </dev/null)"
field "$r" 'r.get("initialize") in (401, 403) and r.get("call") == "skipped"'
check "IAM gateway refuses an unsigned request" $? "status $(show "$r" 'r.get("initialize")')"
r="$(client ws-probe none "$JWT_URL" </dev/null)"
field "$r" 'r.get("initialize") == 401 and "resource_metadata" in r.get("detail", "")'
check "JWT gateway answers 401 with the discovery pointer an MCP client signs in from" $? "status $(show "$r" 'r.get("initialize")')"
# (The application's SigV4 at the JWT gateway is now refused by the network:
# static-app cannot reach that gateway at all, group 9.)
r="$(printf '%s\n' "$token" | client static-app bearer "$IAM_URL")"
field "$r" 'r.get("initialize") in (401, 403) and r.get("call") == "skipped"'
check "IAM gateway refuses a user's bearer token" $? "status $(show "$r" 'r.get("initialize")')"

echo "== 6. the coder pod's role holds no grant on either gateway"
for g in iam jwt; do
  decision="$(aws iam simulate-principal-policy --policy-source-arn "$(tf_out coder_role_arn)" \
    --action-names bedrock-agentcore:InvokeGateway --resource-arns "$(tf_out ${g}_gateway_arn)" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null)"
  [ "$decision" = "implicitDeny" ]; check "coder role, InvokeGateway on the $g gateway" $? "${decision:-no answer}"
done

echo "== 7. nothing but the gateways reaches the hub"
# Prints exactly one of: blocked (no TCP connection), reached (connection
# established - the path is open whatever happened next), error (proved nothing).
PROBE='
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
except OSError as e:
    print("error: name did not resolve:", type(e).__name__); sys.exit()
s = socket.socket(); s.settimeout(8)
try:
    s.connect(addr)
except (socket.timeout, TimeoutError, ConnectionRefusedError) as e:
    print("blocked:", type(e).__name__, addr[0]); sys.exit()
except OSError as e:
    print("error: connect failed:", type(e).__name__, addr[0]); sys.exit()
print("reached:", addr[0])
'
probe() { kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" exec neighbour -- python -c "$PROBE" "$1" "$2" 2>&1 | tail -1; }
out="$(probe "$HUB_HOST" 443)"
case "$out" in blocked:*) rc=0 ;; *) rc=1 ;; esac
check "a pod in the hub VPC cannot connect to the hub NLB" $rc "$out"
pod_ip="$(kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" get pod -l app=hub -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)"
out="$(probe "${pod_ip:-0.0.0.0}" 8000)"
case "$out" in blocked:*) rc=0 ;; *) rc=1 ;; esac
[ -n "$pod_ip" ] || rc=1
check "a pod on the same node cannot connect to the hub pod" $rc "$out"
node_ip="$(kubectl --context "$HUB_CLUSTER" -n "$HUB_NS" get pod -l app=hub -o jsonpath='{.items[0].status.hostIP}' 2>/dev/null)"
out="$(probe "${node_ip:-0.0.0.0}" "$NODE_PORT")"
case "$out" in blocked:*) rc=0 ;; *) rc=1 ;; esac
[ -n "$node_ip" ] || rc=1
check "a pod cannot connect to the hub's NodePort" $rc "$out"

echo "== 8. the Coder entry: through CloudFront, for the operator only"
CODER_URL="$(tf_out coder_url 2>/dev/null || true)"
if [ -z "$CODER_URL" ]; then
  check "coder.tf applied (terraform output coder_url)" 1 "no coder_url"
else
  # This machine is on the allow list, so the dashboard answers.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$CODER_URL/healthz")"
  [ "$code" = "200" ]; check "the operator reaches Coder through CloudFront" $? "GET /healthz -> $code"
  # Everyone else: the WAF blocks by default and allows one IP set.
  acl_id="$(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 --query "WebACLs[?Name=='mcp-gw-entry-coder'].Id | [0]" --output text 2>/dev/null)"
  acl="$(aws wafv2 get-web-acl --scope CLOUDFRONT --region us-east-1 --name mcp-gw-entry-coder --id "$acl_id" --output json 2>/dev/null || echo '{}')"
  field "$acl" '"Block" in r["WebACL"]["DefaultAction"] and len(r["WebACL"]["Rules"]) == 1 and "Allow" in r["WebACL"]["Rules"][0]["Action"] and "IPSetReferenceStatement" in r["WebACL"]["Rules"][0]["Statement"]'
  check "the Coder WAF blocks by default and allows one IP set" $?
  # The ALB has no public address: its name resolves to private IPs only.
  alb="$(tf_out coder_alb_dns_name)"
  ips="$("$PY" -c 'import socket,sys; print(" ".join(sorted({a[4][0] for a in socket.getaddrinfo(sys.argv[1], 80, socket.AF_INET)})))' "$alb" 2>/dev/null)"
  "$PY" -c 'import ipaddress,sys; i=sys.argv[1].split(); sys.exit(0 if i and all(ipaddress.ip_address(x).is_private for x in i) else 1)' "$ips"
  check "the Coder ALB is internal (private addresses only)" $? "${ips:-did not resolve}"
fi

echo "== 9. a valid token or credential, from where its gateway does not admit it"
# 9a: alice's valid token from the app subnet. The JWT gateway's host resolves
# to the workspace endpoint, whose security group admits the workspace subnet
# only: the TCP connection times out (a security group drops, it does not
# reset). Only a timeout counts: a DNS failure, a TLS error or a refused
# connection is some other fault and fails the check, since it would look the
# same with the endpoint wide open.
SG_DROPPED='r.get("initialize") == 0 and r.get("call") == "skipped" and r.get("tools") is None and r.get("transport_error", "").startswith("TimeoutError")'

token="$("$PY" "$HERE/scripts/user_token.py" "$tf_json" "$ALLOWED_USER" 2>"$tok_err")"
[ -n "$token" ]; check "obtained an access token for $ALLOWED_USER (for group 9)" $? "$(tail -1 "$tok_err" | cut -c1-160)"
r="$(printf '%s\n' "$token" | client static-app bearer "$JWT_URL")"
field "$r" "$SG_DROPPED"
check "a valid user token from the app subnet cannot reach the JWT gateway" $? "$(show "$r" 'r.get("initialize"), r.get("transport_error", "")[:100]')"
# 9b: the same token from this machine, outside the VPC: the public endpoint
# answers, and the gateway's resource policy refuses (not via its endpoint).
r="$(printf '%s\n' "$token" | "$PY" "$HERE/client/gateway_mcp_client.py" --auth bearer "$JWT_URL" 2>&1 | sed -n 's/^RESULT //p' | tail -1)"
[ -n "$r" ] || r="{}"
# Refused before Cedar: initialize itself gets the gateway's 403 with
# -32002 "Insufficient permissions", so no tool list at all (a Cedar refusal
# would list none). Anything else (no answer, a DNS or TLS error, a 5xx, a
# 401 for a bad token) is not this refusal and fails the check.
REFUSED='r.get("initialize") == 403 and not r.get("transport_error") and r.get("tools") is None and r.get("call") == "skipped" and "-32002" in r.get("detail", "") and "Insufficient permissions" in r.get("detail", "")'
field "$r" "$REFUSED"
check "the same valid token from outside the VPC is refused by the JWT gateway's resource policy" $? "$(show "$r" 'r.get("initialize"), r.get("call"), r.get("detail", "")[:120]')"
# 9c: a valid SigV4 signature from this machine: the operator's own
# credentials, which IAM allows everything. Outside the app endpoint the IAM
# gateway's resource policy refuses it before Cedar is asked (no tool list at
# all); a stolen static-app credential is in the same place. Without the
# policy the call would reach Cedar and come back with an empty tool list.
r="$("$PY" "$HERE/client/gateway_mcp_client.py" --auth sigv4 "$IAM_URL" </dev/null 2>&1 | sed -n 's/^RESULT //p' | tail -1)"
[ -n "$r" ] || r="{}"
field "$r" "$REFUSED"
check "a valid SigV4 signature from outside the VPC is refused by the IAM gateway's resource policy" $? "$(show "$r" 'r.get("initialize"), r.get("detail", "")[:120]')"
# 9d: the workspace pool cannot reach the IAM gateway either (a timeout, as 9a).
r="$(client ws-probe none "$IAM_URL" </dev/null)"
field "$r" "$SG_DROPPED"
check "the workspace subnet cannot reach the IAM gateway" $? "$(show "$r" 'r.get("initialize"), r.get("transport_error", "")[:100]')"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]

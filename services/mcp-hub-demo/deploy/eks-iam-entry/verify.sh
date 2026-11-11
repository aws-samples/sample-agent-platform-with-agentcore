#!/usr/bin/env bash
# Assert the pod-to-pod IAM entry end to end, from inside the pods.
#
#   1. frontend pod (cluster A, IRSA frontend role) -> API -> hub pod: the MCP
#      exchange succeeds and the hub names the frontend role as the caller.
#   2. intruder pod (same namespace and node, IRSA intruder role): refused by
#      API Gateway (403) - it never reaches the hub.
#   3. unsigned request from the frontend pod: refused by API Gateway (403).
#   4. a forged x-caller-arn on a signed request is overwritten by the gateway:
#      the hub still sees the frontend role.
#   5. a pod beside the hub in cluster B, sending a forged x-caller-arn straight
#      to the hub pod: cannot connect (NetworkPolicy).
#   6. the same pod sending the forged identity to the hub's NLB instead: cannot
#      connect (the NLB admits only the VPC Link). Forwarded through the NLB the
#      packet would arrive from an lb-subnet address the NetworkPolicy admits,
#      so this is the path that would make `trust: network` forgeable.
#   7. no static AWS key anywhere: no AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
#      in either pod, and the frontend's credentials are an IRSA session.
#
# Exit code 0 only when every check passes. A probe that cannot run (kubectl
# exec fails, the pod is missing) is a FAIL, never a pass by omission.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TF="$HERE/terraform"
export KUBECONFIG="$HERE/.kubeconfig"

tf_out() { terraform -chdir="$TF" output -raw "$1"; }
HUB_CLUSTER="$(tf_out hub_cluster_name)"
FRONTEND_CLUSTER="$(tf_out frontend_cluster_name)"
NS="$(tf_out frontend_namespace)"
FRONTEND_ROLE_ARN="$(tf_out frontend_role_arn)"
FRONTEND_ROLE="${FRONTEND_ROLE_ARN##*/}"
# A session of exactly that role: same partition, same account, same name.
# arn:aws:iam::<acct>:role/<name>  ->  arn:aws:sts::<acct>:assumed-role/<name>/
FRONTEND_SESSION_PREFIX="$(printf '%s' "$FRONTEND_ROLE_ARN" | sed -E 's#^arn:([^:]+):iam::([0-9]+):role/(.*/)?([^/]+)$#arn:\1:sts::\2:assumed-role/\4/#')"
case "$FRONTEND_SESSION_PREFIX" in arn:*:sts::*:assumed-role/*/) ;; *) echo "cannot derive the frontend session prefix from $FRONTEND_ROLE_ARN"; exit 2 ;; esac

pass=0 fail=0
check() { # name, condition-exit-code, detail
  if [ "$2" -eq 0 ]; then echo "  [PASS] $1${3:+ - $3}"; pass=$((pass + 1))
  else echo "  [FAIL] $1${3:+ - $3}"; fail=$((fail + 1)); fi
}
in_pod() { local ctx="$1" ns="$2" pod="$3"; shift 3; kubectl --context "$ctx" -n "$ns" exec "$pod" -- "$@"; }

# probe <host> from the neighbour pod: prints exactly one of
#   blocked: <why>   the TCP connection could not be established (timeout or
#                    refused) to an address the name resolved to
#   reached: <line>  the connection was established - whatever happened next
#                    (an answer, a reset, a read timeout), the path is open
#   error: <why>     the probe proved nothing (name did not resolve, exec failed)
# Only "blocked" is evidence of isolation.
PROBE='
import socket, sys
host, arn = sys.argv[1], sys.argv[2]
try:
    addr = socket.getaddrinfo(host, 8000, socket.AF_INET, socket.SOCK_STREAM)[0][4]
except OSError as e:
    print("error: name did not resolve:", type(e).__name__); sys.exit()
s = socket.socket(); s.settimeout(8)
try:
    s.connect(addr)
except (socket.timeout, TimeoutError, ConnectionRefusedError) as e:
    print("blocked:", type(e).__name__, addr[0]); sys.exit()
except OSError as e:
    print("error: connect failed:", type(e).__name__, addr[0]); sys.exit()
try:
    s.sendall(("POST /mcp HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\n"
               "accept: application/json, text/event-stream\r\nx-caller-arn: " + arn +
               "\r\ncontent-length: 2\r\n\r\n{}").encode())
    line = s.recv(64).split(b"\r\n")[0].decode(errors="replace")
    print("reached:", addr[0], line or "(connection closed)")
except OSError as e:
    print("reached:", addr[0], "then", type(e).__name__)
'
probe() { in_pod "$HUB_CLUSTER" mcp-hub neighbour python -c "$PROBE" "$1" "$FRONTEND_ROLE_ARN" 2>&1 | tail -1; }

echo "== 1. frontend pod -> API Gateway (IAM) -> hub pod"
out="$(in_pod "$FRONTEND_CLUSTER" "$NS" frontend python /client/sigv4_mcp_client.py "$(tf_out entry_url)" 2>&1)"; exec_rc=$?
echo "$out" | sed 's/^/     /'
# The client exits 0 only for a complete exchange; a nonzero exit (or a
# failed exec) voids whatever RESULT line was printed.
result="$(echo "$out" | sed -n 's/^RESULT //p')"
[ "$exec_rc" -eq 0 ] || result='{"ok": false}'
check "the client (and the exec) exited 0" "$exec_rc" "exit $exec_rc"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r.get("ok") and r.get("actor")=="frontend-app" else 1)' "$result" 2>/dev/null
check "MCP exchange succeeds and the hub resolves actor frontend-app" $?
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r.get("caller_arn","").startswith(sys.argv[2]) else 1)' "$result" "$FRONTEND_SESSION_PREFIX" 2>/dev/null
check "the identity the hub saw is a session of the frontend IRSA role" $? "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("caller_arn",""))' "$result" 2>/dev/null)"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r.get("tools")==["hub__whoami"] and r.get("user") is None else 1)' "$result" 2>/dev/null
check "application-only call: no user, no backend tools" $?

echo "== 2. intruder pod (other IRSA role, same network path)"
out="$(in_pod "$FRONTEND_CLUSTER" "$NS" intruder python /client/sigv4_mcp_client.py "$(tf_out entry_url)" 2>&1)"
echo "$out" | sed -n '1,2p' | sed 's/^/     /'
status="$(echo "$out" | sed -n 's/^RESULT //p' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null)"
[ "$status" = "403" ]; check "intruder refused by API Gateway" $? "status $status"

echo "== 3. unsigned request from the frontend pod"
code="$(in_pod "$FRONTEND_CLUSTER" "$NS" frontend python -c '
import os, sys
sys.path.insert(0, "/client")
from sigv4_mcp_client import post
r = post(os.environ["ENTRY_URL"], b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}",
         {"content-type": "application/json", "accept": "application/json, text/event-stream",
          "x-caller-arn": "'"$FRONTEND_ROLE_ARN"'"}, timeout=15)
print(r.status)' 2>&1 | tail -1)"
[ "$code" = "403" ]; check "unsigned request refused by API Gateway, forged header or not" $? "status $code"

echo "== 4. signed request carrying a forged x-caller-arn"
who="$(in_pod "$FRONTEND_CLUSTER" "$NS" frontend python -c '
import json, os, sys
sys.path.insert(0, "/client")
from sigv4_mcp_client import SigV4Signer, post, region_of
url = os.environ["ENTRY_URL"]
s = SigV4Signer(region_of(url))
body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "hub__whoami", "arguments": {}}}).encode()
h = {"content-type": "application/json", "accept": "application/json, text/event-stream",
     "x-caller-arn": "arn:aws:sts::" + "0" * 12 + ":assumed-role/someone-else/forged"}
r = post(url, body, s.headers("POST", url, body, h), timeout=15)
res = (json.loads(r.body) if r.status == 200 else {}).get("result") or {}
print(json.loads(res["content"][0]["text"])["caller_arn"] if res.get("content") else f"status {r.status}")' 2>&1 | tail -1)"
case "$who" in "$FRONTEND_SESSION_PREFIX"*) rc=0 ;; *) rc=1 ;; esac
check "the gateway overwrites a caller-supplied x-caller-arn" $rc "$who"

echo "== 5. neighbour pod in cluster B -> hub pod directly, forged identity"
HUB_POD_IP="$(kubectl --context "$HUB_CLUSTER" -n mcp-hub get pod -l app=hub -o jsonpath='{.items[0].status.podIP}')"
if [ -n "$HUB_POD_IP" ]; then direct="$(probe "$HUB_POD_IP")"; else direct="error: no hub pod IP"; fi
case "$direct" in blocked:*) rc=0 ;; *) rc=1 ;; esac
check "a pod beside the hub cannot reach it (NetworkPolicy)" $rc "$direct"

echo "== 6. neighbour pod in cluster B -> hub NLB, forged identity"
# Through the NLB a forged header would reach the hub from an address the
# NetworkPolicy admits; an established connection fails the check whatever
# the hub then answers.
via_nlb="$(probe "$(tf_out hub_nlb_dns_name)")"
case "$via_nlb" in blocked:*) rc=0 ;; *) rc=1 ;; esac
check "a VPC-B pod cannot reach the hub through its NLB (only the VPC Link can)" $rc "$via_nlb"

echo "== 7. no static AWS key in either pod"
# Each pod's environment separately, each read required to succeed: a failed
# exec must not leave the other pod's output to answer for both.
for target in "frontend:$FRONTEND_CLUSTER:$NS:frontend" "hub:$HUB_CLUSTER:mcp-hub:deploy/hub"; do
  IFS=: read -r label ctx ns pod <<<"$target"
  if env_out="$(in_pod "$ctx" "$ns" "$pod" env 2>&1)" && [ -n "$env_out" ]; then
    echo "$env_out" | grep -q -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)='
    [ $? -ne 0 ]; check "no AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN in the $label pod" $?
  else
    check "no AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN in the $label pod" 1 "could not read its environment"
  fi
  [ "$label" = frontend ] && frontend_env="$env_out"
done
echo "${frontend_env:-}" | grep -q '^AWS_WEB_IDENTITY_TOKEN_FILE='; check "the frontend's AWS identity is IRSA (projected web-identity token)" $?
ident="$(in_pod "$FRONTEND_CLUSTER" "$NS" frontend python -c 'import boto3; print(boto3.Session().get_credentials().method)' 2>&1 | tail -1)"
[ "$ident" = "assume-role-with-web-identity" ]; check "botocore resolved its credentials by AssumeRoleWithWebIdentity" $? "$ident"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]

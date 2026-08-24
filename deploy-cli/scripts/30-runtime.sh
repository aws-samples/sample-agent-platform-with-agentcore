#!/bin/bash
# Phase 3 — IAM execution roles (one per kernel) + workspace-access role,
# then the three AgentCore runtimes. Mirrors terraform modules/runtime.
. "$(dirname "$0")/../lib/common.sh"
load
: "${VPC_ID:?run 10-network.sh first}"
: "${WORKSPACE_BUCKET:?run 20-platform.sh first}"

step "runtime roles + AgentCore runtimes ($NAME)"

WS_ARN="arn:aws:s3:::$WORKSPACE_BUCKET"
# No gateway-secret ARN is looked up here on purpose. A kernel role is reachable
# from inside the session it serves — root shell in the Dev Workbench microVM,
# agent tools in the headless kernel's subprocess — so a kernel that *can* read
# the gateway key is a kernel whose users have it. Only the llm-edge task role
# holds that read (35-llm-edge.sh); kernels reach the gateway through it with a
# per-session grant the backend mints.
REPO_ARNS="$(for r in "${KERNEL_REPOS[@]}"; do
  aws ecr describe-repositories --repository-names "agent-platform${SUFFIX}/$r" \
    --query 'repositories[0].repositoryArn' --output text; done | paste -sd',' - \
  | sed 's/[^,]*/"&"/g')"

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

role_ensure() {  # name trust-json description
  local rn="$1" trust="$2" desc="$3" arn
  arn="$(aws iam get-role --role-name "$rn" --query 'Role.Arn' --output text 2>/dev/null || echo None)"
  if [ "$arn" = "None" ]; then
    arn="$(aws iam create-role --role-name "$rn" --assume-role-policy-document "$trust" \
      --description "$desc" --query 'Role.Arn' --output text)"
    log "created role $rn" >&2
  else
    log "role exists $rn" >&2
  fi
  echo "$arn"
}

# ---------------------------------------------------------------- policies
base_stmts() { cat <<JSON
{"Sid":"EcrPull","Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":[$REPO_ARNS]}
{"Sid":"EcrAuth","Effect":"Allow","Action":"ecr:GetAuthorizationToken","Resource":"*"}
{"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogGroups","logs:DescribeLogStreams"],"Resource":"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:/aws/bedrock-agentcore/*"}
JSON
}

agent_stmts() { cat <<JSON
{"Sid":"Skills","Effect":"Allow","Action":"s3:GetObject","Resource":"$WS_ARN/skills/*"}
{"Sid":"SkillsList","Effect":"Allow","Action":"s3:ListBucket","Resource":"$WS_ARN","Condition":{"StringLike":{"s3:prefix":["skills/*","skills/"]}}}
{"Sid":"BedrockInvoke","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"}
{"Sid":"InvokeMcpRuntimes","Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":["arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:runtime/mcp_tools_kernel*","arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:runtime/mcp_tools_kernel*/runtime-endpoint/*"]}
{"Sid":"InvokeGateways","Effect":"Allow","Action":"bedrock-agentcore:InvokeGateway","Resource":["arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:gateway/*","arn:aws:bedrock-agentcore:us-east-1:$ACCOUNT_ID:gateway/*"]}
{"Sid":"BuiltinTools","Effect":"Allow","Action":["bedrock-agentcore:StartCodeInterpreterSession","bedrock-agentcore:InvokeCodeInterpreter","bedrock-agentcore:StopCodeInterpreterSession","bedrock-agentcore:GetCodeInterpreterSession","bedrock-agentcore:StartBrowserSession","bedrock-agentcore:StopBrowserSession","bedrock-agentcore:GetBrowserSession","bedrock-agentcore:UpdateBrowserStream","bedrock-agentcore:ConnectBrowserAutomationStream","bedrock-agentcore:ConnectBrowserLiveViewStream"],"Resource":["arn:aws:bedrock-agentcore:$AWS_REGION:aws:code-interpreter/aws.codeinterpreter.v1","arn:aws:bedrock-agentcore:$AWS_REGION:aws:browser/aws.browser.v1","arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:code-interpreter/*","arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:browser/*"]}
JSON
}

sdk_stmts() { cat <<JSON
{"Sid":"AsyncArtifacts","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload"],"Resource":["$WS_ARN/feeds/*","$WS_ARN/topic-selection/*"]}
{"Sid":"AsyncArtifactsList","Effect":"Allow","Action":"s3:ListBucket","Resource":"$WS_ARN","Condition":{"StringLike":{"s3:prefix":["feeds/*","feeds/","topic-selection/*","topic-selection/"]}}}
{"Sid":"McpSecrets","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT_ID:secret:agent-platform*/remote-mcp-key*"}
{"Sid":"MemoryData","Effect":"Allow","Action":["bedrock-agentcore:CreateEvent","bedrock-agentcore:GetEvent","bedrock-agentcore:ListEvents","bedrock-agentcore:ListActors","bedrock-agentcore:ListSessions","bedrock-agentcore:GetMemoryRecord","bedrock-agentcore:ListMemoryRecords","bedrock-agentcore:RetrieveMemoryRecords"],"Resource":"arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:memory/*"}
JSON
}

# Assemble the document in python rather than by string-pasting in shell: the
# statement helpers emit one JSON object per line, and joining them by hand left
# newlines where commas belonged (a malformed policy that IAM would have taken
# happily as far as syntax goes, but which the validator below catches first).
put_policy() {  # role-name  newline-separated-statements
  local rn="$1" stmts="$2"
  printf '%s' "$stmts" | python3 -c '
import json, sys
objs = [json.loads(l) for l in sys.stdin.read().splitlines() if l.strip()]
json.dump({"Version": "2012-10-17", "Statement": objs}, open("/tmp/pol.json", "w"))
print(len(objs))' > /tmp/pol.count || die "policy for $rn is not valid JSON"
  aws iam put-role-policy --role-name "$rn" --policy-name kernel --policy-document file:///tmp/pol.json
  log "policy attached: $rn ($(cat /tmp/pol.count) statements)"
}

R_INT="agent-platform-interactive-role${SUFFIX}"
R_SDK="agent-platform-sdk-role${SUFFIX}"
R_MCP="agent-platform-mcp-tools-role${SUFFIX}"
R_WS="agent-platform-workspace-access${SUFFIX}"

INT_ARN="$(role_ensure "$R_INT" "$TRUST" "claude-code-kernel: skills read; workspace sync uses backend-minted per-session credentials")"
SDK_ARN="$(role_ensure "$R_SDK" "$TRUST" "agent-sdk-kernel: skills read + async artifact prefixes")"
MCP_ARN="$(role_ensure "$R_MCP" "$TRUST" "mcp-tools-kernel: demo MCP server, no S3 access")"
save INTERACTIVE_ROLE_ARN "$INT_ARN"; save SDK_ROLE_ARN "$SDK_ARN"; save MCP_ROLE_ARN "$MCP_ARN"

put_policy "$R_INT" "$(base_stmts; agent_stmts)"
put_policy "$R_SDK" "$(base_stmts; agent_stmts; sdk_stmts)"
put_policy "$R_MCP" "$(base_stmts)"

# ---- workspace-access role: assumed by the backend per session ----
WS_TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::$ACCOUNT_ID:root\"},\"Action\":\"sts:AssumeRole\"}]}"
WS_ROLE_ARN="$(role_ensure "$R_WS" "$WS_TRUST" "Assumed per session by the backend; session policy narrows to that session prefix")"
cat > /tmp/ws-role-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Workspaces","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload"],"Resource":"$WS_ARN/workspaces/*"},
 {"Sid":"WorkspacesList","Effect":"Allow","Action":"s3:ListBucket","Resource":"$WS_ARN","Condition":{"StringLike":{"s3:prefix":["workspaces/*","workspaces/"]}}}]}
JSON
aws iam put-role-policy --role-name "$R_WS" --policy-name workspaces --policy-document file:///tmp/ws-role-pol.json
save WORKSPACE_ROLE_ARN "$WS_ROLE_ARN"
log "workspace-access role ready"

# ---------------------------------------------------------------- runtimes
# Env shared by both agent kernels (mirrors locals.common_env with use_bedrock=1).
common_env_json() { cat <<JSON
{"AWS_REGION":"$AWS_REGION","CLAUDE_CODE_USE_BEDROCK":"1","ANTHROPIC_MODEL":"$ANTHROPIC_MODEL","ANTHROPIC_SMALL_FAST_MODEL":"$ANTHROPIC_SMALL_FAST_MODEL","ANTHROPIC_DEFAULT_OPUS_MODEL":"$ANTHROPIC_DEFAULT_OPUS_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL":"$ANTHROPIC_MODEL","ANTHROPIC_DEFAULT_HAIKU_MODEL":"$ANTHROPIC_SMALL_FAST_MODEL"}
JSON
}

PRIV0="${PRIVATE_SUBNETS%%,*}"; PRIV1="${PRIVATE_SUBNETS##*,}"
NET_CFG="{\"networkMode\":\"VPC\",\"networkModeConfig\":{\"securityGroups\":[\"$RUNTIME_SG\"],\"subnets\":[\"$PRIV0\",\"$PRIV1\"]}}"

runtime_ensure() {  # logical-name image-repo role-arn protocol env-json
  local lname="$1" repo="$2" role="$3" proto="$4" env="$5"
  local rt_name="${lname}${RUNTIME_SUFFIX}" arn
  arn="$(aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='$rt_name'].agentRuntimeArn | [0]" --output text 2>/dev/null || echo None)"
  if [ "$arn" != "None" ] && [ -n "$arn" ]; then
    log "runtime exists $rt_name" >&2; echo "$arn"; return 0
  fi
  local uri="${REGISTRY}/agent-platform${SUFFIX}/${repo}:${IMAGE_TAG}"
  arn="$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$rt_name" \
    --description "$lname (CLI-deployed)" \
    --role-arn "$role" \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$uri\"}}" \
    --network-configuration "$NET_CFG" \
    --protocol-configuration "{\"serverProtocol\":\"$proto\"}" \
    --environment-variables "$env" \
    --query 'agentRuntimeArn' --output text)"
  log "created runtime $rt_name" >&2
  echo "$arn"
}

# AgentCore validates image pull with the execution role at CREATE time, so the
# inline policy must have propagated first. Terraform expresses this with
# depends_on; here it is a real wait — IAM is eventually consistent and a create
# fired immediately after put-role-policy fails with an image-access error that
# looks like a bad image URI.
log "waiting 20s for IAM policy propagation before creating runtimes"
sleep 20

# The interactive kernel additionally needs the workspace bucket it syncs to.
INT_ENV="$(common_env_json | python3 -c '
import json,sys,os
env = json.load(sys.stdin)
env["WORKSPACE_S3_BUCKET"] = os.environ["WORKSPACE_BUCKET"]
env["WORKSPACE_S3_PREFIX"] = "workspaces"
print(json.dumps(env))')"
INT_RT="$(runtime_ensure claude_code_kernel claude-code-kernel "$INT_ARN" HTTP "$INT_ENV")"
SDK_RT="$(runtime_ensure agent_sdk_kernel agent-sdk-kernel "$SDK_ARN" HTTP "$(common_env_json)")"
MCP_RT="$(runtime_ensure mcp_tools_kernel mcp-tools-kernel "$MCP_ARN" MCP '{"AWS_REGION":"'"$AWS_REGION"'"}')"

save INTERACTIVE_RUNTIME_ARN "$INT_RT"
save SDK_RUNTIME_ARN "$SDK_RT"
save MCP_TOOLS_RUNTIME_ARN "$MCP_RT"

log "waiting for runtimes to become READY…"
for rt in claude_code_kernel agent_sdk_kernel mcp_tools_kernel; do
  n="${rt}${RUNTIME_SUFFIX}"
  for i in $(seq 1 40); do
    st="$(aws bedrock-agentcore-control list-agent-runtimes \
      --query "agentRuntimes[?agentRuntimeName=='$n'].status | [0]" --output text 2>/dev/null)"
    [ "$st" = "READY" ] && { log "  $n READY"; break; }
    case "$st" in
      CREATE_FAILED|UPDATE_FAILED|DELETING) warn "  $n -> $st"; break ;;
    esac
    sleep 15
  done
done

log "runtime phase done"

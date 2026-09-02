#!/bin/bash
# Phase 4b — the backend's IRSA role, scheduler group/DLQ/Lambda and their
# roles, the service-entry secret. Mirrors the rest of terraform modules/portal.
. "$(dirname "$0")/../lib/common.sh"
load
: "${ALB_ARN:?run 40-portal-base.sh}"; : "${TABLE:?run 20-platform.sh}"
: "${OIDC_PROVIDER_ARN:?run 25-eks.sh}"

step "portal app ($NAME)"
PRIV0="${PRIVATE_SUBNETS%%,*}"; PRIV1="${PRIVATE_SUBNETS##*,}"
TABLE_ARN="arn:aws:dynamodb:$AWS_REGION:$ACCOUNT_ID:table/$TABLE"
WS_ARN="arn:aws:s3:::$WORKSPACE_BUCKET"

# ------------------------------------------------------------------ logs
# Fluent Bit routes the backend pods' output here (<prefix>/<namespace>.<app>);
# creating it first pins the retention.
LOG_GROUP="$LOG_PREFIX/portal.backend"
aws logs create-log-group --log-group-name "$LOG_GROUP" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 7 >/dev/null
LAMBDA_LG="/aws/lambda/agent-platform-schedule-runner${SUFFIX}"
aws logs create-log-group --log-group-name "$LAMBDA_LG" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "$LAMBDA_LG" --retention-in-days 7 >/dev/null
# Persist both: later phases reference them, and a derived-but-unsaved value is
# invisible across script boundaries (terraform would just resolve the reference).
save LOG_GROUP "$LOG_GROUP"
save LAMBDA_LG "$LAMBDA_LG"
log "log groups ready"

# ------------------------------------------------------------------ scheduler infra
SCHED_GROUP="agent-platform${SUFFIX}"
aws scheduler create-schedule-group --name "$SCHED_GROUP" >/dev/null 2>&1 || log "schedule group exists"
DLQ_NAME="agent-platform-schedule-dlq${SUFFIX}"
DLQ_URL="$(aws sqs get-queue-url --queue-name "$DLQ_NAME" --query QueueUrl --output text 2>/dev/null || echo None)"
if [ "$DLQ_URL" = "None" ]; then
  DLQ_URL="$(aws sqs create-queue --queue-name "$DLQ_NAME" \
    --attributes MessageRetentionPeriod=1209600 --query QueueUrl --output text)"
  log "created dlq"
fi
DLQ_ARN="$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"
save DLQ_URL "$DLQ_URL"; save DLQ_ARN "$DLQ_ARN"; save SCHED_GROUP "$SCHED_GROUP"

# ------------------------------------------------------------------ IAM
role_ensure() {  # name trust
  local rn="$1" trust="$2" arn
  arn="$(aws iam get-role --role-name "$rn" --query 'Role.Arn' --output text 2>/dev/null || echo None)"
  if [ "$arn" = "None" ]; then
    arn="$(aws iam create-role --role-name "$rn" --assume-role-policy-document "$trust" --query 'Role.Arn' --output text)"
    log "created role $rn" >&2
  else
    log "role exists $rn" >&2
  fi
  echo "$arn"
}
LAMBDA_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
SCHED_TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"scheduler.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT_ID\"}}}]}"

# The backend role keeps its ECS-era "-task" name (it is what the permissions
# doc and runbooks call it) but is assumed through IRSA by the `backend`
# service account in the `portal` namespace. No execution role: image pulls
# are the node's business and logs are Fluent Bit's.
TASK_ROLE="$(role_ensure "agent-platform-backend-task${SUFFIX}" "$(irsa_trust portal backend)")"
aws iam update-assume-role-policy --role-name "agent-platform-backend-task${SUFFIX}" \
  --policy-document "$(irsa_trust portal backend)"
RUNNER_ROLE="$(role_ensure "agent-platform-schedule-runner${SUFFIX}" "$LAMBDA_TRUST")"
SCHED_ROLE="$(role_ensure "agent-platform-scheduler${SUFFIX}" "$SCHED_TRUST")"
save TASK_ROLE "$TASK_ROLE"
save RUNNER_ROLE "$RUNNER_ROLE"; save SCHED_ROLE "$SCHED_ROLE"

ENTRY_SECRET_ARN_GUESS="arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT_ID:secret:${ENTRY_SECRET}*"

put_json_policy() {  # role name file
  aws iam put-role-policy --role-name "$1" --policy-name "$2" --policy-document "file://$3"
  log "policy $2 -> $1"
}

cat > /tmp/task-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"DynamoRw","Effect":"Allow","Action":["dynamodb:BatchGetItem","dynamodb:GetItem","dynamodb:Query","dynamodb:Scan","dynamodb:ConditionCheckItem","dynamodb:BatchWriteItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:DescribeTable"],"Resource":["$TABLE_ARN","$TABLE_ARN/index/*"]},
 {"Sid":"WorkspaceBucketRw","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListBucket","s3:GetBucketLocation"],"Resource":["$WS_ARN","$WS_ARN/*"]},
 {"Sid":"AssumeWorkspaceAccess","Effect":"Allow","Action":"sts:AssumeRole","Resource":"$WORKSPACE_ROLE_ARN"},
 {"Sid":"AgentCoreInvoke","Effect":"Allow","Action":["bedrock-agentcore:InvokeAgentRuntime","bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream","bedrock-agentcore:GetAgentRuntime","bedrock-agentcore:GetAgentRuntimeEndpoint"],"Resource":"arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:runtime/*"},
 {"Sid":"AgentCoreMemory","Effect":"Allow","Action":["bedrock-agentcore:CreateMemory","bedrock-agentcore:GetMemory","bedrock-agentcore:UpdateMemory","bedrock-agentcore:DeleteMemory","bedrock-agentcore:GetEvent","bedrock-agentcore:ListEvents","bedrock-agentcore:ListActors","bedrock-agentcore:ListSessions","bedrock-agentcore:GetMemoryRecord","bedrock-agentcore:ListMemoryRecords","bedrock-agentcore:RetrieveMemoryRecords"],"Resource":"arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:memory/*"},
 {"Sid":"AgentCoreMemoryList","Effect":"Allow","Action":"bedrock-agentcore:ListMemories","Resource":"*"},
 {"Sid":"AgentCoreGatewayRead","Effect":"Allow","Action":["bedrock-agentcore:ListGateways","bedrock-agentcore:GetGateway","bedrock-agentcore:ListGatewayTargets","bedrock-agentcore:GetGatewayTarget"],"Resource":"*"},
 {"Sid":"PipelineTraces","Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords"],"Resource":"*"},
 {"Sid":"SchedulerCrud","Effect":"Allow","Action":["scheduler:CreateSchedule","scheduler:UpdateSchedule","scheduler:DeleteSchedule","scheduler:GetSchedule","scheduler:ListSchedules"],"Resource":"arn:aws:scheduler:$AWS_REGION:$ACCOUNT_ID:schedule/$SCHED_GROUP/*"},
 {"Sid":"PassSchedulerRole","Effect":"Allow","Action":"iam:PassRole","Resource":"$SCHED_ROLE","Condition":{"StringEquals":{"iam:PassedToService":"scheduler.amazonaws.com"}}},
 {"Sid":"ServiceEntrySecret","Effect":"Allow","Action":["secretsmanager:GetSecretValue","secretsmanager:DescribeSecret"],"Resource":"$ENTRY_SECRET_ARN_GUESS"}]}
JSON
put_json_policy "agent-platform-backend-task${SUFFIX}" backend /tmp/task-pol.json

cat > /tmp/runner-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:$LAMBDA_LG:*"},
 {"Sid":"DynamoRw","Effect":"Allow","Action":["dynamodb:BatchGetItem","dynamodb:GetItem","dynamodb:Query","dynamodb:Scan","dynamodb:ConditionCheckItem","dynamodb:BatchWriteItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:DescribeTable"],"Resource":["$TABLE_ARN","$TABLE_ARN/index/*"]},
 {"Sid":"WorkspaceBucketRw","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListBucket","s3:GetBucketLocation"],"Resource":["$WS_ARN","$WS_ARN/*"]},
 {"Sid":"AgentCoreInvoke","Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":"arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:runtime/*"},
 {"Sid":"PipelineTraces","Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords"],"Resource":"*"},
 {"Sid":"PortalAdminSecret","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT_ID:secret:agent-platform${SUFFIX}/portal-admin*"}]}
JSON
put_json_policy "agent-platform-schedule-runner${SUFFIX}" runner /tmp/runner-pol.json

log "portal app IAM done"

# ------------------------------------------------------------------ scheduler role policy
cat > /tmp/sched-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"InvokeRunner","Effect":"Allow","Action":"lambda:InvokeFunction","Resource":["arn:aws:lambda:$AWS_REGION:$ACCOUNT_ID:function:agent-platform-schedule-runner${SUFFIX}","arn:aws:lambda:$AWS_REGION:$ACCOUNT_ID:function:agent-platform-schedule-runner${SUFFIX}:*"]},
 {"Sid":"DlqSend","Effect":"Allow","Action":"sqs:SendMessage","Resource":"$DLQ_ARN"}]}
JSON
put_json_policy "agent-platform-scheduler${SUFFIX}" scheduler /tmp/sched-pol.json

# ------------------------------------------------------------------ entry secret
if ! aws secretsmanager describe-secret --secret-id "$ENTRY_SECRET" >/dev/null 2>&1; then
  ENTRY_VAL="$(python3 -c 'import secrets;print(secrets.token_urlsafe(40).replace("-","").replace("_","")[:48])')"
  aws secretsmanager create-secret --name "$ENTRY_SECRET" \
    --description "Shared header secret: service-entry API Gateway -> backend" \
    --secret-string "$ENTRY_VAL" >/dev/null
  log "created service-entry secret"
else
  ENTRY_VAL="$(aws secretsmanager get-secret-value --secret-id "$ENTRY_SECRET" --query SecretString --output text)"
  log "service-entry secret exists"
fi
save ENTRY_SECRET "$ENTRY_SECRET"; save ENTRY_VAL "$ENTRY_VAL"

# ------------------------------------------------------------------ schedule-runner lambda
FN="agent-platform-schedule-runner${SUFFIX}"
if ! aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  # Placeholder code, same split as terraform: infra here, real package via
  # scripts/deploy-schedule-lambda.sh.
  mkdir -p /tmp/lam && printf 'def handler(event, context):\n    raise RuntimeError("schedule-runner code not deployed")\n' > /tmp/lam/index.py
  (cd /tmp/lam && rm -f f.zip && zip -q f.zip index.py)
  aws lambda create-function --function-name "$FN" --runtime python3.13 --architectures arm64 \
    --role "$RUNNER_ROLE" --handler index.handler --timeout 600 --memory-size 512 \
    --zip-file fileb:///tmp/lam/f.zip \
    --logging-config "LogFormat=Text,LogGroup=$LAMBDA_LG" >/dev/null 2>&1 \
    || { log "lambda create failed once (role propagation) — retrying in 15s"
         sleep 15
         aws lambda create-function --function-name "$FN" --runtime python3.13 --architectures arm64 \
           --role "$RUNNER_ROLE" --handler index.handler --timeout 600 --memory-size 512 \
           --zip-file fileb:///tmp/lam/f.zip \
           --logging-config "LogFormat=Text,LogGroup=$LAMBDA_LG" >/dev/null; }
  log "created schedule-runner lambda (placeholder code)"
else
  log "lambda exists $FN"
fi
LAMBDA_ARN="$(aws lambda get-function --function-name "$FN" --query 'Configuration.FunctionArn' --output text)"
save LAMBDA_ARN "$LAMBDA_ARN"; save SCHEDULE_FN "$FN"

log "portal app phase 1 done"

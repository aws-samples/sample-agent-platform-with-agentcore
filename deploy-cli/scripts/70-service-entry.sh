#!/bin/bash
# Phase 6 — the private service-entry API: PRIVATE REST API -> VPC Link ->
# internal NLB, SigV4 (AWS_IAM) auth, gateway-injected caller ARN + shared
# secret. Mirrors terraform modules/portal/service_entry.tf.
. "$(dirname "$0")/../lib/common.sh"
load
: "${NLB_ARN:?run 40-portal-base.sh}"; : "${ENTRY_VAL:?run 50-portal-app.sh}"

step "service entry ($NAME)"

# ---- VPC Link (takes several minutes; a create against a pending link fails) ----
VPCLINK_ID="$(aws apigateway get-vpc-links \
  --query "items[?name=='agent-platform-service-entry${SUFFIX}'].id | [0]" --output text 2>/dev/null || echo None)"
if [ "$VPCLINK_ID" = "None" ] || [ -z "$VPCLINK_ID" ]; then
  VPCLINK_ID="$(aws apigateway create-vpc-link --name "agent-platform-service-entry${SUFFIX}" \
    --description "agent-platform service entry -> internal NLB" \
    --target-arns "$NLB_ARN" --query id --output text)"
  log "creating vpc link $VPCLINK_ID (several minutes)"
else
  log "vpc link exists $VPCLINK_ID"
fi
save VPCLINK_ID "$VPCLINK_ID"

for i in $(seq 1 60); do
  ST="$(aws apigateway get-vpc-link --vpc-link-id "$VPCLINK_ID" --query status --output text 2>/dev/null)"
  [ "$ST" = "AVAILABLE" ] && { log "vpc link AVAILABLE"; break; }
  [ "$ST" = "FAILED" ] && die "vpc link FAILED"
  sleep 15
done

# ---- REST API (PRIVATE) ----
API_ID="$(aws apigateway get-rest-apis \
  --query "items[?name=='agent-platform-service-entry${SUFFIX}'].id | [0]" --output text 2>/dev/null || echo None)"
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID="$(aws apigateway create-rest-api --name "agent-platform-service-entry${SUFFIX}" \
    --description "SigV4-authenticated server-to-server entry to agent channels (private)" \
    --endpoint-configuration 'types=PRIVATE' --query id --output text)"
  log "created private rest api $API_ID"
else
  log "rest api exists $API_ID"
fi
save SERVICE_API_ID "$API_ID"

# Resource policy: this account only. Attached AFTER create (upstream ae481cd
# moved terraform to the same shape, because an inline policy embeds the API id
# which does not exist yet).
cat > /tmp/api-pol.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::$ACCOUNT_ID:root"},"Action":"execute-api:Invoke","Resource":"arn:aws:execute-api:$AWS_REGION:$ACCOUNT_ID:$API_ID/*"}]}
JSON
aws apigateway update-rest-api --rest-api-id "$API_ID" \
  --patch-operations "op=replace,path=/policy,value=$(python3 -c 'import json,sys;print(json.dumps(open("/tmp/api-pol.json").read()))')" \
  >/dev/null && log "resource policy attached"

ROOT_ID="$(aws apigateway get-resources --rest-api-id "$API_ID" --query "items[?path=='/'].id | [0]" --output text)"

res_ensure() {  # parent-id path-part
  local parent="$1" part="$2" id
  id="$(aws apigateway get-resources --rest-api-id "$API_ID" --limit 200 \
        --query "items[?pathPart=='$part' && parentId=='$parent'].id | [0]" --output text 2>/dev/null || echo None)"
  if [ "$id" = "None" ] || [ -z "$id" ]; then
    id="$(aws apigateway create-resource --rest-api-id "$API_ID" --parent-id "$parent" \
          --path-part "$part" --query id --output text)"
  fi
  echo "$id"
}
R_SERVICE="$(res_ensure "$ROOT_ID" service)"
R_V1="$(res_ensure "$R_SERVICE" v1)"
R_CHANNELS="$(res_ensure "$R_V1" channels)"
R_CHID="$(res_ensure "$R_CHANNELS" '{channelId}')"
R_CHINV="$(res_ensure "$R_CHID" invocations)"
R_INVS="$(res_ensure "$R_V1" invocations)"
R_INVID="$(res_ensure "$R_INVS" '{invocationId}')"
log "resources created"

method_and_integration() {  # resource-id http-method path-param uri-suffix
  local rid="$1" verb="$2" param="$3" suffix="$4"
  aws apigateway put-method --rest-api-id "$API_ID" --resource-id "$rid" \
    --http-method "$verb" --authorization-type AWS_IAM \
    --request-parameters "method.request.path.$param=true" >/dev/null 2>&1 \
    || log "method $verb exists"
  # --request-parameters as key=value,key=value shorthand breaks here: the
  # generated secret can contain characters the CLI parses as part of the
  # mapping expression, producing "Invalid mapping expression specified" with the
  # secret echoed back. Pass JSON instead. (Single quotes around the value mark
  # it as a STATIC value to API Gateway, as opposed to a context lookup.)
  python3 - "$param" "$ENTRY_VAL" > /tmp/reqparams.json <<'PYP'
import json, sys
param, secret = sys.argv[1], sys.argv[2]
json.dump({
    "integration.request.header.x-caller-arn": "context.identity.userArn",
    "integration.request.header.x-service-entry-secret": f"'{secret}'",
    f"integration.request.path.{param}": f"method.request.path.{param}",
}, sys.stdout)
PYP
  aws apigateway put-integration --rest-api-id "$API_ID" --resource-id "$rid" \
    --http-method "$verb" --type HTTP_PROXY --integration-http-method "$verb" \
    --uri "http://${NLB_DNS}${suffix}" \
    --connection-type VPC_LINK --connection-id "$VPCLINK_ID" \
    --request-parameters file:///tmp/reqparams.json \
    >/dev/null && log "integration $verb $suffix"
}
method_and_integration "$R_CHINV" POST channelId '/service/v1/channels/{channelId}/invocations'
method_and_integration "$R_INVID" GET invocationId '/service/v1/invocations/{invocationId}'

# ---- deployment + stage with access logging ----
ACCESS_LG="/aws/apigateway/agent-platform-service-entry${SUFFIX}"
aws logs create-log-group --log-group-name "$ACCESS_LG" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "$ACCESS_LG" --retention-in-days 30 >/dev/null

DEP_ID="$(aws apigateway create-deployment --rest-api-id "$API_ID" --query id --output text)"
log "created deployment $DEP_ID"

if aws apigateway get-stage --rest-api-id "$API_ID" --stage-name svc >/dev/null 2>&1; then
  aws apigateway update-stage --rest-api-id "$API_ID" --stage-name svc \
    --patch-operations "op=replace,path=/deploymentId,value=$DEP_ID" >/dev/null
  log "stage svc updated"
else
  # PREREQUISITE: API Gateway only writes access logs once the ACCOUNT has
  # cloudwatchRoleArn set. Terraform cannot set it per-stack either; if the
  # create fails on that, fall back to a stage without logging rather than
  # leaving no stage at all.
  # Access logging has two CLI-only traps:
  #  1. --patch-operations shorthand cannot carry a JSON format string (the
  #     quotes are parsed as part of the key=value expression), so this goes
  #     through --cli-input-json.
  #  2. describe-log-groups returns the group ARN with a trailing ":*", which
  #     API Gateway rejects as "Invalid ARN specified" — it wants it stripped.
  # It also needs an ACCOUNT-level cloudwatchRoleArn; if that is unset, create
  # the stage without logging rather than leaving no stage at all.
  aws apigateway create-stage --rest-api-id "$API_ID" --stage-name svc --deployment-id "$DEP_ID" >/dev/null
  log "stage svc created"
fi

LG_ARN="$(aws logs describe-log-groups --log-group-name-prefix "$ACCESS_LG" \
  --query 'logGroups[0].arn' --output text 2>/dev/null || echo None)"
if [ "$LG_ARN" != "None" ] && [ -n "$LG_ARN" ]; then
  python3 - "$API_ID" "$LG_ARN" > /tmp/stage-log.json <<'PYS'
import json, sys
api, lg = sys.argv[1:3]
lg = lg[:-2] if lg.endswith(":*") else lg
fmt = json.dumps({
    "requestId": "$context.requestId", "ip": "$context.identity.sourceIp",
    "callerArn": "$context.identity.userArn", "vpce": "$context.identity.vpceId",
    "httpMethod": "$context.httpMethod", "resourcePath": "$context.resourcePath",
    "status": "$context.status", "requestTime": "$context.requestTime",
})
json.dump({"restApiId": api, "stageName": "svc", "patchOperations": [
    {"op": "replace", "path": "/accessLogSettings/destinationArn", "value": lg},
    {"op": "replace", "path": "/accessLogSettings/format", "value": fmt}]}, sys.stdout)
PYS
  if aws apigateway update-stage --cli-input-json file:///tmp/stage-log.json >/dev/null 2>&1; then
    log "stage access logging enabled"
  else
    warn "could not enable stage access logging; the account may need:"
    warn "  aws apigateway update-account --patch-operations op=replace,path=/cloudwatchRoleArn,value=<role-arn>"
  fi
fi

aws apigateway update-stage --rest-api-id "$API_ID" --stage-name svc \
  --patch-operations 'op=replace,path=/*/*/throttling/rateLimit,value=50' \
                     'op=replace,path=/*/*/throttling/burstLimit,value=100' >/dev/null 2>&1 \
  || log "throttle settings unchanged"

save SERVICE_API_URL "https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/svc/"
log "service entry done: $SERVICE_API_URL"

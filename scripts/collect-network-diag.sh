#!/bin/bash
# Read-only network/permissions evidence collector for AgentCore runtimes in
# VPC mode. Built for locked-down environments where the person running it is
# not the person diagnosing it: every command is read-only, denied calls are
# recorded and skipped (the denials themselves are useful evidence), and the
# output is a single directory to tar up and send back.
#
# The one "write" is a pair of warmup probe invocations, which only create a
# throwaway runtime session.
#
# Usage:
#   scripts/collect-network-diag.sh <agent-runtime-id> [region]
#   # e.g. scripts/collect-network-diag.sh claude_code_kernel-AbCdEf1234 ap-northeast-1
#
# What it collects (see docs/permissions.md §10 for how to read it):
#   runtime network config / subnet AZ IDs / route tables / VPC endpoints /
#   NAT / SG rules / runtime ENIs / EKS backend pods+SGs+IRSA roles / runtime and
#   backend log groups / two warmup probes (cold + retry) / caller-side
#   endpoint reachability baseline.
set -u

RUNTIME_ID="${1:?usage: collect-network-diag.sh <agent-runtime-id> [region]}"
REGION="${2:-$(aws configure get region 2>/dev/null || echo us-east-1)}"
OUT="agentcore-diag-$(date +%Y%m%d-%H%M)"; mkdir -p "$OUT"
exec > >(tee "$OUT/console.log") 2>&1
run(){ echo; echo "########## $1 ##########"; shift; "$@" || echo "!! FAILED/DENIED (exit $?)"; }

run "caller-identity" aws sts get-caller-identity
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
RUNTIME_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${RUNTIME_ID}"

# ---- 1. Runtime definition: network mode / subnets / SG / env / status ----
run "runtime-detail" aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" --region "$REGION" --output json
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
  --region "$REGION" --output json > "$OUT/runtime.json" 2>/dev/null

RT_SUBNETS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
  --query 'networkConfiguration.networkModeConfig.subnets' --output text 2>/dev/null)
RT_SGS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
  --query 'networkConfiguration.networkModeConfig.securityGroups' --output text 2>/dev/null)

# ---- 2. Topology: AZ IDs (check against the supported-AZ table!) /
#         route tables / SG egress / VPC endpoints / NAT / flow logs ----
if [ -n "$RT_SUBNETS" ]; then
  run "runtime-subnets-az" aws ec2 describe-subnets --region "$REGION" --subnet-ids $RT_SUBNETS \
    --query 'Subnets[].{id:SubnetId,azId:AvailabilityZoneId,cidr:CidrBlock,vpc:VpcId}' --output table
  VPC_ID=$(aws ec2 describe-subnets --region "$REGION" --subnet-ids $RT_SUBNETS --query 'Subnets[0].VpcId' --output text)
  run "all-route-tables" aws ec2 describe-route-tables --region "$REGION" --filters Name=vpc-id,Values="$VPC_ID" --output json
  run "vpc-endpoints" aws ec2 describe-vpc-endpoints --region "$REGION" --filters Name=vpc-id,Values="$VPC_ID" \
    --query 'VpcEndpoints[].{svc:ServiceName,type:VpcEndpointType,state:State,sgs:Groups[].GroupId,subnets:SubnetIds}' --output json
  run "nat-gateways" aws ec2 describe-nat-gateways --region "$REGION" --filter Name=vpc-id,Values="$VPC_ID" --output json
  run "flow-logs-enabled" aws ec2 describe-flow-logs --region "$REGION" --filter Name=resource-id,Values="$VPC_ID" --output json
fi
[ -n "$RT_SGS" ] && run "runtime-sg-rules" aws ec2 describe-security-groups --region "$REGION" --group-ids $RT_SGS --output json
[ -n "$RT_SGS" ] && run "runtime-enis" aws ec2 describe-network-interfaces --region "$REGION" \
  --filters Name=group-id,Values=$RT_SGS \
  --query 'NetworkInterfaces[].{id:NetworkInterfaceId,subnet:SubnetId,ip:PrivateIpAddress,status:Status,desc:Description}' --output table

# ---- 3. Backend on EKS: cluster / node group / pods, their security groups and IRSA roles ----
EKS_CLUSTER=$(aws eks list-clusters --region "$REGION" --query 'clusters[?contains(@,`agent-platform`)]|[0]' --output text 2>/dev/null)
if [ -n "$EKS_CLUSTER" ] && [ "$EKS_CLUSTER" != "None" ]; then
  run "eks-cluster" aws eks describe-cluster --region "$REGION" --name "$EKS_CLUSTER" \
    --query 'cluster.{status:status,version:version,endpointAccess:resourcesVpcConfig.{public:endpointPublicAccess,private:endpointPrivateAccess,cidrs:publicAccessCidrs},subnets:resourcesVpcConfig.subnetIds,clusterSg:resourcesVpcConfig.clusterSecurityGroupId,oidc:identity.oidc.issuer}' --output json
  for NG in $(aws eks list-nodegroups --region "$REGION" --cluster-name "$EKS_CLUSTER" --query 'nodegroups[]' --output text 2>/dev/null); do
    run "eks-nodegroup $NG" aws eks describe-nodegroup --region "$REGION" --cluster-name "$EKS_CLUSTER" --nodegroup-name "$NG" \
      --query 'nodegroup.{status:status,types:instanceTypes,ami:amiType,scaling:scalingConfig,health:health}' --output json
  done
  run "eks-addons" aws eks describe-addon --region "$REGION" --cluster-name "$EKS_CLUSTER" --addon-name vpc-cni \
    --query 'addon.{status:status,version:addonVersion,irsaRole:serviceAccountRoleArn,config:configurationValues}' --output json
  # kubectl is optional: without it the AWS-side view above still tells most of the story.
  if command -v kubectl >/dev/null 2>&1; then
    KCFG=$(mktemp)
    if aws eks update-kubeconfig --region "$REGION" --name "$EKS_CLUSTER" --kubeconfig "$KCFG" >/dev/null 2>&1; then
      run "k8s-nodes" kubectl --kubeconfig "$KCFG" get nodes -o wide
      run "k8s-pods" kubectl --kubeconfig "$KCFG" get pods -A -o wide
      run "k8s-deployments" kubectl --kubeconfig "$KCFG" get deploy -A -o wide
      # Which security groups each pod carries (branch ENI), and which role it assumes (IRSA).
      run "k8s-securitygrouppolicies" kubectl --kubeconfig "$KCFG" get securitygrouppolicies -A -o yaml
      run "k8s-pod-enis" kubectl --kubeconfig "$KCFG" get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.metadata.annotations.vpc\.amazonaws\.com/pod-eni}{"\n"}{end}'
      run "k8s-serviceaccounts-irsa" kubectl --kubeconfig "$KCFG" get sa -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.metadata.annotations.eks\.amazonaws\.com/role-arn}{"\n"}{end}'
      run "k8s-targetgroupbindings" kubectl --kubeconfig "$KCFG" get targetgroupbindings -A -o wide
      run "k8s-recent-events" kubectl --kubeconfig "$KCFG" get events -A --sort-by=.lastTimestamp
    fi
    rm -f "$KCFG"
  fi
fi

# ---- 4. Logs: has the container ever started + any backend warmup failures ----
SINCE=$(( ($(date +%s) - 259200) * 1000 ))   # last 3 days
run "agentcore-log-groups" aws logs describe-log-groups --region "$REGION" \
  --log-group-name-prefix /aws/bedrock-agentcore --query 'logGroups[].logGroupName' --output table
RLG="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"
run "runtime-log-streams" aws logs describe-log-streams --region "$REGION" --log-group-name "$RLG" \
  --order-by LastEventTime --descending --max-items 10 --output json
run "runtime-recent-logs" aws logs filter-log-events --region "$REGION" --log-group-name "$RLG" \
  --start-time "$SINCE" --max-items 300 --query 'events[].message' --output text
BLG=$(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix /eks/agent-platform \
  --query 'logGroups[?contains(logGroupName,`portal.backend`)].logGroupName|[0]' --output text 2>/dev/null)
if [ -n "$BLG" ] && [ "$BLG" != "None" ]; then
  run "backend-warmup-failures" aws logs filter-log-events --region "$REGION" --log-group-name "$BLG" \
    --filter-pattern '"warmup failed"' --start-time "$SINCE" --query 'events[].message' --output text
  run "backend-recent-errors" aws logs filter-log-events --region "$REGION" --log-group-name "$BLG" \
    --filter-pattern '?ERROR ?Timeout ?timed' --start-time "$SINCE" --max-items 100 --query 'events[].message' --output text
fi

# ---- 5. Warmup probes: cold start, then the same session again 3 min later.
#         A cold failure + warm success just means slow start; two failures
#         with "initialization time exceeded" points at the image-pull path. ----
SID="diag-$(openssl rand -hex 16)"   # 37 chars, satisfies the >=33-char session-id minimum
echo; echo "########## warmup-invoke-1 (session $SID) ##########"
time aws bedrock-agentcore invoke-agent-runtime --region "$REGION" \
  --agent-runtime-arn "$RUNTIME_ARN" --qualifier DEFAULT \
  --runtime-session-id "$SID" \
  --payload '{"action":"warmup"}' --cli-binary-format raw-in-base64-out \
  --cli-read-timeout 300 --cli-connect-timeout 20 \
  "$OUT/warmup-1.json"; echo "exit=$?"
echo "(sleeping 180s, then retrying the same session…)"
sleep 180
echo; echo "########## warmup-invoke-2 (same session) ##########"
time aws bedrock-agentcore invoke-agent-runtime --region "$REGION" \
  --agent-runtime-arn "$RUNTIME_ARN" --qualifier DEFAULT \
  --runtime-session-id "$SID" \
  --payload '{"action":"warmup"}' --cli-binary-format raw-in-base64-out \
  --cli-read-timeout 300 --cli-connect-timeout 20 \
  "$OUT/warmup-2.json"; echo "exit=$?"
cat "$OUT"/warmup-*.json 2>/dev/null

# Pull runtime logs once more: [start.sh]/[startup] lines are the watershed —
# none at all means the container never launched (image pull / logs egress).
sleep 30
run "runtime-logs-after-invoke" aws logs filter-log-events --region "$REGION" --log-group-name "$RLG" \
  --start-time "$(( ($(date +%s) - 900) * 1000 ))" --max-items 300 --query 'events[].message' --output text

# ---- 6. Caller-side reachability baseline. NOTE: this machine's egress path
#         is usually NOT the VPC's — treat as a comparison point, not proof. ----
for h in bedrock-agentcore.$REGION.amazonaws.com bedrock-agentcore-control.$REGION.amazonaws.com \
         sts.$REGION.amazonaws.com secretsmanager.$REGION.amazonaws.com \
         bedrock-runtime.$REGION.amazonaws.com logs.$REGION.amazonaws.com; do
  echo; echo "########## curl $h ##########"
  curl -sv --max-time 10 "https://$h/" -o /dev/null 2>&1 | grep -E 'Connected|HTTP/|timed out|Could not|SSL' || echo "!! no output"
done

echo; echo "Done. Package with: tar czf $OUT.tgz $OUT"

#!/bin/bash
# Phase 2b — the EKS cluster every platform container runs on: cluster + node
# role, the cluster, its OIDC identity provider (IRSA), the three add-ons with
# the VPC CNI in security-groups-for-Pods mode, a Graviton managed node group,
# and two Helm-installed controllers (AWS Load Balancer Controller for
# TargetGroupBinding, Fluent Bit for CloudWatch logs). Mirrors terraform
# modules/eks.
#
# Runs after 10-network (needs the VPC and private subnets); nothing in it needs
# an image, so it can run while images are still building. Budget ~20 minutes,
# most of it the two `wait` calls.
#
# Besides the AWS CLI this phase needs kubectl and helm on PATH (§1.1).
. "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
load
: "${VPC_ID:?run 10-network.sh}"; : "${PRIVATE_SUBNETS:?run 10-network.sh}"
need_tools kubectl helm openssl

step "eks ($EKS_CLUSTER, $EKS_VERSION, ${EKS_NODE_COUNT}x $EKS_NODE_TYPE)"
PRIV0="${PRIVATE_SUBNETS%%,*}"; PRIV1="${PRIVATE_SUBNETS##*,}"

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
attach() {  # role policy-arn
  aws iam attach-role-policy --role-name "$1" --policy-arn "$2" && log "attached ${2##*/} -> $1"
}

# ------------------------------------------------------------------ roles
EKS_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"eks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
EC2_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
CLUSTER_ROLE="$(role_ensure "agent-platform-eks-cluster${SUFFIX}" "$EKS_TRUST")"
attach "agent-platform-eks-cluster${SUFFIX}" arn:aws:iam::aws:policy/AmazonEKSClusterPolicy
# security groups for Pods: the control plane attaches trunk/branch ENIs
attach "agent-platform-eks-cluster${SUFFIX}" arn:aws:iam::aws:policy/AmazonEKSVPCResourceController
# Node role: join + pull images. No CNI policy — aws-node gets its own IRSA role
# below, so a pod cannot manage ENIs by reaching the instance metadata service.
NODE_ROLE="$(role_ensure "agent-platform-eks-node${SUFFIX}" "$EC2_TRUST")"
attach "agent-platform-eks-node${SUFFIX}" arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy
attach "agent-platform-eks-node${SUFFIX}" arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly
save CLUSTER_ROLE "$CLUSTER_ROLE"; save NODE_ROLE "$NODE_ROLE"

# ------------------------------------------------------------------ cluster
# Ahead of the cluster so retention is 7 days instead of "never".
aws logs create-log-group --log-group-name "/aws/eks/$EKS_CLUSTER/cluster" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "/aws/eks/$EKS_CLUSTER/cluster" --retention-in-days 7 >/dev/null

if ! aws eks describe-cluster --name "$EKS_CLUSTER" >/dev/null 2>&1; then
  # IAM role propagation: the first create can be rejected for a few seconds.
  retry_until "the cluster role to be assumable" 6 10 \
    aws eks create-cluster --name "$EKS_CLUSTER" --kubernetes-version "$EKS_VERSION" \
      --role-arn "$CLUSTER_ROLE" \
      --resources-vpc-config "subnetIds=$PRIV0,$PRIV1,endpointPublicAccess=true,endpointPrivateAccess=true,publicAccessCidrs=$EKS_PUBLIC_CIDRS" \
      --access-config authenticationMode=API,bootstrapClusterCreatorAdminPermissions=true \
      --no-bootstrap-self-managed-addons \
      --upgrade-policy supportType=STANDARD \
      --logging '{"clusterLogging":[{"types":["api","audit","authenticator"],"enabled":true}]}' \
    || die "create-cluster failed"
  log "creating cluster $EKS_CLUSTER (about 10 minutes)…"
else
  log "cluster exists $EKS_CLUSTER"
fi
aws eks wait cluster-active --name "$EKS_CLUSTER"
log "cluster ACTIVE"
save EKS_CLUSTER "$EKS_CLUSTER"

CLUSTER_SG="$(aws eks describe-cluster --name "$EKS_CLUSTER" \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)"
OIDC_ISSUER="$(aws eks describe-cluster --name "$EKS_CLUSTER" --query 'cluster.identity.oidc.issuer' --output text)"
OIDC_HOST="${OIDC_ISSUER#https://}"
save CLUSTER_SG "$CLUSTER_SG"; save OIDC_HOST "$OIDC_HOST"

# ------------------------------------------------------------------ IRSA provider
OIDC_PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_PROVIDER_ARN" >/dev/null 2>&1; then
  # Thumbprint of the issuer's TLS certificate (terraform: data.tls_certificate).
  THUMB="$(echo | openssl s_client -servername "${OIDC_HOST%%/*}" -connect "${OIDC_HOST%%/*}:443" -showcerts 2>/dev/null \
    | awk '/BEGIN CERT/{c++} c>0{print} /END CERT/{if(c>0){exit}}' \
    | openssl x509 -fingerprint -sha1 -noout | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f')"
  [ -n "$THUMB" ] || die "could not compute the OIDC issuer thumbprint"
  aws iam create-open-id-connect-provider --url "$OIDC_ISSUER" \
    --client-id-list sts.amazonaws.com --thumbprint-list "$THUMB" >/dev/null
  log "created OIDC provider for IRSA"
else
  log "OIDC provider exists"
fi
save OIDC_PROVIDER_ARN "$OIDC_PROVIDER_ARN"

# ------------------------------------------------------------------ IRSA roles for the controllers
CNI_ROLE="$(role_ensure "agent-platform-eks-cni${SUFFIX}" "$(irsa_trust kube-system aws-node)")"
attach "agent-platform-eks-cni${SUFFIX}" arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy

LBC_ROLE="$(role_ensure "agent-platform-eks-lb-controller${SUFFIX}" "$(irsa_trust kube-system aws-load-balancer-controller)")"
# The upstream policy for the pinned controller release, vendored with the
# terraform module so nothing is fetched from the internet at deploy time.
aws iam put-role-policy --role-name "agent-platform-eks-lb-controller${SUFFIX}" \
  --policy-name aws-load-balancer-controller \
  --policy-document "file://$CHARTS_DIR/../modules/eks/policies/aws-load-balancer-controller.json"
log "policy aws-load-balancer-controller -> lb-controller role"

FB_ROLE="$(role_ensure "agent-platform-eks-fluent-bit${SUFFIX}" "$(irsa_trust kube-system aws-for-fluent-bit)")"
cat > /tmp/fluent-bit-pol.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"WriteClusterLogs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:PutRetentionPolicy"],"Resource":"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:${LOG_PREFIX}*"},
 {"Sid":"Describe","Effect":"Allow","Action":["logs:DescribeLogGroups","logs:DescribeLogStreams"],"Resource":"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:*"}]}
JSON
aws iam put-role-policy --role-name "agent-platform-eks-fluent-bit${SUFFIX}" \
  --policy-name fluent-bit --policy-document file:///tmp/fluent-bit-pol.json
log "policy fluent-bit -> fluent-bit role"
save CNI_ROLE "$CNI_ROLE"; save LBC_ROLE "$LBC_ROLE"; save FB_ROLE "$FB_ROLE"

# ------------------------------------------------------------------ add-ons
addon_version() {  # name
  aws eks describe-addon-versions --kubernetes-version "$EKS_VERSION" --addon-name "$1" \
    --query 'addons[0].addonVersions[0].addonVersion' --output text
}
addon_ensure() {  # name [extra create args...]
  local n="$1"; shift
  if aws eks describe-addon --cluster-name "$EKS_CLUSTER" --addon-name "$n" >/dev/null 2>&1; then
    log "addon exists $n"
  else
    aws eks create-addon --cluster-name "$EKS_CLUSTER" --addon-name "$n" \
      --addon-version "$(addon_version "$n")" --resolve-conflicts OVERWRITE "$@" >/dev/null
    log "created addon $n"
  fi
  aws eks wait addon-active --cluster-name "$EKS_CLUSTER" --addon-name "$n"
}

# VPC CNI with its own IRSA role and security groups for Pods:
#   ENABLE_POD_ENI — trunk ENI per node so pods get branch ENIs with their own
#     security groups.
#   strict enforcing mode — the pod's groups are the ONLY ones evaluated, in
#     and out. `standard` SNATs traffic leaving the VPC to the node and judges
#     it by the node's group, which would void llm-edge's 443-only egress.
#     Strict needs DISABLE_TCP_EARLY_DEMUX so kubelet probes reach the pods.
#   small warm pools — do not reserve dozens of subnet addresses per node.
addon_ensure vpc-cni --service-account-role-arn "$CNI_ROLE" \
  --configuration-values '{"env":{"ENABLE_POD_ENI":"true","POD_SECURITY_GROUP_ENFORCING_MODE":"strict","WARM_IP_TARGET":"2","MINIMUM_IP_TARGET":"4"},"init":{"env":{"DISABLE_TCP_EARLY_DEMUX":"true"}}}'

# ------------------------------------------------------------------ node group
NG="agent-platform-workers${SUFFIX}"
if ! aws eks describe-nodegroup --cluster-name "$EKS_CLUSTER" --nodegroup-name "$NG" >/dev/null 2>&1; then
  aws eks create-nodegroup --cluster-name "$EKS_CLUSTER" --nodegroup-name "$NG" \
    --node-role "$NODE_ROLE" --subnets "$PRIV0" "$PRIV1" \
    --ami-type AL2023_ARM_64_STANDARD --instance-types "$EKS_NODE_TYPE" --capacity-type ON_DEMAND \
    --scaling-config "minSize=$EKS_NODE_COUNT,maxSize=$EKS_NODE_MAX,desiredSize=$EKS_NODE_COUNT" \
    --update-config maxUnavailable=1 \
    --labels agent-platform/role=workers >/dev/null
  log "creating node group $NG (a few minutes)…"
else
  log "node group exists $NG"
fi
aws eks wait nodegroup-active --cluster-name "$EKS_CLUSTER" --nodegroup-name "$NG"
log "node group ACTIVE"
save EKS_NODEGROUP "$NG"

# After the nodes: these add-ons only report ACTIVE once their pods schedule.
addon_ensure kube-proxy
addon_ensure coredns

# ------------------------------------------------------------------ controllers
rm -f "$KUBECONFIG_FILE"; kube_ready
kube get nodes -o wide | sed 's/^/    /'

helm_k repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm_k repo update eks >/dev/null

# AWS Load Balancer Controller — only its TargetGroupBinding half is used: the
# load balancers stay CLI-created resources, the controller keeps each target
# group's membership in step with the pods behind a Service.
helm_k upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system --version "$LBC_CHART_VERSION" \
  --set clusterName="$EKS_CLUSTER" --set region="$AWS_REGION" --set vpcId="$VPC_ID" \
  --set replicaCount=2 \
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$LBC_ROLE" \
  --set enableServiceMutatorWebhook=false --set defaultTargetType=ip \
  --wait --timeout 10m >/dev/null
log "aws-load-balancer-controller installed"

# Fluent Bit → CloudWatch: one log group per workload from the pod's `app`
# label (<prefix>/<namespace>.<app>), the cluster group for everything else.
helm_k upgrade --install aws-for-fluent-bit eks/aws-for-fluent-bit \
  --namespace kube-system --version "$FLUENT_BIT_CHART_VERSION" \
  --set serviceAccount.create=true --set serviceAccount.name=aws-for-fluent-bit \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$FB_ROLE" \
  --set cloudWatchLogs.enabled=true --set cloudWatchLogs.region="$AWS_REGION" \
  --set cloudWatchLogs.logGroupName="$LOG_PREFIX/cluster" \
  --set "cloudWatchLogs.logGroupTemplate=$LOG_PREFIX/\$kubernetes['namespace_name'].\$kubernetes['labels']['app']" \
  --set cloudWatchLogs.logStreamPrefix="pod." \
  --set "cloudWatchLogs.logStreamTemplate=\$kubernetes['pod_name'].\$kubernetes['container_name']" \
  --set cloudWatchLogs.logKey=log --set cloudWatchLogs.autoCreateGroup=true \
  --set cloudWatchLogs.logRetentionDays=7 \
  --set 'tolerations[0].operator=Exists' \
  --wait --timeout 10m >/dev/null
log "aws-for-fluent-bit installed"

log "eks done — kubeconfig: $KUBECONFIG_FILE"

#!/bin/bash
# Phase 1 — network: VPC, 2 public /24 + 2 private /20, IGW, fixed-EIP NAT,
# egress-only runtime SG. Mirrors terraform modules/network.
. "$(dirname "$0")/../lib/common.sh"
load

step "network ($NAME, $VPC_CIDR)"

# ---- VPC ----
VPC_ID="$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$NAME" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo None)"
if [ "$VPC_ID" = "None" ]; then
  VPC_ID="$(aws ec2 create-vpc --cidr-block "$VPC_CIDR" \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'Vpc.VpcId' --output text)"
  # Terraform sets both on the resource; over the API they are two more calls,
  # and the runtimes need DNS names to resolve the gateway/S3 endpoints.
  aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support
  aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames
  log "created vpc $VPC_ID"
else
  log "vpc exists $VPC_ID"
fi
save VPC_ID "$VPC_ID"

# ---- AZs: first two, same as terraform's slice(names, 0, 2) ----
# NB: no mapfile here — macOS ships bash 3.2, where it does not exist. Anything
# in this port that assumes bash 4 breaks on the most likely operator laptop.
AZ_LIST="$(aws ec2 describe-availability-zones --filters Name=state,Values=available \
  --query 'AvailabilityZones[].ZoneName' --output text | tr '\t' '\n' | sort | head -2)"
AZ0="$(echo "$AZ_LIST" | sed -n 1p)"
AZ1="$(echo "$AZ_LIST" | sed -n 2p)"
[ -n "$AZ0" ] && [ -n "$AZ1" ] || die "could not resolve two availability zones"
log "azs: $AZ0 $AZ1"

# Mirror terraform's cidrsubnet() on a /16:
#   cidrsubnet(vpc, 8, i)   -> a.b.i.0/24        (public)
#   cidrsubnet(vpc, 4, i+1) -> a.b.(16*(i+1)).0/20  (private)
# The /20 offset lands in the THIRD octet. Bumping the second (10.20 -> 10.36)
# leaves the VPC range entirely and CreateSubnet rejects it as invalid.
BASE="${VPC_CIDR%%/*}"; O1="${BASE%%.*}"; REST="${BASE#*.}"; O2="${REST%%.*}"

# NB: these *_ensure helpers return an id on stdout, so every progress message
# inside them must go to stderr. A single `log` on stdout gets captured by the
# caller's $( ) and produces ids like "created subnet ...\nsubnet-abc" — which
# then fail downstream as "malformed". Terraform has no equivalent trap.
subnet_ensure() {  # name az cidr public
  local sname="$1" az="$2" cidr="$3" public="$4" sid
  sid="$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=$sname" "Name=vpc-id,Values=$VPC_ID" \
        --query 'Subnets[0].SubnetId' --output text 2>/dev/null || echo None)"
  if [ "$sid" = "None" ]; then
    sid="$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$cidr" --availability-zone "$az" \
      --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$sname}]" \
      --query 'Subnet.SubnetId' --output text)"
    if [ "$public" = "yes" ]; then
      aws ec2 modify-subnet-attribute --subnet-id "$sid" --map-public-ip-on-launch >/dev/null
    fi
    log "created subnet $sname $cidr -> $sid" >&2
  else
    log "subnet exists $sname -> $sid" >&2
  fi
  echo "$sid"
}

PUB0="$(subnet_ensure "$NAME-public-0" "$AZ0" "${O1}.${O2}.0.0/24" yes)"
PUB1="$(subnet_ensure "$NAME-public-1" "$AZ1" "${O1}.${O2}.1.0/24" yes)"
PRIV0="$(subnet_ensure "$NAME-runtime-0" "$AZ0" "${O1}.${O2}.16.0/20" no)"
PRIV1="$(subnet_ensure "$NAME-runtime-1" "$AZ1" "${O1}.${O2}.32.0/20" no)"
save PUBLIC_SUBNETS "$PUB0,$PUB1"
save PRIVATE_SUBNETS "$PRIV0,$PRIV1"

# ---- IGW ----
IGW_ID="$(aws ec2 describe-internet-gateways --filters "Name=tag:Name,Values=$NAME" \
  --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || echo None)"
if [ "$IGW_ID" = "None" ]; then
  IGW_ID="$(aws ec2 create-internet-gateway \
    --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'InternetGateway.InternetGatewayId' --output text)"
  aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
  log "created+attached igw $IGW_ID"
else
  log "igw exists $IGW_ID"
fi
save IGW_ID "$IGW_ID"

# ---- EIP + NAT ----
EIP_ALLOC="$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME-nat" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null || echo None)"
if [ "$EIP_ALLOC" = "None" ]; then
  EIP_ALLOC="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME-nat}]" \
    --query 'AllocationId' --output text)"
  log "allocated eip $EIP_ALLOC"
fi
save EIP_ALLOC "$EIP_ALLOC"
NAT_IP="$(aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC" --query 'Addresses[0].PublicIp' --output text)"
save NAT_EIP "$NAT_IP"

NAT_ID="$(aws ec2 describe-nat-gateways --filter "Name=tag:Name,Values=$NAME" "Name=state,Values=available,pending" \
  --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null || echo None)"
if [ "$NAT_ID" = "None" ]; then
  NAT_ID="$(aws ec2 create-nat-gateway --subnet-id "$PUB0" --allocation-id "$EIP_ALLOC" \
    --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'NatGateway.NatGatewayId' --output text)"
  log "creating nat $NAT_ID (this takes ~1-2 min)"
fi
save NAT_ID "$NAT_ID"
# A route to a NAT that is still pending is rejected, so this wait is load-bearing.
log "waiting for nat to become available…"
aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_ID"
log "nat available, eip $NAT_IP"

# ---- route tables ----
rt_ensure() {  # name
  local rname="$1" rid
  rid="$(aws ec2 describe-route-tables --filters "Name=tag:Name,Values=$rname" "Name=vpc-id,Values=$VPC_ID" \
        --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || echo None)"
  if [ "$rid" = "None" ]; then
    rid="$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
      --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$rname}]" \
      --query 'RouteTable.RouteTableId' --output text)"
    log "created route table $rname -> $rid" >&2
  fi
  echo "$rid"
}
RT_PUB="$(rt_ensure "$NAME-public")"
RT_PRIV="$(rt_ensure "$NAME-private")"
save RT_PUB "$RT_PUB"; save RT_PRIV "$RT_PRIV"

# create-route is not idempotent — it errors if the route exists, so tolerate it
aws ec2 create-route --route-table-id "$RT_PUB" --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$IGW_ID" >/dev/null 2>&1 || log "public default route already present"
aws ec2 create-route --route-table-id "$RT_PRIV" --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id "$NAT_ID" >/dev/null 2>&1 || log "private default route already present"

assoc() {  # rtb subnet
  aws ec2 associate-route-table --route-table-id "$1" --subnet-id "$2" >/dev/null 2>&1 \
    || log "association exists for $2"
}
assoc "$RT_PUB" "$PUB0"; assoc "$RT_PUB" "$PUB1"
assoc "$RT_PRIV" "$PRIV0"; assoc "$RT_PRIV" "$PRIV1"

# ---- runtime SG (egress only; AgentCore reaches runtimes via its data plane) ----
RUNTIME_SG="$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=$NAME-runtime-egress" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"
if [ "$RUNTIME_SG" = "None" ]; then
  RUNTIME_SG="$(aws ec2 create-security-group --group-name "$NAME-runtime-egress" \
    --description "Egress-only SG for AgentCore runtime ENIs" --vpc-id "$VPC_ID" \
    --query GroupId --output text)"
  log "created runtime sg $RUNTIME_SG"
fi
save RUNTIME_SG "$RUNTIME_SG"

log "network done: vpc=$VPC_ID nat_eip=$NAT_IP"

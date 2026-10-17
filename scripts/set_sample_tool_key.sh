#!/usr/bin/env bash
# Register (or rotate) the API key a sample tool's upstream requires, as an
# AgentCore Identity API-key credential provider named for this environment
# and tool. The foundation's next plan finds the provider by that name
# (scripts/discover_sample_tool_providers.py), publishes its ARN in the facts,
# and the workloads root creates the tool's gateway target. Nothing else to
# edit: no tfvars, no ARN to copy.
#
#   scripts/set_sample_tool_key.sh <tool> [--env staging] [--region <region>] [--key-file <path>] [--delete]
#
# The key is read from --key-file, or from stdin when no file is given
# (`scripts/set_sample_tool_key.sh stock-quote < key.txt`); never from an
# argument, so it does not land in shell history or `ps`. The key goes to
# the token vault only; the gateway role reads it at call time. --delete
# removes the provider, which removes the target on the next apply.
#
# Needs: bedrock-agentcore:{Create,Update,Delete}ApiKeyCredentialProvider and
# ListApiKeyCredentialProviders (an operator, not a pipeline role).
set -euo pipefail

tool="" env="" region="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-1}}" key_file="" delete=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env) env="$2"; shift 2 ;;
    --region) region="$2"; shift 2 ;;
    --key-file) key_file="$2"; shift 2 ;;
    --delete) delete=1; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) [ -z "$tool" ] || { echo "one tool at a time" >&2; exit 2; }; tool="$1"; shift ;;
  esac
done
[ -n "$tool" ] || { echo "usage: $0 <tool> [--env staging] [--region <region>] [--key-file <path>] [--delete]" >&2; exit 2; }
# same grammar as a tool package id (samples/tools/<id>); the discovery ignores anything else
[[ "$tool" =~ ^[a-z][a-z0-9-]{1,40}$ ]] || { echo "refusing: $(printf '%q' "$tool") is not a tool id" >&2; exit 2; }
case "$env" in
  "") prefix="sample-tool-" ;;
  staging) prefix="staging-sample-tool-" ;;
  *) echo "refusing: --env must be omitted (production) or staging" >&2; exit 2 ;;
esac
name="$prefix$tool"

existing="$(aws bedrock-agentcore-control list-api-key-credential-providers --region "$region" --output json \
  --query "credentialProviders[?name=='$name'].credentialProviderArn | [0]" | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v or "")')"

if [ "$delete" = 1 ]; then
  if [ -z "$existing" ]; then echo "no provider $name in $region; nothing to delete"; exit 0; fi
  aws bedrock-agentcore-control delete-api-key-credential-provider --region "$region" --name "$name" >/dev/null
  echo "deleted provider $name; the next foundation apply drops the $tool target"
  exit 0
fi

if [ -n "$key_file" ]; then
  key="$(cat -- "$key_file")"
elif [ -t 0 ]; then
  printf 'API key for %s (%s), not echoed: ' "$tool" "$name" >&2
  IFS= read -rs key; echo >&2
else
  key="$(cat)"
fi
key="${key%$'\n'}"; key="${key%$'\r'}"
[ -n "$key" ] || { echo "refusing: empty key" >&2; exit 2; }
case "$key" in *[[:space:]]*) echo "refusing: the key contains whitespace (wrong file?)" >&2; exit 2 ;; esac

# the key travels as a CLI argument of this one process only; keep it out of any
# shell trace a caller may have on
{ set +x; } 2>/dev/null
if [ -n "$existing" ]; then
  aws bedrock-agentcore-control update-api-key-credential-provider --region "$region" --name "$name" --api-key "$key" >/dev/null
  echo "rotated the key of provider $name ($existing); no apply needed, the gateway reads it on the next call"
else
  arn="$(aws bedrock-agentcore-control create-api-key-credential-provider --region "$region" --name "$name" --api-key "$key" \
    --query credentialProviderArn --output text)"
  echo "created provider $name ($arn); the next foundation apply publishes it and the $tool target follows"
fi

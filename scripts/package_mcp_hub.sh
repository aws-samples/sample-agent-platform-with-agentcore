#!/usr/bin/env bash
# Package an MCP hub source tree and upload it to the workspace bucket, where
# the hub EC2's user_data pulls it from (terraform/modules/mcp_hub_demo).
#
# Usage: scripts/package_mcp_hub.sh <path-to-hub-source> [s3-key]
#
# The source tree is any checkout shaped like the sample-mcp-hub-sso-auth
# repository: hub/ (the hub itself) and servers/ (demo backends) at the root.
# The zip is flat — hub/ and servers/ at the archive root — because the
# instance unpacks it straight into /opt/mcp-hub/src.
set -euo pipefail

SRC="${1:?usage: package_mcp_hub.sh <path-to-hub-source> [s3-key]}"
KEY="${2:-mcp-hub/source.zip}"
TF_DIR="$(cd "$(dirname "$0")/../terraform" && pwd)"

for required in hub/app.py hub/requirements.txt servers/requirements.txt; do
  if [[ ! -f "$SRC/$required" ]]; then
    echo "not a hub source tree: $SRC is missing $required" >&2
    exit 1
  fi
done

BUCKET="$(terraform -chdir="$TF_DIR" output -raw workspace_bucket_name)"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
ZIP="$STAGING/source.zip"

(cd "$SRC" && zip -q -r "$ZIP" hub servers \
  -x '*/__pycache__/*' -x '*.pyc' -x '*/.venv/*')

aws s3 cp "$ZIP" "s3://$BUCKET/$KEY"
echo "uploaded: s3://$BUCKET/$KEY"
echo "next: terraform apply -var enable_mcp_hub_demo=true, then scripts/seed_mcp_hub_demo.py"
echo "(an already-running hub instance picks up a new zip only on replacement:"
echo " taint aws_instance.hub or bump user_data)"

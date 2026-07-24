#!/usr/bin/env bash
# Stage topic-selection inputs into the platform S3 workspace so the cloud
# orchestrator can read them. Phase 1: the two feeds are snapshots of the local
# anthropic-tracker / ai-pulse output; index + blacklist are the genai-playbook
# dedup truth sources. (Phase 2's feed layer produces feeds/{name}/ on-cloud and
# the orchestrator prefers those over these staged snapshots.)
set -euo pipefail

BUCKET="${PLATFORM_WORKSPACE_BUCKET:?set PLATFORM_WORKSPACE_BUCKET (agent-platform-workspaces-<account>-<region>)}"
REGION="${AWS_REGION:-ap-northeast-1}"
CLAUDE_ROOT="${CLAUDE_ROOT:-$HOME/Desktop/Claude}"
GENAI="$CLAUDE_ROOT/genai-playbook"
PREFIX="topic-selection/inputs"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

latest() { ls -t "$1"/*.md 2>/dev/null | head -1; }

TRACKER="$(latest "$CLAUDE_ROOT/anthropic-tracker")"
PULSE="$(latest "$CLAUDE_ROOT/ai-pulse")"
[ -n "$TRACKER" ] && aws s3 cp "$TRACKER" "s3://$BUCKET/$PREFIX/anthropic-tracker.md" --region "$REGION" && echo "tracker: $(basename "$TRACKER")"
[ -n "$PULSE" ]   && aws s3 cp "$PULSE"   "s3://$BUCKET/$PREFIX/ai-pulse.md"          --region "$REGION" && echo "pulse:   $(basename "$PULSE")"

# index = genai-playbook content index (from CLAUDE.md) + the real on-disk topic dirs
{
  sed -n '/## 内容索引/,/## 发布与部署/p' "$GENAI/CLAUDE.md" 2>/dev/null || true
  echo; echo "## 实际目录(磁盘,索引表可能漏登)"
  ls -d "$GENAI"/*/ 2>/dev/null | xargs -n1 basename \
    | grep -vE '^(references|repo-export|topic-shortlist|traffic-analytics|about-page)$' | sed 's/^/- /'
} > "$TMP/index.md"
aws s3 cp "$TMP/index.md" "s3://$BUCKET/$PREFIX/index.md" --region "$REGION"

BL="$GENAI/topic-shortlist/blacklist.md"
if [ -f "$BL" ]; then
  aws s3 cp "$BL" "s3://$BUCKET/$PREFIX/blacklist.md" --region "$REGION"
else
  echo "(no blacklist.md — dedup blacklist gate will be empty)"
fi

echo "staged → s3://$BUCKET/$PREFIX/"

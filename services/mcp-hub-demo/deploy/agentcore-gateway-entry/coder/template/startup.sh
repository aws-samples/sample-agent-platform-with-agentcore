#!/bin/sh
# Startup script of the mcp-hub workspace (run by the Coder agent). Installs
# Claude Code and Codex, points both at the JWT gateway with the user's
# Keycloak token, starts code-server. The one credential written here is the
# agent's own token (see the headers helper below); no model key, no user
# token.
set -eu

VERSION=2.1.322
CODEX_VERSION=0.170.0
case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64; TRIPLE=aarch64-unknown-linux-musl
    SHA512=618369d68a7ced659c04413a8c2d6778e8cc0cf767e08cbe9141f215c2351ac2e5e08c07ec48d997054a729b19a956c4d085c415a3a5766c8e1c2d72de3d7c02
    CODEX_SHA512=9892e6e5e89afc672e902ad17d51276150f974e5c613b726a5df5d27f8b191288a89fcc300f686abcdd9126c15864dcfc5b7533d448b4699d765029b352e47a0 ;;
  x86_64|amd64)  ARCH=x64; TRIPLE=x86_64-unknown-linux-musl
    SHA512=d03a1a3405cb58261820669c85604291352c4bc74e3a0be4e11d9f14a76b2de8cef579514f069650c0fd38d7439afd1cb1da70c63e2a2d437acd631a1b64b3ea
    CODEX_SHA512=e9c09925a64bd7bb624c0be17753e2aedf2fb15d5d3fc09f1bde3e207ca050ee26db8a182d0ac82272492f22b370cbffb94a276eed5d176266717840fece5bb2 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

# Everything that goes into a config file or the helper below, checked
# first: a quote or a line break in one of them would rewrite the file
# around it.
for v in "${JWT_GATEWAY_URL:-}" "${CODEX_MODEL:-}" "${AWS_REGION:-}" "${CODER_AGENT_URL:-}"; do
  case "$v" in ''|*[!A-Za-z0-9.:/_-]*) echo "unexpected value in the workspace environment: '$v'" >&2; exit 1 ;; esac
done

# The native build, straight from the npm registry, pinned by version and
# checked against the registry's published sha512 before anything runs.
mkdir -p "$HOME/.local/bin" "$HOME/.local/lib" "$HOME/project" /tmp/cc
curl -fsSL -o /tmp/cc/claude.tgz \
  "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-$ARCH/-/claude-code-linux-$ARCH-$VERSION.tgz"
echo "$SHA512  /tmp/cc/claude.tgz" | sha512sum -c -
tar -xzf /tmp/cc/claude.tgz -C /tmp/cc package/claude
install -m 0755 /tmp/cc/package/claude "$HOME/.local/bin/claude"
rm -rf /tmp/cc

# Codex: the platform package `npm i -g @openai/codex` would pick, from the
# same registry, pinned and checked the same way. Kept: the binary, its
# code-mode host, ripgrep and bubblewrap; left out: the voice files (about
# 170 MB). The binary finds the rest from codex-package.json beside it, so
# the directory stays whole and PATH gets a link to the binary.
mkdir -p /tmp/cx
curl -fsSL -o /tmp/cx/codex.tgz \
  "https://registry.npmjs.org/@openai/codex/-/codex-$CODEX_VERSION-linux-$ARCH.tgz"
echo "$CODEX_SHA512  /tmp/cx/codex.tgz" | sha512sum -c -
CX="package/vendor/$TRIPLE"
tar -xzf /tmp/cx/codex.tgz -C /tmp/cx "$CX/codex-package.json" "$CX/bin" "$CX/codex-path" "$CX/codex-resources/bwrap"
rm -rf "$HOME/.local/lib/codex"
mv "/tmp/cx/$CX" "$HOME/.local/lib/codex"
ln -sf "$HOME/.local/lib/codex/bin/codex" "$HOME/.local/bin/codex"
rm -rf /tmp/cx

# code-server's terminals start from a login shell (/etc/profile sets PATH
# anew), then run an interactive non-login bash, which reads ~/.bashrc. So
# the PATH goes into both files, or the IDE's terminal finds no claude.
cat > "$HOME/.bashrc" <<'SHELLRC'
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac
SHELLRC
cp "$HOME/.bashrc" "$HOME/.profile"

# The user's Keycloak access token, from Coder (external auth `keycloak`):
# Coder refreshes it with the refresh token it keeps. Both clients ask the
# same helper for it. Claude Code runs it with the agent's environment;
# Codex runs it with a cleared one (HOME, PATH, locale), in which neither
# CODER_AGENT_TOKEN nor CODER_AGENT_URL survives. So the helper takes both
# from what is written here: the URL into its text, the token into a file
# only this user can read. The file adds no reader: the same token is in the
# environment of every process the agent starts, and in this pod's spec; and
# what it fetches is this workspace owner's access token, which is what the
# helper hands out anyway. A login shell resets PATH, so the helper names
# the coder binary by absolute path.
CODER_BIN="$(command -v coder || true)"
[ -x "$CODER_BIN" ] || { echo "no coder binary in the workspace" >&2; exit 1; }
[ -n "${CODER_AGENT_TOKEN:-}" ] || { echo "no agent token in the startup environment" >&2; exit 1; }
AGENT_TOKEN_FILE="$HOME/.config/mcp-hub/agent-token"
mkdir -p "$HOME/.config/mcp-hub"
chmod 0700 "$HOME/.config/mcp-hub"
( umask 077; printf '%s' "$CODER_AGENT_TOKEN" > "$AGENT_TOKEN_FILE" )
cat > "$HOME/.local/bin/mcp-hub-headers" <<HELPER
#!/bin/sh
# headersHelper of the mcp-hub MCP server: prints the Authorization header.
set -eu
token="\$("$CODER_BIN" external-auth access-token keycloak --agent-url "$CODER_AGENT_URL" --agent-token-file "$AGENT_TOKEN_FILE")"
printf '{"Authorization": "Bearer %s"}' "\$token"
HELPER
chmod 0755 "$HOME/.local/bin/mcp-hub-headers"

# User scope: the helper runs without a folder trust prompt, and keeps the
# agent's environment. The Authorization header it returns means Claude Code
# never starts an OAuth sign-in of its own for this server.
"$HOME/.local/bin/claude" mcp remove --scope user mcp-hub >/dev/null 2>&1 || true
"$HOME/.local/bin/claude" mcp add-json --scope user mcp-hub \
  "{\"type\":\"http\",\"url\":\"$JWT_GATEWAY_URL\",\"headersHelper\":\"$HOME/.local/bin/mcp-hub-headers\"}"

# Codex: GPT on Bedrock through the pod's role (the built-in provider signs
# bedrock-runtime's Responses API with SigV4), the same gateway through the
# same helper. A helper header carrying Authorization means Codex starts no
# OAuth sign-in for the server either.
# Its own sandbox (bubblewrap) needs a user namespace, which this pod
# (RuntimeDefault seccomp, every capability dropped) does not give it: the
# pod is the boundary, as it is for Claude Code here, and Codex still asks
# before what its approval policy names.
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<CODEXCFG
model = "$CODEX_MODEL"
model_provider = "amazon-bedrock-runtime"
approval_policy = "on-request"
sandbox_mode = "danger-full-access"
check_for_update_on_startup = false

[model_providers.amazon-bedrock-runtime.aws]
region = "$AWS_REGION"

[projects."$HOME/project"]
trust_level = "trusted"

[mcp_servers.mcp-hub]
url = "$JWT_GATEWAY_URL"
http_headers_helper = "$HOME/.local/bin/mcp-hub-headers"
CODEXCFG

# The image ships code-server. Loopback only: Coder proxies it to the owner.
nohup code-server --bind-addr 127.0.0.1:13337 --auth none --disable-telemetry \
  "$HOME/project" >/tmp/code-server.log 2>&1 </dev/null &

#!/usr/bin/env bash
# Configure Claude Code (user scope, visible from every directory / new shell) to use the nx deep-research MCP endpoint.
#  - installs uv/uvx if missing and makes sure ~/.local/bin is on PATH for new shells
#  - writes the least-privilege AWS profile (settings mcp_client.aws_profile) from Secrets Manager (needs your normal AWS credentials once)
#  - registers the MCP server at USER scope (removes any stale local-scope entry) and verifies with `claude mcp list`
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"; [ -x "$PY" ] || PY=python3
# defaults come from config/settings.yaml (+ local.yaml) and config/deploy_state.json (written by deploy.py or use_stack.py)
eval "$(cd "$ROOT" && "$PY" - <<'PYEOF'
import re, shlex, sys
sys.path.insert(0, "src")
from deepresearch.config import load_settings
s = load_settings()
url = s.state("gateway_url") or ""
m = re.search(r"\.gateway\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com", url)
vals = {"D_GATEWAY_URL": url, "D_GATEWAY_REGION": m.group(1) if m else s.gateway_region,
        "D_PROFILE": s.raw["mcp_client"]["aws_profile"], "D_SECRET_ID": s.state("mcp_client_secret_id") or s.raw["mcp_client"]["secret_id"],
        "D_SECRET_REGION": s.region}
print("\n".join(f"{k}={shlex.quote(v or '')}" for k, v in vals.items()))
PYEOF
)"
SERVER_NAME="${SERVER_NAME:-nx-deep-research}"
GATEWAY_URL="${GATEWAY_URL:-$D_GATEWAY_URL}"
GATEWAY_REGION="${GATEWAY_REGION:-$D_GATEWAY_REGION}"
AWS_MCP_PROFILE="${AWS_MCP_PROFILE:-$D_PROFILE}"
SECRET_ID="${SECRET_ID:-$D_SECRET_ID}"
SECRET_REGION="${SECRET_REGION:-$D_SECRET_REGION}"
[ -n "$GATEWAY_URL" ] || { echo "Gateway URL unknown: run scripts/use_stack.py <stack-name> (CloudFormation) or scripts/deploy.py first, or export GATEWAY_URL"; exit 1; }

say() { printf '\033[1;34m[setup-mcp]\033[0m %s\n' "$*"; }

# 1) uv / uvx --------------------------------------------------------------------------------------------
if ! command -v uvx >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uvx" ]; then
  say "installing uv (provides uvx) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || pip install --user -q uv
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uvx >/dev/null || { echo "uvx still not found; install uv manually: https://docs.astral.sh/uv/"; exit 1; }
say "uvx: $(command -v uvx) ($(uvx --version))"

# 2) PATH for future shells (bash + zsh + login shells) --------------------------------------------------
LINE='export PATH="$HOME/.local/bin:$PATH"   # uvx for Claude Code MCP servers'
for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
  # .bashrc/.profile are created if missing (login vs. interactive shells); .zshrc only if the user has zsh
  if [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ] || [ "$rc" = "$HOME/.profile" ]; then
    grep -qs '\.local/bin' "$rc" 2>/dev/null || { echo "$LINE" >> "$rc"; say "added ~/.local/bin to PATH in $rc"; }
  fi
done

# 3) least-privilege AWS profile ------------------------------------------------------------------------
if ! aws configure get aws_access_key_id --profile "$AWS_MCP_PROFILE" >/dev/null 2>&1; then
  say "writing AWS profile [$AWS_MCP_PROFILE] from Secrets Manager $SECRET_ID ..."
  SECRET_JSON=$(aws secretsmanager get-secret-value --region "$SECRET_REGION" --secret-id "$SECRET_ID" --query SecretString --output text)
  aws configure set aws_access_key_id     "$(printf '%s' "$SECRET_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["aws_access_key_id"])')"     --profile "$AWS_MCP_PROFILE"
  aws configure set aws_secret_access_key "$(printf '%s' "$SECRET_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["aws_secret_access_key"])')" --profile "$AWS_MCP_PROFILE"
  aws configure set region "$GATEWAY_REGION" --profile "$AWS_MCP_PROFILE"
fi
say "profile [$AWS_MCP_PROFILE] -> $(aws sts get-caller-identity --profile "$AWS_MCP_PROFILE" --query Arn --output text)"

# 4) Claude Code registration at USER scope --------------------------------------------------------------
command -v claude >/dev/null || { echo "claude CLI not found in PATH"; exit 1; }
claude mcp remove "$SERVER_NAME" -s local  >/dev/null 2>&1 || true
claude mcp remove "$SERVER_NAME" -s user   >/dev/null 2>&1 || true
claude mcp add -s user -e "AWS_PROFILE=$AWS_MCP_PROFILE" -e "AWS_REGION=$GATEWAY_REGION" --transport stdio "$SERVER_NAME" -- \
  uvx mcp-proxy-for-aws-cli@latest "$GATEWAY_URL" --region "$GATEWAY_REGION" --service bedrock-agentcore --profile "$AWS_MCP_PROFILE" \
  --timeout 900 --read-timeout 900 --write-timeout 900 --tool-timeout 900 >/dev/null
say "registered '$SERVER_NAME' at user scope (~/.claude.json), verifying ..."

# 5) verify from a neutral directory so local-scope config cannot mask the result --------------------------
( cd /tmp && claude mcp list 2>&1 | grep -F "$SERVER_NAME" ) || { echo "verification failed; run: claude mcp get $SERVER_NAME"; exit 1; }
say "done. Open a NEW terminal (or 'source ~/.bashrc'), start 'claude' in any directory and run /mcp."

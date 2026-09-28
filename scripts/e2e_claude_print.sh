#!/usr/bin/env bash
# End-to-end tests driven by Claude Code in print mode (`claude -p`): exercises every user-facing mode of the
# nx-deep-research MCP server the way a real user would, and verifies that the research report (.md) really exists.
#
#   bash scripts/e2e_claude_print.sh                                  # all scenarios with the default query
#   bash scripts/e2e_claude_print.sh live                             # one scenario: websearch | live | async | sync | callback | cancel | all
#   bash scripts/e2e_claude_print.sh live -q "2026 年量子纠错的产业化进展" -d quick -o ./reports/qec.md
#
# Options
#   -q|--query   <text>   custom research question (default: AgentCore Runtime V2 vs V1)
#   -d|--depth   <quick|standard|deep>   research depth for live/sync (default quick; standard/deep need a longer -t)
#   -o|--out     <path>   where Claude Code must save the Markdown report (default ./e2e_out/report_<scenario>.md)
#   -t|--timeout <sec>    per-scenario timeout (default 1500)
#   -m|--model   <id>     model for `claude -p` (default: your Claude Code default)
#   -s|--server  <name>   MCP server name (default nx-deep-research)
#
# Prerequisites: scripts/setup_claude_mcp.sh done (user-scope registration), dispatcher running, `claude` + `aws` on PATH.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

SCENARIO=all; QUERY="2026 年 9 月发布的 Amazon Bedrock AgentCore Runtime V2 相比 V1 有哪些变化？请引用来源。"
DEPTH=quick; OUT=""; TIMEOUT=1500; MODEL=""; SERVER=nx-deep-research
[ $# -gt 0 ] && [[ "$1" != -* ]] && { SCENARIO="$1"; shift; }
while [ $# -gt 0 ]; do
  case "$1" in
    -q|--query) QUERY="$2"; shift 2;;
    -d|--depth) DEPTH="$2"; shift 2;;
    -o|--out) OUT="$2"; shift 2;;
    -t|--timeout) TIMEOUT="$2"; shift 2;;
    -m|--model) MODEL="$2"; shift 2;;
    -s|--server) SERVER="$2"; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
OUT_DIR="${OUT_DIR:-./e2e_out}"; mkdir -p "$OUT_DIR"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VALIDATOR="$ROOT/skills/deep-research-harness/scripts/validate_report.py"
MODEL_FLAG=""; [ -n "$MODEL" ] && MODEL_FLAG="--model $MODEL"
ALLOW="--allowedTools mcp__${SERVER}__*,Read,Write"
PASS=0; FAIL=0

say() { printf '\033[1;34m[e2e]\033[0m %s\n' "$*"; }

claude_run() {  # name prompt -> writes $OUT_DIR/<name>.txt, echoes rc
  local name="$1" prompt="$2" out="$OUT_DIR/$name.txt"
  timeout "$TIMEOUT" claude -p "$prompt" $MODEL_FLAG $ALLOW --permission-mode bypassPermissions --output-format text \
      > "$out" 2>&1 <<< ""
  echo $?
}

verify_report() {  # name local_md_path claude_output -> 0/1 ; downloads the S3 copy too and validates structure
  local name="$1" local_md="$2" out="$3" ok=1
  local s3uri; s3uri=$(grep -oE 's3://[^ "]+report\.md' "$out" | head -1)
  if [ -n "$s3uri" ]; then
    if aws s3 cp "$s3uri" "$OUT_DIR/${name}_from_s3.md" --only-show-errors; then
      say "S3 copy: $s3uri -> $OUT_DIR/${name}_from_s3.md ($(wc -c < "$OUT_DIR/${name}_from_s3.md") bytes)"
    else say "S3 object missing: $s3uri"; ok=0; fi
  else say "no s3://.../report.md path in Claude output"; ok=0; fi
  if [ -s "$local_md" ]; then
    say "local report: $local_md ($(wc -c < "$local_md") bytes)"
  else say "local report not written: $local_md"; ok=0; fi
  local md="$local_md"; [ -s "$md" ] || md="$OUT_DIR/${name}_from_s3.md"
  if [ -s "$md" ]; then
    grep -q "## Bibliography" "$md" || { say "missing ## Bibliography"; ok=0; }
    grep -Eq '\[1\]' "$md" || { say "no [1] citation"; ok=0; }
    [ "$(wc -c < "$md")" -ge 3000 ] || { say "report too short"; ok=0; }
    if [ -f "$VALIDATOR" ]; then
      python3 "$VALIDATOR" --report "$md" > "$OUT_DIR/${name}_validate.txt" 2>&1 \
        && say "validate_report.py: PASSED" || { say "validate_report.py reported errors (see $OUT_DIR/${name}_validate.txt)"; ok=0; }
    fi
    if [ -s "$local_md" ] && [ -s "$OUT_DIR/${name}_from_s3.md" ]; then
      cmp -s "$local_md" "$OUT_DIR/${name}_from_s3.md" && say "local file identical to S3 copy" || say "note: local file differs from S3 copy (Claude may have re-formatted)"
    fi
  fi
  [ $ok -eq 1 ]
}

run_case() {  # name prompt expect_regex [local_md]
  local name="$1" prompt="$2" expect="$3" local_md="${4:-}"
  say "=== [$name] $(date +%T) depth=$DEPTH"
  local rc; rc=$(claude_run "$name" "$prompt"); local out="$OUT_DIR/$name.txt"
  local ok=0
  if [ "$rc" -eq 0 ] && grep -Eq "$expect" "$out"; then ok=1; fi
  if [ $ok -eq 1 ] && [ -n "$local_md" ]; then verify_report "$name" "$local_md" "$out" || ok=0; fi
  if [ $ok -eq 1 ]; then echo "PASS [$name]"; PASS=$((PASS+1)); else echo "FAIL [$name] rc=$rc (expected /$expect/)"; tail -15 "$out"; FAIL=$((FAIL+1)); fi
}

REPORT_LIVE="${OUT:-$OUT_DIR/report_live.md}"; REPORT_SYNC="${OUT:-$OUT_DIR/report_sync.md}"
mkdir -p "$(dirname "$REPORT_LIVE")" "$(dirname "$REPORT_SYNC")"
SAVE_INSTR='把返回的 content 字段原样写入本地文件 %s（用 Write 工具，不要改写内容），然后只输出三行：REPORT_S3=<report_s3 字段>、LOCAL_FILE=%s、STATUS=<status 字段>。'

case "$SCENARIO" in
  websearch|all)
    run_case websearch "用 $SERVER 的 WebSearch 工具搜索 \"$QUERY\"，maxResults 3，只输出每条结果的 URL，一行一个，不要其他文字。" 'https?://' ;;&
  live|all)
    run_case live "用 $SERVER 的 research_live 工具（提交并实时流式返回，depth=$DEPTH）研究：\"$QUERY\"。研究过程中不要调用其他工具。完成后$(printf "$SAVE_INSTR" "$REPORT_LIVE" "$REPORT_LIVE")" 'STATUS=completed' "$REPORT_LIVE" ;;&
  async|all)
    run_case async "用 $SERVER 的 research 工具（异步，depth=$DEPTH）提交研究：\"$QUERY\"，拿到 task_id 后调用 watch_research(task_id) 观看直到完成，完成后$(printf "$SAVE_INSTR" "${OUT:-$OUT_DIR/report_async.md}" "${OUT:-$OUT_DIR/report_async.md}")" 'STATUS=completed' "${OUT:-$OUT_DIR/report_async.md}" ;;&
  sync|all)
    run_case sync "用 $SERVER 的 run_research 工具（depth=$DEPTH, wait_seconds=780）研究：\"$QUERY\"。完成后$(printf "$SAVE_INSTR" "$REPORT_SYNC" "$REPORT_SYNC")" 'STATUS=completed' "$REPORT_SYNC" ;;&
  callback|all)
    run_case callback "用 $SERVER 的 submit_research 工具提交 depth=$DEPTH 研究 \"$QUERY\"，callback_url 填 https://example.com/nx-hook。只输出一行 TASK_ID=<task_id 字段>，不要轮询，不要调用其他工具。" 'TASK_ID=t[0-9]+-[0-9a-f]+' ;;&
  cancel|all)
    run_case cancel "用 $SERVER 的 submit_research 提交 depth=deep 研究 \"$QUERY\"，拿到 task_id 后立刻调用 cancel_research 取消它。只输出一行 CANCEL_STATUS=<cancel_research 返回的 status 字段>。" 'CANCEL_STATUS=cancelled' ;;&
esac

echo; echo "passed=$PASS failed=$FAIL  (outputs in $OUT_DIR)"
[ "$FAIL" -eq 0 ]

# 调试指南（日志在哪里、怎么看）

## 0. 一键汇总某个任务
```bash
source .venv/bin/activate
python scripts/debug_task.py --list 10              # 最近 10 个任务及状态
python scripts/debug_task.py <task_id>              # 任务记录 + 事件时间线（🔧 工具 / 💭 推理 / 💬 叙述 / cancelled）+ 回调结果 + 日志位置
python scripts/debug_task.py <task_id> --logs       # 再拉取 harness 会话、live MCP 服务器、Lambda 的 CloudWatch 日志
python scripts/debug_task.py --latest --logs
python scripts/debug_task.py <task_id> --requeue     # 孤儿/失败任务重排（标 retry + 重发 SQS）
python scripts/debug_task.py <task_id> --cancel      # 立即取消（StopRuntimeSession）
```
`live_research.py` 结束时也会打印 `task_id=… (debug: python scripts/debug_task.py …)`。

## 1. 各组件日志位置

| 组件 | 日志 | 关键行 |
|---|---|---|
| dispatcher / worker（本机进程或 EKS Pod） | stdout；`python scripts/run_dispatcher.py --log-file logs/dispatcher.log`（10 MB × 5 轮转）；级别 `DR_LOG_LEVEL=DEBUG` | `task <id> dispatched (inflight=…)`、`[<id>] start depth=… session=…`、`[<id>] progress searches=… in_tok=…`（每 2 s）、`[<id>] completed in …s stop=… report=s3://…`、`[<id>] throttled`、`[<id>] callbacks: {...}`、每 60 s `heartbeat stats=… queue=… inflight=…`。按任务 grep：`grep "\[t1790…\]" logs/dispatcher.log` |
| harness（Agent 本体，microVM 内） | CloudWatch 日志组 `/aws/bedrock-agentcore/runtimes/harness_nxdev_deepresearch_harness-qp16KN5Jyp-DEFAULT`；CloudWatch → GenAI Observability → Harnesses（每次模型调用、工具调用、耗时、token） | 用 `session_id`（任务记录里）过滤 |
| live 流式 MCP 服务器（Runtime） | `/aws/bedrock-agentcore/runtimes/<live_runtime_id>-DEFAULT`（id 见 `config/deploy_state.json` 的 `live_runtime_id`） | `research submitted task=…`、`stream end task=… events=…`、`stream cancelled by client task=…` |
| 语义合并 Lambda（VPC） | `/aws/lambda/nxdev-deepresearch-dedupe`（us-west-2） | `MERGE query=… -> task=… reason=exact_canon/sim_high/judge`、`CREATE task=… reason=no_candidates/sim_low`、`merge window expired` |
| MCP 研究工具 Lambda | `/aws/lambda/nxdev-deepresearch-mcp-tools`（us-east-1） | `tool=submit_research args=…`、`tool=… done in …s -> {...}`、`tool=… failed` + traceback |
| Gateway | CloudWatch → GenAI Observability → Gateway（每次 tools/call 的目标、耗时、限流）；限流响应体含 `retryAfter` | |
| 任务状态与事件 | DynamoDB `nxdev-sessions`：`pk=DR#TASK#<id>`，`sk=meta`（状态/进度/result/callbacks/last_error）与 `sk=evt#<ts>#…`（事件流）；`pk=DR#COUNTER sk=inflight#default`（并发槽） | |
| 报告与证据 | `s3://nxdev-deepresearch-131166810173-us-west-2/reports/YYYY/MM/DD/<task_id>/`：`report.md`、`sources.jsonl`、`evidence.jsonl` | |
| 流式客户端 | `python scripts/live_research.py … --verbose --log logs/live.log`：`--verbose` 打印原始 JSON-RPC 通知，`--log` 把每行带时间戳落盘 | |
| `claude -p` 端到端 | `e2e_out/<scenario>.txt`（Claude 输出）、`e2e_out/<scenario>_from_s3.md`、`e2e_out/<scenario>_validate.txt` | |

常用命令：
```bash
aws logs tail /aws/bedrock-agentcore/runtimes/harness_nxdev_deepresearch_harness-qp16KN5Jyp-DEFAULT --region us-west-2 --since 30m --follow
aws logs tail /aws/lambda/nxdev-deepresearch-mcp-tools --region us-east-1 --since 30m
aws sqs get-queue-attributes --queue-url "$(python -c 'import json;print(json.load(open("config/deploy_state.json"))["queue_url"])')" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --region us-west-2
```

## 2. 症状 → 原因 → 处理

| 症状 | 原因 | 处理 |
|---|---|---|
| 任务一直 `queued` | dispatcher 没在跑，或准入被拒（日志 `denied admission: inflight_limit/session_rate/search_budget/throttle_backoff`） | 启动/查看 dispatcher；`debug_task.py`；调大 `DR_DISPATCHER_MAX_INFLIGHT` |
| `runtimeClientError: maximum token limit` | 单轮输出超过 `maxTokens` | 已设 16384 并自动续跑；仍出现则检查技能"分节写入"指令 |
| `The MCP server requires protocol version 2026-07-28…` | 内部 Gateway 去掉了 2025-11-25 | 保持 `supported_versions: [2025-11-25, 2026-07-28]` |
| Gateway 直连 400 `Unsupported protocol version` | 客户端没带 `MCP-Protocol-Version` 或版本不在列表 | 用 2026-07-28 无状态方式或 2025-11-25 |
| 报告未落 S3（`completed_no_report`） | Agent 未按路径写 `report.md`，或 `DR_STATUS` 行缺失 | `debug_task.py --logs` 看最后几步；worker 日志 `no report collected; final_text_tail=…` |
| webhook 未收到 | `callback_url` 非 https / 目标不可达；日志 `webhook attempt n/3 … failed` | 看任务记录 `callbacks` 字段 |
| `ThrottlingException` 增多 | Bedrock TPM 或 Web Search 10 TPS 触顶 | 调度器自动收缩 C 并退避；申请配额（docs/QUOTAS.md） |
| 新终端 `claude mcp list` 为空 | 注册在 local 作用域 | `bash scripts/setup_claude_mcp.sh`（user 作用域） |
| Claude Code 连接失败 `uvx not found` | PATH 无 `~/.local/bin` | 新开终端或 `source ~/.bashrc` |
| 代理用了错误身份 | 环境里有 `AWS_WEB_IDENTITY_TOKEN_FILE`/`AWS_ACCESS_KEY_ID` | 代理参数已带 `--profile nxdev-mcp` |
| 同义提示词没合并 | 规范化结果不同（看 `dedupe_cache.py lookup` 的 canonical）、时间范围不同、相似度落入分开区 | `validate_embed.py --pair A B` 看原因（time_gate/sim_low/judge）；调 `dedupe.similarity_low/high` 或规范化提示词 |
| 不该合并的合并了 | 灰区判定器判 SAME | 提高 `similarity_low` 或改 JUDGE_SYSTEM；`dedupe_cache.py clear` 清缓存 |
| 取消后任务还在跑 | 任务被多个提交方共享 | 这是设计：带 subscription_id 只撤销自己；`force=true` 才全停 |
| dedupe Lambda 超时/报错 | Valkey 不可达或 Bedrock 限流 | 看 `/aws/lambda/nxdev-deepresearch-dedupe`；系统 fail-open 会创建独立任务 |
| 任务 `running` 但长时间零进度 | （已修复）worker 线程在首个 AgentCore 调用前挂住：并发创建 boto3 客户端不线程安全 + 命令 API 3700 s 读超时 | 客户端改为加锁单例、命令 API 180 s 读超时；看门狗每分钟检查，`running` 且 8 分钟无进度（`DR_WATCHDOG_STALE_SECONDS`）自动 retry+重排，日志 `watchdog: task … -> retry + requeued` |
| 任务一直 `queued`，DLQ 有消息 | （已修复）旧版 dispatcher 在并发槽满时退回消息，累加 SQS 接收次数，3 次后进入 DLQ | 现只在有空槽时收消息，`max_receive_count=50`，启动时自动回捞 DLQ 中未完成任务（日志 `redrive: … requeued N`）；手工 `debug_task.py <id> --requeue` |
| 同步调用 5–6 分钟后客户端读超时，但 Lambda 日志显示已返回 | 链路对无字节传输的响应有约 350 s 空闲超时（实测：≤326 s 成功、≥356 s 全部丢失） | `run_research` 已限时 300 s 并返回 `status=running`+`task_id`；需要一次阻塞拿到报告请用 `live___research_live` 并在 `_meta` 中带 `progressToken`（否则 Gateway 缓冲整个响应，无心跳） |
| `watch_research` 挂在排队任务上长时间无输出后断流 | 空闲 SSE 连接被中间链路断开 | （已修复）流中每 20 s 发送 `⏳` 心跳（progress + log 通知） |
| 流没有 💭 推理 | harness 未开 extended thinking | `harness.thinking_budget_tokens` > 0 后 `python scripts/deploy.py` |
| dispatcher 重启/崩溃后任务停在 `running`、并发槽偏高 | worker 被杀，SQS 重投时状态不可认领 | 已修复：dispatcher 启动时自动把 >5 分钟无进度的 `running` 任务标 `retry` 并重排（日志 `reconcile: … orphaned -> retry + requeued`），并把并发计数重同步；运行中收到重投消息且任务 >180 s 无进度时自动接管（`looks orphaned … taking over`）。手工：`debug_task.py <id> --requeue` |
| 取消后任务仍 running | 取消发生在 `queued`→`running` 切换瞬间 | 再调一次 `cancel_research`；worker 每 2 s 检查状态并停止 |

## 3. 手工探针
```bash
DR_E2E=1 python -m pytest tests/e2e -m e2e -k "gateway or smoke" -q     # Gateway / harness 基础连通
python scripts/live_research.py "test" --depth quick --verbose             # 看原始通知
python - <<'PY'
import sys; sys.path.insert(0,'src')
from deepresearch.config import load_settings; from deepresearch.harness_client import HarnessClient, new_session_id
hc=HarnessClient(load_settings()); sid=new_session_id()
print(hc.invoke_collect("Say OK", sid, overrides={"maxIterations":2}).text)      # 直接调 harness
print(hc.run_command(sid, "ls -la /tmp/research; python3 --version"))          # 进 microVM 看文件
PY
```

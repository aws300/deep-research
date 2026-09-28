# 部署文档

> **新环境请优先使用 CloudFormation 一键部署**：[CLOUDFORMATION.md](CLOUDFORMATION.md)（任意账号，us-east-1 / eu-west-1 / ap-northeast-1）。本文描述 boto3 脚本部署（`scripts/deploy.py`），以现有的 nxdev 部署为例：区域与名称来自 `config/local.yaml`。

## 0. 前提
- 账号 `131166810173`，角色具备管理员权限（当前 `nxdev-MasterRole`）。
- `nx` 栈（us-west-2）状态 CREATE_COMPLETE。
- 本机：Python ≥ 3.11、AWS CLI v2、凭证已配置。**不需要** Docker、CDK、Terraform。
- 区域约束：Web Search 连接器仅 us-east-1 / eu-west-1 / ap-northeast-1 → Gateway 固定在 us-east-1；harness 默认 us-west-2（复用 VPC/Memory/DynamoDB）。

## 1. 安装
```bash
cd /home/core/Workspace/projects/deepresearch
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/unit -q          # 无 AWS 调用
```

## 2. 配置
`config/settings.yaml` 已按 nx 栈填好。常改项：
- `harness.model_id`（默认 Sonnet 4.6）、`harness.memory_mode`（byo/managed/disabled）
- `gateway.domain_exclude`、`gateway.rate_limit_per_caller_rpm`
- `dispatcher.max_inflight`（C）、`dispatcher.workers`
所有键可用环境变量 `DR_<SECTION>_<KEY>` 覆盖，例如 `DR_DISPATCHER_MAX_INFLIGHT=3000`。

## 3. 部署（幂等，可重复执行）
```bash
python scripts/deploy.py
```
步骤与产出（写入 `config/deploy_state.json`，`config/stack_outputs.json` 缓存栈输出，密码类输出不落盘）：
1. 读取 nx 栈输出与子网/SG 物理 ID。
2. IAM：`nxdev-deepresearch-GatewayRole`（InvokeGateway + InvokeWebSearch）、`nxdev-deepresearch-HarnessExecRole`。
3. Gateway `nxdev-deepresearch-gw`（us-east-1，MCP，AWS_IAM，语义搜索开启）+ `web-search` 连接器 1.2.0 + 每主体 600 rpm 限流。
4. S3 桶 `nxdev-deepresearch-131166810173-us-west-2`（加密、阻止公开、报告 180 天过期）+ 上传技能包。
5. SQS `nxdev-deepresearch-intake` + DLQ；校验 DynamoDB `nxdev-sessions` 键结构与 TTL。
6. harness `nxdev_deepresearch_harness`：Sonnet 4.6、Gateway 工具、S3 技能、BYO Memory、VPC 模式、summarization 截断、150 迭代 / 3600 s。

若第 6 步因**跨区域 Gateway** 被拒绝（错误信息含 gateway），按提示改用：
```bash
python scripts/deploy.py --harness-region us-east-1      # 自动切到 PUBLIC 网络 + 托管 Memory
```
此时 DynamoDB/SQS 仍在 us-west-2（跨区 API 调用，无需改动）。

## 3.1 本次部署产出（2026-09-21）
| 资源 | 值 |
|---|---|
| Gateway | `nxdev-deepresearch-gw-ff2tunugib`（us-east-1，AWS_IAM，MCP 2025-11-25 + 2026-07-28），URL `https://nxdev-deepresearch-gw-ff2tunugib.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` |
| MCP 客户端密钥 | IAM 用户 `nxdev-deepresearch-mcp-client`（仅 InvokeGateway），密钥在 Secrets Manager `nxdev/deepresearch/mcp-iam-key` |
| Web Search target | `UKVOINJMSB`（connector web-search 1.2.0，排除 pinterest.com/quora.com） |
| harness | `arn:aws:bedrock-agentcore:us-west-2:131166810173:harness/nxdev_deepresearch_harness-3vFsGfc9r6`（VPC 模式，BYO Memory，跨区挂接 us-east-1 Gateway 已验证可用） |
| 底层 Runtime | `harness_nxdev_deepresearch_harness-qp16KN5Jyp`（CloudWatch 日志组 `/aws/bedrock-agentcore/runtimes/harness_nxdev_deepresearch_harness-qp16KN5Jyp-DEFAULT`） |
| S3 | `nxdev-deepresearch-131166810173-us-west-2` |
| SQS | `nxdev-deepresearch-intake`（+ `-dlq`） |
| IAM | `nxdev-deepresearch-GatewayRole`、`nxdev-deepresearch-HarnessExecRole`、用户 `nxdev-deepresearch-mcp-client` |

部署踩坑记录：harness 名称只允许 `[a-zA-Z][a-zA-Z0-9_]{0,39}`（不能有连字符）；`requireServiceS3Endpoint` 只能在 UpdateHarness 设置；
Gateway 直连 MCP 需带 `MCP-Protocol-Version: 2025-06-18` 头；技能包 `scripts/` 不会落盘，服务在首轮前用命令 API 预装到 `/tmp/research/tools/`；
`UpdateHarness` 的 `memory` 参数需包在 `{"optionalValue": ...}` 里；`requireServiceS3Endpoint` 对 2026-06-11 后创建的 Agent 不可修改（不要设置）；
预签名 URL 要用区域端点 + SigV4，否则 curl 得到 307 重定向 XML；`maxTokens` 4096 会让"一次写完整篇报告"的工具调用触发 `runtimeClientError: maximum token limit`，已提高到 16384 并让 worker 自动续跑。

## 4. 验证（端到端测试）
```bash
DR_E2E=1 python -m pytest tests/e2e -m e2e -s --timeout=2400
```
覆盖：Gateway tools/list → WebSearch 调用返回带 URL 的结果 → harness 冒烟 → harness 搜索并引用 →
microVM 内 Python 与技能包存在 → 完整流水线（submit → SQS → 准入 → 研究 → S3 报告结构校验 → 并发槽释放）。

手工验证：
```bash
python scripts/submit.py "对比 AgentCore Runtime V2 与 V1 的冷启动与计费差异" --depth quick --follow --print-report
```

## 5. 运行调度器
本地/任意主机：`python scripts/run_dispatcher.py`
EKS（复用 nxdev 集群，无 Service/Ingress）：构建镜像 `docker build -t <ecr>/nxdev-deepresearch-dispatcher .`，
按 `deploy/k8s/dispatcher.yaml` 部署，给 ServiceAccount 绑定 Pod Identity 角色（所需权限见文件头注释）。
多副本安全：并发计数在 DynamoDB 原子更新。

## 6. 扩容到 10000
1. 按 `docs/QUOTAS.md` 提交配额（`scripts/request_quotas.py --target 10000 --apply` + Support Case）。
2. 批准后逐档提高 `DR_DISPATCHER_MAX_INFLIGHT`：100 → 1000 → 3000 → 10000，同时提高 `session_create_per_sec`
   （≤ 批准值的 80%）与 `websearch_tps_budget`。
3. 观察：CloudWatch `AWS/Bedrock-AgentCore` `ActiveSessionCount`（Service=AgentCore.Runtime）、Gateway 限流指标、
   Bedrock `ThrottledCount`；调度器日志中 `denied admission` 原因分布。
4. 若单区域批准不足，复制部署到第二分片（`DR_HARNESS_REGION=us-east-1 DR_HARNESS_NAME=...` 或第二账号），
   每分片一个 dispatcher（`shard` 参数区分 DynamoDB 计数器）。

## 7. 成本参考（单任务，AgentCore 侧）
Runtime V1 microVM（harness 当前使用 V1 计费）：约 2 分钟活跃 CPU + 1.5 GB×20 分钟 ≈ $0.01；Web Search 30 次 ≈ $0.21；
Gateway/Memory/DynamoDB/SQS 合计 < $0.01。模型 token 另计，通常为最大项。

## 8. 回滚 / 清理
```bash
python scripts/teardown.py --yes            # 删除 harness、gateway、队列、桶、两个角色；不触碰 nx 栈
python scripts/teardown.py --yes --keep-gateway --keep-bucket
```

## 8.1 MCP endpoint（单 Gateway）

| 项 | 值 |
|---|---|
| Endpoint | `https://nxdev-deepresearch-gw-ff2tunugib.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` |
| 协议版本 | `2025-11-25` + `2026-07-28`（`server/discover` 可查）。harness 运行时目前协商 2025-11-25；2026-07-28 客户端走无状态模式 |
| 认证 | AWS_IAM（SigV4）。**静态密钥 = 最小权限 IAM 用户 `nxdev-deepresearch-mcp-client` 的访问密钥**，仅允许 `bedrock-agentcore:InvokeGateway` 作用于此 Gateway ARN |
| 密钥存放 | Secrets Manager `nxdev/deepresearch/mcp-iam-key`；本机 profile `~/.aws/credentials [nxdev-mcp]` |
| 轮换 | `python scripts/mcp_connect.py --rotate` |
| 工具 | `web-search___WebSearch`（Amazon Web Search）、`x_amz_bedrock_agentcore_search`（语义工具检索） |

为什么不只开 2026-07-28：harness 内部 MCP 客户端最高支持 2025-11-25，只开 2026-07-28 时 InvokeHarness 报
`The MCP server requires protocol version 2026-07-28, which this runtime does not support yet`（已实测）。
为什么不用 Cognito/JWT：Gateway 原生支持 SigV4，最小权限 IAM 密钥即是最简的"静态密钥"，无需身份提供方。

## 8.2 Claude Code 接入（一键脚本）

```bash
bash scripts/setup_claude_mcp.sh
```
脚本做四件事：安装 `uv`（提供 `uvx`）并把 `~/.local/bin` 写进 `~/.bashrc`/`~/.profile`/`~/.zshrc` 的 PATH；从 Secrets Manager
`nxdev/deepresearch/mcp-iam-key` 写入最小权限 AWS profile `[nxdev-mcp]`；以 **user 作用域**注册 `nx-deep-research`
（对所有目录、所有新终端可见，之前的 local 作用域只对本项目目录生效，这就是新窗口 `claude mcp list` 为空的原因）；最后在 `/tmp` 目录下用
`claude mcp list` 验证 `✔ Connected`。脚本可重复执行。运行后**新开终端**（或 `source ~/.bashrc`），在任意目录启动 `claude`，`/mcp` 即可看到。

手工等价命令：
```bash
claude mcp add -s user -e AWS_PROFILE=nxdev-mcp -e AWS_REGION=us-east-1 --transport stdio nx-deep-research -- \
  uvx mcp-proxy-for-aws-cli@latest \
  https://nxdev-deepresearch-gw-ff2tunugib.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
  --region us-east-1 --service bedrock-agentcore --profile nxdev-mcp
```
`--profile nxdev-mcp` 让代理显式使用该 profile；若只靠 `AWS_PROFILE` 环境变量，机器上若存在 `AWS_WEB_IDENTITY_TOKEN_FILE`/`AWS_ROLE_ARN`
或 `AWS_ACCESS_KEY_ID`，AWS SDK 会优先使用它们。代理内置 MCP SDK 1.28，与 Gateway 协商 2025-11-25。首次启动 `uvx` 需数十秒下载依赖。

### Claude Code 中可用的工具
| 工具（Claude Code 内名称前缀 `mcp__nx-deep-research__`） | 作用 |
|---|---|
| `web-search___WebSearch` | Amazon Web Search（query, maxResults, filters） |
| `research___submit_research(query, depth)` | 提交异步深度研究任务，返回 `task_id`（quick 约 3–6 分钟） |
| `research___get_research_status(task_id)` | 状态、进度计数、最近推理/工具步骤、报告 S3 路径 |
| `research___get_research_report(task_id, mode)` | `content` 返回 Markdown 正文（Claude Code 可直接写成 .md 文件）；`url` 返回 1 小时预签名下载链接 |
| `x_amz_bedrock_agentcore_search` | 网关语义工具检索 |

这三个研究工具由 Gateway 的 Lambda 目标 `research`（函数 `nxdev-deepresearch-mcp-tools`，us-east-1）实现，内部复用 SQS/DynamoDB/S3；
任务真正执行依赖 **dispatcher 在运行**（本机 `python scripts/run_dispatcher.py`，或按 `deploy/k8s/dispatcher.yaml` 部署到 EKS `nxdev`）。
报告只产出 Markdown，路径形如 `s3://nxdev-deepresearch-131166810173-us-west-2/reports/YYYY/MM/DD/<task_id>/report.md`，同目录还有
`sources.jsonl`、`evidence.jsonl`。e2e：`test_gateway_exposes_research_tools`、`test_mcp_research_tools_end_to_end`。

### 默认模式：实时流（live）

Gateway 上的第三个目标 `live` 是托管在 AgentCore Runtime 上的流式 MCP 服务器（`src/deepresearch/live_mcp_server.py`，
容器 `deploy/live-mcp.Dockerfile`，Runtime `nxdev_deepresearch_live_mcp`，us-west-2，PUBLIC 网络，仅出网）。Gateway 已开启
`streamingConfiguration.enableResponseStreaming`，工具执行期间以 SSE 下发 MCP `notifications/progress` 与 `notifications/message`。

| 工具（前缀 `mcp__nx-deep-research__`） | 作用 |
|---|---|
| `live___research(query, depth=quick)` | **默认入口**。提交并实时流出每一步（🔧 工具调用/查询词、💭 推理、⚙️ 系统事件），结束时返回 Markdown 正文、S3 路径、下载链接。客户端取消（Claude Code 按 Esc）会通过 `notifications/cancelled`/断连触发 `StopRuntimeSession`，研究立即停止 |
| `live___watch_research(task_id)` | 重新挂到运行中的任务继续看流（单次流窗口 13.5 分钟，受 Gateway 15 分钟限制；standard/deep 需再次调用） |
| `live___cancel_research(task_id)` / `research___cancel_research` | 立即取消：任务标记 cancelled，harness microVM 会话被 StopRuntimeSession 终止，并发槽释放 |
| `live___research_status(task_id)` | 无流式的状态查询 |

Lambda 目标 `research___*`（submit/run/status/report）保留为回退与回调路径，描述中已标注"优先使用 live___research"。
`live___research` 的流每 3 秒从 DynamoDB 事件表拉取增量；推理文本需 harness 模型开启 extended thinking 才会出现（当前 Sonnet 4.6 默认不输出）。
已实测：通过 `mcp-proxy-for-aws-cli`（stdio）连接的 MCP 客户端在 75 秒内收到 13 条 `notifications/progress`，内容为逐条工具调用与查询词，说明进度通知能穿过 Gateway 与代理到达 Claude Code 侧的 MCP 客户端；Claude Code 界面如何渲染这些通知需按 docs/TODO.md 用 `claude -p` 或交互会话确认。

终端里直接看流（不经 Claude Code）。所有 `python scripts/*.py` 都在项目虚拟环境中运行：
```bash
cd /home/core/Workspace/projects/deepresearch && source .venv/bin/activate   # 首次：python3 -m venv .venv && pip install -r requirements.txt
python scripts/live_research.py "研究问题" --depth deep --cache false --save ./reports/x.md   # research_live：逐条带时间戳打印 💭/💬/🔧；每 ~13 分钟流窗口结束自动 watch_research 重挂直到完成（--timeout 总时长，默认 3600）；Ctrl+C 撤销订阅
python scripts/live_research.py --watch <task_id>                              # 重新挂到运行中的任务
python scripts/live_research.py "研究问题" --assert                            # e2e：90 秒内出现事件、含思考步骤、报告完成
```
思考过程可见的两个来源：harness 模型已开启 extended thinking（`harness.thinking_budget_tokens: 3000`，产生 `reasoningContent` → 💭），
以及模型在工具调用之间的叙述文本（→ 💬）。worker 每 2 秒聚合写入，live 服务器每 2 秒拉取，端到端延迟约 2–5 秒。

触发示例（默认实时流）：
```
用 nx-deep-research 的 research 工具（异步）提交"2026 年 9 月 AgentCore Runtime V2 相比 V1 的变化"（depth=quick），
告诉我 task_id 和是否 merged，然后用 watch_research 观看直到完成，把 Markdown 保存到 ./reports/agentcore_v2.md 并给出 S3 路径。
（或一步到位：用 research_live 工具提交并实时转述步骤。）
```

### 语义合并：同义提示词 → 同一个 task_id（异步任务）

**默认 MCP 动作已改为异步**：`live___research(query, depth, callback_url, cache=true)` 与 `research___submit_research(…, cache=true)` 立即返回 `task_id`、`merged`、
`subscription_id`；所有提交类工具（含 `research_live`、`run_research`）都有 `cache` 参数：**默认 true 参与语义合并，false 强制新建独立任务且不写入缓存**；
CLI：`python scripts/live_research.py "…" --cache false`，SDK：`api.submit(query, cache=False)`。随后用 `live___watch_research(task_id)` 看实时流，或等待 `callback_url` 回调。`live___research_live` 仍提供"提交+流式"一体的方式。

判定流水线（`src/deepresearch/dedupe_core.py`，验证脚本 `scripts/validate_embed.py`，14/14 用例正确）：
1. **规范化**（Haiku 4.5，温度 0，注入当天日期）→ 严格 JSON `{topic, entities, aspects, time_from, time_to}`；相对时间解析为绝对日期
   （"昨天"/"最近1天" → 昨日；"最近一周" → 截至昨日 7 天）。
2. **时间硬门**：两侧都有时间范围且不同 → 不合并；单侧有时间 → 交判定器。
3. **精确规范键**相同 → 直接合并（实测"调研最近1天的科技新闻" / "给出昨天的科技新闻" / "Summarize yesterday's tech news" 三者 sim=1.0）。
4. **向量**：`us.cohere.embed-v4:0`（1024 维，search_query）对 "topic | entities | aspects" 求余弦；≥ 0.90 合并，< 0.68 分开，
   灰区交 **LLM 判定器**（Haiku：SAME/DIFFERENT）。
5. **5 秒合并窗口**：缓存未命中时对规范键加 `SET NX PX 5000` 锁，并发同义请求等待并拿到同一 task_id。
6. 任何 Valkey/Bedrock 故障 **fail-open**：照常创建独立任务并记日志。

缓存：ElastiCache Serverless Valkey `nxdev-valkey`（8.1，TLS，凭证在 Secrets Manager `nxdev/deepresearch/valkey`），键前缀 `{dr:dd}`，
默认 TTL 12 小时（`dedupe.ttl_seconds` 或环境变量 `DR_DEDUPE_TTL_SECONDS`）。8.1 Serverless 没有 FT.* 向量索引，故按时间范围分桶、
应用层扫描最近 `max_candidates` 个候选计算余弦（升级到 Valkey 8.2 节点型集群后可换 HNSW）。

**谁访问 Valkey**：它只在 VPC 内可达。VPC Lambda `nxdev-deepresearch-dedupe`（us-west-2，nx 私有子网 + DataServicesSG）是唯一代理；
us-east-1 的工具 Lambda 与公网模式的 live Runtime 通过它调用；VPC 内主机（本机、EKS）可 `DR_DEDUPE_MODE=direct` 直连。

**取消语义**（`cancel_research(task_id, subscription_id, force)`）：每个提交方是一个订阅；带 `subscription_id` 只撤销自己的回调/流；
后端研究只在**没有其他订阅者**（或 `force=true`）时才被 StopRuntimeSession 终止；对共享任务不带 subscription_id 的取消会被拒绝并返回剩余订阅数。
完成回调会发给所有订阅者的 `callback_url`。

运维：
```bash
python scripts/dedupe_cache.py stats | lookup "问题" | clear        # clear 只清合并缓存，不影响任务
python scripts/validate_embed.py                                    # 调整提示词/阈值后回归
python scripts/validate_embed.py --pair "问题A" "问题B"              # 看两条提示词会不会合并及原因
```

### 独立 WebSearch MCP（不依赖 nx-deep-research）
`scripts/websearch_mcp.py` 直接把 Gateway 的 Web Search 暴露为一个 stdio MCP 工具 `web_search(query, max_results, include_domains,
exclude_domains, from_date, to_date)`，也可作 CLI：
```bash
claude mcp add -s user -e AWS_PROFILE=nxdev-mcp websearch -- $(pwd)/.venv/bin/python $(pwd)/scripts/websearch_mcp.py
python scripts/websearch_mcp.py --search "AgentCore Runtime V2" --max 5 --include aws.amazon.com --from 2026-09-01
```

### 同步调用的时长限制（实测）
网络路径对"长时间无字节"的响应约 350 秒断开（实测 326 s 成功、356/367/376 s 均丢失，Lambda 侧已正常返回）。因此：
- `research___run_research` 最多等待 300 s（`DR_SYNC_WAIT_CAP`），超时返回 `status=running` 与 `task_id`，调用方用 `live___watch_research` 接续；
- 需要**一次阻塞调用必定拿到报告**时用 `live___research_live`，客户端须在请求 `_meta` 中带 `progressToken`（Claude Code 等标准 MCP 客户端默认会带）：Gateway 以 SSE 下发每一步与每 20 s 的 `⏳` 心跳，连接不会空闲，单窗口约 13.5 分钟。
- 实测：**不带 progressToken 时 Gateway 不建立流，整个响应被缓冲到结束**，同样会触发约 350 s 空闲断开；这类纯 JSON 客户端请用 `run_research`（≤300 s）+ `watch_research`。

### 不用轮询：同步工具与完成回调

| 方式 | 适用 | 机制 |
|---|---|---|
| `research___run_research(query, depth=quick, wait_seconds<=800)` | quick 档（3–6 分钟，上限约 13 分钟） | 一次工具调用，Lambda 服务端等待完成后直接返回 Markdown 正文、S3 路径与 1 小时下载链接。Claude Code 侧无任何状态轮询 |
| `research___submit_research(query, depth, callback_url)` | standard / deep | 任务完成或失败时，worker 向 `callback_url` POST 一条 JSON（`task_id`、`status`、`report_s3`、`download_url`），头部 `X-NX-Signature: sha256=<HMAC-SHA256>`（密钥在 Secrets Manager `nxdev/deepresearch/webhook-secret`），失败重试 3 次 |
| SNS 主题 `nxdev-deepresearch-task-events`（us-west-2） | 任意订阅方 | 每个任务完成/失败都会发布同一份 JSON；可订阅到 SQS、Lambda、Email、HTTPS，消息属性 `event`、`actor_id` 可做过滤 |

为支撑 13 分钟的同步调用，Claude Code 端的代理参数已加 `--timeout 900 --read-timeout 900 --write-timeout 900 --tool-timeout 900`，
Lambda 超时 840 秒，Gateway 工具调用上限 15 分钟。

为什么不是"服务器推送到 Claude Code"：Claude Code 的 Channels（MCP 服务器主动向会话推送事件）处于研究预览，**不支持 Amazon Bedrock 认证**，
当前环境不可用；A2A 的 push notification 也是 webhook，换协议对 Claude Code 没有增益。因此推送以 Webhook/SNS 形式提供给你的系统，
Claude Code 内用同步工具消除轮询。

触发示例（无轮询）：
```
用 nx-deep-research 的 run_research 做一个 quick 研究："2026 年 9 月 AgentCore Runtime V2 相比 V1 的变化"，
拿到结果后把 Markdown 保存到 ./reports/agentcore_v2.md，并告诉我 S3 路径。
```
带回调的长任务：
```
用 nx-deep-research 提交 standard 深度研究 "……"，callback_url 填 https://hooks.example.com/nx，然后告诉我 task_id 即可，不要轮询。
```

任何能做 SigV4 的 MCP 客户端（含 2026-07-28 无状态客户端）可直接 HTTP 调用该 URL，见 e2e `test_gateway_accepts_least_privilege_iam_key`。

若后续把研究服务本身以 MCP 暴露，推荐在 AgentCore Runtime 上部署 **MCP Python SDK v2（≥ 2.2.0，原生 2026-07-28）** 的无状态服务器
（`FastMCP(stateless_http=True)`，工具 `submit_research / get_status / get_report`，`task_id` 作为显式状态句柄），再作为 MCP server target 挂到同一个 Gateway。

## 8.5 语义合并的生产部署要点

| 项 | 建议 |
|---|---|
| 去重 Lambda 并发 | 每次提交 1 次 Lambda 调用（约 0.8–1.5 s，含 Haiku 规范化 + 嵌入 + 可能的判定）。10000 并发提交/分钟 ≈ 170 并发 Lambda；设置 `ReservedConcurrentExecutions` ≥ 300，并注意 VPC Lambda 的 ENI 冷启动（首个实例 +1–2 s） |
| Bedrock 配额 | Haiku 4.5 与 Cohere Embed V4 的 TPM/RPM 见 docs/QUOTAS.md 12a/12b；提交峰值 > 2000 次/分钟需申请 Embed RPM |
| Valkey 容量 | 每个任务约 5 KB（1024 维 float32 ≈ 4 KB + 元数据）；12 h 内 10 万任务 ≈ 0.5 GB，Serverless 自动扩展；候选扫描上限 `max_candidates=200` 保证单次查找 ≤ 1 MB 读 |
| 阈值与提示词变更 | 先跑 `python scripts/validate_embed.py`（14 用例必须全对），再 `python scripts/deploy.py --only dedupe`，然后 `scripts/dedupe_cache.py clear` 使旧向量失效 |
| TTL | `dedupe.ttl_seconds`（默认 43200）→ 重新部署 dedupe Lambda 生效；临时调整用 Lambda 环境变量 `DR_DEDUPE_TTL_SECONDS`（对新条目生效） |
| 关闭合并 | `dedupe.enabled: false` 并重部署 Lambda/live/tools，或 SDK `submit(dedupe=False)` |
| 故障模式 | Valkey/Bedrock 不可用 → fail-open（创建独立任务，日志 `dedupe failed … -> creating task without merge`）；不会阻塞提交 |
| 安全 | Valkey 凭证仅在 Secrets Manager `nxdev/deepresearch/valkey`，只有 dedupe Lambda/live Runtime/tools Lambda 角色可读；Valkey 仅 VPC 内可达 |
| 观测 | `/aws/lambda/nxdev-deepresearch-dedupe` 的 `MERGE`/`CREATE` 行可直接统计合并率；建议加 CloudWatch 指标过滤器 |
| 一致性 | 合并窗口锁 5 s；并发同义请求超过窗口时会各建一个任务（日志 `merge window expired`），这是有意的降级 |

## 9. 已知限制
- harness 单次调用 ≤ 3600 s（流式上限 60 min）；deep 档位若超时，worker 记录 `timeout_exceeded`，可在同会话续跑（后续迭代）。
- Web Search 中文语料覆盖待验证；可在 Gateway 追加第二搜索源目标。
- harness 无独立配额，受 Runtime 配额约束；Memory 开启时 CreateEvent 200 TPS 会先于其他配额触顶。
- SigV4 调用不传播终端用户身份；需要按用户隔离下游权限时改为 Cognito JWT 入站（`authorizerConfiguration`）。

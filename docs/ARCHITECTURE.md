# 架构（纯 AgentCore harness 方案）

```
 调用方(SDK/CLI, IAM)                         ┌───────────────────────── us-east-1 ──────────────────────────┐
   │ submit()                                │ AgentCore Gateway  nxdev-deepresearch-gw (MCP, AWS_IAM)        │
   ▼                                         │   └ Connector target web-search v1.2.0 (domain exclude)        │
 DynamoDB nxdev-sessions ◀──┐                │   └ 自定义限流: 每 IAM principal 600 rpm                         │
   DR#TASK#<id>/meta,evt#   │                └──────────────────────────────▲────────────────────────────────┘
 SQS nxdev-deepresearch-intake (+DLQ)                                       │ agentcore_gateway tool (SigV4)
   │ receive                                                                │
   ▼                                         ┌───────────────────────── us-west-2 (nx stack) ───────────────┐
 Dispatcher (本地/容器/EKS nxdev)             │ AgentCore harness nxdev_deepresearch_harness                    │
   AdmissionController:                      │   model: global.anthropic.claude-sonnet-4-6                     │
   ① inflight ≤ C (DynamoDB 原子计数)          │   skills: s3://<bucket>/skills/deep-research-harness/            │
   ② 新会话 ≤ 20/s                            │   memory: BYO nxdev_memory (USER_PREFERENCE + SEMANTIC)          │
   ③ 搜索预算 ≤ 8 TPS                          │   network: VPC vpc-07558f58d3d8b5976, 3 私有子网(NAT), DataServicesSG│
   ④ 429 → 退避 + C×0.8                       │   每任务 1 个 microVM 会话 (idle 30 min, max 8 h)                │
   │ InvokeHarness (stream)                   │   事件流: reasoning / toolUse / toolResult / text               │
   │ InvokeAgentRuntimeCommand (cat report)   └───────────────────────────────────────────────────────────────┘
   ▼
 S3 <bucket>/reports/YYYY/MM/DD/<task>/report.md + sources.jsonl + evidence.jsonl
```

## 语义合并层（新）
提交路径（Lambda 工具 / live 服务器 / SDK）先经 `DedupeClient` → VPC Lambda `nxdev-deepresearch-dedupe`（或直连）→ Valkey：
Haiku 规范化 → 时间硬门 → 精确键 → cohere.embed-v4 余弦 → 灰区 Haiku 判定 → 命中则登记订阅返回同一 task_id，未命中则 5 s 锁内创建任务并写缓存（TTL 12 h）。
任务与订阅是一对多；取消默认只撤销订阅，后端仅在无订阅者时停止。

## 复用的 nx 栈资源
| 资源 | 用途 |
|---|---|
| VPC `vpc-07558f58d3d8b5976`，私有子网 A/B/C（带 NAT），`DataServicesSG` | harness VPC 模式，出网经 NAT，S3 经 VPC Endpoint |
| DynamoDB `nxdev-sessions`（pk/sk，TTL=ttl） | 任务状态、进度事件、并发计数 |
| AgentCore Memory `nxdev_memory-MAXuY22e8R` | harness 长期记忆（按 actorId 隔离） |
| Cognito `us-west-2_uXkPGK5dS` | 预留：如需 JWT 入站认证可切换 `authorizerConfiguration` |
| ElastiCache Serverless Valkey `nxdev-valkey` | 语义合并缓存（规范键、向量、时间桶、合并锁），TTL 12 h |
| Aurora / EFS | 未使用；harness 在同一 VPC 与 SG 内，可按需挂 EFS access point 或直连 Aurora |

## 新建资源（`scripts/deploy.py`，可 `scripts/teardown.py` 删除）
Gateway + Web Search target（us-east-1）、2 个 IAM 角色、S3 桶、SQS 队列 + DLQ、harness。

## 入口与暴露面
- 研究任务提交：AWS API（SQS/DynamoDB/S3，SigV4），无公网 IP、无 API Gateway。
- 唯一 MCP endpoint：Gateway `nxdev-deepresearch-gw`（SigV4，MCP 2025-11-25 + 2026-07-28）。harness 与外部 MCP 客户端共用；
  外部客户端使用最小权限 IAM 用户的静态访问密钥，Claude Code 经 `mcp-proxy-for-aws-cli` 接入。步骤见 docs/DEPLOYMENT.md 8.2。
- 研究流程以 MCP 工具暴露：默认走 Gateway MCP-server 目标 `live`（AgentCore Runtime 上的流式 MCP 服务器：research/watch/cancel，SSE 进度通知，可打断）；
  Lambda 目标 `research`（submit/run/status/report/cancel）作为回退与回调路径。两者共用 SQS + DynamoDB + S3，报告 Markdown 落 S3。

## 任务生命周期
1. `DeepResearchAPI.submit()` → DynamoDB 写 `queued` → SQS 消息。
2. Dispatcher 取消息 → 四道准入门 → `claim_task`（queued→running，幂等）→ 线程池执行。
3. Worker 构造任务提示（含 depth 画像）→ `InvokeHarness` 流式 → 每 5 s 把 reasoning/tool_use/tool_result 事件批量写入 DynamoDB。
4. 结束后解析 `DR_STATUS` → `InvokeAgentRuntimeCommand` 读取 `/tmp/research/<task>/report.md` 等文件 → 上传 S3 → `completed`。
5. 429/限流 → `ThrottledError` → 任务置 `retry`、消息 30 s 后可见、调度器收缩 C。

## 推理步骤展示
DynamoDB 事件（`kind`: reasoning / tool_use / tool_result）即"推理步骤"，`DeepResearchAPI.follow()` 轮询输出；
harness 事件流原生包含 `reasoningContent` 与工具调用块，无需额外代码。

## 与方案 B（Strands Graph）的关系
配额、Gateway、队列、DynamoDB 结构完全相同；若未来需要多 Agent 图编排，只需把 `worker.py` 里的 `HarnessClient` 换成
Runtime A2A 客户端，其他组件不变。

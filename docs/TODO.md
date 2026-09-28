# TODO

## 中国区部署（方案见 docs/CHINA_DEPLOYMENT.md）
- [x] 调研：中国区 AgentCore 提供 Runtime / Gateway / Identity / Browser / Code Interpreter；不提供 Bedrock 模型 API 和 harness；SQS 等周边服务均可用
- [ ] P0 中国区账号：确认 Runtime（MCP、VPC、代码包部署）、Gateway（2026-07-28、流式）、Identity API Key 提供方及配额；法务评估；选定方案 A 或 C
- [ ] P1 PoC：`research_agent/`（Strands + LiteLLMModel，Runtime）与 `websearch_relay/`（Runtime MCP）经公网跨境跑通 quick
- [ ] P2 跨境 DX + 美国侧 Gateway VPCE + 中国区私有 DNS + IAM Roles Anywhere
- [ ] P3 `deploy/cloudformation/deepresearch-cn.yaml`（区分分区，中国区 ECR）
- [ ] P4 全 API 矩阵（cn 目标）+ 故障注入 + 语义合并阈值重标定

## 2026-09-28 去除 region / 账号绑定 + CloudFormation 一键部署
- [x] P1 配置层：`settings.yaml` 通用化（不含账号、区域、nx 名称），名称用 `{project}/{region}/{account_id}` 模板；覆盖顺序为 `settings.yaml` < `local.yaml`（或 `DR_LOCAL_FILE`）< `DR_*` 环境变量；运行时状态读 `DR_STATE_*` 或 `DR_DEPLOY_STATE`；nxdev 的值移到 `config/local.yaml`（已核对与原值一致）
- [x] P2 代码：去掉 IAM、Secrets、Lambda、脚本、测试、客户端 profile 中硬编码的区域、账号和名称；Valkey 无密码模式；webhook 密钥按 Secret ID 读取；嵌入模型默认改为 `global.cohere.embed-v4:0`（三个 Web Search 区域实测可用）
- [x] P3 镜像：`deploy/build_artifacts.py --push` 产出 `aws300/deploy:deepresearch-artifacts-<ver>`（`FROM scratch` 单层制品）和 `aws300/deploy:deepresearch-app-<ver>`（arm64，依赖预先打包，Dockerfile 不含 RUN）；已推送 1.0.0、1.0.1、latest；Runtime 入口 `deploy/runtime/live_main.py`
- [x] P4 `deploy/cloudformation/deepresearch.yaml`：VPC 新建或复用、S3/SQS/SNS/DynamoDB/Secrets/Valkey、制品复制自定义资源、Lambda、原生 AgentCore Gateway/Target/RateLimit/Runtime/Harness、`HarnessThinking` 自定义资源、ECS Fargate dispatcher、MCP 客户端策略与可选用户
- [x] P5 cfn-lint 通过；us-east-1 全新部署、升级 1.0.0→1.0.1、升级失败回滚均已实测；新增 `scripts/use_stack.py`；在 CFN 栈上跑全 API 矩阵；新增 C7b（SNS 完成通知），C7 在远端 dispatcher 下显示为跳过
- [x] P6 文档：`docs/CLOUDFORMATION.md`，README 更新
- [ ] 为 webhook 回调提供 VPC 内可达的测试接收端（远端 dispatcher 下 C7 的 HMAC 签名校验目前只在 nxdev 本机路径上覆盖）
- [ ] 将制品同时发布到 public.ecr.aws（Runtime 也可改用容器镜像，并避开 Docker Hub 匿名拉取限流）

## 2026-09-28 全 API 端到端矩阵（`scripts/e2e_matrix.py`）
- [x] 12 个场景覆盖异步/缓存/同步/流式/回调/取消 × quick/standard/deep：最终轮 12/12，pytest 22/22
- [x] 修复：技能预装 curl 经 NAT 偶发挂起（改 base64 内联 + 墙钟超时 + 换会话重试 + 看门狗）；boto3 客户端并发创建加锁；判定器使用原始提示词
- [x] README 增补 mermaid 架构图、生命周期时序图、合并判定流程图、取消状态图
- [x] 修复：准入拒绝导致消息进 DLQ（只在有空槽时收消息，DLQ 自动回捞，maxReceiveCount=50）
- [x] 修复：长同步调用被约 350 s 空闲超时断开（run_research 限时 300 s + research_live 心跳）
- [x] 修复：空闲 SSE 流断开（20 s 心跳）；客户端收到最终结果即停止读取

## 语义合并（同义提示词 → 同一 task_id）与独立 WebSearch MCP
- [x] T1 `scripts/validate_embed.py`：验证"规范化 + cohere.embed-v4"合并策略与阈值（正/负样例、中英文、相对日期）
- [x] T2 `src/deepresearch/dedupe.py`：规范化（Haiku）→ 向量（embed-v4）→ Valkey 缓存（TTL 12h 可配）→ 5 s 合并窗口锁；失败时 fail-open
- [x] T3 VPC Lambda `nxdev-deepresearch-dedupe`（us-west-2，nx 私有子网 + DataServicesSG）+ `scripts/dedupe_cache.py`（stats/lookup/clear）
- [x] T4 订阅模型：同一 task 多个前端订阅；取消只撤销自己的回调/流，后端不停止；默认 MCP 动作改为异步（`research` 返回 task_id，`watch_research` 看流）
- [x] T5 独立 WebSearch MCP：`scripts/websearch_mcp.py`（stdio）+ `scripts/websearch.py`（CLI），不依赖 nx-deep-research
- [x] T6 端到端测试（合并命中、合并任务取消语义、cache=false、WebSearch MCP）与文档（DEPLOYMENT/ARCHITECTURE/DEBUGGING/QUOTAS）
- [x] T7 deep 档 `live_research.py --depth deep --cache false` 端到端：约 10 分钟完成，30 次 WebSearch、74 条流事件，报告 17 KB 落盘并通过校验
- [x] 自动重挂：dispatcher 重启恢复场景下 C6 deep 经多个流窗口重挂完成（1573 s）

- [x] **`claude -p` 端到端测试脚本**：`scripts/e2e_claude_print.sh [scenario] -q "<自定义提示词>" -d quick -o ./reports/x.md -t 1500`
      按用户真实路径跑 5 个场景：websearch / live（默认实时流）/ sync（run_research）/ callback / cancel；live 与 sync 场景会核验本地 .md 与
      S3 副本（`## Bibliography`、`[1]` 引用、长度、`validate_report.py`）。websearch 与 live 已实跑通过。
- [ ] 在交互式 Claude Code 会话中确认 `notifications/progress` 的渲染方式（是否逐条显示步骤），并记录到 docs/DEPLOYMENT.md 8.2。
- [ ] 用 `-d standard` 跑一次 sync/live（需 `-t 2400`），确认 13.5 分钟流窗口结束后 `watch_research` 续看路径。
- [ ] dispatcher 部署到 EKS `nxdev`（`deploy/k8s/dispatcher.yaml`），当前为本机进程。
- [ ] Runtime V2（`platformVersion=V2`）用于 live MCP server 与 harness 底层 Runtime，缩短冷启动。
- [x] harness 已开启 extended thinking（`thinking_budget_tokens: 3000`），实时流中可见 💭 推理与 💬 叙述（首条约 20 秒到达，之后 2 秒粒度）。
- [ ] 观察 extended thinking 带来的 token/TPM 增量，必要时下调 budget 或改为仅 standard/deep 开启。
- [ ] 阶段检查点（inline_function `ask_user` + MRTR）实现"打断后改方向继续"。
- [ ] 配额工单跟踪：`python scripts/request_quotas.py --target 10000 --apply` 与 Runtime 活跃会话 Support Case。

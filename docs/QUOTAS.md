# 配额设置（支撑 10000 并发研究任务）

> 数据来源：AgentCore 配额页、Bedrock 配额页、本账号 Service Quotas 实测（2026-09-21）。
> 计算模型见 `src/deepresearch/capacity.py`，一键生成申请单：`python scripts/request_quotas.py --target 10000 --shards N [--apply]`。

## 1. 容量公式

```
C = 0.8 × min( S_runtime,  Q_search / r_search,  Q_tpm / r_tpm,  Q_gateway / r_gateway,  Q_conn )
```

参考画像（投产前用 Observability 真实轨迹校准）：每任务 20 分钟、30 次 WebSearch、40 次模型调用、每次 8000 输入 + 1500 输出 token。
Claude 输出 token 对 TPM 的燃烧倍率：4.7 及以下 5×，Sonnet 5 10×，4.8 系列 15×；prompt cache 命中的输入 token 不计入 TPM。
默认模型 `global.anthropic.claude-sonnet-4-6`（5× 燃烧、6M TPM）优于 Sonnet 5（10× 燃烧）。

| 场景 | 单分片 C |
|---|---|
| 默认配额 | ≈ 100–150 |
| 默认配额 + 30% 缓存命中 | ≈ 200–300 |
| 下表申请全部批准，单分片 | ≈ 10000 |

## 2. 需要申请的配额（单分片承载 10000）

"区域"列：Gateway 类在 **us-east-1**（Web Search 所在区），Runtime/Memory/模型类在 harness 所在区（默认 **us-west-2**）。

| # | 配额 | Quota Code | 区域 | 默认 | 申请值 | 依据 |
|---|---|---|---|---|---|---|
| 1 | Runtime 活跃会话（Active session workloads per account） | 不在 Service Quotas，需开 Support Case | us-west-2 | 5000 | **12500** | 10000 / 0.8 余量 |
| 2 | Rate of new Runtime session creation | L-8EE2AEA2 | us-west-2 | 25 TPS | **100 TPS** | 0→10000 爬坡 ≈ 100 s |
| 3 | Rate of Runtime data plane APIs | L-46ED137C | us-west-2 | 1000 TPS | **2500 TPS** | InvokeHarness + Command 读取产物 |
| 4 | Rate of Web Search Tool queries | L-84A99A88 | us-east-1 | 10 TPS | **300 TPS** | 10000 × 30 次 / 20 分钟 = 250 TPS |
| 5 | Rate of tool-call/tool-list requests（账号级） | L-A0D48779 | us-east-1 | 200 TPS | **600 TPS** | 搜索 + tools/list |
| 6 | Rate of tool-call/tool-list requests per gateway | L-8CAB3FF3 | us-east-1 | 200 TPS | **600 TPS** | 同上 |
| 7 | Tool-call/tool-list concurrent connections | L-6234C8FD | us-east-1 | 5000 | **12500** | 每活跃会话一条连接 |
| 8 | Global CRIS TPM – Claude Sonnet 4.6 | L-7BEE40FB | us-west-2 | 6,000,000 | **≈ 200,000,000（单分片）/ 50,000,000（4 分片）** | 参考画像下 10000 并发共需 ≈ 330M TPM（10000 × 2 次/分 × (8000×0.7 + 1500×5)），按 Sonnet/Haiku 6:4 分流 |
| 9 | Global CRIS TPM – Claude Haiku 4.5 | L-9A11C666 | us-west-2 | 5,000,000 | **≈ 200,000,000（单分片）/ 50,000,000（4 分片）** | 子步骤（改写、抽取、去重）分流到 Haiku |
| 10 | Global CRIS RPM – 同上两模型 | 见配额页 | us-west-2 | 10000 | **30000** | 10000 × 2 次/分 |
| 11 | Rate of CreateEvent requests（Memory） | L-59AF2B24 | us-west-2 | 200 TPS | **600 TPS** | harness 每轮写事件；或设 `memory_mode: disabled` |
| 12 | Tokens per minute for long-term memory extraction | L-E3D6644C | us-west-2 | 150,000 | **1,000,000** | 托管/BYO 长期记忆抽取 |
| 12a | Global CRIS TPM/RPM – Cohere Embed V4（语义合并） | 见配额页 | us-west-2 | 300,000 TPM / 2,000 RPM | 每次提交 1 次嵌入（≈30 token）+ 1 次 Haiku 规范化（≈400 token）；10000 并发提交/分钟 ≈ 300k TPM，需提升 3–5 倍 | 提交路径的额外模型调用 |
| 12b | Global CRIS TPM – Claude Haiku 4.5（规范化 + 灰区判定） | L-9A11C666 | us-west-2 | 5,000,000 | 与第 9 行合并考虑 | 每次提交约 400–800 token |
| 13 | Concurrent code interpreter sessions（可选） | L-CAF6F552 | us-west-2 | 1000 | 按需 | 仅图表任务 |

**模型 TPM 是最难批到的项，也是唯一需要百倍量级提升的项。** 单分片 10000 并发需要约 330M TPM；若单区域批不到，用多分片：每个"账号×区域"分片跑 2500 并发，4 个分片（us-east-1、us-west-2 各一账号 + 第二账号）叠加；`scripts/request_quotas.py --shards 4` 会按分片输出申请值。

## 3. 一键申请

```bash
# 先看计划
python scripts/request_quotas.py --target 10000 --shards 1
# 提交 Service Quotas 中存在的项（跳过已满足的）
python scripts/request_quotas.py --target 10000 --shards 1 --apply
# 查看状态
aws service-quotas list-requested-service-quota-change-history --service-code bedrock-agentcore --region us-east-1 \
  --query "RequestedQuotas[].{q:QuotaName,v:DesiredValue,s:Status}" --output table
```

Support Case 模板（活跃会话数不在 Service Quotas 中）：

```
Service: Amazon Bedrock AgentCore  |  Category: Service limit increase  |  Region: us-west-2
Limit: AgentCore Runtime – Active session workloads per account
Current: 5000   Requested: 12500
Use case: nxdev deep-research harness; each research task = 1 microVM session (20–40 min, HealthyBusy async);
peak 10000 concurrent tasks; admission control via SQS + DynamoDB token buckets (max_inflight),
session creation capped at 100/s; Web Search via Gateway <gateway-id> in us-east-1.
```

## 4. 调度器参数与配额的对应关系（`config/settings.yaml`）

| 参数 | 含义 | 必须小于 |
|---|---|---|
| `dispatcher.max_inflight` | 执行中任务上限 C | 活跃会话配额 × 0.8，且 ≤ 公式计算值 |
| `dispatcher.session_create_per_sec` | 新会话令牌桶 | L-8EE2AEA2 |
| `dispatcher.websearch_tps_budget` | 搜索预算（按任务生命周期折算准入速率） | L-84A99A88 |
| `gateway.rate_limit_per_caller_rpm` | Gateway 自定义限流（先于服务配额判定） | 不影响服务配额，仅公平分配 |

批准后按下表放大 `max_inflight`：默认配额 100 → 中等批准（Web Search 100 TPS、TPM 5×）3000 → 全部批准 10000（单分片）。
调度器在收到 `ThrottlingException` 时自动把 `max_inflight` 乘 0.8 并退避 30 s。

## 5. 不需要申请的项
Gateway 数量（1000/区）、Memory 资源数（150/区）、每会话 2 vCPU/8 GB（不可调，研究任务 I/O 密集足够）、
单会话 8 小时（quick/standard/deep 均 < 1 小时）、Step Functions（本方案不使用）。

## 6. 实测校准（2026-09-21，quick 档，Sonnet 4.6，VPC 模式）

| 指标 | 实测 | 参考画像假设 |
|---|---|---|
| 任务时长 | 3 分 31 秒 / 5 分 16 秒（两次运行，含排队与产物回传） | 20 分钟（standard/deep） |
| WebSearch 次数 | 10 | 30 |
| 工具调用总数 | 20 | — |
| 输入 token 总量 | 480,037 / 731,628（≈ 24k–35k / 次模型调用，含工具定义与累积搜索结果） | 8,000 / 次 |
| 输出 token 总量 | 9,252 / 9,670 | 1,500 / 次 × 40 |

结论：单次调用输入 token 比假设高约 3 倍，模型 TPM 约束比第 1 节估算更紧；quick 档任务短、搜索少，会话与搜索约束相对宽松。
建议用 `dispatcher.max_inflight` 从 100 起步，观察 CloudWatch 中 Bedrock 的 `ThrottledCount` 后再放大；
并把 `harness.truncation` 保持为 `summarization`、在任务提示中要求"摘要而非粘贴搜索结果"以压低输入 token。
重新计算：`python scripts/request_quotas.py --target 10000 --minutes 4 --searches 10 --calls 20 --cache 0.3` 可得 quick 档口径下的配额需求。

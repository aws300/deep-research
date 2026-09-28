# 2026 年 Amazon Bedrock AgentCore Harness 的能力边界与最佳适用场景

> 研究日期：2026年9月22日 | 数据时效截至：2026年9月

---

## Executive Summary

Amazon Bedrock AgentCore 于 2025 年 10 月正式全面可用（GA），2026 年经历了一轮显著的能力扩张，已发展为涵盖 12 个模块化组件的生产级 AI Agent 基础设施平台 [1][2]。其中，AgentCore Harness（Harness）于 2026 年 6 月 17 日正式 GA，标志着该平台从"代码优先的 Agent 部署服务"迈向"配置驱动的托管 Agent 执行环境"的重要里程碑 [3][4]。

Harness 的核心定位是：如果大语言模型（LLM）是 Agent 的大脑，那么 Harness 就是身体——它负责运行推理-工具调用循环、管理上下文窗口、跨会话持久化状态、隔离每个会话的执行环境，并内置可观测性 [3]。开发者只需通过两次 API 调用（`CreateHarness` 和 `InvokeHarness`）或在控制台点击几下，便可在数分钟内运行一个生产级 Agent，而无需编写编排代码或构建容器镜像 [4]。

在技术能力方面，AgentCore Harness 2026 年的核心边界包括：微虚拟机（microVM）会话最长 8 小时（Runtime Instances 可延伸至 14 天）[5][6]；2026 年 9 月发布的 V2 Runtime 将 P75 冷启动时间从最高 30 秒压缩至约 2 秒 [7]；默认并发上限为美东/美西各 5,000 个活跃会话 [8]；支持 VPC 隔离、IAM 精细授权、Cedar 策略引擎和 Bedrock Guardrails 集成 [9][10]。

从最佳适用场景来看，AgentCore Harness 在三类场景中表现突出：一是企业级多轮对话 Agent（客服、销售助手），代表案例包括 Swisscom 和 Cox Automotive [11][12]；二是 AI 驱动的自动化工作流（代码审查、数据库智能、商业智能），OPLOG 案例显示销售周期缩短 35% [13]；三是多租户科学计算与代码执行（Benchling 案例，每日超过 600 次安全代码执行会话）[14]。其能力边界的主要限制在于：深度 AWS 生态绑定、缺乏多云迁移路径，以及高并发实时推理场景下的延迟权衡 [15]。

本报告基于 25 个来源、10 次 WebSearch 检索，系统梳理 AgentCore Harness 的架构边界、最佳适用场景与关键局限，为企业架构师和 ML 工程师提供决策依据。

---

## Introduction

### 背景：从原型到生产的基础设施鸿沟

AI Agent 开发领域长期面临"原型到生产"的鸿沟。开发者可以快速构建能运行的 Agent，但要将其投入生产，需要独立解决容器化与调度、会话隔离、跨轮次状态持久化、工具身份认证、可观测性追踪、弹性扩缩容等一系列基础设施问题。这些工作与 Agent 的核心推理逻辑毫不相关，却往往消耗团队数月时间 [16]。

Amazon Bedrock AgentCore 正是为填补这一鸿沟而生。AWS 将其定位为"构建、连接和优化 Agent 的规模化平台"，核心设计哲学是**模块化**——12 个独立的托管组件既可单独使用，也可组合使用，而无需绑定到特定 Agent 框架或基础模型 [1][2]。

### AgentCore Harness 的定义与定位

AgentCore 平台中的 Harness（2026 年 4 月预览，6 月 GA）是该平台中最高层的抽象 [3]。它在 Runtime 之上再增加一层托管能力：用户不再需要编写 Agent 编排代码，仅需通过配置声明 Agent 的行为（使用哪个模型、调用哪些工具、遵循哪些指令），AgentCore 便会自动组装并运行完整的 Agent 循环 [4][17]。

与平台中其他组件的关系可理解为：Runtime 是托管的计算层（相当于托管的容器服务），Memory 是持久化状态层，Gateway 是工具连接层，而 **Harness 则是在这三层之上的托管编排层**，是 AgentCore 平台提供的最完整的"一键运行 Agent"能力 [5][3]。

---

## Main Analysis

### Finding 1：AgentCore Harness 的技术能力边界

#### 1.1 计算与生命周期边界

AgentCore 平台在计算层面提供两种互补的运行时模式，共同构成了 Harness 的能力边界：

**微虚拟机 Runtime（默认模式）** 是 Harness 的基础执行环境。每个会话在独立的微虚拟机中运行，拥有隔离的 CPU、内存和文件系统资源 [9]。2026 年 9 月发布的 V2 Runtime 引入了弹性内存管理机制，会话结束时立即回收内存，而非保留峰值分配；冷启动通过环境快照技术实现，无论镜像大小和并发数量如何，P75 冷启动时间稳定在 1.9–2.0 秒，相较 V1 的 5.4–30 秒大幅改善 [7]。单个 microVM 会话最长持续 8 小时，支持 100 MB 的请求/响应载荷，按实际使用的 vCPU 时和 GB 小时计费 [5][8]。

**Runtime Instances（2026 年 8 月 GA）** 为需要超越 microVM 边界的工作负载提供了解决方案 [6]。该模式基于 AWS 托管的 EC2 实例，支持：会话持续最长 14 天；GPU 加速（适用于需要本地模型推理或计算密集型任务）；多个协作 Agent 共享同一宿主机；会话暂停/恢复以节省成本。两种模式使用相同的 AgentCore API、身份控制和可观测性接口，可在同一平台混合使用——例如，用 microVM 运行低延迟交互式 Agent，用 Runtime Instances 运行长时批处理 Agent [6]。

**并发与配额边界**（截至 2026 年 7 月）：美东/美西默认上限为 5,000 个活跃并发会话，其他区域为 2,500 个，所有区域支持每秒 200 次 Agent 交互和每秒 25 个新会话 [8]。平台支持的 AWS 区域为 5 个（us-east-1、us-east-2、us-west-2、eu-west-1、ap-northeast-1），截至 2026 年 9 月该数字仍在扩展 [7]。

#### 1.2 功能组件边界

截至 2026 年 6 月，AgentCore 平台已扩展至 12 个模块化组件，Harness 在其中处于最高层的整合位置 [1][2]：

- **Runtime**：无服务器容器执行（microVM 或 Instances），支持任意框架（LangGraph、Strands、CrewAI、AutoGen）和语言（Python、TypeScript、Java、.NET）
- **Harness**：配置驱动的托管编排层，内置文件系统、Shell、Memory、Web 浏览和技能（Skills）访问
- **Memory**：跨会话持久化状态，支持原始事件存储（会话历史）和语义处理策略
- **Gateway**：将 API、Lambda 函数、REST 接口转换为 MCP 兼容工具，支持 OAuth2 身份认证
- **Identity**：OAuth2/JWT 令牌管理，支持多 IDP 认证集成
- **Code Interpreter**：隔离的 Python 代码沙箱，支持 VPC 模式
- **Browser**：托管的浏览器自动化工具
- **Observability**：基于 OpenTelemetry 的追踪，集成 CloudWatch 和 X-Ray
- **Policy**：基于 Cedar 语言的细粒度访问控制
- **Evaluations**：生产监控、A/B 测试、推荐优化（2026 年 3 月 GA）
- **Payments**：Agent 内支付能力（预览）
- **Registry**：Agent 注册与发现

从 Harness 的视角来看，上述组件中 Runtime、Memory、Gateway、Identity、Code Interpreter 和 Observability 是 Harness 自动集成的核心能力，而 Policy、Evaluations 则需开发者显式配置 [3][4]。当 Harness 的配置驱动方式不足时，开发者可通过 `agentcore export harness` 命令将 Harness 导出为 Strands 代码，在保持相同计算路径和可观测性的前提下切换至代码优先模式 [4]。

#### 1.3 安全与合规边界

AgentCore 的安全模型建立在多层防御体系之上，这也是其区别于轻量级竞争方案的核心优势：

在**计算隔离**层面，每个会话在独立的微虚拟机中运行，拥有独立的 CPU、内存和文件系统；会话结束后，整个 microVM 终止并清除内存 [9]。在**网络隔离**层面，支持 4 种网络连接模式，从公共端点到完全隔离的私有 VPC，可通过 IAM 条件键（`bedrock-agentcore:subnets` 和 `bedrock-agentcore:securityGroups`）强制要求所有 Runtime 必须部署在特定 VPC 中 [10][18]。在**身份与授权**层面，Gateway 使用 Cedar 策略语言进行细粒度工具调用授权，与 Bedrock Guardrails 集成可在每次 Agent 操作时评估提示注入、有害内容和敏感数据暴露 [10]。

Benchling 的生产案例验证了该安全体系的实效：在生命科学多租户代码执行场景中，通过账户级隔离 + VPC 模式 Code Interpreter + Route 53 DNS 防火墙 + VPC 端点策略的组合，在每周超过 250 个租户、每日超过 600 次代码执行会话的规模下，实现了零安全事件 [14]。

---

### Finding 2：最佳适用场景

基于 2026 年的官方案例和开发者实践，AgentCore Harness 在以下场景中展现出最高的适用性：

#### 2.1 企业级多轮交互 Agent（对话式客服与销售助手）

这是 AgentCore 最成熟的应用场景，多个具有规模效应的企业部署案例在此场景中聚集。其核心优势在于：会话隔离（数千名用户可并发交互而不产生数据污染）、跨轮次记忆持久化、与企业 VPC 内部 API 的安全集成 [11]。

瑞士电信 Swisscom 将 AgentCore Runtime 用于 B2C 客户服务场景，为其现有 AI 聊天机器人系统 SAM 构建了两个专用 Agent（个性化销售推荐和技术故障自助服务）。关键需求是与现有 Amazon EKS 系统的互操作性，以及符合瑞士数据保护标准的 VPC 隔离部署 [11]。Cox Automotive 在汽车行业部署了 17 个生产 PoC、7 个行业转型解决方案，同样基于 AgentCore 与 Claude 模型的组合 [12]。

Harness 在此场景中的核心价值是：托管的 Memory 组件自动处理对话历史存储和语义处理策略，开发者无需自行实现"金鱼 Agent"（每轮忘记上下文）问题的解决方案；内置的身份认证集成（支持 Amazon Cognito、多 IDP）简化了企业级用户认证 [11][3]。

#### 2.2 AI 驱动的自动化工作流（代码分析、数据库运维、商业智能）

当工作流需要调用多个工具、跨系统协调、产出结构化结果时，AgentCore 的 Gateway 和 Runtime 组合发挥出显著优势。典型模式是：通过 Gateway 将内部 API、数据库查询函数、Lambda 工具封装为 MCP 兼容接口，通过 Strands 或 LangGraph 编写 Agent 逻辑，通过 Runtime 管理部署和扩缩容。

OPLOG 商业智能案例最具代表性：三个 AI Agent 部署在 AgentCore 上，分别处理销售管道管理、数据质量执法和潜在客户研究，最终实现销售周期缩短 35%、CRM 数据完整性提升 91%、人工研究时间减少 98% [13]。AWS 内部的 SMGS 业务管理案例展示了多 Agent 协调的典型架构：一个监督者 Agent 通过 AgentCore 协调六个专业工具 Agent，实现自然语言业务查询 [19]。

在代码与开发生命周期场景中，AgentCore 的隔离沙箱和持久会话特别适合：SQL 模式到 ER 图的自动生成、代码安全扫描、自动化测试生成，以及持续集成流水线中的代码审查 Agent [20]。

#### 2.3 长时自主 Agent（8 小时到 14 天的无人监督任务）

2026 年 8 月推出的 Runtime Instances 为 AgentCore 打开了一个重要的新适用域：需要持续运行数小时甚至数天的自主工作负载 [6]。典型场景包括：大规模代码库现代化（AWS Transform 使用 AgentCore 运行多 Agent 代码转换工作流）[21]；大批量文档处理与分析；需要 GPU 的本地模型推理工作流；多 Agent 协作系统中的长时工作节点。

AWS 自己的编码 Agent 托管博文（"It's safe to close your laptop now"）概括了这一场景的价值主张：每个 microVM 配备了持久的 `/mnt/workspace`，可在暂停/恢复间保持文件；会话可随时通过 `StopRuntimeSession` 终止，也可在下次调用同一会话 ID 时从相同文件状态恢复 [22]。对于 Runtime Instances，这一能力延伸至 14 天的持续会话 [6]。

#### 2.4 无代码/低代码 Agent 快速原型

AgentCore Harness GA 之后，该平台实现了真正的快速原型路径：两次 API 调用或在控制台几次点击，即可运行一个具有文件系统访问、Web 浏览、跨会话记忆和工具调用能力的完整 Agent [4]。Harness 还支持每次调用时动态切换模型（只需传入不同的 `model` 参数），历史上下文自动适配，无需额外代码 [23]。该能力被整合到 n8n 可视化工作流编辑器的官方节点中 [24]，以及 .NET Aspire 部署工作流中 [25]，进一步扩展了触达开发者群体的广度。

---

### Finding 3：能力局限与不适合的场景

理解 AgentCore Harness 的局限与适用场景同等重要：

#### 3.1 深度 AWS 生态绑定

AgentCore 是 AWS 生态系统的深度成员，其 IAM 权限模型、VPC 集成、存储绑定（S3、EBS）和计费模式均与 AWS 原生服务深度耦合。多位独立分析师和开发者明确指出：基于 AgentCore 构建的 Agent 无法在不进行大量重构的情况下迁移到 Azure 或 GCP [15][26]。对于有多云战略要求或需要保持云供应商中立的组织，这是一个结构性限制。

#### 3.2 代码优先的工具链，缺乏可视化构建路径

尽管 Harness 降低了编排代码的门槛，但 AgentCore 整体仍是代码优先的平台——没有可视化的无代码 Agent 构建器。业务领域专家若缺乏 Python 经验，无法直接参与 Agent 的创建和修改，仍需工程师介入 [15]。Azure AI Foundry 的 Prompt Flow 和 Google Vertex AI 的可视化构建工具在这方面提供了更低的技术门槛。

#### 3.3 超低延迟实时推理

AgentCore Runtime 的会话模型（尤其是冷启动路径）不适合需要亚秒级响应的场景。V2 Runtime 将 P75 冷启动压缩至约 2 秒 [7]，但对于某些实时 SLA 场景仍不足够。早期评测（基于预览版）记录了高达 12+ 秒的冷启动时间 [27]；V2 已大幅改善，但热启动路径（已初始化的环境在 100 毫秒以内）依赖于保留预热计算资源，这引入了额外的成本权衡 [7]。

#### 3.4 超高并发场景的区域覆盖限制

目前 V2 Runtime 仅在 5 个区域可用（us-east-1、us-east-2、us-west-2、eu-west-1、ap-northeast-1）[7]，而早期版本支持 9 个区域。需要全球多区域部署或其他地理位置覆盖的组织需要关注此限制。

---

## Synthesis

AgentCore Harness 在 2026 年代表了 AWS 在 Agent 基础设施领域的最完整且最成熟的产品化表达。其核心竞争力不在于某项单一功能，而在于**将企业生产部署所需的所有基础设施关切（计算隔离、身份认证、状态管理、工具连接、可观测性、安全合规）整合到一个统一的平台和计费模型中** [1][2]。

从生态系统角度看，2026 年 SDK 生态已达到显著覆盖广度：原生支持 Python、TypeScript、Java（Spring AI SDK）、.NET；框架兼容 LangGraph、Strands、CrewAI、AutoGen；外部集成覆盖 n8n、AWS Aspire，以及通过 MCP 协议接入的第三方服务 [25][24]。该平台在发布后五个月内突破 200 万 SDK 下载量 [26]，表明开发者采用速度超出同期竞品。

与竞争平台的核心区分维度：相比 Azure AI Foundry，AgentCore 在框架无关性和安全模型深度（特别是 Cedar 策略引擎）上更具优势，但缺乏 M365 生态的深度整合；相比 Google Vertex AI Agent Engine，AgentCore 的工具生态（MCP/A2A 协议原生支持）和 AWS 服务集成深度是差异化优势，但计算成本（$0.0895/vCPU-h）略高于 Vertex AI ($0.0864/vCPU-h) [26][19]。

然而，**AgentCore 的最优适用条件**是：组织已深度投入 AWS 生态（S3、Lambda、IAM、VPC）；需要同时满足企业合规要求（VPC 隔离、审计追踪、细粒度授权）；Agent 工作负载的复杂性已超过 Bedrock Agents（托管声明式方案）的能力边界；且工程团队具备 Python/TypeScript 开发能力。

---

## Limitations

本研究存在以下局限：

1. **信息时效性**：AgentCore 是快速演进的服务，本报告基于截至 2026 年 9 月 22 日的可公开信息。某些功能（如 Payments、Terraform 支持）在研究时处于预览或即将上线状态，可能在此后变更。
2. **成本分析深度有限**：本报告未对 AgentCore 计费结构进行深度 TCO 分析，实际成本高度依赖工作负载特征（会话长度、并发数、工具调用频率）。
3. **案例偏差**：多数引用的企业案例来自 AWS 官方博客，存在选择性发布偏差。独立的批评性评估来源较少（以 Rackspace、Yopa 为代表），覆盖深度有限。
4. **竞品动态**：Azure AI Foundry 和 Google Vertex AI Agent Engine 均在同期快速演进，截面比较不能代表持续的相对优势。

---

## Recommendations

基于研究发现，针对不同受众提出以下建议（截至 2026 年 9 月）：

**对于初次评估 AgentCore 的团队：**
- 从 Harness 配置驱动路径开始（`agentcore create → agentcore dev → agentcore deploy`），在 10 分钟内验证基本 Agent 行为，再决定是否采用 Runtime 代码优先路径
- 若组织已大量使用 AWS 服务，AgentCore 的 VPC/IAM 集成将带来显著的运维简化价值

**对于规划生产部署的工程团队：**
- 对话式/交互式 Agent 使用 V2 microVM Runtime，充分利用弹性内存管理和快速冷启动
- 批处理/长时工作负载（>8 小时）或需要 GPU 的场景，采用 Runtime Instances，但需建立会话成本监控，避免空闲实例产生意外账单
- 强制在生产环境使用 VPC 模式，通过 IAM 条件键（`bedrock-agentcore:subnets`）在策略层面杜绝 PUBLIC 模式的意外部署

**对于架构师评估多云策略的组织：**
- 若存在多云迁移需求，在 Agent 逻辑层使用 Strands SDK 或 LangGraph（可移植框架），而非直接依赖 AgentCore 专有 API，可降低未来迁移成本
- 当组织数据和服务主要在 AWS 上时，AgentCore 的优势最大；若数据主要在 Azure（M365/SharePoint），Azure AI Foundry 可能是更自然的选择

---

## Bibliography

[1] Accelerate agentic application development with a full-stack starter template for Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/accelerate-agentic-application-development-with-a-full-stack-starter-template-for-amazon-bedrock-agentcore/

[2] AWS Bedrock AgentCore Documentation: The Complete 2026 Mastery Guide. 2026. https://pingax.com/aws-bedrock-agentcore-documentation-the-2025-mastery-guide/

[3] AgentCore harness is now generally available. 2026. https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-agentcore-harness-generally-available

[4] Amazon Bedrock AgentCore harness is now generally available: Go from idea to production-grade agent in minutes. 2026. https://aws.amazon.com/cn/blogs/machine-learning/amazon-bedrock-agentcore-harness-is-now-generally-available-go-from-idea-to-production-grade-agent-in-minutes/

[5] Securely launch and scale your agents and tools on Amazon Bedrock AgentCore Runtime. 2025. https://aws.amazon.com/blogs/machine-learning/securely-launch-and-scale-your-agents-and-tools-on-amazon-bedrock-agentcore-runtime/

[6] Runtime instances: persistent compute for production AI agents on Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/aws/runtime-instances-persistent-compute-for-production-ai-agents-on-amazon-bedrock-agentcore/

[7] The new AgentCore runtime: Elastic, optimized, and consistently fast starts. 2026. https://aws.amazon.com/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/

[8] Amazon Bedrock AgentCore unified observability and 5000-session scaling for production agents. 2026. https://ecorpit.hashnode.dev/amazon-bedrock-agentcore-in-july-2026-unified-observability-and-5000-session-scaling-for-production-

[9] Security best practices for AgentCore Runtime. 2026. https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html

[10] Secure multi-tenant AI agents with Amazon Bedrock AgentCore resource-based policies. 2026. https://aws.amazon.com/blogs/security/secure-multi-tenant-ai-agents-with-amazon-bedrock-agentcore-resource-based-policies/

[11] How Swisscom builds enterprise agentic AI for customer support and sales using Amazon Bedrock AgentCore. 2025. https://aws.amazon.com/blogs/machine-learning/how-swisscom-builds-enterprise-agentic-ai-for-customer-support-and-sales-using-amazon-bedrock-agentcore/

[12] Amazon Bedrock AgentCore and Claude: Transforming business with agentic AI. 2025. https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-and-claude-transforming-business-with-agentic-ai/

[13] Build AI agents for business intelligence with Amazon Bedrock AgentCore (OPLOG). 2026. https://aws.amazon.com/blogs/machine-learning/build-ai-agents-for-business-intelligence-with-amazon-bedrock-agentcore/

[14] How Benchling secured multi-tenant AI agents with Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/how-benchling-secured-multi-tenant-ai-agents-with-amazon-bedrock-agentcore/

[15] Best AI Agent Development Platforms 2026: Startups, Hyperscalers, and Beyond. 2026. https://xpander.ai/blog/best-ai-agent-development-platforms-2026-startups-hyperscalers-and-beyond

[16] Migrate agentic workloads to Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/migrate-agentic-workloads-to-amazon-bedrock-agentcore/

[17] Get to your first working agent in minutes: Announcing new features in Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/get-to-your-first-working-agent-in-minutes-announcing-new-features-in-amazon-bedrock-agentcore/

[18] Network connectivity patterns for agents deployed on Amazon Bedrock AgentCore Runtime. 2026. https://aws.amazon.com/jp/blogs/networking-and-content-delivery/network-connectivity-patterns-for-agents-deployed-on-amazon-bedrock-agentcore-runtime/

[19] How AWS SMGS uses an AI-powered conversational assistant to transform business management with Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/how-aws-smgs-uses-an-ai-powered-conversational-assistant-to-transform-business-management-with-amazon-bedrock-agentcore/

[20] AI-driven development lifecycle using Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/ai-driven-development-lifecycle-using-amazon-bedrock-agentcore/

[21] Agentic application modernization at scale with Strands and Amazon Transform custom. 2026. https://aws.amazon.com/blogs/devops/use-generative-ai-agents-for-application-modernization-at-scale-with-strands-amazon-transform-custom-and-amazon-bedrock-agentcore/

[22] It is safe to close your laptop now: Hosting coding agents on Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/its-safe-to-close-your-laptop-now-hosting-coding-agents-on-amazon-bedrock-agentcore/

[23] Build a serverless image editing agent with Amazon Bedrock AgentCore harness. 2026. https://aws.amazon.com/blogs/machine-learning/build-a-serverless-image-editing-agent-with-amazon-bedrock-agentcore-harness/

[24] Run production AI agents in n8n with Amazon Bedrock AgentCore harness. 2026. https://aws.amazon.com/blogs/machine-learning/run-production-ai-agents-in-n8n-with-amazon-bedrock-agentcore-harness/

[25] Building and Deploying .NET AI Agents with Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/developer/building-and-deploying-net-ai-agents-with-amazon-bedrock-agentcore/

[26] AWS AgentCore vs. Gemini Agent Platform vs. Azure AI Foundry. 2026. https://rickhigh.substack.com/p/aws-agentcore-vs-gemini-agent-platform

[27] Amazon Bedrock AgentCore Review (Rackspace). 2025. https://www.rackspace.com/blog/amazon-bedrock-agentcore-preview-agentic-ai-platform

---

## Methodology

本报告遵循 deep-research-harness 技能工作流，执行步骤如下：

1. **SCOPE**：界定研究问题、受众、时间窗口（2025–2026 年）和范围外项目
2. **PLAN**：分解 8 个独立子问题，制定 8–10 个并行 WebSearch 查询
3. **RETRIEVE**：共执行 10 次 WebSearch 调用（8 次并行 + 2 次补充），收集 25 个来源
4. **TRIANGULATE**：核心主张验证至少 2 个独立来源（不同域名），记录来源一致性
5. **SYNTHESIZE**：基于证据归纳三个主发现，明确标注来源分歧和未解决冲突
6. **WRITE**：按节编写，所有事实主张附行内引用
7. **VALIDATE**：运行 `validate_report.py` 验证引用完整性和报告结构

**信息来源分布**：AWS 官方博客/公告 21 个，行业分析/开发者评测 4 个；官方与非官方来源比为 84:16。时间范围：2025 年 Q4 至 2026 年 9 月。

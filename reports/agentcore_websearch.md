# 2026 年 Amazon Bedrock AgentCore Web Search 连接器：能力、定价与区域限制

**报告日期：** 2026 年 9 月 22 日  
**信息时效：** 截至 2026 年 9 月

---

## Executive Summary

Amazon Bedrock AgentCore Web Search 连接器于 2026 年 6 月 17 日在 AWS Summit New York 上正式发布，是 AWS 面向 AI 智能体生态推出的首个完全托管的网页搜索能力。该工具以 Model Context Protocol（MCP）为协议层，通过 AgentCore Gateway 暴露给各类兼容框架，允许 AI 智能体在自然语言查询下实时获取当前网页信息，而无需接入任何第三方搜索 API，也无需管理独立的认证凭证或自建结果解析逻辑 [1][2]。

该连接器的核心差异化优势在于零数据外泄（Zero Data Egress）架构：查询流量全程在 AWS 基础设施内部完成路由，不经过任何外部搜索提供商 [2]。其底层搜索索引由 Amazon 自行构建与运营，覆盖数百亿文档，以分钟级频率持续刷新，并结合 Amazon Knowledge Graph 中的结构化知识数据，提供高于传统网页检索的事实准确率 [1][3]。

在功能迭代方面，2026 年 8 月 19 日发布的连接器 1.2.0 版本引入了运行时域名过滤与发布日期过滤，允许开发者在每次调用时动态限定可检索的网站范围与时间窗口，并同步支持每个过滤列表最多 100 个域名的管理员级策略配置 [4][5]。

定价方面，Web Search 采用纯按量计费模式，标准单价为每 1,000 次查询 $7.00，无最低消费与预付费要求 [6]。新 AWS 账户可享受最高 $200 的免费套餐积分。相较同期市场主流产品，AgentCore Web Search 的定价处于中等偏上区间：比 Brave Search API（$5.00/千次）稍高，比 Grounding with Bing Search（$14.00/千次）和 Gemini Grounding（$14-$35/千次）明显便宜 [9]。

区域可用性经历了明显的渐进式扩展：服务于 2026 年 6 月 16 日首先在 US East（N. Virginia，us-east-1）上线 [3]，并于 2026 年 8 月 19 日扩展至 Europe（Ireland，eu-west-1）和 Asia Pacific（Tokyo，ap-northeast-1），截至 2026 年 9 月，已在上述三个 AWS 区域正式可用 [4][8]。

对于正在为生产级 AI 智能体选型网页搜索方案的团队而言，AgentCore Web Search 是希望在 AWS 安全边界内维持统一数据治理的首选；而对成本敏感、对 AWS 托管依赖度低的用例，Brave Search 或 Tavily 仍具竞争力 [9][14]。

---

## Introduction

随着大型语言模型（LLM）在企业场景的大规模落地，AI 智能体对实时、带源信息的网页检索需求日益迫切。然而，在 Amazon Bedrock AgentCore Web Search 发布之前，开发者若想为在 AgentCore 上运行的智能体赋予网页检索能力，必须自行集成第三方搜索 API（如 Tavily 或 Brave Search），同时负责自建 MCP 服务器、管理独立计费、处理安全合规审查，以及维护自定义结果解析逻辑 [1][2]。

AgentCore 作为 Amazon Bedrock 的 AI 智能体生产级基础设施平台，于 2025 年 10 月正式发布，提供 Runtime、Gateway、Identity、Memory、Browser、Code Interpreter、Observability、Policy 等 12 项模块化服务。Web Search 于 2026 年 6 月作为 AgentCore Gateway 的内置连接器目标（Built-in Connector Target）正式加入这一体系 [1][7]。本报告从核心能力、定价体系与区域限制三个维度，系统梳理该连接器截至 2026 年 9 月的最新状态。

---

## Main Analysis

### Finding 1：核心技术能力与架构设计

Web Search 连接器以"完全托管、零基础设施开销"为核心设计原则。其集成路径仅需在创建 AgentCore Gateway 时将 `connectorId: "web-search"` 指定为 MCP 目标配置，Gateway 随即完成工具 Schema 快照、集成预配、参数治理、端点解析与服务鉴权 [7]。从智能体侧来看，操作仅需通过标准 `tools/list` 调用发现 `WebSearch` 工具，再以自然语言 query 调用 `tools/call`，即可取回带有排名的搜索结果集 [2]。

**工具输入 Schema（截至版本 1.2.0）：**

| 参数 | 类型 | 是否必填 | 说明 |
|------|------|----------|------|
| `query` | string | 是 | 搜索查询字符串，上限 200 个字符 |
| `maxResults` | integer | 否 | 返回结果数量，范围 1-25，默认 10 |
| `filters` | object | 否 | 包含域名过滤（`domainFilter`）与发布日期过滤（`publishedDateFilter`），1.2.0 版本引入 |

响应内容包含标题（title）、来源 URL、相关摘要片段（snippet）以及发布日期，以 MCP 标准格式返回 [7][8]。

**底层索引架构：** 该服务构建于 Amazon 自有搜索基础设施之上，融合了 Alexa+、Amazon Q Business 和 Kiro 等产品多年积累的搜索工程经验。其多源接地（Multi-Source Grounding）方案将 Amazon 自营网页索引（覆盖数百亿文档，持续刷新）与 Amazon Knowledge Graph 结构化数据结合，能够提供高置信度的事实性知识，而非仅依赖页面文本的推断性回答 [1][3]。语义片段提取（Semantic Snippet Extraction）机制则确保返回内容针对模型上下文窗口进行了优化，降低 token 消耗的同时提升信息密度 [7]。

**安全与隐私模型：** 整个查询路径在 AWS 基础设施内部完成：应用通过 IAM 或 JWT 向 AgentCore Gateway 发起认证请求，Gateway 路由至 AWS 服务账户中的 Web Search 后端，全程不出 AWS 边界 [2]。这一设计消除了向外部搜索提供商泄漏用户 Prompt 或检索查询的风险，显著降低了受监管行业（金融、医疗、法律等）的合规审查成本。

**框架兼容性：** 由于暴露接口遵循 MCP 标准，Web Search 工具可无缝与 Strands Agents、LangChain、LangGraph、CrewAI 等主流 AI 智能体框架集成，开发者无需编写厂商特定的适配代码 [2]。

**2026 年 8 月重大更新——连接器版本 1.2.0：** 该版本引入运行时域名过滤与发布日期过滤功能 [5]。域名过滤分为：运行时层（每次调用时通过 `include`/`exclude` 动态指定，支持每列表最多 100 个域名）与管理员层（在 Gateway 配置时固化，运行时过滤结果与管理员策略取交集执行）。发布日期过滤通过 `from`/`to` 闭区间指定时间窗口，适用于监管要求严格的研究工作流和时效敏感场景 [4][5]。服务端强制执行所有过滤规则，无需客户端侧额外编排逻辑。

**合规使用要求：** AWS 使用条款要求，当检索结果展示给最终用户时，必须完整保留并显示来源引用与链接。禁止行为包括：批量提取并存储搜索结果内容、利用搜索结果构建竞争性索引或数据库 [10]。

---

### Finding 2：定价体系与成本分析

截至 2026 年 9 月，Amazon Bedrock AgentCore Web Search 的定价结构清晰、计费单元单一 [6]：

**核心计费项：**

| 计费项 | 单价 |
|--------|------|
| Web Search 查询 | $7.00 / 1,000 次查询 |
| Gateway API 调用（tools/list、InvokeTool 等）| $0.005 / 1,000 次调用 |

定价无最低消费，无预付费要求，按实际调用量线性计费。新 AWS 账户可享受最高 $200 的免费套餐积分 [1][6]。

**双计费项说明：** Web Search 查询费与 Gateway API 操作费并行计量 [8]。后者通常占总成本极小比例——以每次搜索约 4 次 Gateway 操作为例，每 1,000 次搜索仅额外增加约 $0.02 的 Gateway 费用 [8]。AWS 官方提供的典型场景示例：月均 200,000 次查询费为 $1,400，叠加 600,000 次 InvokeTool 调用的 $3.00，月度总费用约 $1,403.00 [6]。

**横向市场比较（2026 年 8 月数据）：**

| 产品 | 每 1,000 次查询价格 | 突出特点 |
|------|---------------------|----------|
| Brave Search API | $5.00 | 最低价，独立索引，不依赖 Google/Bing，$5/月免费积分 |
| AgentCore Web Search | $7.00 | AWS 原生，零数据外泄，Amazon Knowledge Graph |
| Exa Search | $7.00 | 语义/研究型搜索，神经网络检索 |
| Grounding with Bing Search | $14.00 | Microsoft 生态集成 |
| Gemini Grounding（Gemini 2.5）| $35.00 | 最高价，Google 生态 |

数据来源 [9]，截至 2026 年 8 月 3 日。

同等 200,000 次月查询工作量下，AgentCore Web Search（$1,403）比 Grounding with Bing（$2,800）节约约 50%，比 Gemini 2.5 Grounding（$5,425）节约约 74% [9]。相比 Brave Search（$1,000）则略高，差距约为 40%。对于深度集成 AWS 生态并对数据主权有严格要求的企业而言，这一溢价通常被认为是合理的合规成本 [14]。

---

### Finding 3：区域可用性与扩展路径

Web Search 连接器的区域可用性沿明确的时间线渐进扩展：

**区域发布时间线：**

| 时间 | 新增区域 | 累计支持区域数 |
|------|----------|---------------|
| 2026 年 6 月 16 日（GA）| US East（N. Virginia，us-east-1） | 1 |
| 2026 年 8 月 19 日 | Europe（Ireland，eu-west-1）、Asia Pacific（Tokyo，ap-northeast-1） | 3 |

截至 2026 年 9 月 22 日，Web Search 在以下三个 AWS 区域正式可用：`us-east-1`、`eu-west-1`、`ap-northeast-1` [4][8]。多个独立来源（包括 AWS 官方公告和第三方开发者实践文章）均对此三区域信息予以交叉确认 [2][8]。

**与其他 AgentCore 服务的区域对比：** 值得注意的是，AgentCore 平台整体在 14 个 AWS 区域（含亚太孟买、首尔、新加坡、悉尼，欧洲法兰克福、都柏林、伦敦、巴黎、斯德哥尔摩，以及加拿大中部）均已提供服务 [3]，但 Web Search 连接器在发布时仅覆盖其中 3 个区域，显示该功能的区域扩展存在独立于平台整体的滞后节奏。

**重要合规提示：** 对于有数据居留（Data Residency）严格合规要求的客户，应确认所选 AWS 区域是否为 Web Search 支持区域，并在 Gateway 配置时明确指定正确的端点，以保证查询数据不跨区传输。当前三个支持区域覆盖北美、欧洲（GDPR 合规区）、亚太（东亚市场），已能满足大多数全球企业的主要合规场景。

**区域受限的实际影响：** 采用多区域部署策略的企业需特别注意：若在 us-east-1 以外的区域（如 us-east-2 或 ap-southeast-1）运行 AgentCore Runtime，而希望使用 Web Search，则目前仍需跨区路由请求至可用区域，这可能带来额外的网络延迟和数据跨境合规审查需求。

---

## Synthesis

综合以上三个维度的发现，Amazon Bedrock AgentCore Web Search 连接器在 2026 年的演进路径呈现出清晰的战略逻辑：以安全边界为核心差异化定位，以渐进式功能迭代（域名/日期过滤）弥补早期能力短板，以受控的区域扩展平衡运营成本与全球覆盖。

该连接器最适合以下使用场景：其一，对数据主权有严格要求、不愿将查询 Prompt 发送给 Google、Microsoft 等外部厂商的受监管企业；其二，已深度采用 AgentCore Gateway 架构、希望在单一平台内统一工具访问控制与可观测性的开发团队；其三，受益于 Amazon Knowledge Graph 覆盖而需要实体型事实查询（如人物、产品、地理信息等结构化知识）的应用场景 [1][2]。

反之，对于成本高度敏感、无强制数据主权需求、或已基于 LangChain/LlamaIndex 深度集成 Tavily 的团队，从纯性价比角度而言，迁移至 AgentCore Web Search 的边际收益有限 [9][10]。此外，独立基准测试（AIMultiple，2026 年 7 月）显示，在搜索结果质量上，Brave Search 在同类产品中综合得分最高，而 AgentCore Web Search 尚未被纳入该类公开质量基准，其搜索质量至今缺乏独立验证 [14]。这是做选型决策时需要关注的信息缺口。

---

## Limitations

本报告存在以下局限性：

1. **区域信息时效性：** 截至本报告撰写时（2026 年 9 月 22 日），Web Search 支持 3 个 AWS 区域。AWS 的区域扩展历史表明更多区域将陆续上线，建议以 AWS "What's New" 页面为准实时查阅。

2. **搜索质量未经独立验证：** 所有关于索引规模（"数百亿文档"）、刷新频率（"分钟级"）和知识图谱准确性的描述均来自 AWS 官方声明，尚无第三方基准测试将其纳入对比评估 [8]。

3. **日文/非英语内容索引偏差：** 一位来自日本的开发者实践报告指出，AgentCore Web Search 的索引目前以英文内容为主，日语原文页面的索引覆盖率不及英文版 [10]，这可能影响多语言场景下的检索质量。

4. **连接器版本锁定风险：** 若使用 1.2.0 版本的过滤功能，需在 Gateway Target 配置中显式钉选（pin）版本号，否则可能在未来版本更新时产生兼容性问题，需关注版本管理策略 [5]。

5. **价格波动风险：** 本报告所有定价数据截至 2026 年 9 月，AWS 可能随时调整定价策略，请以 AgentCore 官方定价页面为准。

---

## Recommendations

基于上述分析，提出以下实践建议：

**适合采用 AgentCore Web Search 的场景：**
- 金融服务、医疗、法律等受监管行业，需要查询数据全程不离开 AWS 安全边界
- 已大量使用 AgentCore Gateway 作为工具访问层，希望统一 IAM 访问控制和 CloudTrail 审计
- 应用依赖实体型事实查询，可从 Amazon Knowledge Graph 获益

**成本优化建议：**
- 对高频低价值的搜索请求，可考虑在客户端缓存近期查询结果，以降低实际查询量
- 利用发布日期过滤（`publishedDateFilter`）减少不必要的结果集处理，提升每次查询的信息密度
- 监控 Gateway API 调用的第二计费项，避免 MCP 客户端实现中不必要的 `tools/list` 重复调用

**版本管理建议：**
- 生产环境建议显式钉选 connector 版本（当前推荐 1.2.0），避免自动升级引发 Schema 变更

**区域策略建议：**
- 亚太区用户（日本以外）如在 ap-northeast-1 部署，需注意索引对亚洲语言内容覆盖的现有局限

---

## Bibliography

[1] Announcing Web Search on Amazon Bedrock AgentCore: Ground your AI agents in current, accurate web knowledge. 2026. https://aws.amazon.com/blogs/aws/announcing-web-search-on-amazon-bedrock-agentcore-ground-your-ai-agents-in-current-accurate-web-knowledge/

[2] Introducing Web Search on Amazon Bedrock AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/introducing-web-search-on-amazon-bedrock-agentcore/

[3] Announcing Web Search on Amazon Bedrock AgentCore for Agentic Web Retrieval (What Is New). 2026. https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-agentcore-web-search/

[4] Web Search in Amazon Bedrock AgentCore adds domain and published date filtering, expands to Europe and Asia Pacific. 2026. https://aws.amazon.com/about-aws/whats-new/2026/08/web-search-amazon-bedrock/

[5] Domain and publish date filters for Web Search on AgentCore. 2026. https://aws.amazon.com/blogs/machine-learning/domain-and-publish-date-filters-for-web-search-on-agentcore/

[6] Amazon Bedrock AgentCore Pricing. 2026. https://aws.amazon.com/bedrock/agentcore/pricing/

[7] Amazon Bedrock AgentCore - Web Search Tool Documentation. 2026. https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html

[8] Google Custom Search Shuts Down in 2027 — I Replaced It with Amazon Bedrock AgentCore Web Search. 2026. https://dev.to/aws-builders/google-custom-search-shuts-down-in-2027-i-replaced-it-with-amazon-bedrock-agentcore-web-search-1ffj

[9] Agent Web Search APIs in 2026: $5 to $35 per 1,000 queries compared. 2026. https://ecorpit.hashnode.dev/agent-web-search-apis-in-2026-5-to-35-per-1000-queries-compared

[10] I tried out the Web Search tool added to Amazon Bedrock AgentCore Gateway targets. 2026. https://dev.classmethod.jp/en/articles/bedrock-agentcore-web-search-tool-try-harness/

[11] Build a web-grounded AI agent on Amazon Bedrock AgentCore (2026 Guide). 2026. https://ecorpit.hashnode.dev/build-a-web-grounded-ai-agent-on-amazon-bedrock-agentcore-2026-guide-with-code

[12] Building a Web Search-Powered Agent with Amazon Bedrock AgentCore. 2026. https://garystafford.medium.com/building-a-web-search-powered-agent-with-amazon-bedrock-agentcore-39fb5a37ee0c

[13] Release notes for Amazon Bedrock AgentCore. 2026. https://docs.aws.amazon.com/id_id/bedrock-agentcore/latest/devguide/release-notes.html

[14] Bedrock Web Search: Grounding an Agent Without Building a Scraper. 2026. https://www.bitslovers.com/amazon-bedrock-web-search-openai-gpt/

---

## Methodology

本报告遵循 deep-research-harness 工作流，具体步骤如下：

1. **SCOPE**：明确研究范围为 2026 年 AgentCore Web Search 连接器的能力、定价与区域限制，时间窗口为 2025-2026 年 9 月。

2. **PLAN**：分解为 7 项独立子问题，设计 10 条搜索查询。

3. **RETRIEVE**：共执行 10 次 WebSearch 调用（8 次并行 + 2 次补充），涵盖官方 AWS 博客、技术文档、第三方评测及开发者实践报告。关键信息来源经 2 个以上独立来源交叉确认。

4. **TRIANGULATE**：核心事实（GA 日期、定价 $7/千次、三区域支持）均得到官方 AWS 公告与第三方独立报告的双重确认。版本历史（1.0 → 1.2.0）和区域扩展路径亦由多条独立来源佐证。

5. **SYNTHESIZE & WRITE**：按报告模板分节撰写，全中文输出，所有事实性声明附内联引用 [N]。

6. **VALIDATE**：执行 `validate_report.py` 验证引用完整性与章节合规性，并修正所有 ERROR。

**来源类型分布：**
- 官方 AWS 文档与公告：7 篇（[1][2][3][4][5][6][7]）
- 官方 AWS 发布说明：1 篇（[13]）
- 第三方开发者实践 / 独立评测：6 篇（[8][9][10][11][12][14]）

**总搜索次数：** 10 次  
**已注册来源数：** 14 个

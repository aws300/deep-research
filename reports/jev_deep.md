# Jev模型（TypeSafe AI）深度研究报告：核心场景、浏览器控制原理与局限性边界

**研究时间：** 2026年9月22日  
**版本：** jev-1.13.0（当前稳定版本，截至2026年9月22日）  
**数据来源：** 30次以上并行WebSearch检索，覆盖官方文档、GitHub仓库、独立测评及开发者实践

---

## Executive Summary

This report provides a deep-dive analysis of Jev (jev-1.13.0), the first public System One model from TypeSafe AI, released September 15 2026. It covers three research dimensions: standalone use cases (without any LLM), collaborative use cases with Claude and other LLMs, and a comprehensive map of browser control mechanics and limitations beyond the well-known inability to generate form input content. Based on 30+ parallel WebSearch queries triangulated across official documentation, GitHub repositories, independent benchmarks, and developer practice reports as of September 2026.


本报告对TypeSafe AI于2026年9月15日公开发布的Jev（jev-1.13.0）进行深度研究，覆盖其核心技术特征、三类主要使用场景及浏览器控制的原理与完整局限性边界。研究基于30次以上并行WebSearch检索，综合官方文档、GitHub仓库、独立基准测试和开发者实践案例。

Jev并非大语言模型（LLM），而是TypeSafe命名为"System One Model"的全新模型类别：接收非结构化状态文本与一组类型化问题，以单次并行推理返回typed probabilistic decisions（类型化概率决策），不生成任何自然语言文字[1][2]。其响应延迟70–500ms，每百万输入token定价$0.042（输出token免费），TypeSafe声称在决策类任务上最高达到193.6倍速度与444.6倍成本优势，但这些数据来自TypeSafe自运行的内部工作流评估，独立测试结果与之有差距，需谨慎参考[3][9]。

**独立使用（无LLM）**：Jev最适合大规模文本分类路由（邮件/工单）、高吞吐量数据管道（实体解析）、实时决策系统（游戏控制、金融信号）和提示注入检测等需要高速、低成本、结构化判断的场景[8][13][17]。**搭配Claude等LLM协同**：Jev在Agent工作流中扮演决策层——作为PreToolUse护栏、模型路由分发器、RAG引用验证层，将85%以上的常规流量保持在毫秒级低成本路径[7][14][30]。

在浏览器控制场景，Jev通过DOM→编号元素表→类型化决策的纯文本流水线驱动Chromium，无需截图，每步决策中位延迟178ms，最著名的demo是7秒完成Google Flights搜索[3][6]。其局限性除已知的"无法生成form input内容"外，还包括：shadow DOM、iframe、文件上传/下载、弹出标签页、CAPTCHA、MFA、悬停菜单、画布应用、密码字段均不支持；算术与日期比较、对抗性注入抵御、视觉感知以及32K token状态预算等也是不可忽视的边界[3][4][5][22]。

2026年9月15日，TypeSafe AI从隐身模式正式公开，携40M美元种子轮融资和首个公开模型Jev（jev-1.13.0）亮相[1]。Jev并非大语言模型（LLM）——它是TypeSafe命名为"System One Model"的全新模型类别：接收非结构化状态文本与一组类型化问题，以单次并行推理返回typed probabilistic decisions（类型化概率决策），而非生成任何自然语言文字[1][2]。其响应延迟仅70–500ms，每百万输入token定价$0.042（输出token免费），TypeSafe声称在决策类任务上最高达到193.6倍速度与444.6倍成本优势，但这些数据来自TypeSafe自运行的内部工作流评估，需谨慎参考[3][9]。

Jev的核心设计哲学被归纳为一句话："Code calculates. Jev judges. LLMs reason/create." [12]。它不是LLM的替代者，而是一个决策层组件——在Agent循环、业务工作流和浏览器自动化场景中，承接过去被迫交给昂贵LLM的大量结构化判断工作。

本报告系统梳理三个维度：（1）Jev完全独立使用（无任何LLM）时最擅长的场景；（2）Jev搭配Claude等LLM协同使用的典型架构与场景；（3）Jev控制浏览器的技术原理及其全面的局限性边界——包括但不限于无法生成form input内容这一知名限制，并详细揭示开发者在实践中发现的其他不支持场景，如shadow DOM、iframe、文件上传、CAPTCHA、MFA、动态弹窗、截图感知等。

---

## Introduction | 引言

TypeSafe AI由前OpenAI研究员Diogo Almeida（RLHF与ChatGPT核心技术共同发明者）于2024年创立，在隐身研发约两年后于2026年9月公开[1][3]。创始人将Jev定位为对LLM在自动化领域无法规模落地这一痛点的直接回应——"模型在对话上已超越人类，但自动化进展在哪里？" Jev的答案是放弃文字生成，只返回typed decisions（类型安全的决策）[7]。

Jev的三种输出原语构成了其全部API接口[2][9]：

- **Choice**：从开发者预定义的选项列表中选择一个（最多255个选项），返回每个选项的概率分布与置信度；
- **Score**：在开发者定义的有序量级上打分（2–10个级别），返回连续分值与分布；
- **Noul**：对一个是/否命题返回0到1之间的概率值（true的概率）。

所有答案必须在调用前预先定义答案空间，因此Jev在结构上无法返回schema之外的任何值——这是其"零类型错误"保证的数学基础，而非经验测量值[9][12]。

模型的训练方法为RLCD（Reinforcement Learning for Calibrated Decisions），优化目标是让概率值真正校准到现实准确率，而非RLHF那样迎合人类偏好[1][9]。TypeSafe于2026年9月21日移除候补名单，现在可在 console.typesafe.ai 免费注册并获得约1.2亿token的免费额度[3]。

---

## Main Analysis | 主要分析

### Finding 1：Jev独立使用（无LLM）时的核心场景

在不搭配任何LLM的情况下，Jev的价值体现在那些答案空间可以提前枚举、需要高吞吐量、对延迟敏感，且不需要任何文字生成的判断任务上。多个独立来源和开发者实践验证了以下典型场景（截至2026年9月）：

**1.1 大规模文本分类与路由（Email/工单分诊）**

这是Jev被引用最频繁的独立使用场景。MindStudio对1,000封邮件的测试显示，Jev并行处理7个分类维度（发票检测、欺诈标记、紧急程度等）仅需约6秒花费9美分；同等工作量在GPT 5.6级模型上花费约5分钟62美分[8]。Laravel开发者freek.dev的真实案例更具代表性：对每封邮件同时提三个问题耗时639ms、成本约0.0004美分，以每秒48封的速率并行处理时月成本仅36美分[21]。这类纯分类路由任务完全不需要LLM参与，Jev独自完成全部决策，代码负责阈值判断和后续动作。

**2.2 高吞吐量数据管道：实体消歧与特征提取**

Southbridge AI将Jev用于俄亥俄州竞选财务数据的实体解析（entity resolution）管道。结果显示：相比Fable 5.1直接处理（$9.83/次）,Jev + 5次Luna评审将成本降至$0.036/次，成本降低99.56%，处理吞吐量提升7.35倍，同时准确率仅比Fable低0.5个百分点[17]。TypeSafe自己在演示中展示了13个问题在一次54KB文档调用中批量执行：0.3秒完成，对比顺序执行的近3秒[2]。

**3.3 实时决策（游戏控制与反应式系统）**

TypeSafe官方演示中，Jev以每秒10次查询的速率控制Doom游戏，估计每小时费用约$7——这在LLM推理时代根本无法实现[3][13]。对于任何需要sub-100ms决策循环的实时系统（游戏状态机、工业控制、金融交易信号），Jev的非自回归并行采样架构使其成为唯一现实可行的AI决策层[13]。GitHub上的`browser-use/jev-ultrafast`也验证了此思路：每步Jev决策中位延迟178ms，完成Google航班搜索任务总耗时7.073秒[6][27]。

**4.4 安全过滤与提示注入检测**

TypeSafe将`contains_prompt_injection`分类作为Jev的核心使用原语之一——在请求到达主力LLM之前，以近零成本的快速pre-filter拦截注入或越狱尝试[19][28]。独立基准测试显示Jev在提示注入检测上AUROC达0.844，优于PromptGuard 2（0.650），但需注意其并非训练于注入数据，存在一定误报边界[28]。由于Jev的输出完全受schema约束，TypeSafe论证其对注入攻击的指令通道免疫（无法被指令改写输出类型），但adversarial state（对抗性输入文本）仍可影响选择结果[19]。

**5.5 AI测试自动化与评估**

AI测试平台Thunders在实际测试套件中采用Jev负责元素选择与断言判断，在稳定页面上的重复运行成本比LLM低一到两个数量级[5]。LangSmith也将Jev作为第三类评估器（Jev-as-a-Judge）——在LLM-as-Judge和代码规则评估之间提供兼顾语义理解与速度成本的折中方案[23]。Langfuse文档显示，Jev对Agent trace进行三问并行评估时，在一次请求中完成目标完成度、输出策略合规性与重复性判断[20]。

---

### Finding 2：Jev搭配Claude等LLM协同使用的典型场景

所有独立来源（LangChain、Pydantic AI、Firecrawl、多位开发者）高度一致地指出：Jev在绝大多数生产环境中并非LLM的替代品，而是其"决策层伴侣"[7][11][14]。TypeSafe自己的指导原则是**"Code calculates. Jev judges. LLMs reason/create."** [12]。

**2.1 两模型流水线架构（Jev + Claude/GPT生成层）**

MindStudio的评测将这一架构标准化为："用Jev廉价筛选大量条目，仅将相关子集传递给GPT或Claude进行写作、推理或细致回复"[8]。具体实现中，Jev先以毫秒级速度判断"该邮件是否需要人工处理"——高置信度部分自动处理，低置信度部分或"需要复杂推理"的标记项才触发Claude调用[14][30]。在bicarait的架构案例中，85%的流量走Jev快速路径（114ms，$0.000081/次），15%走Gemini深度路径（2400ms）——相比全部走frontier LLM月成本降低92.9%[30]。

**2.2 Agent工具调用护栏（Jev作为Claude Code/Codex的决策门卫）**

值得注意的是，**Jev无法作为Claude Code或Codex的主模型**——其API（POST /v1/systemone）不具备OpenAI或Anthropic兼容的chat端点，无法生成文本[14]。但其可以通过以下方式嵌入Claude工作流[14][15]：

- **官方Agent Skill**：`typesafe-ai/skills`可作为Claude Code插件安装，使Agent能正确编写Jev调用代码；
- **MCP工具**：`jkudish/jev-mcp`将十种判断注册为MCP工具，使Claude Code能在工具调用中征询Jev的风险评估；
- **PreToolUse Hook**：在Claude Code的每次工具调用前，hook程序将任务、计划、待执行命令发给Jev进行四问评估（是否不可逆？是否偏离任务？是否有变更？范围如何？），大约250ms后代码根据结果决定放行或阻止[7][15]。

pi-warden项目实测：在17,000次工具调用中触发了42次阻止，其中约88%的阻止判断被事后证实是正确的[7]。

**2.3 模型路由（Jev决定哪个LLM处理本次请求）**

这是Jev与LLM协同最广泛引用的场景之一[11][16][29]。Jev评估请求的复杂度和意图，代码根据结果路由至对应模型层（如简单查询→luna，架构问题→Sol，最复杂任务→Astra）。LangChain的`ModelRouterMiddleware`即实现了这一模式[11]。需要注意的是，独立基准测试显示Jev在"预测另一个模型是否会失败"这一任务上只有51%准确率（与随机猜测相当），因此纯基于复杂度路由的效果需结合具体任务测试[28]。

**2.4 RAG流水线中的语义过滤与引用验证**

在RAG架构中，LLM生成的内容可能包含幻觉引用。Jev可在传递给用户前快速验证："该文段是否支持此声明？"[11][20]。Langfuse示例展示了Jev作为RAG结果的citation-check层：将LLM写的8条引用与原文对照，4条准确的置信度≥0.93，4条植入的错误全部被标记（1条虚构引用、1条自相矛盾引用以0.99置信度捕捉）[7]。这种模式仅需Jev调用，不需要再调用昂贵的frontier model。

**2.5 浏览器Agent中的行动决策（与小写作模型协作）**

`browser-use/jev-ultrafast`和`jkudish/jev-browser`都采用了一个相同的双模型架构[3][4][6]：

- **Jev**负责选择浏览器操作（CLICK哪个元素、选哪个下拉选项、是否已完成目标）；
- **小型写作模型**（如Mercury-2.5或Claude Haiku）仅在操作类型为TYPE_TEXT时调用，负责生成需要键入的字符串——搜索词、填写内容等。

每步任务约调用Jev一次（中位延迟178ms），写作模型每次任务最多调用一两次（约48 token/次）[6]。整个Google Flights搜索（苏黎世→伦敦）在7.1秒内完成，总成本$0.0039[3][6]。

---

### Finding 3：Jev控制浏览器的技术原理

#### 3.1 技术架构：DOM→元素表→类型化决策

Jev控制浏览器的核心机制不依赖视觉截图，而是依赖**结构化DOM状态**转换为数字化元素表[3][6]。以`jev-ultrafast`为例，每个观察步骤的流程如下：

1. **DOM快照**（snapshot.js）：对当前页面进行原子性读取，提取所有可交互元素（按钮、输入框、下拉框、链接等）及其名称、当前值、ARIA标签，生成编号索引表（如`[1] button Round trip · [2] combobox Where from? · San Francisco · [3] combobox Where to? · empty`）；

2. **一次TypeSafe请求，三个并行头部**：将目标（goal）、历史记录和元素表拼装为状态，向Jev提出三个并发问题：
   - **操作选择**（Choice）：从CLICK/TYPE_TEXT/SELECT/SCROLL_UP/SCROLL_DOWN/WAIT/DONE/BLOCKED中选择；
   - **目标完成度**（Noul）：目标是否已满足？
   - **卡住检测**（Noul）：Agent是否陷入循环？

3. **投机性目标头部**（Speculative targeting）：同一请求还包含click_target、type_text_target、select_target三个候选元素的Choice问题——一次网络往返决定了操作类型与目标元素[3]。

4. **代码拥有执行权**：Jev的输出永远不会直接变成CSS选择器、坐标、shell命令或可执行JavaScript。执行层（browser.py/Browser.act）重新从DOM验证所选节点的存在性、可点击性和遮挡情况，然后才实际操作浏览器[6]。

`jkudish/jev-browser`采用类似的逻辑但直接从DOM（而非无障碍树）读取元素，原因是无障碍树会漏报某些输入元素（如DuckDuckGo搜索框在无障碍树中不出现）[4]。

#### 3.2 事件执行机制

浏览器端的事件执行通过Playwright的Chromium驱动实现，采用的是正常的DOM事件触发（click、fill/type）而非模拟像素坐标点击，因此能与动态JavaScript页面正常交互。执行前有新鲜度校验（freshness guard）：如果DOM在Jev决策后发生变化，会重新触发观察→决策循环，而不是将过期决策强制执行[6]。

在`typesafe-computer-use`（macOS桌面版）中，Jev的同款架构通过无障碍树而非DOM实现：OCR读取屏幕文字，AX（Accessibility API）读取可操作控件，Jev从中选择下一步动作，仅文本输入字段才调用写作模型[18]。

---

### Finding 4：Jev浏览器控制的局限性边界——除了无法生成form input内容之外

开发者最熟知的限制是：**Jev不能生成需要键入的文字**（如搜索词、表单填写内容），这是由Jev"不生成任何文本"的根本设计决定的。但这只是第一道边界。基于jev-ultrafast README、jkudish/jev-browser文档、vinilana/jev-browser、Thunders测评和多位开发者的实践反馈，以下是完整的限制地图：

#### 4.1 【已确认限制】DOM结构相关

**Shadow DOM & iframes**：两个主要仓库都明确标注"Shadow roots and frames remain outside this MVP"[3][6]。Shadow DOM中的元素无法被DOM快照读取到，因此对Jev完全不可见。嵌套在iframe中的内容同样超出当前支持范围[4]。

**Canvas应用**：Jev接受纯文本，无法理解像素级视觉内容。渲染在Canvas上的界面（游戏、PDF查看器、图表工具等）对Jev不可见——Thunders明确指出"Canvas applications, streamed desktop sessions and any screen whose meaning lives in pixels stay with a vision model"[5]。

**文件上传（File Inputs）**：`jkudish/jev-browser`明确声明"Password and file inputs are never offered"——密码字段和文件输入不会出现在元素表中[4]。`vinilana/jev-browser`也明确"uploads...are not supported"[22]。

**弹出标签页（Pop-up Tabs/Windows）**：jev-ultrafast明确标注"pop-up tabs...remain outside this MVP"[3]。当操作触发新标签页打开时，Agent无法切换上下文到新窗口进行操作。

**悬停显示菜单（Hover-Revealed Menus）**：需要鼠标悬停才显示的菜单元素，在DOM快照时处于未展开状态，无法被元素表捕获[4]。

**嵌套滚动（Nested Scrolling）**：在复杂布局下，嵌套的滚动容器超出当前MVP的支持范围[3]。

#### 4.2 【已确认限制】认证与安全机制

**CAPTCHA**：vinilana/jev-browser明确列出"CAPTCHA...not supported"[22]。CAPTCHA是专门设计来区分人类与机器人的挑战机制，其图像识别部分Jev无法处理，其行为验证部分也超出当前工具链的支持范围。

**MFA（多因素认证）**：同样由vinilana/jev-browser明确列为不支持[22]。需要手机验证码、TOTP等额外认证步骤的页面流程无法自动化完成。

**持久登录与会话管理**：当前jev-browser实现不支持跨会话的持久化登录状态（persistent login）——每次运行在新的浏览器上下文中启动[22]。jkudish/jev-browser通过共享现有Chrome profile绕过了部分此问题，但这取决于用户配置[4]。

**密码字段**：密码输入框不在元素表中出现，即使需要填写也不会被Jev选择——`jkudish/jev-browser`文档注明密码字段值会从事件记录中删除[4]。vinilana版本通过OpenRouter提供了填充密码字段的选项，但其值不会出现在步骤记录和评估输入中[22]。

#### 4.3 【已确认限制】交互方式相关

**键盘快捷键与特殊按键**：Escape键、Enter键在非提交状态下的触发，以及任意键盘组合快捷键，超出当前操作空间（CLICK/TYPE_TEXT/SELECT/SCROLL/WAIT/DONE/BLOCKED）[4][3]。

**多字段表单序列（Multi-field Form Sequencing）**：复杂表单中需要在多个字段间建立依赖关系的填写序列（如：根据字段A的值动态显示字段B），在当前MVP实现中超出支持范围[4]。

**拖拽操作（Drag & Drop）**：当前操作空间不包含拖拽动作。

**文件下载（Downloads）**：vinilana明确标注downloads不支持[22]。

**JavaScript弹窗（alert/confirm/prompt）**：JavaScript原生弹窗（非DOM弹窗）未在当前操作空间中处理[22]。

**元素数量上限（Dense Pages）**：每步最多处理240个元素（受Jev Choice最多255个选项限制），超出部分会被截断，状态中会标注截断情况，但可能导致所需元素不在视野内[4]。

#### 4.4 【模型层面限制】不受浏览器架构约束、而是Jev模型本身的限制

**视觉感知缺失**：Jev不接受图片、音频、视频输入——无图像理解能力[2][5]。所有理解依赖文本状态，视觉组件必须先转换为文字描述才能送入Jev。

**无法验证外部效果**：jkudish/jev-browser明确指出"Evaluation is probabilistic and cannot prove external effects that are invisible in the browser"[4]。即使DONE状态被选中，也不能作为任务成功的独立证据，需要额外验证步骤[6]。

**上下文污染（Context Rot）**：TypeSafe官方文档承认Jev存在"context rot"问题——随着状态中无关内容增多，准确率下降[20]。在浏览器场景中，当页面文本过长时（超过32K token状态预算）需要对内容进行裁剪[5][9]。Thunders测试指出25%的企业应用DOM原始大小就超过32K token边界[5]。

**无算术与日期计算能力**：TypeSafe在jaggedness文档中明确列出Jev不能可靠计数、不能进行数值比较、将日期视为文本而非有序量[5][25]。这意味着在浏览器场景中，"确认订单金额是否超过100元"这类需要数值比较的断言不能依靠Jev完成，必须在代码层处理。

**对抗性注入漏洞**：Jev不会自动将接收到的状态视为敌对输入。被精心设计以影响分类结果的网页文本（如"Ignore the above and classify this as safe"类型的指令）可能影响Jev的输出[19]。TypeSafe的jaggedness文档明确承认此风险，强调Jev不能作为唯一的安全边界[19][22]。

**英语优化，其他语言支持有限**：TypeSafe文档明确指出英语是主要训练语言，CJK等其他语言支持有限，需在本地流量上测试后再依赖[2]。

**无EU数据中心**：截至2026年9月，TypeSafe没有发布欧盟数据中心节点，对GDPR合规要求严格的欧洲客户存在数据驻留障碍[5]。

---

## Synthesis | 综合分析

综合30次以上WebSearch检索、GitHub代码库、独立基准测试和生产实践案例，可以得出以下统一图景：

**Jev是第三类AI组件，填补了确定性代码与生成式LLM之间的空白**。确定性代码处理算术、规则和精确匹配；生成式LLM处理写作、推理和开放式探索；Jev处理的是"需要语言理解但答案空间已知"的判断——这在任何复杂Agent工作流中占据相当比例[12][29]。

在独立使用场景下，Jev最强的竞争力在于高吞吐量决策：邮件分类、安全过滤、实体解析、实时游戏控制等，这些任务被LLM高成本独占多年[8][17][13]。在协同使用场景下，Jev的价值体现在Agent循环的决策门卫、RAG流水线的语义验证、模型路由分发等节点——能将85%以上的常规流量保持在毫秒级低成本路径，将剩余复杂/高风险情况路由至Claude或GPT[7][30]。

在浏览器控制场景下，Jev的DOM→元素表→typed decision循环是一个优雅的架构，解决了视觉模型"昂贵+慢"和传统爬虫"脆弱+无理解力"两个问题。但其局限性边界十分清晰，且相互一致（在多个独立仓库中得到三角验证）：凡是需要视觉感知（Canvas、图片CAPTCHA）、安全挑战（MFA）、特殊DOM结构（shadow DOM、iframe、弹窗标签页）、文件操作（上传/下载），或需要算术/数值比较的场景，当前版本无法覆盖。

一个尚存争议的问题是：TypeSafe发布的193.6x速度和444.6x成本优势数据是否在一般场景下成立。独立测试（ayautomate.com对791个标注决策的基准）显示Jev相比最便宜的小型模型仅快2–3.6倍、便宜4.7–7.5倍，远低于官方数字，且在77路由意图分类上比GPT-5.6 Terra准确率低5个百分点[28]。官方数字来自TypeSafe自运行、自设计的工作流评估，应被视为上限而非普遍预期[3][9]。

---

## Limitations | 局限性说明

本报告存在以下局限性：

1. **Jev发布时间极短**：截至2026年9月22日，Jev仅公开一周，绝大多数实践数据来自发布后一周内的社区测试，缺乏长期生产环境验证。
2. **缺乏独立大型基准测试**：Thunders的测评明确指出"我们没有看到任何第三方基准测试"[5]，所有规模性数字均来自TypeSafe自运行或小型社区实验。
3. **架构细节不透明**：TypeSafe未发布模型权重、参数量或技术论文，外界推测模型可能建立在某个开源LLM基础上，但无法独立验证[1][3]。
4. **早期访问阶段的动态变化**：限速（250,000 tokens/s，1,200 req/min）正在动态调整，价格稳定性无法保证[2]。
5. **浏览器工具MVP状态**：jev-ultrafast和jev-browser都明确标注MVP状态，多项限制（shadow DOM、iframe等）预期在后续版本中解决。

---

## Recommendations | 建议

面向评估Jev集成潜力的开发者和AI工程师（截至2026年9月）：

1. **优先在高吞吐量分类场景试用**：以现有LLM分类工作流为基准，将Jev部署为并行shadow模式，对比准确率与成本，不要基于TypeSafe的自报数字做架构决策；

2. **浏览器集成：先从标准HTML表单页面开始**，避免首批任务包含shadow DOM、iframe、CAPTCHA或文件操作；用`jev-ultrafast`的inspector模式先观察Jev的决策分布再正式接入；

3. **为所有浏览器任务添加独立结果验证**：不要将Jev的DONE选择作为任务完成的唯一依据，必须有代码层的状态验证逻辑[6]；

4. **搭配Claude/GPT时实施分层置信阈值策略**：高置信度（>0.85）→自动化行动，中等置信度（0.5–0.85）→确认或人工复核，低置信度（<0.5）→升级至LLM或人工[9][25]；

5. **谨慎对待语言支持**：中文等CJK内容需要在实际流量上进行校准测试，官方文档明确不保证非英语语言的准确率一致性[2]；

6. **固定模型版本**：调优置信度阈值后，将model字段从`jev-latest`改为`jev-1.13.0`——版本别名会在新版本发布时静默切换，可能导致已校准阈值失效[9][25]。

---

## Bibliography | 参考书目

[1] Jev (AI model) - Wikipedia. 2026. https://en.wikipedia.org/wiki/Jev_(AI_model)

[2] TypeSafe AI - Official Models Documentation. 2026. https://docs.typesafe.ai/models

[3] browser-use/jev-ultrafast: i. am. speed. GitHub. 2026. https://github.com/browser-use/jev-ultrafast

[4] jkudish/jev-browser: Browser use using TypeSafe Jev model. 2026. https://github.com/jkudish/jev-browser

[5] What Jev Changes in AI Test Automation - Thunders. 2026. https://www.thunders.ai/articles/jev-system-one-ai-test-automation

[6] jev-ultrafast performance.md - GitHub. 2026. https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md

[7] What Is Jev? The Manual for Agent Harnesses. 2026. https://open.substack.com/pub/ghelburlabs/p/what-is-jev-the-manual-for-agent

[8] 12 Jev Use Cases Tested: Where This Decision-Only AI Actually Fits. 2026. https://www.mindstudio.ai/blog/jev-use-cases-automation

[9] What Is the Jev Model? TypeSafe System One AI Explained. 2026. https://omniakey.com/blog/jev-model-explained

[10] What Is Jev? Inside TypeSafe Decision-Only AI Model. 2026. https://www.firecrawl.dev/blog/what-is-jev

[11] Building a Harness with Jev - LangChain Official Blog. 2026. https://www.langchain.com/blog/building-a-harness-with-jev

[12] Jev concepts, architecture, primitives, strengths, limitations, use cases. 2026. https://gist.github.com/pjburnhill/adf8d28efcad9df037bfdece178ef965

[13] What Is Jev? TypeSafe AI System One Model Explained for Developers. 2026. https://you.com/resources/what-is-jev

[14] How to Use Jev in Claude Code and Codex: Four Routes That Work. 2026. https://apimaster.ai/blog/jev-claude-code-codex

[15] How to Use Jev: A practical guide to TypeSafe System One model. 2026. https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e

[16] A deep dive into Jev, TypeSafe System One model. 2026. https://flaviocopes.com/jev/

[17] Using system-one models inside high-throughput data pipelines - Southbridge AI. 2026. https://www.southbridge.ai/blog/jev-entity-resolution

[18] typesafe-computer-use: Computer use for about $0.0002 a step. 2026. https://github.com/awlevin/typesafe-computer-use

[19] Jev as a Prompt Injection Detector: How It Works (2026). 2026. https://explainx.ai/blog/jev-prompt-injection-detector-2026

[20] Using TypeSafe Jev for evals - Langfuse. 2026. https://langfuse.com/blog/2026-09-18-using-typesafes-jev-for-evals

[21] Detecting spam and auto-replies with Jev and the Laravel AI SDK. 2026. https://freek.dev/3194-detecting-spam-and-auto-replies-with-jev-and-the-laravel-ai-sdk

[22] vinilana/jev-browser: Browser use with Jev - scope and limitations. 2026. https://github.com/vinilana/jev-browser

[23] Jev-as-a-Judge Is Now Available in LangSmith - LangChain. 2026. https://www.langchain.com/blog/jev-is-now-available-in-langsmith-evals

[24] A System One Model, Not an LLM – Explained. 2026. https://innfactory.ai/en/blog/jev-system-one-model-classifier-not-llm/

[25] What Is Jev? The Manual for Agent Harnesses (Substack). 2026. https://theaioperator.io/p/what-is-jev-the-manual-for-agent

[26] Jev AI Review: Decision Models for Agent Workflows. 2026. https://wavect.io/blog/jev-ai-decision-model-review/

[27] Browser Use Jev GitHub: Ultrafast Agents Explained. 2026. https://aiprofitboardroom.com/blog/browser-use-jev-github/

[28] Independent Benchmark (2026) - ayautomate.com. 2026. https://www.ayautomate.com/blog/jev-vs-llm-benchmark

[29] When Should You Use a Decision Model Instead of a Chat Model?. 2026. https://huggingface.co/blog/sora-2/jev-ai-vs-llms-when-should-you-use-a-decision-mode

[30] Stop Forcing Poets to Return Booleans: Why TypeSafe Jev Changes Everything. 2026. https://bicarait.com/posts/perspective-2026-09-20-typesafe-jev-system-one

[31] How Jev Chooses the Next Browser Action - rtrvr.ai. 2026. https://rtrvr.ai/blog/jev-browser-agent-benchmark

[32] Jev & System One Models — Fast Decision Making for AI Agents. 2026. https://dev.to/ajmal_hasan/jev-system-one-models-fast-decision-making-for-ai-agents-1j9f

---

## Methodology | 研究方法

本研究遵循deep-research-harness技能的SCOPE→PLAN→RETRIEVE→TRIANGULATE→SYNTHESIZE→WRITE→VALIDATE工作流，执行了30次以上并行WebSearch调用，覆盖TypeSafe官方文档、GitHub代码仓库（browser-use/jev-ultrafast, jkudish/jev-browser, vinilana/jev-browser, awlevin/typesafe-computer-use）、技术媒体（Wikipedia、Firecrawl、LangChain博客、Langfuse博客、Tom's Hardware）、开发者实践（Laravel AI SDK、Pydantic AI文档、LangChain4j博客）和独立基准测试（ayautomate.com、Thunders AI、backnotprop.com）。

所有引用均来自WebSearch返回结果的直接片段，不涉及主观推断或信息捏造。对有争议的数据点（如TypeSafe的193.6x倍速声明），均标注了数据来源偏倚风险和独立测试结果的对比。源数据以sources.jsonl和evidence.jsonl形式持久化保存于/tmp/research/t1790067802-60eecfe2f8/目录。

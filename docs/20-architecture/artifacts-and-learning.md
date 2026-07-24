# GoodJob 产物与学习闭环契约

> 状态：待 Owner 核对  
> 权威范围：定义岗位准备包、简历材料、离线 HTML、项目级批量访谈、模拟面试、复习记录与不可变产物快照的内容和失败行为；不定义扫描算法或 SQLite 实体字段  
> 上游：[产品需求](../10-product/product-requirements.md)、[系统设计](system-design.md)、[证据模型](evidence-model.md)、[扫描与分析](scanning-and-analysis.md)、[ADR-0002](../30-decisions/adrs/ADR-0002-python-and-offline-typescript-dashboard.md)、[ADR-0003](../30-decisions/adrs/ADR-0003-evidence-pointers-without-source-snapshots.md)、[ADR-0004](../30-decisions/adrs/ADR-0004-dynamic-role-lens.md)、[ADR-0007](../30-decisions/adrs/ADR-0007-review-state-lineage-and-snapshot-integrity.md)  
> 下游：[验收基线](../40-delivery/acceptance-baseline.md)

## 1. 产物边界与输入

一次岗位准备围绕一个主岗位进行。它冻结一个 `PreparationRun`：已终态的 `ScanRun`、一个动态 `RoleLens`、所选 `ClaimRevision`、`ProjectContextFact`、覆盖状态、知识缺口和中文主语言。`PreparationRun`、`ArtifactSnapshot`、`ExportAttempt`、`DerivedExport`、`InterviewReview` 的字段与状态以[证据模型](evidence-model.md)为唯一事实源。

- Owner 通过显式 Skill 会话提供工作区、主岗位、可选 JD、可选职级覆盖和可选英文导出请求，并在任何项目衍生信息进入 Codex 分析前确认本会话范围回执；无 JD 不阻断流程，但指定而不可读/不可解码的 JD 必须先修正或显式选择 `continue_without_jd`（`FR-01`、`FR-02`、`FR-05`）。
- 首版主包语言固定为中文。英文简历和英文问答从已成功的中文主快照按需派生，不触发重新扫描，也不得引入新事实（`FR-13`）。
- 只有包含至少一个 `fresh|carried_forward` 且具有 `ProjectSnapshot` 项目的终态扫描运行可以开始岗位准备。`partial` 扫描允许生成带缺口的 `partial` 准备包；`failed_no_baseline|excluded` 项目只进入 Coverage，不评分；`failed` 或 `interrupted` ScanRun 不能发布新 `ArtifactSnapshot`（`FR-15`、`NFR-05`）。
- Markdown、HTML 和 manifest 必须由同一个冻结 `ReportBundle v1` 生成。渲染阶段不得重新发现项目、重新分析源码或改变 Claim（`FR-12`、`NFR-04`）。

## 2. 中文完整岗位准备包

中文主包是首版的完整学习与面试材料，必须按“先岗位、后项目、再证据”的阅读顺序组织（`FR-08`、`FR-09`）：

1. **岗位总览**：岗位名、职级、JD 来源或假设、RoleLens 维度与权重、覆盖范围、关键限制、能力地图和跨项目排序理由。
2. **项目排序与章节入口**：每个项目的岗位相关度、所用工作树/快照状态、核心模块、已覆盖与未覆盖范围；排序不得丢掉项目、模块与 Evidence 的回溯链路。
3. **项目/模块章节**：业务问题与领域对象、技术栈和实际使用证据、架构和边界、实现方式、测试/运行线索、关键取舍、可讲解难点、可复习知识点、知识缺口与候选追问。
4. **简历材料**：按岗位筛选的项目经历、可编辑 Markdown bullet、STAR 素材和每条关键表述的证据来源。
5. **面试材料**：两分钟项目讲解、分层题库、追问路径、薄弱点、复习状态和下一次复习日期。

“完整”不表示扫描结果毫无缺口。任何 `carried_forward`、`failed_no_baseline`、`ScanIssue`、stale/missing Evidence、未支持语言或未回答上下文问题必须出现在岗位总览与受影响项目章节中；不得由漂亮叙事掩盖覆盖限制（`FR-15`）。

## 3. 证据、叙事与个人归因

### 3.1 统一追溯

每个关键 Claim 在 Markdown 和 HTML 中必须能返回：项目、worktree/分支（如存在分支差异）、模块（如适用）、Evidence 类型、相对路径/locator、内容哈希或 commit locator、`commit_state`、时效与 `facets`。相同内容可以折叠展示，但必须能展开全部 worktree 来源；展示不能复制完整源码或 diff。

报告只使用该 PreparationRun 冻结的 Claim/Evidence。`current` 证据可支持当前实现；`stale`、`missing` 或已 supersede 的上下文只能作为历史或限制信息显示，不能单独支撑新的强结论。计划和设计文档只能以“已规划/已文档化”呈现（`FR-11`、`NFR-02`）。

### 3.2 能力、实现、学习、贡献与结果的表述

GoodJob 不按 Git 作者排除项目代码；任何授权项目中的实现都可以成为可学习、可讲解的材料。但产物必须按以下证据门槛选择措辞：

| 叙事类型 | 可输出的可靠表述 | 证据门槛 |
| --- | --- | --- |
| 能力与候选学习 | “我能解释/可复习该实现方式”“从该模块可系统复习……” | 当前或明确标旧的 implementation/architecture Evidence；必须标示时效 |
| 个人学习复盘 | “我当时从项目中学到了……” | 当前 ProjectContextFact(learning) + 相关项目 Evidence；无上下文时只输出候选学习要点 |
| 客观实现方式 | “该项目/模块通过……实现……” | implementation Evidence；未提交实现须展示 working-tree 状态 |
| 我实现 | “我实现了……” | implementation Evidence + 当前项目级 role/ownership 上下文或可核验的个人角色 Evidence；未提交实现须展示 working-tree 状态 |
| 我负责/主导 | “我负责/主导……” | 当前项目级 role/ownership 上下文；Git 作者、目录位置或模型推断均不足够 |
| 客观项目结果 | “项目达到/呈现……结果或指标” | 当前 `ProjectContextFact(outcome\|metric)` 或可核验的项目结果 Evidence；不得从代码符号、文档标题或 Git 记录虚构数字 |
| 个人结果与指标 | “我推动/取得……结果或指标” | 客观项目结果证据 + 当前 role/ownership 上下文或可核验个人角色 Evidence；两者缺一时不得做个人归因 |

“学习要点”和“如何实现”是必备章节：前者形成 `learning` Claim，后者形成 `implementation_method` Claim，并且都要关联 Evidence。缺少个人学习/角色上下文时，系统仍提供候选学习、能力叙事与客观实现讲解，不逐条要求 Owner 认领或确认 Claim（`FR-07`、`FR-11`）。

## 4. 项目级批量访谈

当 RoleLens 发现下列信息会实质影响简历或面试材料而证据不足时，准备流程进入 `awaiting_context`：业务目标/目标用户、Owner 的角色与责任范围、可量化结果、关键取舍或个人学习复盘。它按项目聚合为一次紧凑的批量问题卡，而不是围绕每个 Claim 逐题确认（`FR-10`）。

- 同一次准备运行中，每个存在关键缺口的项目最多出现一张上下文卡；卡内可包含多个缺失维度。
- Owner 可以完整回答、仅回答部分或跳过。部分和跳过都不阻断后续分析，系统相应降级叙事并创建可见 `KnowledgeGap`。
- 回答以 `ContextAnswer`、`ProjectContextFact` 和对应 user-statement Evidence 独立持久化；refresh 不得覆盖它们。之后的准备运行复用当前回答，只有 Owner 明确修订时才 supersede。
- 批量访谈不能要求上传源码、不能把自由文本复制到共享 Skill 或仓库，也不能把回答自动提升为代码已实现事实。

## 5. 简历、快照与语言导出

### 5.1 可编辑 Markdown 与不可变快照

每次成功渲染都在独立 `ArtifactSnapshot` 中写入冻结的 `report.zh-CN.md`（完整准备包）、`resume.zh-CN.md`（简历源稿）、入口 HTML、可选本地 assets 和 manifest。GoodJob 对已发布快照只追加、不修改；`latest.json` 只在所有必需文件成功生成并校验 manifest 后原子指向最新中文 `completed` 或带明确缺口的中文 `partial` 主快照。

两个快照 Markdown 都是普通文本但受快照不可变约束。用户编辑必须发生在从 `resume.zh-CN.md`（或明确选择的报告章节）显式导出的 `drafts/` 工作稿中；目标已存在时默认拒绝覆盖，只有 Owner 明确选择替换/合并才可写入。新的 prepare 只生成新主快照，不自动同步人工工作稿（`FR-12`、`NFR-04`、`NFR-06`）。

每个 snapshot manifest 至少引用：`preparation_run_id`、报告契约版本、canonical `report_bundle_sha256`、RoleLens、所用 ScanRun/ProjectSnapshot disposition、Claim/Evidence 版本、覆盖与问题摘要、ReviewTargetBinding/ReviewSubjectProjection hash/InterviewReview 截点、英文 source-item 投影 hash、语言和生成器版本。它不包含源码全文、完整面试对话或 SQLite 内部表结构。

### 5.2 英文按需导出

默认只生成中文主包。`ReportBundle v1` 为每条可导出的中文简历 bullet 和问答建立稳定 `source_item_id`，并附相关 Claim/Evidence/RoleLens 引用、数字与单位、技术标识、实现/测试状态、角色和结果锚点。Owner 明确请求英文时，从指定 `ArtifactSnapshot` 的冻结 source item 创建不可变 `DerivedExport`，仅包含英文简历和英文问答。

对于请求的每个 export kind，导出 manifest 的 source item 与 target item 必须一一映射、集合完全相等；数字、单位、技术标识、状态、角色与结果锚点规范化后必须相等。该校验用于阻止漏译、增项和结构化事实漂移，但不宣称能形式化证明全部自然语言语义。

翻译候选先在当前 task 内存中完成，不写盘。首次写盘前，单个短生命周期发布子进程取得锁并持久化 `ExportAttempt(running)`、owner PID+启动标识、`exports/.tmp/<export-attempt-id>/` 与唯一 final path。它只写 temp；全部通过后原子改名，再在持锁事务中创建 `DerivedExport` 并标 succeeded。失败标 failed；崩溃后只有确认 owner process identity 已不存在，恢复才标 interrupted，并按账本清理 temp 或无 DerivedExport 的预登记 final path。其他 attempt、成功导出和未知目录不得触碰；重试创建新 attempt。导出不重新扫描、不读取数据库当前态、不生成英文 HTML，也不修改源快照或 `latest`（`FR-13`）。

## 6. 离线 HTML 看板

HTML 是与 Markdown 同源的阅读看板，不是服务端应用。每份报告数据内嵌在入口 HTML 中；脚本、样式、字体和图标可以内联，也可以作为同一不可变产物目录中的本地静态资源。页面不得通过 HTTP、WebSocket、`file://` JSON 请求、CDN、远端字体或分析脚本读取额外数据（`NFR-03`）。

项目名、路径、JD、用户回答、Claim、Evidence 摘要和 Markdown 均是不可信数据。报告 JSON 嵌入 HTML 时必须安全编码 `<` 等可提前结束元素的字符；前端使用文本节点或等价安全 API 渲染，不使用未净化 `innerHTML`，不执行 Markdown 原始 HTML、事件属性或 `javascript:` URL。产物必须配置阻断网络、对象、frame、表单和任意脚本执行的 CSP；只允许按 hash 或本地产物路径加载版本化前端代码，不允许 `eval`（`NFR-08`）。

离线看板必须提供：

- 岗位总览、假设、覆盖和缺口提示；
- 项目/模块导航、岗位排序和筛选；
- Claim/Evidence 展开、时效和 commit state 呈现；
- 知识缺口、题库入口与已保存复习状态；
- 长文本可读、窄窗口无横向遮挡的布局。

看板只读展示 `ReportBundle` 投影，不直接打开 SQLite、不写复习状态、不扫描源码。任何复习状态修改都经 Skill/Python 接口落库后，下一份快照才会反映它。HTML 渲染失败、资源缺失、报告契约版本不匹配或发现远端依赖时，该运行不得发布为成功快照（`FR-12`、`FR-14`、`NFR-03`）。

## 7. 模拟面试与复习闭环

模拟面试基于已完成的 PreparationRun、RoleLens 问题策略和 KnowledgeGap 生成问题。可覆盖项目讲解、实现方式、架构、取舍、故障处理、业务理解和行为题，但题目必须能回指对应项目/模块/Claim 或明确标为通用岗位基础。

每个问题先绑定稳定 `ReviewTarget`：目标锚定 `Claim.claim_id` 或版本化 topic key，不以题面相似度识别。Repository 从 `ReviewSubjectProjection` 计算 fingerprint，不直接使用 ClaimRevision/Gap ID。纯 statement 改写、展示顺序、行号或等价 Evidence 替换保持连续；概念、机制、行为、取舍、facet/support/conflict/evidence-validity、角色/结果锚点或 gap 状态变化时显示历史但当前掌握度为“需重评”。无法证明等价时保守重评；不同项目/作用域/主题不得合并。

一轮模拟结束后，只追加 `InterviewReview`：ReviewTargetBinding、题目 ID、结构化摘要、掌握等级、薄弱点和可选 `next_review_at`。不保存完整逐字对话、音频或未结构化回答，也不创建定时任务、通知或主动提醒。看板只显示当前快照已冻结的复习状态和日期；新复盘经 Skill/Python 持久化后，Owner 显式创建可复用同一 ScanRun 的新 PreparationRun 才会生成新看板，旧快照不改写（`FR-14`、`NFR-06`）。

## 8. 渲染失败与可见降级

- **无可用扫描快照**：创建失败的 PreparationRun 诊断，不生成 ArtifactSnapshot，不改动 `latest.json`。
- **扫描为 partial**：允许生成 `partial` 产物，但首屏和受影响章节必须列出项目 disposition、问题、影响与补救动作。
- **关键证据失效或冲突**：不把它写成当前强结论；改为历史限制、知识缺口或要求 refresh。
- **准备阶段源码漂移**：preflight、读取前或提交前发现缺失、不可读或哈希失配时，PreparationRun 终止为 `refresh_required`；不隐式 refresh、不发布半成品，并保留既有 `latest`。
- **上下文未回答**：生成能力/学习叙事与客观实现说明，避免“我实现/负责/主导/取得结果”等强个人表述。
- **Markdown 或 HTML 任一必需渲染失败**：追加失败 RenderAttempt，清理临时目录，不发布 ArtifactSnapshot、不更新 `latest.json`，保留上一次成功或 partial 快照；可对同一冻结分析集重试，不重新生成 Claim。
- **英文按需导出失败**：只报告该导出失败，不回滚或篡改原中文快照。
- **英文导出进程中断**：保留 ExportAttempt 诊断；恢复只清理它预登记且无成功 DerivedExport 引用的 temp/final path，重试使用新 attempt。

上述降级必须可在运行结果、manifest 和下游看板中识别；不得用空章节、默认评分或静默省略伪造完整材料（`FR-15`、`NFR-05`）。

## 9. 可判定验收规则

| ID | 可判定输入 | 必须输出 | 失败或降级行为 | 需求映射 |
| --- | --- | --- | --- | --- |
| `ART-01` | 岗位名、有/无 JD、可选职级覆盖 | 一个冻结单岗位 RoleLens，含假设、排序和问题策略 | 无 JD 继续并显示假设；不创建多岗位混合报告 | `FR-01`、`FR-02`、`FR-05`、`NFR-07` |
| `ART-02` | 可用 ScanRun 与 RoleLens | 中文主包含岗位总览、项目排序、项目/模块、学习/实现、简历、题库和缺口 | partial 时完整呈现覆盖限制，不宣称全量理解 | `FR-08`、`FR-09`、`FR-15` |
| `ART-03` | 多项目、多个模块且岗位相关度不同 | 项目先排序，所有材料仍可回到项目、模块、Claim 与 Evidence | 无模块边界时标为项目级，不虚构模块 | `FR-06`、`FR-09`、`FR-11` |
| `ART-04` | 非本人提交、无角色上下文、未提交实现和有项目级上下文的混合证据 | 学习/如何实现可讲解；个人贡献与结果按证据门槛分层 | 不自动把 Git 作者或代码存在写成主导、负责或指标 | `FR-07`、`FR-11`、`NFR-02` |
| `ART-05` | 项目缺业务、角色、指标或学习上下文 | 每项目一次批量问题卡；回答独立可复用 | 跳过不阻断，形成 KnowledgeGap 与降级措辞 | `FR-10`、`FR-15`、`NFR-06` |
| `ART-06` | 任一关键 Claim，包含 current/stale/missing/plan 等证据 | Markdown 与 HTML 均展示相同的证据追溯、facet、commit state 与限制 | stale/missing/plan 不得单独支撑当前实现或强归因 | `FR-11`、`NFR-02`、`NFR-04` |
| `ART-07` | 同一岗位连续两个 ready PreparationRun；第三个运行渲染中途失败；用户已有工作稿 | 每次尝试有 RenderAttempt；每个运行至多一个成功 ArtifactSnapshot；成功原子更新 latest；可显式导出工作稿 | 失败清理半成品且可对第三个运行重试同一分析集，不更新 latest、不覆盖人工工作稿 | `FR-12`、`NFR-04`、`NFR-06` |
| `ART-08` | 指定中文快照并请求英文 | 先创建 ExportAttempt，再发布不可变 DerivedExport，包含同一事实源的英文简历和问答 | 不生成英文 HTML；成功或失败均不改源快照/latest；不重新扫描或补造事实 | `FR-13`、`NFR-04` |
| `ART-09` | 断网、无服务直接打开 HTML | 本地可用的导航、筛选、证据展开、缺口和复习状态阅读 | 外部资源、网络请求或 file JSON 依赖使渲染失败 | `FR-12`、`FR-14`、`NFR-03` |
| `ART-10` | 完成一轮模拟面试 | 追加题目、摘要、掌握度、薄弱点和复习日期 | 不持久化完整对话，不创建主动提醒 | `FR-14`、`NFR-06` |
| `ART-11` | 无权限/损坏项目、未知语言、carried-forward 快照 | 生成可用 partial 包，列出原因、影响、补救动作 | 没有任何可用快照时不发布新产物 | `FR-15`、`NFR-05` |
| `ART-12` | 报告字段含 `</script>`、事件属性、原始 HTML、外部 URL 或提示注入文本 | Markdown 与 HTML 将其作为可读文本/安全链接提示呈现 | 不执行脚本、不发网络请求、不改变 Skill 行为；安全编码或 CSP 失败则不发布快照 | `NFR-03`、`NFR-08` |
| `ART-13` | 同一 ReviewTarget 分别发生纯文案/等价证据变化与概念/facet/角色锚点/gap 状态变化 | 前者 subject fingerprint 不变并延续；后者显示历史并标“需重评” | 不直接 hash Revision/Gap ID，不按题面相似度或跨项目合并 | `FR-14`、`NFR-04` |
| `ART-14` | 英文源项含数字、单位、技术名、状态、角色与结果锚点，并分别制造漏项、增项和锚点漂移 | 合法导出一一映射且 manifest 可机检 | 任一集合/锚点不等都原子失败，不改变中文快照或 latest | `FR-13`、`NFR-04` |
| `ART-15` | preflight、读取前或 commit 任一阶段修改/删除/收回文件权限 | PreparationRun 为 `refresh_required` 且报告明确要求显式 refresh | 不隐式刷新，不创建 Evidence/Claim/Assessment/Artifact，不更新 latest | `FR-06`、`NFR-04`、`NFR-05` |
| `ART-16` | ScanRun 同时含 fresh、carried-forward、failed-no-baseline 与 excluded 项目 | 只对前两类评分，后两类在 Coverage 中展示；排名连续可复算 | 合资格项目为空则失败；不得以默认分数伪造全项目排序 | `FR-08`、`FR-09`、`FR-15` |
| `ART-17` | 英文导出分别在写 temp、原子改名后、数据库提交前中断，并同时存在既有成功导出和未知目录 | ExportAttempt 变为 interrupted；只清理其预登记 temp 或无 DerivedExport 的 final path | 不扫描/删除其他 exports；旧成功导出与 latest 不变；重试创建新 attempt | `FR-13`、`NFR-05` |

## 10. 非首版边界

首版不做多岗位并排比较、主动复习提醒、完整面试录音/对话留存、需要常驻服务的交互式 Dashboard、公开 GitHub 发布或独立桌面应用。新语言的深读能力也不在产物渲染阶段临时补做；它必须先进入扫描适配器与验收契约。

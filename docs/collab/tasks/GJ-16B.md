# 任务卡 GJ-16B：CodeRoute / SliverShield 真实工作区只读验收

- 状态：待领取，GJ-16A 前置已满足 ｜ 实现：Sol-Impl ｜ 出卡/评审：Sol
- 对应 backlog：里程碑 M2 · 批次 G · GJ-16B
- 前置任务：GJ-16A 已通过信道 #62 验收并以 merge `b7be588` 合入，`IMP-03` 证据裁定已完成
- 分支：`task/GJ-16B-real-workspace-readonly-acceptance`，仅在后续派卡消息指定基线后创建
- **验收强度**：对抗（[protocol §2.2](../protocol.md)）——真实仓库包含未提交/未跟踪内容、项目文档、提示注入面和大量生成目录；验收必须证明只读、证据不越权且不把计划冒充实现

> 本卡必须由独立信道消息正式派发。Implementer 在收到该消息前不得读取 CodeRoute/SliverShield；收到后按消息指定基线与本卡契约执行。

## Owner 输入与目标

`OWN-01` 已裁定：已清理的 CodeRoute 临时工作树属于过渡状态，不再作为真实对象；多工作树能力只用合成测试验收。GJ-16B 对 Owner 在派卡时指定的两个真实工作区执行首版 Skill 的完整只读流程，验证[验收基线 §4](../../40-delivery/acceptance-baseline.md)仍保留的 CodeRoute 三项和 SliverShield 四项，并形成可供 `OWN-03` 人工查看的中文 Markdown/离线 HTML 产物。

当前候选执行输入为 `/Users/damien/Projects/CodeRoute` 与 `/Users/damien/Projects/SliverShield`；它们只属于本次任务输入，不回写为验收基线固定路径。派卡前 Architect 必须再次只读确认 Owner 未替换路径。

## 涉及范围

- **允许写入**：
  - 新增 `docs/40-delivery/real-workspace-acceptance.md`，只记录脱敏结构化证据、哈希、计数、结论和未通过项，不复制源码/JD/上下文原文；
  - 一个由 `mktemp -d` 创建、位于仓库外的临时 GoodJob data directory；保留到 Architect 验收与 Owner 视觉核对结束，再由 Architect 明确收口；
  - `docs/collab/channel.md` 物理 EOF 追加协作消息（元协议豁免）。
- **只读输入**：Owner 派卡时指定的 CodeRoute 与 SliverShield 根；允许按 Skill 契约打开已通过 `verify_source_revision` 的少量源码和文档。
- **禁止**：修改/暂存/提交真实工作区任何文件；运行项目代码、测试、构建、包安装、hook、fetch/checkout/clean/gc；访问网络；修改 GoodJob runtime、测试、schema、前端、依赖、权威契约、backlog 或用户级安装；把真实源码、完整 diff、密钥、环境值或个人上下文写入仓库。

## 固定执行流程

1. Implementer 必须显式使用仓库内 `goodjob-career-review` Skill，不得绕过 Skill 直接把全仓交给模型。使用当前候选 GoodJob runtime、单一临时 data directory、一个 `架构师` 验收镜头和无 JD 输入；该岗位仅用于产品验收，不代表 Owner 的求职选择，RoleLens assumptions 必须明确这一点。
2. 每个真实工作区分别取得授权、校验输入并扫描；不得复用另一工作区的 receipt、validation digest 或运行绑定。先读 bounded EvidenceBundle，再按建议选择少量高价值文件，逐项 `verify_source_revision(phase=before_read)` 通过后才能打开。
3. 如果业务目标、角色、结果、取舍或学习上下文存在实质缺失，按 Skill 一次生成项目级批量 context cards。Implementer 只把结构化问题与可选 `answered/partial/skipped` 边界报给 Architect；Architect 必须在当前 Codex 对话直接询问 Owner，Owner 不负责读信道。收到 Architect 转交的 Owner 原话前不得虚构回答、个人贡献、学习或指标。
4. Owner 对任一项目 partial/skipped 时，分析必须建立可见 open KnowledgeGap。客观技术/业务 Claim 可由代码、文档、测试和迁移 Evidence 支撑；个人 Claim 只能使用 Owner 实际回答形成的 ContextEvidence。计划、测试定义、迁移与当前实现必须按证据类型和 facet 分开。
5. 对每个 workspace 原子 `record_analysis` 并 render 中文快照；英文导出、模拟面试复盘与复习写入不属于本卡。直接以 `file:` 打开返回的单文件 HTML，不启动本地服务、不请求外部资源。

## 只读与隐私证明

对两个真实工作区分别在首次 GoodJob 读取前和最后一次读取后采集同一组只读基线：

- canonical root、当前 branch 与 HEAD；
- `git status --porcelain=v2 --untracked-files=all` 的 SHA-256、行数与状态类别计数；
- 已存在的 dirty/untracked 内容只记录计数和哈希，不在仓库证据文档复制路径或内容。

前后 branch、HEAD、status hash 与计数必须逐项相同。Git 命令本身不得触发写操作；若仓库在执行中由外部主体变化，停止该 workspace 验收，记录 `source_changed`/`refresh_required` 现场并等待 Architect 裁定，不把变化归因给扫描器。

临时 data directory 与产物不得位于 GoodJob 或真实工作区内。提交前递归检查 GoodJob diff 与两个输入仓库状态；仓库证据文档只允许 Evidence locator、短摘要、ID、hash 和计数，不含源码正文、完整 diff、密钥、环境变量值、原始 SessionCapability 或 Owner 回答原文。

## 真实验收判据

### CodeRoute

- 模块清单必须区分 pnpm workspace、Tauri/Rust、React/TypeScript、内容工具和课程内容的角色；不得只列技术名而不说明模块职责。
- 计划中的服务端能力只有文档/plan Evidence 时，只能标为 planned/documented 或 KnowledgeGap，不能写成 implemented/test_verified。
- `node_modules`、`dist`、Rust `target` 及等价构建/依赖目录不得生成 SourceRevision/Evidence；Coverage 应说明排除类别。

### SliverShield

- 模块清单必须区分 Flutter 移动端、Python API、数据库迁移与基础设施角色。
- 扫描开始时已存在的 modified/untracked 非忽略代码或文档可以入证据，但必须保留 working-tree commit state；不得暗示已提交或已发布。
- `.venv`、Flutter `build`、`.dart_tool`、本地运行数据与环境配置不得进入 SourceRevision/Evidence。
- 文档、测试定义、数据库迁移与运行代码必须使用各自 Evidence kind/facet；任一类型不能单独证明另一类型已经实现或测试通过。

## 证据文档契约

`real-workspace-acceptance.md` 是验收记录，不是产品契约，至少包含：

1. GoodJob 候选 commit、Skill/runtime 版本、执行日期、岗位镜头与无 JD assumption；
2. 每个 workspace 的 Owner 指定根、只读前后 branch/HEAD/status digest 对账，不复制 dirty 路径；
3. ScanRun/PreparationRun/ArtifactSnapshot ID、终态、Coverage 与 ScanIssue 摘要、产物文件名和 SHA-256；临时绝对产物路径只放信道，不写仓库文档；
4. 上述 CodeRoute 三项与 SliverShield 四项逐条 `pass / fail / blocked`，每项至少一个结构化 Evidence/Module/ScanIssue locator；
5. 全部 partial/skipped context 与 open KnowledgeGap 数量；个人 Claim 数量及其 ContextEvidence 门禁结果，不保存 Owner 原话；
6. 未通过项的影响与补救。产品缺陷必须对应信道 L1，不得在本卡修 runtime。

## 验收标准（DoD）

- [ ] 两个 Owner 指定真实工作区均通过显式 Skill 流程扫描；无硬编码已清理 CodeRoute worktree，也未创建替代 worktree。
- [ ] 两个工作区的前后 branch、HEAD、status digest/计数完全一致；GoodJob 仓库除允许证据文档和信道外无变化。
- [ ] CodeRoute 三项、SliverShield 四项逐条有结构化证据与明确结论；任何静默漏扫或证据类型冒充均失败。
- [ ] 所有源文件打开前均有同一运行的 `before_read` 通过记录；出现 drift 时按 `refresh_required` 停止，不沿用旧结论。
- [ ] Owner 上下文只经 Architect 在当前对话直接批量询问；未回答项形成 KnowledgeGap，绝不由 Implementer 代答。
- [ ] 两份中文 snapshot 均原子发布，manifest 与文件 hash 可复算；离线 HTML 以 `file:` 打开且零外部请求，产物留给 `OWN-03`。
- [ ] 临时 data directory、仓库证据和信道均不含完整源码/diff、密钥、环境值、capability 或 Owner 回答原文；真实源码副本不进入 Git。
- [ ] `make gate-release` 在未修改 runtime 的候选 HEAD 全绿；交付报告给出动态计数和真实扫描终态。
- [ ] 若发现产品缺陷，立即 L1 停工并保留最小只读现场；不在真实验收卡顺手修产品或改契约。

## 交付与暂停点

Implementer 的第一次预期暂停点是 context cards：在信道发送卡片摘要后等待 Architect 直接向 Owner 提问并转交回答，这不是让 Owner 读信道。最终交付包含候选证据文档 commit、两个 workspace 的只读对账、七项判据、产物路径/hash、Coverage/ScanIssue、KnowledgeGap、完整门禁、隐私检查及全部实施发现。

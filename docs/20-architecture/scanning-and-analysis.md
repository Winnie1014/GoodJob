# GoodJob 扫描与岗位化分析契约

> 状态：待 Owner 核对  
> 权威范围：定义授权工作区的发现、索引、增量刷新、Git 历史读取、语言适配和 Codex 按证据深读的行为；不定义 SQLite 实体字段或最终报告章节  
> 上游：[产品需求](../10-product/product-requirements.md)、[系统设计](system-design.md)、[证据模型](evidence-model.md)、[ADR-0003](../30-decisions/adrs/ADR-0003-evidence-pointers-without-source-snapshots.md)、[ADR-0005](../30-decisions/adrs/ADR-0005-local-first-discovery-and-degradation.md)  
> 下游：[产物与学习闭环](artifacts-and-learning.md)、[验收基线](../40-delivery/acceptance-baseline.md)

## 1. 目标与边界

扫描的目标不是让 Codex 无边界地阅读全部源码，而是把 Owner 明确授权的工作区转换成可增量维护、可定位、可按岗位重排的证据目录。扫描器只做确定性观察：发现项目和模块、提取结构化证据、记录覆盖范围及问题。岗位价值、个人经历和面试叙事由 `RoleLens` 与准备流程在此基础上形成。

- 首版只扫描 Owner 明确提供、在本机可读的工作区根；不递归扫描未授权位置，不触发网络抓取或 Git fetch。工作区内 `.git` 指针可授权扫描器读取 3.2 定义的根外受限 Git 元数据，但不授权读取根外源码、配置或模块（`FR-02`、`NFR-01`）。
- 扫描产物写入证据模型中的 `ScanRun`、`ProjectSnapshot`、`SourceRevision`、`Evidence` 与 `ScanIssue`；字段、身份和状态以[证据模型](evidence-model.md)为唯一事实源。
- 扫描器不保存源码正文、完整函数、完整 diff 或大段文档。原文件仍是实现事实源，数据库只保存指针、哈希、短摘要和结构化状态（`NFR-02`）。
- 文件内容、manifest、Git 文本和路径名一律是不可信数据。适配器只能解析字节/文本并调用参数化的只读 Git 子进程，不得 import 或执行项目模块，不得运行构建脚本、包管理器或 shell 拼接，也不得把仓库中的指令当作 Skill 指令（`NFR-08`）。
- 本文中的“项目”指证据模型的 `Project`，“工作树”指 `Worktree`，“快照”指不可变 `ProjectSnapshot`；不得以目录名或远端 URL 替代其稳定身份。

## 2. 运行契约

### 2.1 输入与终态

| 操作 | 必需输入 | 成功输出 | 不能建立时的行为 |
| --- | --- | --- | --- |
| `scan` | 规范化前的 `workspace_path`、当前 `config_revision` | 一个终态 `ScanRun`、`Workspace` 身份、每个发现项目的 `ScanRunProject`、覆盖摘要与 `ScanIssue` | 路径不存在、不可读、不是目录或越出 Owner 授权边界时，运行终态为 `failed`；不得创建或替换任何可用项目快照 |
| `refresh` | 已登记的 `workspace_id`、当前 `config_revision` | 一个新的终态 `ScanRun`；未变内容复用既有 revision，变化内容新增 revision | 工作区登记不存在或根不可重新访问时，运行终态为 `failed`；历史快照和既有 `latest` 产物保持不变 |
| 岗位证据查询 | 已终态的 `scan_run_id`、`role_lens_id`、可选项目/模块/证据类型过滤 | 有界 `EvidenceBundle`，带项目排序、时效状态、覆盖与深读建议 | 不存在可用 `ProjectSnapshot` 时返回明确的无可用证据结果，不以空包伪装为完整分析 |

`scan` 是首次登记或强制全量索引，`refresh` 是 Owner 显式触发的增量操作（`FR-03`、`FR-04`）。首版没有文件监听、定时扫描或后台服务。空工作区可以完成一次零项目扫描，但空目录本身不得被登记为项目。

### 2.2 事务、快照与降级

每个项目必须在独立 SQLite 事务中处理，并遵守[证据模型 §6.2](evidence-model.md)的 `ScanRunProject.snapshot_disposition`：

- 项目成功索引，创建新 `ProjectSnapshot`，记为 `fresh`。
- 项目本次失败但已有成功基线，引用最近基线，记为 `carried_forward`，同时写入本次 `ScanIssue`；该数据不得在输出中称为 fresh。
- 项目本次失败且没有基线，记为 `failed_no_baseline`，不创建空快照。
- Owner 配置明确排除的项目记为 `excluded`，并记录命中的规则来源。

至少一个项目存在可用快照时，扫描可终态为 `completed` 或 `partial`；有任一 `carried_forward`、`failed_no_baseline` 或未解决覆盖问题时必须为 `partial`。无可用项目快照或工作区无法建立时为 `failed`。失败项目不得删除、回写或污染上一份成功快照（`FR-15`、`NFR-04`、`NFR-05`）。

## 3. 项目发现与隔离

### 3.1 探索顺序

1. 将工作区根解析为规范化实路径；普通目录符号链接只在目标仍位于该授权根内时跟随。指向根外的普通链接不被读取，并产生可定位的 `ScanIssue`；工作区内 `.git` 指针对受限 Git 元数据的授权例外按 3.2 执行。
2. 在普通 ignore 生效前发现 `.git` 目录与 `.git` 指针文件。硬安全排除仍优先，避免进入依赖、构建和密钥区域。
3. 对每个候选 Git 根读取本地 Git 元数据；先确定独立仓库，再按各自仓库规则扫描。父仓库的 `.gitignore` 不得吞掉内层 Git 仓库。
4. 用规范化 `git common-dir` 归并同一 Git 项目的主工作树和 linked worktree；每个实际工作树保留单独的根、分支、HEAD 与 dirty observation。
5. 对不处于已发现 Git 项目内的剩余目录，识别可靠的非 Git 项目根；已发现项目内的 manifest 用于划分 `Module`，不重复变成第二个 `Project`。

Git 元数据损坏时，该候选项目产生 `broken_repository` 类 `ScanIssue`；扫描器不得把其目录悄悄降格为非 Git 项目，也不得中止其他项目。

### 3.2 Git 项目、嵌套仓库与工作树

- Git 项目的身份是 `git common-dir` 的规范化实路径。同一 common-dir 只生成一个 `Project`，即使工作区中存在多个 linked worktree（`FR-03`）。
- `WorktreeObservation` 记录每个工作树在本次 `ScanRun` 的 branch、HEAD 和 dirty state。项目归并只去重 Project 身份；相同内容可按 `content_equivalence_key` 复用解析并折叠展示，但必须保留各自来源。不同分支的当前状态绝不混写为同一份实现事实。
- 若 linked worktree 的 common-dir 位于授权根外，工作区内 `.git` 指针只授权只读获取：指针/commondir、HEAD/ref、index 状态，以及第 5.2 节允许窗口内的 commit hash、时间、作者、标题和变更路径名。不得请求或持久化 blob 内容、diff 正文，不得遍历该 common-dir 的其他工作树或读取根外工作树源码、配置和模块；覆盖报告必须标明使用了此受限元数据例外。
- 内层 Git 根是独立项目。它的子树从父项目源码遍历中排除，避免同一文件同时归属父、子两个项目。

### 3.3 非 Git 项目与模块

首版的非 Git 项目发现以以下根级标记为准：`package.json`、`pyproject.toml`、`Cargo.toml`、`pubspec.yaml`、`go.mod`、`pom.xml`、`build.gradle`/`build.gradle.kts`、`*.sln`、`*.csproj`、`Package.swift`、`CMakeLists.txt`。必须至少存在一个标记文件，普通目录、文档目录和空目录不能成为项目。

模块边界优先由 Git 项目的 workspace manifest、语言 workspace 配置、服务/应用 manifest、数据库迁移根和明确的 build/test 配置给出。无法由这些证据确认的目录只作为文件集合，不创建虚假的 `Module`。模块记录本次 `ProjectSnapshot` 的边界，随快照版本化（`FR-03`、`NFR-07`）。

## 4. 文件选择、安全排除与事实优先级

### 4.1 忽略规则

发现嵌套 Git 根后，扫描器对每个独立项目分别应用其 `.gitignore`、项目工具 ignore 和 Owner 配置的普通忽略规则。忽略只影响该项目中的普通文件纳入，不影响已经确认的内层仓库身份。

以下类别在任何普通扫描中硬排：

- Git 内部对象与 hooks、依赖和包缓存目录；
- 构建产物、测试覆盖产物、临时目录和运行时缓存；
- 实际环境变量文件、私钥、证书私钥、凭据存储和常见密钥命名文件；
- 二进制、大型生成物或无法安全解码的文件。

Owner 可在个人 `config.toml` 中登记少量、精确到相对文件路径的安全例外，以纳入被普通 ignore 或结构性排除误伤的示例、fixture 或源码。例外不得是目录通配，不得把实际环境变量、私钥或凭据重新纳入；`*.example`、fixture 等非秘密样本必须由精确路径和理由确认。排除类别、例外命中与未读原因均应进入覆盖摘要（`NFR-01`、`FR-15`）。

### 4.2 当前工作树是实现事实源

事实强度按来源区分，具体 `Evidence`、`ClaimRevision.facets` 与 `EvidenceValidity` 定义以[证据模型 §5](evidence-model.md)为准：

- 当前可读工作树中的实现、迁移或可执行定义可以支持 `implemented`；测试源码/配置在同时存在实现支持时只支持 `test_defined`。只有能关联相关 revision/commit 的通过结果元数据才支持 `test_verified`；GoodJob 不通过运行测试来创造该状态。
- manifest 只能说明声明的工具或依赖；只有 import、调用、配置接线或运行入口等额外证据才能写成“实际使用”。
- 文档、设计和任务计划只能支持 `documented` 或 `planned`，不得自动升级为 `implemented`、`test_defined` 或 `test_verified`。
- 已修改和未跟踪的非忽略文件同样可入证据库，但其 `commit_state` 必须分别为 `modified` 或 `untracked`，不得伪装成已交付历史。
- Git 作者和提交范围只提供检索线索，不能单独推出 Owner 的负责、主导或结果。
- “当前工作树优先”表示所有授权范围内的当前 worktree 都优先于 Git 历史描述，不表示任意挑一个分支覆盖其他分支。多 worktree 事实一致时可形成 project-scope Claim；不一致时必须按 worktree 分开或显式标冲突。

每个源码型 Evidence 必须持有相对工作树路径、`SourceRevision`、内容哈希和定位器；摘要只说明为什么该位置有意义，不能复制源码正文。准备新材料前，使用者必须按当前 `ScanRun` 解析 `current`、`stale` 或 `missing` 时效；旧摘要不得静默证明当前实现（`FR-11`、`NFR-02`）。

## 5. 全量索引、增量刷新与 Git 历史

### 5.1 初始索引与 refresh

首次 `scan` 对全部合资格文件建立内容身份。`refresh` 先比较项目发现结果、Git HEAD/dirty 状态、文件路径集合、文件元数据和分析器/配置版本；候选变化文件重新计算 `content_sha256` 与 `analysis_fingerprint`。相同内容、适配器版本和配置版本的 Evidence 复用既有不可变 revision；任一项变化均追加新 revision，绝不原地修改历史。

删除路径在新快照中标为 `missing`。同项目内移动文件优先使用本地 Git rename 信息；无法获得 rename 信息时，仅在内容哈希相同的情况下写入 `supersedes_artifact_id` 作为移动线索，仍保留旧路径的历史引用。没有变化时不得重新解析全部源码（`FR-04`、`NFR-04`）。

每次 refresh 都必须重新检查敏感排除和嵌套 Git 根，因为 ignore、目录结构或配置可能已变。分析器或配置版本变更导致的重分析必须在覆盖摘要中可见。

### 5.2 近期 Git 历史

初始 Git 历史窗口固定为扫描开始时刻向前 180 天。查询范围是每个发现工作树的 HEAD 与本地默认分支可达提交的并集，按 commit hash 去重；不 fetch 远端分支、不 checkout、不修改工作树。索引内容限于 commit 定位、时间、作者元数据、标题和已变更路径范围，不保存完整 diff 正文。

当某个具体 Claim 需要解释当前代码的演进而近期窗口无法回答时，Codex 可以通过 `EvidenceQuery` 针对该 Claim 的证据路径、模块或 commit 发起一次有理由的更早历史查询。查询先返回受限 commit 元数据和路径 candidate；对选中的单个 candidate，若 Git 数据库位于授权根内，Codex 可在当前会话有界读取相关 path 的 diff/blob，`EvidenceDraft` 只保存 commit/object/diff hash、locator、理由和短摘要，不保存正文。若 common-dir 位于授权根外，则保持 3.2 的元数据边界并形成可见知识缺口，Owner 可在后续运行显式选择更宽的工作区。采用的 EvidenceDraft 在 record_analysis 中校验并落为 preparation-scope Evidence，不回写 ProjectSnapshot，不做隐式全历史重索引，也不把历史作者自动转换为个人贡献（`FR-06`、`NFR-02`）。

## 6. 语言适配与基础档案

所有项目至少获得通用基础档案：项目/模块根、manifest、依赖声明、入口与构建配置、测试配置、部署/运行配置、数据库或接口线索、领域名词、文档和文件覆盖。这些条目都是候选证据，不能替代实际实现证据。

首版深读适配器如下（`FR-03`、`NFR-07`）：

| 语言/文件 | 深读范围 | 明确产出 |
| --- | --- | --- |
| TypeScript / TSX | package/workspace、import/export、应用/服务入口、路由/UI 接线、构建与测试配置 | 模块依赖、实际技术使用、入口/边界与测试证据 |
| Python | `pyproject`/requirements、包结构、import、CLI/Web 入口、异步/任务与测试配置 | 服务/工具模块、依赖使用、运行和测试证据 |
| Rust | Cargo workspace/crate、module、feature、bin/lib、错误与异步边界、测试 | crate 关系、编译入口、能力边界与测试证据 |
| Dart | `pubspec`、package、Flutter 应用入口、路由/状态/平台接线与测试 | 移动端模块、依赖使用、UI/平台与测试证据 |
| SQL | migration/schema、表/关系、约束/索引、view/trigger、查询文件 | 数据模型、演进和查询能力证据；迁移与计划文档必须区分 |

其他语言仍生成基础档案，并在覆盖中标为“基础分析，未做语言深读”。它们可以支持结构、依赖、文档和配置层面的 Claim；不得生成需要深层调用分析才成立的技术断言。新增深读语言必须作为新的适配器和验收项进入后续决策，不能在首版隐式扩大范围。

## 7. 按证据的岗位化深读

`RoleLens` 先从岗位、JD、职级和覆盖规则构造有限查询。Python 核心返回 `EvidenceBundle`，其中含项目排序、模块、证据定位器、短摘要、时效、覆盖问题和深读建议，但没有源码正文。

Codex 按以下顺序工作：

1. 读取岗位相关 EvidenceBundle，选择能回答岗位维度的高优先级项目和模块；
2. 打开被选中 Evidence 指向的本地原文件，并核对内容哈希与当前时效；
3. 只在证据不足、相互矛盾、需要解释实现方式或出现岗位关键追问时，沿 import、调用、配置或测试链路扩展阅读；
4. 将形成的 Claim 关联已核验的 Evidence；不能核验的内容作为知识缺口或不确定性，而不是补写为事实。

因此，扫描器索引全量模块，Codex 深读有界证据链。系统不得把全仓源码复制进 EvidenceBundle、数据库或最终报告（`FR-05`、`FR-06`、`FR-11`、`NFR-02`）。

## 8. 覆盖报告与可判定验收规则

每个终态扫描必须在 `EvidenceBundle` 和下游产物中呈现：发现项目数、fresh/carried-forward/failed-no-baseline/excluded 数量、工作树数、模块数、纳入/排除文件类别、深读与基础分析语言、每项 `ScanIssue` 的路径范围、原因、影响和补救动作。`coverage_status=complete` 只表示当次配置下合资格输入均被处理，不等于理解全部业务语义。

| ID | 可判定输入 | 必须输出 | 失败或降级行为 | 需求映射 |
| --- | --- | --- | --- | --- |
| `SCAN-01` | 一个可读工作区根或一个无效/不可读根 | 前者产生 `Workspace` 与终态 `ScanRun`；后者不产生可用快照 | 无效根为 `failed`，历史数据不被覆盖 | `FR-02`、`FR-03`、`NFR-01` |
| `SCAN-02` | Git 根、`.git` 指针、嵌套 Git 与 manifest 非 Git 项目混合 | 稳定 Project/Worktree/Module 发现结果；空目录不是项目 | 损坏 Git 根记 `ScanIssue`，不降格、不阻断其他项目 | `FR-03`、`NFR-05` |
| `SCAN-03` | 同一 common-dir 的多个 linked worktree，既有相同文件也有分支差异 | 一个 Project、多份 worktree observation；相同内容复用分析但保留来源；差异事实按 worktree 分开 | 不能读取某个工作树时问题可见；不得把不同分支拼成一个当前项目状态 | `FR-03`、`FR-11`、`FR-15` |
| `SCAN-04` | 父项目 ignore 内层 Git；依赖、构建、缓存、`.env` 与密钥文件 | 内层仓库被独立发现；敏感和硬排项不读、不存、不输出 | 普通 ignore 不覆盖内层 Git；硬排项只报告类别，不泄露内容 | `FR-03`、`FR-15`、`NFR-01` |
| `SCAN-05` | 已提交、modified 与 untracked 的合资格文件、测试定义/结果元数据，以及只含计划的文档 | Evidence 记录正确 commit state/facet；测试定义与通过结果分开；计划不显示为实现 | 无实现不得生成 implemented；无匹配通过结果不得生成 test_verified | `FR-11`、`NFR-02` |
| `SCAN-06` | 首扫后无变化、修改、删除、移动和分析器/配置变更 | 无变化复用 revision；变化追加 revision；旧定位出现 stale/missing | 项目事务失败保留旧快照为 `carried_forward`，不冒充 fresh | `FR-04`、`FR-15`、`NFR-04`、`NFR-05` |
| `SCAN-07` | 当前实现与超过/未超过 180 天的 Git 历史 | 默认只索引近期本地提交；按 Claim 可进行有记录的定向追溯 | 无法读取历史产生 `ScanIssue`，不阻断当前工作树索引 | `FR-06`、`NFR-02` |
| `SCAN-08` | TS/TSX、Python、Rust、Dart、SQL 和未知语言并存 | 首批语言提供深读证据；未知语言提供明确标注的基础档案 | 语言不支持记覆盖缺口，不阻断其他语言/项目 | `FR-03`、`FR-15`、`NFR-07` |
| `SCAN-09` | 大型多项目工作区与一个岗位 RoleLens | 先返回有界 EvidenceBundle，再按证据打开本地原文件 | 证据失效或不足时要求 refresh 或形成知识缺口，不通读全仓或编造结论 | `FR-05`、`FR-06`、`FR-11`、`NFR-02` |
| `SCAN-10` | 无权限目录、损坏仓库和超限/解析失败文件 | 其余可用项目的快照、结构化 ScanIssue 与覆盖影响 | 有可用快照时为 `partial`；没有可用快照时为 `failed` | `FR-15`、`NFR-05` |
| `SCAN-11` | 文件名、manifest、文档或 Git 标题含 shell 元字符、提示注入或 HTML/脚本文本 | 内容仅作为证据数据与安全短摘要，项目代码和指令均不执行 | 解析失败形成 ScanIssue；不得改变授权、运行命令、访问网络或扩大写入范围 | `NFR-01`、`NFR-08` |

## 9. 非首版边界

首版不做 Go、Java、C#、Swift 等语言的深层调用分析，不做全历史默认索引，不监听文件变化，不启动本地服务，也不因 Git 作者自动生成个人贡献结论。这些限制不妨碍之后新增适配器或更强分析，但必须先更新相应决策与验收契约。

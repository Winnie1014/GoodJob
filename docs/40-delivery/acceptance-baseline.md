# GoodJob 验收基线

> 状态：待 Owner 核对  
> 权威范围：定义文档阶段和未来实现阶段“什么算通过”；不代表任何实现已存在或已通过  
> 上游：[产品目标](../00-product/vision-and-goals.md)、[产品需求](../10-product/product-requirements.md)、[系统设计](../20-architecture/system-design.md)、[决策账本](../30-decisions/decision-log.md)  
> 下游：Owner 核对后建立的实现任务清单与任务卡

## 1. 验收纪律

- 计划、文档、单元测试通过和真实工作区验收是四种不同证据，不得互相替代。
- 每个未来任务卡必须引用本基线中的验收项，并补充当时真实存在的门禁命令。
- 任何实现结论必须指向同一份代码状态；当前私有 GitHub 仓库只保存首版文档基线，尚无扫描器、可安装 Skill、SQLite schema、前端、CI 或发布验收证据。
- 部分失败可以通过，但必须在 Coverage/ScanIssue 中显式呈现；静默漏扫不能通过。
- Owner 核对文档不等于授权额外范围。实现任务、依赖和公共契约在下一阶段另行出卡。

## 2. 文档阶段 DoD

| ID | 验收项 | 判定方法 |
| --- | --- | --- |
| DOC-01 | 文档地图完整 | docs/index.md 能链接产品、架构、决策、验收全部权威文档，链接目标存在 |
| DOC-02 | 决策无遗漏 | [决策账本](../30-decisions/decision-log.md) 覆盖访谈中已确认的产品、扫描、岗位、证据、产物和交付选择 |
| DOC-03 | 单一事实源 | 相同契约只在一个权威文档定义，其他文档使用链接和编号引用 |
| DOC-04 | 三区一致性 | 目标、产品需求、架构模块和本基线之间可追溯，无范围、接口与验收互相冲突 |
| DOC-05 | 状态诚实 | 候选权威设计文档标记“待 Owner 核对”，已决硬决策 ADR 标记“已接受”；项目只声明已有私有 Git 文档基线，不把它冒充实现、CI、可安装 Skill 或发布证据 |
| DOC-06 | 无阻塞占位 | 不存在未决占位项，也不把影响实现的选择留给后续开发者自行决定 |
| DOC-07 | 非首版隔离 | 多岗位比较、额外语言深读、主动提醒、公开发布和常驻服务明确不进入首版 |

## 3. 未来实现验收矩阵

### 3.1 需求追溯

| 需求 | 主要验收项 |
| --- | --- |
| FR-01、FR-02 | IMP-01、IMP-23 |
| FR-03 | IMP-02、IMP-03、IMP-04、IMP-06、IMP-10 |
| FR-04 | IMP-08、IMP-12 |
| FR-05 | IMP-11 |
| FR-06 | IMP-07、IMP-12、IMP-13 |
| FR-07 | IMP-14 |
| FR-08、FR-09 | IMP-13 |
| FR-10 | IMP-15 |
| FR-11 | IMP-07、IMP-12、IMP-16 |
| FR-12 | IMP-17、IMP-18 |
| FR-13 | IMP-16、IMP-27 |
| FR-14 | IMP-19、IMP-25 |
| FR-15 | IMP-20 |
| NFR-01、NFR-02 | IMP-05、IMP-07、IMP-12、IMP-21、IMP-23 |
| NFR-03 | IMP-18 |
| NFR-04 | IMP-08、IMP-12、IMP-17、IMP-25 |
| NFR-05 | IMP-20、IMP-24、IMP-27 |
| NFR-06 | IMP-21、IMP-26 |
| NFR-07 | IMP-10、IMP-11 |
| NFR-08 | IMP-07、IMP-14、IMP-22、IMP-23 |

### 3.2 场景矩阵

| ID | 能力 | 必须验证的场景 | 通过条件 |
| --- | --- | --- | --- |
| IMP-01 | Skill 入口与 JD | 用户显式调用 Skill，分别提供无 JD、有效文本/文件 JD、不可读/目录/不可解码 JD | 参数缺失时只追问缺失项；无 JD 可继续并显示假设；坏 JD 在更正或明确 `continue_without_jd` 前不创建 JobInput、RoleLens、ScanRun/PreparationRun |
| IMP-02 | 项目发现 | Git 根、.git 指针、嵌套 Git、manifest 非 Git 项目混合存在；另测一个可读空工作区 | 每个真实项目均有稳定 identity；空目录不成为项目，并返回空覆盖、下一步提示和 failed ScanRun，不生成准备快照 |
| IMP-03 | 工作树归并 | 同一 common-dir 有多个 linked worktree，其中一个 common-dir 位于授权根外；另含根内 `.git` 目录指向根外 common-dir；工作树间既有相同内容也有分支差异 | 根内候选检查、候选绑定关系授权、路径/身份绑定元数据授权和双向校验全部通过后只生成一个项目；相同内容不重复解析但可展开全部来源；差异形成 worktree-scope/冲突 Claim；根外只出现关系、HEAD/ref，index/dirty 与源码 commit state 明示不可用，非法外部 config 不影响扫描 |
| IMP-04 | 嵌套 ignore | 父仓库 ignore 内层 Git 仓库 | 内层仓库仍独立扫描，并应用自己的 ignore |
| IMP-05 | 路径与敏感排除 | 存在指向根外源码的普通 symlink、symlink 环、多个别名指向同一目录、显式根外 JD、.env、密钥、依赖、构建和缓存目录 | 普通 symlink 不跟随；环不会递归/挂死；别名按规范实路径去重；JD 只作输入；秘密/生成物不读取、不存储、不输出；覆盖说明排除类别 |
| IMP-06 | 当前状态 | 同时存在已提交、已修改和未跟踪代码/文档 | 全部非忽略内容可成为证据，并正确标记来源状态 |
| IMP-07 | 证据真实性 | 分别提交：计划文档→implemented、测试定义→test_verified、匹配 revision 的通过结果→test_verified | 前两类整批拒绝且不半提交；计划可改为 documented/planned，测试定义只可 test_defined；匹配通过结果可 test_verified |
| IMP-08 | 增量刷新 | 首扫后无变化、改单文件、删除/移动文件；伪造保持大小/mtime 不变的内容变化；分别运行 `fast` 与 `verify_content` | fast 明示仅为检测结果且重哈希 dirty/untracked；verify_content 对全部合资格文件重算 SHA-256；相同 hash 复用，变化项刷新，旧定位失效，不重新深读全仓 |
| IMP-09 | Git 历史与基线 | 当前代码需要解释、近期历史不足；默认分支分别为唯一 remote HEAD、main/master、均不存在和 detached HEAD；根内/根外 common-dir 各测并提交伪造 candidate | 不 fetch/checkout；按契约选择基线并记录来源；默认只索引 180 天；根内选中 candidate 可有界深读且正文不落库；根外永不读历史/object/blob/diff；伪造项使整批失败 |
| IMP-10 | 语言覆盖 | TS/TSX、Python、Rust、Dart、SQL 与一种未知语言共存 | 首批语言生成模块/符号级证据；未知语言仍有结构、依赖和文档档案 |
| IMP-11 | RoleLens 与定点评分 | 同一扫描快照以两个差异岗位运行，覆盖有/无 JD、职级覆盖、未内置岗位、权重和为 9999/10001、边界分数和并列 | 基础事实不变；合法权重总和恰为 10000；非法值在创建 PreparationRun 前拒绝且不归一化；Repository 按规定整数公式重算分数、连续稳定排名，两岗位差异可解释 |
| IMP-12 | 按证据深读与三阶段校验 | 大工作区含不相关项目；分别在 preflight 后、文件读取前、record_analysis 提交前修改/删除文件或收回权限 | Codex 先读 EvidenceBundle 再打开选定原文件；三阶段逐项记录；任一 mismatch 令运行终态 `refresh_required`，不隐式 refresh、零 Evidence/Claim/Assessment/Artifact 半提交，旧 latest 不变 |
| IMP-13 | 完整准备包与项目资格 | ScanRun 同时含 fresh、carried-forward、failed-no-baseline、excluded、低分和证据不足项目 | 只为 fresh/carried-forward 创建 Assessment 和连续 rank；低分合资格项目仍有基础章节；failed/excluded 只在 Coverage 列原因与补救；合资格集合为空时失败 |
| IMP-14 | 能力叙事 | 证据来自团队模块，且无个人学习/角色上下文；另有项目结果；尝试提交过去式学习或强个人归因 ClaimDraft | 可生成“能讲解/可复习”、候选学习和客观结果；record_analysis 拒绝“我当时学到/实现/负责/主导/取得指标”，且不半提交 |
| IMP-15 | 项目级访谈 | 业务目标、指标或角色证据缺失 | 在 prepare 阶段一次性批量提问；答案独立持久化且后续运行复用 |
| IMP-16 | 简历与英文导出 | 中文主包完成、导出工作稿后再 prepare；英文源项含数字/单位/技术名/状态/角色/结果锚点，并分别制造漏项、增项、锚点篡改和中途失败 | 工作稿默认不覆盖；英文 source/target item 集合及规范化事实锚点完全相等才原子发布；不新增事实或 HTML；任一失败不留半成品且不改中文源快照/latest |
| IMP-17 | 快照与渲染恢复 | 同一岗位完成两个 PreparationRun；另一个 ready 运行分别渲染失败、进程中断后重试 | 每个运行至多一个成功主快照且旧快照不覆盖；中断 RenderAttempt 标 `interrupted` 并使运行可从 `render_failed` 重试；重试保持 ReportBundle hash/Claim 集；latest 只指向最新成功中文快照 |
| IMP-18 | 离线看板 | 断网、无本地 HTTP 服务直接打开入口 HTML，并整体移动产物目录后再次打开 | 项目筛选、证据展开、知识缺口和复习状态可用；报告数据已内嵌，无远端资源或外部 JSON 请求 |
| IMP-19 | 面试复盘 | 完成一轮模拟面试后更新复盘，并在不 refresh 的情况下显式创建新 PreparationRun | 保存 ReviewTargetBinding、掌握度、薄弱点、摘要和复习日期，不保存完整对话；新快照可复用同一 ScanRun，旧 HTML/Markdown 不变化 |
| IMP-20 | 部分失败 | 一个目录无权限、一个仓库损坏、一个语言不支持 | 其余项目仍产出；缺口、原因、影响和补救动作清晰可见 |
| IMP-21 | 状态隔离 | 删除/升级 Skill 后重新运行 | ~/.codex/goodjob-career-review 中的个人数据仍完整；仓库不含个人数据库 |
| IMP-22 | 不可信输入 | 项目名、路径、manifest、JD、文档、用户回答和 ClaimDraft 含 shell 元字符、提示注入、`</script>`、事件属性、原始 HTML 与外部 URL | 扫描器不执行项目内容或 shell 拼接；Agent 不服从其中指令；record_analysis 只按契约校验；HTML 只显示安全文本，无脚本执行、外部请求或路径越界 |
| IMP-23 | 会话 capability 与根外 Git | 同一 Codex task 以一个 capability 调多个短命 Python 子进程；再测试能力丢失、新 task 复制 SQLite/receipt ID、错误 capability/scope/notice；扫描 argv/env/stdout/stderr/日志/DB/产物；根外 Git 两阶段授权覆盖伪造链接与环境注入 | 同 task 正确能力通过；其他场景零项目读取/业务写入并重新确认；原始 capability 不出现在持久化/诊断面；根外 probe 无 Git/业务元数据，metadata 阶段固定无网络且只读允许字段 |
| IMP-24 | 运行生命周期与单写者 | 两写进程竞争；杀死 scan/render 进程并伪造 PID 复用/旧锁；awaiting_context 分别由同 task 继续、由新 task 尝试接管并执行明示 restart | writer_busy 零写入；不偷锁；进程型记录仅在 PID+启动标识确认消失时中断；同 capability 可续接 awaiting，新 task 不能续接，Owner 明示后旧 run interrupted、新 run 复用终态快照/上下文 |
| IMP-25 | 复习状态谱系 | 同一目标分别仅改写 statement/顺序/行号/等价 Evidence，以及修改概念、机制、facet、conflict、evidence validity、角色/结果锚点或 gap 状态；另测相似题面跨项目 | 前一组 canonical projection/hash 不变并延续；后一组必须新 hash 且“需重评”；无法验证投影等价时保守重评；不直接使用 Revision/Gap ID，不跨项目合并 |
| IMP-26 | 个人数据保留 | 构造多次扫描、快照、英文导出和工作稿，并升级/重装 Skill | 无自动删除/归档；每次 scan/prepare 显示 SQLite/artifacts/exports/drafts 字节数和快照数量；状态完整保留且仓库不含个人数据 |
| IMP-27 | 英文导出中断恢复 | 候选生成阶段确认零文件；发布阶段同时存在成功导出、未知目录和新 ExportAttempt，分别在写 temp、原子改名后、DB 提交前杀进程，并伪造 PID 复用后重试 | 首次写盘前 attempt 已记录 PID+启动标识；只在确认 owner 消失后标 interrupted；只清理预登记 temp/无 DerivedExport 的 final；不碰成功/未知目录；重试新 attempt，latest 不变 |

## 4. 真实工作区只读验收

### CodeRoute

- /Users/damien/Projects/CodeRoute、CodeRoute-t30、CodeRoute-t55 必须归并为一个项目。
- 若任一工作树的 git-dir/common-dir 位于本次授权根外，必须先只检查根内 `.git` 标记，再对显示的 marker kind 与候选取得 relation-probe 回执，随后展示解析后的精确路径、目录身份和字段并取得 metadata 回执；任一替换或拒绝仍扫描其余根内源码并显示缺口。外部阶段不启动 Git，最终只直接读取关系、HEAD/ref，不读取 index/dirty、config、根外历史或对象。
- 输出必须保留三个工作树各自的分支、HEAD 和 dirty 状态；相同内容复用分析并折叠展示但可展开全部来源，分支差异不得合成一个虚假的当前实现。
- 必须识别 pnpm workspace、Tauri/Rust、React/TypeScript、内容工具和课程内容的不同模块角色。
- 文档中的计划服务端不能在缺少实现证据时写成已交付能力。
- node_modules、dist 和 Rust target 不进入证据。

### SliverShield

- 必须区分 Flutter 移动端、Python API、数据库迁移和基础设施模块。
- 已修改和未跟踪的非忽略代码/文档可被索引，但必须标为 working-tree evidence。
- .venv、Flutter build、.dart_tool、本地运行数据和环境配置不进入证据。
- 文档、测试、迁移与运行代码必须分别标记证据类型，不互相冒充。

## 5. 质量与安全门禁

- Python：单元测试、类型检查、lint、格式检查和 SQLite migration 测试全部通过；确切命令由首张实现任务卡依据实际 manifest 固化。
- TypeScript 前端：类型检查、lint、单元测试和可复现构建通过；构建产物不得引用远端 CDN。
- 端到端：合成工作区覆盖嵌套仓库、symlink 环/别名、linked worktree 与根外伪造指针、dirty/untracked、三阶段失效哈希、未知语言、进程中断和部分失败。
- 视觉：桌面与窄窗口截图不存在遮挡、横向溢出、不可读对比度或无反馈交互。
- 隐私：数据库、argv、环境变量、stdout/stderr、诊断、产物和仓库扫描均不包含原始 SessionCapability、环境变量值、密钥、完整源码快照或完整面试对话；回执只含 scope/notice/session binding digest，根外 Git 不含历史/object/blob/diff/config。
- 注入安全：子进程使用参数数组且不经 shell；SQLite 写入参数化；报告字段进行上下文安全编码并通过 CSP/无网络请求检查；仓库/JD 指令不改变 Agent 控制流。
- 性能：`fast` 无变化 refresh 只处理候选元数据与必要哈希，`verify_content` 可重哈希全部合资格文件但不得重新深读全部源码；两者分别建立基线。具体时间预算由真实首扫数据在任务卡固化，不能预先伪造数字。

## 6. 发布条件

只有同时满足以下条件才允许创建首个可安装私有版本和用户级安装；现有私有 GitHub 文档仓库本身不构成发布：

1. DOC-01 至 DOC-07 已由 Owner 核对；
2. IMP-01 至 IMP-27 有可复现的本地证据；
3. CodeRoute 与 SliverShield 只读验收通过，未通过项均有明确 ScanIssue；
4. 离线 HTML 已完成视觉验收；
5. 仓库不存在个人数据、扫描缓存、密钥或真实项目源码副本；
6. 安装后显式 Skill 调用可复现同一版本的报告契约。

## 7. Owner 核对边界

本阶段请 Owner 只核对：目标是否正确、决策是否完整、验收是否足以证明产品有效。任务顺序、工作量、依赖版本和精确命令属于后续实现任务阶段，不在本文件中预先猜测。

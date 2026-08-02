# GoodJob 任务池（backlog）

> 状态：协作运行区，非产品契约
> 权威范围：任务状态的唯一事实源（谁在做、做到哪、裁决落在哪）
> 上游：[验收基线](acceptance-baseline.md)、[决策账本](../30-decisions/decision-log.md)
> 维护者：Architect。Implementer 只读，状态变更走[信道](../collab/channel.md)。
> **2026-08-02 起 Architect 由 Sol 担任**（信道 #36 交接）；Claude Opus 5 退出日常，仅做最终验收。

任务卡在 [docs/collab/tasks/](../collab/tasks/)。本表只记状态与归属，不复制卡面内容。

## 里程碑 M1 · 首版实现缺陷收敛

评审基线 commit `a3fac9f`。该基线上 Python 与前端全部门禁为绿；以下任务指向门禁**没有覆盖到**的地方，不是回归。评审记录见 [opus-review.md](../../opus-review.md)（文档层，2026-07-24）与 2026-07-29 实现层评审。

### 批次 A · 门禁判定层级（`D-043`）

| 任务 | 卡面 | 状态 | 验收项 |
| --- | --- | --- | --- |
| GJ-01 · 渲染门禁被工作区内容触发 | [GJ-01](../collab/tasks/GJ-01.md) | ✅ 已验收合入（信道 #5，merge `0e75a78`） | `IMP-28`、`DASH-03` |
| GJ-02 · 归因校验按 token kind 分层 | [GJ-02](../collab/tasks/GJ-02.md) | ✅ 已验收合入（信道 #5，merge `0e75a78`） | `IMP-14`、`IMP-22` |

批量模式：统一分支 `task/GJ-A-structural-gates`，从 `ead37b0` 拉出，按卡独立 commit（`3131465`、`5cebbc1`），统一交付统一验收。

验收裁决要点（信道 #5）：契约与 DoD 逐条成立，范围零越界，体量 64/200 与 84/260。变异测试确认新增用例在缺陷回归时变红；产物在 Chromium 151 与 WebKit 26.5 上零控制台错误、零外部请求、内联 JSON 正常解析。验收中发现的两处**出卡侧疏漏**（非实现缺陷）已转 GJ-08。

### 批次 B · M1 剩余（顺序实施，统一交付）

| 序 | 任务 | 卡面 | 状态 | 体量 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-08 · 让分层判定各自守得住 | [GJ-08](../collab/tasks/GJ-08.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 197/260 | `IMP-28`、`DASH-03`、`IMP-14`、`IMP-22` |
| 2 | GJ-04 · 跨引擎行为门禁进入运行时前端 | [GJ-04](../collab/tasks/GJ-04.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 560/600 | `IMP-28` |
| 3 | GJ-03 · 项目级排除与 `excluded` 生产者 | [GJ-03](../collab/tasks/GJ-03.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 586/650 | `IMP-04`、`IMP-13` |
| 4 | GJ-05 · ignore 子集显式化并可见 | [GJ-05](../collab/tasks/GJ-05.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 254/450 | `IMP-04`、`SCAN-04` |

批量模式：统一分支 `task/GJ-B-m1-remainder`，**严格按上表顺序**逐卡实施，按卡独立 commit，四卡全部完成后一次性交付统一验收。合计体量上限 1900 行。

顺序约束：GJ-05 必须在 GJ-03 之后（同改 `scanner.py` 覆盖摘要）。其余三卡文件范围互不重叠（GJ-08 碰 `reporting.py`/`analysis.py`，GJ-04 碰 `frontend/`，GJ-03 碰 `scanner.py`/`paths.py`），排此序是为让上下文由热到冷推进。

停工点：任一卡触发 L1 即**停止整条链**并立即上报，不得带着未裁决的问题进入下一张。

派卡前自查修正：GJ-04 契约 7 原本要删 5 条源码文本检查却未在契约 3 中给出等价行为断言，属净损失覆盖，派卡前已补齐一一对应关系与反向验证要求，体量上限相应由 520 提到 600。

验收裁决要点（信道 #12）：四卡契约与 DoD 逐条成立，范围零越界，七个提交文件白名单互不重叠，合计体量 1597/1900。Architect 独立复跑全部门禁，并独立复现 GJ-08 三次归因变异、GJ-04 八条行为反证；另做两项卡面未要求的加验——176 组单调性枚举（0 降级、0 漏检、8 例升级为更强归因，严格超集成立）与一次 GJ-04 源码级反证（改 `dashboard.ts` 的 `<a>` 为 `<span>` 后重新构建，`npm run verify` 红 2 断言 × 2 引擎，证明门禁守到源码而非夹具）。验收中发现的一处**出卡侧疏漏**（非实现缺陷）已转 GJ-09。

裁决记录：**#9 L1 → #10 裁决**。GJ-08 原契约 4「散文投影插入换行分隔」会击穿依赖字面相邻的中文 `_PERSONAL_PATTERNS`，使 `implemented`/`responsible`/`led`/`personal_learning` 四类强归因全部降级为 `personal_assertion`；因 `_attribution_covers` 中 `responsible` 覆盖 `personal_assertion` 却不覆盖 `implemented`，实际会放宽个人归因校验。裁决采纳 Implementer 建议的双投影并集方案，新增单调性硬约束与对应测试、变异自检增至 3 次，体量上限 200 → 260。属出卡侧契约缺陷，非实现缺陷。

### 批次 C · 验收回流

| 任务 | 卡面 | 状态 | 体量 | 验收项 |
| --- | --- | --- | --- | --- |
| GJ-09 · 把转义集合的内容本身锚住 | [GJ-09](../collab/tasks/GJ-09.md) | ✅ 已验收合入（信道 #14，merge `9f06b8f`） | 45/80 | `DASH-03` |

验收裁决要点（信道 #14）：契约 1-4 逐条成立，产品代码零差异，单文件范围。Architect 独立复现 DoD 要求的五次变异，每次均由新锚点点名到具体字符；另加一次卡面未要求的加验——删除 `U+202E`（双向覆写符，本组中唯一具实际欺骗价值的字符，可令看板上的路径或函数名视觉反向显示），批次 B 时同样静默，现已被接住。空洞闭合范围超出 #12 点名的 `&`。

出卡缘由（信道 #12 §4）：GJ-08 契约 3 要求转义集合与后置断言同源——这是对的，但同源必然意味着删掉映射里任一条目会让转义与断言**一起**消失。防线强度因此完全取决于是否有独立于该映射的测试锚住集合内容。验收变异实测：删 `=` → 5 failed，删 `&` → **167 全绿**，删 U+2028 → 1 failed（且只因某夹具恰好含它，非针对性断言）。`=` 被接住是因为参数表硬编码为 `("<", ">", "=")`，恰好等于 GJ-08 的 DoD 点名的三个字符。契约 1 点了七类字符、DoD 只要求直接覆盖三类，差额即此洞。属出卡侧疏漏，GJ-08 实现覆盖范围与要求范围逐字相等。

出卡侧措辞修订（不出卡，记账）：GJ-08 契约 6 写的"必须检出同一个归因"过窄，本意是其后两条 `不得` 子句（不得降级、不得变为不检出）。per-pattern 并集会把部分 `personal_assertion` 升级为 `led`/`implemented`（176 组枚举中 8 例），方向更严，契约 7 明确认可。实现无误，措辞待下次改卡时收紧。

### 待 Owner 裁决（不出卡，观察记录）

| 观察 | 出处 | 说明 |
| --- | --- | --- |
| ~~全部项目被排除时终态为 `failed`~~ | GJ-03 验收 | **Owner 2026-08-02 裁定：改。**关键证据 `history.py:244` 的 `status IN ('completed','partial')` 会把 `failed` 运行整个过滤出下游，Owner 的配置意图被当成故障。已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 1-3 |
| ~~ignore 原始模式行非独立字段~~ | GJ-05 验收 | **Owner 2026-08-02 裁定：做。**已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 4-5 |
| ~~文档门禁静默依赖 `python3` 解析到哪个解释器~~ | GJ-11 #26 裁决时实测 | **Owner 2026-08-02 裁定：不固定解释器。**Architect 实测 Python 3.9.6 / 3.12.13 / 3.14.3 三版本结果完全一致（均 `39 files`、测试 `OK`），脚本只用 walrus 等 3.8+ 特性；运行期版本守卫拦不住 `SyntaxError`（解析期即失败），加了是假防护。仅要求在 docstring 写明「只用 3.9+ 语法」，已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 6。原观察： `make gate-docs` 跑 `python3 scripts/check-doc-links.py`；本机 `python3` 解析到 **Python 3.9.6**（Xcode 自带），而 runtime 要求 3.12。检查器在 3.9.6 上工作正常、门禁为绿，故非缺陷。但门禁行为取决于 `python3` 恰好指向什么，是否固定解释器需 Owner 定。GJ-11 明确不处理此项 |
| ~~protocol §8「路径代码」条款的适用范围~~ | GJ-07 验收 | **Owner 2026-08-02 裁定：收窄到运行时。**`O_NOFOLLOW`/descriptor-relative 只约束 `runtime/src/`（会被安装、会读 Owner 任意工作区）；仓库根 dev 工具（`scripts/`、`prototypes/`）只读本仓库自身内容，不受此条约束，已写的加固保留不回退。**已直接改入 [protocol §8](../collab/protocol.md)，不出卡。**原观察： §8 平台行写「涉及路径/进程/文件的代码仍按 `O_NOFOLLOW`、descriptor-relative 的既有写法」。Architect 撰写时指的是**运行时**（会被安装、会读用户任意工作区），但字面覆盖到了仓库根上只读自己文档的 dev 脚本，GJ-07 据此写了 40 行加固。多守一层无害，但条款适用范围应由 Owner 裁定，非 Architect 单方收窄。GJ-11 契约 7 明确该 40 行保留不动 |

## 里程碑 M1 后 · 门禁入口与结构债

### 批次 D（顺序实施，分卡交付）

| 序 | 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-07 · 门禁聚合入口 | [GJ-07](../collab/tasks/GJ-07.md) | ✅ 已验收合入（信道 #20，merge `40491ed`） | 422/180（超额，裁量接受） | `SCAN-04` 门禁可复现性 |
| 2 | GJ-06 · 剥离 Git 元数据与沙箱调用 | [GJ-06](../collab/tasks/GJ-06.md) | 待派发（**须在 GJ-07 合入后**） | 净新增 200（移动行单列） | `SCAN-04` |

批量模式：**与批次 B 不同，本批分卡交付分卡验收**。GJ-06 是纯重构且风险集中在红区，必须先拿到 GJ-07 的 `make gate` 作为等价性证据入口，且要在独立的验收窗口里单独评审，不与其他卡混在一次交付里看。

**GJ-07 出卡依据（2026-07-31 Architect 实测）**：改 `frontend/src/dashboard.ts` 一句 UI 文案而不重新构建，`uv run pytest -q`（180 用例）与 `npm run verify`（跨引擎行为门禁）**双双为绿**，仅 `npm run build:check` 变红——Python 侧与 verify 读的都是已提交的 `dashboard_assets/dashboard.js` 产物，不感知 TS 源码。全套门禁中**只有一条命令**知道源码与产物是否一致，且它挂在需要浏览器准备步骤的链子上。

**GJ-06 出卡依据与范围收窄**：`scanner.py` 4189 行，`WorkspaceScanner` 一类 3317 行 66 个方法。按方法名归簇实测：Git / 历史 / 外部仓库沙箱 **28 个方法 1048 行**，遍历 / ignore / 分类 5 个方法 427 行，其余 33 个方法约 1840 行。红区（子进程调用、超时与输出上限、外部仓库授权、路径校验）全部埋在此类中。**本卡只剥第一簇**——纯重构的评审成本随 diff 体积超线性增长，一次搬 1500 行会让「逐条核对是不是纯移动」失效，而那是本卡唯一的安全网。遍历 + ignore 簇留待后续卡。

裁决记录：**#17 L1 → #18 裁决**。GJ-07 原契约 2 按族手工枚举门禁命令，把前端族写成 `npm test`，与 [protocol §8](../collab/protocol.md) 门禁命令表原文 `npm ci && npm test` 不一致，致使契约 2 自身构成「改门禁命令」，与契约 5 直接冲突，Implementer 无法在不违反其一的前提下交卡。裁决采纳 Implementer 建议保留 `npm ci`。**Architect 核查时另发现同源第二处**：协议表为三族，原契约 2 整个漏掉文档族（全仓 markdown 相对链接检查），而该族是三族中唯一连可执行命令都不存在、纯靠人手工执行的一族——恰好被漏在"消灭靠人记性"这张卡之外。契约 2 因此重写为「以协议表为唯一事实源，逐条一致，不增不减不改写」，不再手工枚举；新增 `make gate-docs` 与契约 8（`scripts/check-doc-links.py`，仅标准库）；契约 5 增加冲突消解规则「契约与 protocol §8 冲突时以协议为准」。**契约 9 系撰写裁决时实测撞出**：临时链接检查器把卡面中描述链接语法的示例文本误判为断链，故要求检查器跳过围栏代码块与行内代码，并配双向 DoD 自检；改用代码区感知版复检，34 个 md 断链为零。体量上限 120 → 180。均属出卡侧契约缺陷，非实现缺陷；Implementer 候选提交 `8f67765` 除被拦下的一条外全部成立，无需返工。

GJ-07 验收裁决要点（信道 #20）：契约 1-9 逐条成立，`runtime/` 与锁文件零改动。Architect 独立复跑 `make gate` 并独立复现全部三项漂移自检（产物漂移红在 `build:check`；依赖漂移删除 `node_modules/typescript` 后由 `npm ci` 2 秒装回；断链与代码区跳过双向成立）。`make -n gate` 展开为协议表三族七条、无 `verify`；检查器仅标准库。**体量 422/180，超授权 2.3 倍**，按 [protocol §7](../collab/protocol.md)「是否重构由 review 裁量」——**裁量结果：接受不返工**（披露流程合规，超额源自 Implementer 自身双轴 review 的发现，重写成本高于收益）。实测分支分布：契约明文核心 176 行、容器围栏与完整链接闭合解析 118 行（**本仓库零实例**）、`O_NOFOLLOW`/descriptor walk 40 行。存疑点裁决：「全仓 = Git 已跟踪 + 未忽略未跟踪」语义确认成立，98 → 38 份的收敛正确，不改契约。

### 批次 E（顺序实施，分卡交付）

| 序 | 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-11 · 给文档门禁补测试并按覆盖收敛 | [GJ-11](../collab/tasks/GJ-11.md) | ✅ 已验收合入（信道 #28，merge `110315f`） | 318/320 | `SCAN-04` |
| 2 | GJ-06 · 剥离 Git 元数据与沙箱调用 | [GJ-06](../collab/tasks/GJ-06.md) | ✅ 已验收合入（信道 #31，merge `ed53ff2`） | 净新增 194/200 | `SCAN-04` |

裁决记录：**#23 L1 → #24 裁决**。GJ-11 的 DoD 把检查器输出份数写死为 `38 files`（取自 #20 验收 GJ-07 时 `40491ed` 的观测值），而卡面指定的基线 `2f0bbbd` 正是新增 `GJ-11.md` 自身、把计数变为 39 的那个提交——卡面在它自己的基线上即自相矛盾。采纳 Implementer 的第二方案改为动态口径（报告份数 == `git ls-files --cached --others --exclude-standard -- '*.md' | wc -l` 在当次 HEAD 的结果），而非把 38 改成 39。**同因扫描**（出卡门禁第 3 条）发现 GJ-06 同病：DoD 写死「既有 180 个 Python 用例」，已改为动态；契约 2 的导入面名字表加注「以实际代码为准，自行重新 grep 核对，不一致按 L2 报告」。**病因回流**：`architect.md` §1.3 出卡门禁新增第 8 条「派生量不写死」与第 9 条「枚举优先指向上游事实源」；`anti-patterns.md` 出卡侧新增对应两条，源案例分别为 GJ-11 与 GJ-07。均属出卡侧缺陷，非实现缺陷；Implementer 已完成的测试与收敛实现不受影响，无需返工。

裁决记录：**#25 L1 → #26 裁决**。GJ-11 契约 1 要求测试可由裸 `python3 -m unittest` 运行，但允许范围不含使 discovery 生效所需的 `scripts/__init__.py`。**采纳 Implementer 的备选方案而非首选**，依据是 Architect 实测——测试文件消失时：裸 `unittest` 与 `discover -s scripts` 均输出 `Ran 0 tests / OK` 并退出 0（假绿），仅显式命名测试模块会硬错非零。`unittest` 在零测试时报 OK，故门禁不得使用任何 discovery 形式。契约 1 改为固定 `python3 -m unittest scripts/test_check_doc_links.py`，不新增 `__init__.py`；新增 DoD「零测试假绿自检」（改名测试文件后 `make gate-docs` 必须变红）。附带发现：`discover -s scripts` 无需 `__init__.py` 即可发现测试（`-t .` 才需要），故首选方案要解决的问题另有无新增文件的解法，但两种 discovery 都留假绿，均不采用。**同因扫描**发现 GJ-06 变异对等自检存在同型空集假绿（两侧均零失败也满足「集合相同」，实则证明该变异点无覆盖），已补「每次变异两侧均须非空失败集合」。L3：`1ea32f8` 提交标题缺 `(Sol)` 执行者尾注（protocol §8 要求，审计必需），下次提交时补齐即可，不单独 amend。

GJ-11 验收裁决要点（信道 #28）：契约 1-7 逐条成立，产品代码仅 +1/-1（删除 `inline_link_targets` 入口处与 `matching_bracket` 重复的 `is_escaped` 判断），`runtime/` 与 `protocol.md` 零改动，体量 318/320。Architect 抽验三处变异（移除 `--cached`、移除 `FileNotFoundError` 容错、禁用围栏遮罩）均独立复现且命中测试名与交付表一致；**#26 裁决的直接产物单独验证**——测试文件移走后 `make gate-docs` 硬错非零，恢复后 `Ran 20 tests / OK`，假绿路径封死。CLI 输出格式与退出码逐字不变，动态计数口径 39 == 39。契约 5 的取舍（118 行「本仓库零实例」分支选择保留并测试而非删除）在授权范围内且理由落在 Architect 给的误红/误绿判据上，认可。

**记账（不出卡）**：文档链接检查功能现总量为检查器 357 行 + 测试 315 行 = 672 行。本卡是净改善（从「357 行没人管」变为「672 行全被守住」），但根因是 GJ-07 契约 8 定的「零依赖、只用标准库」——Python 标准库无 markdown 解析器，该约束直接导致必须手写。将来此处若再出问题，正确的追问方向是「零依赖约束对一个 dev 脚本是否成立」，而非继续在实现上打补丁。

GJ-06 验收裁决要点（信道 #31）：契约 1-7 逐条成立。`runtime/tests/` diff 为空，`cli.py`/`history.py`/`db.py` 零改动，提交仅含 `scanner.py` 与新增 `git_metadata.py`。行数守恒 `scanner.py` 4189 → 3031（−1158）、新模块 1352、净新增 194，落在 −50..+200。对外导入面 14 个名字（卡面表 13 + L2 补报 `ScanResult`）实测全部保留且签名一致。Architect 自选变异点独立复现对等（`GIT_COMMAND_TIMEOUT_SECONDS` 10.0 → 0.0，两侧失败集合 11 = 11 逐条相同），并机械比对逐字性（标注「仅移动」的方法 15/16 字节相同，`_bind_internal_git` 为契约 5 允许的类限定改写，仅表格标注小疏漏）。自主决策认可：28 个候选只搬 25 个，`_discover`/`_history_evidence`/`_resolve_history_evidence_validity` 因混着遍历或持久化而留下；reader 持有五个协作者而非整个 `WorkspaceScanner`（后者会让新模块反向依赖旧类）。产出：红区代码（Git 子进程、超时、输出上限、外部仓库授权、描述符路径校验）现集中于单一文件并有边界 docstring，可单独审阅。

**Implementer L2 成立（出卡侧疏漏）**：卡面契约 2 的导入面表漏了 `tests/test_preparation.py` 依赖的 `ScanResult`，因 Architect 建表时只 grep 了 `test_scanner.py`。未造成损失——#24 裁决为契约 2 补入的「开工前自己重新 grep 核对，以实际代码为准，不一致按 L2 报」正好在此生效，名字原地保留、调用方零改动、全程未停工。这是出卡门禁第 8/9 条第一次产生实际收益。

**Architect 附带发现（不影响验收，记账）**：`HISTORY_WINDOW_DAYS` 的权威定义随簇迁入 `git_metadata.py`，`scanner.py` 仅余别名 `HISTORY_WINDOW_DAYS = _git_metadata.HISTORY_WINDOW_DAYS`。把**两侧 `scanner.py` 暴露的该名字**改为 0：base 失败 2 条、head 失败 1 条（少的一条是原走 `scanner.py` 内部使用点、现改读 `git_metadata` 自有常量的路径）。产品行为不受影响（两处均为 180，运行期无写入，`history.py` 取值一致），当前 180 条测试无一依赖该缝隙；对照之下 `GIT_COMMAND_TIMEOUT_SECONDS` 的缝隙由闭包 `lambda: GIT_COMMAND_TIMEOUT_SECONDS` 保留完好。**方法论盲区**：变异对等要求把变异打在语义等价的**权威位置**，故在构造上看不见「同一名字在两侧可写性改变」这类差异；覆盖这类需把变异打在**对外暴露的名字**上。属出卡侧设计遗漏，后续同类拆分卡的 DoD 应补「对外暴露名字的可写性对等」一条。

排序理由（信道 #21 §3）：两卡文件范围零重叠（GJ-11 碰 `scripts/` 与 `Makefile`，GJ-06 碰 `runtime/src/goodjob/`），技术上无依赖。GJ-11 在先有两个理由——其一，GJ-06 的等价性证据要跑在 `make gate` 上，那就该先让 `make gate` 自己被守住；其二，GJ-06 是纯重构、零行为变更、风险集中在红区，是队列中最难验的一张，需要一个台面上没有别的卡在飞的评审窗口。

**主干推送**：2026-07-31 首次 `git push origin main`（`a3fac9f..2f0bbbd`，29 个提交，纯快进，未使用 force）。此前批次 A / B / C / D 全程仅本地。推送不改变形态 A 的任何纪律。

出卡缘由（信道 #20 §3）：源自 GJ-07 交付的 L3，Architect 采纳并扩了一半。`scripts/check-doc-links.py` 是 357 行手写 markdown 解析器，**在门禁路径上且零测试**，开发期间被双轴 review 抓出 6 个缺陷。关键是**失败方向不对称**：解析过严 → 门禁误红（可见，会被立刻修）；解析过松 → 门禁**误绿**，即断链存在而门禁报告没有——正是 GJ-07 立卡要消灭的失败类。故本卡不只补测试，还带收敛：无测试覆盖的分支须补测试或删除，不允许两者皆非。取舍依据是失败方向而非行数，Architect 不预设结论。

**GJ-06 的核心约束**：零行为变更、`runtime/tests/` 一行不改（需要改测试即为停工信号）、逐字移动、对外导入面逐字不变（含 `history.py` 依赖的 `_safe_history_path` 与测试依赖的 `_open_regular_file` 两个跨模块私有名）。等价性证据为**变异对等自检**：同一组 5 次变异在重构前后两侧的失败用例集合必须逐个名字相同。

### 批次 F（已出卡，待派发）

| 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- |
| GJ-12 · M1 遗留小项收口 | [GJ-12](../collab/tasks/GJ-12.md) | ✅ 已验收合入（信道 #35，merge `9dfdd09`） | 123/220 | `IMP-04`、`IMP-13`、`SCAN-04` |

出卡缘由：Owner 2026-08-02 一次性裁定「待 Owner 裁决」区的三条观察，并要求合并为一张卡。三项为——(1) 全部项目被排除时终态不再 `failed`（`available == 0` 且 `excluded == 0` 才判 `failed`，不新增枚举、不做 migration）；(2) `ignore_pattern_unsupported` 的原始模式行提为独立字段；(3) `check-doc-links.py` docstring 写明只用 3.9+ 语法。

验收裁决要点（信道 #35）：契约 1-6 逐条成立，体量 123/220，禁区文件零改动，无 migration、无新增依赖、无状态枚举变化。`_final_status` 确认只改一个条件表达式、其后判定链一行未动；`check-doc-links.py` diff 仅 docstring。Architect 独立复现两次变异自检，均单条命中；`make gate` 全绿 `182 passed`（180 既有 + 2 新增）。三处实现判断认可：`IgnorePatternIssueDraft` 用专用子类型守住契约 5；保留未 `strip()` 的 `raw_line` 并以 `"  /build/  "` 带空白样例证明逐字性（Spec 首轮抓到原实现取了 `.strip()`）；测试从 `result.coverage` 改读 `overview`，证明字段熬过冻结与重新加载。

**Architect 实测发现（不影响验收，记账）**：`_overview_coverage` 在 `scan_run_overviews` 无行时（扫描中断、未走到 `_finish_run`）走重建分支，`_coverage(..., {})` 传空字典，导致 `ignore_pattern_issues` 条目上 **`raw_pattern` 键整个缺失**（其余六个键均在），消费方写 `issue["raw_pattern"]` 会 KeyError。已在临时库上实测复现。**非实现缺陷**——契约 4 只写了「进入覆盖摘要时成为独立字段」，未提降级路径，且 GJ-03 的 `project_exclusions` 在同一路径上同样为空并已被接受。**留待下一张触及覆盖摘要形状的卡一并定「降级路径字段一致性」；明确不得并入 GJ-10（纯重构卡，卡面禁止顺手改对）。**

**观察（不出卡）**：`_unsupported_issue` 内联复制了 `_issue()` 的构造步骤，两份已有一处不同——`_issue` 为 `relative_path=_short(relative_path) if relative_path else None`，副本为 `relative_path=_short(source)`。`source` 恒非空故今日无行为差异，但 `_issue` 将来变更时副本不会跟随。属「抄一份 vs 指向事实源」同族，暂不处理。

**本卡违反 `anti-patterns.md` 的「一张任务卡塞多个目标」，系 Owner 明确要求的合并。**允许理由：三项互不依赖、各自可独立验收、均不在红区、合计体量小于一张常规卡。卡面已写明：任一项长出超预期复杂度即按 L2 上报，由 Architect 拆卡，不得为「一张卡装得下」而压缩测试。

## 发布验收缺口盘点（2026-08-02，Architect 交接前实测）

> 本节是**交接给下一任 Architect 的工作面**，数据全部实测，非估计。
> 权威的「做完」定义是[验收基线 §6 发布条件](acceptance-baseline.md)，共六条。

### 结论先行

- **产品功能 FR-01~15：15/15 实现完成。**每条功能需求都有对应命令；2026-07-28 真实产出过 3 份中文包（报告 + 简历 + 141 KB 离线看板）与 1 份英文导出。
- **自动化测试 334 条**：Python 182 + 跨引擎行为 132 + 文档检查器 20。
- **发布条件 6 条只满足 1 条。**
- 一句话：**代码基本写完了，验收这道闸门一步没走。**M1（批次 A–F）修的是评审查出的实现缺陷，功能在 M1 开始前就已造完。

### 发布条件逐条状态

| 条件 | 内容 | 状态 | 依据 |
| --- | --- | --- | --- |
| 1 | DOC-01~07 由 Owner 核对 | ❌ 未做 | `index.md` 中 13 项「待 Owner 核对」，5 项已接受。**只有 Owner 能做** |
| 2 | IMP-01~28 有可复现本地证据 | ⚠️ 5/28 | 测试文件中**零处 IMP 标注**，无法逐条指认。可追溯的 5 项来自 M1 卡面：IMP-04、IMP-13、IMP-14、IMP-22、IMP-28 |
| 3 | CodeRoute 与 SliverShield 只读验收 | ❌ 未做 | 从未执行。**且基线 §4 已过期**——点名的 `CodeRoute-t30`/`CodeRoute-t55` 工作树在本机已不存在，实际为主仓 + 一个位于 `/private/tmp/cr-t61` 的 linked worktree，「三工作树归并」场景无法按原文复现 |
| 4 | DASH-01~12 全部通过 + 视觉验收 | ⚠️ 6/12 | 见下表；视觉验收需 Owner 人工 |
| 5 | 仓库无个人数据 / 密钥 / 真实源码副本 | ✅ 满足 | 批次 A–F 每轮 diff 均核，零泄漏 |
| 6 | 安装后调用可复现同一版本报告契约 | ❌ 未做 | 从未验证 |

### DASH-01~12 逐条（6 有 / 3 部分 / 3 空白）

证据来源为 `runtime/frontend/scripts/verify.mjs`（132 断言 × Chromium + WebKit）与 Python 侧测试。

| ID | 场景 | 状态 | 证据 |
| --- | --- | --- | --- |
| DASH-01 | 断网零外部请求 | ✅ | `clean-external-requests`，2 引擎 × 9 视图 |
| DASH-02 | 双引擎零 CSP 违规 + 阳性对照 | ✅ | `clean-console-errors`/`clean-page-errors`/`csp-style-positive-control`/`csp-connect-probe`/`probe-errors-separated` |
| DASH-03 | 注入语料以文本呈现 | ✅ | Python 侧参数化用例 + GJ-09 转义集合独立锚点 |
| DASH-04 | 多宽度无横向滚动 | ✅ | `no-horizontal-overflow`，5 宽 × 9 视图 = 45 切点 |
| DASH-05 | `partial` 快照首屏降级带 | ❌ | **无任何断言** |
| DASH-06 | 两次交互内到达完整证据 | ❌ | **无任何断言** |
| DASH-07 | 打印分支 | ✅ | `print-controls-hidden`/`print-details-expanded`/`print-full-locator` |
| DASH-08 | 纯键盘全流程 | ⚠️ | 有 `coverage-scope-link-focusable`/`deferred-search-focus`/`focused-project-enter-activation`；未覆盖「切视图 → 检索 → 筛选 → 展开证据 → 复制 locator」全链 |
| DASH-09 | `forced-colors` 五组状态可辨 | ✅ | 5 条 `forced-colors-*` 状态断言 + `forced-colors-media-active` |
| DASH-10 | 双快照身份条 + 跨版本深链 | ⚠️ | `version-mismatch` 视图已覆盖；两份快照并存的身份条区分未验 |
| DASH-11 | 复习三态可区分 | ⚠️ | `review-target` 视图与 `continued`/`developing` 夹具存在，无专门断言 |
| DASH-12 | Markdown 与 HTML 逐条一致 | ❌ | **无任何断言** |

### 端到端实测（2026-08-02，当前主干）

M1 全部工作完成后**首次**真实端到端运行，用合成工作区 + 临时数据目录，未触碰 Owner 真实状态：

```text
bootstrap           -> schema_version 10
authorize           -> receipt
validate_job_input  -> validation_sha256
scan                -> status completed / fresh_projects 1 / modules 1 / indexed_files 3 / role_lens_context 存在
scan（加排除规则）  -> status completed / fresh_projects 0 / excluded_projects 1 / indexed_files 0
```

第二次运行同时验证了两件刚合入的事：**GJ-03 的项目排除真实生效**（未读源码、未建快照），以及 **GJ-12 的终态修复首次在真实运行中兑现**（全排除时终态为 `completed` 而非 `failed`，修复前该运行会被 `history.py` 的 `status IN ('completed','partial')` 整个过滤掉）。

**后半条链未验**：`prepare_start` → 上下文访谈 → `record_analysis` → `render` → 英文导出，自 2026-07-28 起未在真实环境跑过重构后的形态。它有 182 个单测与跨引擎门禁覆盖，但单测证明的是部件行为不变，端到端证明的是串起来还对——两者不等价。

### 建议的推进顺序（供下一任 Architect 参考，非指令）

1. **IMP 追溯盘点**——性价比最高。把 28 项逐条对到现有测试并标注。大概率能把条件 2 从 5/28 推到 20+/28，剩下真缺的再出卡。**这不是缺功能，是缺一层标注。**
2. **补 DASH 三条空白（05/06/12）与两条部分（08/10/11）**——条件 4 的机检部分。
3. **裁定基线 §4 怎么办**——`t30`/`t55` 已不存在，需 Owner 定：重建工作树复现原场景，还是改为描述当前实际形态。这是**权威文档变更，需 Owner 核对**。
4. **完整端到端跑通**（含后半条链）——它同时是条件 3 的前置。
5. **安装后可复现性验证**（条件 6）。
6. **Owner 核对 13 份文档**（条件 1）。

### 机动池（未出卡）

| 任务 | 说明 | 触发条件 |
| --- | --- | --- |
| GJ-10 · 剥离遍历与 ignore 簇 | GJ-06 的第二步。**GJ-06 合入后实测**：`WorkspaceScanner` 已降至 2509 行 / 52 方法，本簇为 `_discover`、`_walk_directories`、`_non_git_manifest`、`_iter_project_files`、`_is_sensitive`、`_classify` 共 6 方法 459 行，加 `IgnoreMatcher` 142 行，合计约 601 行。该簇管的是「扫描器能看到什么」——硬安全排除清单与敏感文件判定在其中。**GJ-06 已于 `ed53ff2` 合入，前置满足**。出卡时须在 GJ-06 的 DoD 基础上补一条「对外暴露名字的可写性对等」——见 GJ-06 验收的方法论盲区 | 待 Owner 决定 |
| CI 接入 | 仓库无任何 CI 配置，门禁全靠本地。GJ-07 明确把此项排除在外，是独立决策 | Owner 决定 |

## 已完成

| 任务 | 结论 | 信道 |
| --- | --- | --- |
| 看板呈现契约与 ADR-0008 | [dashboard-design.md](../20-architecture/dashboard-design.md)、[ADR-0008](../30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md) 已接受 | 部署前 |
| 首版实现评审与文档回写 | `D-043`/`D-044`/`EVID-INV-27`/`DASH-INV-11`/`F-009` 已落契约；`D-014`/`D-034`/`D-035` 已收窄 | 部署前 |
| 批次 A · GJ-01 + GJ-02 | 渲染门禁与归因校验改为按结构分层判定；merge `0e75a78` | #4 交付 / #5 验收 / #6 收口 |
| 批次 B · GJ-08 + GJ-04 + GJ-03 + GJ-05 | 数据区运行期守卫与归因双投影、看板跨引擎行为门禁、项目级排除与 `excluded` 生产者、ignore 子集显式化；merge `711cb33` | #7 派卡 / #9 L1 / #10 裁决 / #11 交付 / #12 验收收口 |
| 批次 C · GJ-09 | 转义集合内容获得独立于该集合的测试锚点，批次 B 验收发现的覆盖空洞闭合；merge `9f06b8f` | #13 交付 / #14 验收收口 |

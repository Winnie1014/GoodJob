# GoodJob 任务池（backlog）

> 状态：协作运行区，非产品契约
> 权威范围：任务状态的唯一事实源（谁在做、做到哪、裁决落在哪）
> 上游：[验收基线](acceptance-baseline.md)、[决策账本](../30-decisions/decision-log.md)
> 维护者：Architect。Implementer 只读，状态变更走[信道](../collab/channel.md)。

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

### 后续单卡

| 任务 | 卡面 | 状态 | 前置 | 验收项 |
| --- | --- | --- | --- | --- |
| GJ-08 · 让分层判定各自守得住 | [GJ-08](../collab/tasks/GJ-08.md) | 已出卡·待派 | 批次 A（已合入） | `IMP-28`、`DASH-03`、`IMP-14`、`IMP-22` |
| GJ-04 · 跨引擎行为门禁进入运行时前端 | [GJ-04](../collab/tasks/GJ-04.md) | 已出卡·排队 | 批次 A（已合入） | `IMP-28` |
| GJ-03 · 项目级排除与 `excluded` 生产者 | [GJ-03](../collab/tasks/GJ-03.md) | 已出卡·排队 | 无 | `IMP-04`、`IMP-13` |
| GJ-05 · ignore 子集显式化并可见 | [GJ-05](../collab/tasks/GJ-05.md) | 已出卡·排队 | 无 | `IMP-04`、`SCAN-04` |

GJ-08 与 GJ-03/GJ-04/GJ-05 文件范围互不重叠（GJ-08 碰 `reporting.py`/`analysis.py`，GJ-03/GJ-05 碰 `scanner.py`，GJ-04 碰 `frontend/`），派发顺序由 Owner 定。

### 机动池（未出卡）

| 任务 | 说明 | 触发条件 |
| --- | --- | --- |
| GJ-06 · 拆分 `WorkspaceScanner` | 纯重构，3986 行单文件、`WorkspaceScanner` 一类承担遍历/ignore/Git/分类/持久化/覆盖聚合。建议先剥「遍历 + ignore」与「Git 元数据 + 沙箱调用」两块 | GJ-05 合入后由 Owner 决定是否排期；不得与 GJ-01~GJ-05 混提交 |
| GJ-07 · 聚合门禁入口 | Python 与前端目前是四条独立命令、无聚合入口，Python 侧测试不触发 `build:check` | 随任一卡顺手做则单独提交；否则挂账 |

## 已完成

| 任务 | 结论 | 信道 |
| --- | --- | --- |
| 看板呈现契约与 ADR-0008 | [dashboard-design.md](../20-architecture/dashboard-design.md)、[ADR-0008](../30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md) 已接受 | 部署前 |
| 首版实现评审与文档回写 | `D-043`/`D-044`/`EVID-INV-27`/`DASH-INV-11`/`F-009` 已落契约；`D-014`/`D-034`/`D-035` 已收窄 | 部署前 |
| 批次 A · GJ-01 + GJ-02 | 渲染门禁与归因校验改为按结构分层判定；merge `0e75a78` | #4 交付 / #5 验收 / #6 收口 |

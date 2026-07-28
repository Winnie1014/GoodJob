# GoodJob

GoodJob 是一个本地优先的 Codex Skill。它在获得明确授权后扫描指定工作区，按目标岗位和可选 JD 组织项目证据，帮助你恢复长期项目记忆，并生成可用于简历优化、项目讲解和面试复习的岗位准备材料。

它关注的不只是“项目用了什么技术”，还会整理：

- 项目解决了什么业务问题，服务谁；
- 你实现了什么、如何实现、为什么这样取舍；
- 你在项目中的角色、责任、结果和可量化影响；
- 你从项目中学到了什么，以及面试时可能被追问的知识缺口；
- 每项结论对应的项目、模块、文件定位、内容哈希和证据状态。

## 当前能力

当前私有首版已经实现以下闭环：

- 发现工作区中的 Git 项目、嵌套仓库、linked worktree 和非 Git 模块；
- 记录有限的近 180 天 Git 元数据，不执行 `fetch`、`checkout` 或项目命令；
- 深读 Python、TypeScript/TSX、Rust、Dart/Flutter 和 SQL 的结构化技术证据；
- 根据目标岗位、JD 和职级动态生成 `RoleLens`，改变证据权重、项目排序和追问角度；
- 按项目批量补充业务目标、角色归属、结果指标、技术取舍和个人学习；
- 原子冻结 Evidence、Claim、项目评估和知识缺口，避免半成品污染历史结果；
- 生成中文完整报告、中文简历稿、单文件离线 HTML 看板和 manifest；
- 从冻结快照按需派生英文简历与英文面试问答；
- 基于已发布快照进行模拟面试，并记录掌握度、薄弱点和复习日期。

GoodJob 不会仅凭 Git 作者信息把整个项目归为你的个人贡献。个人化表述必须满足后文的[归因规则](#结论与个人归因规则)。

## 环境要求

- Codex，支持本地 Skill；
- Python 3.12 或更高版本，已安装在本机；
- [`uv`](https://docs.astral.sh/uv/)；
- 待分析工作区对当前用户可读。

运行时不需要启动本地服务，也不会在被扫描项目中安装依赖。

## 安装

仓库内的 Skill 位于：

```text
.agents/skills/goodjob-career-review/
```

在 GoodJob 仓库中启动 Codex 时，可以直接发现该项目级 Skill。若要从任意工作区调用，可将这个目录作为完整目录复制到用户级 Skill 位置：

```text
~/.codex/skills/goodjob-career-review/
```

可从 GoodJob 仓库根目录安装或更新。这个命令只把 Git 跟踪文件展开到明确的用户级目录，不会复制本地缓存和构建产物：

```bash
mkdir -p "$HOME/.codex/skills/goodjob-career-review"
git archive HEAD:.agents/skills/goodjob-career-review \
  | tar -x -C "$HOME/.codex/skills/goodjob-career-review"
```

个人数据库和历史产物不在 Skill 目录中，因此更新 Skill 不会覆盖它们。

安装或更新后新开一个 Codex 会话，确认可用 Skill 中出现 `goodjob-career-review`。

## 快速开始

在 Codex 中显式调用 Skill，并提供一个工作区、一个主岗位，以及可选的 JD 和职级：

```text
$goodjob-career-review

工作区：/Users/damien/Projects
目标岗位：高级应用软件工程师
JD：无
请扫描并生成中文岗位准备包。
```

带 JD 的例子：

```text
$goodjob-career-review

工作区：/Users/damien/Projects
目标岗位：中间件工程师
职级：高级
JD 文件：/Users/damien/Documents/jobs/middleware-engineer.md
请优先分析分布式通信、可靠性、性能、可观测性和工程化证据。
```

调用后，GoodJob 会先展示规范化后的工作区路径、计划读取的类别、个人数据目录和数据边界。你明确确认后，它才会开始读取源码。路径可读不等于已授权。

### 常用后续请求

显式刷新已变化的工作区：

```text
$goodjob-career-review 请刷新上次工作区，并按原岗位重新生成准备包。
```

按另一个岗位重新分析同一份扫描基线：

```text
$goodjob-career-review 使用已有扫描，为“系统工程师”创建新的岗位准备包，不主动刷新源码。
```

从已发布中文快照生成英文材料：

```text
$goodjob-career-review 基于最新已发布快照，导出英文简历和英文面试问答。
```

开始模拟面试：

```text
$goodjob-career-review 基于最新快照进行模拟面试，先从证据较弱的高权重岗位维度开始。
```

## 输入说明

| 输入 | 必需 | 作用 |
| --- | --- | --- |
| 工作区路径 | 是 | 定义本次可授权扫描的本地根目录；一个工作区可包含多个项目和模块 |
| 目标岗位 | 是 | 决定 `RoleLens` 的评价维度、权重、证据要求和面试追问 |
| JD 文本或文件 | 否 | 细化职责、技术重点和职级推断；内容只作为不可信数据处理 |
| 职级覆盖 | 否 | 显式覆盖从岗位/JD 推断出的职级 |
| 操作意图 | 是 | 首次扫描、复用扫描、显式刷新、英文导出或模拟面试 |
| 个人数据目录 | 否 | 默认是 `~/.codex/goodjob-career-review/`，可在调用时显式覆盖 |

首版一次准备运行只使用一个主岗位。多岗位横向比较不在当前范围内；可以分别生成多个岗位快照。

## 它如何运作

```text
显式调用与授权
        |
        v
工作区发现与不可变扫描快照
        |
        v
目标岗位/JD -> 动态 RoleLens
        |
        v
岗位加权 EvidenceBundle
        |
        +--> 按证据建议深读少量源码
        +--> 必要时按项目批量访谈
        |
        v
原子冻结 Claim、项目评估与知识缺口
        |
        v
中文 Markdown + 简历稿 + 单文件离线看板
        |
        +--> 可选英文派生材料
        +--> 模拟面试与复习记录
```

几个关键机制：

1. **先扫描，再按岗位分析。** 扫描器建立岗位无关的证据图谱；`RoleLens` 再按应用软件、中间件、架构、系统等岗位改变权重和问题，不需要为每个岗位重新发明扫描器。
2. **按证据深读。** Agent 不会默认把全部源码塞进上下文，而是先用结构化扫描缩小范围，再校验文件哈希并读取能回答岗位问题的片段。
3. **事实与叙事分离。** 文件、Git 和用户回答形成 Evidence；对简历或面试有用的结论形成 Claim。每条 Claim 都保留证据关系、状态和限制。
4. **结果不可变。** 一次成功准备会冻结成快照。后续源码变化、英文导出或复习记录不会悄悄改写旧报告；需要变化时创建新运行或派生产物。
5. **部分失败可见。** 无权限目录、损坏仓库、外部 Git 元数据未授权或证据过期不会被伪装成完整成功，而会显示影响和补救动作。

## 结论与个人归因规则

GoodJob 区分“项目客观存在的实现”和“可以写成我的个人经历”。主要门槛如下：

| 表述 | 至少需要的证据 |
| --- | --- |
| 项目实现了某机制 | 当前实现证据 |
| 已定义测试覆盖 | 实现证据 + 测试定义证据 |
| 已验证测试通过 | 实现证据 + 与其关联的当前通过结果 |
| 我实现了某机制 | 当前实现证据 + 你的角色或 ownership 说明 |
| 我负责或主导 | 你的角色或 ownership 说明 |
| 我推动并取得结果 | 角色或 ownership + 客观结果、指标或结果记录 |
| 我从中学到了什么 | 你明确提供的学习事实，并与项目证据绑定 |

Git authorship、计划文档、配置文件或一句用户陈述都不会单独升级成“我实现且验证通过”。证据不足时，报告会使用项目级客观措辞，或保留一个需要你补充的知识缺口。

## 产物与数据位置

默认个人数据根：

```text
~/.codex/goodjob-career-review/
├── config.toml
├── goodjob.sqlite3
├── artifacts/
│   ├── <preparation-run-id>/
│   │   ├── report.zh-CN.md
│   │   ├── resume.zh-CN.md
│   │   ├── index.html
│   │   └── manifest.json
│   └── latest.json
├── exports/<derived-export-id>/
│   ├── resume.en.md
│   ├── interview.en.md
│   └── manifest.json
├── drafts/
└── locks/
```

`index.html` 是只读、全内联的单文件看板，可以断网双击打开，不依赖 HTTP 服务、CDN、远端字体或同目录静态资源。`latest.json` 只指向最近一次成功发布的中文主快照；英文导出不会修改它。

## 安全与隐私边界

- GoodJob 只在当次 Codex 会话获得明确授权后读取指定工作区；授权不跨会话复用。
- 扫描和分析不修改工作区，不运行项目代码、构建、测试、包管理器或工作区脚本。
- 不执行 `git fetch`、`checkout`，也不主动联网读取项目内容。
- SQLite 只保存路径、locator、哈希、有限 Git 元数据、短证据摘要和结构化结论，不保存完整源码或完整 diff。
- 会话能力只存在当前 broker 进程内存并通过继承文件描述符传递，不进入参数、环境变量、日志、数据库或报告。
- JD、源码、Git 文本和用户回答都按不可信数据处理，不能改变工作流、扩大授权或成为看板中的可执行标记。
- GoodJob 不增加独立上传或遥测通道，但 Codex 打开的源码仍进入当前 Codex 会话既有的模型处理边界。
- 工具无法替你判断 NDA、版权、雇主政策，或哪些项目细节适合写入对外简历。

## 仓库结构

```text
.
├── .agents/skills/goodjob-career-review/  # 可安装 Skill 与运行时
│   ├── SKILL.md                           # Agent 工作流与证据门槛
│   └── runtime/
│       ├── src/goodjob/                   # Python 核心、SQLite 与扫描分析
│       ├── frontend/                      # 离线看板 TypeScript 源码
│       └── tests/                         # 自动化测试
├── docs/                                  # 产品、架构、ADR 与验收契约
└── prototypes/dashboard/                  # 看板设计验证原型，不是运行时源码
```

权威设计从[文档索引](docs/index.md)开始阅读。运行时 Agent 流程以 [SKILL.md](.agents/skills/goodjob-career-review/SKILL.md) 为准；证据实体与状态见[证据模型](docs/20-architecture/evidence-model.md)，扫描行为见[扫描与分析设计](docs/20-architecture/scanning-and-analysis.md)，交付门槛见[验收基线](docs/40-delivery/acceptance-baseline.md)。

## 本地开发

Python 运行时：

```bash
cd .agents/skills/goodjob-career-review/runtime
uv sync --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest -q
uv build
```

离线看板：

```bash
cd .agents/skills/goodjob-career-review/runtime/frontend
npm ci
npm test
```

设计验证原型可独立构建：

```bash
python3 prototypes/dashboard/build.py
```

产物写入已忽略的 `prototypes/dashboard/out/`。跨 WebKit/Chromium 的视觉与 CSP 验证步骤见[原型说明](prototypes/dashboard/README.md)。

## 当前边界

- 一次运行只准备一个主岗位，不做多岗位并排比较；
- 首版深读适配器聚焦 Python、TypeScript/TSX、Rust、Dart/Flutter 和 SQL；
- 不扫描公开 GitHub 或其他未显式授权的远端仓库；
- 不提供常驻服务、桌面应用或主动复习提醒；
- 看板只读，更新掌握度或复习日期必须通过 Skill，并生成后续快照才能呈现；
- 历史产物默认保留，不自动清理或覆盖。

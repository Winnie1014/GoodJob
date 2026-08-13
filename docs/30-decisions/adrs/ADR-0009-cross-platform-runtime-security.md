# ADR-0009：跨平台运行时安全（macOS/Linux/WSL2 沙箱与 fail-closed 边界）

> 状态：已接受（真实 Linux 门已通过；Architect 于 2026-08-13 确认接受）
> 日期：2026-08-12
> 权威范围：macOS、Linux（含 WSL2）的 Git 沙箱后端选择、进程身份、fail-closed 边界和平台等价性契约
> 上游：[产品需求](../../10-product/product-requirements.md)（FR-16、NFR-09）、[跨平台多 Agent 适配计划](../../40-delivery/cross-platform-multi-agent-plan.md) §4
> 下游：[系统设计](../../20-architecture/system-design.md)、[扫描与分析设计](../../20-architecture/scanning-and-analysis.md)、[验收基线](../../40-delivery/acceptance-baseline.md)
> 关系：部分替代 [ADR-0001](ADR-0001-skill-and-state-isolation.md) 的 macOS-only 平台限定；部分替代 [ADR-0006](ADR-0006-authorized-codex-analysis-and-external-git-metadata.md) 的 macOS-only Git 沙箱前提。旧 ADR 正文保留为历史决策记录，不被改写。

## 背景

ADR-0001 和 ADR-0006 在 2026-07-24 制定时，GoodJob 运行时只在 macOS 上可用：Git 子进程通过 `sandbox-exec` Seatbelt 沙箱运行，进程身份通过 BSD `ps` 获取。`git_metadata.py` 中的 `sys.platform != "darwin"` 守卫使非 macOS 平台直接 fail-closed。

用户目标要求支持 Linux 和 WSL2 平台（原生 Windows 留待后续阶段）。Linux 共享全部 POSIX API（`dir_fd`、`fcntl`、`os.pipe`、`pass_fds`、`O_NOFOLLOW`），因此 Phase 1 改动量小：只需替换 sandbox-exec（→ bwrap）和 BSD ps（→ `/proc`），其余 FS/锁/能力层原样可用。

## 决策

### 1. 平台后端选择器

在 `runtime/src/goodjob/platform/` 新建轻量平台抽象层：

- `detect.py`：`detect_platform()` 基于 `sys.platform` 返回 `Platform.MACOS` / `Platform.LINUX` / `Platform.WINDOWS`；`select_git_sandbox()` 工厂方法按平台选择沙箱后端
- `sandbox_macos.py`：`SeatbeltSandbox`（从 `git_metadata.py` 提取的现有代码）
- `sandbox_linux.py`：`BwrapSandbox`（新）

平台选择在运行时自动决定，不提供手动覆盖。不支持的平台 fail-closed。

### 2. bwrap 沙箱后端

`BwrapSandbox.build_command()` 生成等价于 macOS Seatbelt 的 bwrap 命令行：

| bwrap 参数 | 等价 Seatbelt | 安全作用 |
|---|---|---|
| `--unshare-net` | `(deny network*)` | 拒绝网络 |
| `--unshare-pid` | （隐含在 deny default） | 私有 PID namespace，防止沙箱内进程看到宿主进程列表 |
| `--ro-bind <root> <root>` | `(allow file-read* (subpath AUTHORIZED_ROOT))` | 只读授权根 |
| `--ro-bind /usr /usr` | `(allow file-read* (literal GIT_EXECUTABLE))` | 系统库 + git 二进制可读 |
| `--ro-bind /lib /lib` + `--ro-bind-try /lib64 /lib64` | 同上 | 系统库 |
| `--ro-bind-try /etc /etc` | — | git 需 `/etc/passwd`、`/etc/group` 做身份解析 |
| `--dev /dev` | `(allow file-write-data (literal "/dev/null"))` | 全新 devtmpfs |
| `--proc /proc` | — | procfs（必须在 `--unshare-pid` 之后挂载，否则暴露宿主进程 cmdline） |
| `--tmpfs /tmp` | — | 空 tmpfs，避免宿主 `/tmp` 泄漏 |
| `--die-with-parent` | — | 父进程退出时杀死子进程 |

**安全权衡：** bwrap 使 `/usr`、`/etc` 可读，比 macOS Seatbelt 的"仅 AUTHORIZED_ROOT + GIT_EXECUTABLE"更宽。但 `/usr`、`/etc` 是系统文件不含用户数据；用户 home 目录和其他工作区不可达是关键安全属性。这一差异已记录并接受。

### 3. `--proc /proc` 必须在 `--unshare-pid` 之后

bwrap 的 `--proc /proc` 挂载 procfs。如果在 `--unshare-pid` 之前挂载，procfs 会暴露宿主进程的 cmdline（包括环境变量和参数中的 capability）。因此在 `BwrapSandbox.build_command()` 中，`--unshare-pid` 始终出现在 `--proc` 之前。实现中有测试断言这一顺序。

### 4. fail-closed 边界

- bwrap 不存在（仅检查 `/usr/bin/bwrap`，不信任继承 `PATH`）时，`BwrapSandbox.build_command()` 抛出 `GitSandboxUnavailableError`，不回退到无沙箱
- bwrap 已安装但无法运行（WSL1 不支持用户命名空间、Ubuntu 23.10+ AppArmor 限制、Debian `kernel.unprivileged_userns_clone`、容器/CI 环境常禁 userns，或挂载参数顺序覆盖授权根）时，启动器启动失败或以 `bwrap:` 错误退出；运行时将其分类为 `git_sandbox_unavailable`，给出安装/启用指引，同样 fail-closed
- Linux Git 命令通过 `GIT_NO_LAZY_FETCH=1` 环境变量禁用 lazy fetch；不使用 macOS-only 的 `--no-lazy-fetch` argv 选项，以兼容系统 Git 版本，同时保持同一安全语义
- macOS 上 `sandbox-exec` 不存在时同样 fail-closed
- WSL1 不支持用户命名空间，因此 WSL1 上 bwrap 后端 fail-closed；只有 WSL2 提供完整沙箱

### 5. 进程身份

macOS 分支保持现有 `/bin/ps -o lstart=` 不变。Linux 分支读取 `/proc/<pid>/stat` 第 22 字段（starttime jiffies）——无需子进程，格式稳定。两者满足同一契约：同一进程的标识稳定，PID 重用时标识变化。返回格式不重要（`pid:NNN;started:MMM`），只要同进程一致、PID 重用时变化。

### 6. 与 ADR-0001/ADR-0006 的关系

本 ADR 不改写 ADR-0001 和 ADR-0006 的历史决策正文。以下条款声明 supersede：

- **ADR-0001 决策 5**：个人数据目录默认 `~/.codex/goodjob-career-review`。本 ADR 不改变数据目录位置（Phase 2 处理 agent 无关化），但数据目录在 Linux 上同样可用。
- **ADR-0006 决策 1-8**：授权回执、SessionCapability、根外 Git 两阶段授权等安全模型在 macOS 和 Linux 上完全一致，只是 Git 沙箱后端不同。ADR-0006 中隐含的 macOS-only 前提（`sandbox-exec`）由本 ADR 扩展为"macOS Seatbelt 或 Linux bwrap"。

### 7. 不在范围

- 原生 Windows 平台支持（WFP 网络隔离、NT handle-relative FS、direct launcher/Job Object）——留待 Phase 3
- Agent 无关打包（DB 迁移、参数化 Codex 硬编码、启动器脚本）——留待 Phase 2
- CI/GitHub Actions 矩阵——红区 L1，另出任务卡
- Git 进程的完整文件系统读隔离（bwrap 使 `/usr`、`/etc` 可读是已接受的差异）

## 影响

- macOS + Linux（含 WSL2）均享完整沙箱安全模型
- `git_metadata.py` 的平台守卫从 `sys.platform != "darwin"` 改为 `select_git_sandbox()` 后端选择
- `process_identity.py` 按平台分支选择 BSD `ps` 或 `/proc/<pid>/stat`
- 既有 macOS 行为零回归：SeatbeltSandbox 从 `git_metadata.py` 提取到 `platform/sandbox_macos.py`，Git argv 仍由单一层构造并逐字回归
- Linux/WSL2 真机验收必须在真实环境中通过（不是 mock/monkeypatch 替代）
- `dependencies = []` 不破坏——bwrap/sandbox-exec/ps 是系统二进制（与 sandbox-exec 同性质），不是 Python 包依赖

## 否决方案

- **无沙箱回退**：违背 fail-closed 原则，Git 子进程将能访问网络和任意文件
- **Docker/容器沙箱**：引入重依赖，不适合本地优先工具；bwrap 是无守护进程的轻量替代
- **Firejail**：不如 bwrap 普遍可用，且 API 不如 bwrap 稳定
- **仅用 Git 环境变量"禁网"**：不是真正的网络隔离，helper 进程可绕过；ADR-0006 已拒绝此路径
- **`/proc` 挂载在 `--unshare-pid` 之前**：暴露宿主进程 cmdline，包括参数中可能携带的 capability（评审历史中已被拒绝的错误方案）

## 验证

- `make gate` 在 macOS 和 Linux（含 WSL2）上分别全绿；macOS 证据已具备，Linux 证据为 Ubuntu 24.04 / bwrap 0.9.0 上对提交 `8371c09` 的完整真机门：Python `304 passed, 4 skipped`（仅跳过 macOS Seatbelt 用例），前端 typecheck/lint/unit/build-check 全绿，文档门全绿；WSL2 复用同一 Linux 后端，未单列第二套实现
- macOS sandbox-exec 测试加 `@pytest.mark.skipif(sys.platform != "darwin")`，Linux bwrap 测试加 `@pytest.mark.skipif(not linux or not bwrap)`
- E2E broker 测试在两个平台各跑一次真实沙箱
- `BwrapSandbox.build_command()` 测试断言 `--tmpfs /tmp` 先于授权根 `--ro-bind`，`--proc` 出现在 `--unshare-pid` 之后，且显式 `--chdir` 到授权根内的具体 worktree；授权根可能包含多个项目，不能用它替代 Git 当前工作目录
- Git 仓库配置和 `config.worktree` 的 `include.path` / `includeIf.*.path` 在执行其他 Git 命令前以 `--no-includes --show-origin` 枚举；相对路径按声明文件位置解析，根外路径显式 fail-closed，不能依赖 Seatbelt 的 `EACCES` 或 bwrap 空路径的 `ENOENT` 差异
- bwrap 不存在时 `BwrapSandbox.is_available()` 返回 `False`，`build_command()` 抛出 `OSError`
- 进程身份测试断言同一进程的 marker 稳定，不存在 PID 的 marker 为 `None`

## 附录：全量影响清单

以下清单逐条标注跨平台 Phase 1 对既有文档和 ADR 的影响归属。清单闭合前不得称 Phase 1 独立可交付。

| 条目 | 内容 | 归属 | 说明 |
|---|---|---|---|
| `docs/index.md:10` | 平台状态行："已进入私有首版的实现与验证阶段" | **本 Phase 同步** | 已更新为"支持 macOS 和 Linux（含 WSL2）"，引用 ADR-0009 |
| `docs/index.md:93` | Owner 核对清单 #1："产品以显式 Codex Skill 为入口" | **Phase 2 同步** | 绑定 Codex 的入口产品范围；Phase 2 的 Agent 无关化（ADR-0010）才能闭合。Phase 1 不得提前改成"多 Agent 已支持" |
| `docs/index.md:97` | Owner 核对清单 #5："每个 Codex task 用不落库的易失 SessionCapability" | **Phase 2 同步** | 绑定 Codex task 的会话能力模型；Phase 2 的完整 Agent 契约闭包才能闭合。Phase 1 不改变会话能力模型 |
| `docs/20-architecture/artifacts-and-learning.md:12` | "Owner 通过显式 Skill 会话...并在任何项目衍生信息进入 Codex 分析前确认本会话范围回执" | **Phase 2 同步** | 绑定 Codex 分析的会话授权描述；Phase 2 的 Agent 无关化才能将"Codex 分析"改为"host agent 分析"。Phase 1 只扩展平台，不改变 agent 耦合 |
| `ADR-0003` | 证据指针与摘要，不保存源码快照 | **历史保留** | 与平台无关的架构决策，不涉及 macOS/Linux 限定。本 Phase 不修改 |
| `ADR-0007` | 复习状态谱系与冻结快照完整性 | **历史保留** | 与平台无关的架构决策，不涉及 macOS/Linux 限定。本 Phase 不修改 |
| `ADR-0001` | Skill 版本资产与个人状态分离 | **本 Phase 同步（关系头）** | 状态行标注"平台限定部分由 ADR-0009 替代"；历史决策正文不改写 |
| `ADR-0006` | Codex 源码分析授权与根外 Git 元数据绑定 | **本 Phase 同步（关系头）** | 状态行标注"macOS-only Git 沙箱前提由 ADR-0009 扩展为 macOS/Linux 双平台"；历史决策正文不改写 |
| `docs/10-product/product-requirements.md` | FR-16、NFR-09 | **本 Phase 同步** | 新增跨平台 Git 沙箱与进程身份需求 |
| `docs/00-product/vision-and-goals.md` | 产品愿景平台范围 | **本 Phase 同步** | 更新为"支持 macOS 和 Linux（含 WSL2）"，术语表引用 ADR-0009 |
| `docs/20-architecture/system-design.md` | 系统设计平台边界 | **本 Phase 同步** | 更新运行时边界描述、需求到组件映射 |
| `docs/20-architecture/scanning-and-analysis.md` | 扫描设计平台引用 | **本 Phase 同步** | 上游引用增加 ADR-0009，Git 子进程沙箱说明 |
| `docs/40-delivery/acceptance-baseline.md` | 验收基线 | **本 Phase 同步** | 新增 IMP-29 跨平台验收项、FR-16/NFR-09 追溯、平台门禁要求 |
| `docs/30-decisions/decision-log.md` | 决策账本 | **本 Phase 同步** | 新增 D-045 跨平台运行时安全决策 |
| `README.md` | 环境要求与安全边界 | **本 Phase 同步** | 更新为 macOS + Linux（含 WSL2），补充 bwrap 要求 |
| `docs/collab/protocol.md §8` | 平台行 | **本 Phase 同步** | 从"macOS-only"改为"macOS 和 Linux（含 WSL2）"，引用 ADR-0009 |

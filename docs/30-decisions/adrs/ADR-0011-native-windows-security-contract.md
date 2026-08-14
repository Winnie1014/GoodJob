# ADR-0011：原生 Windows 安全运行时契约

> 状态：待 Owner/Architect 接受
> 日期：2026-08-14
> 权威范围：原生 Windows 的 WFP/Job 网络与进程边界、NT handle-relative 文件系统边界、direct launcher、capability handle、句柄所有权、失败关闭时序和唯一允许的安全降级
> 上游：[产品需求](../../10-product/product-requirements.md)（FR-18、NFR-11）、[跨平台多 Agent 适配计划](../../40-delivery/cross-platform-multi-agent-plan.md) §6、SWO-31 真机 spike 证据
> 下游：[系统设计](../../20-architecture/system-design.md)、[扫描与分析设计](../../20-architecture/scanning-and-analysis.md)、[验收基线](../../40-delivery/acceptance-baseline.md)（IMP-31）
> 关系：接受后扩展 [ADR-0009](ADR-0009-cross-platform-runtime-security.md) 的平台集合，并替代其“原生 Windows 留待 Phase 3、尚无权威后端”的范围声明；同时扩展 [ADR-0006](ADR-0006-authorized-codex-analysis-and-external-git-metadata.md) 决策 3 的传输条款，使 inherited FD 仅适用于 POSIX、原生 Windows 改用 allowlisted inherited HANDLE。ADR-0006 的授权语义、ADR-0009 对 macOS/Linux/WSL2 的决策与 [ADR-0010](ADR-0010-host-agent-neutral-session.md) 的 host agent 准入契约继续有效。

## 背景

[跨平台多 Agent 适配计划](../../40-delivery/cross-platform-multi-agent-plan.md) §6 把 WFP、NT handle-relative FS 和 direct `CreateProcessW` 写成候选 spike 假设，并明确要求真机验证后才能形成权威架构。SWO-31 已在 Windows build 26200.8875、NTFS、64-bit Python 环境完成原语与组合复核：

- 修正 ctypes ABI 后，WFP dynamic session 可打开；按 application ID 安装并回读 V4/V6 ALE filters、关闭会话后自动清理成立。早期 `FwpmEngineOpen0 -> ERROR_NOT_SUPPORTED (50)` 来自错误函数签名、截断的 `FWPM_SESSION0` 和错误参数，已经撤回，不是 Windows/WFP 不支持证据。
- `NtCreateFile(OBJECT_ATTRIBUTES.RootDirectory=...)` 可逐组件相对打开；`FILE_OPEN_REPARSE_POINT` 下 junction 不被跟随，volume serial 与 file ID 可由句柄回读。
- direct `CreateProcessW(CREATE_SUSPENDED) -> AssignProcessToJobObject -> ResumeThread` 成立；进程在执行用户入口前已经进入 Job，`ACTIVE_PROCESS=1` 能拒绝成员派生进程。
- 对真实 `C:\Program Files\Git\mingw64\bin\git.exe` 的组合测试中，本地 `rev-parse`/`log` 成功，远程 helper 派生被 Job 拒绝，WFP filters 在会话关闭后消失。
- 保留 `CREATE_NO_WINDOW` 时，headless `conhost.exe` 可出现在 Job accounting 中，但不会消耗 Job 成员的派生名额；真实 Git helper 仍被 `ACTIVE_PROCESS=1` 拒绝。

这些证据证明候选原语可行，不证明产品实现已经存在，也不代替正式发行版 Windows、真实 IPv6 出口、完整文件系统对抗矩阵、关闭竞态、资源泄漏和 bounded-output 的真机验收。原始证据来源是 SWO-31 评论 `1056aa38`、`01d6ff03`、`259341c8` 及其附件；最早的 `d1b01aa7` 只可用于 NT FS/direct launcher 的已复核部分，其 WFP error 50 结论不得引用。

## 决策

### 1. 支持状态与唯一降级

本 ADR 形成待终审的候选实现契约，但不把原生 Windows 标记为 supported。只有本 ADR 被接受、运行时代码完成并通过 [IMP-31](../../40-delivery/acceptance-baseline.md#33-后续原生-windows-准入门) 的全部真机阳性/负向验收后，支持矩阵、README 和运行前提示才能改为 supported；此前必须 fail-closed，并推荐 WSL2。

唯一允许的安全降级是：Windows Git 子进程没有文件系统读隔离，可能读取授权根外的本机文件。该降级必须在每次原生 Windows 运行前可见。以下边界不得降级：

- Git 网络隔离；
- Python 扫描器的授权根与 reparse/TOCTOU 边界；
- Git 与业务子进程树回收；
- SessionCapability 与私有 payload 的最小 handle 继承隔离。

任一不可建立时整次相关操作失败，不得回退到无 WFP、pathname 检查、`subprocess.Popen` 或宽泛 handle 继承。

### 2. Git 网络与进程树边界

Windows Git 启动必须满足以下顺序和约束：

1. 直接定位 Git for Windows 的真实 `mingw64\bin\git.exe`，拒绝把会先派生真实 Git 的 `cmd\git.exe` shim 作为入口。
2. 用 `FwpmGetAppIdFromFileName0` 从同一个规范化真实二进制生成 application ID。
3. 用 `FwpmEngineOpen0` 建立 dynamic session，在 `ALE_AUTH_CONNECT_V4/V6` 与 `ALE_AUTH_RECV_ACCEPT_V4/V6` 安装 block filters，并按 filter ID 全部回读确认。
4. 预建 `KILL_ON_JOB_CLOSE | ACTIVE_PROCESS=1` 的 Git Job；`ACTIVE_PROCESS=1` 表示入口 Git 不允许派生 helper。
5. filters 未全部安装并回读前不得 resume 进程。WFP/BFE 不可用、无安装权限、回读不一致或真实 Git 无法固定时 fail-closed。

`GIT_NO_LAZY_FETCH`、`GIT_TERMINAL_PROMPT`、`GIT_ALLOW_PROTOCOL` 和禁用 hooks/config/helper 只作为纵深防御，不得记为网络隔离证据。WFP dynamic-session handle 必须活到 Job 中进程全部终止之后，确保清理阶段也不存在网络窗口。

### 3. NT handle-relative 文件系统边界

授权根只在入口从绝对路径打开一次，并立即用 `GetFinalPathNameByHandleW` 与 `GetFileInformationByHandleEx(FileIdInfo)` 固定规范显示路径、volume serial 和 file ID。后续授权只由 root/parent handle 及该身份链决定，不由缓存 pathname 决定。

平台接口只接受单个相对名称组件，并在进入 NT API 前拒绝：

- 绝对路径、drive、UNC 或 device prefix；
- 空组件、`.`、`..`；
- `/`、`\` 等路径分隔符；
- alternate data stream 分隔符 `:`。

每个组件必须经 `NtCreateFile(OBJECT_ATTRIBUTES.RootDirectory=<verified parent>)` 与 `FILE_OPEN_REPARSE_POINT` 相对打开，随后以 `FileAttributeTagInfo`/`FileIdInfo` 校验对象类型、reparse tag、volume 与身份。读取、枚举、创建、rename、publish 与删除都继续使用已经验证的 handle：rename 使用带目标 parent handle 的 `SetFileInformationByHandle(FileRenameInfoEx)`，delete 使用对象 handle 上的 disposition information。禁止 `GetFileAttributesW -> CreateFileW`、`DeleteFileW`、`RemoveDirectoryW` 或任何把已验证名称重新交给 pathname API 的降级。非 NTFS、跨卷或无法证明句柄原子语义的场景在获得单独真机证据前一律 fail-closed。

`safe_fs.py` 八项操作以及 scanner 的 readlink/枚举必须统一委托给该后端；详细操作契约由[扫描与分析设计](../../20-architecture/scanning-and-analysis.md#34-原生-windows-文件系统与-git-边界)定义。

### 4. Direct launcher 与 capability handle

Windows 后端不得用 `subprocess.Popen` 启动受控子进程。统一 launcher 直接调用 `CreateProcessW`，固定包含 `EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW`，并保留 process/thread handles。启动时序固定为：

```text
建立并回读安全边界
  -> CreateProcessW(CREATE_SUSPENDED)
  -> AssignProcessToJobObject
  -> 关闭父进程持有的 child-side handle 副本
  -> ResumeThread
  -> 关闭 primary thread handle
```

assign 或 resume 失败时必须终止 process/Job，子进程入口不得执行。`CREATE_NO_WINDOW` 不得因 headless `conhost.exe` 而移除；该系统进程可计入 Job accounting，但已验证不会占用成员派生名额，不能据此提高 `ACTIVE_PROCESS` 限额。

SessionCapability 与私有 payload 使用 Win32 匿名管道；`STARTUPINFOEXW.PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 必须把继承集合收窄到本次请求必需的 handles，列表内 handle 显式标为 inheritable，`CreateProcessW` 固定 `bInheritHandles=TRUE`，列表外全部 non-inheritable。命令行只传不具备秘密含义的数值 handle（例如 `--capability-handle <N>`），不得传原始 capability；子进程用 `msvcrt.open_osfhandle` 接管并在读取后关闭。

capability/payload、Git 和业务子进程共用同一个 launcher、所有权状态机与逆序清理模型，不共用同一个 Job 实例或 active-process 限额。每次启动创建独立 Job 并在 resume 前按对应 policy 配置：只有 Git 固定 `ACTIVE_PROCESS=1`；业务子进程按自身 policy 允许或限制后代，但所有获准后代必须留在同一 Job containment 中，并在 Job 关闭时整树回收。

stdout/stderr 使用 reader threads、有界 `queue.Queue` 和一个对 pipe、queue、accumulator 共同生效的累计字节预算。每次排队前原子预留预算，禁止 `communicate()` 或任一无界缓冲。

### 5. 句柄所有权与逆序清理

平台层必须用唯一 `OwnedHandle` 语义记录所有权：root/parent 输入是 borrowed，不得由被调用方关闭；成功创建或接管的 process、thread、Job、pipe、capability/payload、WFP session 和 attribute-list storage 均有一个明确 owner，移动所有权后原 owner 立即失效。不得靠垃圾回收决定安全句柄的关闭时机。

正常结束、超限、超时、协议错误、部分构造失败和关闭竞态使用同一依赖逆序清理：先停止执行并 `TerminateJobObject`/等待受控进程退出；再关闭不再使用的 pipe 端点并有限等待 reader threads；关闭 process/thread 与传输 handles，销毁 attribute list；关闭 Job；最后关闭 WFP dynamic session。每步失败仍继续清理其余 owned resources，同时保留首个错误。任何清理路径都不得先移除 WFP filters 再等待可执行进程退出。

## 与 ADR-0006、ADR-0009 的关系

本 ADR 接受后，ADR-0006 的授权确认、SessionCapability digest、跨 task 失效和最小持久化语义继续有效；仅其决策 3 的传输机制按平台分化：POSIX 继续使用专用 stdin/inherited FD，原生 Windows 使用本 ADR 的 allowlisted inherited HANDLE。ADR-0009 继续权威定义 macOS Seatbelt、Linux/WSL2 bwrap 和进程身份；其“原生 Windows 留待 Phase 3”范围声明届时由本 ADR 的 Windows 合同替代。三平台追求相同安全结果，但机制不强求同构：

| 边界 | macOS/Linux/WSL2 | 原生 Windows |
| --- | --- | --- |
| Git 网络 | Seatbelt/bwrap | WFP ALE dynamic filters |
| Git 文件读取 | 授权根 + 必要系统只读范围 | 唯一允许降级：无文件系统读隔离 |
| 扫描器 FS | descriptor-relative POSIX API | NT handle-relative API |
| 进程树 | 平台沙箱/进程组 | Job Object + suspended assign |
| capability | inherited FD | allowlisted inherited HANDLE |

## 影响

- Phase 3-B 必须实现 `sandbox_windows.py`、`fs_windows.py`、`process_windows.py`、`lock_windows.py`、`capability_windows.py` 和 `launcher_windows.py`，并改造现有平台委托点。
- 原生 Windows 当前仍显示 unsupported；README 与 Skill 运行前提示推荐 WSL2，不得因本 ADR 存在而提前进入支持矩阵。
- Windows 运行前提示在未来准入后仍必须明确唯一 Git FS 读隔离降级；若用户要求完整 Git FS 隔离，继续推荐 WSL2。
- 完整准入证据必须来自真实 Windows；mock/monkeypatch 只能补充 ctypes 结构、错误映射和控制流单测。

## 否决方案

- Windows Firewall profile/rule 或 Git 环境变量代替 WFP dynamic filters：scope、生命周期与可证明性不足。
- 启动 `cmd\git.exe` shim：它需要派生真实 Git，与 `ACTIVE_PROCESS=1` 冲突，也会使 WFP scope 指向错误二进制。
- `subprocess.Popen` 后补 Job：无法保留 primary thread handle 并在入口执行前完成 assign。
- pathname 预检后再操作：存在 check-then-use TOCTOU，不能保护并发换父目录。
- 提高 Git Job active-process 限额容纳 conhost/helper：conhost 不消耗成员派生名额，提高限额会放行 helper。
- 把 capability 原值放入 argv/env，或继承全部 handles：扩大秘密暴露与子进程能力边界。
- 清理时先关闭 WFP session：会在进程仍存活时产生网络窗口。

## 验证

原生 Windows 准入以 [IMP-31](../../40-delivery/acceptance-baseline.md#33-后续原生-windows-准入门) 为唯一聚合验收项。它必须覆盖 WFP 权限/BFE 故障、IPv4/IPv6 TCP/UDP/DNS、真实 Git 阳性/负向、NTFS/reparse/别名/ADS/UNC/跨卷/超长组件、open/rename/delete 并发换父目录、关闭竞态、bounded-output 总预算、capability 隔离、handle 泄漏和异常逆序清理；每个安全边界同时有真机阳性与负向证据，每个 FS 拒绝或并发对抗用例都必须断言根外哨兵未读、未写、未删。

# ADR-0012：Windows NT 改名与目录枚举修正

> 状态：已接受（2026-08-16，Architect 基于 SWO-31 真机取证终审接受）
> 日期：2026-08-16
> 权威范围：原生 Windows handle-relative rename/publish primitive 与 retained directory handle 的独立枚举 cursor 契约
> 上游：[ADR-0011](ADR-0011-native-windows-security-contract.md) §3、SWO-31 真机取证
> 下游：[扫描与分析设计](../../20-architecture/scanning-and-analysis.md)、[验收基线](../../40-delivery/acceptance-baseline.md)（IMP-31C）
> 关系：本 ADR 的 rename/publish 决策替代 ADR-0011 §3 中的 Win32 primitive；目录枚举 cursor/reset 决策不替代 ADR-0011，而是在其 handle-relative 枚举安全边界上新增操作细化。ADR-0011 的授权根、逐组件相对打开、reparse/identity/同卷、pathname 禁降级、WFP、Job、launcher、capability、所有权与逆序清理决策继续有效。

## 背景

ADR-0011 接受 `SetFileInformationByHandle(FileRenameInfoEx)` 作为候选 rename primitive 时，早期 NTFS spike 只验证了 `NtCreateFile` 相对打开、reparse 拒绝与 identity 回读，没有执行 rename 或 publish。后续产品候选在 Windows 真机使用已验证的 source handle、live target-parent handle 与相对名称时，Win32 class 3 `FileRenameInfo` 和 class 22 `FileRenameInfoEx` 均返回 `ERROR_INVALID_PARAMETER (87)`。

同一台主机、同一组 source/parent handles、访问权和相对名称下，`NtSetInformationFile(FileRenameInformation=10)` 能成功完成文件替换与目录发布。这一阳性反证了 ADR-0011 中具体 Win32 primitive 的可用性，但没有否定其 handle-relative 授权、安全边界或其他 Windows 运行时决策。

同轮取证还发现：`GetFileInformationByHandleEx(FileIdBothDirectoryInfo=10)` 会推进 retained directory handle 的 cursor。`_walk_directories()` 先枚举 active root 后，后续 `_non_git_manifest()` 在同一 root handle 上得到空集，使真实非 Git `pyproject.toml` 工作区可能形成零项目 `failed` scan。关闭 active root 或按 pathname 重新打开都不符合既有授权与所有权边界，因此必须在每次独立枚举内显式重置 cursor。

取证宿主为 Windows build `26200.8875`。它只形成候选 primitive 和缺陷根因的定点证据，不是 GA Windows 支持或发布准入证据。

## 决策

### 1. Rename/publish 统一使用 NT class 10

`replace_file` 与 `publish_directory` 统一调用 `NtSetInformationFile(FileRenameInformation=10)`。调用只携带已验证的 source handle、已验证的 target parent handle 与一个相对名称组件，不获取新 handle，也不重新解析 pathname。

`FILE_RENAME_INFORMATION` 使用 Windows 固定 16-bit `WCHAR` ABI 和零初始化 buffer。`RootDirectory` 是 target parent handle；`ReplaceIfExists` 按调用语义取 `TRUE` 或 `FALSE`；`FileNameLength` 只计算 UTF-16LE 名称字节且不包含终止 NUL；buffer 分配与上报长度满足结构大小加名称字节的下限。失败统一通过既有 NTSTATUS 到 DOS error 映射向上报告。

禁止退回 Win32 class 3/22、`RootDirectory=NULL` 加绝对路径、`os.replace`、`MoveFileExW`、pathname fallback 或 check-then-use。既有同卷、reparse、大小写别名、rename 后 identity 复核与 owner cleanup 契约保持不变。

### 2. 每次独立目录枚举自行重置 cursor

每次 `list_directory` 调用的首个查询使用 `FileIdBothDirectoryRestartInfo=11`，后续分页使用 `FileIdBothDirectoryInfo=10`，直到 `ERROR_NO_MORE_FILES`。首次查询即返回该错误时结果为空；其他错误原样 fail-closed。

一次调用消费的 cursor 不得影响下一次调用。同一 retained root 或 directory handle 连续两次独立枚举必须得到相同完整结果；不得为重置 cursor 而关闭 active root、重新按 pathname 打开目录或改变 handle 所有权。

### 3. 替代与补充边界

本 ADR 第 1 节仅替代 ADR-0011 §3 的 Win32 rename/publish primitive；第 2 节是在其既有 handle-relative 枚举边界上补充 cursor/reset 操作契约，不表示 ADR-0011 曾定义该规则。本 ADR 不修改 WFP/Job、direct launcher、capability HANDLE、资源所有权、逆序清理、唯一 Git FS 降级或支持状态。原生 Windows 在 [IMP-31](../../40-delivery/acceptance-baseline.md#33-后续原生-windows-准入门) 全部通过前继续 unsupported / fail-closed，并推荐 WSL2。

## 影响与验证

- [扫描与分析设计](../../20-architecture/scanning-and-analysis.md#34-原生-windows-文件系统与-git-边界) 唯一定义运行时操作语义；交付计划只引用，不再复制 class/ABI 契约。
- ABI、错误映射和分页顺序可用确定性单元测试补充，但不能替代 Windows 真机证据。
- `IMP-31C` 必须在同一候选上验证连续独立枚举、多页 `11 -> 10...` 及非 Git manifest 工作区的 `_walk_directories -> _non_git_manifest -> _discover/scan` 完整链路。
- build `26200.8875` 上的诊断不能冒充 GA 双栈、可交互提升权限和完整 `IMP-31A-G` 的发布证据。

## 否决方案

- 保留 Win32 class 3/22：live-parent 真机对照已稳定返回 error 87。
- 使用绝对路径阳性对照：会丢失 target parent handle 授权，违反禁止 pathname 重解析的安全边界。
- 每页都使用 restart class：会重复首页，不能形成正确分页。
- 复用 class 10 cursor 而不重置：连续消费者会静默漏项。
- 关闭或重开 root 来重置：破坏 active-root 所有权与单次绝对路径授权边界。

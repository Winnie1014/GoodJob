# ADR-0006：Codex 源码分析授权与根外 Git 元数据绑定

> 状态：已接受  
> 日期：2026-07-24  
> 权威范围：项目衍生信息进入 Codex 分析前的授权，以及 `.git` 指向授权根外时的最小读取边界  
> 上游：[产品需求](../../10-product/product-requirements.md)  
> 下游：[系统设计](../../20-architecture/system-design.md)、[证据模型](../../20-architecture/evidence-model.md)、[扫描设计](../../20-architecture/scanning-and-analysis.md)

## 背景

“本机路径可读”只说明技术权限，不等于 Owner 已确认可以在当前 Codex 会话中处理其中的源码，也不证明组织策略、NDA 或版权允许使用。linked worktree 的 `.git` 文件还可能指向授权根外；若把该指针当作隐式授权，扫描器会在用户看不见的情况下扩张读取范围。

## 决策

1. 每个显式 Skill 会话在任何项目源码、源码衍生证据、既有项目材料或模型驱动导出进入 Codex 处理前，必须显示规范化工作区、处理类别、本地持久化边界，并明确说明 Codex 打开的原文件会进入当前会话所使用的模型处理链路、该链路受当前产品/账户/工作区策略约束，而 GoodJob 不新增该会话之外的上传/遥测/第三方通道。
2. 当前 Codex task 首次进入授权流程时，`ARCH-C01` 的编排运行时使用密码学安全随机源生成至少 256 bit `SessionCapability`，原始值只保存在 task-scoped 易失状态。Owner 确认后，`AuthorizationReceipt(source_analysis)` 只保存 `SHA256("goodjob-session-binding-v1" + capability)`、规范化 scope、notice version 和确认时间。
3. 每个受保护请求都通过专用 stdin/继承文件描述符临时携带原始 capability；本地核心重算 digest 并 constant-time compare。原始值不得进入 SQLite、配置、argv、环境变量、stdout/stderr、GoodJob 日志、manifest、产物或用户可见输出；Codex host 对当前 task trace 的处理仍服从其既有边界。task 结束、状态丢失或运行时不支持易失能力时必须重新确认，绝不能凭 receipt ID 或数据库恢复。
4. 缺失、拒绝、capability/digest 不匹配或 notice version 变化时，不得创建新的 ScanRun、PreparationRun、EvidenceBundle 或模型驱动导出；既有离线 HTML 仍可直接打开。GoodJob 不替 Owner 判断 NDA、版权或组织政策。
5. `.git` 中的根外路径先按不可信文本解析为词法候选，解析本身不授权打开候选。Owner 可对该精确候选授予 `AuthorizationReceipt(external_git_relation_probe)`；它继承同一 SessionCapability binding，只允许读取关系文件以解析规范化 git-dir/common-dir 和初步回指。
6. 解析后界面展示规范化 git-dir/common-dir、拟读取字段和风险；Owner 再授予同一 task、这两个精确目标的 `AuthorizationReceipt(external_git_metadata)`。扫描器随后使用固定、非交互、无网络的 Git 调用，清除继承的 `GIT_*` 环境变量，并再次验证双向关系。任一失败都拒绝后续根外读取。
7. 根外 Git 只允许读取关系元数据、HEAD/ref、index/dirty 状态；不读取根外历史、object、blob、diff、其他 worktree 内容、Git 配置或源码，也不执行 fetch/checkout/hooks。
8. 工作区、Git 配置、项目文档、JD、历史回执、SQLite 内容或模型输出都不能生成、恢复或扩大授权。该 capability 防止 GoodJob 跨 task 误复用，不构成对控制本机和数据库的 Owner 的安全沙箱。

## 影响

- 授权动作与文件可读性分离，可在证据库中审计而不保存法律判断或源码。
- linked worktree 仍可被正确归并，但根外读取范围从“Git 仓库”收缩为精确关系元数据。
- 每个新显式会话需要一次轻量确认；根外 Git 还需要按候选做关系探测确认，并在解析精确路径后做元数据确认。
- 实现依赖 Codex host 提供 task-scoped 易失编排状态与安全随机源；如果不可用，功能必须 fail closed，而不是把回执降级为 SQLite bearer token。
- GoodJob 无法证明当前 Codex 产品配置是否满足某组织政策，只能清楚揭示这条边界。

## 否决方案

- 把用户传入路径视为全部处理授权：无法区分 POSIX 可读权限与数据处理意图。
- 每条 Claim 都单独确认：交互成本过高，且不能改善范围理解。
- 只把 session nonce/receipt ID 写入 SQLite：任何新 task 都能重放，无法证明同一会话。
- 把 capability 放进 argv、环境变量或日志：容易被进程列表、诊断或产物泄露。
- `.git` 指针自动授权整个 common-dir：会把根内路径升级为根外历史和对象访问。
- 完全拒绝 linked worktree：更简单，但会破坏真实工作区发现与去重目标。

## 验证

- 没有当前 capability 时，scan/prepare/模型导出均为零业务写入、零项目内容读取。
- 复用旧 task 回执、只复制 SQLite、传错 capability、改工作区或改 notice version 均被拒绝；同一 task 的正确 capability 可跨短生命周期 Python 子进程使用。
- SQLite、argv、环境变量、stderr/stdout、manifest 和产物扫描均找不到原始 capability。
- 根外 `.git` 未授权、任一阶段拒绝、单向伪造、环境变量注入、网络/交互配置均不能越过相应读取阶段；probe 阶段无 Git 子进程或业务元数据读取。
- 授权且双向绑定的真实 linked worktree 只产生关系、HEAD/ref、index/dirty 元数据；命令审计中不存在根外 log/show/diff/cat-file/config/fetch/checkout。

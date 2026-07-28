# ADR-0002：Python 数据层与离线 TypeScript 看板

> 状态：已接受  
> 日期：2026-07-24  
> 权威范围：本地扫描器、SQLite 编排和 HTML 看板的实现栈及运行形态  
> 上游：[产品需求](../../10-product/product-requirements.md)  
> 下游：[系统设计](../../20-architecture/system-design.md)、[产物设计](../../20-architecture/artifacts-and-learning.md)、[看板呈现契约](../../20-architecture/dashboard-design.md)  
> 部分收窄：决策 3 与决策 5 中“可放在同一产物目录”“首版不强制单 HTML 文件”的选项已由 [ADR-0008](ADR-0008-single-file-dashboard-and-structured-token-embedding.md) 收窄为强制单文件全内联；其余决策不变

## 背景

扫描器需要遍历多语言项目、调用 Git、管理 SQLite 和生成可复现产物；看板需要良好的信息层级、筛选和证据展开。用户同时要求离线打开，不引入常驻服务。

## 决策

1. 使用 uv 管理的 Python 实现扫描、增量索引、SQLite、Markdown 和产物编排。
2. 使用 TypeScript 前端实现离线静态看板。
3. 前端构建结果随 Skill 分发；Python 将本次报告数据嵌入入口 HTML。脚本、样式和图标的打包形态由 [ADR-0008](ADR-0008-single-file-dashboard-and-structured-token-embedding.md) 收窄为全部内联。
4. 看板不得依赖 HTTP API、后台守护进程、远端 CDN 或运行时包管理器。
5. 入口 HTML 双击即可离线打开；所有样式、图标和脚本必须来自本地产物，字体只使用系统字体栈。看板入口为单个 HTML 文件（见 [ADR-0008](ADR-0008-single-file-dashboard-and-structured-token-embedding.md)）；同一 `ArtifactSnapshot` 内的 Markdown、manifest 与派生导出仍需整体移动其不可变目录。

## 影响

- 扫描和持久化逻辑集中在适合本地工具的 Python 模块。
- 前端可独立做视觉 QA，同时保持运行部署为静态文件。
- 前端源码增加一个构建门禁，但用户运行时不需要 Node 服务。
- 看板只读意味着复习状态更新需回到 Skill/Python，并通过新的不可变 PreparationRun/ArtifactSnapshot 呈现，不能原地修改旧 HTML。

## 否决方案

- Python 用字符串拼接直接产出 HTML（不经 TypeScript 前端）：初期更轻，但复杂筛选和状态展示难以维护，且不可信数据的安全编码会散落在拼接点上。这与 ADR-0008 的“单文件产物形态”无关——那里仍由构建后的 TypeScript 前端渲染。
- React/Vite 常驻本地服务：违背离线静态看板决策。
- TypeScript 单栈：会把 Git、SQLite 和多语言扫描的本地工具链绑到 Node 运行时。

## 验证

- 断网且无本地服务时，HTML 仍能打开并完成搜索、筛选和证据展开。
- 前端构建产物可由固定命令重建，且不包含远端资源引用。
- Python 与前端通过版本化报告契约交互，不直接共享内部数据库结构。

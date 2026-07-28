# 离线看板原型（fixture 验收夹具）

> 状态：原型，不是实现证据。按 [docs/index.md](../../docs/index.md) 单一事实源规则第 7 条，原型和绿灯都不代表功能已完成。
> 权威契约在 [看板呈现契约](../../docs/20-architecture/dashboard-design.md) 与 [ADR-0008](../../docs/30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md)。

这个原型有两个用途：

1. 让 Owner 在写实现代码之前先看到并否决具体的呈现决策；
2. 充当 `DASH-01` 至 `DASH-12`（验收基线 `IMP-28`）的可复现夹具——fixture 里已经埋好注入语料、`partial` 降级、混合时效证据、跨版本深链和复习三态。

与首版实现的差异要说清楚：这里的前端是纯 JavaScript，而 `ADR-0002` 要求实现用 TypeScript；`ReportBundle` 是手写 fixture，不是 Python 生成的。呈现决策、安全边界和构建门禁形态可以照搬，代码不能。

## 运行

```bash
python3 prototypes/dashboard/build.py
```

产出 `out/dashboard.html`（约 59 KiB）。双击打开即可，不需要任何服务、依赖或网络。把这个文件单独复制到任何目录都能正常工作——单文件是 `ADR-0008` 决策 1 的直接后果。

## 呈现基调

按[呈现契约](../../docs/20-architecture/dashboard-design.md) §1.1 做成「取证卷宗」而不是仪表盘：没有卡片网格，层级由留白、细分割线和悬挂式版式建立；报头下方的覆盖条是首屏论点（先讲边界，再讲叙事）；出处悬挂在版心左栏；证据的墨色浓度与悬挂线型是时效的第二条通道，始终配图标与文字。

字体不加载任何文件，性格来自三个角色的分工：显示层用系统衬线（中文落到宋体，对应公文与书籍的字面），正文用系统无衬线，数据层等宽——路径和哈希必须能逐字符核对，等宽在这里是承重的，不是装饰。

## 目录

| 路径 | 作用 |
| --- | --- |
| `fixture/report-bundle.json` | `ReportBundle v1` 样例，富文本全部是 `ReportInlineToken` |
| `src/index.template.html` | 入口骨架，含 CSP 与内联占位符 |
| `src/app.css` | token 系统：冷调纸面 + 三级墨色、三个字体角色、固定字号级差、浅/深两套、打印与 `forced-colors` 分支 |
| `src/app.js` | 渲染器：token 映射、状态编码、覆盖条、出处悬挂栏、hash 路由、筛选、键盘 |
| `build.py` | canonical JSON → `bundle_sha256` → 嵌入转义 → 内联 → CSP 哈希 → 单文件，并执行构建门禁 |
| `verify.mjs` | WebKit + Chromium 跨引擎核对，并抓 1:1 截图到 `out/shots/` |

## build.py 里已经生效的构建门禁

- **禁用 API 静态检查**：以带标签的正则覆盖 `ADR-0008` 决策 6 的直接调用、常见空白和引号变体，命中即失败；每次构建先运行阳性/阴性探针，防止规则自身静默退化。
- **远端引用检查**：`src=`/`href=` 指向 `//`、`@import`、`url(//`、`<link>`、`<img>` 一律失败。
- **`</script>` 转义检查**：嵌入数据里不允许出现未转义的结束标记。
- **style 属性检查**：产物中不允许 ` style="`。原因见下。
- **CSP 哈希一致性**：哈希按真正内联进产物的内容重算，而不是按源文件。
- **字节级可复现**：同一 fixture 连续渲染两次必须完全一致。

## 一条实测出来的约束

`style-src 'sha256-…'` 只覆盖内联 `<style>` 元素，不覆盖 HTML 的 `style` 属性。在实际产物里验证过：

- `element.setAttribute("style", "width:10px")` → 被 CSP 拦下，样式不生效；
- `element.style.width = "33px"` → 生效，CSSOM 不受 `style-src` 约束。

所以进度条这类动态尺寸必须走 CSSOM 属性 setter。这条已经写进 `build.py` 的禁用清单，避免实现阶段重新踩一次。

## DASH 逐条走法

| ID | 怎么做 | 期望 |
| --- | --- | --- |
| `DASH-01` | 断网，双击 `out/dashboard.html`，开 DevTools 网络面板并全量浏览 | 零请求 |
| `DASH-02` | 同上，看 Console | 零 CSP 违规、零脚本错误 |
| `DASH-03` | 看左侧项目 `notes-vault</script><img src=x onerror=alert(1)>`，以及 `failed_no_baseline` 项目里的 `https://example.invalid/...` | 全为文本；`document.querySelectorAll('img').length === 0`；除跳转锚点 `#main` 外无 `<a>` |
| `DASH-04` | 视口依次调到 1440、1024、768、375，逐个视图检查 | `scrollWidth === clientWidth`；≥1024 两栏 + 出处悬挂栏；<768 导航收成顶部 select、悬挂栏归零 |
| `DASH-05` | 打开首屏 | `partial` 徽章；覆盖条读作「5 个项目里有 3 个参与评分」（`carried_forward` 计入）；四条降级注解各带影响与补救，可点进筛选 |
| `DASH-06` | 进 coderoute，看 `c_shard_route` | 两次交互内看到 locator/hash/commit_state/时效/facets；stale 行墨色变浅、悬挂线转点线并标“只作历史限制”；等价来源可展开 3 个 worktree |
| `DASH-07` | 打印预览 | 折叠展开、locator 完整、导航与按钮隐藏、保留身份条与降级带 |
| `DASH-08` | 只用键盘：`/` → 输入 → Tab 到筛选 → `j`/`k` → `e` | 全流程可达，焦点环可见 |
| `DASH-09` | 开 `forced-colors: active`、灰度打印、色觉模拟 | 五组状态靠图标 + 文字仍可区分 |
| `DASH-10` | 同时开两份快照；地址栏改成 `#/v2/overview` | 身份条可区分；跨版本深链明确报错 |
| `DASH-11` | 进「面试与复习」 | `continued`/`reassess_required`/`new` 三态可辨；只有可复制的 Skill 调用，无写状态控件 |
| `DASH-12` | 与同一 bundle 生成的 Markdown 逐条比对 | 证据状态、facet、`commit_state`、限制一致（本原型未生成 Markdown，该条待实现阶段补） |

## 跨引擎自动核对

```bash
cd prototypes/dashboard && npm init -y && npm i playwright && npx playwright install webkit chromium && node verify.mjs
```

`verify.mjs` 在 WebKit 与 Chromium 各跑一遍 `DASH-01`～`DASH-04`、`DASH-09` 的可机检部分，并把截图写到 `out/shots/`（`out/` 已在 `.gitignore` 中）。WebKit 不是可选项——Owner 双击很可能用 Safari 打开，而两个引擎的 CSP 实现不同。

两处判读要点：

- **`cleanLoadErrors` 必须为空**，这是断言。`probeInducedErrors` 非零是预期的：脚本会故意注入 `style` 属性做阳性对照，Playwright 每次截图也会注入一个 `<style>`，两者都被这份 CSP 正确拒绝。任何往页面里注入样式的工具都会撞上它，这是策略够紧的证据，不是缺陷。
- **`cspBehaviour.violationReported` 必须为真**。如果注入 `style` 属性没有引发违规，说明 CSP 根本没生效，其余「通过」都不可信。

已实测通过的结论（WebKit 26.5 / Chromium 151）：两引擎干净加载零报错、零外部请求；7 个宽度 × 7 个视图共 49 种组合无横向溢出；`style` 属性被拦、CSSOM setter 生效；打印时导航与页脚隐藏、取证条与覆盖条保留、折叠全展开。

## 已知未覆盖

- 没有配套的 Markdown 渲染器，`DASH-12` 只能到实现阶段验证。
- `search_index` 是手写的少量词条，不是真实倒排索引；检索只演示「子串匹配、不编译用户输入为正则」这条规则。
- 未做 `missing` 时效的样例证据（fixture 只有 current/stale/plan）。

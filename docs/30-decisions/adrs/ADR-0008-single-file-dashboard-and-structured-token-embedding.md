# ADR-0008：单文件离线看板与结构化 token 嵌入

> 状态：已接受  
> 日期：2026-07-25  
> 权威范围：离线看板入口产物的打包形态、CSP 施加方式、报告数据嵌入与转义方式，以及富文本进入呈现层的数据形态  
> 上游：[ADR-0002](ADR-0002-python-and-offline-typescript-dashboard.md)、[产品需求](../../10-product/product-requirements.md)  
> 下游：[看板呈现契约](../../20-architecture/dashboard-design.md)、[产物与学习闭环](../../20-architecture/artifacts-and-learning.md)、[证据模型](../../20-architecture/evidence-model.md)、[验收基线](../../40-delivery/acceptance-baseline.md)

本 ADR 收窄 [ADR-0002](ADR-0002-python-and-offline-typescript-dashboard.md) 决策 3 与决策 5 中“首版不强制单 HTML 文件、静态资源可放在同一产物目录”的选项；ADR-0002 的其余决策不变。

## 背景

ADR-0002 允许看板的脚本、样式、字体和图标既可内联，也可作为同目录本地静态资源。设计看板呈现契约时出现两个必须现在定的问题。

第一个是 CSP 在 `file://` 下不可靠。看板双击打开，没有 HTTP 响应头，CSP 只能靠 `<meta http-equiv>` 施加。而各浏览器把 `file:` 文档的 origin 视为 opaque，`script-src 'self'` 对同目录脚本的行为不一致：可能把合法脚本挡掉，也可能让策略形同虚设。`NFR-03` 和 `NFR-08` 要求产物必须配置能真正阻断网络、对象、frame、表单和任意脚本执行的 CSP，而“按哈希允许”是唯一在 `file://` 下行为确定的方式；按哈希允许要求脚本与样式内联在入口文档中。

第二个是富文本形态决定了 `NFR-08` 的性质。如果 `ReportBundle` 向前端传 Markdown 字符串，前端就必须内置解析器，并且要在解析结果上正确挡住原始 HTML、事件属性和 `javascript:` URL。项目名、路径、JD、用户回答和模型输出都是不可信数据，这类净化只要有一处遗漏就是可执行标记。这是把安全押在净化函数的完备性上。

## 决策

1. 首版离线看板产物为单个入口 HTML 文件：脚本、样式和图标全部内联，字体只使用系统字体栈，不加载任何字体文件，也不引用任何同目录静态资源。
2. 入口文档必须携带 `<meta http-equiv="Content-Security-Policy">`，按内联内容的 sha256 允许 `script-src` 与 `style-src`，其余取值收紧为 `default-src 'none'`、`img-src 'none'`、`font-src 'none'`、`connect-src 'none'`、`object-src 'none'`、`frame-src 'none'`、`form-action 'none'`、`base-uri 'none'`；不使用 `unsafe-inline`、`unsafe-eval` 或任何远端来源。
3. 图标使用内联 SVG 元素，不使用图标字体或 `<img>`。
4. `ReportBundle` 不向呈现层传递 Markdown 或 HTML 字符串。所有富文本以结构化 inline token 序列传递，token 类型是封闭集合，前端把它映射为文本节点与已知安全元素。看板不含 Markdown 或 HTML 解析器。
5. 报告数据以 canonical JSON 内联嵌入入口文档；序列化时必须把 `<`、`>`、`&`、`=`、U+2028、U+2029 转为 `\uXXXX` 转义序列。`bundle_sha256` 始终计算在 canonical JSON 上，嵌入层转义不参与哈希。转义 `=` 与转义 `<`、`>` 出于同一条理由：数据区不得产生任何形似标记或形似属性的字节序列，否则决策 8 的产物检查会把用户内容误判成产物结构。
6. 呈现层禁用 `eval`、`new Function`、`setTimeout`/`setInterval` 的字符串形式、`innerHTML`、`outerHTML`、`insertAdjacentHTML`、`document.write` 和 `srcdoc`，也不通过属性字符串注册事件处理器。样式方面禁用**全部**运行时写入：`setAttribute("style", …)`、`cssText`，以及 `element.style` 的任何属性赋值。理由有两层：`style-src` 的哈希白名单只覆盖内联 `<style>` 元素、不覆盖 `style` 属性，因此 style 属性会被本策略静默拦下（已在 Chromium 与 WebKit 实测确认）；而 CSSOM 虽不受 CSP 约束，允许它就要求门禁逐处区分「CSP 放行的 CSSOM」与「CSP 拦截的属性字符串」，一条不作区分的禁令既更容易机检，也顺带消掉呈现层的浮点百分比。动态几何一律用内联 SVG 的几何属性（`x`/`width`/`height`）配合整数 `viewBox` 表达，比例保持整数原值，与 `DASH-INV-05` 一致。构建门禁必须能静态检出以上全部调用，并检查产物结构中不含 `style` 属性。
7. 单文件只约束看板入口产物。`ArtifactSnapshot` 中的 Markdown、manifest 和英文派生导出仍是各自独立文件，ADR-0002 的产物目录结构不变。
8. 产物级与提交级的安全门禁必须在结构层判定，不得把结构化文档扁平化成一个字符串后用模式匹配代替。具体到本 ADR 的两处：入口文档的属性检查只作用于产物结构，`<script type="application/json">` 的数据区不参与该检查；对 `ReportInlineToken` 序列的散文级校验只作用于 `text` 与 `emphasis` 的 `value`，`code`、`inert_url` 与 `*_ref` 的 `value` 是数据，不参与散文判定。任何门禁都必须在任意真实用户内容下成立——只在固定夹具下成立的门禁不是门禁。

## 影响

- 一个文件就是一份完整可读快照，ADR-0002 决策 5 中“移动产物时必须整体移动其不可变目录”的约束对看板入口不再成立，快照更符合不可变产物的直觉。
- CSP 由“依赖浏览器对 `file:` origin 的解释”变成确定的哈希白名单，`NFR-03`/`NFR-08` 因此可被 `DASH-02` 机检。
- `NFR-08` 在呈现层的性质从“必须正确净化”变成“结构上不可能”：没有解析器，就没有可被绕过的净化路径。
- 构建阶段多两步确定性工作：内联后计算 sha256 并回填 CSP，以及在嵌入前执行转义。两步都是纯函数，不影响 `bundle_sha256` 复算。
- 每份快照的入口文件包含一份前端代码副本。首版报告规模下这个体积可接受；若未来单文件体积成为问题，需要新 ADR 重新权衡，而不是悄悄拆回多文件。
- Python 侧必须为富文本生成结构化 token，模型输出的富文本需在 `record_analysis` 阶段就落到封闭 token 集合上，不能把 Markdown 原文顺延到渲染阶段。
- 决策 8 把门禁的失败模式限定在“漏判”而非“误判”。渲染发生在冻结 `ReportBundle` 之后，此时任何确定性的渲染拒绝都不可恢复：同一 bundle 重试必然同样失败，Owner 只能重跑整轮分析。因此宁可让门禁判定范围更窄且可解释，也不接受一条会被工作区内容触发的宽泛匹配。

## 否决方案

- **同目录外置脚本 + `script-src 'self'`**：在 `file://` 下行为不确定，等于把安全边界交给浏览器实现差异。
- **同目录外置脚本 + 按哈希允许**：多数浏览器不对外部脚本按哈希放行（需要 `integrity` 配合，而 `file://` 下同样不可靠），且仍要整体移动目录。
- **完全不设 CSP，只靠代码纪律**：`NFR-08` 明确要求产物配置 CSP；纪律不可机检。
- **传 Markdown 字符串 + 前端净化器**：把安全押在净化完备性上，且需要引入并长期维护一个解析器依赖。
- **传预渲染的 HTML 片段**：同样需要净化，还会让 Markdown 与 HTML 的同源约束更难验证。
- **用 `<script type="application/json">` 但不做 `\uXXXX` 转义，只靠 `</script>` 字符串替换**：漏掉 `<!--`、`<script`、U+2028 等情况；转义为 `\uXXXX` 是覆盖完整且不改变 JSON 语义的做法。
- **允许 CSSOM 属性 setter 作为动态尺寸的出口**：本 ADR 早期版本采用该做法。它在 CSP 下确实可用，但把「哪种样式写法被策略拦下」变成需要逐处判断的知识，且重新引入浮点百分比。改为全面禁用 `.style` 并用 SVG 几何属性表达，规则更短、可静态机检，且比例算术保持整数。
- **在整份产物文本上匹配 ` style=` 作为属性检查**：写法最短，但产物文本包含内联的报告数据，用户工作区里任意一段 `<div style="…">` 都会让这条门禁在渲染阶段确定性地失败，且因为 bundle 已冻结而不可恢复。见决策 8。

## 验证

- `DASH-01`：断网双击打开，网络面板零请求。
- `DASH-02`：在 Chromium 与 WebKit 各完整浏览全部视图后，控制台零 CSP 违规、零脚本错误；同一核对必须以「注入 `style` 属性应当触发违规」作为阳性对照，阳性对照不成立说明 CSP 未生效，其余结论不可信。
- `DASH-03`：注入语料（`</script>`、事件属性、`javascript:` URL、U+2028/U+2029、RTL 覆盖字符）全部以可读文本呈现。
- 构建门禁静态检出决策 6 列出的全部禁用 API，且校验 CSP meta 中的哈希与实际内联内容一致。
- 决策 8 的两处门禁各有一条反向用例：一条含 `style=` 的 `code` token 必须能正常渲染；一条含 `code` token 的非个人化 Claim 必须能通过归因校验。
- 同一 `ReportBundle` 重复渲染产出逐字节相同的入口文件，且 `bundle_sha256` 与 manifest 一致。

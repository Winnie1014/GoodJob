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
5. 报告数据以 canonical JSON 内联嵌入入口文档；序列化时必须把 `<`、`>`、`&`、U+2028、U+2029 转为 `\uXXXX` 转义序列。`bundle_sha256` 始终计算在 canonical JSON 上，嵌入层转义不参与哈希。
6. 呈现层禁用 `eval`、`new Function`、`setTimeout`/`setInterval` 的字符串形式、`innerHTML`、`outerHTML`、`insertAdjacentHTML`、`document.write` 和 `srcdoc`，也不通过属性字符串注册事件处理器。同时禁用 `setAttribute("style", …)` 与 `cssText`：`style-src` 的哈希白名单只覆盖内联 `<style>` 元素，不覆盖 `style` 属性，因此 style 属性会被本策略静默拦下（在原型产物中已实测确认）。需要动态尺寸时使用 CSSOM 属性 setter，它不在 CSP 的约束范围内。构建门禁必须能静态检出以上全部调用，并检查产物中不含 `style` 属性。
7. 单文件只约束看板入口产物。`ArtifactSnapshot` 中的 Markdown、manifest 和英文派生导出仍是各自独立文件，ADR-0002 的产物目录结构不变。

## 影响

- 一个文件就是一份完整可读快照，ADR-0002 决策 5 中“移动产物时必须整体移动其不可变目录”的约束对看板入口不再成立，快照更符合不可变产物的直觉。
- CSP 由“依赖浏览器对 `file:` origin 的解释”变成确定的哈希白名单，`NFR-03`/`NFR-08` 因此可被 `DASH-02` 机检。
- `NFR-08` 在呈现层的性质从“必须正确净化”变成“结构上不可能”：没有解析器，就没有可被绕过的净化路径。
- 构建阶段多两步确定性工作：内联后计算 sha256 并回填 CSP，以及在嵌入前执行转义。两步都是纯函数，不影响 `bundle_sha256` 复算。
- 每份快照的入口文件包含一份前端代码副本。首版报告规模下这个体积可接受；若未来单文件体积成为问题，需要新 ADR 重新权衡，而不是悄悄拆回多文件。
- Python 侧必须为富文本生成结构化 token，模型输出的富文本需在 `record_analysis` 阶段就落到封闭 token 集合上，不能把 Markdown 原文顺延到渲染阶段。

## 否决方案

- **同目录外置脚本 + `script-src 'self'`**：在 `file://` 下行为不确定，等于把安全边界交给浏览器实现差异。
- **同目录外置脚本 + 按哈希允许**：多数浏览器不对外部脚本按哈希放行（需要 `integrity` 配合，而 `file://` 下同样不可靠），且仍要整体移动目录。
- **完全不设 CSP，只靠代码纪律**：`NFR-08` 明确要求产物配置 CSP；纪律不可机检。
- **传 Markdown 字符串 + 前端净化器**：把安全押在净化完备性上，且需要引入并长期维护一个解析器依赖。
- **传预渲染的 HTML 片段**：同样需要净化，还会让 Markdown 与 HTML 的同源约束更难验证。
- **用 `<script type="application/json">` 但不做 `\uXXXX` 转义，只靠 `</script>` 字符串替换**：漏掉 `<!--`、`<script`、U+2028 等情况；转义为 `\uXXXX` 是覆盖完整且不改变 JSON 语义的做法。

## 验证

- `DASH-01`：断网双击打开，网络面板零请求。
- `DASH-02`：完整浏览全部视图后控制台零 CSP 违规、零脚本错误。
- `DASH-03`：注入语料（`</script>`、事件属性、`javascript:` URL、U+2028/U+2029、RTL 覆盖字符）全部以可读文本呈现。
- 构建门禁静态检出决策 6 列出的全部禁用 API，且校验 CSP meta 中的哈希与实际内联内容一致。
- 同一 `ReportBundle` 重复渲染产出逐字节相同的入口文件，且 `bundle_sha256` 与 manifest 一致。

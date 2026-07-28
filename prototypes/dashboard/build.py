#!/usr/bin/env python3
"""把 fixture ReportBundle 渲染成单文件离线看板。

这是 ADR-0008 的可执行示例：canonical JSON -> bundle_sha256 -> 嵌入转义 ->
内联 CSS/JS -> 按 sha256 生成 CSP -> 单个 HTML 文件。

它同时充当构建门禁的雏形：禁用 API 静态检查、远端引用检查、
CSP 哈希一致性检查和字节级可复现检查都在这里执行。

用法：
    python3 prototypes/dashboard/build.py
    python3 prototypes/dashboard/build.py --out /tmp/dashboard.html
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture" / "report-bundle.json"
SRC = ROOT / "src"
DEFAULT_OUT = ROOT / "out" / "dashboard.html"

# ADR-0008 决策 6：呈现层禁用 API。
# setAttribute("style") 与 cssText 会被 style-src 哈希策略拦下（它们走
# style 属性通道，不在内联 <style> 的哈希白名单内），因此一并禁用；
# 需要动态尺寸时使用 CSSOM 属性 setter。
FORBIDDEN_JS = (
    "eval(",
    "new Function",
    ".innerHTML",
    ".outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "srcdoc",
    "javascript:",
    'setAttribute("style"',
    "setAttribute('style'",
    ".cssText",
)

# 远端资源引用（ADR-0002 决策 4、NFR-03）。
REMOTE_PATTERNS = (
    re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.I),
    re.compile(r"@import", re.I),
    re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.I),
    re.compile(r"<link\b", re.I),
    re.compile(r"<img\b", re.I),
)

CSP = (
    "default-src 'none'; "
    "script-src '{script_hash}'; "
    "style-src '{style_hash}'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "media-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'"
)


def canonical_json(payload: object) -> str:
    """canonical 序列化：排序键、无多余空白、保留非 ASCII 原文。

    bundle_sha256 只算在这个字符串上；嵌入层转义不参与哈希。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def embed_escape(canonical: str) -> str:
    """把能提前结束宿主元素或破坏 JS 解析的字符转成 \\uXXXX。

    覆盖 ``</script>``、``<!--``、``&`` 实体和 JS 里会被当行终止符的
    U+2028/U+2029。转义后仍是等价 JSON，JSON.parse 会还原原文。
    """
    return (
        canonical.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def csp_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def check_forbidden_api(js: str) -> list[str]:
    return [token for token in FORBIDDEN_JS if token in js]


def check_remote_refs(html: str) -> list[str]:
    hits: list[str] = []
    for pattern in REMOTE_PATTERNS:
        for match in pattern.finditer(html):
            # CSP meta 自身不是资源引用。
            if "Content-Security-Policy" in html[max(0, match.start() - 120):match.start()]:
                continue
            hits.append(f"{pattern.pattern} @ {match.start()}")
    return hits


def build(out_path: Path) -> tuple[str, str]:
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bundle.pop("bundle_sha256", None)

    canonical = canonical_json(bundle)
    bundle_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # bundle_sha256 是自身内容的哈希，因此单独作为呈现字段回填，
    # 不参与 canonical 序列化（否则哈希无法自洽）。
    bundle["bundle_sha256"] = bundle_sha256
    embedded = embed_escape(canonical_json(bundle))

    css = (SRC / "app.css").read_text(encoding="utf-8")
    js = (SRC / "app.js").read_text(encoding="utf-8")

    forbidden = check_forbidden_api(js)
    if forbidden:
        raise SystemExit(f"构建门禁失败：前端代码含禁用 API {forbidden}")

    template = (SRC / "index.template.html").read_text(encoding="utf-8")
    html = (
        template.replace("__CSP__", CSP.format(script_hash=csp_hash(js), style_hash=csp_hash(css)))
        .replace("__CSS__", css)
        .replace("__BUNDLE__", embedded)
        .replace("__JS__", js)
    )

    remote = check_remote_refs(html)
    if remote:
        raise SystemExit(f"构建门禁失败：产物含远端或外部资源引用 {remote}")

    if "</script>" in embedded:
        raise SystemExit("构建门禁失败：嵌入数据未转义 </script>")

    if ' style="' in html or " style='" in html:
        raise SystemExit("构建门禁失败：产物含 style 属性，会触发 style-src 违规")

    # CSP 哈希必须与真正内联进产物的内容一致，而不是与源文件一致。
    inline_style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    inline_script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    csp_line = html.split('http-equiv="Content-Security-Policy" content="', 1)[1].split('"', 1)[0]
    if csp_hash(inline_style) not in csp_line:
        raise SystemExit("构建门禁失败：style-src 哈希与内联样式不一致")
    if csp_hash(inline_script) not in csp_line:
        raise SystemExit("构建门禁失败：script-src 哈希与内联脚本不一致")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return bundle_sha256, html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    bundle_sha256, html = build(args.out)

    # 字节级可复现：同一 ReportBundle 重复渲染必须完全一致。
    _, again = build(args.out)
    if html != again:
        raise SystemExit("构建门禁失败：同一 bundle 两次渲染结果不一致")

    size_kb = len(html.encode("utf-8")) / 1024
    print(f"bundle_sha256 {bundle_sha256}")
    print(f"写入 {args.out}（{size_kb:.1f} KiB，单文件、零外部引用）")
    print("下一步：直接双击打开该文件，或按 prototypes/dashboard/README.md 逐条走 DASH-01~12。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

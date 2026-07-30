import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { transform } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const files = [
  resolve(root, "src/model.ts"),
  resolve(root, "src/dashboard.ts"),
  resolve(root, "../src/goodjob/dashboard_assets/dashboard.js"),
];
const domStyleMutation = /\.style(?:\.|\[)/;
const banned = [
  ["dynamic evaluation", /\beval\s*\(|\bnew\s+Function\b/],
  ["HTML string sink", /\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write|srcdoc)\b/],
  ["style string sink", /setAttribute\s*\(\s*["']style["']|\.cssText\b/],
  ["DOM style mutation", domStyleMutation],
  ["attribute event handler", /setAttribute\s*\(\s*["']on[a-z]+["']/i],
  ["string timer", /set(?:Timeout|Interval)\s*\(\s*["'`]/],
  ["network API", /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b/],
  ["persistent browser state", /\b(?:localStorage|sessionStorage|indexedDB)\b/],
];
for (const mutation of [
  "element.style.color = value",
  'element.style["color"] = value',
]) {
  if (!domStyleMutation.test(mutation)) {
    throw new Error(`DOM style mutation self-check missed: ${mutation}`);
  }
}
for (const file of files) {
  const source = await readFile(file, "utf8");
  for (const [label, pattern] of banned) {
    if (pattern.test(source)) {
      throw new Error(`${label} is forbidden in ${file}`);
    }
  }
}

const cssPath = resolve(root, "../src/goodjob/dashboard_assets/dashboard.css");
const css = await readFile(cssPath, "utf8");
await transform(css, { loader: "css", minify: false });
const forbiddenCss = [
  ["external CSS import", /@import\b/i],
  ["external CSS resource", /\burl\s*\(/i],
  ["horizontal scrolling fallback", /overflow-x\s*:\s*(?:auto|scroll)/i],
];
for (const [label, pattern] of forbiddenCss) {
  if (pattern.test(css)) throw new Error(`${label} is forbidden in ${cssPath}`);
}
for (const [label, pattern] of [
  ["mobile breakpoint", /@media\s*\(max-width:\s*767px\)/],
  ["forced-colors support", /@media\s*\(forced-colors:\s*active\)/],
  ["print expansion", /@media\s+print/],
  ["long-token wrapping", /overflow-wrap:\s*anywhere/],
  [
    "single-line coverage limitation action",
    /\.scope-link\s*\{[^}]*display:\s*inline-flex[^}]*white-space:\s*nowrap/s,
  ],
  [
    "native select width containment",
    /\.filter-select,\s*\.mobile-nav-select\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0[^}]*max-width:\s*100%/s,
  ],
]) {
  if (!pattern.test(css)) throw new Error(`${label} is required in ${cssPath}`);
}

const builtScript = await readFile(
  resolve(root, "../src/goodjob/dashboard_assets/dashboard.js"),
  "utf8",
);
if (/[\u2028-\u202e\u2066-\u2069]/u.test(builtScript)) {
  throw new Error("compiled dashboard must not contain raw bidirectional controls");
}

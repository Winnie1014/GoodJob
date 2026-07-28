// 跨引擎核对离线看板产物：WebKit + Chromium 各跑一遍 DASH-01~04、09 的可机检部分。
//
// Owner 很可能双击用 Safari 打开，所以 WebKit 不是可选项——两个引擎的 CSP 实现不同，
// 必须都验。首次使用需要装浏览器（约 100MB，只装一次）：
//
//   cd prototypes/dashboard
//   npm init -y && npm i playwright && npx playwright install webkit chromium
//   node verify.mjs
//
// 通过标准：末行打印「全部通过（两个引擎）」且退出码 0。
// 注意 probeInducedErrors 非零是预期的——本脚本会故意注入 style 属性做阳性对照，
// 且 Playwright 每次截图都会注入一个 <style>，两者都被这份 CSP 正确拒绝。

import { webkit, chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const FILE = "file://" + join(HERE, "out", "dashboard.html");
const SHOTS = join(HERE, "out", "shots");
mkdirSync(SHOTS, { recursive: true });

const WIDTHS = [1440, 1280, 1024, 900, 768, 480, 375];
const VIEWS = [
  ["overview", "#/v1/overview"],
  ["evidence", "#/v1/evidence"],
  ["gaps", "#/v1/gaps"],
  ["interview", "#/v1/interview"],
  ["project", "#/v1/project/p_coderoute"],
  ["injection", "#/v1/project/p_notes_vault"],
  ["version-mismatch", "#/v9/overview"]
];

async function run(name, engine) {
  const browser = await engine.launch();
  const consoleErrors = [];
  const externalRequests = [];
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 90)); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message.slice(0, 90)));
  page.on("request", (r) => { if (!r.url().startsWith("file://")) externalRequests.push(r.url()); });

  await page.goto(FILE, { waitUntil: "load" });
  await page.waitForTimeout(400);

  // 干净加载的错误必须为零。之后的探针会故意触发 CSP，且 Playwright 每次截图会
  // 注入一个 <style>（同样被本策略拒绝），所以断言只看这个切点之前的错误。
  const cleanLoadErrors = consoleErrors.slice();

  // DASH-01/03：零请求、注入语料只作文本、外部 URL 不成为可导航目标
  const injection = await page.evaluate(() => ({
    imgTags: document.querySelectorAll("img").length,
    anchors: [...document.querySelectorAll("a")].map((a) => a.getAttribute("href")),
    iframes: document.querySelectorAll("iframe, object, embed").length
  }));

  // ADR-0008 决策 6：style 属性被 CSP 拦下，CSSOM setter 可用。
  // 这里同时是一次阳性对照——如果注入 style 属性没有引发 CSP 违规，说明策略没生效。
  const errorsBeforeProbe = consoleErrors.length;
  const cspBehaviour = await page.evaluate(() => {
    const d = document.createElement("div");
    d.setAttribute("style", "width:10px");
    document.body.appendChild(d);
    const attr = getComputedStyle(d).width;
    d.remove();
    const s = document.createElement("span");
    s.style.display = "block";
    s.style.width = "33px";
    document.body.appendChild(s);
    const cssom = getComputedStyle(s).width;
    s.remove();
    return { styleAttrBlocked: attr !== "10px", cssomApplied: cssom === "33px" };
  });
  await page.waitForTimeout(120);
  cspBehaviour.violationReported = consoleErrors.length > errorsBeforeProbe;

  // DASH-04：全宽度 × 全视图无横向溢出
  const overflowFailures = [];
  for (const w of WIDTHS) {
    await page.setViewportSize({ width: w, height: 900 });
    for (const [, hash] of VIEWS) {
      await page.evaluate((h) => { window.location.hash = h; }, hash);
      await page.waitForTimeout(70);
      const bad = await page.evaluate(() =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth);
      if (bad) overflowFailures.push(`${w}px ${hash}`);
    }
  }

  // 打印：折叠展开、导航隐藏、取证条与覆盖条保留
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ media: "print" });
  await page.evaluate(() => { window.location.hash = "#/v1/project/p_coderoute"; });
  await page.waitForTimeout(150);
  const print = await page.evaluate(() => ({
    navHidden: getComputedStyle(document.getElementById("nav")).display === "none",
    footerHidden: getComputedStyle(document.getElementById("footer")).display === "none",
    assayKept: getComputedStyle(document.getElementById("assay")).display !== "none",
    bandKept: getComputedStyle(document.getElementById("band")).display !== "none",
    summariesHidden: [...document.querySelectorAll("details > summary")]
      .every((s) => getComputedStyle(s).display === "none")
  }));
  await page.emulateMedia({ media: "screen" });

  // 截图只在 WebKit 抓一次即可；深色是独立选取的一套，不是反色
  if (name === "webkit") {
    for (const [label, hash] of VIEWS) {
      await page.evaluate((h) => { window.location.hash = h; window.scrollTo(0, 0); }, hash);
      await page.waitForTimeout(150);
      await page.screenshot({ path: join(SHOTS, `1440-${label}.png`) });
    }
    for (const [w, h] of [[375, 812], [768, 1000]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.evaluate(() => { window.location.hash = "#/v1/overview"; window.scrollTo(0, 0); });
      await page.waitForTimeout(150);
      await page.screenshot({ path: join(SHOTS, `${w}-overview.png`) });
    }
    const dark = await browser.newContext({
      viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "dark"
    });
    const dp = await dark.newPage();
    await dp.goto(FILE, { waitUntil: "load" });
    await dp.waitForTimeout(300);
    await dp.screenshot({ path: join(SHOTS, "1440-dark.png") });
  }

  await browser.close();
  return {
    cleanLoadErrors,
    probeInducedErrors: consoleErrors.length - cleanLoadErrors.length,
    externalRequests, overflowFailures, injection, cspBehaviour, print
  };
}

const result = {
  webkit: await run("webkit", webkit),
  chromium: await run("chromium", chromium)
};

const failed = Object.entries(result).flatMap(([engine, r]) => [
  ...r.cleanLoadErrors.map((e) => `${engine} 干净加载报错: ${e}`),
  ...r.externalRequests.map((u) => `${engine} 外部请求: ${u}`),
  ...r.overflowFailures.map((w) => `${engine} 横向溢出: ${w}`),
  ...(r.injection.imgTags ? [`${engine} 出现 <img>`] : []),
  ...(r.injection.iframes ? [`${engine} 出现 iframe/object/embed`] : []),
  ...(r.injection.anchors.some((h) => h !== "#main") ? [`${engine} 出现非锚点链接`] : []),
  ...(r.cspBehaviour.styleAttrBlocked ? [] : [`${engine} style 属性未被 CSP 拦下`]),
  ...(r.cspBehaviour.cssomApplied ? [] : [`${engine} CSSOM setter 未生效`]),
  ...(r.cspBehaviour.violationReported ? [] : [`${engine} 注入 style 属性未触发 CSP 违规，策略可能未生效`]),
  ...Object.entries(r.print).filter(([, ok]) => !ok).map(([k]) => `${engine} 打印检查失败: ${k}`)
]);

console.log(JSON.stringify(result, null, 2));
console.log(failed.length ? "\n未通过：\n" + failed.join("\n") : "\n全部通过（两个引擎）");
process.exit(failed.length ? 1 : 0);

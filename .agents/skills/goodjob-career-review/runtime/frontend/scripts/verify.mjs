import { spawnSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { chromium, webkit } from "playwright";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const runtimeRoot = resolve(frontendRoot, "..");
const outputDirectory = resolve(frontendRoot, "verify-out");
const outputFile = resolve(outputDirectory, "dashboard.html");
const mutation = process.argv.find((value) => value.startsWith("--mutation="))?.split("=", 2)[1] ?? null;

const widths = [1440, 1280, 1024, 768, 375];
const views = [
  ["overview", "#/v1/overview"],
  ["project-list", "#/v1/project"],
  ["project-detail", "#/v1/project/p_primary"],
  ["evidence", "#/v1/evidence"],
  ["gaps", "#/v1/gaps"],
  ["interview", "#/v1/interview"],
  ["version-mismatch", "#/v9/overview"],
];
const locatorText = '{"line_end":12,"line_start":10,"path":"src/app.py"}';

function tokens(value, kind = "text") {
  return [{ kind, value }];
}

function assessment(rank, score) {
  return {
    rank,
    dimension_scores_milli: { implementation_depth: score },
    coverage_bps: 10000,
    base_score_milli: score,
    final_score_milli: score,
    rationale_tokens: tokens("实现机制、边界与验证路径都有冻结证据。"),
  };
}

function project(project_id, display_name, snapshot_disposition, eligible, score = null) {
  return {
    project_id,
    display_name,
    workspace_relative_location: project_id,
    project_snapshot_id: eligible ? `snapshot_${project_id}` : null,
    snapshot_disposition,
    coverage_status: eligible ? "complete" : null,
    eligible,
    assessment: score === null ? null : assessment(score.rank, score.value),
    modules: eligible
      ? [{
          module_id: `module_${project_id}`,
          project_id,
          name: "核心模块",
          kind: "python",
          relative_root: "src",
          adapter_id: "python-v1",
        }]
      : [],
    claim_ids: [],
    gap_ids: [],
  };
}

function evidence(evidence_id, validity, commit_state, locator, project_id = "p_primary") {
  return {
    evidence_id,
    project_id,
    module_id: `module_${project_id}`,
    worktree: {
      worktree_id: `worktree_${project_id}`,
      branch: "main",
      head_commit: "0123456789abcdef",
      dirty_state: "clean",
      observed_scan_run_id: "scan_verify",
    },
    origin_kind: "workspace",
    evidence_kind: "implementation",
    relative_path: locator.path,
    locator,
    summary_tokens: tokens(`证据 ${evidence_id}：当前实现及验证边界。`),
    commit_state,
    validity,
    content_sha256: `sha256_${evidence_id}`,
    content_equivalence_key: null,
  };
}

function claim(claim_id, support_level, evidence_ids, project_id = "p_primary") {
  return {
    claim_id,
    claim_revision_id: `revision_${claim_id}`,
    rank: 1,
    section: "project_story",
    category: "implementation_method",
    scope_kind: "module",
    project_id,
    worktree_id: `worktree_${project_id}`,
    module_id: `module_${project_id}`,
    statement_tokens: tokens(`我实现并验证了 ${claim_id} 的关键机制。`),
    facets: ["implemented", "verified"],
    support_level,
    personal_attribution: "implemented",
    evidence_relations: evidence_ids.map((evidence_id) => ({
      evidence_id,
      relation: "supports",
      supported_facets: ["implemented"],
    })),
  };
}

const projects = [
  project("p_primary", "主要项目", "fresh", true, { rank: 1, value: 900 }),
  project("p_carried", "沿用项目", "carried_forward", true, { rank: 2, value: 700 }),
  project("p_failed", "失败项目", "failed_no_baseline", false),
  project("p_excluded", "排除项目", "excluded", false),
];
const evidenceItems = [
  evidence("e_current", "current", "committed", { path: "src/app.py", line_start: 10, line_end: 12 }),
  evidence("e_stale", "stale", "modified", { path: "src/old.py", symbol: "legacy" }),
  evidence("e_missing", "missing", "untracked", { path: "src/missing.py" }, "p_carried"),
  evidence("e_plan", "plan", "not_applicable", { path: "docs/plan.md", heading: "下一步" }),
  evidence("e_history", "current", "historical", { commit: "0123456789abcdef", path: "src/history.py" }),
];
const claims = [
  claim("c_single", "single_source", ["e_current"]),
  claim("c_cross", "cross_checked", ["e_stale", "e_history"]),
  claim("c_user", "user_confirmed", ["e_missing"], "p_carried"),
  claim("c_conflict", "conflicted", ["e_plan"]),
];
for (const item of projects) {
  item.claim_ids = claims.filter((entry) => entry.project_id === item.project_id).map((entry) => entry.claim_id);
}

const bundle = {
  contract_version: "report-bundle-v1",
  bundle_sha256: "computed-by-python",
  preparation_run_id: "preparation_verify_browser_gate",
  scan_run_id: "scan_verify",
  generated_at: "2026-07-31T00:00:00Z",
  primary_language: "zh-CN",
  package_status: "partial",
  role: {
    name: "应用软件工程师",
    applied_level: "高级",
    level_source: "owner",
    jd: { input_kind: "none", content_sha256: null, has_jd: false },
  },
  role_lens: {
    role_lens_id: "role_lens_verify",
    contract_version: "role-lens-v1",
    lens_sha256: "role_lens_sha256",
    dimensions: [{
      key: "implementation_depth",
      display_name: "实现深度",
      weight_bps: 10000,
      evaluation_criteria: "解释实现机制、边界、验证和取舍",
      required_evidence_kinds: ["implementation"],
    }],
    assumptions: ["VERIFY_ROLE_ASSUMPTION：未提供 JD，按高级应用软件工程师准备。"],
  },
  coverage: {
    projects_total: 4,
    eligible_projects: 2,
    disposition_counts: { fresh: 1, carried_forward: 1, failed_no_baseline: 1, excluded: 1 },
    excluded_by_category: { generated_or_dependency: 1 },
    excluded_by_category_available: true,
    limitations: [{
      limitation_id: "limitation_primary",
      kind: "scan_issue:partial_read",
      severity: "medium",
      project_id: "p_primary",
      message_tokens: tokens("主要项目存在一项可定位的扫描限制。"),
      impact_tokens: tokens("该项目的证据覆盖可能不完整。"),
      remediation_tokens: tokens("在证据视图按项目核对。"),
      filter_route: "#/v1/evidence?project=p_primary",
    }],
  },
  projects,
  claims,
  evidence: evidenceItems,
  knowledge_gaps: [{
    gap_id: "gap_primary",
    gap_key: "tradeoff_context",
    scope_kind: "project",
    project_id: "p_primary",
    module_id: null,
    dimension: "implementation_depth",
    description_tokens: tokens("仍需补充方案取舍的上下文。"),
    severity: "high",
    resolution_kind: "owner_follow_up",
    status: "open",
  }],
  review: {
    contract_version: "review-subject-projection-v1",
    cutoff_at: "2026-07-31T00:00:00Z",
    status: "frozen",
    skill_invocation: "$goodjob-career-review 更新复习状态",
    bindings: [
      { review_target_binding_id: "binding_new", review_target_id: "target_new", continuity_status: "new", summary: null, mastery_level: null, weak_points: [], next_review_at: null, reviewed_at: null, historical_review: null },
      { review_target_binding_id: "binding_continued", review_target_id: "target_continued", continuity_status: "continued", summary: "可以讲清实现路径。", mastery_level: "solid", weak_points: [], next_review_at: "2026-08-07", reviewed_at: "2026-07-31", historical_review: null },
      { review_target_binding_id: "binding_reassess", review_target_id: "target_reassess", continuity_status: "reassess_required", summary: null, mastery_level: null, weak_points: ["取舍"], next_review_at: null, reviewed_at: null, historical_review: { summary: "旧语义下曾复习。", mastery_level: "developing", weak_points: ["边界"], next_review_at: null, reviewed_at: "2026-07-01" } },
    ],
  },
  interview: {
    two_minute_pitch_claim_ids: ["c_cross", "c_single"],
    questions: [{
      question_id: "question_primary",
      level: "L2",
      project_id: "p_primary",
      module_id: "module_p_primary",
      claim_id: "c_cross",
      prompt_tokens: tokens("这个模块如何实现，为什么这样设计？"),
      follow_up_tokens: tokens("如何验证边界条件？"),
    }],
  },
  search_index: {
    index_version: "search-index-v1",
    entries: [
      ...projects.map((item) => ({ item_id: `project:${item.project_id}`, kind: "project", route: `#/v1/project/${item.project_id}`, search_text: item.display_name.toLocaleLowerCase("zh-CN"), project_id: item.project_id, module_id: null })),
      ...claims.map((item) => ({ item_id: `claim:${item.claim_id}`, kind: "claim", route: `#/v1/evidence?claim=${item.claim_id}`, search_text: item.claim_id, project_id: item.project_id, module_id: item.module_id })),
      ...evidenceItems.map((item) => ({ item_id: `evidence:${item.evidence_id}`, kind: "evidence", route: `#/v1/evidence?evidence=${item.evidence_id}`, search_text: item.evidence_id, project_id: item.project_id, module_id: item.module_id })),
    ],
  },
  export_projection: { projection_sha256: "unused-by-dashboard" },
};

const python = [
  "import json, sys",
  "from goodjob.reporting import render_dashboard_html, report_bundle_sha256",
  "bundle = json.load(sys.stdin)",
  "bundle['bundle_sha256'] = report_bundle_sha256(bundle)",
  "sys.stdout.write(render_dashboard_html(bundle))",
].join("; ");

await mkdir(outputDirectory, { recursive: true });
const rendered = spawnSync("uv", ["run", "python", "-c", python], {
  cwd: runtimeRoot,
  input: JSON.stringify(bundle),
  encoding: "utf8",
  maxBuffer: 16 * 1024 * 1024,
});
if (rendered.status !== 0) {
  process.stderr.write(JSON.stringify({ phase: "render", status: rendered.status, stderr: rendered.stderr }, null, 2));
  process.exit(1);
}
let html = rendered.stdout;
if (mutation === "csp-disabled") {
  html = html.replace(/<meta http-equiv="Content-Security-Policy"[^>]+>\n/, "");
}
await writeFile(outputFile, html, "utf8");

const checks = [];
function record(engine, assertion, ok, details = {}, width = null, view = null) {
  checks.push({ engine, width, view, assertion, ok: Boolean(ok), details });
}

async function route(page, hash) {
  await page.evaluate((value) => {
    location.hash = value;
    window.scrollTo(0, 0);
  }, hash);
  await page.waitForTimeout(35);
}

async function taggedGroup(page, statuses) {
  return page.evaluate((expected) => {
    const entries = expected.map((status) => {
      const tag = document.querySelector(`.status-tag[data-status="${status}"]`);
      const parts = tag ? [...tag.children].map((part) => part.textContent?.trim() ?? "") : [];
      return {
        status,
        visible: tag instanceof HTMLElement && tag.getClientRects().length > 0,
        symbol: parts[0] ?? "",
        label: parts[1] ?? "",
        forcedColorAdjust: tag instanceof HTMLElement ? getComputedStyle(tag).forcedColorAdjust : null,
      };
    });
    return {
      ok: entries.every((entry) => entry.visible && Boolean(entry.symbol) && Boolean(entry.label)),
      mediaActive: matchMedia("(forced-colors: active)").matches,
      entries,
    };
  }, statuses);
}

async function runEngine(engineName, engine) {
  const browser = await engine.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const externalRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("request", (request) => {
    if (!request.url().startsWith("file:")) externalRequests.push(request.url());
  });

  try {
    await page.goto(pathToFileURL(outputFile).href, { waitUntil: "load" });
    await page.waitForSelector(".shell");

    await route(page, "#/v1/overview");
    if (mutation === "role-assumption") {
      await page.locator(".role-assumptions li").first().evaluate((node) => node.remove());
    }
    const assumptionVisible = await page.locator(".role-assumptions li", { hasText: "VERIFY_ROLE_ASSUMPTION" }).isVisible().catch(() => false);
    record(engineName, "role-lens-assumption-visible", assumptionVisible);

    const scopeLink = page.locator(".scope-link").first();
    if (mutation === "scope-activation") {
      await scopeLink.evaluate((node) => node.setAttribute("href", "#/v1/gaps"));
    }
    if (mutation === "scope-navigation") {
      await scopeLink.evaluate((node) => document.querySelector("nav")?.append(node));
    }
    await scopeLink.focus();
    record(engineName, "coverage-scope-link-focusable", await scopeLink.evaluate((node) => node === document.activeElement && node instanceof HTMLAnchorElement));
    record(engineName, "coverage-scope-link-outside-nav", await scopeLink.evaluate((node) => node.closest("nav") === null));
    await scopeLink.press("Enter");
    await page.waitForFunction(
      () => document.querySelector(".view-title")?.textContent === "证据横切",
      null,
      { timeout: 500 },
    ).catch(() => undefined);
    const scope = await page.evaluate(() => ({
      hash: location.hash,
      project: document.querySelector('select[aria-label="项目"]')?.value ?? null,
      count: document.querySelector(".result-count")?.textContent ?? null,
    }));
    record(engineName, "coverage-scope-link-activates-filter", scope.hash === "#/v1/evidence?project=p_primary" && scope.project === "p_primary" && scope.count === "3 条 Claim", scope);

    await route(page, "#/v1/overview");
    await page.keyboard.press("/");
    await page.waitForTimeout(40);
    if (mutation === "search-focus") await page.evaluate(() => document.activeElement?.blur());
    const search = await page.evaluate(() => ({ hash: location.hash, activeId: document.activeElement?.id ?? null }));
    record(engineName, "deferred-search-focus", search.hash === "#/v1/evidence" && search.activeId === "report-search", search);

    await route(page, "#/v1/project");
    const projectItem = page.locator(".project-item").first();
    if (mutation === "project-activation") {
      await projectItem.locator("a").first().evaluate((node) => node.removeAttribute("href"));
    }
    await projectItem.focus();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(40);
    const activation = await page.evaluate(() => ({ hash: location.hash, title: document.querySelector(".view-title")?.textContent ?? null }));
    record(engineName, "focused-project-enter-activation", activation.hash === "#/v1/project/p_primary" && activation.title === "主要项目", activation);

    await page.emulateMedia({ forcedColors: "active" });
    await route(page, "#/v1/overview");
    const dispositionGroup = await taggedGroup(page, ["fresh", "carried_forward", "failed_no_baseline", "excluded"]);
    record(engineName, "forced-colors-project-disposition", dispositionGroup.ok, dispositionGroup);
    await route(page, "#/v1/evidence");
    await page.evaluate(() => document.querySelectorAll("details").forEach((item) => { item.open = true; }));
    const validityGroup = await taggedGroup(page, ["current", "stale", "missing", "plan"]);
    record(engineName, "forced-colors-evidence-validity", validityGroup.ok, validityGroup);
    const supportGroup = await taggedGroup(page, ["single_source", "cross_checked", "user_confirmed", "conflicted"]);
    record(engineName, "forced-colors-support-level", supportGroup.ok, supportGroup);
    const commitStates = await page.locator(".evidence-heading .token-code").evaluateAll((nodes) => ({
      values: [...new Set(nodes.map((node) => node.textContent?.trim() ?? ""))].sort(),
      allVisible: nodes.every((node) => node instanceof HTMLElement && node.getClientRects().length > 0),
      monospace: nodes.every((node) => getComputedStyle(node).fontFamily.length > 0),
    }));
    const expectedCommits = ["committed", "historical", "modified", "not_applicable", "untracked"];
    record(engineName, "forced-colors-commit-state-text-channel", commitStates.allVisible && commitStates.monospace && JSON.stringify(commitStates.values) === JSON.stringify(expectedCommits), commitStates);
    await route(page, "#/v1/interview");
    const continuityGroup = await taggedGroup(page, ["new", "continued", "reassess_required"]);
    record(engineName, "forced-colors-review-continuity", continuityGroup.ok, continuityGroup);
    await page.emulateMedia({ forcedColors: "none" });

    await route(page, "#/v1/evidence");
    await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
    await page.emulateMedia({ media: "print" });
    const print = await page.evaluate((expectedLocator) => {
      const hidden = [...document.querySelectorAll(".side-nav,.mobile-nav,.toolbar,.footer,.copy-value,.copy-status,.skip-link")]
        .every((node) => getComputedStyle(node).display === "none");
      const locator = [...document.querySelectorAll(".print-value")].find((node) => node.textContent === expectedLocator);
      return {
        controlsHidden: hidden,
        detailsExpanded: [...document.querySelectorAll("details")].every((item) => item.open),
        locatorComplete: locator instanceof HTMLElement && getComputedStyle(locator).display !== "none",
        locatorText: locator?.textContent ?? null,
      };
    }, locatorText);
    record(engineName, "print-controls-hidden", print.controlsHidden, print);
    record(engineName, "print-details-expanded", print.detailsExpanded, print);
    record(engineName, "print-full-locator", print.locatorComplete && print.locatorText === locatorText, print);
    await page.emulateMedia({ media: "screen" });
    await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));

    if (mutation === "overflow") {
      await page.evaluate(() => {
        const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        node.setAttribute("width", "2000");
        node.setAttribute("height", "1");
        document.body.append(node);
      });
    }
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      for (const [view, hash] of views) {
        await route(page, hash);
        const dimensions = await page.evaluate(() => ({
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        }));
        record(engineName, "no-horizontal-overflow", dimensions.scrollWidth <= dimensions.clientWidth, dimensions, width, view);
      }
    }

    const clean = {
      consoleErrors: [...consoleErrors],
      pageErrors: [...pageErrors],
      externalRequests: [...externalRequests],
    };
    record(engineName, "clean-console-errors", clean.consoleErrors.length === 0, clean);
    record(engineName, "clean-page-errors", clean.pageErrors.length === 0, clean);
    record(engineName, "clean-external-requests", clean.externalRequests.length === 0, clean);

    const violations = await page.evaluate(() => {
      const records = [];
      window.__goodjobCspViolations = records;
      document.addEventListener("securitypolicyviolation", (event) => {
        records.push({ directive: event.effectiveDirective, blockedURI: event.blockedURI });
      });
      return records.length;
    });
    const errorsBeforeProbe = consoleErrors.length;
    const styleProbe = await page.evaluate(() => {
      const node = document.createElement("div");
      node.setAttribute("style", "width: 37px");
      document.body.append(node);
      const applied = getComputedStyle(node).width === "37px";
      node.remove();
      return { applied };
    });
    await page.evaluate(async () => {
      try { await fetch("https://goodjob.invalid/csp-probe"); } catch { /* expected */ }
    });
    await page.waitForTimeout(120);
    const probeViolations = await page.evaluate(() => window.__goodjobCspViolations ?? []);
    const styleViolation = probeViolations.some((item) => ["style-src", "style-src-attr"].includes(item.directive));
    const connectViolation = probeViolations.some((item) => item.directive === "connect-src");
    record(engineName, "csp-style-positive-control", !styleProbe.applied && styleViolation, { styleProbe, violations: probeViolations });
    record(engineName, "csp-connect-probe", connectViolation, { violations: probeViolations });
    record(engineName, "probe-errors-separated", consoleErrors.length >= errorsBeforeProbe && violations === 0, {
      cleanConsoleErrorCount: clean.consoleErrors.length,
      probeConsoleErrorCount: consoleErrors.length - errorsBeforeProbe,
    });
  } catch (error) {
    record(engineName, "engine-run-completed", false, { message: error instanceof Error ? error.stack : String(error) });
  } finally {
    await browser.close();
  }
}

await runEngine("chromium", chromium);
if (mutation === null) await runEngine("webkit", webkit);

const failures = checks.filter((check) => !check.ok);
const result = {
  contract: "goodjob-browser-verification-v1",
  artifact: outputFile,
  renderer: "goodjob.reporting.render_dashboard_html",
  mutation,
  summary: { passed: checks.length - failures.length, failed: failures.length, total: checks.length },
  failures,
};
console.log(JSON.stringify(result, null, 2));
process.exitCode = failures.length === 0 ? 0 : 1;

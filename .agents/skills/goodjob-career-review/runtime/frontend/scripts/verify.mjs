import { spawnSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { chromium, webkit } from "playwright";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const runtimeRoot = resolve(frontendRoot, "..");
const outputDirectory = resolve(frontendRoot, "verify-out");
const outputFile = resolve(outputDirectory, "dashboard.html");
const completedOutputFile = resolve(outputDirectory, "dashboard-completed.html");
const reportOutputFile = resolve(outputDirectory, "report.zh-CN.md");
const mutation = process.argv.find((value) => value.startsWith("--mutation="))?.split("=", 2)[1] ?? null;

const widths = [1440, 1280, 1024, 768, 375];
const views = [
  ["overview", "#/v1/overview"],
  ["project-list", "#/v1/project"],
  ["project-detail", "#/v1/project/p_primary"],
  ["project-module", "#/v1/project/p_primary/module/module_p_primary"],
  ["evidence", "#/v1/evidence"],
  ["gaps", "#/v1/gaps"],
  ["interview", "#/v1/interview"],
  ["review-target", "#/v1/interview/target/target_continued"],
  ["version-mismatch", "#/v9/overview"],
];
const requiredContractAssertions = [
  "dash10-completed-snapshot-identity",
  "dash10-partial-snapshot-identity",
  "dash10-same-role-distinct-snapshots",
  "dash10-cross-version-deep-link-rejected",
  "dash10-cross-version-no-wrong-object",
  "dash12-claim-evidence-parity",
  "dash12-limitation-parity",
  "dash12-no-html-only-conclusions",
];
const locatorText = '{"line_end":12,"line_start":10,"path":"src/app.py"}';
const hostileValue = 'VERIFY" <script> javascript:';
const hostileBidiValue = `${hostileValue}\u202e`;
const hostileLimitationId = `limitation_global_${hostileBidiValue}`;
const statusChannels = {
  fresh: { symbol: "✓", label: "本次新鲜 1" },
  carried_forward: { symbol: "◷", label: "沿用基线 1" },
  failed_no_baseline: { symbol: "!", label: "失败无基线 1" },
  excluded: { symbol: "○", label: "已排除 1" },
  current: { symbol: "✓", label: "当前" },
  stale: { symbol: "◷", label: "历史限制" },
  missing: { symbol: "!", label: "已缺失" },
  plan: { symbol: "◷", label: "已规划" },
  single_source: { symbol: "○", label: "单一来源" },
  cross_checked: { symbol: "✓", label: "交叉验证" },
  user_confirmed: { symbol: "✓", label: "用户确认" },
  conflicted: { symbol: "!", label: "存在冲突" },
  new: { symbol: "○", label: "待首次复习" },
  continued: { symbol: "✓", label: "状态延续" },
  reassess_required: { symbol: "!", label: "需要重评" },
  completed: { symbol: "○", label: "完整快照" },
  partial: { symbol: "!", label: "部分快照" },
  low: { symbol: "○", label: "低" },
  medium: { symbol: "◷", label: "中" },
  high: { symbol: "!", label: "高" },
  critical: { symbol: "!", label: "严重" },
};

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
claims[1].evidence_relations[0].supported_facets = ["implemented", "verified"];
claims[1].facets.push(hostileValue);
claims[1].evidence_relations[0].supported_facets.push(hostileValue);
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
    limitations: [
      {
        limitation_id: "limitation_primary",
        kind: "scan_issue:partial_read",
        severity: "medium",
        project_id: "p_primary",
        message_tokens: tokens("主要项目存在一项可定位的扫描限制。"),
        impact_tokens: tokens("该项目的证据覆盖可能不完整。"),
        remediation_tokens: tokens("在证据视图按项目核对。"),
        filter_route: "#/v1/evidence?project=p_primary",
      },
      {
        limitation_id: hostileLimitationId,
        kind: "job_context:assumed_level",
        severity: "low",
        project_id: null,
        message_tokens: tokens(`岗位职级来自冻结假设：${hostileBidiValue}`),
        impact_tokens: tokens("准备角度可能与真实 JD 有偏差。"),
        remediation_tokens: tokens("补充 JD 后创建新的准备运行。"),
        filter_route: "#/v1/overview",
      },
    ],
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
  export_projection: {
    contract_version: "export-projection-v1",
    items: [],
    projection_sha256: "unused-by-dashboard",
  },
};

function renderSnapshot(source) {
  const python = [
    "import json, sys",
    "from goodjob.reporting import render_dashboard_html, render_report_markdown, report_bundle_sha256",
    "bundle = json.load(sys.stdin)",
    "bundle['bundle_sha256'] = report_bundle_sha256(bundle)",
    "result = {'bundle': bundle, 'html': render_dashboard_html(bundle), 'markdown': render_report_markdown(bundle)}",
    "sys.stdout.write(json.dumps(result, ensure_ascii=False))",
  ].join("; ");
  const rendered = spawnSync("uv", ["run", "python", "-c", python], {
    cwd: runtimeRoot,
    input: JSON.stringify(source),
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (rendered.status !== 0) {
    process.stderr.write(JSON.stringify({ phase: "render", status: rendered.status, stderr: rendered.stderr }, null, 2));
    process.exit(1);
  }
  try {
    return JSON.parse(rendered.stdout);
  } catch (error) {
    process.stderr.write(JSON.stringify({ phase: "parse-rendered-snapshot", error: String(error) }, null, 2));
    process.exit(1);
  }
}

await mkdir(outputDirectory, { recursive: true });
const partialSnapshot = renderSnapshot(bundle);
const completedSource = structuredClone(bundle);
completedSource.package_status = "completed";
completedSource.preparation_run_id = `preparation_verify_browser_gate_completed_${hostileBidiValue}`;
const completedSnapshot = renderSnapshot(completedSource);
let html = partialSnapshot.html;
let reportMarkdown = partialSnapshot.markdown;
if (mutation === "csp-disabled") {
  html = html.replace(/<meta http-equiv="Content-Security-Policy"[^>]+>\n/, "");
}
if (mutation === "parity-field") {
  const mutatedMarkdown = reportMarkdown.replace(
    "`implementation` · `committed` · `current`",
    "`implementation` · `committed` · `stale`",
  );
  if (mutatedMarkdown === reportMarkdown) throw new Error("parity-field mutation did not alter Markdown");
  reportMarkdown = mutatedMarkdown;
}
await Promise.all([
  writeFile(outputFile, html, "utf8"),
  writeFile(completedOutputFile, completedSnapshot.html, "utf8"),
  writeFile(reportOutputFile, reportMarkdown, "utf8"),
]);

function requireProjection(condition, message) {
  if (!condition) throw new Error(message);
}

function decodeEntities(value) {
  return value
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

function inlineCodeValues(line) {
  return [...line.matchAll(/`([^`\r\n]*)`/gu)].map((match) => decodeEntities(match[1]));
}

function decodeMarkdownText(value) {
  return decodeEntities(value)
    .replace(/\*\*([^*]+)\*\*/gu, "$1")
    .replace(/`([^`\r\n]*)`/gu, (_, code) => decodeEntities(code))
    .replace(/\\([\\`*_{}\[\]()#+.!|~:-])/gu, "$1");
}

function compareStrings(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function limitationContentKey(limitation) {
  return JSON.stringify([
    limitation.kind,
    limitation.severity,
    limitation.message,
    limitation.impact,
    limitation.remediation,
  ]);
}

function limitationKey(limitation) {
  return JSON.stringify([limitation.scope, limitationContentKey(limitation)]);
}

function reconcileLimitations(overviewLimitations, projectLimitations) {
  const remaining = [...overviewLimitations];
  const scoped = [];
  for (const projectLimitation of projectLimitations) {
    const key = limitationContentKey(projectLimitation);
    const matches = remaining
      .map((limitation, index) => ({ limitation, index }))
      .filter(({ limitation }) => limitationContentKey(limitation) === key);
    requireProjection(matches.length === 1, `project limitation has ${matches.length} overview matches: ${key}`);
    const match = matches[0];
    remaining.splice(match.index, 1);
    scoped.push({ ...match.limitation, ...projectLimitation });
  }
  const result = [...remaining, ...scoped].sort((left, right) => compareStrings(limitationKey(left), limitationKey(right)));
  const keys = result.map(limitationKey);
  requireProjection(new Set(keys).size === keys.length, "limitation semantic keys must be unique");
  return result;
}

function normalizeClaims(claims) {
  requireProjection(claims.length > 0, "claim projection must not be empty");
  const claimIds = claims.map((claim) => claim.claim_id);
  requireProjection(claimIds.every(Boolean), "claim projection contains an empty ID");
  requireProjection(new Set(claimIds).size === claimIds.length, "claim projection contains duplicate IDs");
  return claims.map((claim) => {
    requireProjection(Array.isArray(claim.facets) && claim.facets.length > 0, `claim ${claim.claim_id} has no facets`);
    requireProjection(Array.isArray(claim.evidence_relations) && claim.evidence_relations.length > 0, `claim ${claim.claim_id} has no Evidence relations`);
    const relations = claim.evidence_relations.map((relation) => {
      requireProjection(
        [relation.evidence_id, relation.relation, relation.validity, relation.commit_state].every(Boolean),
        `claim ${claim.claim_id} has an incomplete Evidence relation`,
      );
      requireProjection(
        Array.isArray(relation.supported_facets) && relation.supported_facets.length > 0,
        `claim ${claim.claim_id} Evidence ${relation.evidence_id} has no supported facets`,
      );
      return relation;
    }).sort((left, right) => compareStrings(
      JSON.stringify([left.evidence_id, left.relation]),
      JSON.stringify([right.evidence_id, right.relation]),
    ));
    const relationKeys = relations.map((relation) => JSON.stringify([relation.evidence_id, relation.relation]));
    requireProjection(new Set(relationKeys).size === relationKeys.length, `claim ${claim.claim_id} has duplicate Evidence relations`);
    return { claim_id: claim.claim_id, facets: claim.facets, evidence_relations: relations };
  }).sort((left, right) => compareStrings(left.claim_id, right.claim_id));
}

function parseLimitation(lines, index, scope) {
  const match = lines[index].match(/^- \*\*(.+?) · (.+?)\*\*：(.*)$/u);
  if (!match) return null;
  const impact = lines[index + 1];
  const remediation = lines[index + 2];
  requireProjection(impact?.startsWith("  - 影响："), `limitation impact is missing at line ${index + 2}`);
  requireProjection(remediation?.startsWith("  - 补救："), `limitation remediation is missing at line ${index + 3}`);
  return {
    nextIndex: index + 2,
    value: {
      scope,
      severity: match[1],
      kind: match[2],
      message: decodeMarkdownText(match[3]),
      impact: decodeMarkdownText(impact.slice("  - 影响：".length)),
      remediation: decodeMarkdownText(remediation.slice("  - 补救：".length)),
    },
  };
}

function parseMarkdownProjection(markdown) {
  const lines = markdown.split("\n");
  const claims = [];
  const claimById = new Map();
  const overviewLimitations = [];
  const projectLimitations = [];
  let currentClaim = null;
  let currentRelation = null;
  let currentProjectId = null;
  let inCoverage = false;
  let inProjectLimitations = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.startsWith("## ")) {
      inCoverage = line === "## 覆盖与限制";
      inProjectLimitations = false;
    }
    if (line.startsWith("### ") && !line.startsWith("#### ")) {
      currentProjectId = null;
      inProjectLimitations = false;
    }
    if (line.startsWith("- Project ID：")) {
      const values = inlineCodeValues(line);
      requireProjection(values.length === 1 && values[0].length > 0, `invalid Project ID line ${index + 1}`);
      currentProjectId = values[0];
    }
    if (line.startsWith("#### ")) inProjectLimitations = line === "#### 扫描限制";

    if (inCoverage || inProjectLimitations) {
      const parsed = parseLimitation(lines, index, inProjectLimitations ? currentProjectId : null);
      if (parsed) {
        requireProjection(!inProjectLimitations || currentProjectId !== null, `project limitation lacks Project ID at line ${index + 1}`);
        (inProjectLimitations ? projectLimitations : overviewLimitations).push(parsed.value);
        index = parsed.nextIndex;
        continue;
      }
    }

    if (/^#### .+ · Claim `/u.test(line)) {
      requireProjection(currentRelation === null, `Evidence relation lacks supported facets before line ${index + 1}`);
      const values = inlineCodeValues(line);
      requireProjection(values.length === 1 && values[0].length > 0, `invalid Claim heading at line ${index + 1}`);
      requireProjection(!claimById.has(values[0]), `duplicate Claim ${values[0]}`);
      currentClaim = { claim_id: values[0], facets: null, evidence_relations: [] };
      claimById.set(values[0], currentClaim);
      claims.push(currentClaim);
      continue;
    }
    if (currentClaim && line.startsWith("- Facets：")) {
      requireProjection(currentClaim.facets === null, `duplicate facets for Claim ${currentClaim.claim_id}`);
      currentClaim.facets = inlineCodeValues(line);
      continue;
    }
    if (currentClaim && line.startsWith("- Evidence ")) {
      requireProjection(currentRelation === null, `Evidence relation lacks supported facets before line ${index + 1}`);
      const values = inlineCodeValues(line);
      requireProjection(values.length === 5, `invalid Evidence line ${index + 1}`);
      currentRelation = {
        evidence_id: values[0],
        relation: values[1],
        commit_state: values[3],
        validity: values[4],
        supported_facets: null,
      };
      currentClaim.evidence_relations.push(currentRelation);
      continue;
    }
    if (currentRelation && line.startsWith("  - Supported facets：")) {
      currentRelation.supported_facets = inlineCodeValues(line);
      currentRelation = null;
    }
  }
  requireProjection(currentRelation === null, "final Evidence relation lacks supported facets");
  return {
    claims: normalizeClaims(claims),
    limitations: reconcileLimitations(overviewLimitations, projectLimitations),
    limitationHooksOk: true,
  };
}

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
  const expectedEntries = statuses.map((status) => ({ status, ...statusChannels[status] }));
  return page.evaluate((expected) => {
    const mediaActive = matchMedia("(forced-colors: active)").matches;
    const entries = expected.map(({ status, symbol: expectedSymbol, label: expectedLabel }) => {
      const tag = document.querySelector(`.status-tag[data-status="${status}"]`);
      const parts = tag ? [...tag.children].map((part) => part.textContent?.trim() ?? "") : [];
      return {
        status,
        visible: tag instanceof HTMLElement && tag.getClientRects().length > 0,
        symbol: parts[0] ?? "",
        label: parts[1] ?? "",
        expectedSymbol,
        expectedLabel,
        forcedColorAdjust: tag instanceof HTMLElement ? getComputedStyle(tag).forcedColorAdjust : null,
      };
    });
    return {
      ok: entries.every((entry) => (
        entry.visible
        && entry.symbol === entry.expectedSymbol
        && entry.label === entry.expectedLabel
      )),
      mediaActive,
      entries,
    };
  }, expectedEntries);
}

async function captureProjection(operation) {
  try {
    return { ok: true, value: await operation(), error: null };
  } catch (error) {
    return { ok: false, value: null, error: error instanceof Error ? error.message : String(error) };
  }
}

function firstDifference(left, right, path = "$") {
  if (Object.is(left, right)) return null;
  if (typeof left !== typeof right || left === null || right === null) return { path, left, right };
  if (typeof left !== "object") return { path, left, right };
  if (Array.isArray(left) !== Array.isArray(right)) return { path, left, right };
  if (Array.isArray(left)) {
    if (left.length !== right.length) return { path: `${path}.length`, left: left.length, right: right.length };
    for (let index = 0; index < left.length; index += 1) {
      const difference = firstDifference(left[index], right[index], `${path}[${index}]`);
      if (difference) return difference;
    }
    return null;
  }
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  if (JSON.stringify(leftKeys) !== JSON.stringify(rightKeys)) return { path: `${path}.keys`, left: leftKeys, right: rightKeys };
  for (const key of leftKeys) {
    const difference = firstDifference(left[key], right[key], `${path}.${key}`);
    if (difference) return difference;
  }
  return null;
}

function claimProjectionCoverage(claimsProjection) {
  const relations = claimsProjection.flatMap((claim) => claim.evidence_relations);
  const validities = new Set(relations.map((relation) => relation.validity));
  const commitStates = new Set(relations.map((relation) => relation.commit_state));
  return {
    ok: (
      claimsProjection.length >= 4
      && ["current", "stale", "missing", "plan"].every((value) => validities.has(value))
      && commitStates.size >= 5
      && claimsProjection.some((claim) => claim.facets.length > 1)
      && relations.some((relation) => relation.supported_facets.length > 1)
    ),
    claimCount: claimsProjection.length,
    relationCount: relations.length,
    validities: [...validities].sort(),
    commitStates: [...commitStates].sort(),
  };
}

function limitationProjectionCoverage(limitationsProjection) {
  return {
    ok: (
      limitationsProjection.length >= 2
      && limitationsProjection.some((limitation) => limitation.scope === null)
      && limitationsProjection.some((limitation) => limitation.scope !== null)
    ),
    limitationCount: limitationsProjection.length,
    scopes: limitationsProjection.map((limitation) => limitation.scope),
  };
}

function parseJsonStringArray(value, label) {
  requireProjection(typeof value === "string", `${label} is missing`);
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    throw new Error(`${label} is not JSON: ${String(error)}`);
  }
  requireProjection(Array.isArray(parsed) && parsed.every((item) => typeof item === "string"), `${label} is not a string array`);
  return parsed;
}

function expectedStatusText(value) {
  const channel = statusChannels[value];
  requireProjection(channel !== undefined, `status ${value} has no visible channel`);
  return `${channel.symbol}${channel.label}`;
}

function visibleText(value) {
  return value.replace(/[\u2028-\u202e\u2066-\u2069]/gu, (control) => `[U+${control.codePointAt(0).toString(16).toUpperCase()}]`);
}

async function visibleDomLimitations(page) {
  return page.locator(".degradation-item").evaluateAll((nodes) => nodes.map((node) => {
    const paragraphs = [...node.querySelectorAll(".degradation-copy > p")];
    return {
      limitationId: node.dataset.limitationId ?? null,
      kind: node.dataset.kind ?? null,
      severity: node.dataset.severity ?? null,
      projectIdHook: node.dataset.projectId ?? null,
      visible: node instanceof HTMLElement && node.getClientRects().length > 0,
      visibleChannels: [...node.querySelectorAll(".hanging-label > .status-tag, .hanging-label > span:not(.status-tag)")].every((channel) => channel instanceof HTMLElement && channel.getClientRects().length > 0),
      visibleKind: node.querySelector(".hanging-label > span:not(.status-tag)")?.textContent?.trim() ?? null,
      visibleSeverity: node.querySelector(".hanging-label > .status-tag")?.textContent?.trim() ?? null,
      message: paragraphs[0]?.textContent?.trim() ?? null,
      impact: paragraphs[1]?.textContent?.trim() ?? null,
      remediation: paragraphs[2]?.textContent?.trim() ?? null,
    };
  }));
}

function normalizeDomLimitation(raw, scope) {
  requireProjection(raw.visible, "DOM limitation is not visible");
  requireProjection(raw.visibleChannels, "DOM limitation visible channels are hidden");
  requireProjection([raw.kind, raw.severity, raw.message, raw.impact, raw.remediation].every((value) => typeof value === "string"), "DOM limitation has missing fields");
  requireProjection(raw.visibleKind === visibleText(raw.kind), `DOM limitation kind mirror differs: ${raw.visibleKind} != ${raw.kind}`);
  requireProjection(raw.visibleSeverity === expectedStatusText(raw.severity), `DOM limitation severity mirror differs: ${raw.visibleSeverity} != ${raw.severity}`);
  requireProjection(raw.impact.startsWith("影响："), "DOM limitation impact prefix is missing");
  requireProjection(raw.remediation.startsWith("补救："), "DOM limitation remediation prefix is missing");
  return {
    scope,
    kind: raw.kind,
    severity: raw.severity,
    message: raw.message,
    impact: raw.impact.slice("影响：".length),
    remediation: raw.remediation.slice("补救：".length),
    limitationId: raw.limitationId,
    projectIdHook: raw.projectIdHook,
  };
}

async function extractDomProjection(page) {
  await route(page, "#/v1/evidence");
  await page.evaluate(() => document.querySelectorAll("details").forEach((item) => { item.open = true; }));
  if (mutation === "visible-projection-field") {
    await page.locator(".claim-meta > span:last-child").first().evaluate((node) => { node.textContent = "mutated-visible-facet"; });
  }
  const rawClaims = await page.locator(".claim-item").evaluateAll((nodes) => nodes.map((node) => ({
    claim_id: node.dataset.claimId ?? null,
    facets: node.dataset.facets ?? null,
    visible_facets: [...node.querySelectorAll(".claim-meta > span")].slice(4).map((facet) => facet.textContent?.trim() ?? ""),
    visible_facets_shown: [...node.querySelectorAll(".claim-meta > span")].slice(4).every((facet) => facet instanceof HTMLElement && facet.getClientRects().length > 0),
    visible: node instanceof HTMLElement && node.getClientRects().length > 0,
    evidence_relations: [...node.querySelectorAll("[data-evidence-id]")].map((relation) => ({
      evidence_id: relation.dataset.evidenceId ?? null,
      relation: relation.dataset.relation ?? null,
      validity: relation.dataset.validity ?? null,
      commit_state: relation.dataset.commitState ?? null,
      supported_facets: relation.dataset.supportedFacets ?? null,
      visible_validity: relation.querySelector(".evidence-heading > .status-tag")?.textContent?.trim() ?? null,
      visible_relation: relation.querySelector(".evidence-heading")?.children[2]?.textContent?.trim() ?? null,
      visible_commit_state: relation.querySelector(".evidence-heading > .token-code")?.textContent?.trim() ?? null,
      visible_supported_facets: [...relation.querySelectorAll(".evidence-fields > dt")].find((field) => field.textContent === "Facets")?.nextElementSibling?.textContent?.trim() ?? null,
      visible_channels_shown: [...relation.querySelectorAll(".evidence-heading > .status-tag, .evidence-heading > span:nth-child(3), .evidence-heading > .token-code, .evidence-fields > dd")].every((channel) => channel instanceof HTMLElement && channel.getClientRects().length > 0),
      visible: relation instanceof HTMLElement && relation.getClientRects().length > 0,
    })),
  })));
  const claimsProjection = normalizeClaims(rawClaims.map((claim) => {
    requireProjection(claim.visible, "DOM Claim is not visible");
    requireProjection(claim.visible_facets_shown, `Claim ${claim.claim_id ?? "<missing>"} visible facets are hidden`);
    const facets = parseJsonStringArray(claim.facets, `Claim ${claim.claim_id ?? "<missing>"} facets`);
    requireProjection(JSON.stringify(claim.visible_facets) === JSON.stringify(facets.map(visibleText)), `Claim ${claim.claim_id ?? "<missing>"} visible facets differ from mirror`);
    return {
      claim_id: claim.claim_id,
      facets,
      evidence_relations: claim.evidence_relations.map((relation) => {
        requireProjection(relation.visible, `Claim ${claim.claim_id ?? "<missing>"} has a hidden Evidence relation`);
        requireProjection(relation.visible_channels_shown, `Evidence ${relation.evidence_id} visible channels are hidden`);
        const supportedFacets = parseJsonStringArray(relation.supported_facets, `Claim ${claim.claim_id ?? "<missing>"} Evidence ${relation.evidence_id ?? "<missing>"} supported facets`);
        requireProjection(relation.visible_validity === expectedStatusText(relation.validity), `Evidence ${relation.evidence_id} visible validity differs from mirror`);
        requireProjection(relation.visible_relation === visibleText(relation.relation), `Evidence ${relation.evidence_id} visible relation differs from mirror`);
        requireProjection(relation.visible_commit_state === visibleText(relation.commit_state), `Evidence ${relation.evidence_id} visible commit state differs from mirror`);
        requireProjection(relation.visible_supported_facets === visibleText(supportedFacets.join(", ")), `Evidence ${relation.evidence_id} visible facets differ from mirror`);
        return {
          evidence_id: relation.evidence_id,
          relation: relation.relation,
          validity: relation.validity,
          commit_state: relation.commit_state,
          supported_facets: supportedFacets,
        };
      }),
    };
  }));

  await route(page, "#/v1/overview");
  if (mutation === "limitation-id") {
    await page.locator(".degradation-item").evaluateAll((nodes) => {
      for (const node of nodes) node.dataset.limitationId = `mutated_${node.dataset.limitationId ?? ""}`;
    });
  }
  const overviewRaw = await visibleDomLimitations(page);
  const overviewLimitations = overviewRaw.map((limitation) => normalizeDomLimitation(limitation, null));
  await route(page, "#/v1/project");
  const projectRoutes = await page.locator('.project-item a[href^="#/v1/project/"]').evaluateAll((nodes) => (
    [...new Set(nodes.map((node) => node.getAttribute("href")).filter((href) => /^#\/v1\/project\/[^/?#]+$/u.test(href ?? "")))]
  ));
  requireProjection(projectRoutes.length > 0, "DOM project routes must not be empty");
  const projectLimitations = [];
  for (const projectRoute of projectRoutes) {
    await route(page, projectRoute);
    if (mutation === "limitation-id") {
      await page.locator(".degradation-item").evaluateAll((nodes) => {
        for (const node of nodes) node.dataset.limitationId = `mutated_${node.dataset.limitationId ?? ""}`;
      });
    }
    const projectId = decodeURIComponent(projectRoute.split("/")[3]);
    const raw = await visibleDomLimitations(page);
    projectLimitations.push(...raw.map((limitation) => normalizeDomLimitation(limitation, projectId)));
  }
  const limitationsProjection = reconcileLimitations(overviewLimitations, projectLimitations);
  const overviewIds = overviewLimitations.map((limitation) => limitation.limitationId);
  const overviewHooksOk = overviewLimitations.every((limitation) => {
    const projected = limitationsProjection.find((candidate) => (
      limitationContentKey(candidate) === limitationContentKey(limitation)
    ));
    return (
      projected !== undefined
      && limitation.projectIdHook === (projected.scope ?? "")
      && typeof limitation.limitationId === "string"
      && limitation.limitationId.length > 0
    );
  });
  const projectHooksOk = projectLimitations.every((limitation) => {
    const overview = overviewLimitations.find((candidate) => (
      limitationContentKey(candidate) === limitationContentKey(limitation)
    ));
    return (
      overview !== undefined
      && limitation.projectIdHook === limitation.scope
      && limitation.limitationId === overview.limitationId
    );
  });
  const limitationHooksOk = (
    overviewIds.every((value) => typeof value === "string" && value.length > 0)
    && new Set(overviewIds).size === overviewIds.length
    && overviewHooksOk
    && projectHooksOk
  );
  return {
    claims: claimsProjection,
    limitations: limitationsProjection.map(({ scope, kind, severity, message, impact, remediation }) => ({
      scope,
      kind,
      severity,
      message,
      impact,
      remediation,
    })),
    limitationHooks: overviewLimitations.map((limitation) => ({
      limitation_id: limitation.limitationId,
      kind: limitation.kind,
      severity: limitation.severity,
      project_id: limitation.projectIdHook,
    })).sort((left, right) => compareStrings(JSON.stringify(left), JSON.stringify(right))),
    limitationHooksOk,
  };
}

async function runEngine(engineName, engine) {
  const browser = await engine.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const completedPage = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const externalRequests = [];
  for (const observedPage of [page, completedPage]) {
    observedPage.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    observedPage.on("pageerror", (error) => pageErrors.push(error.message));
    observedPage.on("request", (request) => {
      if (!request.url().startsWith("file:")) externalRequests.push(request.url());
    });
  }

  try {
    await Promise.all([
      page.goto(pathToFileURL(outputFile).href, { waitUntil: "load" }),
      completedPage.goto(pathToFileURL(completedOutputFile).href, { waitUntil: "load" }),
    ]);
    await Promise.all([page.waitForSelector(".shell"), completedPage.waitForSelector(".shell")]);

    if (mutation === "snapshot-identity") {
      await page.locator(".forensic-strip").evaluate((node, expected) => {
        Object.assign(node.dataset, expected);
      }, {
        packageStatus: completedSnapshot.bundle.package_status,
        preparationRunId: completedSnapshot.bundle.preparation_run_id,
        bundleSha256: completedSnapshot.bundle.bundle_sha256,
      });
    }
    if (mutation === "visible-snapshot-identity") {
      await page.locator(".forensic-strip > span:nth-child(2)").evaluate((node) => { node.textContent = "run erased"; });
    }
    const [partialIdentity, completedIdentity] = await Promise.all(
      [page, completedPage].map((snapshotPage) => snapshotPage.locator(".forensic-strip").evaluate((node) => ({
        packageStatus: node.dataset.packageStatus ?? null,
        preparationRunId: node.dataset.preparationRunId ?? null,
        bundleSha256: node.dataset.bundleSha256 ?? null,
        visibleChannels: [...node.children].slice(0, 3).every((channel) => channel instanceof HTMLElement && channel.getClientRects().length > 0),
        statusText: node.querySelector(":scope > .status-tag")?.textContent?.trim() ?? null,
        runText: node.children[1]?.textContent?.trim() ?? null,
        shaText: node.children[2]?.textContent?.trim() ?? null,
        roleName: document.querySelector(".role-title")?.textContent ?? null,
        roleMeta: document.querySelector(".role-meta")?.textContent ?? null,
        assumptions: document.querySelector(".role-assumptions")?.textContent ?? null,
      }))),
    );
    const expectedPartialIdentity = {
      packageStatus: partialSnapshot.bundle.package_status,
      preparationRunId: partialSnapshot.bundle.preparation_run_id,
      bundleSha256: partialSnapshot.bundle.bundle_sha256,
    };
    const expectedCompletedIdentity = {
      packageStatus: completedSnapshot.bundle.package_status,
      preparationRunId: completedSnapshot.bundle.preparation_run_id,
      bundleSha256: completedSnapshot.bundle.bundle_sha256,
    };
    const identityMatches = (actual, expected) => (
      actual.packageStatus === expected.packageStatus
      && actual.visibleChannels
      && actual.preparationRunId === expected.preparationRunId
      && actual.bundleSha256 === expected.bundleSha256
      && actual.statusText === expectedStatusText(expected.packageStatus)
      && actual.runText === visibleText(`run ${expected.preparationRunId.slice(0, 12)}`)
      && actual.shaText === `sha ${expected.bundleSha256.slice(0, 14)}`
    );
    record(engineName, "dash10-completed-snapshot-identity", identityMatches(completedIdentity, expectedCompletedIdentity), { actual: completedIdentity, expected: expectedCompletedIdentity });
    record(engineName, "dash10-partial-snapshot-identity", identityMatches(partialIdentity, expectedPartialIdentity), { actual: partialIdentity, expected: expectedPartialIdentity });
    const sameRole = (
      JSON.stringify(partialSnapshot.bundle.role) === JSON.stringify(completedSnapshot.bundle.role)
      && JSON.stringify(partialSnapshot.bundle.role_lens) === JSON.stringify(completedSnapshot.bundle.role_lens)
      && partialIdentity.roleName === completedIdentity.roleName
      && partialIdentity.roleMeta === completedIdentity.roleMeta
      && partialIdentity.assumptions === completedIdentity.assumptions
    );
    const distinctSnapshots = (
      partialIdentity.packageStatus !== completedIdentity.packageStatus
      && partialIdentity.preparationRunId !== completedIdentity.preparationRunId
      && partialIdentity.bundleSha256 !== completedIdentity.bundleSha256
    );
    record(engineName, "dash10-same-role-distinct-snapshots", sameRole && distinctSnapshots, { sameRole, distinctSnapshots, partial: partialIdentity, completed: completedIdentity });

    const hostileLimitationHooks = await page.evaluate((expected) => {
      const limitation = [...document.querySelectorAll(".degradation-item")].find((node) => (
        node.dataset.limitationId?.includes(expected)
      ));
      return {
        limitationIdPreserved: limitation !== undefined,
        inertTextVisible: limitation?.textContent?.includes("<script> javascript:") === true,
        controlMadeVisible: limitation?.textContent?.includes("[U+202E]") === true,
        noInjectedScriptElement: limitation?.querySelector("script") === null,
      };
    }, hostileLimitationId);
    await route(page, "#/v1/evidence");
    const hostileEvidenceHooks = await page.evaluate((expected) => ({
      claimFacetsPreserved: [...document.querySelectorAll(".claim-item")].some((node) => {
        try { return JSON.parse(node.dataset.facets ?? "[]").includes(expected); } catch { return false; }
      }),
      relationFacetsPreserved: [...document.querySelectorAll("[data-evidence-id]")].some((node) => {
        try { return JSON.parse(node.dataset.supportedFacets ?? "[]").includes(expected); } catch { return false; }
      }),
    }), hostileValue);
    await route(page, "#/v1/overview");
    const hostileHooks = { ...hostileLimitationHooks, ...hostileEvidenceHooks };
    const hostileValuesInert = (
      completedIdentity.preparationRunId === completedSnapshot.bundle.preparation_run_id
      && Object.values(hostileHooks).every(Boolean)
      && consoleErrors.length === 0
      && pageErrors.length === 0
      && externalRequests.length === 0
    );
    record(engineName, "semantic-hooks-inert-untrusted-values", hostileValuesInert, {
      hooks: hostileHooks,
      completedPreparationRunIdPreserved: completedIdentity.preparationRunId === completedSnapshot.bundle.preparation_run_id,
      consoleErrorCount: consoleErrors.length,
      pageErrorCount: pageErrors.length,
      externalRequestCount: externalRequests.length,
    });

    const rejectedRoutes = ["#/v0/overview", "#/v2/overview", "#/v9/overview", "#/%E9%9D%9E%E7%89%88%E6%9C%AC/overview"];
    const rejectionCases = [];
    for (const rejectedHash of rejectedRoutes) {
      await route(page, rejectedHash);
      if (mutation === "cross-version-fallback") await route(page, "#/v1/overview");
      rejectionCases.push(await page.evaluate((expectedHash) => {
        const error = document.querySelector('main .route-error[data-error-kind="contract-version-mismatch"]');
        const forbidden = document.querySelectorAll("main .view, main .view-title, main .claim-item");
        return {
          expectedHash,
          actualHash: location.hash,
          errorVisible: error instanceof HTMLElement && error.getClientRects().length > 0,
          message: error?.textContent ?? null,
          forbiddenCount: forbidden.length,
        };
      }, rejectedHash));
    }
    record(engineName, "dash10-cross-version-deep-link-rejected", rejectionCases.every((entry) => (
      entry.errorVisible
      && entry.message?.includes("该链接属于其他契约版本")
      && entry.actualHash === entry.expectedHash
    )), { cases: rejectionCases });
    record(engineName, "dash10-cross-version-no-wrong-object", rejectionCases.every((entry) => entry.forbiddenCount === 0), { cases: rejectionCases });

    await route(page, "#/v1/not-a-real-view");
    const sameVersionUnknown = await page.evaluate(() => {
      const error = document.querySelector("main .route-error");
      return {
        hash: location.hash,
        visible: error instanceof HTMLElement && error.getClientRects().length > 0,
        errorKind: error?.getAttribute("data-error-kind"),
        message: error?.textContent ?? null,
        forbiddenCount: document.querySelectorAll("main .view, main .view-title, main .claim-item").length,
      };
    });
    record(
      engineName,
      "same-version-unknown-deep-link-not-version-mismatch",
      (
        sameVersionUnknown.visible
        && sameVersionUnknown.hash === "#/v1/not-a-real-view"
        && sameVersionUnknown.errorKind === null
        && sameVersionUnknown.message?.includes("该深链在当前快照中不存在")
        && sameVersionUnknown.forbiddenCount === 0
      ),
      sameVersionUnknown,
    );

    const markdownProjection = await captureProjection(() => parseMarkdownProjection(reportMarkdown));
    const domProjection = await captureProjection(() => extractDomProjection(page));
    const claimDifference = markdownProjection.ok && domProjection.ok
      ? firstDifference(markdownProjection.value.claims, domProjection.value.claims)
      : { markdownError: markdownProjection.error, domError: domProjection.error };
    const markdownClaimCoverage = markdownProjection.ok
      ? claimProjectionCoverage(markdownProjection.value.claims)
      : { ok: false };
    const domClaimCoverage = domProjection.ok
      ? claimProjectionCoverage(domProjection.value.claims)
      : { ok: false };
    record(
      engineName,
      "dash12-claim-evidence-parity",
      claimDifference === null && markdownClaimCoverage.ok && domClaimCoverage.ok,
      {
      firstDifference: claimDifference,
        markdownCoverage: markdownClaimCoverage,
        domCoverage: domClaimCoverage,
      },
    );
    const limitationDifference = markdownProjection.ok && domProjection.ok
      ? firstDifference(markdownProjection.value.limitations, domProjection.value.limitations)
      : { markdownError: markdownProjection.error, domError: domProjection.error };
    const markdownLimitationCoverage = markdownProjection.ok
      ? limitationProjectionCoverage(markdownProjection.value.limitations)
      : { ok: false };
    const domLimitationCoverage = domProjection.ok
      ? limitationProjectionCoverage(domProjection.value.limitations)
      : { ok: false };
    const expectedLimitationHooks = partialSnapshot.bundle.coverage.limitations.map((limitation) => ({
      limitation_id: limitation.limitation_id,
      kind: limitation.kind,
      severity: limitation.severity,
      project_id: limitation.project_id ?? "",
    })).sort((left, right) => compareStrings(JSON.stringify(left), JSON.stringify(right)));
    const limitationHookDifference = domProjection.ok
      ? firstDifference(expectedLimitationHooks, domProjection.value.limitationHooks)
      : { domError: domProjection.error };
    record(
      engineName,
      "dash12-limitation-parity",
      (
        limitationDifference === null
        && domProjection.value?.limitationHooksOk === true
        && limitationHookDifference === null
        && markdownLimitationCoverage.ok
        && domLimitationCoverage.ok
      ),
      {
        firstDifference: limitationDifference,
        limitationHooksOk: domProjection.value?.limitationHooksOk ?? false,
        limitationHookDifference,
        markdownCoverage: markdownLimitationCoverage,
        domCoverage: domLimitationCoverage,
      },
    );
    const markdownConclusions = markdownProjection.ok ? {
      claims: markdownProjection.value.claims.map((claim) => ({
        claim_id: claim.claim_id,
        evidence: claim.evidence_relations.map((relation) => `${relation.relation}:${relation.evidence_id}`),
      })),
      limitations: markdownProjection.value.limitations.map(limitationKey),
    } : null;
    const domConclusions = domProjection.ok ? {
      claims: domProjection.value.claims.map((claim) => ({
        claim_id: claim.claim_id,
        evidence: claim.evidence_relations.map((relation) => `${relation.relation}:${relation.evidence_id}`),
      })),
      limitations: domProjection.value.limitations.map(limitationKey),
    } : null;
    const conclusionDifference = markdownConclusions && domConclusions
      ? firstDifference(markdownConclusions, domConclusions)
      : { markdownError: markdownProjection.error, domError: domProjection.error };
    record(engineName, "dash12-no-html-only-conclusions", conclusionDifference === null, {
      firstDifference: conclusionDifference,
      markdown: markdownConclusions,
      dom: domConclusions,
    });

    await route(page, "#/v1/overview");
    if (mutation === "role-assumption") {
      await page.locator(".role-assumptions li").first().evaluate((node) => node.remove());
    }
    const assumptionVisible = await page.locator(".role-assumptions li", { hasText: "VERIFY_ROLE_ASSUMPTION" }).isVisible().catch(() => false);
    record(engineName, "role-lens-assumption-visible", assumptionVisible);

    const scopeLink = page.locator(".scope-link").first();
    let originalScopeHref = null;
    if (mutation === "scope-focusable") {
      originalScopeHref = await scopeLink.getAttribute("href");
      await scopeLink.evaluate((node) => node.removeAttribute("href"));
    }
    if (mutation === "scope-activation") {
      await scopeLink.evaluate((node) => node.setAttribute("href", "#/v1/gaps"));
    }
    if (mutation === "scope-navigation") {
      await scopeLink.evaluate((node) => document.querySelector("nav")?.append(node));
    }
    await scopeLink.focus();
    record(engineName, "coverage-scope-link-focusable", await scopeLink.evaluate((node) => node === document.activeElement && node instanceof HTMLAnchorElement));
    if (originalScopeHref !== null) {
      await scopeLink.evaluate((node, href) => node.setAttribute("href", href), originalScopeHref);
    }
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
    record(engineName, "forced-colors-media-active", dispositionGroup.mediaActive, dispositionGroup);
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

const expectedEngines = mutation === null ? ["chromium", "webkit"] : ["chromium"];
const cardinalityFailures = expectedEngines.flatMap((engine) => requiredContractAssertions.flatMap((assertion) => {
  const count = checks.filter((check) => check.engine === engine && check.assertion === assertion).length;
  return count === 1 ? [] : [{
    engine,
    width: null,
    view: null,
    assertion: "required-assertion-cardinality",
    ok: false,
    details: { requiredAssertion: assertion, expected: 1, actual: count },
  }];
}));
const allChecks = [...checks, ...cardinalityFailures];
const failures = allChecks.filter((check) => !check.ok);
const result = {
  contract: "goodjob-browser-verification-v1",
  artifact: outputFile,
  renderer: "goodjob.reporting.render_dashboard_html",
  mutation,
  summary: { passed: allChecks.length - failures.length, failed: failures.length, total: allChecks.length },
  failures,
};
console.log(JSON.stringify(result, null, 2));
process.exitCode = failures.length === 0 ? 0 : 1;

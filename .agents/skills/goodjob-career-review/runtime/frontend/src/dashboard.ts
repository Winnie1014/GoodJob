import {
  claimMatchesFilters,
  displayText,
  formatBps,
  parseRoute,
  projectScanLimitations,
  searchEntries,
  statusLabel,
  statusSymbol,
  tokensText,
  type InlineToken,
  type RouteState,
  type SearchEntry,
} from "./model";

interface Dimension {
  key: string;
  display_name: string;
  weight_bps: number;
  evaluation_criteria: string;
  required_evidence_kinds: string[];
}

interface RoleLens {
  role_lens_id: string;
  dimensions: Dimension[];
  assumptions: string[];
}

interface Role {
  name: string;
  applied_level: string | null;
  level_source: string;
  jd: {
    input_kind: string;
    content_sha256: string | null;
    has_jd: boolean;
  };
}

interface Assessment {
  rank: number;
  dimension_scores_milli: Record<string, number>;
  coverage_bps: number;
  base_score_milli: number;
  final_score_milli: number;
  rationale_tokens: InlineToken[];
}

interface ModuleItem {
  module_id: string;
  project_id: string;
  name: string;
  kind: string;
  relative_root: string;
  adapter_id: string;
}

interface ProjectItem {
  project_id: string;
  display_name: string;
  workspace_relative_location: string | null;
  project_snapshot_id: string | null;
  snapshot_disposition: string;
  coverage_status: string | null;
  eligible: boolean;
  assessment: Assessment | null;
  modules: ModuleItem[];
  claim_ids: string[];
  gap_ids: string[];
}

interface EvidenceRelation {
  evidence_id: string;
  relation: string;
  supported_facets: string[];
}

interface ClaimItem {
  claim_id: string;
  claim_revision_id: string;
  rank: number;
  section: string;
  category: string;
  scope_kind: string;
  project_id: string;
  worktree_id: string | null;
  module_id: string | null;
  statement_tokens: InlineToken[];
  facets: string[];
  support_level: string;
  personal_attribution: string;
  evidence_relations: EvidenceRelation[];
}

interface WorktreeProjection {
  worktree_id: string;
  branch: string | null;
  head_commit: string | null;
  dirty_state: string;
  observed_scan_run_id: string;
}

interface EvidenceItem {
  evidence_id: string;
  project_id: string;
  module_id: string | null;
  worktree: WorktreeProjection | null;
  origin_kind: string;
  evidence_kind: string;
  relative_path: string | null;
  locator: Record<string, unknown>;
  summary_tokens: InlineToken[];
  commit_state: string;
  validity: string;
  content_sha256: string | null;
  content_equivalence_key: string | null;
}

interface GapItem {
  gap_id: string;
  gap_key: string;
  scope_kind: string;
  project_id: string | null;
  module_id: string | null;
  dimension: string;
  description_tokens: InlineToken[];
  severity: string;
  resolution_kind: string;
  status: string;
}

interface Limitation {
  limitation_id: string;
  kind: string;
  severity: string;
  project_id: string | null;
  message_tokens: InlineToken[];
  impact_tokens: InlineToken[];
  remediation_tokens: InlineToken[];
  filter_route: string;
}

interface InterviewQuestion {
  question_id: string;
  level: string;
  project_id: string | null;
  module_id: string | null;
  claim_id?: string;
  gap_id?: string;
  prompt_tokens: InlineToken[];
  follow_up_tokens: InlineToken[];
}

interface ReviewBinding {
  review_target_binding_id: string;
  review_target_id?: string;
  continuity_status: string;
  summary: string | null;
  mastery_level: string | null;
  weak_points: string[];
  next_review_at: string | null;
  reviewed_at: string | null;
  historical_review: HistoricalReview | null;
}

interface HistoricalReview {
  summary: string;
  mastery_level: string;
  weak_points: string[];
  next_review_at: string | null;
  reviewed_at: string;
}

interface ReportBundle {
  contract_version: string;
  bundle_sha256: string;
  preparation_run_id: string;
  generated_at: string;
  primary_language: string;
  package_status: string;
  role: Role;
  role_lens: RoleLens;
  coverage: {
    projects_total: number;
    eligible_projects: number;
    disposition_counts: Record<string, number>;
    excluded_by_category: Record<string, number>;
    excluded_by_category_available: boolean;
    limitations: Limitation[];
  };
  projects: ProjectItem[];
  claims: ClaimItem[];
  evidence: EvidenceItem[];
  knowledge_gaps: GapItem[];
  review: {
    cutoff_at: string;
    status: string;
    bindings: ReviewBinding[];
    skill_invocation: string;
  };
  interview: {
    two_minute_pitch_claim_ids: string[];
    questions: InterviewQuestion[];
  };
  search_index: {
    index_version: string;
    entries: SearchEntry[];
  };
}

const NAV_ITEMS = [
  ["overview", "总览", "#/v1/overview"],
  ["project", "项目与模块", "#/v1/project"],
  ["evidence", "证据横切", "#/v1/evidence"],
  ["gaps", "知识缺口", "#/v1/gaps"],
  ["interview", "面试与复习", "#/v1/interview"],
] as const;

const MASTERY_LABELS: Record<string, string> = {
  unfamiliar: "不熟悉",
  developing: "正在掌握",
  solid: "较扎实",
  mastered: "已掌握",
};

function masteryLabel(value: string | null): string {
  return value === null ? "未评估" : (MASTERY_LABELS[value] ?? value);
}

const ICON_PATHS: Record<string, string[]> = {
  overview: ["M3 3h7v7H3z", "M14 3h7v4h-7z", "M14 11h7v10h-7z", "M3 14h7v7H3z"],
  project: ["M3 6h18", "M3 12h18", "M3 18h18", "M7 3v6", "M17 9v6", "M7 15v6"],
  evidence: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "m9 15 2 2 4-4"],
  gaps: ["M12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z", "M12 8v5", "M12 17h.01"],
  interview: ["M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z", "M8 9h8", "M8 13h5"],
};

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const value = document.createElement(tag);
  if (className) {
    value.className = className;
  }
  if (text !== undefined) {
    value.textContent = displayText(text);
  }
  return value;
}

function icon(name: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("nav-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const pathData of ICON_PATHS[name] ?? ICON_PATHS.overview ?? []) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  return svg;
}

function ratioBar(value: number, maximum: number, className: string, label: string): SVGSVGElement {
  const safeMaximum = Math.max(1, Math.trunc(maximum));
  const safeValue = Math.max(0, Math.min(safeMaximum, Math.trunc(value)));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("bar-track");
  svg.setAttribute("viewBox", `0 0 ${safeMaximum} 1`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label);
  const fill = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  fill.classList.add("bar-fill", className);
  fill.setAttribute("x", "0");
  fill.setAttribute("y", "0");
  fill.setAttribute("width", String(safeValue));
  fill.setAttribute("height", "1");
  svg.append(fill);
  return svg;
}

function appendTokens(parent: HTMLElement, tokens: readonly InlineToken[]): void {
  for (const token of tokens) {
    let node: HTMLElement | Text;
    if (token.kind === "code") {
      node = element("code", "token-code", token.value);
    } else if (token.kind === "emphasis") {
      node = element("strong", "token-emphasis", token.value);
    } else if (["claim_ref", "evidence_ref", "gap_ref"].includes(token.kind)) {
      node = element("span", "token-reference", token.value);
      node.title = displayText(`${token.kind}: ${token.ref_id ?? ""}`);
    } else if (token.kind === "inert_url") {
      node = element("span", "token-inert-url", `${token.value}（未激活）`);
    } else {
      node = document.createTextNode(displayText(token.value));
    }
    parent.append(node);
  }
}

function mirrorLimitationData(item: HTMLElement, limitation: Limitation): void {
  item.dataset.limitationId = limitation.limitation_id;
  item.dataset.kind = limitation.kind;
  item.dataset.severity = limitation.severity;
  item.dataset.projectId = limitation.project_id ?? "";
}

function statusTag(value: string, label = statusLabel(value)): HTMLElement {
  const tag = element("span", "status-tag");
  tag.dataset.status = value;
  tag.append(element("span", undefined, statusSymbol(value)), element("span", undefined, label));
  return tag;
}

function copyValue(text: string, label: string, status: HTMLElement): HTMLElement {
  const wrapper = element("span", "copy-pair");
  const control = element("button", "copy-value", text);
  control.type = "button";
  control.setAttribute("aria-label", label);
  control.title = label;
  control.addEventListener("click", async () => {
    try {
      if (!navigator.clipboard) {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      status.textContent = "已复制。";
    } catch {
      status.textContent = "剪贴板不可用，请手动选中上方文本。";
    }
  });
  wrapper.append(control, element("span", "print-value", text));
  return wrapper;
}

function readBundle(): ReportBundle {
  const source = document.getElementById("report-data");
  if (!source || source.textContent === null) {
    throw new Error("冻结报告数据不存在");
  }
  const parsed: unknown = JSON.parse(source.textContent);
  if (!parsed || typeof parsed !== "object") {
    throw new Error("冻结报告数据不是对象");
  }
  const bundle = parsed as ReportBundle;
  if (bundle.contract_version !== "report-bundle-v1") {
    throw new Error("不支持的 ReportBundle 契约版本");
  }
  if (!Array.isArray(bundle.projects) || !Array.isArray(bundle.claims) || !Array.isArray(bundle.evidence)) {
    throw new Error("冻结报告数据缺少必需集合");
  }
  return bundle;
}

class Dashboard {
  private readonly app: HTMLElement;
  private readonly bundle: ReportBundle;
  private readonly projects = new Map<string, ProjectItem>();
  private readonly claims = new Map<string, ClaimItem>();
  private readonly evidence = new Map<string, EvidenceItem>();
  private readonly gaps = new Map<string, GapItem>();
  private printState = new Map<HTMLDetailsElement, boolean>();
  private pendingSearchFocus = false;

  constructor(app: HTMLElement, bundle: ReportBundle) {
    this.app = app;
    this.bundle = bundle;
    for (const project of bundle.projects) this.projects.set(project.project_id, project);
    for (const claim of bundle.claims) this.claims.set(claim.claim_id, claim);
    for (const item of bundle.evidence) this.evidence.set(item.evidence_id, item);
    for (const gap of bundle.knowledge_gaps) this.gaps.set(gap.gap_id, gap);
  }

  mount(): void {
    if (!location.hash) {
      location.hash = "/v1/overview";
    }
    this.render();
    window.addEventListener("hashchange", () => this.render());
    document.addEventListener("keydown", (event) => this.handleKeyboard(event));
    window.addEventListener("beforeprint", () => this.expandForPrint());
    window.addEventListener("afterprint", () => this.restoreAfterPrint());
  }

  private render(): void {
    const route = parseRoute(location.hash);
    const shell = element("div", "shell");
    shell.append(this.renderSideNav(route), this.renderContent(route));
    this.app.replaceChildren(shell);
    if (this.pendingSearchFocus) {
      const search = document.getElementById("report-search") as HTMLInputElement | null;
      if (search) {
        this.pendingSearchFocus = false;
        search.focus();
      }
    }
  }

  private renderSideNav(route: RouteState): HTMLElement {
    const nav = element("nav", "side-nav");
    nav.setAttribute("aria-label", "主要视图");
    nav.append(element("p", "wordmark", "GoodJob"));
    const list = element("ul", "nav-list");
    for (const [view, label, href] of NAV_ITEMS) {
      const item = element("li");
      const link = element("a", "nav-link") as HTMLAnchorElement;
      link.href = href;
      if (route.view === view) link.setAttribute("aria-current", "page");
      link.append(icon(view), document.createTextNode(label));
      item.append(link);
      list.append(item);
    }
    nav.append(list);
    return nav;
  }

  private renderMobileNav(route: RouteState): HTMLElement {
    const wrapper = element("div", "mobile-nav");
    wrapper.append(element("p", "wordmark", "GoodJob"));
    const select = element("select", "mobile-nav-select");
    select.setAttribute("aria-label", "切换视图");
    for (const [view, label, href] of NAV_ITEMS) {
      const option = element("option", undefined, label);
      option.value = href;
      option.selected = route.view === view;
      select.append(option);
    }
    select.addEventListener("change", () => {
      location.hash = select.value.slice(1);
    });
    wrapper.append(select);
    return wrapper;
  }

  private renderContent(route: RouteState): HTMLElement {
    const content = element("div", "content");
    content.append(this.renderMobileNav(route), this.renderForensicStrip(), this.renderRoleHeader(), this.renderCoverage());
    const main = element("main");
    main.id = "main";
    if (route.version !== "v1") {
      main.append(
        this.renderRouteError(
          "该链接属于其他契约版本，当前快照只支持 v1。",
          "contract-version-mismatch",
        ),
      );
    } else if (route.view === "overview") {
      main.append(this.renderOverview());
    } else if (route.view === "project") {
      main.append(this.renderProjects(route));
    } else if (route.view === "evidence") {
      main.append(this.renderEvidenceView(route));
    } else if (route.view === "gaps") {
      main.append(this.renderGaps(route));
    } else if (route.view === "interview") {
      main.append(this.renderInterview(route));
    } else {
      main.append(this.renderRouteError("该深链在当前快照中不存在。"));
    }
    content.append(main, this.renderFooter());
    return content;
  }

  private renderForensicStrip(): HTMLElement {
    const strip = element("div", "forensic-strip");
    strip.setAttribute("aria-label", "冻结快照身份");
    strip.dataset.packageStatus = this.bundle.package_status;
    strip.dataset.preparationRunId = this.bundle.preparation_run_id;
    strip.dataset.bundleSha256 = this.bundle.bundle_sha256;
    strip.append(
      statusTag(this.bundle.package_status),
      element("span", undefined, `run ${this.bundle.preparation_run_id.slice(0, 12)}`),
      element("span", undefined, `sha ${this.bundle.bundle_sha256.slice(0, 14)}`),
      element("span", undefined, this.bundle.generated_at),
      element("span", undefined, this.bundle.primary_language),
      element("span", undefined, "已冻结 · 只读"),
    );
    return strip;
  }

  private renderRoleHeader(): HTMLElement {
    const header = element("header", "role-header");
    header.append(element("p", "eyebrow", "ROLE DOSSIER / 岗位卷宗"));
    const title = element("h1", "role-title", this.bundle.role.name);
    const meta = element("p", "role-meta");
    meta.append(
      element("span", undefined, `职级：${this.bundle.role.applied_level ?? "未指定"}`),
      element("span", undefined, `职级来源：${this.bundle.role.level_source}`),
      element(
        "span",
        undefined,
        this.bundle.role.jd.has_jd
          ? `JD：${this.bundle.role.jd.input_kind} · ${this.bundle.role.jd.content_sha256?.slice(0, 12) ?? ""}`
          : "JD：未提供，使用冻结假设",
      ),
    );
    header.append(title, meta);
    const assumptions = element("div", "role-assumptions");
    assumptions.append(element("p", "section-kicker", "冻结岗位假设"));
    const assumptionList = element("ul");
    for (const assumption of this.bundle.role_lens.assumptions) {
      assumptionList.append(element("li", undefined, assumption));
    }
    if (!this.bundle.role_lens.assumptions.length) {
      assumptionList.append(element("li", undefined, "本次 RoleLens 未记录额外假设。"));
    }
    assumptions.append(assumptionList);
    header.append(assumptions);
    return header;
  }

  private renderCoverage(): HTMLElement {
    const block = element("section", "coverage-block");
    block.setAttribute("aria-labelledby", "coverage-title");
    const kicker = element("h2", "section-kicker", "L2 / WORKSPACE COVERAGE");
    kicker.id = "coverage-title";
    block.append(
      kicker,
      element(
        "p",
        "coverage-summary",
        `共 ${this.bundle.coverage.projects_total} 个项目里有 ${this.bundle.coverage.eligible_projects} 个参与评分`,
      ),
    );
    const order = ["fresh", "carried_forward", "failed_no_baseline", "excluded"] as const;
    const band = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    band.classList.add("coverage-band");
    const total = Math.max(
      1,
      order.reduce(
        (sum, disposition) => sum + (this.bundle.coverage.disposition_counts[disposition] ?? 0),
        0,
      ),
    );
    band.setAttribute("viewBox", `0 0 ${total} 1`);
    band.setAttribute("preserveAspectRatio", "none");
    band.setAttribute("role", "img");
    band.setAttribute("aria-label", "项目快照 disposition 构成");
    let offset = 0;
    for (const disposition of order) {
      const count = this.bundle.coverage.disposition_counts[disposition] ?? 0;
      if (count === 0) continue;
      const segment = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      segment.classList.add("coverage-segment");
      segment.dataset.status = disposition;
      segment.setAttribute("x", String(offset));
      segment.setAttribute("y", "0");
      segment.setAttribute("width", String(count));
      segment.setAttribute("height", "1");
      band.append(segment);
      offset += count;
    }
    const legend = element("ul", "coverage-legend");
    for (const disposition of order) {
      const count = this.bundle.coverage.disposition_counts[disposition] ?? 0;
      const item = element("li");
      item.append(statusTag(disposition, `${statusLabel(disposition)} ${count}`));
      legend.append(item);
    }
    block.append(band, legend);
    const exclusions = element("div", "coverage-exclusions");
    exclusions.append(element("p", "section-kicker", "NORMAL EXCLUSIONS / 扫描排除统计"));
    if (this.bundle.coverage.excluded_by_category_available) {
      const exclusionList = element("ul", "coverage-legend");
      const entries = Object.entries(this.bundle.coverage.excluded_by_category).filter(
        ([, count]) => count > 0,
      );
      for (const [kind, count] of entries) {
        exclusionList.append(element("li", undefined, `${kind} ${count}`));
      }
      if (!entries.length) exclusionList.append(element("li", undefined, "无"));
      exclusions.append(exclusionList);
    } else {
      exclusions.append(element("p", "remediation", "该旧扫描快照未记录分类计数。"));
    }
    block.append(exclusions);
    return block;
  }

  private renderOverview(): HTMLElement {
    const view = element("div", "view");
    view.append(element("h2", "view-title", "总览"));
    const degradation = element("section", "lens-section");
    degradation.append(element("h3", "section-title", "覆盖限制"));
    const limitations = this.bundle.coverage.limitations;
    if (!limitations.length) {
      degradation.append(element("p", "empty-state", "本次未记录覆盖限制。"));
    } else {
      const list = element("ul", "degradation-list");
      for (const limitation of limitations) {
        const item = element("li", "degradation-item");
        mirrorLimitationData(item, limitation);
        const label = element("div", "hanging-label");
        label.append(statusTag(limitation.severity), element("span", undefined, limitation.kind));
        const copy = element("div", "degradation-copy");
        const message = element("p");
        appendTokens(message, limitation.message_tokens);
        const impact = element("p", "remediation");
        impact.append(document.createTextNode("影响："));
        appendTokens(impact, limitation.impact_tokens);
        const remedy = element("p", "remediation");
        remedy.append(document.createTextNode("补救："));
        appendTokens(remedy, limitation.remediation_tokens);
        const route = element("a", "scope-link", "查看受影响范围") as HTMLAnchorElement;
        route.href = limitation.filter_route;
        copy.append(message, impact, remedy, route);
        item.append(label, copy);
        list.append(item);
      }
      degradation.append(list);
    }
    view.append(degradation, this.renderLens(), this.renderRanking());
    return view;
  }

  private renderLens(): HTMLElement {
    const section = element("section", "lens-section");
    section.append(element("h3", "section-title", "岗位镜头权重"));
    for (const dimension of this.bundle.role_lens.dimensions) {
      const row = element("div", "bar-row");
      const name = element("span", undefined, dimension.display_name);
      name.title = displayText(dimension.evaluation_criteria);
      const track = ratioBar(
        dimension.weight_bps,
        10_000,
        "weight",
        `${dimension.display_name} 权重 ${formatBps(dimension.weight_bps)}`,
      );
      const value = element("span", "numeric", `${formatBps(dimension.weight_bps)} · ${dimension.weight_bps} bps`);
      row.append(name, track, value);
      section.append(row);
    }
    return section;
  }

  private renderRanking(): HTMLElement {
    const section = element("section", "rank-section");
    section.append(element("h3", "section-title", "项目排序"));
    const list = element("ol", "rank-list");
    const eligible = this.bundle.projects.filter((project) => project.assessment !== null);
    for (const project of eligible) {
      const assessment = project.assessment;
      if (!assessment) continue;
      const item = element("li", "rank-item");
      item.append(element("span", "rank-number", String(assessment.rank)));
      const body = element("div");
      const heading = element("div", "rank-heading");
      const link = element("a") as HTMLAnchorElement;
      link.href = `#/v1/project/${encodeURIComponent(project.project_id)}`;
      link.append(element("h3", undefined, project.display_name));
      heading.append(link, statusTag(project.snapshot_disposition));
      const track = ratioBar(
        assessment.final_score_milli,
        1_000,
        "score",
        `${project.display_name} 最终得分 ${assessment.final_score_milli}`,
      );
      const equation = element(
        "p",
        "score-equation",
        `${assessment.base_score_milli} × ${formatBps(assessment.coverage_bps)} → ${assessment.final_score_milli}`,
      );
      body.append(heading, track, equation);
      item.append(body);
      list.append(item);
    }
    section.append(list);
    const ineligible = this.bundle.projects.filter((project) => project.assessment === null);
    if (ineligible.length) {
      const note = element("p", "section-kicker", "仅进入覆盖，不评分");
      section.append(note);
      for (const project of ineligible) {
        const row = element("div", "rank-heading");
        row.append(element("span", undefined, project.display_name), statusTag(project.snapshot_disposition));
        section.append(row);
      }
    }
    return section;
  }

  private renderProjects(route: RouteState): HTMLElement {
    const view = element("div", "view");
    const selected = route.projectId ? this.projects.get(route.projectId) : undefined;
    view.append(
      element("h2", "view-title", selected ? selected.display_name : "项目与模块"),
      element(
        "p",
        "lede",
        selected ? "冻结项目快照、模块边界与关联 Claim。" : "按岗位排序查看所有项目及其冻结模块边界。",
      ),
    );
    const list = element("ul", "project-list");
    for (const project of selected ? [selected] : this.bundle.projects) {
      const item = element("li", "project-item focus-item");
      item.tabIndex = 0;
      const heading = element("div", "rank-heading");
      const link = element("a") as HTMLAnchorElement;
      link.href = `#/v1/project/${encodeURIComponent(project.project_id)}`;
      link.append(element("h3", undefined, project.display_name));
      heading.append(link, statusTag(project.snapshot_disposition));
      item.append(heading);
      if (project.assessment) {
        const rationale = element("p", "lede");
        appendTokens(rationale, project.assessment.rationale_tokens);
        item.append(rationale);
      } else {
        item.append(element("p", "lede", "当前项目没有可评分基线。"));
      }
      if (project.modules.length) {
        const modules = element("ul", "module-list");
        for (const module of project.modules) {
          const moduleItem = element("li", "module-item");
          const moduleLink = element("a", undefined, module.name) as HTMLAnchorElement;
          moduleLink.href = `#/v1/project/${encodeURIComponent(project.project_id)}/module/${encodeURIComponent(module.module_id)}`;
          moduleItem.append(
            moduleLink,
            element("span", "module-path", `${module.relative_root} · ${module.kind} · ${module.adapter_id}`),
          );
          modules.append(moduleItem);
        }
        item.append(modules);
      } else {
        item.append(element("p", "remediation", "本次未识别独立模块边界，材料保持项目级作用域。"));
      }
      if (selected) {
        const scanLimitations = projectScanLimitations(
          this.bundle.coverage.limitations,
          project.project_id,
        );
        if (scanLimitations.length) {
          item.append(this.renderProjectScanLimitations(scanLimitations));
        }
        const claims = this.bundle.claims.filter(
          (claim) => claim.project_id === project.project_id && (!route.moduleId || claim.module_id === route.moduleId),
        );
        const learning = claims.filter((claim) => claim.category === "learning");
        const candidateLearning = learning.length
          ? []
          : claims.filter((claim) => claim.personal_attribution === "capability");
        const implementation = claims.filter(
          (claim) => claim.category === "implementation_method",
        );
        const primaryIds = new Set(
          [...learning, ...candidateLearning, ...implementation].map((claim) => claim.claim_id),
        );
        item.append(
          this.renderProjectClaimSection(
            "学习要点",
            learning.length ? learning : candidateLearning,
            learning.length
              ? undefined
              : candidateLearning.length
                ? "未冻结个人学习复盘；以下仅为可复习的候选学习要点。"
                : "本次没有可由当前 Evidence 支撑的学习 Claim，保留为知识缺口。",
          ),
          this.renderProjectClaimSection(
            "如何实现",
            implementation,
            implementation.length
              ? undefined
              : "本次未冻结 implementation_method Claim，不从其他类别推断实现方式。",
          ),
        );
        const remaining = claims.filter((claim) => !primaryIds.has(claim.claim_id));
        if (remaining.length) {
          item.append(this.renderProjectClaimSection("其他项目要点", remaining));
        }
      }
      list.append(item);
    }
    view.append(list);
    return view;
  }

  private renderProjectScanLimitations(limitations: Limitation[]): HTMLElement {
    const section = element("section", "project-claim-section");
    section.append(element("h4", "subsection-title", "扫描限制"));
    const list = element("ul", "degradation-list");
    for (const limitation of limitations) {
      const item = element("li", "degradation-item");
      mirrorLimitationData(item, limitation);
      const label = element("div", "hanging-label");
      label.append(statusTag(limitation.severity), element("span", undefined, limitation.kind));
      const copy = element("div", "degradation-copy");
      const message = element("p");
      appendTokens(message, limitation.message_tokens);
      const impact = element("p", "remediation");
      impact.append(document.createTextNode("影响："));
      appendTokens(impact, limitation.impact_tokens);
      const remedy = element("p", "remediation");
      remedy.append(document.createTextNode("补救："));
      appendTokens(remedy, limitation.remediation_tokens);
      copy.append(message, impact, remedy);
      item.append(label, copy);
      list.append(item);
    }
    section.append(list);
    return section;
  }

  private renderProjectClaimSection(
    title: string,
    claims: ClaimItem[],
    note?: string,
  ): HTMLElement {
    const section = element("section", "project-claim-section");
    section.append(element("h4", "subsection-title", title));
    if (note) section.append(element("p", "remediation", note));
    if (claims.length) section.append(this.renderClaimList(claims));
    return section;
  }

  private renderEvidenceView(route: RouteState): HTMLElement {
    const view = element("div", "view");
    view.append(
      element("h2", "view-title", "证据横切"),
      element("p", "lede", "跨项目收敛 Claim 与完整 Evidence 指针。"),
    );
    const toolbar = element("div", "toolbar");
    const search = element("input", "search-field") as HTMLInputElement;
    search.id = "report-search";
    search.type = "search";
    search.placeholder = "检索项目、Claim、Evidence 或缺口";
    search.setAttribute("aria-label", "检索冻结报告");
    search.value = route.query.q ?? "";
    search.addEventListener("input", () => this.updateQuery(route, "q", search.value));
    const dimensionFilter = this.filterSelect(
      "岗位维度",
      "dimension",
      route.query.dimension ?? "",
      [
        ["", "全部岗位维度"],
        ...this.bundle.role_lens.dimensions.map((dimension) => [
          dimension.key,
          dimension.display_name,
        ]),
      ],
      route,
    );
    const projectFilter = this.filterSelect(
      "项目",
      "project",
      route.query.project ?? "",
      [["", "全部项目"], ...this.bundle.projects.map((project) => [project.project_id, project.display_name])],
      route,
    );
    const moduleFilter = this.filterSelect(
      "模块",
      "module",
      route.query.module ?? "",
      [
        ["", "全部模块"],
        ...this.bundle.projects.flatMap((project) =>
          project.modules.map((module) => [
            module.module_id,
            `${project.display_name} / ${module.name}`,
          ]),
        ),
      ],
      route,
    );
    const dispositionFilter = this.filterSelect(
      "项目快照状态",
      "disposition",
      route.query.disposition ?? "",
      [
        ["", "全部快照状态"],
        ["fresh", "本次新鲜"],
        ["carried_forward", "沿用基线"],
        ["failed_no_baseline", "失败无基线"],
        ["excluded", "已排除"],
      ],
      route,
    );
    const validityFilter = this.filterSelect(
      "时效",
      "validity",
      route.query.validity ?? "",
      [["", "全部时效"], ["current", "当前"], ["stale", "历史限制"], ["missing", "已缺失"], ["plan", "已规划"]],
      route,
    );
    const commitFilter = this.filterSelect(
      "提交状态",
      "commit",
      route.query.commit ?? "",
      [
        ["", "全部提交状态"],
        ["committed", "committed"],
        ["modified", "modified"],
        ["untracked", "untracked"],
        ["historical", "historical"],
        ["not_applicable", "not_applicable"],
      ],
      route,
    );
    const supportFilter = this.filterSelect(
      "支持等级",
      "support",
      route.query.support ?? "",
      [["", "全部支持等级"], ["single_source", "单一来源"], ["cross_checked", "交叉验证"], ["user_confirmed", "用户确认"], ["conflicted", "存在冲突"]],
      route,
    );
    const facetValues = [...new Set(this.bundle.claims.flatMap((claim) => claim.facets))].sort(
      (left, right) => left.localeCompare(right, "zh-CN"),
    );
    const facetFilter = this.filterSelect(
      "Claim facet",
      "facet",
      route.query.facet ?? "",
      [["", "全部 facets"], ...facetValues.map((facet) => [facet, facet])],
      route,
    );
    toolbar.append(
      search,
      dimensionFilter,
      projectFilter,
      moduleFilter,
      dispositionFilter,
      validityFilter,
      commitFilter,
      supportFilter,
      facetFilter,
    );
    view.append(toolbar);

    let claims = [...this.bundle.claims];
    const explicitClaim = route.query.claim;
    const explicitEvidence = route.query.evidence;
    if (explicitClaim) claims = claims.filter((claim) => claim.claim_id === explicitClaim);
    if (explicitEvidence) {
      claims = claims.filter((claim) => claim.evidence_relations.some((relation) => relation.evidence_id === explicitEvidence));
    }
    const selectedDimension = this.bundle.role_lens.dimensions.find(
      (dimension) => dimension.key === route.query.dimension,
    );
    const dimensionEvidenceKinds = selectedDimension
      ? new Set(selectedDimension.required_evidence_kinds)
      : undefined;
    claims = claims.filter((claim) => {
      const relatedEvidence = claim.evidence_relations.flatMap((relation) => {
        const item = this.evidence.get(relation.evidence_id);
        return item ? [item] : [];
      });
      return claimMatchesFilters(
        claim,
        relatedEvidence,
        route.query,
        this.projects.get(claim.project_id)?.snapshot_disposition,
        dimensionEvidenceKinds,
      );
    });
    if (route.query.q) {
      const matches = searchEntries(this.bundle.search_index.entries, route.query.q);
      const matchingIds = new Set(matches.map((entry) => entry.item_id));
      const matchingProjects = new Set(matches.map((entry) => entry.project_id).filter((value): value is string => typeof value === "string"));
      claims = claims.filter(
        (claim) =>
          matchingIds.has(`claim:${claim.claim_id}`) ||
          matchingProjects.has(claim.project_id) ||
          claim.evidence_relations.some((relation) => matchingIds.has(`evidence:${relation.evidence_id}`)),
      );
    }
    view.append(element("p", "result-count", `${claims.length} 条 Claim`));
    view.append(claims.length ? this.renderClaimList(claims, explicitClaim !== undefined || explicitEvidence !== undefined) : element("p", "empty-state", "当前筛选没有匹配 Claim。"));
    return view;
  }

  private filterSelect(
    label: string,
    key: string,
    selected: string,
    options: string[][],
    route: RouteState,
  ): HTMLSelectElement {
    const select = element("select", "filter-select");
    select.setAttribute("aria-label", label);
    for (const optionValue of options) {
      const value = optionValue[0] ?? "";
      const text = optionValue[1] ?? value;
      const option = element("option", undefined, text);
      option.value = value;
      option.selected = value === selected;
      select.append(option);
    }
    select.addEventListener("change", () => this.updateQuery(route, key, select.value));
    return select;
  }

  private updateQuery(route: RouteState, key: string, value: string): void {
    const query = new URLSearchParams(route.query);
    if (value) query.set(key, value);
    else query.delete(key);
    const suffix = query.toString();
    const path = `#/v1/${route.view}`;
    history.replaceState(null, "", `${path}${suffix ? `?${suffix}` : ""}`);
    this.render();
    if (key === "q") {
      const input = document.getElementById("report-search") as HTMLInputElement | null;
      input?.focus();
      input?.setSelectionRange(value.length, value.length);
    }
  }

  private renderClaimList(claims: ClaimItem[], forceOpen = false): HTMLElement {
    const list = element("div", "claim-list");
    for (const claim of claims) {
      const details = element("details", "claim-item focus-item") as HTMLDetailsElement;
      details.dataset.claimId = claim.claim_id;
      details.dataset.facets = JSON.stringify(claim.facets);
      details.tabIndex = 0;
      details.open = forceOpen;
      const summary = element("summary", "claim-summary");
      const project = this.projects.get(claim.project_id);
      const scope = element("span", "hanging-label", project?.display_name ?? claim.project_id);
      if (claim.module_id) {
        const module = project?.modules.find((item) => item.module_id === claim.module_id);
        scope.append(element("span", undefined, ` / ${module?.name ?? claim.module_id}`));
      }
      const statement = element("div", "claim-statement");
      appendTokens(statement, claim.statement_tokens);
      const meta = element("div", "claim-meta");
      meta.append(
        statusTag(claim.support_level),
        element("span", undefined, claim.category),
        element("span", undefined, claim.scope_kind),
        element("span", undefined, `归因 ${claim.personal_attribution}`),
        ...claim.facets.map((facet) => element("span", undefined, facet)),
      );
      statement.append(meta);
      summary.append(scope, statement);
      details.append(summary, this.renderClaimEvidence(claim));
      list.append(details);
    }
    return list;
  }

  private renderClaimEvidence(claim: ClaimItem): HTMLElement {
    const list = element("ul", "evidence-list");
    const groups = new Map<string, Array<{ relation: EvidenceRelation; item: EvidenceItem }>>();
    for (const relation of claim.evidence_relations) {
      const item = this.evidence.get(relation.evidence_id);
      if (!item) continue;
      const key = `${relation.relation}:${item.content_equivalence_key ?? item.evidence_id}`;
      const group = groups.get(key) ?? [];
      group.push({ relation, item });
      groups.set(key, group);
    }
    for (const group of groups.values()) {
      const primary = group[0];
      if (!primary) continue;
      const row = element("li", "evidence-item");
      row.append(this.renderEvidenceItem(primary.item, primary.relation));
      if (group.length > 1) {
        const equivalents = element("details");
        equivalents.append(element("summary", undefined, `展开 ${group.length - 1} 个内容等价来源`));
        for (const equivalent of group.slice(1)) {
          equivalents.append(this.renderEvidenceItem(equivalent.item, equivalent.relation));
        }
        row.append(equivalents);
      }
      list.append(row);
    }
    return list;
  }

  private renderEvidenceItem(item: EvidenceItem, relation: EvidenceRelation): HTMLElement {
    const wrapper = element("div");
    wrapper.dataset.evidenceId = item.evidence_id;
    wrapper.dataset.relation = relation.relation;
    wrapper.dataset.validity = item.validity;
    wrapper.dataset.commitState = item.commit_state;
    wrapper.dataset.supportedFacets = JSON.stringify(relation.supported_facets);
    const heading = element("div", "evidence-heading");
    heading.append(
      statusTag(item.validity),
      element("span", undefined, item.evidence_kind),
      element("span", undefined, relation.relation),
      element("span", "token-code", item.commit_state),
    );
    const summary = element("p", "evidence-summary");
    appendTokens(summary, item.summary_tokens);
    if (item.validity !== "current") {
      const validityNote = {
        stale: "（只作历史限制，不得单独支撑当前强结论）",
        missing: "（当前定位已缺失，不得支撑当前强结论）",
        plan: "（仅作已规划/已文档化表述）",
      }[item.validity] ?? "（证据状态已降级）";
      summary.append(
        document.createTextNode(validityNote),
      );
    }
    const fields = element("dl", "evidence-fields");
    const project = this.projects.get(item.project_id);
    const module = project?.modules.find((candidate) => candidate.module_id === item.module_id);
    this.addField(fields, "项目", project?.display_name ?? item.project_id);
    this.addField(fields, "模块", module?.name ?? item.module_id ?? "not_applicable");
    this.addField(fields, "Worktree", item.worktree ? `${item.worktree.worktree_id}${item.worktree.branch ? ` · ${item.worktree.branch}` : ""}` : "not_applicable");
    this.addField(
      fields,
      "Worktree 冻结批次",
      item.worktree?.observed_scan_run_id ?? "not_applicable",
    );
    this.addField(fields, "路径", item.relative_path ?? "not_applicable");
    const locatorStatus = element("span", "copy-status");
    const locator = JSON.stringify(item.locator);
    this.addFieldNode(fields, "Locator", copyValue(locator, "复制 Evidence locator", locatorStatus));
    this.addField(fields, "内容哈希", item.content_sha256 ?? "not_applicable");
    this.addField(fields, "Facets", relation.supported_facets.join(", ") || "none");
    wrapper.append(heading, summary, fields, locatorStatus);
    return wrapper;
  }

  private addField(list: HTMLDListElement, label: string, value: string): void {
    this.addFieldNode(list, label, document.createTextNode(displayText(value)));
  }

  private addFieldNode(list: HTMLDListElement, label: string, value: Node): void {
    list.append(element("dt", undefined, label));
    const description = element("dd");
    description.append(value);
    list.append(description);
  }

  private renderGaps(route: RouteState): HTMLElement {
    const view = element("div", "view");
    view.append(element("h2", "view-title", "知识缺口"), element("p", "lede", "按作用域和严重度保留尚不能可靠讲述的内容。"));
    const toolbar = element("div", "toolbar compact-toolbar");
    const projectFilter = this.filterSelect(
      "项目",
      "project",
      route.query.project ?? "",
      [
        ["", "全部项目"],
        ...this.bundle.projects.map((project) => [project.project_id, project.display_name]),
      ],
      route,
    );
    const moduleFilter = this.filterSelect(
      "模块",
      "module",
      route.query.module ?? "",
      [
        ["", "全部模块"],
        ...this.bundle.projects.flatMap((project) =>
          project.modules.map((module) => [
            module.module_id,
            `${project.display_name} / ${module.name}`,
          ]),
        ),
      ],
      route,
    );
    const gapDimensions = [
      ...new Set([
        ...this.bundle.role_lens.dimensions.map((dimension) => dimension.key),
        ...this.bundle.knowledge_gaps.map((gap) => gap.dimension),
      ]),
    ].sort((left, right) => left.localeCompare(right, "zh-CN"));
    const dimensionFilter = this.filterSelect(
      "岗位维度",
      "dimension",
      route.query.dimension ?? "",
      [
        ["", "全部岗位维度"],
        ...gapDimensions.map((dimension) => [
          dimension,
          this.bundle.role_lens.dimensions.find((item) => item.key === dimension)?.display_name ??
            dimension,
        ]),
      ],
      route,
    );
    const severityFilter = this.filterSelect(
      "严重度",
      "severity",
      route.query.severity ?? "",
      [
        ["", "全部严重度"],
        ["low", "低"],
        ["medium", "中"],
        ["high", "高"],
        ["critical", "严重"],
      ],
      route,
    );
    toolbar.append(projectFilter, moduleFilter, dimensionFilter, severityFilter);
    view.append(toolbar);
    let gaps = [...this.bundle.knowledge_gaps];
    if (route.query.gap) gaps = gaps.filter((gap) => gap.gap_id === route.query.gap);
    if (route.query.project) gaps = gaps.filter((gap) => gap.project_id === route.query.project);
    if (route.query.module) gaps = gaps.filter((gap) => gap.module_id === route.query.module);
    if (route.query.dimension) {
      gaps = gaps.filter((gap) => gap.dimension === route.query.dimension);
    }
    if (route.query.severity) gaps = gaps.filter((gap) => gap.severity === route.query.severity);
    view.append(element("p", "result-count", `${gaps.length} 个知识缺口`));
    const list = element("div", "gap-list");
    for (const gap of gaps) {
      const details = element("details", "gap-item focus-item") as HTMLDetailsElement;
      details.tabIndex = 0;
      details.open = route.query.gap === gap.gap_id;
      const summary = element("summary", "gap-summary");
      const hanging = element("span", "hanging-label");
      hanging.append(statusTag(gap.severity), element("span", undefined, gap.dimension));
      const description = element("div", "claim-statement");
      appendTokens(description, gap.description_tokens);
      description.append(element("div", "claim-meta", `${gap.scope_kind} · ${gap.status}`));
      summary.append(hanging, description);
      const body = element("div", "gap-body");
      body.append(
        element("p", undefined, `候选追问：哪些证据或上下文能解决这个缺口？`),
        element("p", "remediation", `建议动作：${gap.resolution_kind}`),
      );
      details.append(summary, body);
      list.append(details);
    }
    view.append(gaps.length ? list : element("p", "empty-state", "当前筛选没有知识缺口。"));
    return view;
  }

  private renderInterview(route: RouteState): HTMLElement {
    const view = element("div", "view");
    view.append(element("h2", "view-title", "面试与复习"), element("p", "lede", "问题只引用冻结 Claim 或明确的 KnowledgeGap。"));
    const continuityFilter = this.filterSelect(
      "复习连续性",
      "continuity",
      route.query.continuity ?? "",
      [
        ["", "全部连续性状态"],
        ["new", "待首次复习"],
        ["continued", "状态延续"],
        ["reassess_required", "需要重评"],
      ],
      route,
    );
    const toolbar = element("div", "toolbar single-filter-toolbar");
    toolbar.append(continuityFilter);
    view.append(toolbar);
    const pitch = element("section", "interview-section");
    pitch.append(element("h3", "section-title", "两分钟讲解骨架"));
    const pitchList = element("ol");
    for (const claimId of this.bundle.interview.two_minute_pitch_claim_ids) {
      const claim = this.claims.get(claimId);
      if (!claim) continue;
      const item = element("li");
      appendTokens(item, claim.statement_tokens);
      pitchList.append(item);
    }
    pitch.append(pitchList);
    const questionSection = element("section", "interview-section");
    questionSection.append(element("h3", "section-title", "分层题库与追问"));
    const questions = element("div", "question-list");
    for (const question of this.bundle.interview.questions) {
      const details = element("details", "question-item focus-item") as HTMLDetailsElement;
      details.tabIndex = 0;
      details.open = route.query.target === question.question_id;
      const summary = element("summary", "question-summary");
      summary.append(element("span", "hanging-label", question.level));
      const prompt = element("div", "claim-statement");
      appendTokens(prompt, question.prompt_tokens);
      summary.append(prompt);
      const body = element("div", "question-body");
      const followup = element("p");
      appendTokens(followup, question.follow_up_tokens);
      body.append(followup);
      details.append(summary, body);
      questions.append(details);
    }
    questionSection.append(questions);
    const review = element("section", "readonly-note");
    review.append(
      element("h3", "section-title", "冻结复习状态"),
      element("p", undefined, `截止 ${this.bundle.review.cutoff_at} · ${this.bundle.review.status}`),
    );
    let bindings = [...this.bundle.review.bindings];
    if (route.query.continuity) {
      bindings = bindings.filter(
        (binding) => binding.continuity_status === route.query.continuity,
      );
    }
    if (route.query.target) {
      bindings = bindings.filter(
        (binding) =>
          binding.review_target_id === route.query.target ||
          binding.review_target_binding_id === route.query.target,
      );
    }
    const masteryCounts = new Map<string, number>();
    for (const binding of bindings) {
      const level = masteryLabel(binding.mastery_level);
      masteryCounts.set(level, (masteryCounts.get(level) ?? 0) + 1);
    }
    if (masteryCounts.size) {
      const mastery = element("ul", "mastery-summary");
      mastery.setAttribute("aria-label", "掌握度分布");
      for (const [level, count] of masteryCounts) {
        const segment = element("li", "mastery-segment", `${level} ${count}`);
        mastery.append(segment);
      }
      review.append(mastery);
    }
    review.append(element("p", "result-count", `${bindings.length} 个复习目标`));
    for (const binding of bindings) {
      const row = element("div", "review-binding");
      row.dataset.continuity = binding.continuity_status;
      const heading = element("p", "review-binding-heading");
      if (binding.review_target_id) {
        const target = element("a", "review-target-link", "复习目标") as HTMLAnchorElement;
        target.href = `#/v1/interview/target/${encodeURIComponent(binding.review_target_id)}`;
        heading.append(target);
      }
      heading.append(
        statusTag(binding.continuity_status),
        document.createTextNode(
          displayText(
            ` 当前掌握度：${masteryLabel(binding.mastery_level)} · ${binding.next_review_at ?? "未设置复习日期"}`,
          ),
        ),
      );
      row.append(heading);
      if (binding.summary) {
        row.append(element("p", "review-summary", binding.summary));
      }
      if (binding.weak_points.length) {
        row.append(element("p", "review-weak-points", `薄弱点：${binding.weak_points.join("；")}`));
      }
      if (binding.historical_review) {
        const historical = binding.historical_review;
        const history = element("div", "review-history");
        history.append(
          element("p", "review-history-heading", "上次复盘（仅供历史参考，不代表当前掌握度）"),
          element(
            "p",
            "review-history-meta",
            `${masteryLabel(historical.mastery_level)} · ${historical.reviewed_at} · ${historical.next_review_at ?? "未设置复习日期"}`,
          ),
          element("p", "review-history-summary", historical.summary),
        );
        if (historical.weak_points.length) {
          history.append(
            element(
              "p",
              "review-history-weak-points",
              `历史薄弱点：${historical.weak_points.join("；")}`,
            ),
          );
        }
        row.append(history);
      }
      review.append(row);
    }
    const copyStatus = element("p", "copy-status");
    review.append(
      copyValue(this.bundle.review.skill_invocation, "复制复习状态更新 Skill 调用", copyStatus),
      copyStatus,
    );
    view.append(pitch, questionSection, review);
    return view;
  }

  private renderRouteError(message: string, errorKind?: string): HTMLElement {
    const error = element("div", "route-error");
    if (errorKind) error.dataset.errorKind = errorKind;
    error.append(element("h2", "section-title", "无法打开深链"), element("p", undefined, message));
    return error;
  }

  private renderFooter(): HTMLElement {
    return element("footer", "footer", "/ 检索 · j/k 移动 · Enter 打开 · e 展开 Evidence · Esc 返回检索");
  }

  private handleKeyboard(event: KeyboardEvent): void {
    const target = event.target;
    const isField =
      target instanceof HTMLInputElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLTextAreaElement ||
      (target instanceof HTMLElement && target.isContentEditable);
    const isInteractive =
      isField || target instanceof HTMLAnchorElement || target instanceof HTMLButtonElement;
    if (event.key === "/" && !isField) {
      event.preventDefault();
      this.focusSearchOrNavigate();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      this.focusSearchOrNavigate();
      return;
    }
    if (isInteractive) return;
    const items = [...document.querySelectorAll<HTMLElement>(".focus-item")];
    if (!items.length) return;
    const activeIndex = items.findIndex((item) => item === document.activeElement || item.contains(document.activeElement));
    if (event.key === "j" || event.key === "k") {
      event.preventDefault();
      const offset = event.key === "j" ? 1 : -1;
      const next = activeIndex < 0 ? (offset > 0 ? 0 : items.length - 1) : (activeIndex + offset + items.length) % items.length;
      items[next]?.focus();
    } else if (event.key === "Enter" && document.activeElement instanceof HTMLElement) {
      const active = document.activeElement;
      const details = active.closest("details");
      if (details) {
        event.preventDefault();
        details.open = !details.open;
      } else if (active.matches(".project-item")) {
        const link = active.querySelector<HTMLAnchorElement>("a[href]");
        if (link) {
          event.preventDefault();
          link.click();
        }
      }
    } else if (event.key === "e") {
      event.preventDefault();
      const active = (document.activeElement as HTMLElement | null)?.closest("details");
      if (active) active.open = !active.open;
    }
  }

  private focusSearchOrNavigate(): void {
    const search = document.getElementById("report-search") as HTMLInputElement | null;
    if (search) {
      this.pendingSearchFocus = false;
      search.focus();
      return;
    }
    this.pendingSearchFocus = true;
    if (parseRoute(location.hash).view === "evidence") {
      this.render();
    } else {
      location.hash = "/v1/evidence";
    }
  }

  private expandForPrint(): void {
    this.printState.clear();
    for (const details of document.querySelectorAll<HTMLDetailsElement>("details")) {
      this.printState.set(details, details.open);
      details.open = true;
    }
  }

  private restoreAfterPrint(): void {
    for (const [details, wasOpen] of this.printState) details.open = wasOpen;
    this.printState.clear();
  }
}

const app = document.getElementById("app");
if (!app) {
  throw new Error("GoodJob dashboard root is unavailable");
}
try {
  new Dashboard(app, readBundle()).mount();
} catch (error) {
  const failure = element("div", "route-error");
  failure.append(
    element("h1", "section-title", "无法打开冻结报告"),
    element("p", undefined, error instanceof Error ? error.message : "未知报告错误"),
  );
  app.replaceChildren(failure);
}

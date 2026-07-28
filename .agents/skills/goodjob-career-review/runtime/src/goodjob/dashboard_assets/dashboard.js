"use strict";
(() => {
  // src/model.ts
  var VISIBLE_CONTROLS = {
    "\u2028": "[U+2028]",
    "\u2029": "[U+2029]",
    "\u202A": "[U+202A]",
    "\u202B": "[U+202B]",
    "\u202C": "[U+202C]",
    "\u202D": "[U+202D]",
    "\u202E": "[U+202E]",
    "\u2066": "[U+2066]",
    "\u2067": "[U+2067]",
    "\u2068": "[U+2068]",
    "\u2069": "[U+2069]"
  };
  function displayText(value) {
    return value.replace(/[\u2028-\u202e\u2066-\u2069]/gu, (control) => VISIBLE_CONTROLS[control] ?? "[CONTROL]");
  }
  function formatBps(value) {
    return `${Math.floor(value / 100)}.${String(value % 100).padStart(2, "0")}%`;
  }
  function parseRoute(hash) {
    const source = hash.startsWith("#") ? hash.slice(1) : hash;
    const [pathPart = "/v1/overview", queryPart = ""] = source.split("?", 2);
    const parts = pathPart.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
    const version = parts[0] ?? "v1";
    const view = parts[1] ?? "overview";
    const query = {};
    for (const [key, value] of new URLSearchParams(queryPart)) {
      query[key] = value;
    }
    const state = { version, view, query };
    if (view === "project" && parts[2]) {
      state.projectId = parts[2];
    }
    if (view === "project" && parts[3] === "module" && parts[4]) {
      state.moduleId = parts[4];
    }
    if (view === "interview" && parts[2] === "target" && parts[3]) {
      state.query.target = parts[3];
    }
    return state;
  }
  function searchEntries(entries, rawQuery) {
    const query = rawQuery.normalize("NFKC").toLocaleLowerCase().trim();
    if (!query) {
      return [...entries];
    }
    return entries.filter((entry) => entry.search_text.includes(query));
  }
  function projectScanLimitations(limitations, projectId) {
    return limitations.filter(
      (limitation) => limitation.project_id === projectId && limitation.kind.startsWith("scan_issue:")
    );
  }
  function claimMatchesFilters(claim, relatedEvidence, query, projectDisposition, dimensionEvidenceKinds) {
    const selectedFacet = query.facet;
    if (query.project && claim.project_id !== query.project) return false;
    if (query.module && claim.module_id !== query.module) return false;
    if (query.disposition && projectDisposition !== query.disposition) return false;
    if (query.support && claim.support_level !== query.support) return false;
    if (selectedFacet && !claim.facets.includes(selectedFacet) && !claim.evidence_relations.some(
      (relation) => relation.supported_facets.includes(selectedFacet)
    )) {
      return false;
    }
    if (query.validity && !relatedEvidence.some((item) => item.validity === query.validity)) {
      return false;
    }
    if (query.commit && !relatedEvidence.some((item) => item.commit_state === query.commit)) {
      return false;
    }
    if (query.dimension && (!dimensionEvidenceKinds || !relatedEvidence.some((item) => dimensionEvidenceKinds.has(item.evidence_kind)))) {
      return false;
    }
    return true;
  }
  function statusLabel(value) {
    const labels = {
      current: "\u5F53\u524D",
      stale: "\u5386\u53F2\u9650\u5236",
      missing: "\u5DF2\u7F3A\u5931",
      plan: "\u5DF2\u89C4\u5212",
      fresh: "\u672C\u6B21\u65B0\u9C9C",
      carried_forward: "\u6CBF\u7528\u57FA\u7EBF",
      failed_no_baseline: "\u5931\u8D25\u65E0\u57FA\u7EBF",
      excluded: "\u5DF2\u6392\u9664",
      single_source: "\u5355\u4E00\u6765\u6E90",
      cross_checked: "\u4EA4\u53C9\u9A8C\u8BC1",
      user_confirmed: "\u7528\u6237\u786E\u8BA4",
      conflicted: "\u5B58\u5728\u51B2\u7A81",
      new: "\u5F85\u9996\u6B21\u590D\u4E60",
      continued: "\u72B6\u6001\u5EF6\u7EED",
      reassess_required: "\u9700\u8981\u91CD\u8BC4",
      completed: "\u5B8C\u6574\u5FEB\u7167",
      partial: "\u90E8\u5206\u5FEB\u7167",
      low: "\u4F4E",
      medium: "\u4E2D",
      high: "\u9AD8",
      critical: "\u4E25\u91CD"
    };
    return labels[value] ?? value;
  }
  function statusSymbol(value) {
    if (["fresh", "current", "cross_checked", "user_confirmed", "continued"].includes(value)) {
      return "\u2713";
    }
    if (["missing", "failed_no_baseline", "conflicted", "reassess_required", "high", "critical", "partial"].includes(value)) {
      return "!";
    }
    if (["stale", "carried_forward", "plan", "medium"].includes(value)) {
      return "\u25F7";
    }
    return "\u25CB";
  }

  // src/dashboard.ts
  var NAV_ITEMS = [
    ["overview", "\u603B\u89C8", "#/v1/overview"],
    ["project", "\u9879\u76EE\u4E0E\u6A21\u5757", "#/v1/project"],
    ["evidence", "\u8BC1\u636E\u6A2A\u5207", "#/v1/evidence"],
    ["gaps", "\u77E5\u8BC6\u7F3A\u53E3", "#/v1/gaps"],
    ["interview", "\u9762\u8BD5\u4E0E\u590D\u4E60", "#/v1/interview"]
  ];
  var MASTERY_LABELS = {
    unfamiliar: "\u4E0D\u719F\u6089",
    developing: "\u6B63\u5728\u638C\u63E1",
    solid: "\u8F83\u624E\u5B9E",
    mastered: "\u5DF2\u638C\u63E1"
  };
  function masteryLabel(value) {
    return value === null ? "\u672A\u8BC4\u4F30" : MASTERY_LABELS[value] ?? value;
  }
  var ICON_PATHS = {
    overview: ["M3 3h7v7H3z", "M14 3h7v4h-7z", "M14 11h7v10h-7z", "M3 14h7v7H3z"],
    project: ["M3 6h18", "M3 12h18", "M3 18h18", "M7 3v6", "M17 9v6", "M7 15v6"],
    evidence: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "m9 15 2 2 4-4"],
    gaps: ["M12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z", "M12 8v5", "M12 17h.01"],
    interview: ["M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z", "M8 9h8", "M8 13h5"]
  };
  function element(tag, className, text) {
    const value = document.createElement(tag);
    if (className) {
      value.className = className;
    }
    if (text !== void 0) {
      value.textContent = displayText(text);
    }
    return value;
  }
  function icon(name) {
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
  function ratioBar(value, maximum, className, label) {
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
  function appendTokens(parent, tokens) {
    for (const token of tokens) {
      let node;
      if (token.kind === "code") {
        node = element("code", "token-code", token.value);
      } else if (token.kind === "emphasis") {
        node = element("strong", "token-emphasis", token.value);
      } else if (["claim_ref", "evidence_ref", "gap_ref"].includes(token.kind)) {
        node = element("span", "token-reference", token.value);
        node.title = displayText(`${token.kind}: ${token.ref_id ?? ""}`);
      } else if (token.kind === "inert_url") {
        node = element("span", "token-inert-url", `${token.value}\uFF08\u672A\u6FC0\u6D3B\uFF09`);
      } else {
        node = document.createTextNode(displayText(token.value));
      }
      parent.append(node);
    }
  }
  function statusTag(value, label = statusLabel(value)) {
    const tag = element("span", "status-tag");
    tag.dataset.status = value;
    tag.append(element("span", void 0, statusSymbol(value)), element("span", void 0, label));
    return tag;
  }
  function copyValue(text, label, status) {
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
        status.textContent = "\u5DF2\u590D\u5236\u3002";
      } catch {
        status.textContent = "\u526A\u8D34\u677F\u4E0D\u53EF\u7528\uFF0C\u8BF7\u624B\u52A8\u9009\u4E2D\u4E0A\u65B9\u6587\u672C\u3002";
      }
    });
    wrapper.append(control, element("span", "print-value", text));
    return wrapper;
  }
  function readBundle() {
    const source = document.getElementById("report-data");
    if (!source || source.textContent === null) {
      throw new Error("\u51BB\u7ED3\u62A5\u544A\u6570\u636E\u4E0D\u5B58\u5728");
    }
    const parsed = JSON.parse(source.textContent);
    if (!parsed || typeof parsed !== "object") {
      throw new Error("\u51BB\u7ED3\u62A5\u544A\u6570\u636E\u4E0D\u662F\u5BF9\u8C61");
    }
    const bundle = parsed;
    if (bundle.contract_version !== "report-bundle-v1") {
      throw new Error("\u4E0D\u652F\u6301\u7684 ReportBundle \u5951\u7EA6\u7248\u672C");
    }
    if (!Array.isArray(bundle.projects) || !Array.isArray(bundle.claims) || !Array.isArray(bundle.evidence)) {
      throw new Error("\u51BB\u7ED3\u62A5\u544A\u6570\u636E\u7F3A\u5C11\u5FC5\u9700\u96C6\u5408");
    }
    return bundle;
  }
  var Dashboard = class {
    constructor(app2, bundle) {
      this.projects = /* @__PURE__ */ new Map();
      this.claims = /* @__PURE__ */ new Map();
      this.evidence = /* @__PURE__ */ new Map();
      this.gaps = /* @__PURE__ */ new Map();
      this.printState = /* @__PURE__ */ new Map();
      this.pendingSearchFocus = false;
      this.app = app2;
      this.bundle = bundle;
      for (const project of bundle.projects) this.projects.set(project.project_id, project);
      for (const claim of bundle.claims) this.claims.set(claim.claim_id, claim);
      for (const item of bundle.evidence) this.evidence.set(item.evidence_id, item);
      for (const gap of bundle.knowledge_gaps) this.gaps.set(gap.gap_id, gap);
    }
    mount() {
      if (!location.hash) {
        location.hash = "/v1/overview";
      }
      this.render();
      window.addEventListener("hashchange", () => this.render());
      document.addEventListener("keydown", (event) => this.handleKeyboard(event));
      window.addEventListener("beforeprint", () => this.expandForPrint());
      window.addEventListener("afterprint", () => this.restoreAfterPrint());
    }
    render() {
      const route = parseRoute(location.hash);
      const shell = element("div", "shell");
      shell.append(this.renderSideNav(route), this.renderContent(route));
      this.app.replaceChildren(shell);
      if (this.pendingSearchFocus) {
        const search = document.getElementById("report-search");
        if (search) {
          this.pendingSearchFocus = false;
          search.focus();
        }
      }
    }
    renderSideNav(route) {
      const nav = element("nav", "side-nav");
      nav.setAttribute("aria-label", "\u4E3B\u8981\u89C6\u56FE");
      nav.append(element("p", "wordmark", "GoodJob"));
      const list = element("ul", "nav-list");
      for (const [view, label, href] of NAV_ITEMS) {
        const item = element("li");
        const link = element("a", "nav-link");
        link.href = href;
        if (route.view === view) link.setAttribute("aria-current", "page");
        link.append(icon(view), document.createTextNode(label));
        item.append(link);
        list.append(item);
      }
      nav.append(list);
      return nav;
    }
    renderMobileNav(route) {
      const wrapper = element("div", "mobile-nav");
      wrapper.append(element("p", "wordmark", "GoodJob"));
      const select = element("select", "mobile-nav-select");
      select.setAttribute("aria-label", "\u5207\u6362\u89C6\u56FE");
      for (const [view, label, href] of NAV_ITEMS) {
        const option = element("option", void 0, label);
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
    renderContent(route) {
      const content = element("div", "content");
      content.append(this.renderMobileNav(route), this.renderForensicStrip(), this.renderRoleHeader(), this.renderCoverage());
      const main = element("main");
      main.id = "main";
      if (route.version !== "v1") {
        main.append(this.renderRouteError("\u8BE5\u94FE\u63A5\u5C5E\u4E8E\u5176\u4ED6\u5951\u7EA6\u7248\u672C\uFF0C\u5F53\u524D\u5FEB\u7167\u53EA\u652F\u6301 v1\u3002"));
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
        main.append(this.renderRouteError("\u8BE5\u6DF1\u94FE\u5728\u5F53\u524D\u5FEB\u7167\u4E2D\u4E0D\u5B58\u5728\u3002"));
      }
      content.append(main, this.renderFooter());
      return content;
    }
    renderForensicStrip() {
      const strip = element("div", "forensic-strip");
      strip.setAttribute("aria-label", "\u51BB\u7ED3\u5FEB\u7167\u8EAB\u4EFD");
      strip.append(
        statusTag(this.bundle.package_status),
        element("span", void 0, `run ${this.bundle.preparation_run_id.slice(0, 12)}`),
        element("span", void 0, `sha ${this.bundle.bundle_sha256.slice(0, 14)}`),
        element("span", void 0, this.bundle.generated_at),
        element("span", void 0, this.bundle.primary_language),
        element("span", void 0, "\u5DF2\u51BB\u7ED3 \xB7 \u53EA\u8BFB")
      );
      return strip;
    }
    renderRoleHeader() {
      const header = element("header", "role-header");
      header.append(element("p", "eyebrow", "ROLE DOSSIER / \u5C97\u4F4D\u5377\u5B97"));
      const title = element("h1", "role-title", this.bundle.role.name);
      const meta = element("p", "role-meta");
      meta.append(
        element("span", void 0, `\u804C\u7EA7\uFF1A${this.bundle.role.applied_level ?? "\u672A\u6307\u5B9A"}`),
        element("span", void 0, `\u804C\u7EA7\u6765\u6E90\uFF1A${this.bundle.role.level_source}`),
        element(
          "span",
          void 0,
          this.bundle.role.jd.has_jd ? `JD\uFF1A${this.bundle.role.jd.input_kind} \xB7 ${this.bundle.role.jd.content_sha256?.slice(0, 12) ?? ""}` : "JD\uFF1A\u672A\u63D0\u4F9B\uFF0C\u4F7F\u7528\u51BB\u7ED3\u5047\u8BBE"
        )
      );
      header.append(title, meta);
      const assumptions = element("div", "role-assumptions");
      assumptions.append(element("p", "section-kicker", "\u51BB\u7ED3\u5C97\u4F4D\u5047\u8BBE"));
      const assumptionList = element("ul");
      for (const assumption of this.bundle.role_lens.assumptions) {
        assumptionList.append(element("li", void 0, assumption));
      }
      if (!this.bundle.role_lens.assumptions.length) {
        assumptionList.append(element("li", void 0, "\u672C\u6B21 RoleLens \u672A\u8BB0\u5F55\u989D\u5916\u5047\u8BBE\u3002"));
      }
      assumptions.append(assumptionList);
      header.append(assumptions);
      return header;
    }
    renderCoverage() {
      const block = element("section", "coverage-block");
      block.setAttribute("aria-labelledby", "coverage-title");
      const kicker = element("h2", "section-kicker", "L2 / WORKSPACE COVERAGE");
      kicker.id = "coverage-title";
      block.append(
        kicker,
        element(
          "p",
          "coverage-summary",
          `\u5171 ${this.bundle.coverage.projects_total} \u4E2A\u9879\u76EE\u91CC\u6709 ${this.bundle.coverage.eligible_projects} \u4E2A\u53C2\u4E0E\u8BC4\u5206`
        )
      );
      const order = ["fresh", "carried_forward", "failed_no_baseline", "excluded"];
      const band = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      band.classList.add("coverage-band");
      const total = Math.max(
        1,
        order.reduce(
          (sum, disposition) => sum + (this.bundle.coverage.disposition_counts[disposition] ?? 0),
          0
        )
      );
      band.setAttribute("viewBox", `0 0 ${total} 1`);
      band.setAttribute("preserveAspectRatio", "none");
      band.setAttribute("role", "img");
      band.setAttribute("aria-label", "\u9879\u76EE\u5FEB\u7167 disposition \u6784\u6210");
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
      exclusions.append(element("p", "section-kicker", "NORMAL EXCLUSIONS / \u626B\u63CF\u6392\u9664\u7EDF\u8BA1"));
      if (this.bundle.coverage.excluded_by_category_available) {
        const exclusionList = element("ul", "coverage-legend");
        const entries = Object.entries(this.bundle.coverage.excluded_by_category).filter(
          ([, count]) => count > 0
        );
        for (const [kind, count] of entries) {
          exclusionList.append(element("li", void 0, `${kind} ${count}`));
        }
        if (!entries.length) exclusionList.append(element("li", void 0, "\u65E0"));
        exclusions.append(exclusionList);
      } else {
        exclusions.append(element("p", "remediation", "\u8BE5\u65E7\u626B\u63CF\u5FEB\u7167\u672A\u8BB0\u5F55\u5206\u7C7B\u8BA1\u6570\u3002"));
      }
      block.append(exclusions);
      return block;
    }
    renderOverview() {
      const view = element("div", "view");
      view.append(element("h2", "view-title", "\u603B\u89C8"));
      const degradation = element("section", "lens-section");
      degradation.append(element("h3", "section-title", "\u8986\u76D6\u9650\u5236"));
      const limitations = this.bundle.coverage.limitations;
      if (!limitations.length) {
        degradation.append(element("p", "empty-state", "\u672C\u6B21\u672A\u8BB0\u5F55\u8986\u76D6\u9650\u5236\u3002"));
      } else {
        const list = element("ul", "degradation-list");
        for (const limitation of limitations) {
          const item = element("li", "degradation-item");
          item.dataset.severity = limitation.severity;
          const label = element("div", "hanging-label");
          label.append(statusTag(limitation.severity), element("span", void 0, limitation.kind));
          const copy = element("div", "degradation-copy");
          const message = element("p");
          appendTokens(message, limitation.message_tokens);
          const impact = element("p", "remediation");
          impact.append(document.createTextNode("\u5F71\u54CD\uFF1A"));
          appendTokens(impact, limitation.impact_tokens);
          const remedy = element("p", "remediation");
          remedy.append(document.createTextNode("\u8865\u6551\uFF1A"));
          appendTokens(remedy, limitation.remediation_tokens);
          const route = element("a", "scope-link", "\u67E5\u770B\u53D7\u5F71\u54CD\u8303\u56F4");
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
    renderLens() {
      const section = element("section", "lens-section");
      section.append(element("h3", "section-title", "\u5C97\u4F4D\u955C\u5934\u6743\u91CD"));
      for (const dimension of this.bundle.role_lens.dimensions) {
        const row = element("div", "bar-row");
        const name = element("span", void 0, dimension.display_name);
        name.title = displayText(dimension.evaluation_criteria);
        const track = ratioBar(
          dimension.weight_bps,
          1e4,
          "weight",
          `${dimension.display_name} \u6743\u91CD ${formatBps(dimension.weight_bps)}`
        );
        const value = element("span", "numeric", `${formatBps(dimension.weight_bps)} \xB7 ${dimension.weight_bps} bps`);
        row.append(name, track, value);
        section.append(row);
      }
      return section;
    }
    renderRanking() {
      const section = element("section", "rank-section");
      section.append(element("h3", "section-title", "\u9879\u76EE\u6392\u5E8F"));
      const list = element("ol", "rank-list");
      const eligible = this.bundle.projects.filter((project) => project.assessment !== null);
      for (const project of eligible) {
        const assessment = project.assessment;
        if (!assessment) continue;
        const item = element("li", "rank-item");
        item.append(element("span", "rank-number", String(assessment.rank)));
        const body = element("div");
        const heading = element("div", "rank-heading");
        const link = element("a");
        link.href = `#/v1/project/${encodeURIComponent(project.project_id)}`;
        link.append(element("h3", void 0, project.display_name));
        heading.append(link, statusTag(project.snapshot_disposition));
        const track = ratioBar(
          assessment.final_score_milli,
          1e3,
          "score",
          `${project.display_name} \u6700\u7EC8\u5F97\u5206 ${assessment.final_score_milli}`
        );
        const equation = element(
          "p",
          "score-equation",
          `${assessment.base_score_milli} \xD7 ${formatBps(assessment.coverage_bps)} \u2192 ${assessment.final_score_milli}`
        );
        body.append(heading, track, equation);
        item.append(body);
        list.append(item);
      }
      section.append(list);
      const ineligible = this.bundle.projects.filter((project) => project.assessment === null);
      if (ineligible.length) {
        const note = element("p", "section-kicker", "\u4EC5\u8FDB\u5165\u8986\u76D6\uFF0C\u4E0D\u8BC4\u5206");
        section.append(note);
        for (const project of ineligible) {
          const row = element("div", "rank-heading");
          row.append(element("span", void 0, project.display_name), statusTag(project.snapshot_disposition));
          section.append(row);
        }
      }
      return section;
    }
    renderProjects(route) {
      const view = element("div", "view");
      const selected = route.projectId ? this.projects.get(route.projectId) : void 0;
      view.append(
        element("h2", "view-title", selected ? selected.display_name : "\u9879\u76EE\u4E0E\u6A21\u5757"),
        element(
          "p",
          "lede",
          selected ? "\u51BB\u7ED3\u9879\u76EE\u5FEB\u7167\u3001\u6A21\u5757\u8FB9\u754C\u4E0E\u5173\u8054 Claim\u3002" : "\u6309\u5C97\u4F4D\u6392\u5E8F\u67E5\u770B\u6240\u6709\u9879\u76EE\u53CA\u5176\u51BB\u7ED3\u6A21\u5757\u8FB9\u754C\u3002"
        )
      );
      const list = element("ul", "project-list");
      for (const project of selected ? [selected] : this.bundle.projects) {
        const item = element("li", "project-item focus-item");
        item.tabIndex = 0;
        const heading = element("div", "rank-heading");
        const link = element("a");
        link.href = `#/v1/project/${encodeURIComponent(project.project_id)}`;
        link.append(element("h3", void 0, project.display_name));
        heading.append(link, statusTag(project.snapshot_disposition));
        item.append(heading);
        if (project.assessment) {
          const rationale = element("p", "lede");
          appendTokens(rationale, project.assessment.rationale_tokens);
          item.append(rationale);
        } else {
          item.append(element("p", "lede", "\u5F53\u524D\u9879\u76EE\u6CA1\u6709\u53EF\u8BC4\u5206\u57FA\u7EBF\u3002"));
        }
        if (project.modules.length) {
          const modules = element("ul", "module-list");
          for (const module of project.modules) {
            const moduleItem = element("li", "module-item");
            const moduleLink = element("a", void 0, module.name);
            moduleLink.href = `#/v1/project/${encodeURIComponent(project.project_id)}/module/${encodeURIComponent(module.module_id)}`;
            moduleItem.append(
              moduleLink,
              element("span", "module-path", `${module.relative_root} \xB7 ${module.kind} \xB7 ${module.adapter_id}`)
            );
            modules.append(moduleItem);
          }
          item.append(modules);
        } else {
          item.append(element("p", "remediation", "\u672C\u6B21\u672A\u8BC6\u522B\u72EC\u7ACB\u6A21\u5757\u8FB9\u754C\uFF0C\u6750\u6599\u4FDD\u6301\u9879\u76EE\u7EA7\u4F5C\u7528\u57DF\u3002"));
        }
        if (selected) {
          const scanLimitations = projectScanLimitations(
            this.bundle.coverage.limitations,
            project.project_id
          );
          if (scanLimitations.length) {
            item.append(this.renderProjectScanLimitations(scanLimitations));
          }
          const claims = this.bundle.claims.filter(
            (claim) => claim.project_id === project.project_id && (!route.moduleId || claim.module_id === route.moduleId)
          );
          const learning = claims.filter((claim) => claim.category === "learning");
          const candidateLearning = learning.length ? [] : claims.filter((claim) => claim.personal_attribution === "capability");
          const implementation = claims.filter(
            (claim) => claim.category === "implementation_method"
          );
          const primaryIds = new Set(
            [...learning, ...candidateLearning, ...implementation].map((claim) => claim.claim_id)
          );
          item.append(
            this.renderProjectClaimSection(
              "\u5B66\u4E60\u8981\u70B9",
              learning.length ? learning : candidateLearning,
              learning.length ? void 0 : candidateLearning.length ? "\u672A\u51BB\u7ED3\u4E2A\u4EBA\u5B66\u4E60\u590D\u76D8\uFF1B\u4EE5\u4E0B\u4EC5\u4E3A\u53EF\u590D\u4E60\u7684\u5019\u9009\u5B66\u4E60\u8981\u70B9\u3002" : "\u672C\u6B21\u6CA1\u6709\u53EF\u7531\u5F53\u524D Evidence \u652F\u6491\u7684\u5B66\u4E60 Claim\uFF0C\u4FDD\u7559\u4E3A\u77E5\u8BC6\u7F3A\u53E3\u3002"
            ),
            this.renderProjectClaimSection(
              "\u5982\u4F55\u5B9E\u73B0",
              implementation,
              implementation.length ? void 0 : "\u672C\u6B21\u672A\u51BB\u7ED3 implementation_method Claim\uFF0C\u4E0D\u4ECE\u5176\u4ED6\u7C7B\u522B\u63A8\u65AD\u5B9E\u73B0\u65B9\u5F0F\u3002"
            )
          );
          const remaining = claims.filter((claim) => !primaryIds.has(claim.claim_id));
          if (remaining.length) {
            item.append(this.renderProjectClaimSection("\u5176\u4ED6\u9879\u76EE\u8981\u70B9", remaining));
          }
        }
        list.append(item);
      }
      view.append(list);
      return view;
    }
    renderProjectScanLimitations(limitations) {
      const section = element("section", "project-claim-section");
      section.append(element("h4", "subsection-title", "\u626B\u63CF\u9650\u5236"));
      const list = element("ul", "degradation-list");
      for (const limitation of limitations) {
        const item = element("li", "degradation-item");
        item.dataset.severity = limitation.severity;
        const label = element("div", "hanging-label");
        label.append(statusTag(limitation.severity), element("span", void 0, limitation.kind));
        const copy = element("div", "degradation-copy");
        const message = element("p");
        appendTokens(message, limitation.message_tokens);
        const impact = element("p", "remediation");
        impact.append(document.createTextNode("\u5F71\u54CD\uFF1A"));
        appendTokens(impact, limitation.impact_tokens);
        const remedy = element("p", "remediation");
        remedy.append(document.createTextNode("\u8865\u6551\uFF1A"));
        appendTokens(remedy, limitation.remediation_tokens);
        copy.append(message, impact, remedy);
        item.append(label, copy);
        list.append(item);
      }
      section.append(list);
      return section;
    }
    renderProjectClaimSection(title, claims, note) {
      const section = element("section", "project-claim-section");
      section.append(element("h4", "subsection-title", title));
      if (note) section.append(element("p", "remediation", note));
      if (claims.length) section.append(this.renderClaimList(claims));
      return section;
    }
    renderEvidenceView(route) {
      const view = element("div", "view");
      view.append(
        element("h2", "view-title", "\u8BC1\u636E\u6A2A\u5207"),
        element("p", "lede", "\u8DE8\u9879\u76EE\u6536\u655B Claim \u4E0E\u5B8C\u6574 Evidence \u6307\u9488\u3002")
      );
      const toolbar = element("div", "toolbar");
      const search = element("input", "search-field");
      search.id = "report-search";
      search.type = "search";
      search.placeholder = "\u68C0\u7D22\u9879\u76EE\u3001Claim\u3001Evidence \u6216\u7F3A\u53E3";
      search.setAttribute("aria-label", "\u68C0\u7D22\u51BB\u7ED3\u62A5\u544A");
      search.value = route.query.q ?? "";
      search.addEventListener("input", () => this.updateQuery(route, "q", search.value));
      const dimensionFilter = this.filterSelect(
        "\u5C97\u4F4D\u7EF4\u5EA6",
        "dimension",
        route.query.dimension ?? "",
        [
          ["", "\u5168\u90E8\u5C97\u4F4D\u7EF4\u5EA6"],
          ...this.bundle.role_lens.dimensions.map((dimension) => [
            dimension.key,
            dimension.display_name
          ])
        ],
        route
      );
      const projectFilter = this.filterSelect(
        "\u9879\u76EE",
        "project",
        route.query.project ?? "",
        [["", "\u5168\u90E8\u9879\u76EE"], ...this.bundle.projects.map((project) => [project.project_id, project.display_name])],
        route
      );
      const moduleFilter = this.filterSelect(
        "\u6A21\u5757",
        "module",
        route.query.module ?? "",
        [
          ["", "\u5168\u90E8\u6A21\u5757"],
          ...this.bundle.projects.flatMap(
            (project) => project.modules.map((module) => [
              module.module_id,
              `${project.display_name} / ${module.name}`
            ])
          )
        ],
        route
      );
      const dispositionFilter = this.filterSelect(
        "\u9879\u76EE\u5FEB\u7167\u72B6\u6001",
        "disposition",
        route.query.disposition ?? "",
        [
          ["", "\u5168\u90E8\u5FEB\u7167\u72B6\u6001"],
          ["fresh", "\u672C\u6B21\u65B0\u9C9C"],
          ["carried_forward", "\u6CBF\u7528\u57FA\u7EBF"],
          ["failed_no_baseline", "\u5931\u8D25\u65E0\u57FA\u7EBF"],
          ["excluded", "\u5DF2\u6392\u9664"]
        ],
        route
      );
      const validityFilter = this.filterSelect(
        "\u65F6\u6548",
        "validity",
        route.query.validity ?? "",
        [["", "\u5168\u90E8\u65F6\u6548"], ["current", "\u5F53\u524D"], ["stale", "\u5386\u53F2\u9650\u5236"], ["missing", "\u5DF2\u7F3A\u5931"], ["plan", "\u5DF2\u89C4\u5212"]],
        route
      );
      const commitFilter = this.filterSelect(
        "\u63D0\u4EA4\u72B6\u6001",
        "commit",
        route.query.commit ?? "",
        [
          ["", "\u5168\u90E8\u63D0\u4EA4\u72B6\u6001"],
          ["committed", "committed"],
          ["modified", "modified"],
          ["untracked", "untracked"],
          ["historical", "historical"],
          ["not_applicable", "not_applicable"]
        ],
        route
      );
      const supportFilter = this.filterSelect(
        "\u652F\u6301\u7B49\u7EA7",
        "support",
        route.query.support ?? "",
        [["", "\u5168\u90E8\u652F\u6301\u7B49\u7EA7"], ["single_source", "\u5355\u4E00\u6765\u6E90"], ["cross_checked", "\u4EA4\u53C9\u9A8C\u8BC1"], ["user_confirmed", "\u7528\u6237\u786E\u8BA4"], ["conflicted", "\u5B58\u5728\u51B2\u7A81"]],
        route
      );
      const facetValues = [...new Set(this.bundle.claims.flatMap((claim) => claim.facets))].sort(
        (left, right) => left.localeCompare(right, "zh-CN")
      );
      const facetFilter = this.filterSelect(
        "Claim facet",
        "facet",
        route.query.facet ?? "",
        [["", "\u5168\u90E8 facets"], ...facetValues.map((facet) => [facet, facet])],
        route
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
        facetFilter
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
        (dimension) => dimension.key === route.query.dimension
      );
      const dimensionEvidenceKinds = selectedDimension ? new Set(selectedDimension.required_evidence_kinds) : void 0;
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
          dimensionEvidenceKinds
        );
      });
      if (route.query.q) {
        const matches = searchEntries(this.bundle.search_index.entries, route.query.q);
        const matchingIds = new Set(matches.map((entry) => entry.item_id));
        const matchingProjects = new Set(matches.map((entry) => entry.project_id).filter((value) => typeof value === "string"));
        claims = claims.filter(
          (claim) => matchingIds.has(`claim:${claim.claim_id}`) || matchingProjects.has(claim.project_id) || claim.evidence_relations.some((relation) => matchingIds.has(`evidence:${relation.evidence_id}`))
        );
      }
      view.append(element("p", "result-count", `${claims.length} \u6761 Claim`));
      view.append(claims.length ? this.renderClaimList(claims, explicitClaim !== void 0 || explicitEvidence !== void 0) : element("p", "empty-state", "\u5F53\u524D\u7B5B\u9009\u6CA1\u6709\u5339\u914D Claim\u3002"));
      return view;
    }
    filterSelect(label, key, selected, options, route) {
      const select = element("select", "filter-select");
      select.setAttribute("aria-label", label);
      for (const optionValue of options) {
        const value = optionValue[0] ?? "";
        const text = optionValue[1] ?? value;
        const option = element("option", void 0, text);
        option.value = value;
        option.selected = value === selected;
        select.append(option);
      }
      select.addEventListener("change", () => this.updateQuery(route, key, select.value));
      return select;
    }
    updateQuery(route, key, value) {
      const query = new URLSearchParams(route.query);
      if (value) query.set(key, value);
      else query.delete(key);
      const suffix = query.toString();
      const path = `#/v1/${route.view}`;
      history.replaceState(null, "", `${path}${suffix ? `?${suffix}` : ""}`);
      this.render();
      if (key === "q") {
        const input = document.getElementById("report-search");
        input?.focus();
        input?.setSelectionRange(value.length, value.length);
      }
    }
    renderClaimList(claims, forceOpen = false) {
      const list = element("div", "claim-list");
      for (const claim of claims) {
        const details = element("details", "claim-item focus-item");
        details.tabIndex = 0;
        details.open = forceOpen;
        const summary = element("summary", "claim-summary");
        const project = this.projects.get(claim.project_id);
        const scope = element("span", "hanging-label", project?.display_name ?? claim.project_id);
        if (claim.module_id) {
          const module = project?.modules.find((item) => item.module_id === claim.module_id);
          scope.append(element("span", void 0, ` / ${module?.name ?? claim.module_id}`));
        }
        const statement = element("div", "claim-statement");
        appendTokens(statement, claim.statement_tokens);
        const meta = element("div", "claim-meta");
        meta.append(
          statusTag(claim.support_level),
          element("span", void 0, claim.category),
          element("span", void 0, claim.scope_kind),
          element("span", void 0, `\u5F52\u56E0 ${claim.personal_attribution}`),
          ...claim.facets.map((facet) => element("span", void 0, facet))
        );
        statement.append(meta);
        summary.append(scope, statement);
        details.append(summary, this.renderClaimEvidence(claim));
        list.append(details);
      }
      return list;
    }
    renderClaimEvidence(claim) {
      const list = element("ul", "evidence-list");
      const groups = /* @__PURE__ */ new Map();
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
          equivalents.append(element("summary", void 0, `\u5C55\u5F00 ${group.length - 1} \u4E2A\u5185\u5BB9\u7B49\u4EF7\u6765\u6E90`));
          for (const equivalent of group.slice(1)) {
            equivalents.append(this.renderEvidenceItem(equivalent.item, equivalent.relation));
          }
          row.append(equivalents);
        }
        list.append(row);
      }
      return list;
    }
    renderEvidenceItem(item, relation) {
      const wrapper = element("div");
      const heading = element("div", "evidence-heading");
      heading.append(
        statusTag(item.validity),
        element("span", void 0, item.evidence_kind),
        element("span", void 0, relation.relation),
        element("span", "token-code", item.commit_state)
      );
      const summary = element("p", "evidence-summary");
      appendTokens(summary, item.summary_tokens);
      if (item.validity !== "current") {
        const validityNote = {
          stale: "\uFF08\u53EA\u4F5C\u5386\u53F2\u9650\u5236\uFF0C\u4E0D\u5F97\u5355\u72EC\u652F\u6491\u5F53\u524D\u5F3A\u7ED3\u8BBA\uFF09",
          missing: "\uFF08\u5F53\u524D\u5B9A\u4F4D\u5DF2\u7F3A\u5931\uFF0C\u4E0D\u5F97\u652F\u6491\u5F53\u524D\u5F3A\u7ED3\u8BBA\uFF09",
          plan: "\uFF08\u4EC5\u4F5C\u5DF2\u89C4\u5212/\u5DF2\u6587\u6863\u5316\u8868\u8FF0\uFF09"
        }[item.validity] ?? "\uFF08\u8BC1\u636E\u72B6\u6001\u5DF2\u964D\u7EA7\uFF09";
        summary.append(
          document.createTextNode(validityNote)
        );
      }
      const fields = element("dl", "evidence-fields");
      const project = this.projects.get(item.project_id);
      const module = project?.modules.find((candidate) => candidate.module_id === item.module_id);
      this.addField(fields, "\u9879\u76EE", project?.display_name ?? item.project_id);
      this.addField(fields, "\u6A21\u5757", module?.name ?? item.module_id ?? "not_applicable");
      this.addField(fields, "Worktree", item.worktree ? `${item.worktree.worktree_id}${item.worktree.branch ? ` \xB7 ${item.worktree.branch}` : ""}` : "not_applicable");
      this.addField(
        fields,
        "Worktree \u51BB\u7ED3\u6279\u6B21",
        item.worktree?.observed_scan_run_id ?? "not_applicable"
      );
      this.addField(fields, "\u8DEF\u5F84", item.relative_path ?? "not_applicable");
      const locatorStatus = element("span", "copy-status");
      const locator = JSON.stringify(item.locator);
      this.addFieldNode(fields, "Locator", copyValue(locator, "\u590D\u5236 Evidence locator", locatorStatus));
      this.addField(fields, "\u5185\u5BB9\u54C8\u5E0C", item.content_sha256 ?? "not_applicable");
      this.addField(fields, "Facets", relation.supported_facets.join(", ") || "none");
      wrapper.append(heading, summary, fields, locatorStatus);
      return wrapper;
    }
    addField(list, label, value) {
      this.addFieldNode(list, label, document.createTextNode(displayText(value)));
    }
    addFieldNode(list, label, value) {
      list.append(element("dt", void 0, label));
      const description = element("dd");
      description.append(value);
      list.append(description);
    }
    renderGaps(route) {
      const view = element("div", "view");
      view.append(element("h2", "view-title", "\u77E5\u8BC6\u7F3A\u53E3"), element("p", "lede", "\u6309\u4F5C\u7528\u57DF\u548C\u4E25\u91CD\u5EA6\u4FDD\u7559\u5C1A\u4E0D\u80FD\u53EF\u9760\u8BB2\u8FF0\u7684\u5185\u5BB9\u3002"));
      const toolbar = element("div", "toolbar compact-toolbar");
      const projectFilter = this.filterSelect(
        "\u9879\u76EE",
        "project",
        route.query.project ?? "",
        [
          ["", "\u5168\u90E8\u9879\u76EE"],
          ...this.bundle.projects.map((project) => [project.project_id, project.display_name])
        ],
        route
      );
      const moduleFilter = this.filterSelect(
        "\u6A21\u5757",
        "module",
        route.query.module ?? "",
        [
          ["", "\u5168\u90E8\u6A21\u5757"],
          ...this.bundle.projects.flatMap(
            (project) => project.modules.map((module) => [
              module.module_id,
              `${project.display_name} / ${module.name}`
            ])
          )
        ],
        route
      );
      const gapDimensions = [
        .../* @__PURE__ */ new Set([
          ...this.bundle.role_lens.dimensions.map((dimension) => dimension.key),
          ...this.bundle.knowledge_gaps.map((gap) => gap.dimension)
        ])
      ].sort((left, right) => left.localeCompare(right, "zh-CN"));
      const dimensionFilter = this.filterSelect(
        "\u5C97\u4F4D\u7EF4\u5EA6",
        "dimension",
        route.query.dimension ?? "",
        [
          ["", "\u5168\u90E8\u5C97\u4F4D\u7EF4\u5EA6"],
          ...gapDimensions.map((dimension) => [
            dimension,
            this.bundle.role_lens.dimensions.find((item) => item.key === dimension)?.display_name ?? dimension
          ])
        ],
        route
      );
      const severityFilter = this.filterSelect(
        "\u4E25\u91CD\u5EA6",
        "severity",
        route.query.severity ?? "",
        [
          ["", "\u5168\u90E8\u4E25\u91CD\u5EA6"],
          ["low", "\u4F4E"],
          ["medium", "\u4E2D"],
          ["high", "\u9AD8"],
          ["critical", "\u4E25\u91CD"]
        ],
        route
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
      view.append(element("p", "result-count", `${gaps.length} \u4E2A\u77E5\u8BC6\u7F3A\u53E3`));
      const list = element("div", "gap-list");
      for (const gap of gaps) {
        const details = element("details", "gap-item focus-item");
        details.tabIndex = 0;
        details.open = route.query.gap === gap.gap_id;
        const summary = element("summary", "gap-summary");
        const hanging = element("span", "hanging-label");
        hanging.append(statusTag(gap.severity), element("span", void 0, gap.dimension));
        const description = element("div", "claim-statement");
        appendTokens(description, gap.description_tokens);
        description.append(element("div", "claim-meta", `${gap.scope_kind} \xB7 ${gap.status}`));
        summary.append(hanging, description);
        const body = element("div", "gap-body");
        body.append(
          element("p", void 0, `\u5019\u9009\u8FFD\u95EE\uFF1A\u54EA\u4E9B\u8BC1\u636E\u6216\u4E0A\u4E0B\u6587\u80FD\u89E3\u51B3\u8FD9\u4E2A\u7F3A\u53E3\uFF1F`),
          element("p", "remediation", `\u5EFA\u8BAE\u52A8\u4F5C\uFF1A${gap.resolution_kind}`)
        );
        details.append(summary, body);
        list.append(details);
      }
      view.append(gaps.length ? list : element("p", "empty-state", "\u5F53\u524D\u7B5B\u9009\u6CA1\u6709\u77E5\u8BC6\u7F3A\u53E3\u3002"));
      return view;
    }
    renderInterview(route) {
      const view = element("div", "view");
      view.append(element("h2", "view-title", "\u9762\u8BD5\u4E0E\u590D\u4E60"), element("p", "lede", "\u95EE\u9898\u53EA\u5F15\u7528\u51BB\u7ED3 Claim \u6216\u660E\u786E\u7684 KnowledgeGap\u3002"));
      const continuityFilter = this.filterSelect(
        "\u590D\u4E60\u8FDE\u7EED\u6027",
        "continuity",
        route.query.continuity ?? "",
        [
          ["", "\u5168\u90E8\u8FDE\u7EED\u6027\u72B6\u6001"],
          ["new", "\u5F85\u9996\u6B21\u590D\u4E60"],
          ["continued", "\u72B6\u6001\u5EF6\u7EED"],
          ["reassess_required", "\u9700\u8981\u91CD\u8BC4"]
        ],
        route
      );
      const toolbar = element("div", "toolbar single-filter-toolbar");
      toolbar.append(continuityFilter);
      view.append(toolbar);
      const pitch = element("section", "interview-section");
      pitch.append(element("h3", "section-title", "\u4E24\u5206\u949F\u8BB2\u89E3\u9AA8\u67B6"));
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
      questionSection.append(element("h3", "section-title", "\u5206\u5C42\u9898\u5E93\u4E0E\u8FFD\u95EE"));
      const questions = element("div", "question-list");
      for (const question of this.bundle.interview.questions) {
        const details = element("details", "question-item focus-item");
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
        element("h3", "section-title", "\u51BB\u7ED3\u590D\u4E60\u72B6\u6001"),
        element("p", void 0, `\u622A\u6B62 ${this.bundle.review.cutoff_at} \xB7 ${this.bundle.review.status}`)
      );
      let bindings = [...this.bundle.review.bindings];
      if (route.query.continuity) {
        bindings = bindings.filter(
          (binding) => binding.continuity_status === route.query.continuity
        );
      }
      if (route.query.target) {
        bindings = bindings.filter(
          (binding) => binding.review_target_id === route.query.target || binding.review_target_binding_id === route.query.target
        );
      }
      const masteryCounts = /* @__PURE__ */ new Map();
      for (const binding of bindings) {
        const level = masteryLabel(binding.mastery_level);
        masteryCounts.set(level, (masteryCounts.get(level) ?? 0) + 1);
      }
      if (masteryCounts.size) {
        const mastery = element("ul", "mastery-summary");
        mastery.setAttribute("aria-label", "\u638C\u63E1\u5EA6\u5206\u5E03");
        for (const [level, count] of masteryCounts) {
          const segment = element("li", "mastery-segment", `${level} ${count}`);
          mastery.append(segment);
        }
        review.append(mastery);
      }
      review.append(element("p", "result-count", `${bindings.length} \u4E2A\u590D\u4E60\u76EE\u6807`));
      for (const binding of bindings) {
        const row = element("div", "review-binding");
        row.dataset.continuity = binding.continuity_status;
        const heading = element("p", "review-binding-heading");
        if (binding.review_target_id) {
          const target = element("a", "review-target-link", "\u590D\u4E60\u76EE\u6807");
          target.href = `#/v1/interview/target/${encodeURIComponent(binding.review_target_id)}`;
          heading.append(target);
        }
        heading.append(
          statusTag(binding.continuity_status),
          document.createTextNode(
            displayText(
              ` \u5F53\u524D\u638C\u63E1\u5EA6\uFF1A${masteryLabel(binding.mastery_level)} \xB7 ${binding.next_review_at ?? "\u672A\u8BBE\u7F6E\u590D\u4E60\u65E5\u671F"}`
            )
          )
        );
        row.append(heading);
        if (binding.summary) {
          row.append(element("p", "review-summary", binding.summary));
        }
        if (binding.weak_points.length) {
          row.append(element("p", "review-weak-points", `\u8584\u5F31\u70B9\uFF1A${binding.weak_points.join("\uFF1B")}`));
        }
        if (binding.historical_review) {
          const historical = binding.historical_review;
          const history2 = element("div", "review-history");
          history2.append(
            element("p", "review-history-heading", "\u4E0A\u6B21\u590D\u76D8\uFF08\u4EC5\u4F9B\u5386\u53F2\u53C2\u8003\uFF0C\u4E0D\u4EE3\u8868\u5F53\u524D\u638C\u63E1\u5EA6\uFF09"),
            element(
              "p",
              "review-history-meta",
              `${masteryLabel(historical.mastery_level)} \xB7 ${historical.reviewed_at} \xB7 ${historical.next_review_at ?? "\u672A\u8BBE\u7F6E\u590D\u4E60\u65E5\u671F"}`
            ),
            element("p", "review-history-summary", historical.summary)
          );
          if (historical.weak_points.length) {
            history2.append(
              element(
                "p",
                "review-history-weak-points",
                `\u5386\u53F2\u8584\u5F31\u70B9\uFF1A${historical.weak_points.join("\uFF1B")}`
              )
            );
          }
          row.append(history2);
        }
        review.append(row);
      }
      const copyStatus = element("p", "copy-status");
      review.append(
        copyValue(this.bundle.review.skill_invocation, "\u590D\u5236\u590D\u4E60\u72B6\u6001\u66F4\u65B0 Skill \u8C03\u7528", copyStatus),
        copyStatus
      );
      view.append(pitch, questionSection, review);
      return view;
    }
    renderRouteError(message) {
      const error = element("div", "route-error");
      error.append(element("h2", "section-title", "\u65E0\u6CD5\u6253\u5F00\u6DF1\u94FE"), element("p", void 0, message));
      return error;
    }
    renderFooter() {
      return element("footer", "footer", "/ \u68C0\u7D22 \xB7 j/k \u79FB\u52A8 \xB7 Enter \u6253\u5F00 \xB7 e \u5C55\u5F00 Evidence \xB7 Esc \u8FD4\u56DE\u68C0\u7D22");
    }
    handleKeyboard(event) {
      const target = event.target;
      const isField = target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement || target instanceof HTMLElement && target.isContentEditable;
      const isInteractive = isField || target instanceof HTMLAnchorElement || target instanceof HTMLButtonElement;
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
      const items = [...document.querySelectorAll(".focus-item")];
      if (!items.length) return;
      const activeIndex = items.findIndex((item) => item === document.activeElement || item.contains(document.activeElement));
      if (event.key === "j" || event.key === "k") {
        event.preventDefault();
        const offset = event.key === "j" ? 1 : -1;
        const next = activeIndex < 0 ? offset > 0 ? 0 : items.length - 1 : (activeIndex + offset + items.length) % items.length;
        items[next]?.focus();
      } else if (event.key === "Enter" && document.activeElement instanceof HTMLElement) {
        const active = document.activeElement;
        const details = active.closest("details");
        if (details) {
          event.preventDefault();
          details.open = !details.open;
        } else if (active.matches(".project-item")) {
          const link = active.querySelector("a[href]");
          if (link) {
            event.preventDefault();
            link.click();
          }
        }
      } else if (event.key === "e") {
        event.preventDefault();
        const active = document.activeElement?.closest("details");
        if (active) active.open = !active.open;
      }
    }
    focusSearchOrNavigate() {
      const search = document.getElementById("report-search");
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
    expandForPrint() {
      this.printState.clear();
      for (const details of document.querySelectorAll("details")) {
        this.printState.set(details, details.open);
        details.open = true;
      }
    }
    restoreAfterPrint() {
      for (const [details, wasOpen] of this.printState) details.open = wasOpen;
      this.printState.clear();
    }
  };
  var app = document.getElementById("app");
  if (!app) {
    throw new Error("GoodJob dashboard root is unavailable");
  }
  try {
    new Dashboard(app, readBundle()).mount();
  } catch (error) {
    const failure = element("div", "route-error");
    failure.append(
      element("h1", "section-title", "\u65E0\u6CD5\u6253\u5F00\u51BB\u7ED3\u62A5\u544A"),
      element("p", void 0, error instanceof Error ? error.message : "\u672A\u77E5\u62A5\u544A\u9519\u8BEF")
    );
    app.replaceChildren(failure);
  }
})();

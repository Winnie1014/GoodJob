(function () {
  "use strict";

  var CONTRACT = 1;
  var ROUTE_VERSION = "v1";

  var bundle = JSON.parse(document.getElementById("gj-bundle").textContent);
  if (bundle.contract_version !== CONTRACT) {
    throw new Error("unsupported report bundle contract version");
  }

  /* ---------- safe DOM primitives: text nodes only ---------- */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.appendChild(document.createTextNode(String(text)));
    return node;
  }

  function put(parent) {
    for (var i = 1; i < arguments.length; i += 1) {
      if (arguments[i]) parent.appendChild(arguments[i]);
    }
    return parent;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  var ICON_PATHS = {
    check: ["M8 1.6a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 0 0 0-12.8z", "M4.9 8.2 7 10.3l4.1-4.5"],
    warn: ["M8 2 15 14H1z", "M8 6.2v3.9", "M8 11.9v.2"],
    cross: ["M8 1.6a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 0 0 0-12.8z", "M5.6 5.6l4.8 4.8", "M10.4 5.6l-4.8 4.8"],
    dash: ["M3.2 8h9.6"],
    clock: ["M8 1.6a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 0 0 0-12.8z", "M8 4.5V8l2.5 1.7"],
    lock: ["M3.6 7.3h8.8v6.9H3.6z", "M5.7 7.3V5.2a2.3 2.3 0 0 1 4.6 0v2.1"],
    ask: ["M8 1.6a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 0 0 0-12.8z", "M6.4 6.1a1.7 1.7 0 1 1 2.3 1.6v1.1", "M8.7 11.6v.2"],
    minus: ["M3.2 8h9.6"]
  };

  function icon(name, tone) {
    var paths = ICON_PATHS[name];
    if (!paths) throw new Error("unknown icon: " + name);
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.35");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("aria-hidden", "true");
    if (tone) svg.setAttribute("class", "i-" + tone);
    for (var i = 0; i < paths.length; i += 1) {
      var p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", paths[i]);
      svg.appendChild(p);
    }
    return svg;
  }

  /* ---------- ReportInlineToken renderer: closed kind set ---------- */

  var TOKEN_KINDS = {
    text: function (t) { return document.createTextNode(t.value); },
    code: function (t) { return el("code", "data", t.value); },
    emphasis: function (t) { return el("strong", null, t.value); },
    claim_ref: function (t) { return el("span", "data", t.value); },
    evidence_ref: function (t) { return el("span", "data", t.value); },
    gap_ref: function (t) { return el("span", "data", t.value); },
    inert_url: function (t) {
      return put(el("span", "inert"), el("span", "data", t.value), el("em", null, "（外部链接，未激活）"));
    }
  };

  function tokens(list, tag, cls) {
    var node = el(tag || "span", cls || null);
    if (!Array.isArray(list)) return node;
    for (var i = 0; i < list.length; i += 1) {
      var t = list[i];
      var render = TOKEN_KINDS[t && t.kind];
      if (!render) throw new Error("unknown ReportInlineToken kind: " + (t && t.kind));
      node.appendChild(render(t));
    }
    return node;
  }

  /* ---------- state vocabularies: icon + label, never colour alone ---------- */

  var FRESHNESS = {
    current: { label: "当前有效", icon: "check", tone: "good" },
    stale: { label: "已过期", icon: "clock", tone: "warning", note: "只作历史限制" },
    missing: { label: "已失效", icon: "cross", tone: "critical", note: "不支撑任何当前结论" },
    plan: { label: "仅已规划", icon: "dash", tone: null, note: "只能说“已规划/已文档化”" }
  };
  var DISPOSITION = {
    fresh: { label: "本次重扫", icon: "check", tone: "good" },
    carried_forward: { label: "沿用旧快照", icon: "clock", tone: "warning" },
    failed_no_baseline: { label: "无可用基线", icon: "cross", tone: "critical" },
    excluded: { label: "已排除", icon: "minus", tone: null }
  };
  var SEVERITY = {
    warning: { label: "注意", icon: "warn", tone: "warning" },
    serious: { label: "较重", icon: "warn", tone: "serious" },
    critical: { label: "严重", icon: "cross", tone: "critical" }
  };
  var LIMIT_ICON = {
    carried_forward: "clock",
    failed_no_baseline: "cross",
    stale_evidence: "clock",
    missing_role_context: "ask"
  };
  var SUPPORT = {
    single_source: { label: "单一来源", pips: 1 },
    cross_checked: { label: "交叉验证", pips: 2 },
    user_confirmed: { label: "本人确认", pips: 3 },
    conflicted: { label: "存在反证", pips: 0 }
  };
  // 边框样式承担区分：无框 = 首次，实线 = 延续，虚线 = 需重评（呈现契约 §4）。
  var CONTINUITY = {
    "new": { label: "首次出现", cls: "tag" },
    continued: { label: "延续掌握度", cls: "tag box" },
    reassess_required: { label: "需重评", cls: "tag dash" }
  };
  var NARRATIVE = {
    capability: "可讲解能力",
    learning_candidate: "候选学习点",
    objective_implementation: "客观实现",
    personal_attribution: "个人归因"
  };
  var MASTERY = {
    strong: "掌握扎实",
    medium: "掌握一般",
    weak: "掌握薄弱",
    unassessed: "未评估"
  };
  var RELATION = { supports: "支撑", contradicts: "反驳", contextualizes: "背景" };
  var COMMIT_STATE = {
    committed: "已提交",
    modified: "工作树已改",
    untracked: "未跟踪",
    historical: "历史版本",
    not_applicable: "不适用"
  };
  var SCOPE_KIND = { role_global: "岗位级", project: "项目级", module: "模块级" };
  var GAP_STATUS = { open: "待补充", resolved: "已解决", superseded: "已替代" };

  function stateTag(vocab, key, cls) {
    var v = vocab[key] || { label: String(key), icon: "dash", tone: null };
    var tag = el("span", cls || "tag");
    if (v.icon) tag.appendChild(icon(v.icon, v.tone));
    tag.appendChild(document.createTextNode(v.label));
    return tag;
  }

  function supportTag(level) {
    var v = SUPPORT[level] || { label: String(level), pips: 0 };
    var tag = el("span", "tag");
    if (level === "conflicted") {
      tag.appendChild(icon("warn", "critical"));
    } else {
      var pips = el("span", "pips");
      for (var i = 0; i < 3; i += 1) pips.appendChild(el("i", i < v.pips ? "on" : null));
      tag.appendChild(pips);
    }
    tag.appendChild(document.createTextNode(v.label));
    return tag;
  }

  /* ---------- fixed-point formatting: integers in, no float maths ---------- */

  function bpsPercent(bps) {
    var frac = bps % 100;
    return Math.trunc(bps / 100) + "." + (frac < 10 ? "0" + frac : String(frac)) + "%";
  }

  function widthOf(numerator, denominator) {
    return bpsPercent(Math.trunc((numerator * 10000) / denominator));
  }

  /* ---------- derived collections ---------- */

  var projects = bundle.projects;
  var eligible = projects.filter(function (p) { return p.eligible; })
    .sort(function (a, b) { return a.rank - b.rank; });
  var ineligible = projects.filter(function (p) { return !p.eligible; });
  var allClaims = [];
  projects.forEach(function (p) {
    p.claims.forEach(function (c) { allClaims.push({ project: p, claim: c }); });
  });

  function claimById(id) {
    for (var i = 0; i < allClaims.length; i += 1) {
      if (allClaims[i].claim.claim_id === id) return allClaims[i];
    }
    return null;
  }

  function projectById(id) {
    for (var i = 0; i < projects.length; i += 1) {
      if (projects[i].project_id === id) return projects[i];
    }
    return null;
  }

  function moduleName(p, moduleId) {
    for (var i = 0; i < p.modules.length; i += 1) {
      if (p.modules[i].module_id === moduleId) return p.modules[i].name;
    }
    return moduleId;
  }

  function go(hash) { window.location.hash = hash; }

  function link(text, hash, cls) {
    var b = el("button", cls || "quiet", text);
    b.type = "button";
    b.addEventListener("click", function () { go(hash); });
    return b;
  }

  function copyable(text, cls) {
    var b = el("button", cls, text);
    b.type = "button";
    b.title = "点击复制";
    b.setAttribute("aria-label", "复制 " + text);
    b.addEventListener("click", function () {
      var flag = b.querySelector(".copied");
      if (!flag) {
        flag = el("span", "copied", " 已复制");
        b.appendChild(flag);
      }
      function done(msg) {
        flag.textContent = " " + msg;
        window.setTimeout(function () { if (flag.parentNode) flag.parentNode.removeChild(flag); }, 1300);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done("已复制"); },
          function () { done("请手动选中"); });
      } else {
        done("请手动选中");
      }
    });
    return b;
  }

  /* ---------- chrome: assay strip, masthead, coverage band ---------- */

  function renderAssay() {
    var bar = document.getElementById("assay");
    bar.textContent = "";
    var partial = bundle.snapshot_status !== "completed";
    var seal = el("span", "seal");
    seal.appendChild(icon(partial ? "warn" : "check", partial ? "warning" : "good"));
    seal.appendChild(document.createTextNode(partial ? "带缺口的快照" : "完整快照"));
    put(bar, seal,
      el("span", "data", "run " + bundle.preparation_run_id.slice(-8)),
      el("span", "data", "bundle " + bundle.bundle_sha256.slice(0, 8)),
      el("span", "data", bundle.generated_at.slice(0, 10) + " " + bundle.generated_at.slice(11, 16)),
      el("span", "data", bundle.primary_language),
      el("span", "tail", "已冻结 · 只读"));
  }

  function renderMasthead() {
    var head = document.getElementById("masthead");
    head.textContent = "";
    var lens = bundle.role_lens;
    head.appendChild(el("h1", null, lens.target_role));
    var sub = el("div", "sub");
    put(sub,
      el("span", "note", lens.level + (lens.level_source === "owner_override" ? "（你指定的职级）" : "（推断职级）")),
      el("span", "note", lens.jd_source === "local_file" ? "读取了本地 JD" : "没有 JD，按假设生成"));
    head.appendChild(sub);
    head.appendChild(tokens(lens.assumptions, "p", "note measure"));
  }

  var BAND_SEGMENTS = [
    ["fresh", "seg-scored", "本次重扫"],
    ["carried_forward", "seg-carried", "沿用旧快照"],
    ["failed_no_baseline", "seg-nobase", "无可用基线"],
    ["excluded", "seg-excluded", "已排除"]
  ];

  function renderBand() {
    var host = document.getElementById("band");
    host.textContent = "";
    var track = el("div", "track");
    var keys = el("div", "keys");
    BAND_SEGMENTS.forEach(function (spec) {
      var count = projects.filter(function (p) {
        return p.snapshot_disposition === spec[0];
      }).length;
      if (!count) return;
      var seg = el("span", spec[1]);
      seg.style.flexGrow = String(count);
      track.appendChild(seg);
      var key = el("span");
      put(key, el("i", spec[1]), el("b", null, count), document.createTextNode(" " + spec[2]));
      keys.appendChild(key);
    });
    host.appendChild(el("p", "label",
      "工作区覆盖 · " + projects.length + " 个项目里有 " + eligible.length + " 个参与评分"));
    put(host, track, keys);
  }

  function renderFooter() {
    var foot = document.getElementById("footer");
    foot.textContent = "";
    [["/", "检索"], ["j k", "上下移动"], ["e", "展开证据"], ["Esc", "回到检索"]].forEach(function (pair) {
      var item = el("span", "note");
      put(item, el("span", "data", pair[0]), document.createTextNode(" " + pair[1]));
      foot.appendChild(item);
    });
    foot.appendChild(el("span", "note", "这份卷宗只读；改动复习状态要回到 Skill 并重新生成快照。"));
  }

  /* ---------- components ---------- */

  function evidenceBlock(ev, project) {
    var f = FRESHNESS[ev.freshness] || FRESHNESS.current;
    var row = el("div", "ev f-" + ev.freshness);

    var kind = el("div", "kind");
    put(kind, icon(f.icon, f.tone), el("em", null, ev.evidence_kind));
    row.appendChild(kind);

    var body = el("div", "body");
    body.appendChild(copyable(ev.locator, "loc data"));

    // worktree 只在项目确实存在分支差异时呈现，否则是噪声（呈现契约 §5）。
    var cite = [ev.content_hash, COMMIT_STATE[ev.commit_state] || ev.commit_state];
    if (project && project.worktree_divergent && ev.worktree) cite.push("worktree " + ev.worktree);
    cite.push(RELATION[ev.relation] || ev.relation);
    if (ev.supported_facets.length) cite.push(ev.supported_facets.join(" "));
    body.appendChild(el("p", "cite data", cite.join("  ·  ")));

    var why = el("p", "why", ev.summary);
    if (f.note) {
      why.appendChild(document.createTextNode(" "));
      why.appendChild(put(el("span", "tag"), icon(f.icon, f.tone), document.createTextNode(f.note)));
    }
    body.appendChild(why);

    if (ev.equivalent_sources.length) {
      var det = el("details");
      det.appendChild(el("summary", null,
        "另有 " + ev.equivalent_sources.length + " 个工作树内容等价，展开全部来源"));
      var ul = el("ul", "stack");
      ev.equivalent_sources.forEach(function (src) {
        ul.appendChild(el("li", "data", src.worktree + " · " + src.locator));
      });
      det.appendChild(ul);
      body.appendChild(det);
    }

    row.appendChild(body);
    return row;
  }

  function claimBlock(entry, collapsed) {
    var p = entry.project;
    var c = entry.claim;
    var block = el("article", "claim");
    block.id = "claim-" + c.claim_id;
    block.setAttribute("tabindex", "-1");
    block.setAttribute("data-claim", c.claim_id);

    var head = el("div", "head");
    var scope = el("div", "scope");
    scope.appendChild(el("span", "proj", p.name));
    scope.appendChild(el("span", "mod data", c.module_id ? moduleName(p, c.module_id) : "项目级"));
    head.appendChild(scope);
    var right = el("div");
    right.appendChild(tokens(c.statement, "p", "stmt"));
    var tags = el("div", "tags");
    tags.appendChild(supportTag(c.support_level));
    tags.appendChild(el("span", "tag", NARRATIVE[c.narrative_kind] || c.narrative_kind));
    c.facets.forEach(function (f) { tags.appendChild(el("span", "tag box", f)); });
    right.appendChild(tags);
    if (c.support_level === "conflicted") {
      right.appendChild(el("p", "conflict",
        "两条证据互相反驳，哪一个在生产环境生效没有证据。讲这一条时必须同时说出冲突，不能只挑一边。"));
    }
    head.appendChild(right);
    block.appendChild(head);

    var evidence = el("div");
    c.evidence.forEach(function (ev) { evidence.appendChild(evidenceBlock(ev, p)); });

    if (collapsed) {
      var det = el("details");
      det.appendChild(el("summary", null, "展开 " + c.evidence.length + " 条证据出处"));
      det.appendChild(evidence);
      block.appendChild(det);
    } else {
      block.appendChild(evidence);
    }
    return block;
  }

  function section(heading, blurb) {
    var sec = el("section");
    sec.appendChild(el("h2", null, heading));
    if (blurb) sec.appendChild(el("p", "note measure", blurb));
    sec.appendChild(el("hr", "rule"));
    return sec;
  }

  /* ---------- views ---------- */

  function viewOverview() {
    var out = document.createDocumentFragment();

    var limits = section("先看清边界",
      "这些是本次扫描没能覆盖或已经过期的部分。任何叙事都不能盖过它们。");
    if (!bundle.degradations.length) {
      limits.appendChild(el("p", "note", "本次没有记录到覆盖限制。"));
    } else {
      bundle.degradations.forEach(function (d) {
        var item = el("div", "limit");
        var side = el("div", "side");
        var sev = SEVERITY[d.severity] || SEVERITY.warning;
        put(side, icon(LIMIT_ICON[d.kind] || sev.icon, sev.tone), el("span", null, sev.label));
        item.appendChild(side);
        var body = el("div");
        body.appendChild(el("h3", null, d.headline));
        body.appendChild(tokens(d.impact, "p", "note measure"));
        var fix = el("p", "fix measure");
        fix.appendChild(el("b", null, "怎么办："));
        fix.appendChild(tokens(d.remediation));
        body.appendChild(fix);
        body.appendChild(link("查看受影响的项", d.target, "quiet noprint"));
        item.appendChild(body);
        limits.appendChild(item);
      });
    }
    out.appendChild(limits);

    var lens = bundle.role_lens;
    var weights = section("岗位镜头",
      "这次排序用的权重。权重之和固定为 10000 bps，改岗位会改权重，但不会改证据。");
    var maxW = lens.dimensions.reduce(function (m, d) { return Math.max(m, d.weight_bps); }, 1);
    lens.dimensions.forEach(function (d) {
      var row = el("div", "wgt");
      row.appendChild(el("span", null, d.label));
      var bar = el("span", "bar");
      var fill = el("span");
      fill.style.width = widthOf(d.weight_bps, maxW);
      bar.appendChild(fill);
      row.appendChild(bar);
      var amt = el("span", "amt data", bpsPercent(d.weight_bps));
      amt.title = d.weight_bps + " bps";
      row.appendChild(amt);
      weights.appendChild(row);
    });
    out.appendChild(weights);

    var rank = section("项目排序",
      "总分 = 基础分 × 覆盖度。覆盖度低的项目分数会被压下来，那不代表项目本身差。");
    eligible.forEach(function (p) {
      var row = el("div", "rank");
      var side = el("div", "side");
      put(side, el("span", "no", p.rank < 10 ? "0" + p.rank : String(p.rank)),
        el("span", "final", String(p.final_score_milli)),
        el("span", "of", "/ 1000"));
      row.appendChild(side);
      var body = el("div");
      var h = el("h3");
      h.appendChild(link(p.name, "#/" + ROUTE_VERSION + "/project/" + p.project_id));
      body.appendChild(h);
      var tags = el("div", "tags");
      tags.appendChild(stateTag(DISPOSITION, p.snapshot_disposition));
      tags.appendChild(el("span", "tag", p.claims.length + " 条断言"));
      body.appendChild(tags);
      body.appendChild(tokens(p.rationale, "p", "note measure"));
      body.appendChild(el("p", "calc data",
        "基础 " + p.base_score_milli + " × 覆盖 " + bpsPercent(p.coverage_bps) +
        "（" + p.coverage_bps + " bps） = " + p.final_score_milli));
      var track = el("div", "track");
      var fill = el("span");
      fill.style.width = widthOf(p.final_score_milli, 1000);
      track.appendChild(fill);
      body.appendChild(track);
      row.appendChild(body);
      rank.appendChild(row);
    });
    ineligible.forEach(function (p) {
      var row = el("div", "rank-out");
      row.appendChild(put(el("div", "side"), stateTag(DISPOSITION, p.snapshot_disposition)));
      var body = el("div");
      body.appendChild(el("h3", null, p.name));
      body.appendChild(tokens(p.rationale, "p", "note measure"));
      row.appendChild(body);
      rank.appendChild(row);
    });
    out.appendChild(rank);
    return out;
  }

  function viewProject(projectId, moduleId) {
    var out = document.createDocumentFragment();
    var p = projectById(projectId);
    if (!p) {
      out.appendChild(section("没有这个项目", "路由里的项目 ID 不在本快照内：" + projectId));
      return out;
    }
    var blurb = p.eligible
      ? "排名 " + p.rank + " · 总分 " + p.final_score_milli + " · 覆盖度 " + bpsPercent(p.coverage_bps)
      : "这个项目只进入覆盖范围，不参与评分和排序。";
    var sec = section(p.name + (moduleId ? " / " + moduleName(p, moduleId) : ""), blurb);
    sec.appendChild(put(el("div", "tags"), stateTag(DISPOSITION, p.snapshot_disposition)));
    sec.appendChild(tokens(p.rationale, "p", "note measure"));

    var claims = p.claims.filter(function (c) { return !moduleId || c.module_id === moduleId; });
    if (!claims.length) {
      sec.appendChild(el("p", "note", "本快照里这个范围没有可讲的断言。"));
    }
    claims.forEach(function (c) { sec.appendChild(claimBlock({ project: p, claim: c }, false)); });
    out.appendChild(sec);
    return out;
  }

  var filters = { freshness: "", support: "", disposition: "", q: "" };

  var FILTER_GROUPS = [
    ["freshness", "证据时效", ["current", "stale", "missing", "plan"], FRESHNESS],
    ["support", "证据强度", ["single_source", "cross_checked", "user_confirmed", "conflicted"], SUPPORT],
    ["disposition", "快照状态", ["fresh", "carried_forward"], DISPOSITION]
  ];

  function viewEvidence() {
    var out = document.createDocumentFragment();
    var sec = section("查证据",
      "跨项目按时效、强度和快照状态收敛，找出哪些话现在还能讲。");

    FILTER_GROUPS.forEach(function (g) {
      var row = el("div", "filterset");
      row.appendChild(el("span", "label", g[1]));
      var set = el("div", "facets");
      g[2].forEach(function (val) {
        var v = g[3][val] || { label: val };
        var b = el("button", null, v.label);
        b.type = "button";
        b.setAttribute("aria-pressed", filters[g[0]] === val ? "true" : "false");
        b.addEventListener("click", function () {
          filters[g[0]] = filters[g[0]] === val ? "" : val;
          var parts = [];
          if (filters.freshness) parts.push("freshness=" + encodeURIComponent(filters.freshness));
          if (filters.support) parts.push("support=" + encodeURIComponent(filters.support));
          if (filters.disposition) parts.push("disposition=" + encodeURIComponent(filters.disposition));
          var next = "#/" + ROUTE_VERSION + "/evidence" + (parts.length ? "?" + parts.join("&") : "");
          if (window.location.hash === next) { render(); } else { go(next); }
        });
        set.appendChild(b);
      });
      row.appendChild(set);
      sec.appendChild(row);
    });

    var matched = matchClaims();
    sec.appendChild(el("p", "count",
      "命中 " + matched.length + " / " + allClaims.length + " 条断言" +
      (filters.q ? "，检索词「" + filters.q + "」" : "")));
    if (!matched.length) {
      sec.appendChild(el("p", "note", "没有符合当前条件的断言。放宽一个筛选试试。"));
    }
    matched.forEach(function (entry) { sec.appendChild(claimBlock(entry, true)); });
    out.appendChild(sec);
    return out;
  }

  function matchClaims() {
    var fromQuery = null;
    if (filters.q) {
      fromQuery = Object.create(null);
      var needle = filters.q.toLowerCase();
      Object.keys(bundle.search_index).forEach(function (term) {
        if (term.toLowerCase().indexOf(needle) !== -1) {
          bundle.search_index[term].forEach(function (id) { fromQuery[id] = true; });
        }
      });
    }
    return allClaims.filter(function (entry) {
      var c = entry.claim;
      if (fromQuery && !fromQuery[c.claim_id]) return false;
      if (filters.support && c.support_level !== filters.support) return false;
      if (filters.disposition && entry.project.snapshot_disposition !== filters.disposition) return false;
      if (filters.freshness) {
        return c.evidence.some(function (ev) { return ev.freshness === filters.freshness; });
      }
      return true;
    });
  }

  function viewGaps() {
    var out = document.createDocumentFragment();
    var sec = section("知识缺口",
      "证据不足、需要你补一句话的地方。跳过不阻断流程，但对应的叙事会一直保持降级。");
    if (!bundle.gaps.length) {
      sec.appendChild(el("p", "note", "本快照没有待补充的缺口。"));
    }
    bundle.gaps.forEach(function (g) {
      var item = el("div", "limit");
      var sev = SEVERITY[g.severity] || SEVERITY.warning;
      item.appendChild(put(el("div", "side"), icon(sev.icon, sev.tone), el("span", null, sev.label)));
      var body = el("div");
      body.appendChild(el("h3", null,
        (SCOPE_KIND[g.scope_kind] || g.scope_kind) + " · " + g.scope_label + " · " + g.dimension));
      body.appendChild(tokens(g.description, "p", "note measure"));
      if (g.follow_ups.length) {
        body.appendChild(el("p", "label", "下次访谈会问"));
        var ul = el("ul", "stack measure");
        g.follow_ups.forEach(function (q) { ul.appendChild(el("li", "note", q)); });
        body.appendChild(ul);
      }
      body.appendChild(put(el("div", "tags"),
        el("span", "tag", GAP_STATUS[g.status] || g.status),
        el("span", "tag mono", g.gap_key)));
      item.appendChild(body);
      sec.appendChild(item);
    });
    out.appendChild(sec);
    return out;
  }

  function viewInterview(targetId) {
    var out = document.createDocumentFragment();
    var sec = section("面试与复习",
      "掌握度冻结于 " + bundle.review_frozen_at.slice(0, 10) + " " +
      bundle.review_frozen_at.slice(11, 16) +
      "。这份卷宗改不了它：复盘要回到 Skill 落库，再显式生成一份新快照。");

    var list = bundle.interview.targets.filter(function (t) {
      return !targetId || t.review_target_id === targetId;
    });
    list.forEach(function (t) {
      var item = el("div", "limit");
      var cont = CONTINUITY[t.continuity_status] || { label: t.continuity_status, cls: "tag box" };
      var side = el("div", "side");
      side.appendChild(el("span", cont.cls, cont.label));
      item.appendChild(side);

      var body = el("div");
      body.appendChild(tokens(t.question, "p", "stmt measure"));
      var tags = el("div", "tags");
      put(tags,
        el("span", "tag", MASTERY[t.mastery_level] || t.mastery_level),
        el("span", "tag", t.project_label),
        t.next_review_at ? el("span", "tag box", "下次复习 " + t.next_review_at) : null);
      body.appendChild(tags);

      if (t.weak_points.length) {
        body.appendChild(el("p", "label", "薄弱点"));
        var ul = el("ul", "stack measure");
        t.weak_points.forEach(function (w) { ul.appendChild(el("li", "note", w)); });
        body.appendChild(ul);
      }
      if (t.follow_ups.length) {
        var det = el("details");
        det.appendChild(el("summary", null, "展开追问路径"));
        var ul2 = el("ul", "stack measure");
        t.follow_ups.forEach(function (q) { ul2.appendChild(el("li", "note", q)); });
        det.appendChild(ul2);
        body.appendChild(det);
      }

      var entry = claimById(t.claim_id);
      var actions = el("div", "tags noprint");
      if (entry) {
        actions.appendChild(link("查看支撑证据",
          "#/" + ROUTE_VERSION + "/project/" + entry.project.project_id + "/claim/" + t.claim_id));
      }
      var cmd = "$goodjob-career-review review --target " + t.review_target_id;
      actions.appendChild(copyable(cmd, "loc data"));
      body.appendChild(actions);
      item.appendChild(body);
      sec.appendChild(item);
    });
    out.appendChild(sec);
    return out;
  }

  /* ---------- nav ---------- */

  var VIEWS = [
    ["overview", "总览"],
    ["evidence", "查证据"],
    ["gaps", "知识缺口"],
    ["interview", "面试与复习"]
  ];

  var NARROW = window.matchMedia("(max-width: 767px)");

  function option(value, label, selected) {
    var opt = el("option", null, label);
    opt.value = value;
    if (selected) opt.selected = true;
    return opt;
  }

  function compactNav(route) {
    var wrap = el("div", "noprint");
    var label = el("label", "label", "视图与项目");
    label.setAttribute("for", "navselect");
    wrap.appendChild(label);
    var select = document.createElement("select");
    select.id = "navselect";
    var gv = document.createElement("optgroup");
    gv.label = "视图";
    VIEWS.forEach(function (v) {
      gv.appendChild(option("#/" + ROUTE_VERSION + "/" + v[0], v[1], route.view === v[0]));
    });
    select.appendChild(gv);
    var gp = document.createElement("optgroup");
    gp.label = "项目";
    projects.forEach(function (p) {
      var base = "#/" + ROUTE_VERSION + "/project/" + p.project_id;
      gp.appendChild(option(base, p.name,
        route.view === "project" && route.id === p.project_id && !route.moduleId));
      p.modules.forEach(function (m) {
        gp.appendChild(option(base + "/module/" + m.module_id, "　" + m.name,
          route.view === "project" && route.moduleId === m.module_id));
      });
    });
    select.appendChild(gp);
    select.addEventListener("change", function () { go(select.value); });
    wrap.appendChild(select);
    return wrap;
  }

  function renderNav(route) {
    var nav = document.getElementById("nav");
    nav.textContent = "";

    var box = el("div", "noprint");
    var label = el("label", "label", "检索");
    label.setAttribute("for", "search");
    box.appendChild(label);
    var input = el("input");
    input.type = "search";
    input.id = "search";
    input.value = filters.q;
    input.setAttribute("autocomplete", "off");
    input.setAttribute("placeholder", "分片、限流、幂等…");
    input.addEventListener("input", function () {
      filters.q = input.value.trim();
      if (parseRoute().view !== "evidence") {
        go("#/" + ROUTE_VERSION + "/evidence");
      } else {
        render(true);
      }
    });
    box.appendChild(input);
    nav.appendChild(box);

    if (NARROW.matches) {
      nav.appendChild(compactNav(route));
      return;
    }

    var ul = el("ul");
    VIEWS.forEach(function (v) {
      var b = el("button", null, v[1]);
      b.type = "button";
      b.setAttribute("aria-current", route.view === v[0] ? "true" : "false");
      b.addEventListener("click", function () { go("#/" + ROUTE_VERSION + "/" + v[0]); });
      ul.appendChild(put(el("li"), b));
    });
    nav.appendChild(ul);

    nav.appendChild(el("p", "label", "项目"));
    var pl = el("ul");
    projects.forEach(function (p) {
      var b = el("button", null, p.name);
      b.type = "button";
      b.setAttribute("aria-current",
        route.view === "project" && route.id === p.project_id && !route.moduleId ? "true" : "false");
      b.addEventListener("click", function () { go("#/" + ROUTE_VERSION + "/project/" + p.project_id); });
      pl.appendChild(put(el("li"), b));
      p.modules.forEach(function (m) {
        var mb = el("button", null, m.name);
        mb.type = "button";
        mb.setAttribute("aria-current", route.moduleId === m.module_id ? "true" : "false");
        mb.addEventListener("click", function () {
          go("#/" + ROUTE_VERSION + "/project/" + p.project_id + "/module/" + m.module_id);
        });
        pl.appendChild(put(el("li", "mod"), mb));
      });
    });
    nav.appendChild(pl);
  }

  /* ---------- router ---------- */

  function splitHash() {
    var raw = window.location.hash;
    if (raw.charAt(0) === "#") raw = raw.slice(1);
    var q = raw.indexOf("?");
    return { path: q === -1 ? raw : raw.slice(0, q), query: q === -1 ? "" : raw.slice(q + 1) };
  }

  function parseRoute() {
    var path = splitHash().path;
    if (!path || path.charAt(0) !== "/") return { view: "overview" };
    var parts = path.split("/").filter(function (s) { return s.length > 0; });
    if (parts[0] !== ROUTE_VERSION) return { view: "version_mismatch", got: parts[0] };
    var view = parts[1] || "overview";
    if (view === "project") {
      var route = { view: "project", id: parts[2] };
      for (var i = 3; i < parts.length; i += 2) {
        if (parts[i] === "module") route.moduleId = parts[i + 1];
        if (parts[i] === "claim") route.claimId = parts[i + 1];
      }
      return route;
    }
    if (view === "interview") return { view: "interview", id: parts[3] };
    return { view: view };
  }

  var lastHash = null;

  function syncFiltersFromHash() {
    var current = window.location.hash;
    if (current === lastHash) return;
    lastHash = current;
    var query = splitHash().query;
    filters.freshness = "";
    filters.support = "";
    filters.disposition = "";
    if (!query) return;
    query.split("&").forEach(function (pair) {
      var kv = pair.split("=");
      var value = decodeURIComponent(kv[1] || "");
      if (kv[0] === "freshness") filters.freshness = value;
      if (kv[0] === "disposition") filters.disposition = value;
      if (kv[0] === "support") filters.support = value;
    });
  }

  function render(keepFocus) {
    var focusId = keepFocus && document.activeElement ? document.activeElement.id : null;
    syncFiltersFromHash();
    var route = parseRoute();
    renderAssay();
    renderMasthead();
    renderBand();
    renderNav(route);
    renderFooter();

    var main = document.getElementById("main");
    main.textContent = "";
    if (route.view === "version_mismatch") {
      main.appendChild(section("这个链接来自别的契约版本",
        "本卷宗按 ReportBundle v" + CONTRACT + " 渲染，链接声明的是 " + String(route.got) +
        "。深链只在同一契约版本的快照里有效，所以这里不猜、直接停下。"));
    } else if (route.view === "project") {
      main.appendChild(viewProject(route.id, route.moduleId));
    } else if (route.view === "evidence") {
      main.appendChild(viewEvidence());
    } else if (route.view === "gaps") {
      main.appendChild(viewGaps());
    } else if (route.view === "interview") {
      main.appendChild(viewInterview(route.id));
    } else {
      main.appendChild(viewOverview());
    }

    if (focusId) {
      var back = document.getElementById(focusId);
      if (back) back.focus();
    }
    if (route.claimId) {
      var target = document.getElementById("claim-" + route.claimId);
      if (target) { target.focus(); target.scrollIntoView({ block: "start" }); }
    }
  }

  /* ---------- keyboard ---------- */

  var cursor = -1;

  function moveCursor(delta) {
    var cards = Array.prototype.slice.call(document.querySelectorAll("[data-claim]"));
    if (!cards.length) return;
    cursor = Math.min(Math.max(cursor + delta, 0), cards.length - 1);
    cards[cursor].focus();
    cards[cursor].scrollIntoView({ block: "nearest" });
  }

  document.addEventListener("keydown", function (e) {
    var tag = e.target && e.target.tagName;
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (e.key === "Escape") {
      var s = document.getElementById("search");
      if (s) s.focus();
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") {
      e.preventDefault();
      var input = document.getElementById("search");
      if (input) input.focus();
    } else if (e.key === "j") {
      e.preventDefault();
      moveCursor(1);
    } else if (e.key === "k") {
      e.preventDefault();
      moveCursor(-1);
    } else if (e.key === "e") {
      var host = e.target && e.target.getAttribute && e.target.getAttribute("data-claim")
        ? e.target : null;
      if (host) {
        var det = host.querySelector("details");
        if (det) det.open = !det.open;
      }
    }
  });

  window.addEventListener("hashchange", function () { cursor = -1; render(true); });
  NARROW.addEventListener("change", function () { render(); });
  render();
})();

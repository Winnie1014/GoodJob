export type TokenKind =
  | "text"
  | "code"
  | "emphasis"
  | "claim_ref"
  | "evidence_ref"
  | "gap_ref"
  | "inert_url";

export interface InlineToken {
  kind: TokenKind;
  value: string;
  ref_id?: string;
}

export interface SearchEntry {
  item_id: string;
  kind: string;
  route: string;
  search_text: string;
  project_id?: string | null;
  module_id?: string | null;
}

export interface RouteState {
  version: string;
  view: string;
  projectId?: string;
  moduleId?: string;
  query: Record<string, string>;
}

export interface FilterEvidence {
  evidence_id: string;
  evidence_kind: string;
  validity: string;
  commit_state: string;
}

export interface FilterEvidenceRelation {
  evidence_id: string;
  supported_facets: string[];
}

export interface FilterClaim {
  project_id: string;
  module_id: string | null;
  support_level: string;
  facets: string[];
  evidence_relations: FilterEvidenceRelation[];
}

export interface ProjectLimitation {
  project_id: string | null;
  kind: string;
}

const VISIBLE_CONTROLS: Record<string, string> = {
  "\u2028": "[U+2028]",
  "\u2029": "[U+2029]",
  "\u202a": "[U+202A]",
  "\u202b": "[U+202B]",
  "\u202c": "[U+202C]",
  "\u202d": "[U+202D]",
  "\u202e": "[U+202E]",
  "\u2066": "[U+2066]",
  "\u2067": "[U+2067]",
  "\u2068": "[U+2068]",
  "\u2069": "[U+2069]",
};

export function displayText(value: string): string {
  return value.replace(/[\u2028-\u202e\u2066-\u2069]/gu, (control) => VISIBLE_CONTROLS[control] ?? "[CONTROL]");
}

export function formatBps(value: number): string {
  return `${Math.floor(value / 100)}.${String(value % 100).padStart(2, "0")}%`;
}

export function scoreWidth(value: number): string {
  const bounded = Math.max(0, Math.min(1000, Math.trunc(value)));
  return `${Math.floor(bounded / 10)}.${String(bounded % 10).padStart(1, "0")}%`;
}

export function bpsWidth(value: number): string {
  const bounded = Math.max(0, Math.min(10000, Math.trunc(value)));
  return `${Math.floor(bounded / 100)}.${String(bounded % 100).padStart(2, "0")}%`;
}

export function tokensText(tokens: readonly InlineToken[]): string {
  return tokens.map((token) => token.value).join("");
}

export function parseRoute(hash: string): RouteState {
  const source = hash.startsWith("#") ? hash.slice(1) : hash;
  const [pathPart = "/v1/overview", queryPart = ""] = source.split("?", 2);
  const parts = pathPart.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
  const version = parts[0] ?? "v1";
  const view = parts[1] ?? "overview";
  const query: Record<string, string> = {};
  for (const [key, value] of new URLSearchParams(queryPart)) {
    query[key] = value;
  }
  const state: RouteState = { version, view, query };
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

export function searchEntries(
  entries: readonly SearchEntry[],
  rawQuery: string,
): SearchEntry[] {
  const query = rawQuery.normalize("NFKC").toLocaleLowerCase().trim();
  if (!query) {
    return [...entries];
  }
  return entries.filter((entry) => entry.search_text.includes(query));
}

export function projectScanLimitations<T extends ProjectLimitation>(
  limitations: readonly T[],
  projectId: string,
): T[] {
  return limitations.filter(
    (limitation) =>
      limitation.project_id === projectId && limitation.kind.startsWith("scan_issue:"),
  );
}

export function claimMatchesFilters(
  claim: FilterClaim,
  relatedEvidence: readonly FilterEvidence[],
  query: Readonly<Record<string, string>>,
  projectDisposition: string | undefined,
  dimensionEvidenceKinds: ReadonlySet<string> | undefined,
): boolean {
  const selectedFacet = query.facet;
  if (query.project && claim.project_id !== query.project) return false;
  if (query.module && claim.module_id !== query.module) return false;
  if (query.disposition && projectDisposition !== query.disposition) return false;
  if (query.support && claim.support_level !== query.support) return false;
  if (
    selectedFacet &&
    !claim.facets.includes(selectedFacet) &&
    !claim.evidence_relations.some((relation) =>
      relation.supported_facets.includes(selectedFacet),
    )
  ) {
    return false;
  }
  if (query.validity && !relatedEvidence.some((item) => item.validity === query.validity)) {
    return false;
  }
  if (query.commit && !relatedEvidence.some((item) => item.commit_state === query.commit)) {
    return false;
  }
  if (
    query.dimension &&
    (!dimensionEvidenceKinds ||
      !relatedEvidence.some((item) => dimensionEvidenceKinds.has(item.evidence_kind)))
  ) {
    return false;
  }
  return true;
}

export function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    current: "当前",
    stale: "历史限制",
    missing: "已缺失",
    plan: "已规划",
    fresh: "本次新鲜",
    carried_forward: "沿用基线",
    failed_no_baseline: "失败无基线",
    excluded: "已排除",
    single_source: "单一来源",
    cross_checked: "交叉验证",
    user_confirmed: "用户确认",
    conflicted: "存在冲突",
    new: "待首次复习",
    continued: "状态延续",
    reassess_required: "需要重评",
    completed: "完整快照",
    partial: "部分快照",
    low: "低",
    medium: "中",
    high: "高",
    critical: "严重",
  };
  return labels[value] ?? value;
}

export function statusSymbol(value: string): string {
  if (["fresh", "current", "cross_checked", "user_confirmed", "continued"].includes(value)) {
    return "✓";
  }
  if (["missing", "failed_no_baseline", "conflicted", "reassess_required", "high", "critical", "partial"].includes(value)) {
    return "!";
  }
  if (["stale", "carried_forward", "plan", "medium"].includes(value)) {
    return "◷";
  }
  return "○";
}

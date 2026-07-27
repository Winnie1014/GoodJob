import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname } from "node:path";
import { build } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(join(tmpdir(), "goodjob-dashboard-test-"));
try {
  const output = join(temporary, "model.mjs");
  await build({
    entryPoints: [resolve(root, "src/model.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    target: ["node22"],
    outfile: output,
  });
  const model = await import(pathToFileURL(output));
  assert.equal(model.formatBps(2600), "26.00%");
  assert.equal(model.scoreWidth(0), "0.0%");
  assert.equal(model.scoreWidth(1000), "100.0%");
  assert.equal(model.displayText("A\u202eB\u2067C\u2028D"), "A[U+202E]B[U+2067]C[U+2028]D");
  assert.equal(model.tokensText([{ kind: "text", value: "实现" }, { kind: "code", value: "API" }]), "实现API");
  assert.deepEqual(model.parseRoute("#/v1/project/p1/module/m1?validity=current"), {
    version: "v1",
    view: "project",
    projectId: "p1",
    moduleId: "m1",
    query: { validity: "current" },
  });
  assert.equal(model.parseRoute("#/v2/overview").version, "v2");
  assert.deepEqual(
    model.searchEntries(
      [
        { item_id: "a", kind: "claim", route: "#/v1/evidence", search_text: "python sqlite" },
        { item_id: "b", kind: "gap", route: "#/v1/gaps", search_text: "swift ui" },
      ],
      "SQL",
    ).map((item) => item.item_id),
    ["a"],
  );
  const filterClaim = {
    project_id: "p1",
    module_id: "m1",
    support_level: "cross_checked",
    facets: ["implemented", "test_defined"],
    evidence_relations: [
      { evidence_id: "e1", supported_facets: ["implemented"] },
      { evidence_id: "e2", supported_facets: ["test_defined"] },
    ],
  };
  const filterEvidence = [
    {
      evidence_id: "e1",
      evidence_kind: "implementation",
      validity: "current",
      commit_state: "modified",
    },
    {
      evidence_id: "e2",
      evidence_kind: "test_definition",
      validity: "stale",
      commit_state: "committed",
    },
  ];
  assert.equal(
    model.claimMatchesFilters(
      filterClaim,
      filterEvidence,
      {
        project: "p1",
        module: "m1",
        disposition: "fresh",
        support: "cross_checked",
        facet: "implemented",
        validity: "stale",
        commit: "modified",
        dimension: "delivery",
      },
      "fresh",
      new Set(["implementation"]),
    ),
    true,
  );
  assert.equal(
    model.claimMatchesFilters(
      filterClaim,
      filterEvidence,
      { dimension: "architecture" },
      "fresh",
      new Set(["configuration"]),
    ),
    false,
  );
  assert.equal(
    model.claimMatchesFilters(
      filterClaim,
      filterEvidence,
      { commit: "untracked" },
      "fresh",
      undefined,
    ),
    false,
  );
} finally {
  await rm(temporary, { recursive: true, force: true });
}

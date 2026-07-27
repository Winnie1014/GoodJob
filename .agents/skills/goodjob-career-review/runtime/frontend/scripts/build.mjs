import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { build } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(root, "../src/goodjob/dashboard_assets/dashboard.js");
const result = await build({
  entryPoints: [resolve(root, "src/dashboard.ts")],
  bundle: true,
  charset: "ascii",
  format: "iife",
  legalComments: "none",
  minify: false,
  platform: "browser",
  target: ["es2020"],
  treeShaking: true,
  write: false,
});
const built = `${result.outputFiles[0].text.trimEnd()}\n`;
if (process.argv.includes("--check")) {
  const committed = await readFile(output, "utf8").catch(() => "");
  if (committed !== built) {
    throw new Error("dashboard.js is not reproducible from the committed TypeScript source");
  }
} else {
  await writeFile(output, built, "utf8");
}

// Week 24 bundle budget: gzipped INITIAL payload (entry JS + CSS) < 300 KB.
// Lazy route chunks load on demand and are exempt.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const BUDGET_KB = 300;
const assets = join(process.argv[2] ?? "frontend/dist", "assets");

let total = 0;
const initial = readdirSync(assets).filter((f) => /^index-.*\.(js|css)$/.test(f));
if (initial.length === 0) {
  console.error("No entry assets found — did the build run?");
  process.exit(2);
}
for (const file of initial) {
  const gz = gzipSync(readFileSync(join(assets, file))).length;
  total += gz;
  console.log(`${file}: ${(gz / 1024).toFixed(1)} KB gzipped`);
}
const totalKb = total / 1024;
console.log(`Initial payload: ${totalKb.toFixed(1)} KB gzipped (budget ${BUDGET_KB} KB)`);
process.exit(totalKb < BUDGET_KB ? 0 : 1);

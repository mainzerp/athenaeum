// Rebuilds src/athenaeum/webui/static/vendor/graph3d-vendor.min.js.
// Run once when upgrading the 3D stack (not part of the app build):
//   node scripts/build_graph_vendor.mjs
// Requires network access for npm install.
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const PINS = { three: "0.185.1", "3d-force-graph": "1.80.0", "three-spritetext": "1.10.0" };

const work = mkdtempSync(join(tmpdir(), "graph-vendor-"));
try {
  execFileSync("npm", ["init", "-y"], { cwd: work, stdio: "ignore" });
  execFileSync(
    "npm",
    ["install", ...Object.entries(PINS).map(([k, v]) => `${k}@${v}`), "esbuild"],
    { cwd: work, stdio: "inherit" }
  );
  const entry = join(work, "entry.js");
  writeFileSync(
    entry,
    "import ForceGraph3D from '3d-force-graph';\n" +
      "import SpriteText from 'three-spritetext';\n" +
      "import * as THREE from 'three';\n" +
      "import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';\n" +
      "window.Graph3DVendor = { ForceGraph3D, SpriteText, THREE, UnrealBloomPass };\n"
  );
  const out = join(work, "graph3d-vendor.min.js");
  execFileSync(
    join(work, "node_modules", ".bin", "esbuild"),
    [entry, "--bundle", "--minify", "--format=iife", `--outfile=${out}`],
    { stdio: "inherit" }
  );
  const target = new URL("../src/athenaeum/webui/static/vendor/graph3d-vendor.min.js", import.meta.url);
  copyFileSync(out, target);
  console.log(`vendored ${out} -> ${target.pathname}`);
} finally {
  rmSync(work, { recursive: true, force: true });
}

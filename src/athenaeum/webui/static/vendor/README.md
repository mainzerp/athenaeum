# Vendored third-party assets

## graph3d-vendor.min.js

- Self-contained 3D-graph bundle (IIFE, global `Graph3DVendor` exposing
  `ForceGraph3D`, `SpriteText`, `THREE`, `UnrealBloomPass`) — one shared
  three.js instance, so custom `nodeThreeObject` visuals, sprite labels and
  bloom post-processing are safe.
- Contents and versions:
  - three 0.185.1 (MIT, https://github.com/mrdoob/three.js)
  - 3d-force-graph 1.80.0 (MIT, https://github.com/vasturiano/3d-force-graph)
  - three-spritetext 1.10.0 (MIT, https://github.com/vasturiano/three-spritetext)
  - UnrealBloomPass from three's examples/jsm (MIT)
- Built once via `node scripts/build_graph_vendor.mjs` (esbuild bundle at
  vendor time; the app itself has no build step).
- SHA-256: 3377E23123BB2D20CACC298F5D1C286B4469FE85B86669FE65E85D391D83F7C5
- Size: 1,567,924 bytes

Vendored 2026-07-29 (0.10.0). The MIT license texts ship in the upstream
npm packages.

Supersedes: `3d-force-graph.min.js` (1.80.0 UMD, vendored 2026-07-29 for
0.9.0, replaced by this bundle in 0.10.0).

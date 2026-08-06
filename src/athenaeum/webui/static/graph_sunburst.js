/* Sunburst graph view (SUNBURST-ONLY rework).
 *
 * The ONLY graph view on /library/graph: the flat /api/graph/universe
 * payload (fixed link_density metric) rendered on a plain 2D <canvas> — no
 * THREE, no WebGL. Layout:
 * - Root-level documents (cluster "root") fill the DISC of a ring around a
 *   small glowing center anchor (deterministic golden-angle spiral, one dot
 *   per file, never on the ring line itself).
 * - Each top-level folder is a sector docking outward around that center
 *   circle; the sector angle is proportional to its file count (clusters
 *   sorted by id, starting at 12 o'clock) with thin radial dividers that
 *   start OUTSIDE the root ring (never crossing it) and a letter-spaced
 *   ALL-CAPS label. The root ring itself is drawn with the same visibility
 *   as the dividers.
 * - EVERY FILE IS EXACTLY ONE DOT. Files DIRECTLY in a top-level folder
 *   fill a (deliberately undrawn) folder circle at the sector bisector near
 *   the inner band edge — a golden-angle disc whose dots form their own
 *   visible area between the center ring and the subfolder band (radius
 *   grows with sqrt(count), capped by the sector's angular width); clicking
 *   inside the disc zooms in. Subfolder files sit in the radial band
 *   OUTSIDE the folder circle. Folders nest HIERARCHICALLY: every folder
 *   subdivides its parent's angular span (its direct files get a share by
 *   count, each child folder by its recursive total), so depth reads from
 *   angular containment; each folder gets a cluster-colored bracket arc on
 *   its own thin level ring plus a small progressive name label (appears
 *   once its on-screen arc is wide enough). Hovering/selecting a dot
 *   lights up its ENTIRE folder path — every ancestor bracket arc and
 *   folder/sector name label (and the root ring for root documents).
 *   The radial position is the sqrt-scaled link_density radius
 *   (hubs near the center ring, isolates far out) on every level.
 * - Payload edges render as thin quadratic arcs bowed toward the center,
 *   subtle by default; hovering/selecting a dot brightens its arcs and
 *   direct neighbors and dims everything else.
 * Interaction: click a dot for an animated 2D zoom (ease-in-out, cancelable)
 * centered on it plus an info tooltip (label, folder path, link degree,
 * trust/stale); clicking the selected dot again navigates to the document
 * (disabled with opts.navigate === false, used by the trace replay page).
 * Click a sector (or the center ring) to zoom to it; click empty space or
 * press Esc to return to the overview (the Fit view button does the same).
 * ?folder= / ?focus= deep links zoom straight to a sector / dot.
 *
 * The mount handle also exposes setOverlay(...) + redraw(): an opt-in
 * overlay channel for the trace replay page. Overlay shape:
 * {accent, states: {id: "visited"|"hit"}, hopNumbers: {id: 1},
 * hopEdges: [{source, target}], fadeRest: true, pulsePhase: false}.
 * Visited/hit dots render in the accent color (visited larger with a hop
 * badge, hits pulsing with pulsePhase), everything else fades when
 * fadeRest is set, and hopEdges draw as accent arcs over the payload edges.
 * The sentinel id "__core__" stands for the central anchor: hopEdges with a
 * "__core__" endpoint route through the middle, a "__core__" hopNumber
 * badges the anchor, and focusCore(animate) flies the camera there.
 *
 * All placement is deterministic (FNV-1a hash seeds; never Math.random for
 * data dots — the starfield is hash-seeded too). The hash is a local copy of
 * the Graph3D.hash algorithm (graph3d.js has been removed).
 */
(function (global) {
  "use strict";

  var CONFIG = {
    padPx: 24, // screen px padding around the scene circle at overview zoom
    // Geometry in scene units (the whole scene is a disc of radius 1).
    sectorInner: 0.2, // sector band starts here (outside the center ring)
    sectorOuter: 0.98,
    sectorGapDeg: 2.5,
    centerRingR: 0.105, // root documents sit inside this circle (ring boundary)
    centerRingFill: 0.82, // max dot radius as a fraction of centerRingR
    ringCount: 5, // concentric reference rings across the sector band
    ringColor: "rgba(255,255,255,0.06)",
    dividerColor: "rgba(255,255,255,0.12)",
    subDividerColor: "rgba(255,255,255,0.06)",
    groupArcAlpha: 0.4, // cluster-colored bracket arc marking each folder's angular turf
    groupArcWidthPx: 2,
    groupArcHiAlpha: 0.9, // bracket of the hovered/selected dot's folder lights up
    groupArcHiWidthPx: 3,
    groupLevelStep: 0.03, // bracket ring spacing per hierarchy level
    groupBracketMax: 0.095, // bracket zone cap (deeper levels share the last ring)
    dotBandGap: 0.11, // dot band starts here (above the bracket zone)
    groupLabelGap: 0.007, // label stands directly on its bracket line (outward)
    folderCircleR: 0.055, // folder circle radius per sqrt(direct file count)
    folderCircleMinR: 0.035,
    folderCircleFill: 0.78, // max dot radius inside the circle (fraction of r)
    folderCircleBandCap: 0.55, // max circle top radius when subfolders exist
    folderCircleSoloCap: 0.8, // max circle top radius without subfolders
    groupLabelFontSize: 8,
    groupLabelMinArcPx: 44, // folder label appears once its on-screen arc is wide enough
    lineWidthPx: 1, // screen px for rings/dividers (zoom-independent)
    // Zoom controller (2D scale+translate over the base fit transform).
    zoomMs: 650,
    dotZoomK: 4.5, // scale when zooming onto a single dot
    maxK: 14,
    sectorMinK: 1.35, // sector zoom always visibly zooms ...
    sectorMaxK: 3, // ... but never past the dot band filling the viewport
    fitFrac: 0.75, // fraction of the viewport a zoomed region fills
    // Dots: one per file, radius in scene units so dots grow with the zoom.
    dotRadius: 0.0072,
    dotHaloFactor: 2.4, // soft halo radius = dotRadius * this (cheap glow)
    brightnessBase: 0.75,
    brightnessVar: 0.35,
    brightnessMetric: 0.5, // link density brightens
    // Faint deterministic starfield behind the scene (screen space).
    starCount: 110,
    starAlphaMax: 0.35,
    // Central anchor: glowing core in the app accent color (--clr-accent).
    accentColor: "#e2a84b",
    anchorGlowR: 0.055,
    anchorSolidR: 0.012,
    anchorRingR: 0.035,
    labelColor: "#f5f7fa",
    labelFontSize: 10,
    labelRadiusFrac: 0.55, // sector labels sit mid-band at the bisector
    // Edges between linked document dots.
    edgeColor: "#dfe6f0",
    edgeAlpha: 0.1,
    edgeWidthPx: 0.8,
    edgeBow: 0.45, // control point pulled from the midpoint toward the center
    edgeHiAlpha: 0.85,
    edgeHiWidthPx: 1.5,
    dimFactor: 0.12, // brightness of non-neighbor dots while one is active
    // Trace-replay overlay (setOverlay): visited/hit accents + hop edges.
    overlayFade: 0.15, // brightness multiplier for non-trace nodes (fadeRest)
    visitedScale: 1.8, // visited dot radius factor
    pulseMinAlpha: 0.35, // hit dot brightness in the pulse off-phase
    hopEdgeAlpha: 0.9,
    hopEdgeWidthPx: 2,
    // Hover/click hit-testing.
    hoverPx: 10, // screen px pick threshold around a dot
    // Deterministic pastel-neon palette on black, assigned per cluster id via
    // hash with linear probing.
    palette: [
      "#b39dff", // lavender
      "#4fc3f7", // light blue
      "#2dd4bf", // teal
      "#ff8a3d", // orange
      "#f472b6", // pink
      "#ffd54a", // yellow
      "#7ee787", // green
      "#90a4ae" // grey
    ]
  };

  // FNV-1a 32-bit hash — identical algorithm to Graph3D.hash (graph3d.js),
  // kept locally because graph3d.js has been removed.
  function hash(id) {
    var h = 0x811c9dc5;
    var s = String(id);
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  // Deterministic pseudo-random in [0, 1) derived from the seed string.
  function unit(id, salt) {
    return hash(id + "|" + salt) / 4294967295;
  }

  // Stable per-cluster colors: hash-seeded start index into the palette,
  // linear-probed to the next free slot.
  function assignColors(clusters) {
    var used = {};
    var colors = {};
    clusters.forEach(function (c) {
      var idx = hash(c.id) % CONFIG.palette.length;
      while (used[idx]) idx = (idx + 1) % CONFIG.palette.length;
      used[idx] = true;
      colors[c.id] = CONFIG.palette[idx];
    });
    return colors;
  }

  function hexToRgb(hex) {
    var n = parseInt(String(hex).slice(1), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb.r + "," + rgb.g + "," + rgb.b + "," + alpha + ")";
  }

  // The metric brightens, stale dims hardest, then unverified /
  // machine-confirmed (brightness only, never hue).
  function brightnessOf(node) {
    var b = CONFIG.brightnessBase + CONFIG.brightnessVar * unit(node.id, "b");
    b *= 1 + CONFIG.brightnessMetric * (node.radius || 0);
    if (node.stale) b *= 0.5;
    else if (node.trust_tier === "unverified") b *= 0.8;
    else if (node.trust_tier === "machine-confirmed") b *= 0.9;
    return b;
  }

  // Letter-spaced ALL-CAPS like the reference screenshots (U+2009 thin spaces).
  function letterSpace(text) {
    return String(text)
      .toUpperCase()
      .split("")
      .join(" ");
  }

  function metricLabelOf(data) {
    return String((data && data.metric) || "").replace(/_/g, " ");
  }

  function trustText(node) {
    var parts = [];
    parts.push(node.trust_tier || "unverified");
    if (node.stale) parts.push("stale");
    return parts.join(" · ");
  }

  function mount(container, universeData, opts) {
    opts = opts || {};
    if (typeof container === "string") container = document.getElementById(container);
    if (!container) return null;
    var clusters = (universeData && universeData.clusters) || [];
    var nodes = (universeData && universeData.nodes) || [];
    if (!clusters.length || !nodes.length) return null;

    var canvas = document.createElement("canvas");
    canvas.className = "graph-sunburst-canvas";
    container.appendChild(canvas);

    var tip = document.createElement("div");
    tip.className = "graph-sunburst-tip";
    tip.style.display = "none";
    container.appendChild(tip);

    var ctx = canvas.getContext("2d");
    var dpr = Math.max(global.devicePixelRatio || 1, 1);
    var viewW = 0;
    var viewH = 0;
    var data = universeData;
    var layout = null; // rebuilt per setData
    var hoverIdx = -1;
    var selectedIdx = -1;
    var activeNeighbors = null; // Set of node indexes linked to the active dot
    var view = { k: 1, tx: 0, ty: 0 }; // zoom transform over the base fit
    var tween = null;
    var overlay = null; // trace-replay overlay (see setOverlay), null on the graph page

    // --- layout -------------------------------------------------------------

    // Compute all static geometry from the payload (size-independent; scene
    // units, center at 0/0, disc radius 1). One entry in pos per node.
    function computeLayout() {
      var nodesIn = (data && data.nodes) || [];
      var clustersIn = (data && data.clusters) || [];
      var colors = assignColors(clustersIn);
      var idxById = {};
      nodesIn.forEach(function (n, i) {
        idxById[n.id] = i;
      });

      // Link degrees + edge index pairs from the payload edges.
      var degrees = [];
      var edgePairs = [];
      nodesIn.forEach(function () {
        degrees.push(0);
      });
      ((data && data.edges) || []).forEach(function (e) {
        var a = idxById[e.source];
        var b = idxById[e.target];
        if (a == null || b == null || a === b) return;
        degrees[a]++;
        degrees[b]++;
        edgePairs.push({ a: a, b: b });
      });

      // Sectors: top-level folders only (the "root" cluster becomes the
      // center ring), sorted by id, angle proportional to count, 12 o'clock
      // start, clockwise.
      var sectorClusters = clustersIn
        .filter(function (c) {
          return c.id !== "root";
        })
        .sort(function (a, b) {
          return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
        });
      var total = 0;
      sectorClusters.forEach(function (c) {
        total += c.count || 0;
      });
      var gap = (CONFIG.sectorGapDeg * Math.PI) / 180;
      var span = Math.PI * 2 - gap * sectorClusters.length;
      var angle = -Math.PI / 2;
      var sectors = {};
      var sectorList = [];
      sectorClusters.forEach(function (c) {
        var a = total > 0 ? span * ((c.count || 0) / total) : 0;
        var sec = { id: c.id, start: angle + gap / 2, end: angle + gap / 2 + a, groups: [] };
        sectors[c.id] = sec;
        sectorList.push(sec);
        angle += gap + a;
      });

      var pos = [];
      nodesIn.forEach(function () {
        pos.push(null);
      });

      // Center ring: root-level documents fill the ring's DISC (never sit on
      // the ring line itself) — a deterministic phyllotaxis (golden-angle)
      // spiral with area-uniform radius, ordered by id.
      var rootIdxs = [];
      nodesIn.forEach(function (n, i) {
        if (n.cluster === "root") rootIdxs.push(i);
      });
      rootIdxs.sort(function (a, b) {
        return nodesIn[a].id < nodesIn[b].id ? -1 : nodesIn[a].id > nodesIn[b].id ? 1 : 0;
      });
      var GOLDEN = Math.PI * (3 - Math.sqrt(5));
      rootIdxs.forEach(function (ni, k) {
        var frac = (k + 0.5) / rootIdxs.length;
        var r = CONFIG.centerRingR * CONFIG.centerRingFill * Math.sqrt(frac);
        var a = -Math.PI / 2 + k * GOLDEN;
        pos[ni] = { x: Math.cos(a) * r, y: Math.sin(a) * r, angle: a, r: r };
      });

      // Sector layout: files DIRECTLY in the top-level folder fill a folder
      // circle at the sector bisector (golden-angle disc, same idiom as the
      // root ring); subfolder groups share the sector's full angular width
      // and sit in the radial band OUTSIDE the circle.
      sectorList.forEach(function (sec) {
        var direct = [];
        var folderMap = {}; // path -> {folder, level, idxs, children}
        nodesIn.forEach(function (n, i) {
          if (n.cluster !== sec.id) return;
          var fk = n.parent_folder || "/" + sec.id;
          if (fk === "/" + sec.id) {
            direct.push(i);
            return;
          }
          // Register the folder and all missing intermediate ancestors so
          // the tree is complete even when a folder has no direct files.
          var parts = fk.split("/").filter(Boolean);
          for (var d = 2; d <= parts.length; d++) {
            var path = "/" + parts.slice(0, d).join("/");
            if (!folderMap[path]) folderMap[path] = { folder: path, level: d - 1, idxs: [], children: [] };
          }
          folderMap[fk].idxs.push(i);
        });
        direct.sort(function (a, b) {
          return nodesIn[a].id < nodesIn[b].id ? -1 : nodesIn[a].id > nodesIn[b].id ? 1 : 0;
        });
        Object.keys(folderMap).forEach(function (path) {
          folderMap[path].idxs.sort(function (a, b) {
            return nodesIn[a].id < nodesIn[b].id ? -1 : nodesIn[a].id > nodesIn[b].id ? 1 : 0;
          });
          var parent = path.slice(0, path.lastIndexOf("/"));
          if (folderMap[parent]) folderMap[parent].children.push(path);
        });
        var level1 = [];
        Object.keys(folderMap).forEach(function (path) {
          folderMap[path].children.sort();
          if (folderMap[path].level === 1) level1.push(path);
        });
        level1.sort();
        // Recursive file counts (direct files + all descendants).
        function totalCount(node) {
          var c = node.idxs.length;
          node.children.forEach(function (ch) {
            c += totalCount(folderMap[ch]);
          });
          node._total = c;
          return c;
        }
        level1.forEach(function (p) {
          totalCount(folderMap[p]);
        });

        // Folder circle sizing: grows with sqrt(direct count), capped by the
        // sector's angular width and by the radial share left for the band.
        var mid = (sec.start + sec.end) / 2;
        var halfSpan = (sec.end - sec.start) / 2;
        var circle = null;
        var bandInner = CONFIG.sectorInner;
        if (direct.length > 0) {
          var sinH = Math.sin(Math.max(halfSpan - 0.04, 0.05));
          var maxCrAngular = ((CONFIG.sectorInner + 0.03) * sinH) / (1 - sinH);
          var cap = level1.length > 0 ? CONFIG.folderCircleBandCap : CONFIG.folderCircleSoloCap;
          var maxCrRadial = (cap - CONFIG.sectorInner - 0.03) / 2;
          var cr = Math.min(CONFIG.folderCircleR * Math.sqrt(direct.length), maxCrAngular, maxCrRadial);
          cr = Math.max(cr, CONFIG.folderCircleMinR);
          var rc = CONFIG.sectorInner + 0.03 + cr;
          circle = { x: Math.cos(mid) * rc, y: Math.sin(mid) * rc, r: cr };
          bandInner = rc + cr + 0.035;
          direct.forEach(function (ni, k) {
            var frac = (k + 0.5) / direct.length;
            var rr = cr * CONFIG.folderCircleFill * Math.sqrt(frac);
            var a = k * GOLDEN + unit(nodesIn[ni].cluster, "coff") * Math.PI * 2;
            pos[ni] = { x: circle.x + Math.cos(a) * rr, y: circle.y + Math.sin(a) * rr, angle: a, r: rr };
          });
        }
        sec.circle = circle;
        sec.bandInner = bandInner;

        // Hierarchical angular nesting: every folder subdivides its parent's
        // span — its direct files get a share proportional to their count,
        // each child folder a share proportional to its recursive total.
        // Radial stays the sqrt-scaled link_density metric on every level.
        function placeDots(ni2, start, end, count, i2) {
          var node = nodesIn[ni2];
          var frac = (i2 + 0.5) / count;
          var a =
            start +
            frac * (end - start) +
            (unit(node.id, "ajit") - 0.5) * Math.min(((end - start) / count) * 0.5, 0.01);
          var rr = bandInner + (1 - (node.radius || 0)) * (CONFIG.sectorOuter - bandInner);
          rr += (unit(node.id, "rjit") - 0.5) * 0.02;
          rr = Math.max(bandInner + CONFIG.dotBandGap, Math.min(rr, CONFIG.sectorOuter));
          pos[ni2] = { x: Math.cos(a) * rr, y: Math.sin(a) * rr, angle: a, r: rr };
        }
        function allocate(path, start, end) {
          var node = folderMap[path];
          sec.groups.push({ folder: path, level: node.level, start: start, end: end });
          var parts = [];
          if (node.idxs.length) parts.push({ kind: "direct", weight: node.idxs.length });
          node.children.forEach(function (ch) {
            parts.push({ kind: "child", path: ch, weight: folderMap[ch]._total });
          });
          var totalW = 0;
          parts.forEach(function (pt) {
            totalW += pt.weight;
          });
          var gap = parts.length > 1 ? Math.min((end - start) * 0.02, 0.008) : 0;
          var usable = end - start - gap * (parts.length - 1);
          var cursor = start;
          parts.forEach(function (pt) {
            var w = totalW > 0 ? usable * (pt.weight / totalW) : 0;
            if (pt.kind === "direct") {
              node.idxs.forEach(function (ni, i) {
                placeDots(ni, cursor, cursor + w, node.idxs.length, i);
              });
            } else {
              allocate(pt.path, cursor, cursor + w);
            }
            cursor += w + gap;
          });
        }
        var pad = Math.min((sec.end - sec.start) * 0.04, 0.02);
        var avail = sec.end - sec.start - 2 * pad;
        var topTotal = 0;
        level1.forEach(function (p) {
          topTotal += folderMap[p]._total;
        });
        var gap1 = level1.length > 1 ? Math.min(avail * 0.02, 0.012) : 0;
        var usable1 = avail - gap1 * (level1.length - 1);
        var cursor = sec.start + pad;
        level1.forEach(function (p) {
          var w = topTotal > 0 ? usable1 * (folderMap[p]._total / topTotal) : 0;
          allocate(p, cursor, cursor + w);
          cursor += w + gap1;
        });
      });

      return {
        pos: pos,
        sectors: sectors,
        sectorList: sectorList,
        edgePairs: edgePairs,
        degrees: degrees,
        colors: colors,
        idxById: idxById,
        rootCount: rootIdxs.length,
        // Outer radius of the root-document ring area; sector dividers must
        // start outside it so they never cross the root ring.
        centerRingOuter: CONFIG.centerRingR,
        centerHitR: CONFIG.centerRingR + 0.05
      };
    }

    // --- view transform -----------------------------------------------------

    function baseScale() {
      return Math.max(Math.min(viewW, viewH) / 2 - CONFIG.padPx, 40);
    }

    function sceneToScreen(p) {
      var bs = baseScale();
      return { x: viewW / 2 + bs * (view.k * p.x + view.tx), y: viewH / 2 + bs * (view.k * p.y + view.ty) };
    }

    function screenToScene(x, y) {
      var bs = baseScale();
      return { x: ((x - viewW / 2) / bs - view.tx) / view.k, y: ((y - viewH / 2) / bs - view.ty) / view.k };
    }

    function notifyView() {
      if (typeof opts.onViewChange === "function") opts.onViewChange();
    }

    // Cancelable ease-in-out tween of the view transform; the optional
    // onDone fires only when the tween runs to completion (a cancel skips it).
    function animateTo(target, animate, onDone) {
      if (tween) {
        global.cancelAnimationFrame(tween.raf);
        tween = null;
      }
      if (!animate) {
        view = { k: target.k, tx: target.tx, ty: target.ty };
        render();
        syncTip();
        notifyView();
        if (onDone) onDone();
        return;
      }
      var from = { k: view.k, tx: view.tx, ty: view.ty };
      var start = global.performance.now();
      var tw = { raf: 0 };
      function step(now) {
        var t = Math.min((now - start) / CONFIG.zoomMs, 1);
        var e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        view.k = from.k + (target.k - from.k) * e;
        view.tx = from.tx + (target.tx - from.tx) * e;
        view.ty = from.ty + (target.ty - from.ty) * e;
        render();
        syncTip();
        notifyView();
        if (t < 1) tw.raf = global.requestAnimationFrame(step);
        else {
          tween = null;
          if (onDone) onDone();
        }
      }
      tween = tw;
      tw.raf = global.requestAnimationFrame(step);
    }

    // View target fitting a scene-space bounding box.
    function fitTarget(x0, y0, x1, y1) {
      var hw = Math.max((x1 - x0) / 2, 0.02);
      var hh = Math.max((y1 - y0) / 2, 0.02);
      var k = Math.min(CONFIG.fitFrac / Math.max(hw, hh), CONFIG.maxK);
      k = Math.max(k, 1);
      var cx = (x0 + x1) / 2;
      var cy = (y0 + y1) / 2;
      return { k: k, tx: -k * cx, ty: -k * cy };
    }

    function dotTarget(ni) {
      var p = layout.pos[ni];
      return { k: CONFIG.dotZoomK, tx: -CONFIG.dotZoomK * p.x, ty: -CONFIG.dotZoomK * p.y };
    }

    // Scene bbox of a sector wedge (corners + cardinal arc extremes).
    // Zoom target: center on the dot-band centroid (mid angle, mid radius)
    // with a scale that fits the wedge's angular width at that radius —
    // bbox fitting cannot zoom wide sectors (their bbox is nearly the whole
    // disc), so the scale derives from the angular half-width instead.
    function sectorTarget(sec) {
      var halfSpan = (sec.end - sec.start) / 2;
      var rc = (CONFIG.sectorInner + 1) / 2 + 0.03; // dot-band centroid radius
      var k = CONFIG.fitFrac / Math.max(rc * Math.sin(halfSpan), 0.1);
      k = Math.max(CONFIG.sectorMinK, Math.min(k, CONFIG.sectorMaxK));
      var mid = (sec.start + sec.end) / 2;
      return { k: k, tx: -k * rc * Math.cos(mid), ty: -k * rc * Math.sin(mid) };
    }

    // --- selection / highlight ----------------------------------------------

    // The active dot drives the highlight: hover wins over the selection.
    function activeIdx() {
      return hoverIdx >= 0 ? hoverIdx : selectedIdx;
    }

    function computeNeighbors(ni) {
      var set = {};
      layout.edgePairs.forEach(function (e) {
        if (e.a === ni) set[e.b] = true;
        else if (e.b === ni) set[e.a] = true;
      });
      return set;
    }

    function select(ni) {
      selectedIdx = ni;
      activeNeighbors = ni >= 0 ? computeNeighbors(ni) : null;
      render();
    }

    // --- tooltip --------------------------------------------------------------

    function setTip(node, sx, sy) {
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var title = document.createElement("div");
      title.className = "graph-sunburst-tip-title";
      title.textContent = node.label || node.id;
      var meta = document.createElement("div");
      meta.className = "graph-sunburst-tip-meta";
      meta.textContent =
        letterSpace(node.cluster) +
        "  ·  " +
        (node.parent_folder || "/") +
        "  ·  " +
        (layout.degrees[layout.idxById[node.id]] || 0) +
        " links";
      var meta2 = document.createElement("div");
      meta2.className = "graph-sunburst-tip-meta";
      meta2.textContent = trustText(node) + "  ·  " + metricLabelOf(data) + ": " + metricValueText(node);
      tip.appendChild(title);
      tip.appendChild(meta);
      tip.appendChild(meta2);
      tip.style.display = "block";
      var tx = sx + 14;
      var ty = sy + 14;
      if (tx + 240 > viewW) tx = sx - 244;
      if (ty + 76 > viewH) ty = sy - 80;
      tip.style.left = tx + "px";
      tip.style.top = ty + "px";
    }

    function metricValueText(node) {
      var v = node.metric_value;
      if (v == null) return "-";
      var s = String(v);
      if (s.indexOf("T") > 0) return s.slice(0, s.indexOf("T"));
      return s;
    }

    // Keep the pinned tooltip glued to the selected dot during zoom frames.
    function syncTip() {
      if (selectedIdx < 0 || hoverIdx >= 0) return;
      var node = ((data && data.nodes) || [])[selectedIdx];
      if (!node || !layout.pos[selectedIdx]) return;
      var sp = sceneToScreen(layout.pos[selectedIdx]);
      setTip(node, sp.x, sp.y);
    }

    // --- rendering ------------------------------------------------------------

    function render() {
      if (viewW <= 0 || viewH <= 0 || !layout) return;
      canvas.width = Math.round(viewW * dpr);
      canvas.height = Math.round(viewH * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, viewW, viewH);

      // Faint starfield (deterministic, screen space; decoration only).
      for (var i = 0; i < CONFIG.starCount; i++) {
        var sx = unit("star" + i, "xpos") * viewW;
        var sy = unit("star" + i, "ypos") * viewH;
        var sr = 0.4 + unit("star" + i, "rpos") * 0.9;
        var sa = 0.06 + unit("star" + i, "apos") * CONFIG.starAlphaMax;
        ctx.fillStyle = "rgba(255,255,255," + sa.toFixed(3) + ")";
        ctx.beginPath();
        ctx.arc(sx, sy, sr, 0, Math.PI * 2);
        ctx.fill();
      }

      // Scene transform: base fit + zoom.
      var bs = baseScale();
      ctx.setTransform(
        dpr * bs * view.k,
        0,
        0,
        dpr * bs * view.k,
        dpr * (viewW / 2 + bs * view.tx),
        dpr * (viewH / 2 + bs * view.ty)
      );
      var px = 1 / (bs * view.k); // 1 screen px in scene units
      var nodesIn = (data && data.nodes) || [];
      var active = activeIdx();
      var dimmed = active >= 0;
      var nb = activeNeighbors || {};

      // Concentric reference rings across the sector band (decoration only).
      ctx.strokeStyle = CONFIG.ringColor;
      ctx.lineWidth = CONFIG.lineWidthPx * px;
      for (var ri = 0; ri <= CONFIG.ringCount; ri++) {
        var rr = CONFIG.sectorInner + ((1 - CONFIG.sectorInner) * ri) / CONFIG.ringCount;
        ctx.beginPath();
        ctx.arc(0, 0, rr, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Root-document ring: same visibility as the top-level dividers so the
      // center circle reads as a structural element, not decoration. Always
      // drawn — the center boundary stays visible even with zero root
      // documents. It lights up when a root document is hovered/selected.
      var rootHot = active >= 0 && nodesIn[active].cluster === "root";
      ctx.strokeStyle = rootHot ? "rgba(255,255,255,0.45)" : CONFIG.dividerColor;
      ctx.lineWidth = CONFIG.lineWidthPx * px;
      ctx.beginPath();
      ctx.arc(0, 0, CONFIG.centerRingR, 0, Math.PI * 2);
      ctx.stroke();

      // Radial sector dividers + subfolder sub-dividers. Dividers start
      // OUTSIDE the root ring (plus a small margin) and never cross it.
      ctx.strokeStyle = CONFIG.dividerColor;
      ctx.lineWidth = CONFIG.lineWidthPx * px;
      var dividerInner = layout.centerRingOuter + 0.02;
      var gapHalf = (CONFIG.sectorGapDeg * Math.PI) / 360;
      layout.sectorList.forEach(function (sec) {
        [sec.start - gapHalf, sec.end + gapHalf].forEach(function (a) {
          ctx.beginPath();
          ctx.moveTo(Math.cos(a) * dividerInner, Math.sin(a) * dividerInner);
          ctx.lineTo(Math.cos(a), Math.sin(a));
          ctx.stroke();
        });
      });
      // Subfolder sub-dividers: level-1 folders only (deeper levels read
      // from their bracket arcs, radial lines would just add noise).
      ctx.strokeStyle = CONFIG.subDividerColor;
      layout.sectorList.forEach(function (sec) {
        sec.groups.forEach(function (g) {
          if (g.level !== 1) return;
          ctx.beginPath();
          ctx.moveTo(Math.cos(g.start) * sec.bandInner, Math.sin(g.start) * sec.bandInner);
          ctx.lineTo(Math.cos(g.start) * CONFIG.sectorOuter, Math.sin(g.start) * CONFIG.sectorOuter);
          ctx.stroke();
        });
      });

      // Folder brackets: a cluster-colored arc at the inner band edge marks
      // each subfolder's angular turf, so folders read as structure. The
      // bracket of the hovered/selected dot's folder lights up.
      var activeSec = null;
      var activeFolder = null;
      var activePathSet = {};
      if (active >= 0) {
        var actNode = nodesIn[active];
        activeSec = actNode.cluster;
        activeFolder = actNode.parent_folder || "/" + actNode.cluster;
        // All ancestor folders of the active dot (the full path chain):
        // /helix/daily/log -> {/helix/daily, /helix/daily/log}.
        var fparts = activeFolder.split("/").filter(Boolean);
        for (var fd = 2; fd <= fparts.length; fd++) {
          activePathSet["/" + fparts.slice(0, fd).join("/")] = true;
        }
      }
      layout.sectorList.forEach(function (sec) {
        var gc = hexToRgb(layout.colors[sec.id] || CONFIG.palette[CONFIG.palette.length - 1]);
        sec.groups.forEach(function (g) {
          var hot = activePathSet[g.folder] === true;
          ctx.strokeStyle = rgba(gc, hot ? CONFIG.groupArcHiAlpha : CONFIG.groupArcAlpha);
          ctx.lineWidth = (hot ? CONFIG.groupArcHiWidthPx : CONFIG.groupArcWidthPx) * px;
          // Bracket radius per hierarchy level: nested folders stack on
          // their own thin rings inside the bracket zone (dot band starts
          // above it at dotBandGap, so lines never touch dots).
          var br = sec.bandInner + Math.min(CONFIG.groupLevelStep * g.level, CONFIG.groupBracketMax);
          ctx.beginPath();
          ctx.arc(0, 0, br, g.start, g.end);
          ctx.stroke();
        });
      });

      // Folder circles are NOT drawn: the direct-file dots form a visible
      // disc of their own between the center ring and the subfolder band
      // (the circle geometry still drives layout, hit-testing and zoom).

      // Edge arcs: quadratic curves bowed toward the center.
      var edgeRgb = hexToRgb(CONFIG.edgeColor);
      layout.edgePairs.forEach(function (e) {
        var p1 = layout.pos[e.a];
        var p2 = layout.pos[e.b];
        if (!p1 || !p2) return;
        var hot = dimmed && (e.a === active || e.b === active);
        var alpha = dimmed ? (hot ? CONFIG.edgeHiAlpha : 0.03) : CONFIG.edgeAlpha;
        ctx.strokeStyle = rgba(edgeRgb, alpha);
        ctx.lineWidth = (hot ? CONFIG.edgeHiWidthPx : CONFIG.edgeWidthPx) * px;
        var cxq = ((p1.x + p2.x) / 2) * (1 - CONFIG.edgeBow);
        var cyq = ((p1.y + p2.y) / 2) * (1 - CONFIG.edgeBow);
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.quadraticCurveTo(cxq, cyq, p2.x, p2.y);
        ctx.stroke();
      });

      // Trace overlay hop edges: accent arcs over the payload edges, using
      // the same center-bowed quadratic math; pairs with missing endpoints
      // (hops pointing at documents not in the universe) are skipped. The
      // sentinel "__core__" resolves to the central anchor (scene 0,0).
      if (overlay && overlay.hopEdges) {
        var hopRgb = hexToRgb(overlay.accent || "#f1c40f");
        overlay.hopEdges.forEach(function (he) {
          if (he.source === he.target) return;
          var CORE = "__core__";
          var hp1 = he.source === CORE ? { x: 0, y: 0 } : layout.pos[layout.idxById[he.source]];
          var hp2 = he.target === CORE ? { x: 0, y: 0 } : layout.pos[layout.idxById[he.target]];
          if (!hp1 || !hp2) return;
          ctx.strokeStyle = rgba(hopRgb, CONFIG.hopEdgeAlpha);
          ctx.lineWidth = CONFIG.hopEdgeWidthPx * px;
          var hcx = ((hp1.x + hp2.x) / 2) * (1 - CONFIG.edgeBow);
          var hcy = ((hp1.y + hp2.y) / 2) * (1 - CONFIG.edgeBow);
          ctx.beginPath();
          ctx.moveTo(hp1.x, hp1.y);
          ctx.quadraticCurveTo(hcx, hcy, hp2.x, hp2.y);
          ctx.stroke();
        });
      }

      // Document dots: exactly one per file (halo + solid core). The trace
      // overlay recolors visited/hit dots in the accent color and fades
      // everything else when fadeRest is set.
      var oStates = (overlay && overlay.states) || {};
      var oHops = (overlay && overlay.hopNumbers) || {};
      var oAccent = overlay && overlay.accent ? hexToRgb(overlay.accent) : null;
      var oFade = !!(overlay && overlay.fadeRest);
      nodesIn.forEach(function (node, ni) {
        var p = layout.pos[ni];
        if (!p) return;
        var state = overlay ? oStates[node.id] : null;
        var hasHop = overlay && oHops[node.id] != null;
        var base = hexToRgb(layout.colors[node.cluster] || CONFIG.palette[CONFIG.palette.length - 1]);
        if (state && oAccent) base = oAccent;
        var b = brightnessOf(node);
        if (dimmed && ni !== active && !nb[ni]) b *= CONFIG.dimFactor;
        if (oFade && !state && !hasHop) b *= CONFIG.overlayFade;
        if (state === "visited") b = Math.max(b, 1); // full brightness
        if (state === "hit") b *= overlay.pulsePhase ? 1 : CONFIG.pulseMinAlpha;
        var dr = CONFIG.dotRadius * (state === "visited" ? CONFIG.visitedScale : 1);
        ctx.fillStyle = rgba(base, Math.min(0.32 * b, 1));
        ctx.beginPath();
        ctx.arc(p.x, p.y, dr * CONFIG.dotHaloFactor, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = rgba(base, Math.min(0.95 * b, 1));
        ctx.beginPath();
        ctx.arc(p.x, p.y, dr, 0, Math.PI * 2);
        ctx.fill();
        // Hop-number badge above the visited dot (screen-constant size).
        if (hasHop) {
          ctx.fillStyle = rgba(oAccent || base, 0.95);
          ctx.font = "600 " + 11 * px + "px sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "bottom";
          ctx.fillText(String(oHops[node.id]), p.x, p.y - dr * 2.2);
        }
      });

      // Central anchor: accent glow + solid core + thin ring. The glow fades
      // with the zoom so it does not dominate deep dot/sector views.
      var accent = hexToRgb(CONFIG.accentColor);
      var anchorFade = Math.max(0.15, Math.min(1, 1.4 - 0.3 * view.k));
      var grad = ctx.createRadialGradient(0, 0, 0, 0, 0, CONFIG.anchorGlowR);
      grad.addColorStop(0, rgba(accent, 0.85 * anchorFade));
      grad.addColorStop(0.45, rgba(accent, 0.3 * anchorFade));
      grad.addColorStop(1, rgba(accent, 0));
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(0, 0, CONFIG.anchorGlowR, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = rgba(accent, 0.95 * Math.min(1, anchorFade + 0.3));
      ctx.beginPath();
      ctx.arc(0, 0, CONFIG.anchorSolidR, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = rgba(accent, 0.8 * anchorFade);
      ctx.lineWidth = CONFIG.lineWidthPx * px;
      ctx.beginPath();
      ctx.arc(0, 0, CONFIG.anchorRingR, 0, Math.PI * 2);
      ctx.stroke();

      // Hop badge on the anchor when the trace passes through the core
      // (overlay.hopNumbers["__core__"], screen-constant size like the
      // dot badges).
      if (overlay && oHops["__core__"] != null) {
        ctx.fillStyle = rgba(oAccent || accent, 0.95);
        ctx.font = "600 " + 11 * px + "px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillText(String(oHops["__core__"]), 0, -(CONFIG.anchorRingR + 0.02));
      }

      // Active dot highlight: white core + cluster-colored ring.
      if (active >= 0 && layout.pos[active]) {
        var ap = layout.pos[active];
        var anode = nodesIn[active];
        var abase = hexToRgb(layout.colors[anode.cluster] || CONFIG.palette[CONFIG.palette.length - 1]);
        ctx.fillStyle = rgba(abase, 0.4);
        ctx.beginPath();
        ctx.arc(ap.x, ap.y, CONFIG.dotRadius * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(255,255,255,0.95)";
        ctx.beginPath();
        ctx.arc(ap.x, ap.y, CONFIG.dotRadius * 1.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = rgba(abase, 1);
        ctx.lineWidth = 1.5 * px;
        ctx.beginPath();
        ctx.arc(ap.x, ap.y, CONFIG.dotRadius * 2.6, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Labels at constant screen size (zoom-independent), drawn over the
      // scene; they fade while zooming deep into a sector.
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var labelAlpha = Math.max(0.15, Math.min(1, 1.4 - 0.3 * view.k));
      ctx.fillStyle = rgba(hexToRgb(CONFIG.labelColor), labelAlpha);
      ctx.font = "600 " + CONFIG.labelFontSize + "px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      layout.sectorList.forEach(function (sec) {
        var mid = (sec.start + sec.end) / 2;
        var lr = CONFIG.sectorInner + (1 - CONFIG.sectorInner) * CONFIG.labelRadiusFrac;
        var sp = sceneToScreen({ x: Math.cos(mid) * lr, y: Math.sin(mid) * lr });
        // The active dot's whole path chain lights up, sector label included.
        ctx.fillStyle = rgba(hexToRgb(CONFIG.labelColor), activeSec === sec.id ? 1 : labelAlpha);
        ctx.fillText(letterSpace(sec.id), sp.x, sp.y);
      });
      // Subfolder name labels: curved text standing directly on its
      // bracket line, following the ring's curvature (letters stay upright;
      // reading direction reverses on the bottom half so nothing is upside
      // down). Progressive: only when the arc is wide enough for the text.
      // Labels on the active dot's folder path light up like their brackets.
      ctx.font = "600 " + CONFIG.groupLabelFontSize + "px sans-serif";
      var centerPx = sceneToScreen({ x: 0, y: 0 });
      var scalePx = baseScale() * view.k;
      layout.sectorList.forEach(function (sec) {
        sec.groups.forEach(function (g) {
          var gspan = g.end - g.start;
          var lr =
            sec.bandInner +
            Math.min(CONFIG.groupLevelStep * g.level, CONFIG.groupBracketMax) +
            CONFIG.groupLabelGap;
          var rPx = lr * scalePx;
          var arcLen = gspan * rPx;
          if (arcLen < CONFIG.groupLabelMinArcPx) return;
          var name = (g.folder.split("/").filter(Boolean).pop() || sec.id).toUpperCase();
          var chars = name.split("");
          var widths = [];
          var total = 0;
          chars.forEach(function (ch) {
            var w = ctx.measureText(ch).width + CONFIG.groupLabelFontSize * 0.18; // tracking
            widths.push(w);
            total += w;
          });
          if (total > arcLen * 0.92) return;
          ctx.fillStyle = rgba(hexToRgb(CONFIG.labelColor), activePathSet[g.folder] ? 0.95 : 0.55);
          var mid = (g.start + g.end) / 2;
          var flip = Math.sin(mid) > 0; // bottom half (canvas y is down)
          var a0 = mid - total / 2 / rPx;
          var acc = 0;
          for (var i = 0; i < chars.length; i++) {
            var idx = flip ? chars.length - 1 - i : i;
            var a = a0 + (acc + widths[idx] / 2) / rPx;
            acc += widths[idx];
            ctx.save();
            ctx.translate(centerPx.x + Math.cos(a) * rPx, centerPx.y + Math.sin(a) * rPx);
            ctx.rotate(flip ? a - Math.PI / 2 : a + Math.PI / 2);
            ctx.textAlign = "center";
            ctx.textBaseline = flip ? "top" : "bottom";
            ctx.fillText(chars[idx], 0, 0);
            ctx.restore();
          }
        });
      });
    }

    // --- picking --------------------------------------------------------------

    function pickDot(x, y) {
      var p = screenToScene(x, y);
      var bs = baseScale();
      var best = -1;
      var bestD = CONFIG.hoverPx / (bs * view.k); // threshold in scene units
      for (var i = 0; i < layout.pos.length; i++) {
        var dpos = layout.pos[i];
        if (!dpos) continue;
        var dx = dpos.x - p.x;
        var dy = dpos.y - p.y;
        var d = Math.sqrt(dx * dx + dy * dy);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      return best;
    }

    // Sector id at a scene point, or null (inside the center ring / outside).
    function sectorAt(p) {
      var r = Math.sqrt(p.x * p.x + p.y * p.y);
      if (r < CONFIG.sectorInner || r > 1) return null;
      var a = Math.atan2(p.y, p.x);
      while (a < -Math.PI / 2) a += Math.PI * 2;
      while (a >= (Math.PI * 3) / 2) a -= Math.PI * 2;
      for (var i = 0; i < layout.sectorList.length; i++) {
        var sec = layout.sectorList[i];
        if (a >= sec.start && a <= sec.end) return sec.id;
      }
      return null;
    }

    // Sector whose folder circle contains the scene point, or null.
    function circleAt(p) {
      for (var i = 0; i < layout.sectorList.length; i++) {
        var c = layout.sectorList[i].circle;
        if (!c) continue;
        var dx = p.x - c.x;
        var dy = p.y - c.y;
        if (Math.sqrt(dx * dx + dy * dy) <= c.r) return layout.sectorList[i];
      }
      return null;
    }

    // --- events ---------------------------------------------------------------

    function onMove(e) {
      var rect = canvas.getBoundingClientRect();
      var x = e.clientX - rect.left;
      var y = e.clientY - rect.top;
      var best = pickDot(x, y);
      if (best === hoverIdx && best >= 0) {
        setTip(((data && data.nodes) || [])[best], x, y);
        return;
      }
      if (best === hoverIdx) {
        // No dot under the cursor: keep the sector/center pointer hint fresh.
        if (best < 0) {
          var pp = screenToScene(x, y);
          var pr = Math.sqrt(pp.x * pp.x + pp.y * pp.y);
          canvas.style.cursor = sectorAt(pp) || circleAt(pp) || pr <= layout.centerHitR ? "pointer" : "";
        }
        return;
      }
      hoverIdx = best;
      activeNeighbors = activeIdx() >= 0 ? computeNeighbors(activeIdx()) : null;
      render();
      if (best >= 0) {
        setTip(((data && data.nodes) || [])[best], x, y);
        canvas.style.cursor = "pointer";
      } else {
        if (selectedIdx >= 0) syncTip();
        else tip.style.display = "none";
        var p = screenToScene(x, y);
        var r = Math.sqrt(p.x * p.x + p.y * p.y);
        canvas.style.cursor = sectorAt(p) || circleAt(p) || r <= layout.centerHitR ? "pointer" : "";
      }
    }

    function onLeave() {
      hoverIdx = -1;
      activeNeighbors = selectedIdx >= 0 ? computeNeighbors(selectedIdx) : null;
      if (selectedIdx >= 0) syncTip();
      else tip.style.display = "none";
      canvas.style.cursor = "";
      render();
    }

    function onClick(e) {
      var rect = canvas.getBoundingClientRect();
      var x = e.clientX - rect.left;
      var y = e.clientY - rect.top;
      var best = pickDot(x, y);
      if (best >= 0) {
        var node = ((data && data.nodes) || [])[best];
        if (best === selectedIdx) {
          // Second click on the selected dot navigates to the document
          // (disabled with opts.navigate === false, e.g. trace replay).
          if (opts.navigate !== false) {
            global.location.href = "/library/document?path=" + encodeURIComponent(node.id + ".md");
          }
          return;
        }
        select(best);
        syncTip();
        animateTo(dotTarget(best), true);
        return;
      }
      var p = screenToScene(x, y);
      var r = Math.sqrt(p.x * p.x + p.y * p.y);
      var circSec = circleAt(p);
      if (circSec) {
        // Click inside a folder circle zooms to it.
        select(-1);
        tip.style.display = "none";
        var c = circSec.circle;
        animateTo(fitTarget(c.x - c.r, c.y - c.r, c.x + c.r, c.y + c.r), true);
        return;
      }
      var secId = sectorAt(p);
      if (secId) {
        select(-1);
        tip.style.display = "none";
        animateTo(sectorTarget(layout.sectors[secId]), true);
        return;
      }
      if (r <= layout.centerHitR && layout.rootCount > 0) {
        select(-1);
        tip.style.display = "none";
        var cr = layout.centerHitR + 0.02;
        animateTo(fitTarget(-cr, -cr, cr, cr), true);
        return;
      }
      resetView(true);
    }

    function onKey(e) {
      if (e.key === "Escape") resetView(true);
    }

    function resetView(animate) {
      select(-1);
      hoverIdx = -1;
      tip.style.display = "none";
      animateTo({ k: 1, tx: 0, ty: 0 }, animate);
    }

    function zoomToSector(id, animate) {
      var sec = layout.sectors[id];
      if (!sec) return;
      select(-1);
      tip.style.display = "none";
      animateTo(sectorTarget(sec), animate !== false);
    }

    function focusNode(id, animate) {
      var ni = layout.idxById[id];
      if (ni == null || !layout.pos[ni]) return;
      hoverIdx = -1;
      select(ni);
      animateTo(dotTarget(ni), animate !== false);
      syncTip();
    }

    // Sequenced zoom-out → zoom-in flight (document view navigation): the
    // camera pulls straight back OUT OF the current dot (the zoom-out stays
    // centered on it) before diving into the target dot, so the move reads
    // as "out of the old document, into the new one" instead of a detour via
    // the graph center. The old dot stays SELECTED during the pull-back; the
    // selection flips to the new dot only at the apex of the flight (fully
    // zoomed out), right before the dive-in. Cancellation-safe: any new
    // animateTo/flightTo cancels the in-flight leg, and a cancelled first leg
    // never starts the second (its onDone only fires on completion), so the
    // selection never flips for an aborted flight. Silent no-op on unknown
    // ids (same posture as focusNode).
    function flightTo(id, animate) {
      var ni = layout.idxById[id];
      if (ni == null || !layout.pos[ni]) return;
      hoverIdx = -1;
      var fromIdx = selectedIdx;
      var target = dotTarget(ni);
      if (
        animate === false ||
        view.k <= 1.02 ||
        fromIdx == null ||
        fromIdx < 0 ||
        fromIdx === ni ||
        !layout.pos[fromIdx]
      ) {
        // No animation requested, already at the overview, or no distinct
        // previous dot to pull out of: select and fly direct.
        select(ni);
        animateTo(target, animate !== false);
        syncTip();
        return;
      }
      var p = layout.pos[fromIdx];
      animateTo({ k: 1, tx: -p.x, ty: -p.y }, true, function () {
        select(ni);
        animateTo(target, true);
        syncTip();
      });
    }

    // Fly to the central anchor (trace replay core hops: index.md reads
    // route through the middle). Same target as the center-ring click.
    function focusCore(animate) {
      select(-1);
      hoverIdx = -1;
      tip.style.display = "none";
      var cr = layout.centerHitR + 0.02;
      animateTo(fitTarget(-cr, -cr, cr, cr), animate !== false);
    }

    // Trace-replay overlay channel: {accent, states, hopNumbers, hopEdges,
    // fadeRest, pulsePhase} — null clears it. Only stores + re-renders.
    function setOverlay(next) {
      overlay = next || null;
      render();
    }

    function resize() {
      var w = container.clientWidth;
      var h = container.clientHeight;
      if (w === viewW && h === viewH) return;
      viewW = w;
      viewH = h;
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      render();
      syncTip();
      notifyView();
    }

    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("click", onClick);
    global.addEventListener("keydown", onKey);
    var ro = null;
    if (typeof global.ResizeObserver === "function") {
      ro = new global.ResizeObserver(resize);
      ro.observe(container);
    } else {
      global.addEventListener("resize", resize);
    }

    layout = computeLayout();
    resize();

    return {
      setData: function (next) {
        data = next;
        layout = computeLayout();
        hoverIdx = -1;
        selectedIdx = -1;
        activeNeighbors = null;
        tip.style.display = "none";
        view = { k: 1, tx: 0, ty: 0 };
        render();
        notifyView();
      },
      zoomToSector: zoomToSector,
      focusNode: focusNode,
      flightTo: flightTo,
      focusCore: focusCore,
      resetView: resetView,
      setOverlay: setOverlay,
      redraw: render,
      // Minimap glue: static scene geometry + the currently visible scene rect.
      getScene: function () {
        var nodesIn = (data && data.nodes) || [];
        var dots = [];
        layout.pos.forEach(function (p, ni) {
          if (!p) return;
          dots.push({ x: p.x, y: p.y, color: layout.colors[nodesIn[ni].cluster] || "#90a4ae" });
        });
        return {
          dots: dots,
          sectors: layout.sectorList.map(function (s) {
            return { id: s.id, start: s.start, end: s.end };
          }),
          sectorInner: CONFIG.sectorInner
        };
      },
      getViewport: function () {
        if (view.k <= 1.02) return null;
        var tl = screenToScene(0, 0);
        var br = screenToScene(viewW, viewH);
        return { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y };
      },
      dispose: function () {
        if (tween) global.cancelAnimationFrame(tween.raf);
        if (ro) ro.disconnect();
        else global.removeEventListener("resize", resize);
        global.removeEventListener("keydown", onKey);
        canvas.removeEventListener("mousemove", onMove);
        canvas.removeEventListener("mouseleave", onLeave);
        canvas.removeEventListener("click", onClick);
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
        if (tip.parentNode) tip.parentNode.removeChild(tip);
      }
    };
  }

  var api = { CONFIG: CONFIG, mount: mount, hash: hash, assignColors: assignColors };
  global.GraphSunburst = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

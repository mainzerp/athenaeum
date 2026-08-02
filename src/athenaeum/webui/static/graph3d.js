(function (global) {
  "use strict";

  var CONFIG = {
    starScaleBase: 5,
    starScalePerDegree: 1.2,
    labelStarHeight: 9,
    labelPlanetHeight: 5,
    // Fixed folder body sizes; star > planet holds by construction.
    folderBodyPlanetSize: 14,
    folderBodyStarSize: 22,
    planetSphereSegments: 32,
    labelBodyGap: 3,
    bloomStrength: 0.7,
    bloomRadius: 0.4,
    bloomThreshold: 0.3,
    backgroundColor: "#000000",
    autoRotateSpeed: 0.5,
    idleRotateDelayMs: 8000,
    fogDensity: 0.0011,
    starfieldCount: 2200,
    starfieldRadius: [600, 1800],
    starfieldSize: 2.4,
    starfieldOpacity: 0.6,
    starfieldColor: "#aabbdd",
    dimNodeAlpha: 0.15,
    dimLinkAlpha: 0.05,
    linkEdgeAlpha: 0.35,
    linkEdgeColor: "#5a6a8a",
    starColor: "#e8b45a",
    planetColor: "#b8a37a",
    particlesPerLink: 2,
    particleSpeed: 0.005,
    particleWidth: 1.1,
    // Live d3 force layout (detail/trace pages): pre-spread nodes with warmup
    // ticks, then let the engine run the library-default duration.
    warmupTicks: 100,
    cooldownTime: 15000,
    fitMs: 1000,
    focusMs: 800
  };

  function endId(end) {
    return end && typeof end === "object" ? end.id : end;
  }

  function withAlpha(color, alpha) {
    if (typeof color !== "string") return color;
    if (color.charAt(0) === "#") {
      var hex = color.slice(1);
      if (hex.length === 3) hex = hex.replace(/./g, "$&$&");
      if (hex.length !== 6) return color;
      var n = parseInt(hex, 16);
      if (isNaN(n)) return color;
      return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
    }
    if (color.indexOf("rgb(") === 0) return "rgba(" + color.slice(4, -1) + "," + alpha + ")";
    if (color.indexOf("rgba(") === 0) return color.replace(/,[^,]+\)$/, "," + alpha + ")");
    return color;
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function parentFolder(path) {
    if (typeof path !== "string" || !path || path === "/") return null;
    var i = path.lastIndexOf("/");
    return i <= 0 ? "/" : path.slice(0, i);
  }

  function nearestEmittedFolder(folder, folderIds) {
    var cur = folder;
    while (cur && cur !== "/") {
      if (folderIds[cur]) return cur;
      cur = parentFolder(cur);
    }
    return null;
  }

  function buildUniverse(apiData) {
    var apiNodes = (apiData && apiData.nodes) || [];
    var apiFolders = (apiData && apiData.folders) || [];
    var apiEdges = (apiData && apiData.edges) || [];

    var folderIds = {};
    apiFolders.forEach(function (f) {
      folderIds[f.id] = true;
    });

    var degree = {};
    apiEdges.forEach(function (e) {
      degree[e.from] = (degree[e.from] || 0) + 1;
      degree[e.to] = (degree[e.to] || 0) + 1;
    });

    var nodes = [];
    var links = [];

    apiFolders.forEach(function (f) {
      nodes.push({
        id: f.id,
        label: f.name,
        kind: f.kind,
        isFolder: true,
        depth: f.depth,
        baseColor: f.kind === "star" ? CONFIG.starColor : CONFIG.planetColor
      });
    });

    apiNodes.forEach(function (n) {
      nodes.push({
        id: n.id,
        label: n.label,
        kind: n.kind,
        isFolder: false,
        group: n.group,
        title: n.title,
        folder: n.folder,
        depth: n.depth,
        trust_tier: n.trust_tier,
        stale: n.stale,
        baseColor: n.color,
        degree: degree[n.id] || 0
      });
    });

    apiFolders.forEach(function (f) {
      if (f.kind === "planet" && folderIds[f.parent]) {
        links.push({ source: f.id, target: f.parent, linkType: "containment" });
      }
    });

    apiNodes.forEach(function (n) {
      var host = nearestEmittedFolder(n.folder, folderIds);
      if (host) links.push({ source: n.id, target: host, linkType: "containment" });
    });

    apiEdges.forEach(function (e) {
      links.push({ source: e.from, target: e.to, linkType: "link" });
    });

    return { nodes: nodes, links: links };
  }

  // FNV-1a 32-bit hash of a node id as an unsigned number. Stable across
  // reloads (deterministic placement); graph_sunburst.js keeps an identical
  // local copy — the graph page no longer loads this module.
  function hash(id) {
    var h = 0x811c9dc5;
    var s = String(id);
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  function computeNeighbors(universe, id) {
    var out = {};
    ((universe && universe.links) || []).forEach(function (l) {
      if (l.linkType !== "link") return;
      var s = endId(l.source);
      var t = endId(l.target);
      if (s === id) out[t] = true;
      if (t === id) out[s] = true;
    });
    return out;
  }

  function makeStarTexture(THREE) {
    var size = 64;
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext("2d");
    var g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0, "rgba(255,255,255,1)");
    g.addColorStop(0.25, "rgba(255,255,255,0.85)");
    g.addColorStop(0.5, "rgba(255,255,255,0.25)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    return new THREE.CanvasTexture(canvas);
  }

  function makeStarfield(THREE, starTex) {
    var count = CONFIG.starfieldCount;
    var positions = new Float32Array(count * 3);
    for (var i = 0; i < count; i++) {
      var r = CONFIG.starfieldRadius[0] + Math.random() * (CONFIG.starfieldRadius[1] - CONFIG.starfieldRadius[0]);
      var theta = Math.random() * Math.PI * 2;
      var phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      positions[i * 3 + 2] = r * Math.cos(phi);
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    var mat = new THREE.PointsMaterial({
      size: CONFIG.starfieldSize,
      map: starTex,
      color: CONFIG.starfieldColor,
      transparent: true,
      opacity: CONFIG.starfieldOpacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      sizeAttenuation: true,
      fog: false
    });
    return new THREE.Points(geo, mat);
  }

  function mount(container, universe, opts) {
    opts = opts || {};
    var vendor = global.Graph3DVendor || {};
    var ForceGraph3D = vendor.ForceGraph3D;
    var SpriteText = vendor.SpriteText;
    var THREE = vendor.THREE;
    var UnrealBloomPass = vendor.UnrealBloomPass;

    if (typeof container === "string") container = document.getElementById(container);
    if (!container || typeof ForceGraph3D !== "function" || !THREE) return null;

    if (!universe.nodes.length) {
      container.innerHTML =
        '<div class="empty-state"><h3>No documents yet</h3>' +
        "<p>The library universe is empty. Add documents to see them here.</p></div>";
      return null;
    }

    var state = {
      universe: universe,
      selectedId: null,
      keepSet: null,
      filters: { showMoons: true },
      navigate: opts.navigate !== false,
      interactive: opts.interactive !== false,
      autoRotate: opts.autoRotate !== false,
      initialFitDone: false,
      engineSettled: false,
      starTex: makeStarTexture(THREE)
    };

    function nodeAlphaOf(node) {
      var st = node.__style;
      if (st && st.alpha != null) return st.alpha;
      if (state.selectedId && !(state.keepSet && state.keepSet[node.id])) {
        return CONFIG.dimNodeAlpha;
      }
      return 1;
    }

    function nodeColorOf(node) {
      var st = node.__style;
      if (st && st.color) return st.color;
      return node.baseColor || "#95a5a6";
    }

    function nodeScaleOf(node) {
      // All documents are moons; size scales uniformly with link degree.
      var base = CONFIG.starScaleBase + (node.degree || 0) * CONFIG.starScalePerDegree;
      var st = node.__style;
      if (st && st.scale) base *= st.scale;
      return base;
    }

    function applyNodeStyle(node) {
      var obj = node.__obj;
      if (!obj) return;
      if (node.isFolder) {
        // Folders are Groups (body + label). __style.alpha (trace replay
        // fade/pulse, selection dimming) dims body AND label; __style.color
        // tints the body only (the label keeps baseColor for readability);
        // __style.scale stays ignored on folders.
        var alpha = nodeAlphaOf(node);
        var body = obj.userData && obj.userData.body;
        var label = obj.userData && obj.userData.label;
        if (body && body.material) {
          body.material.color.set(nodeColorOf(node));
          body.material.opacity = alpha;
        }
        if (label && label.material) label.material.opacity = alpha;
        return;
      }
      if (obj.material) {
        obj.material.color.set(nodeColorOf(node));
        obj.material.opacity = nodeAlphaOf(node);
      }
      var s = nodeScaleOf(node);
      obj.scale.set(s, s, 1);
    }

    function restyleAll() {
      universe.nodes.forEach(applyNodeStyle);
    }

    function nodeLabelOf(node) {
      var st = node.__style;
      var badge =
        st && st.badge != null
          ? '<span style="color:#f1c40f;font-weight:700">#' + st.badge + "</span> "
          : "";
      if (node.isFolder) return badge + "<b>" + esc(node.label) + "</b><br/>" + esc(node.kind);
      var parts = [badge, "<b>", esc(node.label), "</b><br/>", esc(node.group || "unknown")];
      if (node.trust_tier) parts.push(" &middot; ", esc(node.trust_tier));
      if (node.stale) parts.push(" &middot; stale");
      if (node.title) parts.push("<br/>", esc(node.title));
      return parts.join("");
    }

    function makeNodeObject(node) {
      var obj;
      if (node.isFolder) {
        // Folder = THREE.Group: a real body (star: additive glow sprite,
        // planet: matte lit sphere mesh) plus the SpriteText label offset
        // above the body. Fixed CONFIG sizes; star > planet by construction.
        var isStar = node.kind === "star";
        var size = isStar ? CONFIG.folderBodyStarSize : CONFIG.folderBodyPlanetSize;
        var body;
        if (isStar) {
          body = new THREE.Sprite(
            new THREE.SpriteMaterial({
              map: state.starTex,
              color: nodeColorOf(node),
              transparent: true,
              depthWrite: false,
              blending: THREE.AdditiveBlending
            })
          );
          body.scale.set(size, size, 1);
        } else {
          body = new THREE.Mesh(
            new THREE.SphereGeometry(size / 2, CONFIG.planetSphereSegments, CONFIG.planetSphereSegments),
            new THREE.MeshLambertMaterial({ color: nodeColorOf(node), transparent: true })
          );
        }
        var label = new SpriteText(node.label);
        label.color = node.baseColor;
        label.textHeight = isStar ? CONFIG.labelStarHeight : CONFIG.labelPlanetHeight;
        if (label.material) label.material.depthWrite = false;
        label.position.y = size / 2 + CONFIG.labelBodyGap + label.textHeight / 2;
        obj = new THREE.Group();
        obj.add(body);
        obj.add(label);
        obj.userData.body = body;
        obj.userData.label = label;
      } else {
        obj = new THREE.Sprite(
          new THREE.SpriteMaterial({
            map: state.starTex,
            color: nodeColorOf(node),
            transparent: true,
            depthWrite: false,
            blending: THREE.AdditiveBlending
          })
        );
        var s = nodeScaleOf(node);
        obj.scale.set(s, s, 1);
      }
      node.__obj = obj;
      applyNodeStyle(node);
      return obj;
    }

    function linkBase(link) {
      if (link.color) return { color: link.color, alpha: link.alpha == null ? 0.9 : link.alpha };
      return { color: CONFIG.linkEdgeColor, alpha: CONFIG.linkEdgeAlpha };
    }

    function linkColorOf(link) {
      var st = link.__style;
      if (st && st.color) return withAlpha(st.color, st.alpha == null ? 1 : st.alpha);
      var base = linkBase(link);
      if (state.selectedId) {
        var hot = endId(link.source) === state.selectedId || endId(link.target) === state.selectedId;
        if (!hot) base = { color: base.color, alpha: CONFIG.dimLinkAlpha };
      }
      return withAlpha(base.color, base.alpha);
    }

    function visibleData() {
      var f = state.filters;
      var nodes = state.universe.nodes.filter(function (n) {
        // folders always show; documents (moons) follow the moon filter
        return n.isFolder || f.showMoons;
      });
      var keep = {};
      nodes.forEach(function (n) {
        keep[n.id] = true;
      });
      var links = state.universe.links.filter(function (l) {
        return keep[endId(l.source)] && keep[endId(l.target)];
      });
      return { nodes: nodes, links: links };
    }

    function contentOnly(node) {
      return !node.isFolder && node.folder && node.folder !== "/";
    }

    function fitContent(filter, ms, padRatio) {
      var bbox = graph.getGraphBbox(filter);
      if (!bbox) return;
      var cx = (bbox.x[0] + bbox.x[1]) / 2;
      var cy = (bbox.y[0] + bbox.y[1]) / 2;
      var cz = (bbox.z[0] + bbox.z[1]) / 2;
      var sizeX = bbox.x[1] - bbox.x[0];
      var sizeY = bbox.y[1] - bbox.y[0];
      var sizeZ = bbox.z[1] - bbox.z[0];
      var fov = (graph.camera().fov || 50) * (Math.PI / 180);
      var aspect = container.clientWidth / Math.max(container.clientHeight, 1) || 1;
      var tan = Math.tan(fov / 2);
      var distY = Math.max(sizeY, sizeZ) / 2 / tan;
      var distX = sizeX / 2 / (tan * aspect);
      var dist = Math.max(Math.max(distX, distY) * (1 + (padRatio || 0.25)), 30);
      var cam = graph.cameraPosition();
      var dx = cam.x - cx;
      var dy = cam.y - cy;
      var dz = cam.z - cz;
      var dl = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      graph.cameraPosition(
        {
          x: cx + (dx / dl) * dist,
          y: cy + (dy / dl) * dist + dist * 0.15,
          z: cz + (dz / dl) * dist
        },
        { x: cx, y: cy, z: cz },
        ms
      );
    }

    var graph = new ForceGraph3D(container, { controlType: "orbit" })
      .width(container.clientWidth)
      .height(container.clientHeight)
      .backgroundColor(CONFIG.backgroundColor)
      .nodeThreeObject(makeNodeObject)
      .nodeLabel(nodeLabelOf)
      .linkWidth(function (l) {
        return l.width != null ? l.width : l.linkType === "link" ? 0.5 : 0.5;
      })
      .linkOpacity(1)
      .linkColor(linkColorOf)
      .linkLabel(function (l) {
        return l.label || null;
      })
      .linkCurvature(function (l) {
        return l.curvature || 0;
      })
      .linkVisibility(function (l) {
        return l.linkType === "link";
      })
      .linkDirectionalParticles(function (l) {
        if (l.particles != null) return l.particles;
        return l.linkType === "link" ? CONFIG.particlesPerLink : 0;
      })
      .linkDirectionalParticleSpeed(function (l) {
        var count = l.particles != null ? l.particles : l.linkType === "link" ? CONFIG.particlesPerLink : 0;
        return count > 0 ? CONFIG.particleSpeed : 0;
      })
      .linkDirectionalParticleWidth(function (l) {
        var count = l.particles != null ? l.particles : l.linkType === "link" ? CONFIG.particlesPerLink : 0;
        return count > 0 ? CONFIG.particleWidth : 0;
      })
      .warmupTicks(CONFIG.warmupTicks)
      .cooldownTime(CONFIG.cooldownTime)
      .enableNodeDrag(false)
      .onEngineStop(function () {
        state.engineSettled = true;
        if (!state.initialFitDone) {
          state.initialFitDone = true;
          fitContent(contentOnly, CONFIG.fitMs, 0.3);
          setTimeout(function () {
            fitContent(contentOnly, CONFIG.fitMs, 0.3);
          }, CONFIG.fitMs + 2500);
          if (state.autoRotate) {
            setTimeout(function () {
              graph.controls().autoRotate = true;
            }, CONFIG.fitMs * 2 + 2900);
          }
        }
      })
      .graphData(visibleData());

    if (typeof UnrealBloomPass === "function") {
      var bloom = new UnrealBloomPass(
        new THREE.Vector2(container.clientWidth, container.clientHeight),
        CONFIG.bloomStrength,
        CONFIG.bloomRadius,
        CONFIG.bloomThreshold
      );
      graph.postProcessingComposer().addPass(bloom);
    }

    graph.scene().add(makeStarfield(THREE, state.starTex));
    graph.scene().fog = new THREE.FogExp2(CONFIG.backgroundColor, CONFIG.fogDensity);

    var idleTimer = null;
    if (state.autoRotate) {
      var controls = graph.controls();
      controls.autoRotate = false;
      controls.autoRotateSpeed = CONFIG.autoRotateSpeed;
      controls.addEventListener("start", function () {
        controls.autoRotate = false;
        if (idleTimer) {
          clearTimeout(idleTimer);
          idleTimer = null;
        }
      });
      controls.addEventListener("end", function () {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(function () {
          controls.autoRotate = true;
        }, CONFIG.idleRotateDelayMs);
      });
    }

    function selectNode(id) {
      state.selectedId = id;
      state.keepSet = computeNeighbors(state.universe, id);
      state.keepSet[id] = true;
      restyleAll();
      graph.refresh();
    }

    function clearSelection() {
      state.selectedId = null;
      state.keepSet = null;
      restyleAll();
      graph.refresh();
    }

    function folderContains(folderId, node) {
      if (node.isFolder) return node.id === folderId || node.id.indexOf(folderId + "/") === 0;
      if (!node.folder || node.folder === "/") return false;
      return node.folder === folderId || node.folder.indexOf(folderId + "/") === 0;
    }

    function focusFolder(folderId) {
      fitContent(
        function (node) {
          return !node.isFolder && folderContains(folderId, node);
        },
        CONFIG.focusMs,
        0.6
      );
    }

    function applyFilters() {
      graph.graphData(visibleData());
    }

    function nodeById(id) {
      for (var i = 0; i < universe.nodes.length; i++) {
        if (universe.nodes[i].id === id) return universe.nodes[i];
      }
      return null;
    }

    if (state.interactive) {
      graph.onNodeClick(function (node) {
        if (!node) return;
        if (node.isFolder) {
          focusFolder(node.id);
          return;
        }
        if (state.selectedId === node.id) {
          if (state.navigate) {
            window.location.href = "/library/document?path=" + encodeURIComponent(node.id + ".md");
          }
          return;
        }
        selectNode(node.id);
      });
      graph.onNodeHover(function (node) {
        container.style.cursor = node ? "pointer" : "";
      });
      graph.onBackgroundClick(function () {
        clearSelection();
        fitContent(contentOnly, CONFIG.fitMs, 0.3);
      });
    }

    if (typeof ResizeObserver === "function") {
      new ResizeObserver(function () {
        graph.width(container.clientWidth).height(container.clientHeight);
      }).observe(container);
    }

    var api = {
      graph: graph,
      state: state,
      universe: universe,
      cancelAutoFit: function () {
        state.initialFitDone = true;
      },
      refresh: function () {
        restyleAll();
        graph.refresh();
      },
      nodeById: nodeById
    };
    global.__athenaeumGraph = api;
    return api;
  }

  var api = {
    CONFIG: CONFIG,
    buildUniverse: buildUniverse,
    computeNeighbors: computeNeighbors,
    nearestEmittedFolder: nearestEmittedFolder,
    parentFolder: parentFolder,
    withAlpha: withAlpha,
    hash: hash,
    mount: mount
  };
  global.Graph3D = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

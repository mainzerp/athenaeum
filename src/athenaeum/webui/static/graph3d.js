(function (global) {
  "use strict";

  var CONFIG = {
    starScalePlanet: 7,
    starScalePerDegree: 1.2,
    starScaleMoon: 3.5,
    labelGalaxyHeight: 9,
    labelSystemHeight: 5,
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
    galaxyColor: "#e8b45a",
    systemColor: "#b8a37a",
    particlesPerLink: 2,
    particleSpeed: 0.005,
    particleWidth: 1.1,
    orbitRadiusMin: 18,
    orbitRadiusMinGalaxy: 90,
    orbitSlotArc: 16,
    rogueRingOffset: 30,
    warmupTicks: 0,
    cooldownTime: 300,
    fitMs: 1000,
    focusMs: 800,
    flyMs: 1200,
    flyOffset: { x: 30, y: 20, z: 30 }
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
        baseColor: f.kind === "galaxy" ? CONFIG.galaxyColor : CONFIG.systemColor
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
      if (f.kind === "system" && folderIds[f.parent]) {
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

    var universe = { nodes: nodes, links: links };
    computeOrbitLayout(universe);
    return universe;
  }

  // FNV-1a 32-bit hash of a node id as an unsigned number. Stable across
  // reloads, so orbit slot angles never reshuffle for an unchanged library.
  function hash(id) {
    var h = 0x811c9dc5;
    var s = String(id);
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  // Deterministic orbit layout (pure function of the full node/link set;
  // no Math.random, no time input). Every body is pinned to a ring in the
  // shared ecliptic plane (y = 0): folders orbit their parent folder,
  // depth-1 folders orbit the virtual universe center, root-level documents
  // sit on one outer "rogue" ring. Wikilinked siblings are ordered into
  // adjacent slots. Mutates nodes in place (x/y/z and fx/fy/fz) and returns
  // the universe.
  function computeOrbitLayout(universe) {
    var nodes = (universe && universe.nodes) || [];
    var links = (universe && universe.links) || [];

    var byId = {};
    nodes.forEach(function (n) {
      byId[n.id] = n;
    });

    // Undirected wikilink adjacency (link edges only) for sibling ordering.
    var adjacency = {};
    links.forEach(function (l) {
      if (l.linkType !== "link") return;
      var s = endId(l.source);
      var t = endId(l.target);
      if (!adjacency[s]) adjacency[s] = {};
      adjacency[s][t] = true;
      if (!adjacency[t]) adjacency[t] = {};
      adjacency[t][s] = true;
    });

    // Children per parent folder. Folder nodes parent to their parent
    // folder, documents to their own folder; only folders act as parents
    // (documents are always leaves). Depth-1 folders and root-level
    // documents are collected separately (center rings).
    var childrenOf = {};
    var galaxyIds = [];
    var rogueIds = [];
    nodes.forEach(function (n) {
      var parent;
      if (n.isFolder) {
        parent = parentFolder(n.id);
        if (parent === "/" || !byId[parent]) {
          galaxyIds.push(n.id);
          return;
        }
      } else {
        parent = n.folder;
        if (!parent || parent === "/" || !byId[parent]) {
          rogueIds.push(n.id); // also the defensive fallback for missing folders
          return;
        }
      }
      if (!childrenOf[parent]) childrenOf[parent] = [];
      childrenOf[parent].push(n.id);
    });

    // Order a sibling set so wikilinked siblings occupy adjacent slots:
    // connected components under the link adjacency (sibling edges only),
    // components ordered by smallest member hash, members by hash.
    function orderSiblings(ids) {
      var siblingSet = {};
      ids.forEach(function (id) {
        siblingSet[id] = true;
      });
      var seen = {};
      var components = [];
      ids.forEach(function (id) {
        if (seen[id]) return;
        var component = [];
        var queue = [id];
        seen[id] = true;
        while (queue.length) {
          var cur = queue.pop();
          component.push(cur);
          var neighbors = adjacency[cur] || {};
          for (var other in neighbors) {
            if (siblingSet[other] && !seen[other]) {
              seen[other] = true;
              queue.push(other);
            }
          }
        }
        components.push(component);
      });
      components.forEach(function (component) {
        component.sort(function (a, b) {
          return hash(a) - hash(b);
        });
      });
      components.sort(function (a, b) {
        return hash(a[0]) - hash(b[0]);
      });
      var out = [];
      components.forEach(function (component) {
        out = out.concat(component);
      });
      return out;
    }

    function pin(node, x, z) {
      node.x = node.fx = x;
      node.y = node.fy = 0;
      node.z = node.fz = z;
    }

    // Constant arc length per slot: the radius grows deterministically with
    // the child count, so slots never crowd.
    function ringRadius(m, minRadius) {
      return Math.max(minRadius, (CONFIG.orbitSlotArc * m) / (2 * Math.PI));
    }

    // Place the children of one parent on a ring around it, then recurse
    // into folder children. Returns the ring radius.
    function placeRing(parentId, px, pz, childIds, minRadius) {
      var ordered = orderSiblings(childIds);
      var m = ordered.length;
      var r = ringRadius(m, minRadius);
      if (!m) return r;
      // Stable, id-dependent ring phase; the virtual center uses phi = 0.
      var phi = parentId ? (hash(parentId) % 6283) / 1000 : 0;
      ordered.forEach(function (id, i) {
        var node = byId[id];
        if (!node) return;
        var theta = phi + (2 * Math.PI * i) / m;
        var x = px + r * Math.cos(theta);
        var z = pz + r * Math.sin(theta);
        pin(node, x, z);
        if (node.isFolder) {
          placeRing(node.id, x, z, childrenOf[node.id] || [], CONFIG.orbitRadiusMin);
        }
      });
      return r;
    }

    // Galaxy ring: depth-1 folders around the virtual center at the origin.
    var galaxyR = placeRing(null, 0, 0, galaxyIds, CONFIG.orbitRadiusMinGalaxy);

    // Rogue ring: root-level documents on one ring outside the galaxy ring.
    var rogueOrdered = orderSiblings(rogueIds);
    var rm = rogueOrdered.length;
    if (rm) {
      var rogueR = Math.max(galaxyR + CONFIG.rogueRingOffset, ringRadius(rm, 0));
      rogueOrdered.forEach(function (id, i) {
        var theta = (2 * Math.PI * i) / rm;
        pin(byId[id], rogueR * Math.cos(theta), rogueR * Math.sin(theta));
      });
    }

    // Defensive: anything the tree walk never reached is pinned to the
    // origin rather than left for the simulation to place randomly.
    nodes.forEach(function (n) {
      if (n.fx == null) pin(n, 0, 0);
    });

    return universe;
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
      filters: { showGalaxies: true, showSystems: true, showPlanets: true, showMoons: true },
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
      var base;
      if (node.kind === "moon") base = CONFIG.starScaleMoon;
      else base = CONFIG.starScalePlanet + (node.degree || 0) * CONFIG.starScalePerDegree;
      var st = node.__style;
      if (st && st.scale) base *= st.scale;
      return base;
    }

    function applyNodeStyle(node) {
      var obj = node.__obj;
      if (!obj) return;
      if (node.isFolder) {
        if (obj.material) obj.material.opacity = nodeAlphaOf(node);
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
        obj = new SpriteText(node.label);
        obj.color = node.kind === "galaxy" ? CONFIG.galaxyColor : CONFIG.systemColor;
        obj.textHeight = node.kind === "galaxy" ? CONFIG.labelGalaxyHeight : CONFIG.labelSystemHeight;
        if (obj.material) obj.material.depthWrite = false;
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
        if (n.isFolder) {
          if (n.kind === "galaxy") return f.showGalaxies;
          return f.showSystems;
        }
        if (n.kind === "moon") return f.showMoons;
        return f.showPlanets;
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

    // Deterministic orbit layout: computeOrbitLayout pinned every node
    // (fx/fy/fz), so the force simulation has nothing to do. Drop the
    // default forces entirely instead of tuning them.
    graph.d3Force("link", null);
    graph.d3Force("charge", null);
    graph.d3Force("center", null);

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

    function flyToNode(node) {
      if (!node || node.x == null) return;
      graph.cameraPosition(
        {
          x: node.x + CONFIG.flyOffset.x,
          y: node.y + CONFIG.flyOffset.y,
          z: node.z + CONFIG.flyOffset.z
        },
        { x: node.x, y: node.y, z: node.z },
        CONFIG.flyMs
      );
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
      fitAll: function () {
        fitContent(contentOnly, CONFIG.fitMs, 0.5);
      },
      focusFolder: focusFolder,
      selectNode: selectNode,
      clearSelection: clearSelection,
      cancelAutoFit: function () {
        state.initialFitDone = true;
      },
      refresh: function () {
        restyleAll();
        graph.refresh();
      },
      listFolders: function () {
        return universe.nodes.filter(function (n) {
          return n.isFolder;
        });
      },
      setShowGalaxies: function (v) {
        state.filters.showGalaxies = !!v;
        applyFilters();
      },
      setShowSystems: function (v) {
        state.filters.showSystems = !!v;
        applyFilters();
      },
      setShowPlanets: function (v) {
        state.filters.showPlanets = !!v;
        applyFilters();
      },
      setShowMoons: function (v) {
        state.filters.showMoons = !!v;
        applyFilters();
      },
      nodeById: nodeById,
      flyToNode: flyToNode,
      searchFly: function (query) {
        var q = String(query || "").trim().toLowerCase();
        if (!q) return null;
        var found = null;
        universe.nodes.forEach(function (n) {
          if (found || n.isFolder) return;
          if ((n.label || "").toLowerCase().indexOf(q) !== -1) found = n;
          else if (n.id.toLowerCase().indexOf(q) !== -1) found = n;
        });
        if (!found) return null;
        selectNode(found.id);
        flyToNode(found);
        return found;
      }
    };
    global.__athenaeumGraph = api;
    return api;
  }

  var api = {
    CONFIG: CONFIG,
    buildUniverse: buildUniverse,
    computeOrbitLayout: computeOrbitLayout,
    computeNeighbors: computeNeighbors,
    nearestEmittedFolder: nearestEmittedFolder,
    parentFolder: parentFolder,
    withAlpha: withAlpha,
    mount: mount
  };
  global.Graph3D = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

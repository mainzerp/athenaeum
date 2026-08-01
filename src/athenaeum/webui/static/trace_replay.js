(function (global) {
  "use strict";

  var ACCENT = "#f1c40f";
  var FADE_NODE_ALPHA = 0.15;
  var FADE_LINK_ALPHA = 0.1;
  var VISITED_SCALE = 1.8;
  var PULSE_INTERVAL_MS = 450;
  var PULSE_MIN_ALPHA = 0.35;
  var CAMERA_MS = 1500;
  var CAMERA_OFFSET = 60;
  var RESERVED = { "index.md": true, "log.md": true };
  var WRITE_TOOLS = {
    write_concept: true,
    edit_concept: true,
    move_concept: true,
    deprecate_concept: true,
    delete_concept: true,
  };

  function nodeId(path) {
    if (typeof path !== "string" || !path) return null;
    return path.endsWith(".md") ? path.slice(0, -3) : path;
  }

  function isConceptPath(path) {
    if (typeof path !== "string" || !path.endsWith(".md")) return false;
    var name = path.slice(path.lastIndexOf("/") + 1);
    return !RESERVED[name];
  }

  function baseColor(node) {
    if (typeof node.baseColor === "string") return node.baseColor;
    if (typeof node.color === "string") return node.color;
    if (node.color && typeof node.color.background === "string") return node.color.background;
    return "#95a5a6";
  }

  function visitEvents(trace) {
    var visits = [];
    (trace.events || []).forEach(function (event) {
      if (event.error) return;
      if (event.tool === "read_document") {
        var path = (event.args && event.args.path) || (event.result && event.result.path);
        if (isConceptPath(path)) visits.push({ id: nodeId(path), seq: event.seq });
      } else if (WRITE_TOOLS[event.tool]) {
        var id = event.result && event.result.id;
        if (typeof id === "string" && id) visits.push({ id: nodeId(id), seq: event.seq });
      }
    });
    return visits;
  }

  function visitedNodes(trace) {
    return visitEvents(trace).map(function (v) {
      return v.id;
    });
  }

  function visitedHops(trace) {
    var hops = [];
    visitEvents(trace).forEach(function (v) {
      if (!hops.length || hops[hops.length - 1].id !== v.id) {
        hops.push({ hop: hops.length + 1, id: v.id, seq: v.seq });
      }
    });
    return hops;
  }

  function searchHitNodes(trace) {
    var hits = [];
    (trace.events || []).forEach(function (event) {
      if (event.tool !== "search_metadata" || !event.result) return;
      (event.result.hits || []).forEach(function (path) {
        var id = nodeId(path);
        if (id) hits.push(id);
      });
    });
    return hits;
  }

  function computeTraceOverlay(universeNodeIds, trace) {
    var visited = [];
    var ringed = {};
    visitedHops(trace).forEach(function (h) {
      if (!ringed[h.id]) {
        ringed[h.id] = true;
        visited.push(h.id);
      }
    });
    var hits = [];
    var hitSet = {};
    searchHitNodes(trace).forEach(function (id) {
      if (ringed[id]) return;
      if (hitSet[id]) return;
      hitSet[id] = true;
      hits.push(id);
    });
    var keep = {};
    visited.forEach(function (id) {
      keep[id] = true;
    });
    hits.forEach(function (id) {
      keep[id] = true;
    });
    var faded = (universeNodeIds || []).filter(function (id) {
      return !keep[id];
    });
    return { faded: faded, visited: visited, hits: hits };
  }

  function hopLinks(hops) {
    var links = [];
    for (var i = 0; i < hops.length - 1; i++) {
      links.push({
        source: hops[i].id,
        target: hops[i + 1].id,
        linkType: "hop",
        color: ACCENT,
        alpha: 0.9,
        width: 2,
        particles: 2,
        curvature: 0.2,
        label: "Hop " + (i + 1),
      });
    }
    return links;
  }

  function applyOverlay3D(ctrl, trace) {
    var universe = ctrl.universe;
    var hops = visitedHops(trace);
    var overlay = computeTraceOverlay(
      universe.nodes.map(function (n) {
        return n.id;
      }),
      trace
    );
    var visitedSet = {};
    overlay.visited.forEach(function (id) {
      visitedSet[id] = true;
    });
    var hitSet = {};
    overlay.hits.forEach(function (id) {
      hitSet[id] = true;
    });
    var firstHop = {};
    hops.forEach(function (h) {
      if (firstHop[h.id] == null) firstHop[h.id] = h.hop;
    });

    universe.nodes.forEach(function (n) {
      if (visitedSet[n.id]) {
        n.__style = { color: ACCENT, alpha: 1, scale: VISITED_SCALE, badge: firstHop[n.id] };
      } else if (hitSet[n.id]) {
        n.__style = { color: ACCENT, alpha: 1, scale: 1, pulse: true };
      } else {
        n.__style = { color: baseColor(n), alpha: FADE_NODE_ALPHA };
      }
    });
    universe.links.forEach(function (l) {
      if (l.linkType === "hop") return;
      var base = l.color || (l.linkType === "link" ? "#e2a84b" : "#8a8580");
      l.__style = { color: base, alpha: FADE_LINK_ALPHA };
    });
    ctrl.refresh();
    return overlay;
  }

  function startPulse(ctrl, hits) {
    if (!hits.length) return null;
    var phase = false;
    return setInterval(function () {
      phase = !phase;
      hits.forEach(function (id) {
        var n = ctrl.nodeById(id);
        if (n && n.__style && n.__style.pulse) {
          n.__style.alpha = phase ? 1 : PULSE_MIN_ALPHA;
          n.__style.scale = phase ? 1.3 : 1;
        }
      });
      ctrl.refresh();
    }, PULSE_INTERVAL_MS);
  }

  function cameraToHop(ctrl, hop) {
    var node = ctrl.nodeById(hop.id);
    if (!node || node.x == null) return false;
    var d = CAMERA_OFFSET;
    ctrl.graph.cameraPosition(
      { x: node.x + d, y: node.y + d * 0.6, z: node.z + d },
      { x: node.x, y: node.y, z: node.z },
      CAMERA_MS
    );
    return true;
  }

  function makePlayback(ctrl, hops, els) {
    var idx = -1;
    var timer = null;

    function highlightTimeline(hop) {
      var items = document.querySelectorAll("#timeline li[data-seq]");
      for (var i = 0; i < items.length; i++) items[i].classList.remove("hop-active");
      if (!hop || hop.seq == null) return;
      var active = document.querySelector('#timeline li[data-seq="' + hop.seq + '"]');
      if (active) {
        active.classList.add("hop-active");
        active.scrollIntoView({ block: "nearest" });
      }
    }

    function updateUi() {
      if (els.counter) {
        els.counter.textContent = hops.length ? "Hop " + (idx + 1) + " of " + hops.length : "No hops";
      }
      if (els.play) els.play.textContent = timer ? "Pause" : "Play";
      if (els.prev) els.prev.disabled = idx <= 0;
      if (els.next) els.next.disabled = idx >= hops.length - 1;
    }

    function goTo(i) {
      if (i < 0 || i >= hops.length) return;
      idx = i;
      ctrl.cancelAutoFit();
      cameraToHop(ctrl, hops[idx]);
      highlightTimeline(hops[idx]);
      updateUi();
    }

    function stop() {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      updateUi();
    }

    function scheduleNext() {
      timer = setTimeout(function () {
        if (idx >= hops.length - 1) {
          stop();
          return;
        }
        goTo(idx + 1);
        scheduleNext();
      }, CAMERA_MS);
    }

    function play() {
      if (timer || !hops.length) return;
      goTo(idx < 0 ? 0 : Math.min(idx + 1, hops.length - 1));
      if (idx < hops.length - 1) scheduleNext();
      updateUi();
    }

    if (els.play) {
      els.play.addEventListener("click", function () {
        if (timer) stop();
        else play();
      });
    }
    if (els.prev) {
      els.prev.addEventListener("click", function () {
        stop();
        goTo(idx - 1);
      });
    }
    if (els.next) {
      els.next.addEventListener("click", function () {
        stop();
        goTo(idx + 1);
      });
    }

    updateUi();
    return { play: play, stop: stop, goTo: goTo };
  }

  function replay(containerId, traceId) {
    var graphRequest = fetch("/api/graph").then(function (r) {
      return r.json();
    });
    var traceRequest = fetch("/api/traces/" + encodeURIComponent(traceId)).then(function (r) {
      return r.json();
    });
    return Promise.all([graphRequest, traceRequest]).then(function (results) {
      var graphData = results[0];
      var trace = results[1];
      var Graph3D = global.Graph3D;
      if (!Graph3D) return null;

      var universe = Graph3D.buildUniverse(graphData);
      var hops = visitedHops(trace);
      universe.links = universe.links.concat(hopLinks(hops));

      var ctrl = Graph3D.mount(document.getElementById(containerId), universe, {
        navigate: false,
        autoRotate: false,
      });
      if (!ctrl) return null;

      var overlay = applyOverlay3D(ctrl, trace);
      var pulseTimer = startPulse(ctrl, overlay.hits);
      var playback = makePlayback(ctrl, hops, {
        play: document.getElementById("replay-play"),
        prev: document.getElementById("replay-prev"),
        next: document.getElementById("replay-next"),
        counter: document.getElementById("replay-counter"),
      });

      window.addEventListener("beforeunload", function () {
        playback.stop();
        if (pulseTimer) clearInterval(pulseTimer);
      });

      return ctrl.graph;
    });
  }

  var api = {
    ACCENT: ACCENT,
    nodeId: nodeId,
    isConceptPath: isConceptPath,
    visitedNodes: visitedNodes,
    visitedHops: visitedHops,
    searchHitNodes: searchHitNodes,
    computeTraceOverlay: computeTraceOverlay,
    hopLinks: hopLinks,
    applyOverlay3D: applyOverlay3D,
    replay: replay,
  };
  global.TraceReplay = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

(function (global) {
  "use strict";

  var ACCENT = "#f1c40f";
  var PULSE_INTERVAL_MS = 450;
  var RESERVED = { "index.md": true, "log.md": true };
  var WRITE_TOOLS = {
    write_concept: true,
    edit_concept: true,
    move_concept: true,
    deprecate_concept: true,
    delete_concept: true,
  };

  // Playback pacing follows the sunburst zoom tween duration.
  function zoomMs() {
    var sb = global.GraphSunburst;
    return (sb && sb.CONFIG && sb.CONFIG.zoomMs) || 650;
  }

  function nodeId(path) {
    if (typeof path !== "string" || !path) return null;
    return path.endsWith(".md") ? path.slice(0, -3) : path;
  }

  function isConceptPath(path) {
    if (typeof path !== "string" || !path.endsWith(".md")) return false;
    var name = path.slice(path.lastIndexOf("/") + 1);
    return !RESERVED[name];
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

  // Sunburst overlay object (graph_sunburst.js setOverlay): visited/hit
  // states, first hop number per visited id, hop edges as {source, target}.
  function buildOverlay(trace, universeNodeIds) {
    var overlay = computeTraceOverlay(universeNodeIds, trace);
    var hops = visitedHops(trace);
    var states = {};
    overlay.visited.forEach(function (id) {
      states[id] = "visited";
    });
    overlay.hits.forEach(function (id) {
      states[id] = "hit";
    });
    var hopNumbers = {};
    hops.forEach(function (h) {
      if (hopNumbers[h.id] == null) hopNumbers[h.id] = h.hop;
    });
    var hopEdges = hopLinks(hops).map(function (l) {
      return { source: l.source, target: l.target };
    });
    return {
      accent: ACCENT,
      states: states,
      hopNumbers: hopNumbers,
      hopEdges: hopEdges,
      fadeRest: true,
      pulsePhase: false,
      hits: overlay.hits, // pulsed by startPulse; ignored by the sunburst renderer
    };
  }

  // Pulse hit nodes by toggling the stored overlay's phase and re-applying it.
  function startPulse(ctrl, hits, overlay) {
    if (!hits.length) return null;
    return setInterval(function () {
      overlay.pulsePhase = !overlay.pulsePhase;
      ctrl.setOverlay(overlay);
    }, PULSE_INTERVAL_MS);
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
      ctrl.focusNode(hops[idx].id, true);
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
      }, zoomMs());
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
    var graphRequest = fetch("/api/graph/universe?metric=link_density").then(function (r) {
      return r.json();
    });
    var traceRequest = fetch("/api/traces/" + encodeURIComponent(traceId)).then(function (r) {
      return r.json();
    });
    return Promise.all([graphRequest, traceRequest]).then(function (results) {
      var universeData = results[0];
      var trace = results[1];
      var GraphSunburst = global.GraphSunburst;
      if (!GraphSunburst) return null;

      var universeNodeIds = ((universeData && universeData.nodes) || []).map(function (n) {
        return n.id;
      });
      var inUniverse = {};
      universeNodeIds.forEach(function (id) {
        inUniverse[id] = true;
      });
      // Playback only steps through nodes that exist in the universe.
      var hops = visitedHops(trace).filter(function (h) {
        return inUniverse[h.id];
      });
      var els = {
        play: document.getElementById("replay-play"),
        prev: document.getElementById("replay-prev"),
        next: document.getElementById("replay-next"),
        counter: document.getElementById("replay-counter"),
      };

      var container = document.getElementById(containerId);
      var ctrl = GraphSunburst.mount(container, universeData, { navigate: false });
      if (!ctrl) {
        // Empty library: keep the controls wired so the UI never dead-ends.
        if (container) {
          var empty = document.createElement("p");
          empty.className = "muted";
          empty.textContent = "No documents in the library yet.";
          container.appendChild(empty);
        }
        makePlayback({ focusNode: function () {} }, hops, els);
        return null;
      }

      var overlay = buildOverlay(trace, universeNodeIds);
      ctrl.setOverlay(overlay);
      var pulseTimer = startPulse(ctrl, overlay.hits, overlay);
      var playback = makePlayback(ctrl, hops, els);

      window.addEventListener("beforeunload", function () {
        playback.stop();
        if (pulseTimer) clearInterval(pulseTimer);
      });

      return ctrl;
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
    buildOverlay: buildOverlay,
    replay: replay,
  };
  global.TraceReplay = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

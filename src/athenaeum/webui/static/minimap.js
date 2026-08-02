/* Minimap (SUNBURST-ONLY rework).
 *
 * Small always-visible 2D overview map pinned bottom-left of the graph
 * container (like the reference screenshots): the sunburst scene in
 * miniature — the center ring plus every sector dot in its cluster color,
 * thin sector dividers — and a white viewport rectangle whenever the main
 * view is zoomed in. Clicking a sector zooms the main view to it; clicking
 * anything else returns to the overview.
 *
 * The module is data-agnostic: the owner (graph.html) provides getScene() /
 * getViewport() callbacks (both from the GraphSunburst handle, so the
 * miniature always matches the main canvas) and receives onSectorClick /
 * onOverviewClick. Redraws are event-driven and rAF-throttled — no permanent
 * animation loop.
 */
(function (global) {
  "use strict";

  var CONFIG = {
    size: 180, // css px, square
    padding: 14, // css px inside the canvas
    dotRadius: 1.1, // css px per document dot
    dividerColor: "rgba(255,255,255,0.14)",
    viewportColor: "rgba(255,255,255,0.9)",
    anchorColor: "#e2a84b" // app dark-theme --clr-accent
  };

  function hexToRgb(hex) {
    var n = parseInt(String(hex).slice(1), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb.r + "," + rgb.g + "," + rgb.b + "," + alpha + ")";
  }

  // opts: {
  //   getScene: fn -> {dots: [{x, y, color}], sectors: [{id, start, end}],
  //                    sectorInner} (scene units, disc radius 1),
  //   getViewport: fn -> {x0, y0, x1, y1} in scene units, or null at overview,
  //   onSectorClick: fn(id),
  //   onOverviewClick: fn()
  // }
  function mount(container, opts) {
    opts = opts || {};
    if (typeof container === "string") container = document.getElementById(container);
    if (!container) return null;

    var canvas = document.createElement("canvas");
    canvas.className = "graph-minimap-canvas";
    container.appendChild(canvas);
    var ctx = canvas.getContext("2d");
    var dpr = Math.max(global.devicePixelRatio || 1, 1);
    var size = CONFIG.size;
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);

    var scene = { dots: [], sectors: [], sectorInner: 0.2 };

    function mapR() {
      return size / 2 - CONFIG.padding;
    }

    function toMap(p) {
      return { x: size / 2 + p.x * mapR(), y: size / 2 + p.y * mapR() };
    }

    function fromMap(x, y) {
      return { x: (x - size / 2) / mapR(), y: (y - size / 2) / mapR() };
    }

    function draw() {
      scene = typeof opts.getScene === "function" ? opts.getScene() || scene : scene;
      var vp = typeof opts.getViewport === "function" ? opts.getViewport() : null;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);
      if (!scene.dots.length) return;

      // Sector dividers.
      ctx.strokeStyle = CONFIG.dividerColor;
      ctx.lineWidth = 1;
      (scene.sectors || []).forEach(function (sec) {
        var a = toMap({ x: Math.cos(sec.start), y: Math.sin(sec.start) });
        ctx.beginPath();
        ctx.moveTo(size / 2, size / 2);
        ctx.lineTo(a.x, a.y);
        ctx.stroke();
      });

      // Document dots (center ring + sectors) in cluster colors.
      (scene.dots || []).forEach(function (d) {
        var m = toMap(d);
        var rgb = hexToRgb(d.color || "#90a4ae");
        ctx.fillStyle = rgba(rgb, 0.85);
        ctx.beginPath();
        ctx.arc(m.x, m.y, CONFIG.dotRadius, 0, Math.PI * 2);
        ctx.fill();
      });

      // Center anchor.
      ctx.fillStyle = rgba(hexToRgb(CONFIG.anchorColor), 0.95);
      ctx.beginPath();
      ctx.arc(size / 2, size / 2, 2.2, 0, Math.PI * 2);
      ctx.fill();

      // Viewport rectangle while zoomed in.
      if (vp) {
        var tl = toMap({ x: vp.x0, y: vp.y0 });
        var br = toMap({ x: vp.x1, y: vp.y1 });
        ctx.strokeStyle = CONFIG.viewportColor;
        ctx.lineWidth = 1;
        ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
      }
    }

    // rAF-throttled redraw: the owner may fire on every zoom frame.
    var pending = false;
    function update() {
      if (pending) return;
      pending = true;
      global.requestAnimationFrame(function () {
        pending = false;
        draw();
      });
    }

    // Sector id at a scene point (same angle convention as the main view).
    function sectorAt(p) {
      var r = Math.sqrt(p.x * p.x + p.y * p.y);
      if (r < (scene.sectorInner || 0.2) || r > 1) return null;
      var a = Math.atan2(p.y, p.x);
      while (a < -Math.PI / 2) a += Math.PI * 2;
      while (a >= (Math.PI * 3) / 2) a -= Math.PI * 2;
      for (var i = 0; i < (scene.sectors || []).length; i++) {
        var sec = scene.sectors[i];
        if (a >= sec.start && a <= sec.end) return sec.id;
      }
      return null;
    }

    function onClick(e) {
      var rect = canvas.getBoundingClientRect();
      var p = fromMap(e.clientX - rect.left, e.clientY - rect.top);
      var id = sectorAt(p);
      if (id && typeof opts.onSectorClick === "function") opts.onSectorClick(id);
      else if (!id && typeof opts.onOverviewClick === "function") opts.onOverviewClick();
    }

    function onMove(e) {
      var rect = canvas.getBoundingClientRect();
      canvas.style.cursor = sectorAt(fromMap(e.clientX - rect.left, e.clientY - rect.top)) ? "pointer" : "";
    }

    function onLeave() {
      canvas.style.cursor = "";
    }

    canvas.addEventListener("click", onClick);
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);

    update(); // initial draw

    return {
      update: update,
      dispose: function () {
        canvas.removeEventListener("click", onClick);
        canvas.removeEventListener("mousemove", onMove);
        canvas.removeEventListener("mouseleave", onLeave);
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      }
    };
  }

  var api = { CONFIG: CONFIG, mount: mount };
  global.GraphMinimap = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

/* Document view (tree page takeover): in-page note selection with linked-note
   highlighting (tree + sunburst minimap overlay), minimap flight, the
   time-machine slider (inline diff vs HEAD), and the inline edit toggle.
   All behavior lives here — inline per-page handlers do not survive htmx
   tree-pane swaps. Vanilla JS, no dependencies (GraphSunburst global). */
(function (global) {
  "use strict";

  var ACCENT = "#e2a84b"; // --clr-accent (dark theme)

  var state = {
    cfg: null, // boot config from the server-rendered page
    universe: null, // fetched /api/graph/universe payload
    minimap: null, // GraphSunburst handle, or null (empty library)
    path: null, // currently selected document path
    linked: {}, // nodeId -> true (outgoing + backlinks of the selection)
    pendingFlight: false, // a selection was made while the minimap was still mounting
    debounceTimer: null,
  };

  function nodeId(path) {
    if (typeof path !== "string" || !path) return null;
    return path.endsWith(".md") ? path.slice(0, -3) : path;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function hiddenInput(name, value) {
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    return input;
  }

  function csrfInput() {
    return hiddenInput("csrf_token", (state.cfg && state.cfg.csrf) || "");
  }

  /* ----- linked-note computation (universe payload edges) ----- */

  function computeLinked(id) {
    state.linked = {};
    if (!id || !state.universe) return;
    (state.universe.edges || []).forEach(function (e) {
      if (e.source === id) state.linked[e.target] = true; // outgoing
      else if (e.target === id) state.linked[e.source] = true; // backlink
    });
  }

  function applyOverlay() {
    if (!state.minimap) return;
    var ids = Object.keys(state.linked);
    if (!ids.length) {
      state.minimap.setOverlay(null);
      return;
    }
    var states = {};
    ids.forEach(function (id) {
      states[id] = "visited";
    });
    state.minimap.setOverlay({ accent: ACCENT, states: states, fadeRest: true });
  }

  function applyTreeHighlights() {
    var current = nodeId(state.path);
    var links = document.querySelectorAll("#docview-tree-pane [data-doc-path]");
    for (var i = 0; i < links.length; i++) {
      var id = nodeId(links[i].getAttribute("data-doc-path"));
      links[i].classList.toggle("tree-current", id != null && id === current);
      links[i].classList.toggle("tree-linked", !!state.linked[id]);
    }
  }

  /* ----- center pane rendering (mirrors document_view.html) ----- */

  function badge(cls, text) {
    return el("span", "badge " + cls, text);
  }

  function renderHeader(p) {
    var header = el("div", "page-header page-header-sticky");
    header.id = "docview-header";
    var title = el("h1", null, p.title || p.path);
    title.id = "docview-title";
    header.appendChild(title);
    var badges = el("div", "badges mt-2");
    badges.id = "docview-badges";
    badges.appendChild(badge("badge-info", p.type || "unknown"));
    (p.tags || []).forEach(function (tag) {
      badges.appendChild(badge("badge-neutral", tag));
    });
    badges.appendChild(badge("badge-success", p.status || "stable"));
    badges.appendChild(badge("badge-trust badge-" + p.trust, p.trust));
    if (p.stale) badges.appendChild(badge("badge-warning", "stale"));
    header.appendChild(badges);
    if (p.description) {
      var desc = el("p", "subtitle mt-2", p.description);
      desc.id = "docview-description";
      header.appendChild(desc);
    }
    var actions = el("div", "mt-2");
    actions.id = "docview-actions";
    var editToggle = el("button", "btn btn-secondary btn-sm", "Edit");
    editToggle.type = "button";
    editToggle.id = "docview-edit-toggle";
    actions.appendChild(editToggle);
    var deleteForm = document.createElement("form");
    deleteForm.method = "post";
    deleteForm.action = "/library/document/delete";
    deleteForm.className = "inline";
    deleteForm.id = "delete-form";
    deleteForm.setAttribute(
      "data-confirm",
      "Delete this note? The deletion is recorded as a new commit."
    );
    deleteForm.setAttribute("data-loading", "");
    deleteForm.appendChild(csrfInput());
    deleteForm.appendChild(hiddenInput("path", p.path));
    var deleteBtn = el("button", "btn btn-sm btn-danger", "Delete");
    deleteBtn.type = "submit";
    deleteForm.appendChild(deleteBtn);
    actions.appendChild(deleteForm);
    header.appendChild(actions);
    return header;
  }

  function renderHistory(p) {
    var card = el("div", "card");
    card.id = "docview-history";
    if (!p.history_available) {
      var unavailable = el("div", "card-body");
      unavailable.appendChild(el("p", "muted", "Git history is unavailable."));
      var hint = el("p", "muted");
      if (!p.history_configured) {
        hint.appendChild(document.createTextNode("Enable it in "));
        var settingsLink = el("a", "text-accent", "Library settings");
        settingsLink.href = "/config/library";
        hint.appendChild(settingsLink);
        hint.appendChild(document.createTextNode("."));
      } else {
        hint.textContent = "git binary not found on this server.";
      }
      unavailable.appendChild(hint);
      card.appendChild(unavailable);
      return card;
    }
    var timeline = p.timeline || [];
    if (!timeline.length) {
      var empty = el("div", "card-body");
      empty.appendChild(
        el(
          "p",
          "muted",
          "No history yet — the first write touching this document creates its first commit."
        )
      );
      card.appendChild(empty);
      return card;
    }
    card.appendChild(el("div", "card-header", "History"));
    var body = el("div", "card-body");
    var slider = document.createElement("input");
    slider.type = "range";
    slider.id = "history-slider";
    slider.min = "0";
    slider.max = String(timeline.length - 1);
    slider.value = String(p.viewed_index);
    slider.step = "1";
    body.appendChild(slider);
    var ticks = el("div", "history-ticks");
    ticks.id = "history-ticks";
    timeline.forEach(function (c, i) {
      var tick = el("button", "history-tick" + (i === p.viewed_index ? " active" : ""));
      tick.type = "button";
      tick.setAttribute("data-index", String(i));
      tick.title = c.short + " · " + String(c.timestamp || "").slice(0, 10);
      ticks.appendChild(tick);
    });
    body.appendChild(ticks);
    var label = el("p", "range-label");
    label.id = "history-label";
    body.appendChild(label);
    var note = el("p", "muted");
    note.id = "preview-note";
    note.hidden = true;
    body.appendChild(note);
    var form = document.createElement("form");
    form.method = "post";
    form.action = "/library/document/restore";
    form.id = "restore-form";
    form.setAttribute(
      "data-confirm",
      "Restore this document to the selected commit? The restore is recorded as a new commit."
    );
    form.setAttribute("data-loading", "");
    form.hidden = true; // in-page selections always land on the live view
    form.appendChild(csrfInput());
    form.appendChild(hiddenInput("path", p.path));
    var sha = hiddenInput("sha", "");
    sha.id = "restore-sha";
    form.appendChild(sha);
    var restoreBtn = el("button", "btn btn-primary", "Restore this version");
    restoreBtn.type = "submit";
    form.appendChild(restoreBtn);
    body.appendChild(form);
    card.appendChild(body);
    return card;
  }

  function renderEditForm(p) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = "/library/document/edit";
    form.id = "edit-form";
    form.setAttribute("data-loading", "");
    form.hidden = true;
    form.appendChild(csrfInput());
    form.appendChild(hiddenInput("path", p.path));
    var row = el("div", "form-row");
    var textarea = document.createElement("textarea");
    textarea.name = "body";
    textarea.id = "edit-body";
    textarea.rows = 16;
    textarea.value = p.body || "";
    row.appendChild(textarea);
    form.appendChild(row);
    var save = el("button", "btn btn-primary", "Save");
    save.type = "submit";
    form.appendChild(save);
    var cancel = el("button", "btn btn-secondary", "Cancel");
    cancel.type = "button";
    cancel.id = "edit-cancel";
    form.appendChild(cancel);
    return form;
  }

  function renderDoc(p) {
    var top = document.getElementById("docview-top");
    var root = document.getElementById("docview-doc");
    if (!top || !root) return;
    // Top cell (grid row 1, beside the minimap): header, history, banner.
    // Clearing it also drops the empty-state card.
    top.textContent = "";
    top.appendChild(renderHeader(p));
    top.appendChild(renderHistory(p));
    // The banner is a server-rendered deep-link feature; in-page selections
    // always land on the live view, so it stays hidden here.
    var banner = el("div", "card");
    banner.id = "docview-banner";
    banner.hidden = true;
    banner.appendChild(el("div", "card-body"));
    top.appendChild(banner);
    // Document cell (grid row 2, beside the tree): the markdown area.
    root.textContent = "";
    var card = el("div", "card");
    var cardBody = el("div", "card-body");
    var rendered = el("div", "doc-body");
    rendered.id = "md-rendered";
    rendered.innerHTML = p.body_html; // trusted: server-rendered markdown
    cardBody.appendChild(rendered);
    cardBody.appendChild(renderEditForm(p));
    card.appendChild(cardBody);
    root.appendChild(card);
    root.hidden = false;
    wireEditToggle();
    initSlider(p.timeline || [], p.path, true);
  }

  /* ----- edit toggle ----- */

  function wireEditToggle() {
    var toggle = document.getElementById("docview-edit-toggle");
    var form = document.getElementById("edit-form");
    var cancel = document.getElementById("edit-cancel");
    var body = document.getElementById("md-rendered");
    if (!toggle || !form || !cancel || !body) return;
    toggle.addEventListener("click", function () {
      form.hidden = false;
      body.hidden = true;
      toggle.disabled = true;
    });
    cancel.addEventListener("click", function () {
      form.hidden = true;
      body.hidden = false;
      toggle.disabled = false;
    });
  }

  /* ----- history slider (inline diff vs HEAD; rightmost stop = live) ----- */

  function initSlider(commits, docPath, landedLive) {
    var slider = document.getElementById("history-slider");
    if (!slider || !commits.length) return;
    var label = document.getElementById("history-label");
    var body = document.getElementById("md-rendered");
    var liveBodyHtml = landedLive ? body.innerHTML : null;
    var restoreForm = document.getElementById("restore-form");
    var restoreSha = document.getElementById("restore-sha");
    var previewNote = document.getElementById("preview-note");
    var ticks = document.querySelectorAll("#docview-history .history-tick");
    function updateLabel() {
      var c = commits[Number(slider.value)];
      label.textContent = c.short + " · " + String(c.timestamp || "").slice(0, 10) + " · " + c.subject;
    }
    function updateTicks() {
      var current = Number(slider.value);
      ticks.forEach(function (t) {
        t.classList.toggle("active", Number(t.getAttribute("data-index")) === current);
      });
    }
    ticks.forEach(function (t) {
      t.addEventListener("click", function () {
        slider.value = t.getAttribute("data-index");
        slider.dispatchEvent(new Event("input"));
      });
    });
    function showLive() {
      if (!landedLive) {
        /* Deep-linked historical view: the live body was never rendered, so
           the live stop navigates to the plain live URL instead. */
        global.location = "/library/tree?path=" + encodeURIComponent(docPath);
        return;
      }
      body.innerHTML = liveBodyHtml;
      restoreForm.hidden = true;
      previewNote.hidden = true;
    }
    function showPreview(sha) {
      fetch(
        "/library/document/diff?path=" +
          encodeURIComponent(docPath) +
          "&sha=" +
          encodeURIComponent(sha) +
          "&mode=inline"
      )
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .then(function (data) {
          if (!data) return;
          var c = commits[Number(slider.value)];
          if (c.sha !== sha) return; /* stale response, slider moved on */
          body.innerHTML = data.diff_html || '<p class="muted">No changes vs current version.</p>';
          previewNote.textContent = "Preview of " + c.short + " vs current version.";
          previewNote.hidden = false;
          restoreSha.value = sha;
          restoreForm.hidden = false;
        });
    }
    slider.addEventListener("input", function () {
      updateLabel();
      updateTicks();
      var i = Number(slider.value);
      if (state.debounceTimer) {
        clearTimeout(state.debounceTimer);
        state.debounceTimer = null;
      }
      if (i === commits.length - 1) {
        showLive();
      } else {
        var sha = commits[i].sha;
        state.debounceTimer = setTimeout(function () {
          showPreview(sha);
        }, 150);
      }
    });
    updateLabel();
    updateTicks();
  }

  /* ----- selection ----- */

  function select(path) {
    state.path = path;
    var id = nodeId(path);
    computeLinked(id);
    applyOverlay();
    applyTreeHighlights();
    if (state.minimap) {
      if (id) state.minimap.flightTo(id, true);
      else state.minimap.resetView(true);
    } else {
      // Minimap still mounting (universe fetch in flight): the boot handler
      // picks up state.path on arrival and must animate, not snap.
      state.pendingFlight = true;
    }
    fetch("/library/document/data?path=" + encodeURIComponent(path))
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (p) {
        if (!p || p.path !== state.path) return; // stale response, moved on
        renderDoc(p);
      })
      .catch(function () {});
    global.history.replaceState(null, "", "/library/tree?path=" + encodeURIComponent(path));
  }

  /* ----- boot ----- */

  function boot(cfg) {
    state.cfg = cfg || {};
    state.path = state.cfg.path || null;
    if (state.path) {
      wireEditToggle();
      initSlider(state.cfg.timeline || [], state.path, state.cfg.landedLive !== false);
      computeLinked(nodeId(state.path)); // empty until the universe arrives
      applyTreeHighlights();
    }
    fetch("/api/graph/universe?metric=link_density")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        state.universe = data;
        var GraphSunburst = global.GraphSunburst;
        var container = document.getElementById("docview-minimap");
        if (!GraphSunburst || !container) return;
        var ctrl = GraphSunburst.mount(container, data, { navigate: false });
        if (!ctrl) {
          // Empty library (mount returns null): the page stays functional.
          container.appendChild(el("p", "muted", "No documents in the library yet."));
          return;
        }
        state.minimap = ctrl;
        global.addEventListener("beforeunload", function () {
          ctrl.dispose();
        });
        if (state.path) {
          computeLinked(nodeId(state.path));
          applyOverlay();
          applyTreeHighlights();
          // Initial server-rendered positioning snaps (animate=false); a
          // selection made while the minimap was mounting must animate.
          ctrl.flightTo(nodeId(state.path), state.pendingFlight);
          state.pendingFlight = false;
        }
      })
      .catch(function () {});

    /* Delegated clicks: tree entries select in-page (htmx swaps survive);
       in-document links to absolute .md bundle paths select too. */
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var entry = target.closest("#docview-tree-pane [data-doc-path]");
      if (entry) {
        event.preventDefault();
        select(entry.getAttribute("data-doc-path"));
        return;
      }
      var mdLink = target.closest("#md-rendered a[href]");
      if (mdLink) {
        var href = mdLink.getAttribute("href");
        if (href && href.charAt(0) === "/" && href.endsWith(".md")) {
          event.preventDefault();
          select(href);
        }
      }
    });

    /* Lazily expanded tree folders need the highlights re-applied. */
    document.addEventListener("htmx:afterSwap", function (event) {
      var target = event.detail && event.detail.target;
      if (target && target.closest && target.closest("#docview-tree-pane")) {
        applyTreeHighlights();
      }
    });
  }

  var api = { boot: boot, select: select, nodeId: nodeId };
  global.DocumentView = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

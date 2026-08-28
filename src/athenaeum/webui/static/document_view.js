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
    dataCache: new Map(), // path -> /document/data JSON (page-load lifetime)
    diffCache: new Map(), // path + "@" + sha -> diff_html (page-load lifetime)
    selectAbort: null, // AbortController of the in-flight selection fetch
    diffAbort: null, // AbortController of the in-flight diff-preview fetch
    dirtySource: null, // textarea value when the editor was opened (edit guard)
    editDirty: null, // set in boot: () -> bool, unsaved editor changes pending
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

  /* Collapsible folders (V11): collapse state lives as .collapsed on the
     <li> (CSS hides li.collapsed > ul); aria-expanded on the expander is
     "true" iff the <li> is loaded and not collapsed. */
  function setExpanded(btn, expanded) {
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    var li = btn.closest("li");
    var dirname = li && li.querySelector(":scope > .dirname");
    var name = dirname ? dirname.textContent.replace(/\/+$/, "") : "";
    btn.setAttribute("aria-label", (expanded ? "Collapse " : "Expand ") + name);
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
    slider.setAttribute("aria-label", "Document history");
    body.appendChild(slider);
    var ticks = el("div", "history-ticks");
    ticks.id = "history-ticks";
    timeline.forEach(function (c, i) {
      var tick = el("button", "history-tick" + (i === p.viewed_index ? " active" : ""));
      tick.type = "button";
      tick.setAttribute("data-index", String(i));
      tick.title = c.short + " · " + String(c.timestamp || "").slice(0, 10);
      tick.setAttribute("aria-label", tick.title);
      ticks.appendChild(tick);
    });
    body.appendChild(ticks);
    var label = el("p", "range-label");
    label.id = "history-label";
    label.setAttribute("aria-live", "polite");
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
    document.title = (p.title || p.path) + " - Athenaeum";
    // Top cell (grid row 1, beside the minimap): header and history. Clearing
    // it wholesale also drops the empty-state card AND the server-rendered
    // historical banner on ?sha= pages: in-page selections always land on the
    // live view, so the banner is a deep-link-only feature (F22/F26).
    top.textContent = "";
    top.appendChild(renderHeader(p));
    top.appendChild(renderHistory(p));
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

  /* ----- edit toggle + unsaved-edit guard ----- */

  function editDirty() {
    var form = document.getElementById("edit-form");
    var textarea = document.getElementById("edit-body");
    if (!form || !textarea || form.hidden) return false;
    return textarea.value !== state.dirtySource;
  }

  function confirmDiscardEdits() {
    if (!editDirty()) return true;
    return global.confirm("Discard unsaved edits?");
  }

  function wireEditToggle() {
    var toggle = document.getElementById("docview-edit-toggle");
    var form = document.getElementById("edit-form");
    var cancel = document.getElementById("edit-cancel");
    var body = document.getElementById("md-rendered");
    var textarea = document.getElementById("edit-body");
    if (!toggle || !form || !cancel || !body || !textarea) return;
    toggle.addEventListener("click", function () {
      state.dirtySource = textarea.value; // baseline for the edit guard
      form.hidden = false;
      body.hidden = true;
      toggle.disabled = true;
    });
    cancel.addEventListener("click", function () {
      form.hidden = true; // discarding clears the guard (form hidden)
      body.hidden = false;
      toggle.disabled = false;
    });
    form.addEventListener("submit", function () {
      // Saving must not trip the beforeunload guard during the POST.
      state.dirtySource = textarea.value;
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
           the live stop loads the live document in-page. renderDoc rebuilds
           #docview-top wholesale, which also drops the server-rendered
           historical banner and the restore form state (F22). */
        api.select(docPath);
        return;
      }
      body.innerHTML = liveBodyHtml;
      restoreForm.hidden = true;
      previewNote.hidden = true;
    }
    function applyPreview(sha, diffHtml) {
      var c = commits[Number(slider.value)];
      if (c.sha !== sha) return; /* stale response, slider moved on */
      body.innerHTML = diffHtml || '<p class="muted">No changes vs current version.</p>';
      previewNote.textContent = "Preview of " + c.short + " vs current version.";
      previewNote.hidden = false;
      restoreSha.value = sha;
      restoreForm.hidden = false;
    }
    function showPreview(sha) {
      var cacheKey = docPath + "@" + sha;
      if (state.diffCache.has(cacheKey)) {
        applyPreview(sha, state.diffCache.get(cacheKey));
        return;
      }
      if (state.diffAbort) state.diffAbort.abort(); // superseded request
      var controller = (state.diffAbort = new AbortController());
      var docRoot = document.getElementById("docview-doc");
      if (docRoot) docRoot.classList.add("doc-loading");
      fetch(
        "/library/document/diff?path=" +
          encodeURIComponent(docPath) +
          "&sha=" +
          encodeURIComponent(sha) +
          "&mode=inline",
        { signal: controller.signal }
      )
        .then(function (r) {
          if (!r.ok) throw new Error("diff preview failed: HTTP " + r.status);
          return r.json();
        })
        .then(function (data) {
          state.diffCache.set(cacheKey, data.diff_html || "");
          applyPreview(sha, data.diff_html);
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return; // superseded, not an error
          window.showToast("Could not load diff preview.", "danger");
        })
        .finally(function () {
          if (docRoot && state.diffAbort === controller) {
            docRoot.classList.remove("doc-loading");
          }
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

  /* opts.push === false re-selects without touching history (popstate). The
     public signature stays select(path); only internal callers pass opts. */
  function select(path, opts) {
    var push = !opts || opts.push !== false;
    if (!confirmDiscardEdits()) return; // unsaved-edit guard (F5)
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
    function applyPayload(p) {
      renderDoc(p);
      // The URL changes only after a successful load (F34): a failed
      // selection keeps the previous document and URL.
      if (push) {
        global.history.pushState(null, "", "/library/tree?path=" + encodeURIComponent(path));
      }
    }
    var cached = state.dataCache.get(path);
    if (cached) {
      applyPayload(cached);
      return;
    }
    if (state.selectAbort) state.selectAbort.abort(); // superseded request
    var controller = (state.selectAbort = new AbortController());
    var docRoot = document.getElementById("docview-doc");
    if (docRoot) docRoot.classList.add("doc-loading");
    fetch("/library/document/data?path=" + encodeURIComponent(path), {
      signal: controller.signal,
    })
      .then(function (r) {
        if (!r.ok) throw new Error("document fetch failed: HTTP " + r.status);
        return r.json();
      })
      .then(function (p) {
        if (!p || p.path !== state.path) return; // stale response, moved on
        state.dataCache.set(path, p);
        applyPayload(p);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return; // superseded, not an error
        window.showToast("Could not load document.", "danger");
      })
      .finally(function () {
        if (docRoot && state.selectAbort === controller) {
          docRoot.classList.remove("doc-loading");
        }
      });
  }

  /* ----- boot ----- */

  function boot(cfg) {
    state.cfg = cfg || {};
    state.path = state.cfg.path || null;
    state.editDirty = editDirty;
    if (state.path) {
      wireEditToggle();
      initSlider(state.cfg.timeline || [], state.path, state.cfg.landedLive !== false);
      computeLinked(nodeId(state.path)); // empty until the universe arrives
      applyTreeHighlights();
      /* Deep-link reveal: the server rendered the ancestor folders expanded
         (contract V10); scroll the selected note into view in the tree pane. */
      var paneLinks = document.querySelectorAll("#docview-tree-pane [data-doc-path]");
      for (var i = 0; i < paneLinks.length; i++) {
        if (paneLinks[i].getAttribute("data-doc-path") === state.path) {
          paneLinks[i].scrollIntoView({ block: "nearest" });
          break;
        }
      }
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
      .catch(function () {
        window.showToast("Could not load the graph overview.", "danger");
      });

    /* Back/forward: re-select the URL's document in-page without pushing a
       new history entry (the entry already exists). */
    global.addEventListener("popstate", function () {
      var path = new URLSearchParams(global.location.search).get("path");
      if (path && path !== state.path) {
        select(path, { push: false });
      }
    });

    /* Unsaved-edit guard on page leave (selections are guarded in select). */
    global.addEventListener("beforeunload", function (event) {
      if (editDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    /* Delegated clicks: tree entries select in-page (htmx swaps survive);
       in-document links to absolute .md bundle paths select too. Modifier
       clicks (new tab/window) keep their native behavior. */
    document.addEventListener("click", function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
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

    /* Collapsible folders (V11): expander and dirname clicks toggle the
       loaded folder. The first expansion stays on the htmx "click once"
       path — this handler returns while no child ul.tree exists (no
       preventDefault, so htmx still fires), and afterwards a collapse/
       re-expand cycle only toggles .collapsed: zero network requests. */
    document.addEventListener("click", function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
      var target = event.target;
      if (!target || !target.closest) return;
      var toggle = target.closest("#docview-tree-pane .expander, #docview-tree-pane .dirname");
      if (!toggle) return;
      var li = toggle.closest("li");
      if (!li || !li.querySelector(":scope > ul.tree")) return; // not loaded: htmx path
      li.classList.toggle("collapsed");
      var btn = li.querySelector(":scope > .expander");
      if (btn) setExpanded(btn, !li.classList.contains("collapsed"));
    });

    /* Failed lazy folder expansion: toast + re-arm the expander (htmx
       consumed the "click once" trigger, so swap in a fresh clone and let
       htmx re-process it — the retry then works without a reload). */
    document.addEventListener("htmx:responseError", function (event) {
      var target = event.detail && event.detail.target;
      if (!target || !target.closest || !target.closest("#docview-tree-pane")) return;
      window.showToast("Could not load folder.", "danger");
      if (target.parentNode && global.htmx) {
        var clone = target.cloneNode(true);
        target.parentNode.replaceChild(clone, target);
        global.htmx.process(clone);
      }
    });

    /* Lazily expanded tree folders need the highlights re-applied; the
       freshly loaded folder is expanded by definition (.collapsed absent),
       so mark its expander aria-expanded (V11). The requesting expander
       survives the outerHTML swap of its next-sibling <ul>; fall back to
       locating it from the swap target. */
    document.addEventListener("htmx:afterSwap", function (event) {
      var detail = event.detail || {};
      var target = detail.target;
      var inPane = target && target.closest && target.closest("#docview-tree-pane");
      var requester = detail.requestConfig && detail.requestConfig.elt;
      if (!inPane && !(requester && requester.closest && requester.closest("#docview-tree-pane"))) {
        return;
      }
      applyTreeHighlights();
      if (requester && requester.matches && requester.matches(".expander")) {
        setExpanded(requester, true);
        return;
      }
      if (inPane) {
        var li = target.closest("li");
        var btn = li && li.querySelector(":scope > .expander");
        if (btn && li.querySelector(":scope > ul.tree")) setExpanded(btn, true);
      }
    });
  }

  var api = { boot: boot, select: select, nodeId: nodeId };
  global.DocumentView = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

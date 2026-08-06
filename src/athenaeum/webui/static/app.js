/* Athenaeum WebUI — global client behavior.
   Vanilla JS, no dependencies. htmx handles the dynamic endpoints;
   this file covers theming, the mobile sidebar, toasts, the server-rendered
   flash bridge, and declarative data-* handlers. */
(function () {
  "use strict";

  var THEME_KEY = "athenaeum_theme";

  /* ----- Theme ----- */

  window.applyTheme = function (theme) {
    var root = document.documentElement;
    if (theme === "light") {
      root.classList.remove("dark");
    } else {
      theme = "dark";
      root.classList.add("dark");
    }
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch (e) {
      /* storage unavailable; cookie below still persists the choice */
    }
    document.cookie = THEME_KEY + "=" + theme + ";max-age=31536000;SameSite=Lax;path=/";
  };

  function currentTheme() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  /* ----- Toasts ----- */

  window.showToast = function (message, type, title) {
    var container = document.getElementById("toast-container");
    if (!container) {
      return;
    }
    var kind = ["success", "warning", "danger", "info"].indexOf(type) >= 0 ? type : "info";

    var toast = document.createElement("div");
    toast.className = "toast toast-" + kind;
    toast.setAttribute("role", "status");

    var body = document.createElement("div");
    body.className = "min-w-0";
    if (title) {
      var titleEl = document.createElement("div");
      titleEl.className = "toast-title";
      titleEl.textContent = title;
      body.appendChild(titleEl);
    }
    var msgEl = document.createElement("div");
    msgEl.className = "toast-message";
    msgEl.textContent = message;
    body.appendChild(msgEl);
    toast.appendChild(body);

    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "\u00d7";
    close.addEventListener("click", function () {
      toast.remove();
    });
    toast.appendChild(close);

    container.appendChild(toast);
    setTimeout(function () {
      toast.remove();
    }, 5000);
  };

  /* ----- UTC time inputs -----
     input[type="time"][data-utc-time] posts UTC HH:MM but shows browser-local
     time when JS is available; without JS the UTC value posts verbatim and
     the [data-utc-time-hint] label keeps saying "UTC". */

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function utcToLocalTime(value) {
    var match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(value || "");
    if (!match) {
      return null;
    }
    var now = new Date();
    var date = new Date(
      Date.UTC(now.getFullYear(), now.getMonth(), now.getDate(), Number(match[1]), Number(match[2]))
    );
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function localToUtcTime(value) {
    var match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(value || "");
    if (!match) {
      return null;
    }
    var now = new Date();
    var date = new Date(now.getFullYear(), now.getMonth(), now.getDate(), Number(match[1]), Number(match[2]));
    return pad2(date.getUTCHours()) + ":" + pad2(date.getUTCMinutes());
  }

  /* ----- UTC timestamps -> browser-local display -----
     <time data-utc="ISO"> renders server-side UTC text as a no-JS fallback;
     with JS the text is rewritten to local YYYY-MM-DD HH:MM. Runs on load
     and after every htmx swap (the Activity table polls every 5s). */

  function renderLocalTimes(root) {
    var els = root.querySelectorAll("time[data-utc]");
    for (var i = 0; i < els.length; i++) {
      var date = new Date(els[i].getAttribute("data-utc"));
      if (isNaN(date.getTime())) {
        continue;
      }
      els[i].textContent =
        date.getFullYear() +
        "-" + pad2(date.getMonth() + 1) +
        "-" + pad2(date.getDate()) +
        " " + pad2(date.getHours()) +
        ":" + pad2(date.getMinutes());
    }
  }

  /* ----- Copy helper ----- */

  function copySourceText(el) {
    var selector = el.getAttribute("data-copy");
    var src = null;
    if (selector) {
      src = document.querySelector(selector);
    }
    if (!src && el.parentElement) {
      src = el.parentElement.querySelector(".token-value, code, pre");
    }
    return src ? src.textContent.trim() : "";
  }

  /* ----- Wire up on DOM ready ----- */

  document.addEventListener("DOMContentLoaded", function () {
    var i;

    /* Theme toggle */
    var themeToggle = document.getElementById("theme-toggle");
    if (themeToggle) {
      themeToggle.addEventListener("click", function () {
        window.applyTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    }

    /* Mobile sidebar: hamburger + overlay click-to-close */
    var sidebarToggle = document.getElementById("sidebar-toggle");
    var overlay = document.getElementById("sidebar-overlay");
    if (sidebarToggle) {
      sidebarToggle.addEventListener("click", function () {
        document.body.classList.toggle("sidebar-open");
      });
    }
    if (overlay) {
      overlay.addEventListener("click", function () {
        document.body.classList.remove("sidebar-open");
      });
    }

    /* Flash bridge: hidden server-rendered divs become toasts */
    var flashes = document.querySelectorAll("div.js-flash[data-type][data-message]");
    for (i = 0; i < flashes.length; i++) {
      window.showToast(flashes[i].getAttribute("data-message"), flashes[i].getAttribute("data-type"));
    }

    /* UTC time inputs: render UTC values as browser-local time */
    var utcInputs = document.querySelectorAll('input[type="time"][data-utc-time]');
    for (i = 0; i < utcInputs.length; i++) {
      var localValue = utcToLocalTime(utcInputs[i].value);
      if (localValue) {
        utcInputs[i].value = localValue;
        var scope = utcInputs[i].closest("form") || document;
        var hint = scope.querySelector("[data-utc-time-hint]");
        if (hint) {
          hint.textContent = "local time";
        }
      }
    }

    /* Server-rendered UTC timestamps shown in browser-local time */
    renderLocalTimes(document);
    document.addEventListener("htmx:afterSwap", function (event) {
      renderLocalTimes(event.detail.target || document);
    });

    /* Global delegation: data-confirm + data-loading on forms */
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || !form.getAttribute) {
        return;
      }
      var utcFields = form.querySelectorAll('input[type="time"][data-utc-time]');
      for (var j = 0; j < utcFields.length; j++) {
        var utcValue = localToUtcTime(utcFields[j].value);
        if (utcValue) {
          utcFields[j].value = utcValue;
        }
      }
      var question = form.getAttribute("data-confirm");
      if (question && !window.confirm(question)) {
        event.preventDefault();
        return;
      }
      if (form.hasAttribute("data-loading")) {
        var btn = form.querySelector('button[type="submit"]');
        if (btn) {
          btn.classList.add("btn-loading");
        }
      }
    });

    /* htmx requests: reflect loading state on [data-loading] buttons
       (a type="button" htmx trigger never submits its form, so the
       submit handler above cannot see it) */
    document.addEventListener("htmx:beforeRequest", function (event) {
      var elt = event.detail && event.detail.elt;
      if (elt && elt.hasAttribute && elt.hasAttribute("data-loading")) {
        elt.classList.add("btn-loading");
      }
    });
    document.addEventListener("htmx:afterRequest", function (event) {
      var elt = event.detail && event.detail.elt;
      if (elt && elt.hasAttribute && elt.hasAttribute("data-loading")) {
        elt.classList.remove("btn-loading");
      }
    });

    /* Global delegation: data-dismiss-click + data-copy */
    document.addEventListener("click", function (event) {
      var dismiss = event.target.closest("[data-dismiss-click]");
      if (dismiss) {
        var alertBox = dismiss.closest(".alert");
        if (alertBox) {
          alertBox.remove();
        }
        return;
      }
      var copyBtn = event.target.closest("[data-copy]");
      if (copyBtn) {
        var text = copySourceText(copyBtn);
        if (text && navigator.clipboard) {
          navigator.clipboard.writeText(text).then(function () {
            window.showToast("Copied to clipboard.", "success");
          });
        }
      }
    });
  });
})();

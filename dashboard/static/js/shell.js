/* App shell: sidebar collapse/drawer + attention chip polling (#183). */
(function () {
  "use strict";
  var POLL_MS = 10000;
  var MAX_MS = 20000; // idle: nothing changed for a while
  var NARROW = window.matchMedia("(max-width: 768px)");
  var KEY = "stira.sidebar";

  function $(id) { return document.getElementById(id); }

  function setChip(id, state, text) {
    var el = $(id);
    if (!el) return;
    el.dataset.state = state;
    var v = el.querySelector('[data-role="value"]');
    if (v) v.textContent = text;
  }

  function initSidebar() {
    var shell = $("app-shell");
    var btn = $("app-sidebar-toggle");
    var side = $("app-sidebar");
    var scrim = $("app-scrim");
    if (!shell || !btn || !side) return;

    function isOpen() { return shell.dataset.sidebar === "expanded"; }
    function apply(open, persist) {
      shell.dataset.sidebar = open ? "expanded" : "collapsed";
      btn.setAttribute("aria-expanded", String(open));
      var drawer = NARROW.matches;
      shell.classList.toggle("is-drawer", drawer);
      if (scrim) scrim.hidden = !(drawer && open);
      if (drawer && !open) side.setAttribute("inert", "");
      else side.removeAttribute("inert");
      if (persist && !drawer) {
        try { localStorage.setItem(KEY, open ? "expanded" : "collapsed"); } catch (e) { /* */ }
      }
    }
    function initial() {
      if (NARROW.matches) return false;
      try { return localStorage.getItem(KEY) !== "collapsed"; } catch (e) { return true; }
    }
    apply(initial(), false);

    btn.addEventListener("click", function () {
      var open = !isOpen();
      apply(open, true);
      if (open && NARROW.matches) {
        var first = side.querySelector("a, button");
        if (first) first.focus();
      }
    });
    if (scrim) scrim.addEventListener("click", function () { apply(false, false); btn.focus(); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && NARROW.matches && isOpen()) {
        apply(false, false);
        btn.focus();
      }
    });
    var onChange = function () { apply(initial(), false); };
    if (NARROW.addEventListener) NARROW.addEventListener("change", onChange);
    else if (NARROW.addListener) NARROW.addListener(onChange);
  }

  function getJSON(url) {
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function countPending(data) {
    if (Array.isArray(data)) return data.length;
    if (!data || typeof data !== "object") return 0;
    var keys = ["pending", "items", "requests", "approvals"];
    for (var i = 0; i < keys.length; i++) {
      if (Array.isArray(data[keys[i]])) return data[keys[i]].length;
    }
    if (typeof data.count === "number") return data.count;
    return 0;
  }

  function breakerLevel(data) {
    if (!data || typeof data !== "object") return "unknown";
    var b = data.breaker !== undefined ? data.breaker : data.circuit_breaker;
    var lv = (b && typeof b === "object") ? (b.level || b.state) : b;
    if (lv === undefined) lv = data.breaker_level;
    return typeof lv === "string" ? lv.toLowerCase() : "unknown";
  }

  var BREAKER_TEXT = {
    normal: ["ok", t("shell.normal")],
    warning: ["warn", t("shell.warning")],
    coherence: ["warn", t("shell.coherence")],
    critical: ["crit", t("shell.critical")],
  };

  // Per-app endpoints (install_shell): the observer has no HITL broker or gateway status.
  var shellEl = $("app-shell");
  var STATUS_URL = (shellEl && shellEl.dataset.statusUrl) || "/api/gateway/status";
  var APPROVALS_URL = shellEl ? shellEl.dataset.approvalsUrl || "" : "/api/hitl/pending";

  // Chips: asked every POLL_MS; a hidden tab does not ask, a failing server is asked less
  // often (poll.js, #369). The approvals chip also follows the cards' own event below.
  var poller = null;
  var lastChips = "";
  var lastCount = 0;

  function poll() {
    // A page with its own approval queue (chat, approvals) already reads /api/hitl/pending
    // far more often and publishes the count below: the chip does not ask a second time.
    var queueOnPage = Boolean($("approval-list"));
    var approvals = APPROVALS_URL && !queueOnPage ? getJSON(APPROVALS_URL) : Promise.resolve(null);
    return Promise.all([approvals, getJSON(STATUS_URL)])
      .then(function (res) {
        var n = lastCount;
        if (APPROVALS_URL && !queueOnPage) {
          n = countPending(res[0]);
          lastCount = n;
          setChip("nav-approvals", n > 0 ? "warn" : "ok", String(n));
        }
        var b = BREAKER_TEXT[breakerLevel(res[1])] || ["unknown", t("base.unknown")];
        setChip("nav-breaker", b[0], b[1]);
        setChip("nav-connection", "ok", t("shell.online"));
        var chips = n + "|" + b[0];
        var changed = chips !== lastChips;
        lastChips = chips;
        // A waiting approval keeps the short interval (the page shows it quickly).
        return changed || n > 0;
      }, function (error) {
        setChip("nav-connection", "crit", t("shell.offline"));
        throw error;
      });
  }

  // #189: approval cards publish the live queue size, so the chip never lags the cards.
  if (document.addEventListener) {
    document.addEventListener("stira:approvals", function (event) {
      var n = event && event.detail && typeof event.detail.count === "number" ? event.detail.count : null;
      if (APPROVALS_URL && n !== null) { lastCount = n; setChip("nav-approvals", n > 0 ? "warn" : "ok", String(n)); }
    });
  }

  function start() {
    if (poller !== null) return;
    if (window.StiraPoll) {
      poller = window.StiraPoll.create({ run: poll, base: POLL_MS, max: MAX_MS, doc: document });
      poller.start();
      return;
    }
    // Without poll.js (an isolated page): the plain timer, still paused while hidden.
    poll().catch(function () {});
    poller = { stop: function () {} };
    window.setInterval(function () { if (!document.hidden) poll().catch(function () {}); }, POLL_MS);
  }

  function initLang() {
    var select = $("app-lang");
    if (!select || !window.i18n) return;
    select.value = window.i18n.lang;
    select.addEventListener("change", function () { window.i18n.setLang(select.value); });
  }

  function init() {
    initSidebar();
    initLang();
    start();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

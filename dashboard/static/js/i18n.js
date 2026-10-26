// UI i18n runtime (#184): catalogs are embedded by the server as JSON (#i18n-catalog).
// t(key, vars) -> string ({name} placeholders); [data-i18n] / [data-i18n-<attr>] are applied on load.
(function () {
  var STORE = "stira.lang";
  var LANGS = ["ja", "en"];
  var node = document.getElementById("i18n-catalog");
  var catalog = { ja: {}, en: {} };
  try { if (node) catalog = JSON.parse(node.textContent); } catch (e) { /* keep empty */ }

  var lang = "ja";
  try {
    // ?lang=en|ja switches (and persists) the language, e.g. for shared links and screenshots.
    var wanted = new URLSearchParams(window.location.search).get("lang");
    if (LANGS.indexOf(wanted) !== -1) localStorage.setItem(STORE, wanted);
  } catch (e) { /* ignore */ }
  try {
    var saved = localStorage.getItem(STORE);
    if (LANGS.indexOf(saved) !== -1) lang = saved;
  } catch (e) { /* storage unavailable */ }

  function lookup(key) {
    var primary = catalog[lang] || {};
    if (Object.prototype.hasOwnProperty.call(primary, key)) return primary[key];
    var fallback = catalog.ja || {};
    return Object.prototype.hasOwnProperty.call(fallback, key) ? fallback[key] : undefined;
  }

  function t(key, vars) {
    var text = lookup(key);
    if (text === undefined) {
      if (window.console) console.warn("i18n: missing key", key);
      return key;
    }
    if (vars) {
      text = text.replace(/\{(\w+)\}/g, function (m, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : m;
      });
    }
    return text;
  }

  var ATTRS = ["placeholder", "aria-label", "title", "alt", "aria-valuetext"];
  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach(function (el) {
      var value = lookup(el.getAttribute("data-i18n"));
      if (value !== undefined) el.textContent = value; // unknown keys keep the server text
    });
    ATTRS.forEach(function (attr) {
      root.querySelectorAll("[data-i18n-" + attr + "]").forEach(function (el) {
        var value = lookup(el.getAttribute("data-i18n-" + attr));
        if (value !== undefined) el.setAttribute(attr, value);
      });
    });
    document.documentElement.setAttribute("lang", lang);
  }

  function setLang(next) {
    if (LANGS.indexOf(next) === -1 || next === lang) return;
    try { localStorage.setItem(STORE, next); } catch (e) { /* ignore */ }
    window.location.reload();
  }

  // label(key, raw): translated text when the key exists, else the raw value (no warning);
  // for open-ended identifiers such as stimulus names read from a state file.
  function label(key, raw) { return lookup(key) === undefined ? raw : t(key); }

  window.i18n = { t: t, label: label, apply: apply, setLang: setLang, langs: LANGS };
  Object.defineProperty(window.i18n, "lang", { get: function () { return lang; } });
  window.t = t;
  document.documentElement.setAttribute("lang", lang);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { apply(); });
  } else {
    apply();
  }
})();

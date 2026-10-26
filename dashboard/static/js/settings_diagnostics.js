/* Settings page: the Diagnostics section (#267) — a read-only view of the `stira doctor` report.
 *
 * StiraDiagnosticsTab.create({ document, elements, getJSON, post }) loads
 * GET /api/diagnostics/doctor and words every finding from the catalog by its code (a code it
 * does not know falls back to the server's English message). "Run again" calls the existing
 * POST /api/diagnostics/doctor (CSRF through stiraFetch): the same read-only check as the chat
 * sidebar. This file reads and shows; it saves no setting and adds no route.
 */
(function (root) {
  "use strict";
  const LEVELS = new Set(["info", "warning", "error"]);
  const SKIPS = new Set(["no_policy", "unsupported", "symlink_in_path", "changed"]);
  const PRINCIPALS = new Set(["terminal", "app", "launchd", "process", "user"]);

  // t() that returns null for a key the catalog does not have (no console warning).
  function tryT(key, vars) {
    if (root.i18n && typeof root.i18n.label === "function") {
      const text = root.i18n.label(key, null);
      return text === null ? null : t(key, vars);
    }
    const text = t(key, vars);
    return text === key ? null : text;
  }

  function make(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  const level = value => (LEVELS.has(value) ? value : "info");

  function describe(finding, entry) {
    const vars = {
      zone: finding.zone || "?",
      flags: Array.isArray(finding.flags) ? finding.flags.join(", ") : "?",
      step: finding.step || "?",
      errno: finding.errno || "?",
      reason: "?",
    };
    if (finding.code === "write_test_skipped") {
      const skipped = entry?.write_test?.skipped;
      vars.reason = SKIPS.has(skipped) ? t(`diagnostics.skip_${skipped}`) : String(skipped ?? "?");
    }
    return tryT(`diagnostics.finding_${finding.code}`, vars) || String(finding.message || finding.code);
  }

  function count(report) {
    let errors = 0, warnings = 0;
    const all = [...(report.roots || []).flatMap(entry => entry.findings || []), ...(report.checks || [])];
    for (const finding of all) {
      if (finding.level === "error") errors += 1;
      else if (finding.level === "warning") warnings += 1;
    }
    return { errors, warnings, roots: (report.roots || []).length };
  }

  function verdict(report) {
    if (report.config_error) return t("diagnostics.tab_config_error");
    const n = count(report);
    if (n.errors || !report.ok) return t("diagnostics.tab_verdict_errors", n);
    return n.warnings ? t("diagnostics.tab_verdict_warnings", n) : t("diagnostics.tab_verdict_ok", n);
  }

  function principalText(info) {
    const kind = PRINCIPALS.has(info?.kind) ? info.kind : "process";
    return t(`diagnostics.principal_${kind}`, { name: info?.name || "?", executable: info?.executable || "?" });
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const els = options.elements || {};
    const getJSON = options.getJSON || (async url => {
      const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const post = options.post || (async url => {
      const response = await root.stiraFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const el = (tag, className, content) => make(doc, tag, className, content);
    const clear = node => { if (node) node.replaceChildren(); };

    function fact(label, value) {
      const row = el("div");
      row.append(el("dt", null, label), el("dd", null, value));
      els.facts.append(row);
    }

    function levelBadge(value) {
      const badge = el("span", "diag-level", t(`diagnostics.level_${level(value)}`));
      badge.dataset.level = level(value);
      return badge;
    }

    function findingItem(finding, entry) {
      const item = el("li", "diag-finding");
      item.dataset.level = level(finding.level);
      item.dataset.code = String(finding.code || "");
      const line = el("p", "diag-line");
      line.append(levelBadge(finding.level), el("span", null, describe(finding, entry)));
      item.append(line);
      const fix = finding.level !== "info" && finding.remedy ? tryT(`diagnostics.fix_${finding.remedy}`) : null;
      if (fix) {
        const how = el("p", "hint diag-fix");
        how.append(el("strong", null, `${t("diagnostics.how")}: `), el("span", null, fix));
        item.append(how);
      }
      return item;
    }

    function list(container, findings, entry) {
      for (const finding of findings) container.append(findingItem(finding, entry));
    }

    function render(report) {
      for (const node of [els.facts, els.checks, els.roots, els.notes]) clear(node);
      els.status.textContent = verdict(report);
      els.status.dataset.level = report.config_error || !report.ok ? "error" : count(report).warnings ? "warning" : "info";
      fact(t("diagnostics.tab_workspace"), report.workspace || "—");
      fact(t("diagnostics.tab_platform"), report.platform || "—");
      fact(t("diagnostics.who"), report.principal ? principalText(report.principal) : "—");
      fact(t("diagnostics.tab_generated"), report.generated_at || "—");
      const checks = report.checks || [];
      els.checksBox.hidden = checks.length === 0;
      list(els.checks, checks, null);
      const roots = report.roots || [];
      els.rootsBox.hidden = roots.length === 0;
      for (const entry of roots) {
        const item = el("li", "diag-root");
        item.dataset.status = String(entry.status || "ok");
        const head = el("p", "diag-line");
        const status = entry.status === "error" ? "error" : entry.status === "warning" ? "warning" : null;
        const badge = status ? levelBadge(status) : el("span", "diag-level", t("diagnostics.tab_status_ok"));
        if (!status) badge.dataset.level = "ok";
        head.append(badge, el("code", "diag-path", String(entry.path || entry.root || "?")));
        if (entry.root && entry.root !== entry.path) head.append(el("span", "hint", ` ← ${entry.root}`));
        item.append(head);
        const findings = entry.findings || [];
        const inner = el("ul", "diag-list");
        list(inner, findings, entry);
        if (entry.write_test && entry.write_test.ok === true) {
          const ok = el("li", "diag-finding");
          ok.dataset.level = "info";
          ok.append(el("p", "hint", t("diagnostics.tab_write_ok")));
          inner.append(ok);
        }
        if (!inner.children.length) inner.append(el("li", "hint", t("diagnostics.tab_no_findings")));
        item.append(inner);
        els.roots.append(item);
      }
      const notes = report.notes || [];
      els.notesBox.hidden = notes.length === 0;
      list(els.notes, notes, null);
    }

    function showMessage(text) {
      for (const node of [els.facts, els.checks, els.roots, els.notes]) clear(node);
      for (const box of [els.checksBox, els.rootsBox, els.notesBox]) box.hidden = true;
      els.status.textContent = text;
      els.status.dataset.level = "info";
    }

    async function refresh() {
      els.status.textContent = t("diagnostics.tab_loading");
      try {
        const report = await getJSON("/api/diagnostics/doctor");
        if (!report) showMessage(t("diagnostics.tab_not_run"));
        else render(report);
      } catch {
        showMessage(t("diagnostics.tab_failed"));
      }
    }

    async function rerun() {
      if (els.rerun) els.rerun.disabled = true;
      els.status.textContent = t("diagnostics.tab_running");
      try {
        render(await post("/api/diagnostics/doctor"));
      } catch {
        showMessage(t("diagnostics.tab_failed"));
      } finally {
        if (els.rerun) els.rerun.disabled = false;
      }
    }

    if (els.rerun) els.rerun.addEventListener("click", rerun);
    return { refresh, rerun, render };
  }

  function init() {
    const doc = root.document;
    const id = name => doc.getElementById(name);
    if (!id("diag-status")) return;
    const tab = create({
      document: doc,
      elements: {
        status: id("diag-status"), facts: id("diag-facts"), checks: id("diag-checks"), roots: id("diag-roots"),
        notes: id("diag-notes"), checksBox: id("diag-checks-box"), rootsBox: id("diag-roots-box"),
        notesBox: id("diag-notes-box"), rerun: id("diag-rerun"),
      },
    });
    tab.refresh();
  }

  root.StiraDiagnosticsTab = { create, describe, count, verdict };
  if (root.document && typeof root.document.addEventListener === "function" && !root.__STIRA_NO_AUTOINIT__) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", init);
    else init();
  }
})(typeof window !== "undefined" ? window : globalThis);

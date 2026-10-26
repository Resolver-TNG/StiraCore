/* Shared confirmation dialog (#282) for actions that widen what runs or what is allowed.
 *
 *   StiraConfirm.open({ title, intro, items, confirmLabel, cancelLabel, tone,
 *                       typed: { word, prompt }, names: { expected, prompt, hint, parse },
 *                       checks: [label, ...] })
 *     -> Promise<boolean>
 *
 * One native <dialog> (showModal): the list of what changes, Cancel is the default
 * (focused unless a word has to be typed or a box ticked; Enter in a text field submits
 * Cancel, the first button), Esc cancels, focus returns to the opener. ``checks`` (ADR 0012
 * phase 2c): one checkbox per label, all to be ticked before the confirm button is enabled;
 * Space ticks, Enter on a box does nothing (neither confirms nor cancels), Tab moves on to
 * Cancel and the confirm button. Resolves true only when the confirm button closed it and
 * the typed word / names / ticks still match. A second open() while
 * one is showing resolves false. With ``onConfirm`` the request runs inside the dialog's
 * busy window: until it settles another open() resolves false (no second dialog, no
 * second POST) — except ``followUp: true`` (#650): the confirmation that pending request
 * itself asks for (api.js, a 428 host confirmation of the browser channel) opens once the
 * first dialog has closed; the busy window still holds after it. If showModal() throws, nothing is confirmed and the dialog is usable again.
 * Auto-repeated Enter / Space inside the dialog are ignored (a held key answers nothing).
 * Callers decide *whether* to ask (the safety meaning); this only renders the question.
 */
(function (root) {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  // ``showing``: the dialog is on screen. ``pending``: confirmed onConfirm requests still running.
  let dialog = null, parts = null, showing = false, pending = 0;

  function node(tag, attrs = {}, text) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (name === "className") el.className = value;
      else el.setAttribute(name, value);
    }
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function build() {
    dialog = node("dialog", {
      id: "stira-confirm", className: "stira-confirm",
      "aria-labelledby": "stira-confirm-title", "aria-describedby": "stira-confirm-intro stira-confirm-items",
    });
    const form = node("form", { method: "dialog" });
    const title = node("h2", { id: "stira-confirm-title" });
    const intro = node("p", { id: "stira-confirm-intro", className: "stira-confirm-intro" });
    const items = node("ul", { id: "stira-confirm-items", className: "stira-confirm-items" });
    const typedWrap = node("div", { className: "stira-confirm-field" });
    const typedLabel = node("label", { for: "stira-confirm-input", id: "stira-confirm-prompt" });
    const typedInput = node("input", { id: "stira-confirm-input", type: "text", autocomplete: "off", spellcheck: "false" });
    typedWrap.append(typedLabel, typedInput);
    const namesWrap = node("div", { className: "stira-confirm-field" });
    const namesLabel = node("label", { for: "stira-confirm-names", id: "stira-confirm-names-prompt" });
    const namesInput = node("input", {
      id: "stira-confirm-names", type: "text", autocomplete: "off", spellcheck: "false",
      "aria-describedby": "stira-confirm-names-hint",
    });
    const namesHint = node("p", { id: "stira-confirm-names-hint", className: "hint" });
    namesWrap.append(namesLabel, namesInput, namesHint);
    const checksWrap = node("div", { className: "stira-confirm-checks" });
    // Enter on a box must not submit the form (implicit submission = Cancel): a person who
    // ticked with Space and pressed Enter would lose the operation without meaning to.
    checksWrap.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target && event.target.type === "checkbox") event.preventDefault();
    });
    const actions = node("div", { className: "dialog-actions" });
    // Cancel first: it is the form's default button (implicit submission) and the default focus.
    const cancel = node("button", { type: "submit", value: "cancel", className: "btn btn-secondary", id: "stira-confirm-cancel" });
    const confirm = node("button", { type: "submit", value: "confirm", className: "btn btn-danger", id: "stira-confirm-ok" });
    actions.append(cancel, confirm);
    form.append(title, intro, items, typedWrap, namesWrap, checksWrap, actions);
    dialog.append(form);
    // A held key must not walk through the dialog (#350): auto-repeated Enter / Space are
    // ignored, so the press that opened it cannot also answer it; one fresh press is needed.
    dialog.addEventListener("keydown", (event) => {
      if (event.repeat && (event.key === "Enter" || event.key === " ")) event.preventDefault();
    });
    document.body.append(dialog);
    parts = { title, intro, items, typedWrap, typedLabel, typedInput, namesWrap, namesLabel, namesInput, namesHint, checksWrap, cancel, confirm };
  }

  function open(options = {}) {
    if (showing || (pending > 0 && options.followUp !== true)) return Promise.resolve(false);
    if (!dialog) build();
    const p = parts;
    const typed = options.typed && options.typed.word ? options.typed : null;
    const names = options.names && Array.isArray(options.names.expected) && options.names.expected.length
      ? options.names : null;
    const parse = names && typeof names.parse === "function" ? names.parse : (text) => text.split(/[\s,]+/).filter(Boolean);
    p.title.textContent = options.title || "";
    p.intro.textContent = options.intro || "";
    p.intro.hidden = !options.intro;
    p.items.replaceChildren(...(options.items || []).map((text) => node("li", {}, String(text))));
    p.items.hidden = !(options.items || []).length;
    p.typedWrap.hidden = !typed;
    p.typedLabel.textContent = typed ? typed.prompt || "" : "";
    p.typedInput.value = "";
    p.namesWrap.hidden = !names;
    p.namesLabel.textContent = names ? names.prompt || "" : "";
    p.namesHint.textContent = names ? names.hint || "" : "";
    p.namesHint.hidden = !(names && names.hint);
    p.namesInput.value = "";
    const labels = Array.isArray(options.checks) ? options.checks.filter((text) => typeof text === "string" && text) : [];
    const boxes = labels.map((text, index) => {
      const box = node("input", { type: "checkbox", id: `stira-confirm-check-${index}` });
      const label = node("label", { for: box.id, className: "stira-confirm-check" });
      label.append(box, node("span", {}, text));
      return { box, label };
    });
    p.checksWrap.replaceChildren(...boxes.map((b) => b.label));
    p.checksWrap.hidden = !boxes.length;
    p.cancel.textContent = options.cancelLabel || tr("hub.cancel");
    p.confirm.textContent = options.confirmLabel || tr("confirm.ok");
    p.confirm.className = `btn ${options.tone === "primary" ? "btn-primary" : "btn-danger"}`;
    dialog.dataset.tone = options.tone === "primary" ? "primary" : "danger";
    const matches = () => (!typed || p.typedInput.value.trim() === typed.word)
      && (!names || JSON.stringify(parse(p.namesInput.value)) === JSON.stringify(names.expected))
      && boxes.every((b) => b.box.checked);
    const sync = () => { p.confirm.disabled = !matches(); };
    sync();
    const opener = document.activeElement;
    showing = true;
    return new Promise((resolve) => {
      p.typedInput.addEventListener("input", sync);
      p.namesInput.addEventListener("input", sync);
      p.checksWrap.addEventListener("change", sync);
      const detach = () => {
        p.typedInput.removeEventListener("input", sync);
        p.namesInput.removeEventListener("input", sync);
        p.checksWrap.removeEventListener("change", sync);
      };
      const onClose = () => {
        detach();
        const ok = dialog.returnValue === "confirm" && matches();
        if (opener && typeof opener.focus === "function" && opener.isConnected !== false) opener.focus();
        if (!ok || typeof options.onConfirm !== "function") {
          showing = false;
          resolve(ok);
          return;
        }
        // Busy until the confirmed request settles (the dialog itself is closed).
        showing = false;
        pending += 1;
        Promise.resolve()
          .then(() => options.onConfirm())
          .catch(() => {})
          .finally(() => { pending -= 1; resolve(true); });
      };
      dialog.addEventListener("close", onClose, { once: true });
      dialog.returnValue = "";
      try {
        dialog.showModal();
      } catch (error) {
        dialog.removeEventListener("close", onClose);
        detach();
        showing = false;
        resolve(false);
        return;
      }
      (typed ? p.typedInput : boxes.length ? boxes[0].box : p.cancel).focus();
    });
  }

  root.StiraConfirm = { open, isOpen: () => showing || pending > 0 };
})(typeof window !== "undefined" ? window : globalThis);

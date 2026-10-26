// Test helper: jsdom has no <dialog> behaviour. This mirrors what browsers do that the
// UI relies on: showModal/close, returnValue, <form method="dialog"> closing with the
// submitter's value, and Esc firing "cancel" then closing with an empty returnValue.
function polyfillDialog(window) {
  const proto = window.HTMLDialogElement.prototype;
  if (typeof proto.showModal === 'function') return;
  const values = new WeakMap();
  Object.defineProperty(proto, 'open', {
    configurable: true,
    get() { return this.hasAttribute('open'); },
    set(value) { if (value) this.setAttribute('open', ''); else this.removeAttribute('open'); },
  });
  Object.defineProperty(proto, 'returnValue', {
    configurable: true,
    get() { return values.get(this) ?? ''; },
    set(value) { values.set(this, String(value)); },
  });
  proto.showModal = function showModal() { this.setAttribute('open', ''); };
  proto.close = function close(value) {
    if (!this.hasAttribute('open')) return;
    this.removeAttribute('open');
    if (value !== undefined) this.returnValue = value;
    this.dispatchEvent(new window.Event('close'));
  };
  const doc = window.document;
  doc.addEventListener('submit', (event) => {
    const form = event.target;
    if ((form.getAttribute('method') || '').toLowerCase() !== 'dialog') return;
    event.preventDefault();
    const dialog = form.closest('dialog');
    if (dialog) dialog.close(event.submitter ? event.submitter.value : '');
  }, true);
  doc.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = [...doc.querySelectorAll('dialog[open]')].pop();
    if (!open) return;
    const cancel = new window.Event('cancel', { cancelable: true });
    open.dispatchEvent(cancel);
    if (!cancel.defaultPrevented) open.close();
  }, true);
}

module.exports = { polyfillDialog };

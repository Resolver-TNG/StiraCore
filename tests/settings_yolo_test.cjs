// Settings page YOLO section (ADR 0012 phase 7c): node --test tests/settings_yolo_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource, catalog } = require('./ui_shim.cjs');
const { Element } = require('./chat_dom_mock.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/settings_yolo.js'), 'utf8');
const FIELDS = ['allow', 'offer_exec', 'max_auto_approvals', 'max_external', 'max_exec', 'host_confirm'];
const APPLIES = {
  allow: 'restart', offer_exec: 'restart', host_confirm: 'restart',
  max_auto_approvals: 'next_window', max_external: 'next_window', max_exec: 'next_window',
};
const DEFAULTS = {
  allow: 'off', offer_exec: true, max_auto_approvals: 2000, max_external: 200, max_exec: 200, host_confirm: 'browser',
};

function answer(values = {}, extra = {}) {
  const fields = {};
  for (const name of FIELDS) {
    const value = name in values ? values[name] : DEFAULTS[name];
    fields[name] = {
      saved: name in values ? value : null, value, source: name in values ? 'config' : 'default',
      running: DEFAULTS[name], default: DEFAULTS[name], env: name === 'offer_exec' ? null : `STIRA_YOLO_${name.toUpperCase()}`,
      pinned: false, applies: APPLIES[name], pending_restart: APPLIES[name] === 'restart' && value !== DEFAULTS[name],
      ...(extra.fields && extra.fields[name]),
    };
  }
  const { fields: _ignored, ...rest } = extra;
  return {
    fields, version: 'v1', enabled: false, exec_tools: false, exec_tools_env: 'STIRA_ENABLE_EXEC_TOOLS',
    busy: false, wildcard_bind: false, warnings: [], file: 'gateway-config.json',
    restart_pending: FIELDS.filter((name) => fields[name].pending_restart), ...rest,
  };
}

function harness({ get = answer(), posts = [], confirmResult = true } = {}) {
  const doc = { createElement: (tag) => new Element(doc, tag) };
  const context = { document: doc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, Object, __STIRA_NO_AUTOINIT__: true };
  context.window = context;
  vm.runInNewContext(shimSource() + source, context);
  const el = (tag, extra = {}) => Object.assign(new Element(doc, tag), extra);
  const inputs = {
    allow: el('select'), offer_exec: el('input', { type: 'checkbox', checked: false }),
    max_auto_approvals: el('input', { type: 'number' }), max_external: el('input', { type: 'number' }),
    max_exec: el('input', { type: 'number' }), host_confirm: el('select'),
  };
  const map = (fn) => Object.fromEntries(FIELDS.map((name) => [name, fn(name)]));
  const els = {
    inputs, errors: map(() => el('p')), pins: map(() => el('p')), sources: map(() => el('span')),
    fields: el('div'), load: el('p'), restart: el('p'), busy: el('p'), warnings: el('p'), execTools: el('p'),
    stateBadge: el('span'), summary: el('a'), save: el('button'), discard: el('button'), status: el('span'),
    footerNote: el('a', { hidden: true }),
  };
  const calls = [];
  const confirms = [];
  const section = context.StiraYoloSettings.create({
    document: doc, elements: els,
    getJSON: async (url) => { calls.push(['GET', url]); return { ok: true, status: 200, data: get }; },
    post: async (url, body) => { calls.push(['POST', url, body]); return posts.shift(); },
    confirm: async (options) => { confirms.push(options); return confirmResult; },
  });
  return { context, section, els, inputs, calls, confirms };
}

const type = (input, value) => { input.value = String(value); input.dispatch('input'); };
const pick = (input, value) => { input.value = value; input.dispatch('change'); };

test('validate mirrors the server ranges', () => {
  const { context } = harness();
  const { validate } = context.StiraYoloSettings;
  const ok = { ...DEFAULTS };
  assert.deepEqual(Object.keys(validate(ok, null)), []);
  assert.equal(validate({ ...ok, max_auto_approvals: 0 }, null).max_auto_approvals.code, 'out_of_range');
  assert.equal(validate({ ...ok, max_auto_approvals: 10001 }, null).max_auto_approvals.code, 'out_of_range');
  assert.equal(validate({ ...ok, max_auto_approvals: NaN }, null).max_auto_approvals.code, 'out_of_range');
  assert.equal(validate({ ...ok, max_exec: 2001 }, null).max_exec.code, 'exceeds_cap');
  assert.equal(validate({ ...ok, max_external: -1 }, null).max_external.code, 'out_of_range');
  assert.equal(validate({ ...ok, allow: 'all' }, null).allow.code, 'invalid_choice');
  // A pinned cap: the sub-caps are checked against the variable's value.
  const fields = { max_auto_approvals: { pinned: true, value: 30 } };
  assert.equal(validate({ ...ok, max_auto_approvals: 30, max_exec: 31 }, fields).max_exec.code, 'exceeds_cap');
});

test('loads, shows sources and pins, and keeps save disabled until something changes', async () => {
  const get = answer({ allow: 'internal' }, { fields: { max_exec: { pinned: true, source: 'env', value: 5 } } });
  const { section, els, inputs } = harness({ get });
  await section.load();
  assert.equal(els.fields.hidden, false);
  assert.equal(inputs.allow.value, 'internal');
  assert.equal(inputs.max_exec.disabled, true);
  assert.match(els.pins.max_exec.textContent, /STIRA_YOLO_MAX_EXEC/);
  assert.equal(els.sources.allow.textContent, catalog['yset.source_config']);
  assert.equal(els.sources.max_exec.dataset.state, 'warn');
  assert.equal(els.save.disabled, true);
  assert.equal(els.restart.hidden, false); // allow saved, not running
  type(inputs.max_auto_approvals, 100);
  // max_external is at its default (200): it follows the smaller cap, no error.
  assert.equal(els.save.disabled, false);
  assert.equal(els.errors.max_external.hidden, true);
  type(inputs.max_external, 101); // typed above the cap
  assert.equal(els.save.disabled, true);
  assert.equal(inputs.max_external.getAttribute('aria-invalid'), 'true');
  assert.equal(els.errors.max_external.textContent, catalog['yset.err_exceeds_cap'].replace('{max}', '100'));
});

test('a loosening save asks once, then sends confirm and the acknowledged codes', async () => {
  const saved = { ...answer({ allow: 'internal' }), saved: true, changed: ['allow'], loosened: ['yolo_allow'], caps_applied: null };
  const posts = [
    { ok: false, status: 409, data: { code: 'needs_confirmation', needs_confirmation: ['yolo_allow'] } },
    { ok: true, status: 200, data: saved },
  ];
  const { section, els, inputs, calls, confirms } = harness({ posts });
  await section.load();
  pick(inputs.allow, 'internal');
  await section.save();
  const sent = calls.filter((c) => c[0] === 'POST').map((c) => c[2]);
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0])), { values: { allow: 'internal' }, base_version: 'v1' });
  assert.deepEqual(JSON.parse(JSON.stringify(sent[1])), {
    values: { allow: 'internal' }, base_version: 'v1', confirm: true, acknowledged: ['yolo_allow'],
  });
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0].checks.length, 1);
  assert.ok(confirms[0].items.some((item) => item.includes(catalog['yset.allow_internal'])));
  assert.match(els.status.textContent, new RegExp(catalog['yset.saved_restart']));
  assert.equal(els.restart.hidden, false);
});

test('cancelling the confirmation saves nothing', async () => {
  const posts = [{ ok: false, status: 409, data: { code: 'needs_confirmation', needs_confirmation: ['yolo_cap'] } }];
  const { section, els, inputs, calls } = harness({ posts, confirmResult: false });
  await section.load();
  type(inputs.max_auto_approvals, 3000);
  await section.save();
  assert.equal(calls.filter((c) => c[0] === 'POST').length, 1);
  assert.equal(els.status.textContent, catalog['yset.cancelled']);
  assert.equal(els.save.disabled, false); // still dirty: can try again
});

test('server field errors and refusal codes are worded', async () => {
  const posts = [
    { ok: false, status: 400, data: { code: 'invalid', errors: [{ field: 'allow', code: 'wildcard_bind', params: {} }] } },
    { ok: false, status: 409, data: { code: 'turn_running' } },
  ];
  const { section, els, inputs } = harness({ posts });
  await section.load();
  pick(inputs.allow, 'external');
  await section.save();
  assert.equal(els.errors.allow.textContent, catalog['yset.err_wildcard_bind']);
  assert.equal(els.status.textContent, catalog['yset.fix_errors']);
  await section.save();
  assert.equal(els.status.textContent, catalog['yset.code_turn_running']);
});

test('a busy window disables every input and the save', async () => {
  const { section, els, inputs } = harness({ get: answer({}, { busy: true }) });
  await section.load();
  assert.equal(els.busy.hidden, false);
  for (const name of FIELDS) assert.equal(inputs[name].disabled, true, name);
  assert.equal(els.save.disabled, true);
});

test('the confirmation says hand-edited invalid values are rounded (PR #444 m1)', async () => {
  const posts = [
    { ok: false, status: 409, data: { code: 'needs_confirmation', needs_confirmation: ['yolo_cap'] } },
  ];
  const get = answer({}, { warnings: ['max_exec_rounded', 'unknown_key'] });
  const { section, inputs, confirms } = harness({ get, posts, confirmResult: false });
  await section.load();
  type(inputs.max_auto_approvals, 3000);
  await section.save();
  const note = catalog['yset.confirm_rounds_note'].replace('{codes}', 'max_exec_rounded, unknown_key');
  assert.ok(confirms[0].items.includes(note), confirms[0].items.join(' | '));
  // Without warnings, no such line.
  const clean = harness({ posts: [{ ok: false, status: 409, data: { code: 'needs_confirmation', needs_confirmation: ['yolo_cap'] } }], confirmResult: false });
  await clean.section.load();
  type(clean.inputs.max_auto_approvals, 3000);
  await clean.section.save();
  assert.equal(clean.confirms[0].items.length, confirms[0].items.length - 1);
});

test('the common footer says when this section has unsaved changes', async () => {
  const { section, els, inputs } = harness();
  await section.load();
  assert.equal(els.footerNote.hidden, true);
  type(inputs.max_exec, 10);
  assert.equal(els.footerNote.hidden, false);
  els.discard.dispatch('click');
  assert.equal(els.footerNote.hidden, true);
});

test('a refused audit row is worded', async () => {
  const posts = [{ ok: false, status: 503, data: { code: 'audit_failed' } }];
  const { section, els, inputs } = harness({ posts });
  await section.load();
  type(inputs.max_exec, 10);
  await section.save();
  assert.equal(els.status.textContent, catalog['yset.code_audit_failed']);
});

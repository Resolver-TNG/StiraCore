// Gadget mapping (#341, stage 2): which prop is on, from which real event category.
// Pure functions, no DOM. Called by model.derive(); every value names its cause.
//
// Rule: a gadget is shown only while the state carried by events says so (a tool of that
// category is running, the open turn used it, a subagent is out, a job runs, ...). With no
// event there is no gadget. The look of each gadget comes from the declarative catalog in
// gadget_catalog.js; nothing here or there is agent-supplied code.

import { MOTION_CUES } from './gadget_catalog.js';

// Tool categories Stage understands. Anything else is shown as a generic `tool`.
export const GADGET_KINDS = ['code', 'test', 'subagent', 'file'];
export const MEMORY_LAYERS = ['episodic', 'semantic', 'procedural'];
export const DRONE_SLOTS = 6; // drones drawn; more running agents show as "+N"
export const AGENT_LIMIT = 12;

// Tool name -> category, for adapters that only have the tool name (stage 2 SSE). Exact
// names first, then a few prefixes. Unknown names are a generic `tool`.
const NAME_KIND = {
  editor: 'code',
  file_write: 'code',
  file_editor: 'code',
  lsp_hover: 'code',
  lsp_diagnostics: 'code',
  editor_read_only: 'file',
  file_read: 'file',
  file_flags: 'file',
  memory_search: 'memory',
  memory_read: 'memory',
  memory_list: 'memory',
  memory_write: 'memory',
  web_search: 'web',
  web_fetch: 'web',
  perplexity_search: 'web',
  run_tests: 'test',
  pytest: 'test',
  sessions_spawn: 'subagent',
};
const PREFIX_KIND = [
  ['lsp_', 'code'],
  ['memory_', 'memory'],
  ['web_', 'web'],
  ['test_', 'test'],
  ['delegate_', 'subagent'],
  ['subagent_', 'subagent'],
  ['spawn_', 'subagent'],
];
const WRITE_NAMES = new Set(['memory_write', 'file_write', 'editor']);

export function classifyTool(name) {
  if (typeof name !== 'string') return { kind: 'tool', write: false };
  const n = name.toLowerCase().slice(0, 64);
  let kind = NAME_KIND[n];
  if (!kind) kind = (PREFIX_KIND.find(([p]) => n.startsWith(p)) || [null, 'tool'])[1];
  return { kind, write: WRITE_NAMES.has(n) || /_(write|save|append)$/.test(n) };
}

const DECISIONS = { approve: 'approve', approved: 'approve', allow: 'approve', deny: 'deny', denied: 'deny', reject: 'deny', rejected: 'deny', expired: 'deny', timeout: 'deny' };
export const stampFor = (decision) => (typeof decision === 'string' ? DECISIONS[decision.toLowerCase()] || null : null);

const AGENT_CODE = { run: 'run', done: 'done', err: 'err' };

// Input key of the gadget part of the state (drift check: equal keys -> equal gadgets).
export function gadgetInputKey(s) {
  const tools = s.tools.map((t) => `${t.kind}${t.write ? '+w' : ''}${t.layer ? `@${t.layer}` : ''}`).sort().join(',');
  return [
    `tk=${tools}`,
    `used=${[...(s.turnKinds || [])].sort().join(',')}`,
    `ag=${(s.agents || []).map((a) => AGENT_CODE[a.status]).join(',')}`,
    `test=${s.lastTest}`,
    `err=${s.lastError ? s.lastError.kind : null}`,
    `stamp=${s.stamp}`,
    `jobs=${(s.jobs || []).length}`,
    `arch=${s.archived > 0 ? 1 : 0}`,
    `cue=${s.agentCue}`,
  ].join('|');
}

// Adds gadget channels to a pose. `set(channel, value, cause)`, `ref(field)` -> "#seq type".
const isOn = (v) => Boolean(v) && v !== 'off' && v !== 'none';

// Sets the gadget channels; the last one, gadget.motion, names the motion (MOTION_IDS) that
// comes in with the props, chosen by MOTION_CUES (data). The motion catalog connects with
// `trigger: { state: 'gadget.motion', is: '<id>' }`; an id nobody defines plays nothing.
export function deriveGadgets(s, set0, ref) {
  const out = {};
  const set = (channel, value, cause) => {
    out[channel] = value;
    out[`${channel}#cause`] = cause;
    set0(channel, value, cause);
  };
  const running = (k) => s.tools.filter((t) => t.kind === k);
  const names = (list) => list.map((t) => t.name || t.id).join(', ');
  const used = new Set(s.turnKinds || []);
  const code = running('code');
  const test = running('test');
  const err = s.lastError;

  // (1) coding: laptop on the desk, typing, code on the screen, result lamp
  const laptopWhy = code.length || test.length
    ? `${code.length ? 'code' : 'test'} running: ${names([...code, ...test])} (${ref('tools')})`
    : s.thinking && (used.has('code') || used.has('test'))
      ? `turn open and it used ${[...used].filter((k) => k === 'code' || k === 'test').join('+')} (${ref('turnKinds')})`
      : 'no code or test tool in this turn';
  const laptop = code.length || test.length || (s.thinking && (used.has('code') || used.has('test'))) ? 1 : 0;
  set('gadget.laptop', laptop, laptopWhy);
  set('gadget.typing', code.length ? 1 : 0, code.length ? `code running: ${names(code)} (${ref('tools')})` : 'no code tool running');
  let lamp = 'off';
  let lampWhy = 'no test in this turn';
  if (test.length) {
    lamp = 'run';
    lampWhy = `test running: ${names(test)} (${ref('tools')})`;
  } else if (laptop && s.lastTest) {
    lamp = s.lastTest;
    lampWhy = `last test ${s.lastTest === 'pass' ? 'passed' : 'failed'} (${ref('lastTest')})`;
  }
  set('gadget.lamp', lamp, lampWhy);

  // (2) delegation: one drone per subagent, in spawn order (slot = index)
  const agents = s.agents || [];
  const nRun = agents.filter((a) => a.status === 'run').length;
  set('gadget.drones', agents.map((a) => AGENT_CODE[a.status]).join(',') || 'none',
    agents.length ? `${agents.map((a) => `${a.name || a.id}:${a.status}`).join(', ')} (${ref('agents')})` : 'no subagent');
  set('gadget.drones.running', nRun, nRun ? `${nRun} subagent(s) running (${ref('agents')})` : 'no subagent running');

  // (3) memory / files: binder out of the shelf (read), note + stamp (write), shelf row by layer
  const mem = [...running('memory'), ...running('file')];
  const reads = mem.filter((t) => !t.write);
  const writes = mem.filter((t) => t.write);
  set('gadget.folder', reads.length || (err && (err.kind === 'memory' || err.kind === 'file') && !err.write) ? 1 : 0,
    reads.length ? `read running: ${names(reads)} (${ref('tools')})` : err && !err.write && (err.kind === 'memory' || err.kind === 'file') ? `read failed: ${err.name} (${ref('lastError')})` : 'no memory/file read running');
  set('gadget.note', writes.length || (err && (err.kind === 'memory' || err.kind === 'file') && err.write) ? 1 : 0,
    writes.length ? `write running: ${names(writes)} (${ref('tools')})` : err && err.write && (err.kind === 'memory' || err.kind === 'file') ? `write failed: ${err.name} (${ref('lastError')})` : 'no memory/file write running');
  const layer = running('memory').map((t) => t.layer).find((l) => MEMORY_LAYERS.includes(l)) || 'none';
  set('gadget.shelf.row', layer, layer !== 'none' ? `memory layer ${layer} (${ref('tools')})` : 'no memory layer named');

  // web: magnifier at the window
  const web = running('web');
  set('gadget.magnifier', web.length || (err && err.kind === 'web') ? 1 : 0,
    web.length ? `web running: ${names(web)} (${ref('tools')})` : err && err.kind === 'web' ? `web failed: ${err.name} (${ref('lastError')})` : 'no web tool running');

  // approval decided: seal on a slip (approve / deny) until the turn ends
  set('gadget.stamp', s.stamp || 'off', s.stamp ? `approval ${s.stamp === 'approve' ? 'approved' : 'denied'} (${ref('stamp')})` : 'no approval decided in this turn');

  // breaker: warning beacon on the pipe
  const beacon = s.breaker === 'CRITICAL' ? 'critical' : s.breaker === 'WARNING' ? 'warning' : 'off';
  set('gadget.beacon', beacon, `breaker ${s.breaker} (${ref('breaker')})`);

  // scheduler / heartbeat job: hourglass (+ bell when it starts)
  const jobs = s.jobs || [];
  set('gadget.hourglass', jobs.length ? 1 : 0, jobs.length ? `job running: ${jobs.map((j) => j.name || j.id).join(', ')} (${ref('jobs')})` : 'no scheduled job running');

  // forgetting: archive box at the back of the shelf (reversible, so it stays visible)
  set('gadget.box', s.archived > 0 ? 1 : 0, s.archived > 0 ? `${s.archived} entr${s.archived === 1 ? 'y' : 'ies'} archived, restorable (${ref('archived')})` : 'nothing archived');

  // tool failure: the prop of that category flickers once and stays marked until a retry
  const glitchKind = err ? ({ code: 'laptop', test: 'laptop', web: 'magnifier', memory: err.write ? 'note' : 'folder', file: err.write ? 'note' : 'folder' })[err.kind] || 'scope' : 'off';
  set('gadget.glitch', glitchKind, err ? `${err.kind} failed: ${err.name || '?'} (${ref('lastError')})` : 'no failed tool');

  // which motion comes with the props (contract with the motion catalog: ids, not code)
  const cue = s.agentCue || 'none';
  set('gadget.drones.cue', cue, s.agentCue ? `last subagent event: ${s.agentCue} (${ref('agentCue')})` : 'no subagent event in this turn');
  const pose = { ...out };
  const hit = MOTION_CUES.find((c) => (c.is !== undefined ? pose[c.when] === c.is : isOn(pose[c.when])));
  set('gadget.motion', hit ? hit.motion : 'none', hit ? `${hit.when}${hit.is !== undefined ? ` = ${hit.is}` : ''} → ${hit.motion} (${pose[`${hit.when}#cause`]})` : 'no prop asks for a motion');
}

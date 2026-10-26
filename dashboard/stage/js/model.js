// Stage state model (#341): events -> state -> derived pose. Pure functions, no DOM.
//
// Rule: everything the scene shows about the agent (expression, steam, prop lights, neon)
// is derived here from real state carried by events. No event, no "busy" look. Idle motion
// (breathing, blinking) is the only thing that runs on its own, and it never encodes state.

import { AGENT_LIMIT, classifyTool, deriveGadgets, gadgetInputKey, MEMORY_LAYERS, stampFor } from './gadget_model.js';
import { motionKey, nextCue, selectMotion } from './motion_model.js';

export const DIMS = ['trust', 'curiosity', 'vigilance', 'attachment', 'stability', 'satisfaction'];
export const BASELINE = Object.freeze({
  trust: 0.5,
  curiosity: 0.5,
  vigilance: 0.1,
  attachment: 0.5,
  stability: 0.7,
  satisfaction: 0.5,
});
export const BREAKER_LEVELS = ['NORMAL', 'WARNING', 'CRITICAL'];
// tool / memory / web light the room's props; code / test / subagent / file bring gadgets (#341)
export const TOOL_KINDS = ['tool', 'memory', 'web', 'code', 'test', 'subagent', 'file'];

export function initialState() {
  return {
    seq: 0,
    neural: { ...BASELINE },
    breaker: 'NORMAL',
    thinking: false,
    tools: [],
    approval: null,
    lastOutcome: null,
    lastStop: null, // why the last turn stopped when it was cut off ('max_tokens', 'max_tokens_before_reply' #416), else null (#363)
    // gadgets (#341 stage 2)
    turnKinds: [], // tool categories used in the open turn
    agents: [], // subagents: {id, name, status: run | done | err}, in spawn order
    lastTest: null, // pass | fail (last test tool of the open turn)
    lastError: null, // {kind, name, write} of the last failed tool until a retry / turn end
    stamp: null, // approve | deny (approval decided in the open turn)
    jobs: [], // scheduler / heartbeat jobs running
    archived: 0, // entries moved to the forgetting archive (restorable)
    agentCue: null, // last subagent event of the turn: spawn | done | err
    src: {}, // field -> {seq, type} of the event that last set it
  };
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const toolKind = (k) => (TOOL_KINDS.includes(k) ? k : 'tool');
const shortText = (v, n) => (typeof v === 'string' ? v.slice(0, n) : '');

// Events the stage understands. Anything else is ignored (returns the same state).
export function reduce(state, ev) {
  if (!ev || typeof ev !== 'object' || typeof ev.type !== 'string') return state;
  const s = { ...state, seq: state.seq + 1, src: { ...state.src }, cue: nextCue(state, ev, state.seq + 1) };
  const mark = (field) => {
    s.src[field] = { seq: s.seq, type: ev.type };
  };
  switch (ev.type) {
    case 'neural': {
      const n = { ...state.neural };
      for (const d of DIMS) {
        const v = ev.values && ev.values[d];
        if (typeof v === 'number' && Number.isFinite(v)) {
          n[d] = clamp01(v);
          mark(`neural.${d}`);
        }
      }
      s.neural = n;
      return s;
    }
    case 'breaker':
      if (!BREAKER_LEVELS.includes(ev.level)) return state;
      s.breaker = ev.level;
      mark('breaker');
      return s;
    case 'turn.start':
      s.thinking = true;
      s.lastOutcome = null;
      s.lastStop = null;
      s.turnKinds = [];
      s.lastTest = null;
      s.lastError = null;
      s.stamp = null;
      s.agentCue = null;
      s.agents = state.agents.filter((a) => a.status === 'run');
      mark('thinking');
      mark('turnKinds');
      if (s.agents.length !== state.agents.length) mark('agents');
      return s;
    case 'turn.end':
      s.thinking = false;
      s.tools = [];
      s.lastOutcome = ev.ok === false ? 'error' : 'ok';
      // #416: cut before the reply began (thinking used the limit) has its own sentence.
      s.lastStop = ev.stop !== 'max_tokens' ? null : ev.cut === 'before_reply' ? 'max_tokens_before_reply' : 'max_tokens';
      s.turnKinds = [];
      s.lastError = null;
      s.stamp = null;
      s.agents = state.agents.filter((a) => a.status !== 'done');
      mark('thinking');
      mark('tools');
      mark('lastOutcome');
      mark('lastStop');
      mark('turnKinds');
      if (s.agents.length !== state.agents.length) mark('agents');
      return s;
    case 'tool.start': {
      // live adapter (adapter.js) sends kind tool|memory|web plus a finer `category` and `op`
      let kind = toolKind(ev.kind);
      if (kind === 'tool' && (ev.category === 'code' || ev.category === 'subagent')) kind = ev.category;
      if (kind === 'code' && classifyTool(ev.name).kind === 'file') kind = 'file';
      if (kind === 'tool' && ev.category === 'other' && classifyTool(ev.name).kind === 'test') kind = 'test'; // live only
      const id = shortText(ev.id, 40) || `t${s.seq}`;
      const name = shortText(ev.name, 40);
      s.lastError = null; // a new call (retry) clears the failure mark
      if (state.lastError) mark('lastError');
      if (kind === 'subagent') {
        const others = state.agents.filter((a) => a.id !== id);
        while (others.length >= AGENT_LIMIT) others.splice(Math.max(0, others.findIndex((a) => a.status !== 'run')), 1);
        s.agents = [...others, { id, name, status: 'run' }];
        s.agentCue = 'spawn';
        mark('agents');
        mark('agentCue');
        return s;
      }
      const tool = { id, kind, name };
      if (ev.write === true || ev.op === 'write' || (kind === 'code' && classifyTool(ev.name).write)) tool.write = true;
      if (MEMORY_LAYERS.includes(ev.layer)) tool.layer = ev.layer;
      s.tools = [...state.tools.filter((t) => t.id !== id), tool].slice(-8);
      if (!state.turnKinds.includes(kind)) {
        s.turnKinds = [...state.turnKinds, kind];
        mark('turnKinds');
      }
      mark('tools');
      return s;
    }
    case 'tool.end': {
      const failed = ev.ok === false || ev.status === 'error' || ev.status === 'timeout';
      const agent = state.agents.find((a) => a.id === ev.id);
      if (agent) {
        s.agents = state.agents.map((a) => (a === agent ? { ...a, status: failed ? 'err' : 'done' } : a));
        s.agentCue = failed ? 'err' : 'done';
        mark('agents');
        mark('agentCue');
        return s;
      }
      const tool = state.tools.find((t) => t.id === ev.id);
      s.tools = state.tools.filter((t) => t.id !== ev.id);
      mark('tools');
      if (tool && tool.kind === 'test') {
        s.lastTest = failed ? 'fail' : 'pass';
        mark('lastTest');
      }
      if (tool && failed) {
        s.lastError = { kind: tool.kind, name: tool.name, write: Boolean(tool.write) };
        mark('lastError');
      }
      return s;
    }
    case 'approval.request':
      s.approval = { id: shortText(ev.id, 40), summary: shortText(ev.summary, 120) };
      mark('approval');
      return s;
    case 'approval.resolved':
      if (!state.approval || (ev.id && ev.id !== state.approval.id)) return state;
      s.approval = null;
      mark('approval');
      if (stampFor(ev.decision)) {
        s.stamp = stampFor(ev.decision);
        mark('stamp');
      }
      return s;
    case 'job.start':
      s.jobs = [...state.jobs.filter((j) => j.id !== ev.id), { id: shortText(ev.id, 40) || `j${s.seq}`, name: shortText(ev.name, 40) }].slice(-4);
      mark('jobs');
      return s;
    case 'job.end':
      if (!state.jobs.some((j) => j.id === ev.id)) return state;
      s.jobs = state.jobs.filter((j) => j.id !== ev.id);
      mark('jobs');
      return s;
    case 'memory.forget':
    case 'memory.restore': {
      const n = Number.isInteger(ev.count) && ev.count > 0 ? Math.min(ev.count, 999) : 0;
      if (!n) return state;
      s.archived = Math.max(0, Math.min(9999, state.archived + (ev.type === 'memory.forget' ? n : -n)));
      mark('archived');
      return s;
    }
    case 'reset':
      return { ...initialState(), seq: s.seq };
    default:
      return state;
  }
}

// Emotion mapping table (documented in docs/stage.md). Evaluated top to bottom; first match wins.
export const EMOTION_RULES = [
  { emotion: 'tense', when: 'breaker is CRITICAL', test: (s) => s.breaker === 'CRITICAL', cause: 'breaker' },
  { emotion: 'worried', when: 'breaker is WARNING', test: (s) => s.breaker === 'WARNING', cause: 'breaker' },
  { emotion: 'joy', when: 'satisfaction >= 0.72', test: (s) => s.neural.satisfaction >= 0.72, cause: 'neural.satisfaction' },
  { emotion: 'curious', when: 'curiosity >= 0.70', test: (s) => s.neural.curiosity >= 0.7, cause: 'neural.curiosity' },
  { emotion: 'calm', when: 'otherwise', test: () => true, cause: null },
];

// Expression per emotion: eyes/mouth variants (sprite ids), posture and scene lighting.
export const EXPRESSIONS = {
  calm: { eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, dim: 0, desat: 0, breath: 1 },
  curious: { eyes: 'wide', mouth: 'open', headY: -1, ears: 1, dim: 0, desat: 0, breath: 1.1 },
  joy: { eyes: 'happy', mouth: 'grin', headY: -1, ears: 1, dim: -0.05, desat: 0, breath: 1.1 },
  worried: { eyes: 'down', mouth: 'frown', headY: 1, ears: -1, dim: 0.28, desat: 0.55, breath: 0.8 },
  tense: { eyes: 'wide', mouth: 'flat', headY: 1, ears: -2, dim: 0.18, desat: 0.35, breath: 1.6 },
};

export const NEON = {
  NORMAL: 'cyan',
  WARNING: 'violet',
  CRITICAL: 'red',
  approval: 'amber',
};

// Work overrides on the eyes (activity shows in the eyes only while the event is open).
const WORK_EYES = { tool: 'half', memory: 'up', web: 'up', thinking: 'up' };

const ref = (s, field) => (s.src[field] ? `#${s.src[field].seq} ${s.src[field].type}` : 'initial');

// derive(state) -> pose: the only input of the renderer. Deterministic: same state, same pose.
export function derive(state) {
  const causes = [];
  const out = {};
  const set = (channel, value, why) => {
    out[channel] = value;
    causes.push({ channel, value, cause: why });
  };
  const rule = EMOTION_RULES.find((r) => r.test(state));
  const ex = EXPRESSIONS[rule.emotion];
  const emoCause = rule.cause ? `${rule.when} (${ref(state, rule.cause)})` : 'no rule matched (baseline)';
  set('emotion', rule.emotion, emoCause);
  // gadget channels (#341 stage 2), listed after the others
  const gadgetCauses = [];
  deriveGadgets(state, (channel, value, cause) => gadgetCauses.push({ channel, value, cause }), (f) => ref(state, f));

  const kinds = new Set(state.tools.map((t) => t.kind));
  let eyes = ex.eyes;
  let eyesWhy = emoCause;
  if (rule.emotion !== 'tense' && rule.emotion !== 'worried') {
    const work = kinds.has('tool') ? 'tool' : kinds.has('memory') ? 'memory' : kinds.has('web') ? 'web' : state.thinking ? 'thinking' : null;
    if (work) {
      eyes = WORK_EYES[work];
      eyesWhy = work === 'thinking' ? `turn open (${ref(state, 'thinking')})` : `${work} running (${ref(state, 'tools')})`;
    }
  }
  if (state.approval && rule.emotion !== 'tense') {
    eyes = 'open';
    eyesWhy = `approval pending (${ref(state, 'approval')})`;
  }
  set('eyes', eyes, eyesWhy);
  set('mouth', state.approval && rule.emotion !== 'tense' ? 'open' : ex.mouth, state.approval ? `approval pending (${ref(state, 'approval')})` : emoCause);
  set('headY', ex.headY, emoCause);
  set('ears', ex.ears + (state.neural.vigilance >= 0.5 ? -1 : 0), state.neural.vigilance >= 0.5 ? `vigilance >= 0.5 (${ref(state, 'neural.vigilance')})` : emoCause);
  const fond = state.neural.attachment >= 0.7 && state.neural.trust >= 0.6;
  set('blush', rule.emotion === 'joy' || fond ? 1 : 0, fond ? `attachment >= 0.7 and trust >= 0.6 (${ref(state, 'neural.attachment')})` : emoCause);
  set('sweat', rule.emotion === 'tense' ? 1 : 0, emoCause);
  set('breathRate', ex.breath, emoCause);
  set('dim', ex.dim, emoCause);
  set('desat', ex.desat, emoCause);

  const neon = state.approval && state.breaker !== 'CRITICAL' ? NEON.approval : NEON[state.breaker];
  set('neon', neon, state.approval && state.breaker !== 'CRITICAL' ? `approval pending (${ref(state, 'approval')})` : `breaker ${state.breaker} (${ref(state, 'breaker')})`);
  set('neonLevel', state.breaker === 'WARNING' ? 0.55 : 1, `breaker ${state.breaker} (${ref(state, 'breaker')})`);
  set('shake', state.breaker === 'CRITICAL' ? 1 : 0, `breaker ${state.breaker} (${ref(state, 'breaker')})`);

  set('steam', state.thinking ? 2 : 0, state.thinking ? `turn open (${ref(state, 'thinking')})` : `no open turn (${ref(state, 'thinking')})`);
  const toolWhy = (k) => (kinds.has(k) ? `${k} running: ${state.tools.filter((t) => t.kind === k).map((t) => t.name || t.id).join(', ')} (${ref(state, 'tools')})` : `no ${k} running`);
  set('light.desk', kinds.has('tool') ? 1 : 0, toolWhy('tool'));
  set('light.shelf', kinds.has('memory') ? 1 : 0, toolWhy('memory'));
  set('light.window', kinds.has('web') ? 1 : 0, toolWhy('web'));
  set('choice', state.approval ? 1 : 0, state.approval ? `approval pending (${ref(state, 'approval')})` : 'no approval pending');
  set('sparkle', state.lastOutcome === 'ok' && rule.emotion === 'joy' ? 1 : 0, state.lastOutcome === 'ok' ? `turn ended ok (${ref(state, 'lastOutcome')}) and ${rule.when}` : 'no finished turn');
  for (const c of gadgetCauses) set(c.channel, c.value, c.cause);
  // the character's motion (motion_model.js, rules are data): last, it reads the channels above
  const motion = selectMotion(state, out, causes, (f) => ref(state, f));
  set('motion', motion.id, motion.cause);
  causes.unshift(causes.pop()); // listed first in the raw view and the debug panel
  return { pose: out, causes };
}

// Signature of the state-derived pose (no idle motion, no causes): used for the drift check.
export function poseSignature(pose) {
  return Object.keys(pose)
    .sort()
    .map((k) => `${k}=${pose[k]}`)
    .join(';');
}

// Key of the inputs that the mapping reads (so equal keys must give equal signatures).
export function inputKey(state) {
  const kinds = [...new Set(state.tools.map((t) => t.kind))].sort().join(',');
  const n = DIMS.map((d) => `${d}=${state.neural[d].toFixed(3)}`).join(',');
  return `${state.breaker}|think=${state.thinking}|tools=${kinds}|appr=${state.approval ? 1 : 0}|out=${state.lastOutcome}|${n}|${gadgetInputKey(state)}|${motionKey(state)}`;
}

// Records pose signatures per input key; reports when one key ever maps to two poses.
export class DriftMonitor {
  constructor(limit = 256) {
    this.limit = limit;
    this.seen = new Map();
    this.drifts = [];
    this.checks = 0;
  }

  record(state, pose) {
    const key = inputKey(state);
    const sig = poseSignature(pose);
    this.checks += 1;
    const prev = this.seen.get(key);
    if (prev !== undefined && prev !== sig) {
      this.drifts.push({ key, before: prev, after: sig });
      return false;
    }
    if (prev === undefined) {
      if (this.seen.size >= this.limit) this.seen.delete(this.seen.keys().next().value);
      this.seen.set(key, sig);
    }
    return true;
  }
}

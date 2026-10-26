// Motion player for the Stage character (#341). Pure: no DOM; tested in Node.
//
// Each frame it builds the character frame (char_rig.js) from, in this order:
//   1. the state-derived face and posture (model.derive: eyes, mouth, emotion, ears, blush ...),
//   2. the legacy gesture channels of anim.js (head.y, ear.*, arm.y, eyes, mouth), mapped to parts,
//   3. the motion selected from the state (pose.motion, see motion_model.js) — or a motion the
//      agent requested by id (only `selectable` ones, never over a state motion),
//   4. life signs that encode nothing: blinking, a glance now and then, an ear flick, breathing.
// It interprets catalog data only (validated, frozen); nothing is evaluated as code.

import { rng } from './anim.js';
import { EMOTION_POSE, ENUM_TRACKS, EYE_MAP, FACE_TRACKS, tailFor, trackAt } from './motion_catalog.js';

const AGENT_COOLDOWN = 2;
const OPEN_EYES = new Set(['open', 'wide', 'sharp', 'soft', 'half', 'surprised', 'puzzled', 'teary', 'jito', 'shy', 'panic']);
const HEAD_SHEET = { '-1': 'up1', 0: 'base', 1: 'down1' };

export class MotionPlayer {
  constructor(motions, seed = 341) {
    this.motions = motions; // Map id -> validated motion
    this.random = rng(seed ^ 0x5eed);
    this.cur = null; // { m, start, source: 'state' | 'agent', phase: 'main' | 'exit' | 'done' }
    this.pending = null;
    this.selKey = null;
    this.selId = 'none';
    this.lastAgent = -Infinity;
    this.ignored = [];
    this.nextBlink = null;
    this.blink = null;
    this.nextGlance = null;
    this.glance = null;
    this.nextFlick = null;
    this.flick = null;
  }

  // The state-selected motion (pose.motion + its cause), applied at the next frame: events
  // that arrive together (turn.start + tool.start in one poll) select only the last motion.
  select(id, cause) {
    this.want = { id, cause };
  }

  // A one-shot restarts when its cause changes (a new event); a looping motion only when the
  // id changes.
  applySelection(now) {
    if (!this.want) return;
    const { id, cause } = this.want;
    this.want = null;
    const m = this.motions.get(id);
    const key = m && !m.loop ? `${id}|${cause}` : id;
    if (key === this.selKey) return;
    this.selKey = key;
    this.selId = m ? id : 'none';
    // an agent-requested motion finishes when the state selects nothing
    if (!m && this.cur && this.cur.source === 'agent') return;
    this.switchTo(m ? { m, source: 'state' } : null, now);
  }

  switchTo(next, now) {
    const c = this.cur;
    if (c && next && c.phase === 'main' && c.m.group && c.m.group === next.m.group) {
      this.pending = null;
      this.cur = { ...next, start: now - next.m.intro, phase: 'main' };
      return;
    }
    if (c && c.phase === 'main' && c.m.loop && c.m.exit) {
      this.cur = { ...c, phase: 'exit', start: now };
      this.pending = next;
      return;
    }
    this.pending = null;
    this.cur = next ? { ...next, start: now, phase: 'main' } : null;
  }

  // Agent-facing: request a motion by id. Refused unless it is selectable, nothing selected
  // by the state is playing, the emotion is not worried / tense, and the cooldown has passed.
  request(id, now, pose) {
    this.applySelection(now);
    const m = typeof id === 'string' ? this.motions.get(id) : undefined;
    let reason = null;
    if (!m) reason = 'unknown motion';
    else if (!m.selectable) reason = 'not selectable (shows a real state)';
    else if (this.cur && this.cur.source === 'state' && this.cur.phase !== 'done') reason = 'a state motion is playing';
    else if (pose && (pose.emotion === 'worried' || pose.emotion === 'tense')) reason = `emotion ${pose.emotion}`;
    else if (now - this.lastAgent < AGENT_COOLDOWN) reason = 'cooldown';
    if (reason) {
      this.ignored.push({ id: typeof id === 'string' ? id.slice(0, 32) : String(typeof id), reason });
      if (this.ignored.length > 20) this.ignored.shift();
      return { ok: false, reason };
    }
    this.lastAgent = now;
    this.pending = null;
    this.cur = { m, start: now, source: 'agent', phase: 'main' };
    return { ok: true };
  }

  has(id) {
    return this.motions.has(id);
  }

  // pose: model.derive().pose; legacy: { numeric, enums, gesture } from anim.js.
  // Returns { frame, info } — frame for CharRig.compose, info for the debug panel / raw view.
  // expr: the face the state asks for (motion_model.FaceHolder.update): eyes / gaze / mouth /
  // brows / xfx / ears; optional.
  tick(now, pose, legacy = { numeric: {}, enums: {}, gesture: false }, expr = null) {
    this.applySelection(now);
    const [eyes, gaze] = EYE_MAP[pose.eyes] || EYE_MAP.open;
    const f = {
      sheet: HEAD_SHEET[String(Math.max(-1, Math.min(1, pose.headY || 0)))] || 'base',
      breath: 0,
      eyes,
      gaze,
      brows: (EMOTION_POSE[pose.emotion] || EMOTION_POSE.calm).brows,
      mouth: pose.mouth || 'neutral',
      earL: -(pose.ears || 0) * 7,
      earR: -(pose.ears || 0) * 7,
      armR: 'mug',
      armL: 'none',
      blush: pose.blush ? 1 : 0,
      sweat: pose.sweat ? 1 : 0,
      fx: 'none',
      desk: 'none', // the desk shot (desk_seat.js): none = she is at the front
      tail: null, // set below: the motion's track, else TAIL_MOOD
      ears: null, // set below: the motion's track, else the state's face (FaceHolder), else angle
    };
    // legacy gestures (anim.js scope "gesture"): numbers -> nearest drawn part
    const n = legacy.numeric || {};
    if (legacy.gesture) {
      const hy = n['head.y'] || 0;
      if (hy > 0.6) f.sheet = hy > 1.5 ? 'down2' : 'down1';
      else if (hy < -0.6) f.sheet = 'up1';
      if ((n['head.x'] || 0) > 0.6) f.sheet = 'yaw_r';
      else if ((n['head.x'] || 0) < -0.6) f.sheet = 'yaw_l';
      f.earL += (n['ear.l'] || 0) * 5;
      f.earR += (n['ear.r'] || 0) * 5;
      if ((n['arm.y'] || 0) < -1.5) f.armR = 'mug_up';
      if (legacy.enums && legacy.enums.eyes) [f.eyes, f.gaze] = EYE_MAP[legacy.enums.eyes] || [f.eyes, f.gaze];
      if (legacy.enums && legacy.enums.mouth) f.mouth = legacy.enums.mouth;
    }
    f.breath = (n['body.breath'] || 0) > 0.35 ? 1 : 0;

    // the motion
    let info = { id: 'none', phase: 'rest', source: null, t: 0 };
    const c = this.cur;
    if (c) {
      let t = now - c.start;
      const m = c.m;
      if (c.phase === 'exit') {
        if (t >= m.exit.duration) {
          this.cur = this.pending ? { ...this.pending, start: now, phase: 'main' } : null;
          this.pending = null;
          return this.tick(now, pose, legacy, expr);
        }
        this.apply(f, m.exit.tracks, t, false, pose);
        info = { id: m.id, phase: 'exit', source: c.source, t };
      } else if (c.phase === 'main') {
        if (!m.loop && t >= m.duration) {
          if (m.then && this.motions.has(m.then)) {
            this.cur = { m: this.motions.get(m.then), start: now, source: c.source, phase: 'main' };
          } else this.cur = { ...c, phase: 'done' };
          return this.tick(now, pose, legacy, expr);
        }
        if (m.loop && t >= m.duration) t = m.intro + ((t - m.intro) % (m.duration - m.intro));
        this.apply(f, m.tracks, t, c.source === 'agent', pose);
        info = { id: m.id, phase: m.loop && t >= m.intro ? 'loop' : 'main', source: c.source, t };
      } else {
        info = { id: m.id, phase: 'done', source: c.source, t };
      }
    }
    // the face from the state (FACE_RULES): over the motion's face, except closed eyes (a sip, a
    // stretch, a blink in the data); its effects replace the old blush / sweat marks
    if (expr && expr.id && expr.id !== 'none') {
      if (expr.eyes && f.eyes !== 'closed') {
        f.eyes = expr.eyes;
        if (expr.gaze) f.gaze = expr.gaze;
      }
      if (expr.mouth) f.mouth = expr.mouth;
      if (expr.brows) f.brows = expr.brows;
    }
    f.xfx = (expr && expr.xfx) || '';
    if (/blush/.test(f.xfx)) f.blush = 0;
    if (/sweat/.test(f.xfx)) f.sweat = 0;
    // the ears and the tail from the state where the motion has no track for them
    if (f.ears === null || f.ears === 'state') f.ears = (expr && expr.ears) || 'angle';
    if (f.tail === null) f.tail = tailFor(pose.emotion, now);
    this.lifeSigns(now, f, pose, info);
    return { frame: f, info };
  }

  apply(f, tracks, t, agent, pose) {
    const faceLocked = agent && (pose.emotion === 'worried' || pose.emotion === 'tense');
    for (const [ch, keys] of Object.entries(tracks)) {
      if (faceLocked && FACE_TRACKS.includes(ch)) continue;
      const isEnum = ENUM_TRACKS.includes(ch);
      const v = trackAt(keys, t, isEnum);
      if (ch === 'blush' || ch === 'sweat') f[ch] = v >= 0.5 ? 1 : 0;
      else f[ch] = v;
    }
  }

  // Blinks (single, double, slow), a glance now and then while nothing happens, an ear flick.
  lifeSigns(now, f, pose, info) {
    const R = this.random;
    if (this.nextBlink === null) this.nextBlink = now + 1.2 + R() * 3;
    if (!this.blink && now >= this.nextBlink) {
      const r = R();
      this.blink = { start: now, kind: r < 0.18 ? 'double' : r < 0.28 && pose.emotion === 'calm' ? 'slow' : 'single' };
      this.nextBlink = now + 2.4 + R() * 3.4;
    }
    if (this.blink) {
      const t = now - this.blink.start;
      const k = this.blink.kind;
      const seq = k === 'slow' ? [[0, 'half'], [0.06, 'closed'], [0.32, 'half'], [0.4, null]]
        : k === 'double' ? [[0, 'half'], [0.04, 'closed'], [0.1, 'half'], [0.14, null], [0.24, 'half'], [0.28, 'closed'], [0.34, 'half'], [0.38, null]]
          : [[0, 'half'], [0.04, 'closed'], [0.11, 'half'], [0.15, null]];
      let v = null;
      for (const [kt, kv] of seq) if (t >= kt) v = kv;
      if (t >= seq[seq.length - 1][0]) this.blink = null;
      if (v && OPEN_EYES.has(f.eyes)) f.eyes = f.eyes === 'half' || v === 'closed' ? 'closed' : 'half';
    }
    const quiet = info.id === 'none' || info.phase === 'done';
    if (this.nextGlance === null) this.nextGlance = now + 5 + R() * 6;
    if (quiet && !this.glance && now >= this.nextGlance && pose.emotion === 'calm') {
      const dirs = ['l', 'r', 'd', 'ul'];
      this.glance = { start: now, dir: dirs[Math.floor(R() * dirs.length)], len: 0.6 + R() * 0.8 };
      this.nextGlance = now + 6 + R() * 7;
    }
    if (this.glance) {
      if (!quiet || now - this.glance.start > this.glance.len) this.glance = null;
      else if (f.eyes === 'open') f.gaze = this.glance.dir;
    }
    if (this.nextFlick === null) this.nextFlick = now + 4 + R() * 6;
    if (!this.flick && now >= this.nextFlick) {
      this.flick = { start: now, side: R() < 0.6 ? 'earR' : 'earL' };
      this.nextFlick = now + 5 + R() * 7;
    }
    if (this.flick) {
      const t = now - this.flick.start;
      if (t > 0.3) this.flick = null;
      else f[this.flick.side] += t < 0.1 ? 8 : t < 0.2 ? -4 : 2;
    }
  }

  activeIds() {
    const c = this.cur;
    return c && c.phase !== 'done' ? [`${c.m.id}${c.phase === 'exit' ? ' (exit)' : ''}${c.source === 'agent' ? ' (agent)' : ''}`] : [];
  }
}

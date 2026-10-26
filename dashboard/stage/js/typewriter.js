// Typewriter for the text window (#350). Pure: no DOM, no clock — tick(dt) with the same dt
// sequence always shows the same text (tests/stage_talk_test.cjs).
//
// - Characters are user-perceived characters (grapheme clusters when Intl.Segmenter exists,
//   else code points): a kana, a kanji, an emoji with its modifiers never splits.
// - It follows a stream: receive(textSoFar, { open }) adds what arrived; the typewriter never
//   shows a character that has not arrived.
// - Speed: BASE_CPS. A backlog longer than CATCH_UP_S seconds at that pace (a whole reply
//   arriving at once) is shown faster so it ends in about CATCH_UP_S seconds, but never faster
//   than MAX_CPS (the upper bound of the display speed).
// - skip() shows everything that has arrived; `instant` (prefers-reduced-motion) shows each
//   arrival at once.

export const BASE_CPS = 38;
export const MAX_CPS = 90;
export const CATCH_UP_S = 3;

const segmenter = typeof Intl !== 'undefined' && Intl.Segmenter ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null;

export function splitChars(text) {
  const s = String(text ?? '');
  if (segmenter) return Array.from(segmenter.segment(s), (x) => x.segment);
  return Array.from(s);
}

export class Typewriter {
  constructor({ cps = BASE_CPS, maxCps = MAX_CPS, catchUp = CATCH_UP_S, instant = false } = {}) {
    this.cps = cps;
    this.maxCps = maxCps;
    this.catchUp = catchUp;
    this.instant = instant;
    this.reset('');
  }

  // A new line. open: more text may still arrive (receive / close).
  reset(text = '', { open = false } = {}) {
    this.source = String(text ?? '');
    this.chars = splitChars(this.source);
    this.shown = this.instant ? this.chars.length : 0;
    this.open = open;
    this.acc = 0;
    this.cache = null;
  }

  // The whole text received so far (a stream only grows). Anything that does not extend the
  // current text starts a new line.
  receive(text, { open = true } = {}) {
    const s = String(text ?? '');
    if (!s.startsWith(this.source)) {
      this.reset(s, { open });
      return;
    }
    if (s.length > this.source.length) {
      this.source = s;
      this.chars = splitChars(s);
      this.cache = null;
      if (this.instant) this.shown = this.chars.length;
    }
    this.open = open;
  }

  close() {
    this.open = false;
  }

  get received() {
    return this.chars.length;
  }

  get typing() {
    return this.shown < this.chars.length;
  }

  // Everything that will ever be shown is shown.
  get done() {
    return !this.typing && !this.open;
  }

  get text() {
    if (this.cache === null || this.cache.n !== this.shown) this.cache = { n: this.shown, s: this.chars.slice(0, this.shown).join('') };
    return this.cache.s;
  }

  rate() {
    const left = this.chars.length - this.shown;
    if (left <= this.cps * this.catchUp) return this.cps;
    return Math.min(this.maxCps, left / this.catchUp);
  }

  // Returns true when the shown text changed.
  tick(dt) {
    if (!this.typing) {
      this.acc = 0;
      return false;
    }
    if (this.instant) {
      this.shown = this.chars.length;
      return true;
    }
    this.acc += Math.max(0, dt) * this.rate();
    const n = Math.floor(this.acc);
    if (n <= 0) return false;
    this.acc -= n;
    this.shown = Math.min(this.chars.length, this.shown + n);
    return true;
  }

  // Show what has arrived now. Returns false if there was nothing left to show.
  skip() {
    if (!this.typing) return false;
    this.shown = this.chars.length;
    this.acc = 0;
    return true;
  }
}

// Phase 1 of #350 (no whiteboard yet): a long reply speaks only its first lines in the text
// window and points to the log for the rest. These are display limits of the text window,
// not the phase-2 board rules (docs/stage-main-ui.md §5).
export const LINE_MAX_CHARS = 120;
export const LINE_MAX_ROWS = 3;

// -> { text, more }: the excerpt of a longer prefix of the reply starts with the excerpt of a
// shorter one, so it can follow a stream; more = something was cut.
export function dialogueExcerpt(reply, { maxChars = LINE_MAX_CHARS, maxRows = LINE_MAX_ROWS } = {}) {
  const full = String(reply ?? '').replace(/^\s+/, '');
  const rows = full.split('\n');
  let text = rows.slice(0, maxRows).join('\n');
  let more = rows.length > maxRows;
  const chars = splitChars(text);
  if (chars.length > maxChars) {
    text = chars.slice(0, maxChars).join('');
    more = true;
  }
  return { text, more };
}

// #350 phase 1: the Stage's conversation — typewriter, dialogue excerpt, backlog (sanitised
// rendering), composer rules (IME), narration vs reply, and the live feed taking the chat
// stream's own turn events. node --test tests/stage_talk_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const core = require('../dashboard/static/js/chat_core.js');

// --- typewriter ----------------------------------------------------------------------------
test('typewriter is deterministic: the same dt sequence shows the same text', async () => {
  const { Typewriter, BASE_CPS } = await load('typewriter.js');
  const run = () => {
    const tw = new Typewriter();
    tw.reset('こんにちは、今日もお疲れさまでした。');
    const seen = [];
    for (const dt of [0.016, 0.017, 0.05, 0.1, 0.016, 0.2, 0.033]) {
      tw.tick(dt);
      seen.push(tw.text);
    }
    return seen;
  };
  assert.deepEqual(run(), run());
  const tw = new Typewriter();
  tw.reset('あいうえおかきくけこ');
  tw.tick(1 / BASE_CPS); // one character per 1/38 s
  assert.equal(tw.text, 'あ');
  tw.tick(3 / BASE_CPS);
  assert.equal(tw.text, 'あいうえ');
  assert.equal(tw.typing, true);
  assert.equal(tw.skip(), true);
  assert.equal(tw.text, 'あいうえおかきくけこ');
  assert.equal(tw.done, true);
  assert.equal(tw.skip(), false);
});

test('typewriter never splits a character (kana, kanji, emoji with modifiers, surrogates)', async () => {
  const { Typewriter, splitChars } = await load('typewriter.js');
  const text = '漢字👍🏽と👨‍👩‍👧𠮷';
  assert.deepEqual(splitChars(text), ['漢', '字', '👍🏽', 'と', '👨‍👩‍👧', '𠮷']);
  const tw = new Typewriter();
  tw.reset(text);
  const shown = [];
  while (tw.typing) {
    tw.tick(0.03);
    shown.push(tw.text);
  }
  for (const s of shown) assert.ok(text.startsWith(s) && !/[\uD800-\uDBFF]$/.test(s), s);
  assert.ok(shown.includes('漢字👍🏽'));
});

test('typewriter follows a stream and never shows what has not arrived', async () => {
  const { Typewriter } = await load('typewriter.js');
  const tw = new Typewriter();
  tw.receive('今日', { open: true });
  tw.tick(10);
  assert.equal(tw.text, '今日');
  assert.equal(tw.done, false); // the stream is still open
  tw.receive('今日は晴れ', { open: true });
  tw.tick(1 / 38);
  assert.equal(tw.text, '今日は');
  tw.receive('今日は晴れです。', { open: false });
  tw.tick(10);
  assert.equal(tw.text, '今日は晴れです。');
  assert.equal(tw.done, true);
  tw.receive('別の行'); // not an extension: a new line
  assert.equal(tw.text, '');
});

test('a long backlog is shown faster, but never above the speed limit', async () => {
  const { Typewriter, MAX_CPS, BASE_CPS, CATCH_UP_S } = await load('typewriter.js');
  const tw = new Typewriter();
  tw.reset('あ'.repeat(2000));
  tw.tick(1);
  assert.equal(tw.shown, MAX_CPS); // capped
  const mid = new Typewriter();
  mid.reset('い'.repeat(Math.round(BASE_CPS * CATCH_UP_S * 1.5)));
  mid.tick(1);
  assert.ok(mid.shown > BASE_CPS && mid.shown < MAX_CPS, String(mid.shown));
  const short = new Typewriter();
  short.reset('う'.repeat(50));
  short.tick(1);
  assert.equal(short.shown, BASE_CPS);
});

test('reduced motion (instant): each arrival is shown at once', async () => {
  const { Typewriter } = await load('typewriter.js');
  const tw = new Typewriter({ instant: true });
  tw.reset('すぐに表示');
  assert.equal(tw.text, 'すぐに表示');
  tw.receive('すぐに表示されます', { open: true });
  assert.equal(tw.text, 'すぐに表示されます');
});

test('dialogue excerpt: first 3 rows / 120 characters, stable under a growing stream', async () => {
  const { dialogueExcerpt } = await load('typewriter.js');
  assert.deepEqual(dialogueExcerpt('短い返答です。'), { text: '短い返答です。', more: false });
  const long = 'あ'.repeat(130);
  assert.equal(dialogueExcerpt(long).text.length, 120);
  assert.equal(dialogueExcerpt(long).more, true);
  const rows = 'one\ntwo\nthree\nfour\nfive';
  assert.deepEqual(dialogueExcerpt(rows), { text: 'one\ntwo\nthree', more: true });
  const reply = '最初の段落です。\n\n```python\nprint(1)\n```\n' + 'い'.repeat(200);
  let prev = '';
  for (let i = 1; i <= reply.length; i += 7) {
    const { text } = dialogueExcerpt(reply.slice(0, i));
    assert.ok(text.startsWith(prev), `excerpt of ${i} chars extends the previous one`);
    prev = text;
  }
});

// --- composer rules ------------------------------------------------------------------------
test('composer keys: IME Enter (isComposing / keyCode 229 / composition) never sends', async () => {
  const { keyAction } = await load('talk.js');
  const ctx = (o = {}) => ({ composing: false, empty: false, isSendKey: core.isSendKey, ...o });
  const enter = (o = {}) => ({ key: 'Enter', keyCode: 13, isComposing: false, shiftKey: false, ...o });
  assert.equal(keyAction(enter(), ctx()), 'send');
  assert.equal(keyAction(enter({ shiftKey: true }), ctx()), null);
  assert.equal(keyAction(enter({ isComposing: true }), ctx()), null);
  assert.equal(keyAction(enter({ keyCode: 229 }), ctx()), null);
  assert.equal(keyAction(enter(), ctx({ composing: true })), null);
  // ↑ in an empty composer opens the log; not while composing, not with text
  assert.equal(keyAction({ key: 'ArrowUp' }, ctx({ empty: true })), 'log');
  assert.equal(keyAction({ key: 'ArrowUp' }, ctx({ empty: false })), null);
  assert.equal(keyAction({ key: 'ArrowUp', isComposing: true }, ctx({ empty: true })), null);
  assert.equal(keyAction({ key: 'ArrowUp' }, ctx({ empty: true, composing: true })), null);
});

test('composer state: one reason at a time; stop only while this page sends', async () => {
  const { composerState } = await load('talk.js');
  const base = { ready: true, busy: false, reading: false, offline: false, approval: false, turnOpen: false, unsettled: false };
  assert.deepEqual(composerState(base), { canSend: true, stop: false, reason: null });
  assert.deepEqual(composerState({ ...base, busy: true }), { canSend: false, stop: true, reason: 'stage.note_sending' });
  assert.equal(composerState({ ...base, busy: true, approval: true }).reason, 'stage.note_approval_own');
  assert.equal(composerState({ ...base, ready: false }).reason, 'stage.note_loading');
  assert.equal(composerState({ ...base, offline: true }).reason, 'stage.note_offline');
  assert.equal(composerState({ ...base, approval: true }).reason, 'stage.note_approval');
  assert.equal(composerState({ ...base, turnOpen: true }).reason, 'stage.note_busy_other');
  assert.equal(composerState({ ...base, turnOpen: true, turnStopped: true }).reason, 'stage.note_stopped_running');
  assert.deepEqual(composerState({ ...base, unsettled: true }), { canSend: true, stop: false, reason: 'stage.note_unsettled' });
  assert.equal(composerState({ ...base, offline: true }).canSend, false);
});

test('a reply on screen stays for "turn finished", yields to new activity and urgent states', async () => {
  const { narrationWins } = await load('talk.js');
  assert.equal(narrationWins(null, 'stage.say_idle', false), true);
  const pin = { key: undefined };
  assert.equal(narrationWins(pin, 'stage.say_thinking', true), false); // adopts the current key
  assert.equal(pin.key, 'stage.say_thinking');
  assert.equal(narrationWins(pin, 'stage.say_tool', true), false); // own turn: a tool does not take it
  assert.equal(narrationWins(pin, 'stage.say_approval', true), true); // urgent
  assert.equal(narrationWins(pin, 'stage.say_done_ok', false), false); // the turn ended: keep the reply
  assert.equal(narrationWins(pin, 'stage.say_idle', false), false);
  assert.equal(narrationWins(pin, 'stage.say_thinking', false), false); // same key as when pinned
  assert.equal(narrationWins({ key: 'stage.say_done_ok' }, 'stage.say_thinking', false), true); // another turn
  const soft = { key: 'stage.say_done_ok', soft: true }; // just sent
  assert.equal(narrationWins(soft, 'stage.say_done_ok', true), false);
  assert.equal(narrationWins(soft, 'stage.say_thinking', true), true);
});

test('a re-send passes the same gate as a send (canSend)', async () => {
  const { composerState, resendAllowed } = await load('talk.js');
  const base = { ready: true, busy: false, reading: false, offline: false, approval: false, turnOpen: false, unsettled: true };
  const u = { message: 'x', images: [], body: '{}', key: 'k' };
  assert.equal(resendAllowed(u, composerState(base)), true);
  for (const blocked of [{ busy: true }, { ready: false }, { offline: true }, { approval: true }, { turnOpen: true }, { turnOpen: true, turnStopped: true }, { reading: true }]) {
    assert.equal(resendAllowed(u, composerState({ ...base, ...blocked })), false, JSON.stringify(blocked));
    assert.equal(composerState({ ...base, ...blocked }).canSend, false);
  }
  assert.equal(resendAllowed(null, composerState(base)), false);
  const src = fs.readFileSync(path.join(STAGE, 'talk.js'), 'utf8');
  const click = src.slice(src.indexOf("el.resend.addEventListener('click'"), src.indexOf("el.discard.addEventListener('click'"));
  assert.match(click, /resendAllowed\(u, status\(\)\)/);
  assert.match(src, /el\.resend\.disabled = !resendAllowed\(u, c\)/);
});

test('discard is its own confirmed step; Stop never drops the key', async () => {
  const { confirmDiscard } = await load('talk.js');
  const t = (k, v) => (v ? `${k}:${JSON.stringify(v)}` : k);
  const u = { message: 'hello', images: [{}, {}] };
  assert.equal(await confirmDiscard(undefined, u, t), false); // no dialog: nothing discarded
  assert.equal(await confirmDiscard({ open: async () => false }, u, t), false);
  assert.equal(await confirmDiscard({ open: async () => { throw new Error('x'); } }, u, t), false);
  assert.equal(await confirmDiscard({ open: async () => 'yes' }, u, t), false); // only true
  let asked = null;
  assert.equal(await confirmDiscard({ open: async (o) => { asked = o; return true; } }, u, t), true);
  assert.equal(asked.title, 'stage.discard_title');
  assert.equal(asked.intro, 'stage.discard_intro');
  assert.deepEqual(asked.items, ['hello', 'chat.unsettled_images:{"count":2}']);
  const src = fs.readFileSync(path.join(STAGE, 'talk.js'), 'utf8');
  assert.equal(src.split('sender.discard()').length - 1, 1, 'one discard call: the confirmed one');
  const handler = src.slice(src.indexOf("el.discard.addEventListener('click'"));
  assert.ok(handler.indexOf('confirmDiscard(') > 0 && handler.indexOf('confirmDiscard(') < handler.indexOf('sender.discard()'));
  const stopped = src.slice(src.indexOf('} else if (result.stopped) {'), src.indexOf('} else {', src.indexOf('} else if (result.stopped) {')));
  assert.doesNotMatch(stopped, /\.discard\(/);
});

test('restored text is cleared only by its own send settling, and only if never edited', async () => {
  const { refillOutcome } = await load('talk.js');
  const marker = { key: 'kA', gen: 3 };
  const none = { clear: false, drop: false };
  // another send (new key B) completing never touches the restored draft A
  assert.deepEqual(refillOutcome(marker, { key: 'kB', ok: true, gen: 3 }), none);
  assert.deepEqual(refillOutcome(marker, { key: 'kB', ok: false, delivery: 'duplicate', gen: 3 }), none);
  // its own send: delivered, composer untouched since the text was put back -> cleared
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: true, gen: 3 }), { clear: true, drop: true });
  // "already received" counts too, for that key only
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: false, delivery: 'duplicate', gen: 3 }), { clear: true, drop: true });
  // edited after the restore (any input event, a single space included): kept, marker gone
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: true, gen: 4 }), { clear: false, drop: true });
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: false, delivery: 'duplicate', gen: 9 }), { clear: false, drop: true });
  // refused (nothing ran): never cleared, marker gone; still unknown: nothing changes
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: false, delivery: 'refused', gen: 3 }), { clear: false, drop: true });
  assert.deepEqual(refillOutcome(marker, { key: 'kA', ok: false, delivery: 'unknown', gen: 3 }), none);
  // no marker (e.g. after a confirmed discard), or no key: nothing
  assert.deepEqual(refillOutcome(null, { key: 'kA', ok: true, gen: 3 }), none);
  assert.deepEqual(refillOutcome(marker, { key: undefined, ok: true, gen: 3 }), none);
});

test('the restore marker is tied to the key, every edit bumps the generation, discard drops it', () => {
  const src = fs.readFileSync(path.join(STAGE, 'talk.js'), 'utf8');
  const refill = src.slice(src.indexOf('function refill('), src.indexOf('async function deliver('));
  assert.match(refill, /st\.refill = u \? \{ key: u\.key, gen: st\.inputGen \} : null/);
  assert.doesNotMatch(src, /trim\(\) === st\./, 'no text comparison decides the clearing');
  const input = src.slice(src.indexOf("el.input.addEventListener('input'"), src.indexOf("el.input.addEventListener('paste'"));
  assert.match(input, /st\.inputGen \+= 1/);
  const discard = src.slice(src.indexOf("el.discard.addEventListener('click'"));
  assert.ok(discard.indexOf('st.refill = null') > discard.indexOf('sender.discard()'), 'a confirmed discard drops the marker');
  const deliver = src.slice(src.indexOf('async function deliver('), src.indexOf('// --- backlog'));
  assert.match(deliver, /refillOutcome\(st\.refill, \{ key: result\.key/);
  // the only places that clear the composer in deliver: the marker decision
  assert.equal((deliver.match(/el\.input\.value = ''/g) || []).length, 1);
});

test("the reviewer's sequence: A stopped, restored, discarded; B sent; the new draft A stays", async () => {
  const { refillOutcome } = await load('talk.js');
  // a small model of talk.js's state transitions (marker, generation) driven by the exported rule
  const st = { refill: null, inputGen: 0, input: '' };
  const restore = (text, key) => { st.input = text; st.refill = { key, gen: st.inputGen }; };
  const type = (text) => { st.input = text; st.inputGen += 1; };
  const settle = (r) => {
    const o = refillOutcome(st.refill, { ...r, gen: st.inputGen });
    if (o.clear) st.input = '';
    if (o.drop) st.refill = null;
  };
  restore('A', 'kA'); // A sent, Stop: A back in the composer
  st.refill = null; // confirmed discard
  type('B');
  st.input = ''; // B submitted (the composer is emptied on submit, no input event)
  type('A'); // a new draft that happens to read A
  settle({ key: 'kB', ok: true });
  assert.equal(st.input, 'A');
  // whitespace-only edit after a restore protects the draft as well
  restore('A', 'kA2');
  type('A ');
  settle({ key: 'kA2', ok: false, delivery: 'duplicate' });
  assert.equal(st.input, 'A ');
  // untouched restore + its own duplicate -> cleared
  restore('C', 'kC');
  settle({ key: 'kC', ok: false, delivery: 'duplicate' });
  assert.equal(st.input, '');
});

test('a duplicate refusal is noted after the turn of that key, else as a standalone note', async () => {
  const { duplicateNote } = await load('talk.js');
  const { buildEntries } = await load('backlog.js');
  const t = (k, v) => (v ? `${k}(${v.message})` : k);
  const turns = [
    { message: 'A', response: 'ra', turn_id: 'tA' }, // the stopped send, recorded
    { message: 'X', response: 'rx', turn_id: 'tX' }, // a newer turn from elsewhere
  ];
  const show = (notes) => buildEntries(turns, notes).map((x) => `${x.who}:${x.text}`);
  // known turn id: after that turn, not after the newest one
  assert.deepEqual(show([duplicateNote('tA', 'tX', 'A', t)]), ['user:A', 'agent:ra', 'sys:stage.log_duplicate_turn', 'user:X', 'agent:rx']);
  // id not in the window yet: in time order (the anchor) until the history has it
  assert.deepEqual(show([duplicateNote('tLater', 'tA', 'A', t)]), ['user:A', 'agent:ra', 'sys:stage.log_duplicate_turn', 'user:X', 'agent:rx']);
  // no id: a standalone note naming the message, in time order, not tied to a turn
  const lone = duplicateNote(undefined, 'tX', 'A', t);
  assert.equal(lone.standalone, true);
  assert.equal(lone.turn, undefined);
  assert.equal(lone.text, 'stage.log_duplicate(A)');
  assert.deepEqual(show([lone]), ['user:A', 'agent:ra', 'user:X', 'agent:rx', 'sys:stage.log_duplicate(A)']);
  const src = fs.readFileSync(path.join(STAGE, 'talk.js'), 'utf8');
  assert.doesNotMatch(src, /after: lastTurnId\(\)/, 'a note is never attached to the newest turn after the fact');
});

// --- backlog -------------------------------------------------------------------------------
test('backlog entries: history turns, local notes after their anchor, the turn in flight', async () => {
  const { buildEntries } = await load('backlog.js');
  const turns = [
    { message: 'a', response: 'A', turn_id: 't1' },
    { message: 'b', response: 'B', turn_id: 't2', image_count: 2 },
  ];
  const notes = [{ after: 't1', who: 'user', text: 'x' }, { after: 't1', who: 'sys', text: 'stopped' }];
  const e = buildEntries(turns, notes, { message: 'c', reply: 'C…' });
  assert.deepEqual(e.map((x) => `${x.who}:${x.text}`), ['user:a', 'agent:A', 'user:x', 'sys:stopped', 'user:b', 'agent:B', 'user:c', 'agent:C…']);
  assert.equal(e[4].images, 2);
  assert.equal(e[7].pending, true);
  // a stopped send that the server finished and recorded: no duplicate message, note after it
  const done = [...turns, { message: 'x', response: 'X', turn_id: 't3' }];
  const notes2 = [{ after: 't2', who: 'user', text: 'x' }, { after: 't2', who: 'sys', text: 'stopped' }];
  assert.deepEqual(buildEntries(done, notes2).map((x) => `${x.who}:${x.text}`), ['user:a', 'agent:A', 'user:b', 'agent:B', 'user:x', 'agent:X', 'sys:stopped']);
  // anchor fell out of the 50-turn window: the notes come first
  assert.deepEqual(buildEntries(turns, [{ after: 'gone', who: 'sys', text: 'n' }]).map((x) => x.text), ['n', 'a', 'A', 'b', 'B']);
  // non-text fields are ignored, not shown
  assert.deepEqual(buildEntries([{ message: { x: 1 }, response: 3 }]).map((x) => x.text), ['', '']);
});

test('backlog: a stopped send is matched to its recorded turn by turn id first', async () => {
  const { buildEntries } = await load('backlog.js');
  const show = (e) => e.map((x) => `${x.who}:${x.text}`);
  const turns = [
    { message: 'a', response: 'A', turn_id: 't1' },
    { message: 'same', response: 'other turn', turn_id: 't2' }, // same text, another turn
    { message: 'same', response: 'S', turn_id: 't3' }, // the stopped send, recorded
  ];
  const n = (o) => [{ after: 't1', group: 1, who: 'user', text: 'same', ...o }, { after: 't1', group: 1, who: 'sys', text: 'stopped', ...o }];
  // by id: the note follows t3, not the text match right after the anchor (t2)
  assert.deepEqual(show(buildEntries(turns, n({ turn: 't3' }))), ['user:a', 'agent:A', 'user:same', 'agent:other turn', 'user:same', 'agent:S', 'sys:stopped']);
  // id known but not recorded (yet): nothing dropped, even though t2 has the same text
  assert.deepEqual(show(buildEntries(turns, n({ turn: 't9' }))), ['user:a', 'agent:A', 'user:same', 'sys:stopped', 'user:same', 'agent:other turn', 'user:same', 'agent:S']);
  // no id: the fallback needs the same image count
  const withImages = [{ after: 't1', group: 1, who: 'user', text: 'same', images: 2 }, { after: 't1', group: 1, who: 'sys', text: 'stopped' }];
  assert.deepEqual(show(buildEntries(turns, withImages)).slice(0, 4), ['user:a', 'agent:A', 'user:same', 'sys:stopped']);
  // a recorded turn is matched once: a second note with the same text keeps its own copy
  const twice = [...n({}), { after: 't1', group: 2, who: 'user', text: 'same' }, { after: 't1', group: 2, who: 'sys', text: 'failed' }];
  assert.deepEqual(show(buildEntries(turns, twice)), ['user:a', 'agent:A', 'user:same', 'sys:failed', 'user:same', 'agent:other turn', 'sys:stopped', 'user:same', 'agent:S']);
  // the id claims its turn before any text fallback can take it
  const both = [...n({ turn: 't2' }), { after: 't1', group: 2, who: 'user', text: 'same' }, { after: 't1', group: 2, who: 'sys', text: 'failed' }];
  assert.deepEqual(show(buildEntries(turns, both)), ['user:a', 'agent:A', 'user:same', 'sys:failed', 'user:same', 'agent:other turn', 'sys:stopped', 'user:same', 'agent:S']);
});

// A DOM that records what is built and refuses every HTML-parsing entry point.
function fakeDoc() {
  const made = [];
  class Node {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this._text = '';
      this.className = '';
      made.push(tag);
    }
    set textContent(v) {
      this._text = String(v);
      this.children = [];
    }
    get textContent() {
      return this._text + this.children.map((c) => c.textContent).join('');
    }
    set innerHTML(_v) {
      throw new Error('innerHTML used');
    }
    insertAdjacentHTML() {
      throw new Error('insertAdjacentHTML used');
    }
    setAttribute(k) {
      if (/^on/i.test(k) || k === 'href' || k === 'src') throw new Error(`attribute ${k} set`);
    }
    append(...n) {
      this.children.push(...n);
    }
    replaceChildren(...n) {
      this.children = n;
    }
  }
  return { made, createElement: (tag) => new Node(tag), list: new Node('ol') };
}

test('backlog rendering is text only: <script>, on* and javascript: stay plain text', async () => {
  const { buildEntries, renderEntries } = await load('backlog.js');
  const { buildStrings } = await load('i18n.js');
  const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  const S = buildStrings({ ja, en: {} }, 'ja');
  const evil = '<script>alert(1)</script><img src=x onerror="alert(2)"><a href="javascript:alert(3)" onclick="x()">l</a>';
  const doc = fakeDoc();
  const items = renderEntries(doc, doc.list, buildEntries([{ message: '<b onmouseover="y()">hi</b>', response: evil, turn_id: 't' }]), S);
  assert.equal(items.length, 2);
  assert.deepEqual([...new Set(doc.made)].sort(), ['li', 'ol', 'p', 'span']);
  assert.equal(items[1].children[1].textContent, evil); // the reply, verbatim, as text
  assert.equal(items[0].children[1].textContent, '<b onmouseover="y()">hi</b>');
  assert.equal(items[1].children[0].textContent, S.name);
});

test('Stage sources never parse HTML or run code; the composer posts only to /api/chat', () => {
  const files = fs.readdirSync(STAGE).filter((f) => f.endsWith('.js'));
  for (const f of files) {
    const src = fs.readFileSync(path.join(STAGE, f), 'utf8');
    assert.doesNotMatch(src, /\.innerHTML\s*=|outerHTML\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function\(/, f);
  }
  for (const f of ['talk.js', 'backlog.js', 'typewriter.js']) {
    const src = fs.readFileSync(path.join(STAGE, f), 'utf8');
    assert.doesNotMatch(src, /https?:\/\//, f);
    assert.doesNotMatch(src, /[`'"]\/api\//, `${f}: requests go through chat_core.js`);
    assert.doesNotMatch(src, /\bfetch\(/, `${f}: no direct fetch`);
  }
});

// --- live feed: the chat stream's own turn events ------------------------------------------
test('turn events from the chat stream are shown at once and not again by the poll', async () => {
  const { LiveFeed } = await load('live.js');
  const evs = [
    { turn_id: 'T1', seq: 1, type: 'turn.started', data: {} },
    { turn_id: 'T1', seq: 2, type: 'turn.summary', data: { after: { stability: 0.6 } } },
    { turn_id: 'T1', seq: 3, type: 'turn.completed', data: { error: null, stop: 'completed' } },
  ];
  const getJSON = async (url) => {
    if (url.startsWith('/api/turns?')) return { turns: [{ turn_id: 'T1', started_at: 100, completed: true }] };
    if (url.startsWith('/api/turns/T1/events')) {
      const after = Number(new URL(url, 'http://x').searchParams.get('after_seq'));
      return { events: evs.filter((e) => e.seq > after) };
    }
    return {};
  };
  let now = 0;
  const feed = new LiveFeed({ getJSON, clock: () => now, wall: () => 1000 });
  feed.booted = true;
  assert.equal(feed.ingestTurnEvent(evs[0]), true);
  assert.equal(feed.turnOpen, true);
  assert.deepEqual(feed.drain(0).map((e) => e.type), ['turn.start']);
  assert.equal(feed.ingestTurnEvent(evs[0]), false); // same seq twice: once
  await feed.followTurns(); // the poll reads seq 2..3 only
  now = 5;
  assert.deepEqual(feed.drain(now).map((e) => e.type), ['neural', 'turn.end']);
  assert.equal(feed.turnOpen, false);
  assert.equal(feed.ingestTurnEvent({ type: 'x' }), false); // no turn id: ignored
});

test('i18n: every key the conversation uses exists in both catalogs', () => {
  const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
  const used = new Set();
  for (const f of ['talk.js', 'backlog.js', 'ui.js']) {
    const src = fs.readFileSync(path.join(STAGE, f), 'utf8');
    for (const m of src.matchAll(/'((?:stage|chat)\.[a-z0-9_]+)'/g)) used.add(m[1]);
  }
  const tpl = fs.readFileSync(path.join(ROOT, 'dashboard/templates/stage.html'), 'utf8');
  for (const m of tpl.matchAll(/data-i18n(?:-[a-z-]+)?="([^"]+)"/g)) used.add(m[1]);
  assert.ok(used.size > 20);
  for (const k of used) {
    assert.ok(k in ja, `ja: ${k}`);
    assert.ok(k in en, `en: ${k}`);
  }
});

// Stage conversation (#350 phase 1): the composer inside the text window, the reply as a
// typed line, the backlog (log), stop and re-send. Sending is the chat page's own logic
// (static/js/chat_core.js: POST /api/chat, CSRF via stiraFetch, Idempotency-Key, re-send /
// discard of an unknown outcome, the image rules); this file is only the Stage's DOM side.
// No new API and no new permission. All text is set with textContent.
//
// Phase 2: a reply that does not fit the text window goes on the whiteboard (board.js,
// markdown.classify; the line speaks its lead), the log renders replies with the same
// Markdown renderer, and the context gauge shows the last turn's input against the context
// window (chat_core.contextGauge).
// Phase 3: "New conversation" (the button next to the log, or `/new` typed alone; never sent
// as a message) asks for confirmation and calls POST /api/chat/session/new
// (chat_core.createSessionStarter). It is unavailable while a turn runs, an approval waits or a
// send's outcome is unknown (chat_core.newSessionBlock); the server refuses the same cases.

import { buildEntries, renderEntries } from './backlog.js';
import { finalBoardAction } from './board.js';
import { classify, classifyStream, parse } from './markdown.js';
import { renderBlocks } from './md_render.js';
import { dialogueExcerpt } from './typewriter.js';

const $ = (id) => document.getElementById(id);

// --- pure rules (tests/stage_talk_test.cjs) ------------------------------------------------

// Narration keys that only say "the turn is over / nothing runs": a reply on screen stays.
export const AFTER_TURN = new Set(['stage.say_done_ok', 'stage.say_done_error', 'stage.say_idle']);
// Keys that take the window even while this page's own turn is streaming.
export const URGENT = new Set(['stage.say_offline', 'stage.say_approval', 'stage.say_approval_more', 'stage.say_critical', 'stage.say_warning', 'stage.say_coherence']);

// pin: the conversation's hold on the text window ({ key, soft }), or null. key = the
// narration key when the hold began (adopted from the first narration seen). soft: any new
// narration wins (just after sending, until the turn's own events speak).
export function narrationWins(pin, key, busy) {
  if (!pin) return true;
  if (pin.key === undefined) {
    pin.key = key;
    return false;
  }
  if (key === pin.key) return false;
  if (pin.soft) return true;
  if (busy) return URGENT.has(key);
  return !AFTER_TURN.has(key);
}

// What the composer allows now and the one line that says why not.
// s: { ready, busy, reading, offline, approval, turnOpen, turnStopped, unsettled, resetUnknown,
//      starting (a new conversation is being started, until its history is on screen),
//      historyStale (the reset succeeded but the new history could not be read: until a re-read) }
// (resetUnknown: a new-conversation request whose outcome is unknown; a message now could land
// in the conversation about to be dropped, so nothing is sent until it is resolved.)
// (turnStopped: the open turn is the one this page stopped; the server finishes it.)
export function composerState(s) {
  if (s.busy) return { canSend: false, stop: true, reason: s.approval ? 'stage.note_approval_own' : 'stage.note_sending' };
  if (!s.ready) return { canSend: false, stop: false, reason: 'stage.note_loading' };
  if (s.offline) return { canSend: false, stop: false, reason: 'stage.note_offline' };
  if (s.approval) return { canSend: false, stop: false, reason: 'stage.note_approval' };
  if (s.turnOpen) return { canSend: false, stop: false, reason: s.turnStopped ? 'stage.note_stopped_running' : 'stage.note_busy_other' };
  if (s.starting) return { canSend: false, stop: false, reason: 'chat.new_blocked_starting' };
  if (s.historyStale) return { canSend: false, stop: false, reason: 'chat.new_history_failed' };
  if (s.resetUnknown) return { canSend: false, stop: false, reason: 'chat.new_unknown_blocks_send' };
  if (s.reading) return { canSend: false, stop: false, reason: null };
  return { canSend: true, stop: false, reason: s.unsettled ? 'stage.note_unsettled' : null };
}

// A key in the composer -> 'send' | 'log' | null. core.isSendKey owns the IME rule: Enter
// while composing (isComposing, keyCode 229, compositionstart..end) never sends.
export function keyAction(event, { composing, empty, isSendKey }) {
  if (isSendKey(event, composing)) return 'send';
  if (event.key === 'ArrowUp' && empty && !composing && !event.isComposing && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey) return 'log';
  return null;
}

const oneLine = (text, n = 80) => {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
};

// The restored-text marker ({ key, gen } | null) after a send with `key` ended:
// clear the composer only when that very send settled (ok, or "already received") and the
// composer was not edited since the text was put back; the marker goes when its send settles
// or was refused (nothing ran). Other sends never touch it.
export function refillOutcome(marker, { key, ok, delivery, gen }) {
  if (!marker || !key || marker.key !== key) return { clear: false, drop: false };
  const settled = Boolean(ok) || delivery === 'duplicate';
  return { clear: settled && marker.gen === gen, drop: settled || delivery === 'refused' };
}

// The log note for a send refused as a duplicate: after the turn the stream announced for
// that key when known, otherwise a standalone note (naming the message) in time order.
export function duplicateNote(turnId, anchor, message, t) {
  // (in time order until the history has that turn; backlog.js then moves it after the turn)
  if (typeof turnId === 'string' && turnId) return { after: anchor, turn: turnId, who: 'sys', text: t('stage.log_duplicate_turn') };
  return { after: anchor, who: 'sys', standalone: true, text: t('stage.log_duplicate', { message: oneLine(message, 60) }) };
}

// A re-send goes through the same gate as a send (status().canSend).
export function resendAllowed(unsettled, state) {
  return Boolean(unsettled && state && state.canSend);
}

// Discarding an unknown outcome is its own confirmed step: the send may already have been
// processed. Without the dialog nothing is discarded (fail closed).
export async function confirmDiscard(confirm, unsettled, t) {
  if (!confirm || typeof confirm.open !== 'function' || !unsettled) return false;
  const items = [oneLine(unsettled.message, 80) || t('stage.discard_no_text')];
  if (unsettled.images && unsettled.images.length) items.push(t('chat.unsettled_images', { count: unsettled.images.length }));
  try {
    return (await confirm.open({
      title: t('stage.discard_title'),
      intro: t('stage.discard_intro'),
      items,
      confirmLabel: t('stage.discard_confirm'),
      cancelLabel: t('hub.cancel'),
    })) === true;
  } catch {
    return false;
  }
}

// What the text window says for a reply (pure): { mode, say, more, c }. A board reply speaks
// its lead (the first short paragraph) or a fixed line; a line reply speaks its text without
// Markdown marks. prevMode: the mode this reply had so far (a stream is never demoted).
export function replyView(reply, prevMode, t) {
  const c = classifyStream(String(reply ?? '').replace(/^\s+/, ''), prevMode);
  if (c.mode === 'board') return { mode: 'board', say: c.lead || t('stage.say_board'), more: false, c };
  const ex = dialogueExcerpt(c.line);
  return { mode: 'line', say: ex.text, more: ex.more, c };
}

// `/new` typed alone (any case, surrounding spaces, with or without images) is the page's own
// command and is never sent. Fail closed: if the shared core lacks commandOf (an old copy),
// the same rule is applied here.
const NEW_COMMAND = /^\/new$/i;
export function newCommand(core, message) {
  const text = String(message ?? '').trim();
  if (core && typeof core.commandOf === 'function') return core.commandOf(text) === 'new' || NEW_COMMAND.test(text);
  return NEW_COMMAND.test(text);
}

// While a reply streams in, the line and the board are refreshed at most this often (a long
// stream is classified and drawn a bounded number of times); the end of the reply always draws.
export const STREAM_REFRESH_S = 0.25;
// the log's drawing budget per reply (50 replies at most: 150,000 elements in the worst case)
export const LOG_NODES = 3000;

// --- DOM controller --------------------------------------------------------------------------

// ctx: { ui (StageUI), core (StiraChatCore), fetch (stiraFetch), strings() -> S,
//        live (startLive controller), stage (the #stage element), reducedMotion,
//        confirm (StiraConfirm, the shell's confirmation dialog), board (board.js controller,
//        optional), openLink(url), copy(text) }
export function startTalk(ctx) {
  const { ui, core } = ctx;
  const S = () => ctx.strings();
  const t = (key, vars) => S().t(key, vars);
  const sender = core.createSender({ fetch: ctx.fetch, t });
  const el = {
    form: $('stage-form'), input: $('stage-input'), send: $('stage-send'), stop: $('stage-stop'),
    attach: $('stage-attach'), file: $('stage-image-input'), images: $('stage-images'),
    note: $('stage-note'), unsettled: $('stage-unsettled'), unsettledText: $('stage-unsettled-text'),
    resend: $('stage-resend'), discard: $('stage-discard'), asked: $('asked'), more: $('line-more'),
    logBtn: $('btn-log'), backlog: $('backlog'), list: $('backlog-list'), close: $('backlog-close'),
    gauge: $('ctx-gauge'), newBtn: $('btn-new'), newReason: $('btn-new-reason'),
    historyReload: $('stage-history-reload'),
  };
  const starter = core.createSessionStarter ? core.createSessionStarter({ fetch: ctx.fetch, t }) : null;
  const st = { ready: false, busy: false, reading: false, composing: false, attachments: [], history: [], notes: [], liveTurn: null, pin: null, more: false, error: '', sig: '', refill: null, inputGen: 0, turnOfKey: new Map(), confirming: false, stoppedTurn: null, noteSeq: 0, sendSeq: 0, reply: null, gaugeTurn: null, gaugeSig: '', session: null, starting: false, historyStale: false };
  const board = ctx.board || null;
  st.streamAt = null;
  st.streamPending = null;

  const lastTurnId = () => {
    for (let i = st.history.length - 1; i >= 0; i -= 1) if (typeof st.history[i].turn_id === 'string') return st.history[i].turn_id;
    return null;
  };

  function setAsked(message) {
    el.asked.hidden = !message;
    el.asked.textContent = message ? `${t('stage.log_you')} › ${oneLine(message)}` : '';
  }

  // The conversation takes the text window with `text` (a reply, or a note about a send).
  function hold(text, { more = false, stream = false, open = false } = {}) {
    if (!st.pin || st.pin.soft) st.pin = { key: undefined };
    st.more = more;
    if (stream) ui.stream(text, { open });
    else ui.say(text);
  }

  // The reply on screen: { key, text, mode, c } (the line, and the board when it is one).
  // live: a reply of this page's own send (it lowers the board by itself); otherwise (history on
  // load) the board waits for "Open on the board".
  // open: more text may come (a stream). The end of a reply (open = false) is classified from
  // scratch, as the log and a reload do; a board this reply raised rolls up if the final text
  // is a line (finalBoardAction).
  function showReply(reply, open, { key, live = true } = {}) {
    if (open && st.streamAt !== null && performance.now() - st.streamAt < STREAM_REFRESH_S * 1000) {
      st.streamPending = { reply, key, live };
      return;
    }
    st.streamPending = null;
    st.streamAt = open ? performance.now() : null;
    const prev = open && st.reply && st.reply.key === key ? st.reply.mode : null;
    const view = replyView(reply, prev, t);
    if (!view.say && !open) return;
    st.reply = { key, text: reply, mode: view.mode, c: view.c };
    hold(view.say, { more: view.more, stream: true, open: view.mode === 'line' && open });
    if (view.mode === 'board' && board && live) board.show({ key, text: reply, c: view.c });
    if (!open && board && finalBoardAction({ finalMode: view.mode, boardKey: board.currentKey, key, open: board.open, pinned: board.pinned }) === 'close') board.close();
  }

  function openBoard(entry) {
    if (!board || !entry) return;
    board.show({ key: entry.key, text: entry.text, c: entry.c || classify(entry.text) }, { explicit: true });
  }

  // --- composer ----------------------------------------------------------------------------
  function status() {
    const live = ctx.live ? ctx.live.status() : { offline: false, turnOpen: false, approval: false, turnId: null };
    const turnStopped = Boolean(live.turnId && live.turnId === st.stoppedTurn);
    return composerState({ ready: st.ready, busy: st.busy, reading: st.reading, unsettled: Boolean(sender.unsettled), resetUnknown: Boolean(starter && starter.pendingKey), starting: st.starting, historyStale: st.historyStale, ...live, turnStopped });
  }

  // Why "new conversation" cannot run now (catalog key) or null; fail closed without the API.
  function newBlock() {
    if (!starter || !core.newSessionBlock) return 'chat.new_blocked_loading';
    const live = ctx.live ? ctx.live.status() : { offline: false, turnOpen: false, approval: false };
    return core.newSessionBlock({ ready: st.ready, busy: st.busy, turnOpen: live.turnOpen, approval: live.approval || live.approvalsPending, unsettled: Boolean(sender.unsettled), offline: live.offline, starting: st.starting || starter.running });
  }

  function update() {
    const c = status();
    const nb = newBlock();
    const reason = st.error || (c.reason ? t(c.reason) : '');
    const boardOpen = Boolean(board && board.open);
    const sig = `${st.historyStale}|${nb}|${document.body.classList.contains('portrait')}|${c.canSend}|${c.stop}|${reason}|${Boolean(sender.unsettled) && !st.busy}|${st.more && !ui.typing}|${S().title}|${boardOpen}|${st.reply ? st.reply.mode : ''}|${Boolean(st.pin)}|${ui.typing}`;
    if (sig === st.sig) return;
    st.sig = sig;
    const hadFocus = document.activeElement === el.send || document.activeElement === el.stop;
    if (el.historyReload) el.historyReload.hidden = !st.historyStale;
    if (el.newBtn) {
      // disabled with the reason as its description (title for the pointer, the hidden text for
      // screen readers)
      el.newBtn.disabled = Boolean(nb);
      const why = nb ? t(nb) : t('chat.new_hint');
      el.newBtn.title = why;
      if (el.newReason) el.newReason.textContent = why;
    }
    el.send.hidden = c.stop;
    el.stop.hidden = !c.stop;
    el.send.disabled = !c.canSend;
    el.attach.disabled = st.busy || st.reading || !st.ready;
    el.file.disabled = el.attach.disabled;
    const narrow = document.body.classList.contains('portrait');
    el.input.placeholder = t(st.busy ? (narrow ? 'stage.log_waiting' : 'stage.placeholder_waiting') : narrow ? 'chat.message' : 'stage.placeholder');
    el.note.textContent = reason;
    el.note.dataset.kind = st.error ? 'error' : c.reason === 'stage.note_sending' ? 'info' : c.reason ? 'warn' : '';
    // a board reply whose board is closed: "Open on the board"; a cut line: "read the rest in the log"
    const boardClosed = Boolean(board && st.reply && st.reply.mode === 'board' && st.pin && !board.open);
    el.more.hidden = !((st.more || boardClosed) && !ui.typing);
    el.more.textContent = t(boardClosed ? 'stage.line_board' : 'stage.line_more');
    el.more.dataset.action = boardClosed ? 'board' : 'log';
    $('line').dataset.more = st.more || boardClosed ? '1' : '0';
    // the key is held from the start of a send; the panel is for an outcome that stayed unknown
    const u = st.busy ? null : sender.unsettled;
    el.unsettled.hidden = !u;
    if (u) {
      const imgs = u.images.length ? ` ${t('chat.unsettled_images', { count: u.images.length })}` : '';
      el.unsettledText.textContent = `${t('chat.unsettled_notice')} — ${oneLine(u.message, 60)}${imgs}`;
      // a re-send is a send: the same conditions as the send button (status().canSend)
      el.resend.disabled = !resendAllowed(u, c);
      el.discard.disabled = st.busy || Boolean(st.confirming);
    }
    if (hadFocus) (c.stop ? el.stop : el.send).focus({ preventScroll: true });
  }

  function renderImages() {
    el.images.replaceChildren();
    el.images.hidden = st.attachments.length === 0;
    st.attachments.forEach((a, i) => {
      const chip = document.createElement('span');
      chip.className = 'img-chip';
      const img = document.createElement('img');
      img.src = a.url;
      img.alt = a.name;
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'mini';
      rm.textContent = `× ${oneLine(a.name, 24)}`;
      rm.setAttribute('aria-label', t('chat.remove_name', { name: a.name }));
      rm.addEventListener('click', () => {
        if (st.busy || st.reading) return;
        st.attachments.splice(i, 1);
        renderImages();
        el.input.focus();
      });
      chip.append(img, rm);
      el.images.append(chip);
    });
  }

  async function addImages(files) {
    if (!st.ready || st.busy || st.reading || !files.length) return;
    const refused = core.imageError(st.attachments, files);
    st.error = refused ? t(refused) : '';
    st.sig = '';
    if (refused) return;
    st.reading = true;
    try {
      st.attachments.push(...(await core.readImages(files, { FileReader, t })));
      renderImages();
    } catch (err) {
      st.error = err.message;
    } finally {
      st.reading = false;
      st.sig = '';
    }
  }

  async function submit() {
    const c = status();
    const message = el.input.value.trim();
    // `/new` is the page's own command: never sent to the model (chat_core.commandOf; the
    // shared sender refuses it too). It opens the new-conversation confirmation; the text and
    // the images stay unless the new conversation started.
    if (newCommand(core, message)) {
      await startNew({ fromComposer: true });
      return;
    }
    if (!c.canSend || (!message && !st.attachments.length)) return;
    const payload = core.payloadOf(message, st.attachments);
    if (sender.blocked(payload)) {
      st.error = t('chat.unsettled_blocked');
      st.sig = '';
      el.resend.focus();
      return;
    }
    el.input.value = '';
    await deliver(message, st.attachments.slice(), payload, false);
  }

  // ADR 0014 (#451): the progress report's Continue / Redirect are ordinary messages of this
  // conversation (progress_reply names the report). -> false when it cannot send now.
  globalThis.StiraProgressSend = async (message, reply) => {
    if (!status().canSend) return false;
    const payload = JSON.stringify({ message, progress_reply: reply });
    if (sender.blocked(payload)) return false;
    await deliver(message, [], payload, true);
    return true;
  };

  // A send the server did not (visibly) record: the message and what happened, after the
  // turn that was last when it was sent. `turn` is the turn id the stream announced, if any:
  // the log matches the note to that recorded turn by id (backlog.js).
  function addNotes(anchor, turn, message, images, text) {
    st.noteSeq = (st.noteSeq || 0) + 1;
    const group = st.noteSeq;
    st.notes.push({ after: anchor, group, turn, who: 'user', text: message, images }, { after: anchor, group, turn, who: 'sys', text });
  }

  // The text put back into the composer after a stop or an unknown outcome belongs to that
  // send (its key). The marker remembers the key and the edit generation at that moment;
  // any edit of the composer (even whitespace) bumps the generation (input event).
  function refill(message, fromPanel) {
    if (fromPanel || el.input.value) return;
    el.input.value = message;
    const u = sender.unsettled;
    st.refill = u ? { key: u.key, gen: st.inputGen } : null; // nothing unsettled: nothing to clear later
  }

  async function deliver(message, images, payload, fromPanel) {
    st.busy = true;
    st.error = '';
    st.more = false;
    st.sig = '';
    setAsked(message);
    // Until the turn's own events speak (turn.started -> "turn running"), the previous line
    // stays; the first new narration takes over.
    st.pin = { key: undefined, soft: true };
    if (ctx.live) ctx.live.resay();
    const anchor = lastTurnId();
    st.sendSeq += 1;
    const replyKey = `send-${st.sendSeq}`;
    st.liveTurn = { message, reply: '', images: images.length, turnId: null };
    if (!el.backlog.hidden) renderLog();
    const result = await sender.send({ message, images, payload }, {
      delta(data) {
        st.liveTurn.reply += typeof data.text === 'string' ? data.text : '';
        showReply(st.liveTurn.reply, true, { key: replyKey });
      },
      uiEvent(data) {
        if (data && typeof data.turn_id === 'string') st.liveTurn.turnId = data.turn_id;
        if (ctx.live) ctx.live.ingestTurnEvent(data);
        // ADR 0012 phase 5b: the YOLO badge reads its status after a tool call (its log
        // line, its count); without STIRA_YOLO_ALLOW there is no badge and nothing is read.
        if (data && data.type === 'tool_call.updated' && globalThis.StiraYolo) globalThis.StiraYolo.poke();
      },
      turn(data) {
        st.gaugeTurn = data; // the last turn's usage (input + cache buckets) for the gauge
        st.history.push({ message, response: st.liveTurn.reply, turn_id: data.turn_id, image_count: images.length || undefined });
        if (st.history.length > 50) st.history.shift();
      },
    });
    const reply = st.liveTurn.reply;
    const ownTurn = st.liveTurn.turnId;
    st.liveTurn = null;
    if (ownTurn && result.key) st.turnOfKey.set(result.key, ownTurn);
    // Once the send the restored text belongs to settles (delivered, or "already received"),
    // the text is cleared, so pressing Send again does not send it once more as a new
    // message — only that send's key, and only if the composer was not edited since.
    const settle = refillOutcome(st.refill, { key: result.key, ok: result.ok, delivery: result.delivery, gen: st.inputGen });
    if (settle.clear) el.input.value = '';
    if (settle.drop) st.refill = null;
    if (result.ok) {
      if (!fromPanel) {
        st.attachments = [];
        renderImages();
      }
      if (reply) showReply(reply, false, { key: replyKey });
      else st.pin = null; // nothing to say: the narration speaks
    } else if (result.stopped) {
      // Stop only stopped this page's reading: the server is not asked to cancel and may
      // finish and record the turn. The outcome is unknown, like a cut connection: the key
      // stays (sender.unsettled), the same text re-sent from the composer or the panel goes
      // with the same key (the server deduplicates it), anything else waits for an explicit,
      // confirmed discard.
      st.stoppedTurn = ownTurn;
      addNotes(anchor, ownTurn, message, images.length, t('stage.log_stopped'));
      hold(t('stage.say_stopped'));
      refill(message, fromPanel);
    } else {
      const { delivery, completed, error } = result;
      const why = error && error.message ? error.message : t('chat.connection_failed');
      const key = delivery === 'duplicate' ? 'stage.say_send_duplicate' : delivery === 'unknown' && !completed ? 'stage.say_send_unknown' : 'stage.say_send_failed';
      // "already received": the server has (or runs) that turn. The log note is tied to the
      // turn id the stream announced for this key earlier (stop or cut after turn.started);
      // without one it is a standalone note naming the message, in time order — never
      // attached to whatever turn happens to be newest.
      // A turn cut off at the output limit was received and ran: the reason alone, not "could not send" (#363).
      const said = delivery === 'duplicate' || (error && (error.code === 'max_tokens' || error.code === 'model_failure')) ? why : `${t(key)} ${why}`;
      if (delivery === 'duplicate') {
        st.notes.push(duplicateNote(st.turnOfKey.get(result.key), anchor, message, t));
        void refreshHistory().then(() => {
          if (!el.backlog.hidden) renderLog();
        });
      } else addNotes(anchor, ownTurn, message, images.length, said);
      if (completed && reply) showReply(reply, false, { key: replyKey });
      else hold(said);
      if (delivery !== 'duplicate') refill(message, fromPanel);
    }
    st.busy = false;
    st.sig = '';
    update();
    if (!el.backlog.hidden) renderLog();
    el.input.focus({ preventScroll: true });
    if (globalThis.StiraProgress) globalThis.StiraProgress.poke(); // a report may have come
  }

  // --- backlog -------------------------------------------------------------------------------
  function renderLog() {
    const entries = buildEntries(st.history, st.notes, st.liveTurn);
    // a conversation started with /new begins with its boundary line
    const note = core.sessionNote ? core.sessionNote(st.session, t, document.documentElement?.lang) : null;
    const boundary = () => {
      const li = document.createElement('li');
      li.className = 'log-item sys session-boundary';
      li.textContent = note;
      return li;
    };
    if (!entries.length) {
      const empty = document.createElement('li');
      empty.className = 'log-item sys';
      empty.textContent = t('stage.log_empty');
      el.list.replaceChildren(...(note ? [boundary()] : []), empty);
      return;
    }
    const strings = S();
    renderEntries(document, el.list, entries, strings, {
      // replies: the same sanitised Markdown renderer as the board; a board reply can be opened
      // parse() keeps the first 64 KiB in linear time; each reply draws at most LOG_NODES elements
      renderReply: (text) => renderBlocks(document, parse(text), { t, openLink: ctx.openLink, copy: ctx.copy, maxNodes: LOG_NODES }),
      // the board copies the whole reply: the recorded response, not the log's cut copy
      boardOf: board ? (e) => {
        if (classify(e.text).mode !== 'board') return null;
        const rec = e.turn ? st.history.find((h) => h && h.turn_id === e.turn) : null;
        const full = rec && typeof rec.response === 'string' ? rec.response : e.text;
        return { key: e.turn ? `turn-${e.turn}` : `log-${e.text.length}`, text: full };
      } : null,
      openBoard: (entry) => {
        setLog(false);
        openBoard(entry);
      },
    });
    if (note) el.list.prepend(boundary());
  }

  async function refreshHistory() {
    try {
      if (!core.fetchConversation) {
        st.history = await core.fetchHistory(ctx.fetch);
        return true;
      }
      const { turns, session } = await core.fetchConversation(ctx.fetch);
      // another page started a new conversation: the notes belong to the old one
      if (session && st.session && session.id !== st.session.id) {
        st.notes = [];
        st.turnOfKey.clear();
      }
      st.history = turns;
      if (session) st.session = session;
      st.historyStale = false; // the server's conversation is what the page holds now
      return true;
    } catch {
      return false;
    }
  }

  function setLog(open, { scrollEnd = true } = {}) {
    el.backlog.hidden = !open;
    ctx.stage.classList.toggle('log-open', open);
    el.logBtn.setAttribute('aria-expanded', String(open));
    if (!open) {
      el.input.focus({ preventScroll: true });
      return;
    }
    renderLog();
    if (scrollEnd) el.list.scrollTop = el.list.scrollHeight;
    el.list.focus({ preventScroll: true });
    // the server's history may have turns from other pages (chat): re-read it
    if (!st.busy) {
      void refreshHistory().then((ok) => {
        if (ok && !el.backlog.hidden) {
          renderLog();
          if (scrollEnd) el.list.scrollTop = el.list.scrollHeight;
        }
      });
    }
  }

  // --- new conversation (#350 phase 3) -----------------------------------------------------
  // The page forgets the old conversation only after the server reset it (or said it already
  // had, or the history's session id shows it did).
  // After a reset (this request's, or this key's earlier one) the page shows the server's
  // conversation as it is now (re-read), never one it only assumes is empty.
  async function showServerConversation(result) {
    st.notes = [];
    st.turnOfKey.clear();
    st.reply = null;
    st.more = false;
    st.refill = null;
    st.stoppedTurn = null;
    st.gaugeSig = '';
    if (!(await refreshHistory())) {
      // the old conversation is gone on the server; nothing is sent until a re-read works
      st.history = [];
      st.historyStale = true;
    }
    const last = st.history[st.history.length - 1];
    st.gaugeTurn = last || null;
    setAsked(null);
    if (board && board.open && !board.pinned) board.close();
    st.pin = null;
    const key = st.historyStale ? 'chat.new_history_failed' : result.outcome === 'already' ? (result.session && result.session.current ? 'chat.new_already' : 'chat.new_already_superseded') : 'chat.new_started';
    hold(t(key));
  }

  async function startNew({ fromComposer = false } = {}) {
    const blocked = newBlock();
    if (blocked) {
      st.error = t(blocked);
      st.sig = '';
      return;
    }
    st.confirming = true;
    st.sig = '';
    let ok = false;
    try {
      ok = await core.confirmNewSession(ctx.confirm, t);
    } finally {
      st.confirming = false;
      st.sig = '';
    }
    if (!ok) {
      st.error = t('chat.new_cancelled');
      return;
    }
    // the state may have changed while the dialog was open (a send from another page, an approval)
    const again = newBlock();
    if (again) {
      st.error = t(again);
      return;
    }
    st.starting = true;
    st.error = '';
    st.sig = '';
    let result;
    try {
      result = await starter.start();
      if (!result.ok && result.outcome === 'unknown' && starter.resolve && core.fetchConversation) {
        // only this key's digest in the history resolves it (not another tab's reset)
        try {
          result = (await starter.resolve(await core.fetchConversation(ctx.fetch))) || result;
        } catch {
          /* stays unknown */
        }
      }
      // Sending stays off until the server's conversation is on screen (composerState
      // `starting`): a reply arriving before the re-read history would be overwritten.
      if (result.ok) await showServerConversation(result);
    } finally {
      st.starting = false;
      st.sig = '';
    }
    if (result.ok) {
      if (fromComposer && newCommand(core, el.input.value)) el.input.value = '';
      st.error = '';
      if (!el.backlog.hidden) renderLog();
    } else {
      st.error = result.outcome === 'unknown' ? `${t('chat.new_unknown')} ${result.error ? result.error.message : ''}`.trim() : result.error.message;
    }
    st.sig = '';
    update();
  }

  // --- wiring --------------------------------------------------------------------------------
  el.form.addEventListener('submit', (e) => {
    e.preventDefault();
    void submit();
  });
  el.stop.addEventListener('click', () => {
    if (sender.stop()) {
      st.error = '';
      el.note.textContent = t('stage.note_stopping');
    }
  });
  el.input.addEventListener('compositionstart', () => {
    st.composing = true;
  });
  el.input.addEventListener('compositionend', () => {
    st.composing = false;
  });
  el.input.addEventListener('keydown', (e) => {
    const action = keyAction(e, { composing: st.composing, empty: !el.input.value, isSendKey: core.isSendKey });
    if (action === 'send') {
      e.preventDefault();
      if (status().canSend) el.form.requestSubmit();
    } else if (action === 'log') {
      e.preventDefault();
      setLog(true);
    }
  });
  el.input.addEventListener('input', () => {
    st.inputGen += 1; // any edit, whitespace included: a restored text is now the person's draft
    if (st.error) {
      st.error = '';
      st.sig = '';
    }
  });
  el.input.addEventListener('paste', (e) => {
    const files = Array.from(e.clipboardData?.items || []).filter((i) => i.kind === 'file').map((i) => i.getAsFile()).filter(Boolean);
    if (files.length) {
      e.preventDefault();
      void addImages(files);
    }
  });
  el.attach.addEventListener('click', () => el.file.click());
  el.file.addEventListener('change', async (e) => {
    await addImages(Array.from(e.target.files || []));
    e.target.value = '';
  });
  el.resend.addEventListener('click', () => {
    const u = sender.unsettled;
    // the same rule as a send (loading, offline, approval pending, another turn running,
    // images still being read: not now); the key is the unsettled one
    if (!resendAllowed(u, status())) return;
    void deliver(u.message, u.images, u.body, true);
  });
  el.discard.addEventListener('click', async () => {
    const u = sender.unsettled;
    if (!u || st.busy || st.confirming) return;
    st.confirming = true;
    st.sig = '';
    let ok = false;
    try {
      ok = await confirmDiscard(ctx.confirm, u, t);
    } finally {
      st.confirming = false;
      st.sig = '';
    }
    // only a confirmed discard drops the key; the next send is a new message (new key)
    if (!ok || sender.unsettled !== u || st.busy) return;
    sender.discard();
    st.refill = null; // the restored text is now an ordinary draft: never cleared for it
    st.turnOfKey.delete(u.key);
    st.error = t('chat.unsettled_discarded');
    st.sig = '';
    el.input.focus();
  });
  el.logBtn.addEventListener('click', () => setLog(el.backlog.hidden));
  if (el.newBtn) el.newBtn.addEventListener('click', () => void startNew());
  if (el.historyReload) {
    el.historyReload.addEventListener('click', async () => {
      el.historyReload.disabled = true;
      try {
        if (await refreshHistory()) {
          st.error = '';
          hold(t('chat.new_started'));
          if (!el.backlog.hidden) renderLog();
        } else st.error = t('chat.new_history_failed');
      } finally {
        el.historyReload.disabled = false;
        st.sig = '';
      }
    });
  }
  el.close.addEventListener('click', () => setLog(false));
  el.more.addEventListener('click', () => {
    if (el.more.dataset.action === 'board' && st.reply) openBoard(st.reply);
    else setLog(true);
  });
  $('line').addEventListener('click', () => ui.finishLine());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !el.backlog.hidden) {
      e.preventDefault();
      setLog(false);
    }
  });

  // Start: the recorded conversation; the last reply is the line on screen.
  void refreshHistory().then(() => {
    st.ready = true;
    st.sig = '';
    const last = st.history[st.history.length - 1];
    if (last && !st.gaugeTurn) st.gaugeTurn = last;
    if (last && !st.busy) {
      setAsked(last.message);
      const key = typeof last.turn_id === 'string' ? `turn-${last.turn_id}` : 'history-last';
      const view = replyView(last.response, null, t);
      if (view.say) {
        st.reply = { key, text: String(last.response || ''), mode: view.mode, c: view.c };
        st.pin = { key: undefined };
        st.more = view.more;
        ui.say(view.say);
        if (ctx.reducedMotion) ui.finishLine();
      }
    }
  });

  // The context gauge (chat_core.contextGauge): the last turn's input (uncached + cache read +
  // cache write, per provider) against the context window when the status knows it; otherwise
  // the token count only. Read-only: from the turn event / history and /api/gateway/status.
  function renderGauge() {
    if (!el.gauge || !core.contextGauge) return;
    const status = ctx.live && ctx.live.gatewayStatus ? ctx.live.gatewayStatus() : null;
    const g = core.contextGauge({ turn: st.gaugeTurn, ...core.contextOf(status) });
    const sig = `${g ? `${g.tokens}|${g.ratio}|${g.level}|${g.near}` : '-'}|${S().title}`;
    if (sig === st.gaugeSig) return;
    st.gaugeSig = sig;
    el.gauge.hidden = !g;
    if (!g) return;
    const view = core.gaugeText(g, t);
    el.gauge.dataset.level = g.level;
    el.gauge.querySelector('.ctx-text').textContent = view.text;
    el.gauge.title = view.title;
    const bar = el.gauge.querySelector('.ctx-bar');
    if (bar) {
      bar.hidden = g.ratio === null;
      bar.style.setProperty('--ctx', String(Math.min(1, g.ratio || 0)));
    }
  }

  return {
    // every frame
    tick() {
      // a throttled stream refresh that is due
      if (st.streamPending && performance.now() - (st.streamAt || 0) >= STREAM_REFRESH_S * 1000) {
        const p = st.streamPending;
        st.streamAt = null;
        if (st.liveTurn) showReply(p.reply, true, { key: p.key, live: p.live });
        else st.streamPending = null;
      }
      update();
      renderGauge();
    },
    // the board was closed (×, Esc, swipe): the line offers to open it again
    boardClosed() {
      st.sig = '';
    },
    // live_ui asks before the narration replaces the line
    yieldTo(key) {
      const wins = narrationWins(st.pin, key, st.busy);
      if (wins && st.pin) {
        st.pin = null;
        st.more = false;
        st.sig = '';
      }
      return wins;
    },
    relabel() {
      st.sig = '';
      if (!el.backlog.hidden) renderLog();
    },
    // ADR 0012 phase 5b: one automatic-approval line (yolo_ui.js) as a Stage note, after
    // the turn it belongs to (the one in flight, once recorded; else the last one).
    systemLine(text) {
      if (typeof text !== 'string' || !text) return;
      // anchored now (the line is live): the turn in flight once recorded, else the last one
      const turn = st.liveTurn && typeof st.liveTurn.turnId === 'string' ? st.liveTurn.turnId : undefined;
      const anchor = lastTurnId();
      st.notes.push({ after: anchor, turn, who: 'sys', text });
      if (st.notes.length > 200) st.notes.shift();
      if (!el.backlog.hidden) renderLog();
    },
    get logOpen() {
      return !el.backlog.hidden;
    },
    // test/capture hook (?log=1)
    openLog: () => setLog(true),
  };
}

// Backlog (#350): the conversation log of the Stage. Entries come from GET /api/chat/history
// (the recorded turns: the person's message and the reply text) plus what this page sent
// that the server did not record (stopped, failed, in flight).
//
// What is shown: the person's own message and the reply text only — the same text the chat
// page shows. Tool input/output, file contents, approval arguments, memory and observation
// events are never read here (docs/stage.md "Conversation"). Rendering is createElement +
// textContent only: a reply containing <script> or on* attributes is shown as plain text.

export const MAX_TEXT = 64 * 1024; // longer texts are cut (the chat page shows them whole)

const clipText = (v) => {
  const s = typeof v === 'string' ? v : '';
  return s.length > MAX_TEXT ? { text: s.slice(0, MAX_TEXT), cut: true } : { text: s, cut: false };
};

// turns: /api/chat/history `turns`; notes: [{ after: turn_id | null, who: 'user' | 'sys', text,
// images, group, turn }] (local entries, placed after the turn that was newest when they
// happened; one send's message and its note share `group`; `turn` is the turn id the stream
// announced for that send, if any); live: { message, reply } of the turn in flight, or null.
export function buildEntries(turns, notes = [], live = null) {
  const out = [];
  const list = Array.isArray(turns) ? turns.filter((x) => x && typeof x === 'object') : [];
  const known = new Set(list.map((x) => x.turn_id).filter((id) => typeof id === 'string'));
  // A send stopped (or cut) here may still have been finished and recorded by the server.
  // Then the recorded turn is the message: the note's own copy is dropped and its notes
  // follow that turn. The match is by the turn id the stream announced; only a send that
  // never got an id falls back to the turn right after its anchor with the same message and
  // the same image count, and a recorded turn is matched once.
  const placed = notes.filter(Boolean).map((n) => ({ ...n }));
  const index = new Map(list.map((x, i) => [x.turn_id, i]));
  const imagesOf = (x) => (Number.isInteger(x.image_count) && x.image_count > 0 ? x.image_count : 0);
  const claimed = new Set(placed.map((n) => n.turn).filter((id) => typeof id === 'string' && known.has(id)));
  for (const n of placed) {
    if (n.who !== 'user') continue;
    let target = null;
    if (typeof n.turn === 'string') {
      if (known.has(n.turn)) target = n.turn; // not recorded (yet): the note stays as it is
    } else {
      const at = n.after ? index.get(n.after) : -1;
      const next = at === undefined ? null : list[at + 1];
      if (next && typeof next.turn_id === 'string' && !claimed.has(next.turn_id)
        && next.message === n.text && imagesOf(next) === (n.images || 0)) {
        target = next.turn_id;
        claimed.add(target);
      }
    }
    if (!target) continue;
    n.drop = true;
    for (const m of placed) {
      const sibling = n.group !== undefined ? m.group === n.group : m.after === n.after && m.group === undefined;
      if (m !== n && m.who === 'sys' && sibling && !m.moved) Object.assign(m, { after: target, moved: true });
    }
  }
  // A note that only names a turn (a re-send refused as a duplicate) follows that turn by id.
  for (const n of placed) {
    if (n.who === 'sys' && n.group === undefined && typeof n.turn === 'string' && known.has(n.turn) && !n.moved) n.after = n.turn;
  }
  const kept = placed.filter((n) => !n.drop);
  const pending = kept.filter((n) => !(n.after && known.has(n.after)));
  for (const n of pending) out.push(note(n)); // anchor gone or none: before the window
  for (const turn of list) {
    const msg = clipText(turn.message);
    const images = Number.isInteger(turn.image_count) && turn.image_count > 0 ? turn.image_count : 0;
    out.push({ who: 'user', text: msg.text, images, cut: msg.cut });
    const reply = clipText(turn.response);
    out.push({ who: 'agent', text: reply.text, cut: reply.cut, turn: typeof turn.turn_id === 'string' ? turn.turn_id : null });
    for (const n of kept) if (n.after && n.after === turn.turn_id) out.push(note(n));
  }
  if (live) {
    out.push({ who: 'user', text: clipText(live.message).text, images: live.images || 0, cut: false });
    out.push({ who: 'agent', text: clipText(live.reply).text, cut: false, pending: true });
  }
  return out;
}

function note(n) {
  return { who: n.who === 'user' ? 'user' : 'sys', text: clipText(n.text).text, cut: false, images: n.images || 0 };
}

// S: translated strings (S.t). Returns the list items (built with doc.createElement only).
// opts (#350 phase 2): { renderReply(text) -> nodes (the board's sanitised Markdown renderer,
// md_render.js), boardOf(entry) -> { key, text } | null (a reply that is a board),
// openBoard(entry) }. Without renderReply a reply is one text paragraph (phase 1).
export function renderEntries(doc, list, entries, S, opts = {}) {
  const el = (tag, cls, text) => {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const who = { user: S.t('stage.log_you'), agent: S.name, sys: S.t('stage.log_stage') };
  const items = entries.map((e) => {
    const li = el('li', `log-item ${e.who}`);
    li.append(el('span', 'log-who', who[e.who] || ''));
    if (e.who === 'agent' && e.text && typeof opts.renderReply === 'function') {
      const body = el('div', 'log-text md');
      body.append(...opts.renderReply(e.text));
      li.append(body);
      const target = typeof opts.boardOf === 'function' ? opts.boardOf(e) : null;
      if (target && typeof opts.openBoard === 'function') {
        const btn = el('button', 'mini log-board', S.t('stage.log_open_board'));
        btn.type = 'button';
        btn.addEventListener('click', () => opts.openBoard(target));
        li.append(btn);
      }
    } else li.append(el('p', 'log-text', e.text || (e.pending ? S.t('stage.log_waiting') : '')));
    if (e.images) li.append(el('p', 'log-meta', S.t('chat.images_not_retained', { count: e.images })));
    if (e.cut) li.append(el('p', 'log-meta', S.t('stage.log_cut')));
    return li;
  });
  list.replaceChildren(...items);
  return items;
}

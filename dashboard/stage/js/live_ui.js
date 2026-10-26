// Live mode controller (#341, stage 2): feed -> model, narration, approval choice window.
// DOM side only; the mapping rules live in adapter.js / approval.js (tested in Node).

import { narrate } from './adapter.js';
import { ApprovalClient, allowClassText, allowReasonText, approvalChoices, remainingText, whyText } from './approval.js';
import { LiveFeed, schedule } from './live.js';

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

function pretty(raw) {
  if (typeof raw !== 'string' || !raw) return '{}';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

async function getJSON(url) {
  // Same origin only: relative URL, no credentials beyond the page's own, no redirects out.
  const r = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' }, redirect: 'error' });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

let csrf = '';
async function token() {
  if (csrf) return csrf;
  const meta = document.querySelector('meta[name="csrf-token"]');
  csrf = (meta && meta.content) || '';
  if (!csrf) csrf = (await getJSON('/api/csrf')).token || '';
  return csrf;
}

// ctx: { ui, strings() -> S, dispatch(ev), state() -> model state, note(line), clock(),
//        talk?: { yieldTo(sayKey) -> bool } (#350: the conversation may hold the text window) }
// #367: which classifier drives Neural State and how many turns moved it, for the raw view,
// from the last /api/gateway/status (the chat page's own wording, chat_core.js). Nothing
// before the first status, or when chat_core is not loaded.
export function emotionRows(status, S, core = globalThis.StiraChatCore) {
  if (!status || !core || typeof core.classifierText !== 'function') return [];
  const classifier = core.classifierText(status.classifier, S.t);
  const updates = core.emotionUpdatesText(status.emotion_updates, S.t);
  return [
    [S.t('chat.stimulus_classifier'), classifier.text],
    [S.t('chat.emotion_updates'), updates.text],
  ];
}

export function startLive(ctx) {
  const feed = new LiveFeed({
    getJSON,
    clock: ctx.clock,
    wall: () => Date.now() / 1000,
    onNote: (n) => ctx.note(n.key ? ctx.strings().t(n.key, n.vars) : n.text),
  });
  const client = new ApprovalClient({
    post: (url, body, headers) => fetch(url, { method: 'POST', body, headers, redirect: 'error' }),
    token,
    // ADR 0012 §8b revision 6 (only with STIRA_YOLO_ALLOW): the browser confirmation.
    hostConfirm: (operation) => (window.StiraHostConfirm
      ? window.StiraHostConfirm.browser(operation) : Promise.resolve({ code: 'host_channel_unavailable' })),
  });
  // seen: ids whose masked arguments were shown here (approve needs it); open: panel shown
  const view = { seen: new Set(), open: null, busy: false, error: '', choiceSig: '', sayKey: '', notice: null };
  const conn = $('conn');
  schedule(feed, document);

  function expired(rec) {
    return Boolean(rec && rec.expiresAt !== null && rec.expiresAt <= feed.serverNow());
  }

  function renderDetail(rec) {
    const box = $('approval-detail');
    const S = ctx.strings();
    if (!rec || view.open !== rec.id) {
      box.hidden = true;
      view.detailSig = '';
      return;
    }
    const left = rec.expiresAt === null ? null : rec.expiresAt - feed.serverNow();
    const sig = `${rec.id}|${left === null ? '' : Math.ceil(left)}|${view.error}|${S.title}`;
    if (sig === view.detailSig) return;
    view.detailSig = sig;
    box.hidden = false;
    $('approval-detail-title').textContent = S.t('approvals.card_title', { tool: rec.tool });
    const dl = $('approval-facts');
    dl.replaceChildren();
    const rows = [
      [S.t('approvals.why'), whyText(rec, S.t) || S.t('approvals.none')],
      [S.t('approvals.expires'), left === null ? S.t('approvals.none') : remainingText(left, S.t)],
      [S.t('approvals.request_id'), rec.id.slice(0, 8)],
      [S.t('approvals.args_hash'), rec.argsHash ? rec.argsHash.slice(0, 12) : S.t('approvals.none')],
    ];
    // ADR 0025: what "allow again" would cover, or why this card offers only approve once.
    const allowWhy = allowReasonText(rec, S.t);
    if (allowWhy) rows.push([S.t('approvals.allow_heading'), allowWhy]);
    else if (rec.allowAgain && rec.allowAgain.offered && choicesOffer(rec)) rows.push([S.t('approvals.allow_heading'), allowClassText(rec, S.t)]);
    for (const [k, v] of rows) {
      const row = el('div');
      row.append(el('dt', null, k), el('dd', null, v));
      dl.append(row);
    }
    const args = pretty(rec.args);
    if ($('approval-args').textContent !== args) $('approval-args').textContent = args;
    $('approval-args').setAttribute('aria-label', S.t('approvals.normalized_args'));
    $('approval-error').textContent = view.error || S.t('stage.detail_masked');
    $('approval-error').classList.toggle('bad', Boolean(view.error));
  }

  function choicesOffer(rec) {
    return approvalChoices(rec, { argsSeen: true, expired: false, busy: false }).some((c) => c.id === 'allow_session');
  }

  function renderChoices(rec) {
    const S = ctx.strings();
    const choices = approvalChoices(rec, { argsSeen: Boolean(rec && view.seen.has(rec.id)), argsOpen: Boolean(rec && view.open === rec.id), expired: expired(rec), busy: view.busy });
    const sig = `${rec ? rec.id : ''}|${choices.map((c) => `${c.key}${c.disabled ? '-' : '+'}`).join(',')}|${rec && rec.allowAgain ? rec.allowAgain.token || '' : ''}|${S.title}`;
    if (sig === view.choiceSig) return;
    const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest('#choices') ? document.activeElement.dataset.id : null;
    view.choiceSig = sig;
    if (!choices.length) {
      ctx.ui.hideChoices();
      return;
    }
    ctx.ui.showChoices(choices.map((c) => ({ id: c.id, label: S.t(c.key), disabled: c.disabled })));
    const again = focused && document.querySelector(`#choices [data-id="${focused}"]`);
    if (again) again.focus({ preventScroll: true });
    // A new request takes the keyboard focus only if nothing else has it (Tab on the page
    // body toggles the raw view, so the window must be reachable without it).
    else if (rec && rec.id !== view.focusedFor && (!document.activeElement || document.activeElement === document.body)) {
      ctx.ui.focusChoice();
    }
    if (rec) view.focusedFor = rec.id;
  }

  async function onChoice(c) {
    const rec = feed.approval;
    if (!rec) return;
    if (c.id === 'args') {
      // Once shown, approving stays possible for this request even if the panel is closed.
      view.open = view.open === rec.id ? null : rec.id;
      view.seen.add(rec.id);
      view.error = '';
      return;
    }
    if (c.id === 'page') {
      window.location.assign('/approvals');
      return;
    }
    const allowing = c.id === 'allow_session' || c.id === 'allow_always';
    if (c.id !== 'approve' && c.id !== 'reject' && !allowing) return;
    view.busy = true;
    view.error = '';
    const result = await client.decide(rec, c.id, { argsSeen: view.seen.has(rec.id), expired: expired(rec) });
    view.busy = false;
    if (result.ok) {
      view.open = null;
      view.notice = { key: c.id === 'reject' ? 'stage.say_decided_reject' : 'stage.say_decided_approve', vars: { tool: rec.tool }, until: ctx.clock() + 4 };
      ctx.note(`decision ${c.id} ${rec.id.slice(0, 8)} → 202 accepted`);
      feed.markDecided(rec.id, c.id === 'reject' ? 'reject' : 'approve');
      if (allowing) allowResult(rec, c.id, result.allow);
    } else if (!result.busy) {
      view.error = result.error || ctx.strings().t(result.errorKey, result.errorVars);
      view.open = rec.id; // show the panel with the error
      view.seen.add(rec.id);
      ctx.note(`decision ${c.id} ${rec.id.slice(0, 8)} → refused (${result.code || result.errorKey})`);
    }
  }

  // ADR 0025: the press's result line (the badge script logs it and re-reads its list);
  // Make Always opens the shared dialog (allow_ui.js) once the call has run.
  function allowResult(rec, action, result) {
    const S = ctx.strings();
    const status = result && typeof result.status === 'string' ? result.status : '';
    const text = action === 'allow_session'
      ? (status === 'allowed' ? S.t('approvals.allow_result_allowed', { what: allowClassText(rec, S.t) }) : S.t('approvals.allow_result_once'))
      : S.t('approvals.allow_result_always_pending', { tool: rec.tool });
    try {
      document.dispatchEvent(new CustomEvent('stira:allow-result', { detail: { text, action, request_id: rec.id, tool: rec.tool } }));
    } catch {
      ctx.note(text);
    }
    const shared = window.StiraAllow;
    if (action === 'allow_always' && shared && typeof shared.makeAlways === 'function') shared.makeAlways({ requestId: rec.id, tool: rec.tool });
  }

  return {
    onChoice,
    // every frame
    tick(now) {
      for (const ev of feed.drain(now)) ctx.dispatch(ev);
      const rec = feed.approval && ctx.state().approval ? feed.approval : null;
      if (view.open && (!rec || view.open !== rec.id)) view.open = null;
      renderDetail(rec);
      renderChoices(rec);
      const S = ctx.strings();
      const say = view.notice && view.notice.until > now ? view.notice : narrate(ctx.state(), { offline: feed.offline, pendingCount: feed.pendingCount, breakerRaw: feed.breakerRaw, memoryWrite: feed.memoryWrite, generatingSec: feed.generatingSec() });
      const sayKey = `${say.key}|${JSON.stringify(say.vars || {})}|${S.title}`;
      // A reply in the text window stays until the narration has something new to say
      // (talk.yieldTo decides; docs/stage.md "Conversation").
      if (sayKey !== view.sayKey && (!ctx.talk || ctx.talk.yieldTo(say.key))) {
        view.sayKey = sayKey;
        ctx.ui.say(S.t(say.key, say.vars));
      }
      const state = feed.offline ? 'offline' : 'live';
      conn.dataset.state = state;
      conn.textContent = state === 'live' ? S.conn_live : S.conn_offline;
    },
    rawRows() {
      const S = ctx.strings();
      const t = feed.turn;
      return [
        [S.mode, S.mode_live],
        [S.turn, t ? `${t.id.slice(0, 8)} #${t.seq} ${t.done ? S.turn_done : S.turn_open}` : S.none],
        ...emotionRows(feed.lastStatus, S),
        ...feed.info().map((s) => [s.url, `${s.state}${s.age === null ? '' : ` · ${s.age.toFixed(1)}s`}`]),
      ];
    },
    sources: () => feed.info(),
    // #350: the last /api/gateway/status snapshot as fetched (read only; the context gauge
    // reads provider / context_management from it)
    gatewayStatus: () => feed.lastStatus,
    // #350: the chat stream's own turn events, shown at once (deduplicated with the poll).
    ingestTurnEvent: (ev) => feed.ingestTurnEvent(ev),
    // What the composer needs to know: may a message be sent now, and if not, why.
    status: () => ({
      offline: feed.offline,
      turnOpen: feed.turnOpen,
      turnId: feed.turn ? feed.turn.id : null,
      approval: Boolean(feed.approval && ctx.state().approval),
      // any request in the queue, shown or not (#350: "new conversation" waits for all of them)
      approvalsPending: feed.pendingCount > 0 || Boolean(feed.approval),
    }),
    // The narration speaks its current state again (after the conversation let go of the window).
    resay() {
      view.sayKey = '';
    },
    relabel() {
      view.choiceSig = '';
      view.sayKey = '';
      view.detailSig = '';
    },
  };
}

# ADR 0014: Progress reports — stop at a checkpoint, report on the board, let the person continue, redirect or stop

**Path:** `docs/adr/0014-progress-report-phase.md`
**Status:** Accepted (revision 2, 2026-10-19; phase 1 implemented, #451). Revision 1 was
Proposed; the operator answered its nine questions on 2026-10-19 (21:20, 21:56, 22:17) — the
recommendations, except question 4 (see [Operational decisions](#operational-decisions-2026-10-19)).
Accepted because every open point now has an answer.
**Issue:** #451. Related: #365 / [ADR 0012](0012-yolo-mode.md) (YOLO window, state machine,
stop, audit), [ADR 0008](0008-scheduler.md) (scheduled jobs call no model), [ADR 0013](0013-audit-event-contract.md)
(audit vs observability), #299 / [ADR 0011](0011-agent-self-scheduling.md) (goal loop), #446
(activity board), #452 (turn tied to the SSE stream), #366 (line vs board), #254 (prompt-cache
prefix), #448 (tool observation in a window), #445 (long model calls), #239 (notifications),
#456 (agent feedback: work journal), #454 (artifacts).
**Decision (revision 1, as proposed; changed by the operational decisions below):** when the feature is switched on (off by default), a long human chat /
Stage turn is brought to a **checkpoint** every 30 minutes (configurable) and when the agent
itself declares a milestone. At a checkpoint the harness **ends the turn cleanly** — it never
holds a running turn open — and puts a structured report on the board: what was done, the
artifacts (paths only), what is open, the usage, the YOLO window's remaining time and counts,
and the agent's own short note. The person answers with **Continue**, **Redirect (one line)**
or **Stop**; Continue and Redirect start the next turn as an ordinary human message, Stop ends
the YOLO window normally. **Without an answer nothing runs** (the safe default); an opt-in
countdown in the open page can send "continue" by itself. The YOLO state machine, the gate
stack, the tool list, the static prompt (in phase 1) and `handoff_to_user` / `stop` are
unchanged.

## Operational decisions (2026-10-19)

**English.** The operator took the recommendations of questions 1–3 and 5–9 — (T) end the turn,
the model writes the note with a facts-only fallback, 30 minutes (at least 10), Stop ends the
work **and** the YOLO window, the declaration reuses `handoff_to_user(breakout_of_loop: true)`
(phase 2+), turn events only, irreversible operations are not a trigger, off by default with a
tick in the YOLO start dialog (question 9 (b)) — and **changed question 4: without an answer
for 10 minutes (configurable) the agent goes on working.** Safety conditions (proposed by the
assistant, approved by the operator), all built in phase 1:

1. An automatic continue happens **only while the YOLO window is `active`** (not paused, not
   ended; the same window the report was made in).
2. Only when the report's turn was a **chat / Stage turn a person started** (or a continuation
   of one). The continuation is "the continuation of a turn a person started": it carries the
   human-turn mark, and the window, the cap, the sub-caps, the breaker, Stop / `/new` and the
   latch apply to it unchanged (nothing about it is looser than the person's own turn).
3. **At most one automatic continue per report.**
4. **After N automatic continues in a row without a person's answer (default 3, configurable),
   it stops and waits** — the run never reaches the window's end with nobody there.
5. The report's Stop, the chat Stop of a continuation turn, the window's end and `/new` stop it
   at any time.
6. The only model call made for it is the continuation turn itself: the countdown and the
   checks call no model and use no tokens.

This is a timer that starts a model call — the thing §4 (c) set aside for #299. It is accepted
here for YOLO windows only, as the continuation of a person's own turn under conditions 1–6,
not as a scheduler job (ADR 0008 is unchanged: no scheduled job calls a model). Cost: on the
convenience side a report no longer blocks an unattended run for more than `continue_after_min`;
on the safety side the agent may run up to N × `interval_min` (default 90 minutes) without a
person, still inside the window's limits — the bound of condition 4.

**日本語.** 指揮官は質問 1〜3、5〜9 を推奨どおりに決め、**質問 4 だけを変更した: 10 分（設定可）返答が
なければ AI は作業を続ける。** 安全条件（提案・承認済み）: ①自動継続は YOLO の窓が `active` の間だけ
②直前のターンが人間の開始したチャット / Stage のターン（またはその継続）だったときだけ。継続ターンは
『人間が開始したターンの継続』として扱い、人間のターンの印・窓・上限・内訳の上限・ブレーカー・停止 / `/new`・
ラッチの規則はそのまま ③報告 1 回につき自動継続は 1 回だけ ④人間の返答なしに連続 N 回（既定 3、設定可）
自動継続したら止めて待つ ⑤報告の『ここで止める』・継続ターンのチャットの停止・窓の終了・`/new` でいつでも
止まる ⑥モデルを呼ぶのは継続ターンそのものだけ（カウントダウンや判定はトークンを使わない）。ADR 0008
（スケジュールジョブはモデルを呼ばない）は変えない。これはジョブではなく、YOLO の窓の中で人間のターンを
続けるもの。

### Phase 1 as built (what differs from revision 1)

* **YOLO windows only** (the operator's phase-1 scope): a checkpoint needs an open window
  (`active` or `paused`) whose start dialog had the tick, and a human chat / Stage turn
  (`yolo.origin.covered`). Turns outside a window are not covered in phase 1 (§8 (a) later).
* **Redirect is in phase 1** (with Continue and Stop). An ordinary message answers too
  (`message`). A reply that names a report which is no longer pending (a stale page) is an
  ordinary message, not a 409 — nothing is lost and nothing runs twice.
* **Hard end** uses the SDK's `AfterToolsEvent.end_turn` with the line `[checkpoint]`, not
  `stop_event_loop` (measured below: the latter leaves two user messages in a row).
* **Settings**: the `progress` section of `gateway-config.json` **and** four variables
  (`STIRA_PROGRESS_REPORTS`, `_INTERVAL_MIN`, `_CONTINUE_AFTER_MIN`, `_MAX_AUTO_CONTINUES`),
  environment > config > default; the settings page does not edit it yet (the dialog's tick is
  the UI). Default off.
* **Answers** (`POST /api/chat` with `progress_reply`, `POST /api/progress/<id>/stop`, and
  `POST /api/progress/<id>/hold` — "don't continue by itself", which leaves the report pending)
  and `GET /api/progress/current`. Stop is never refused: it cancels the continue first, then
  ends the window, then stops a continuation turn that already runs, and records the answer
  last (best effort).
* **Records**: `progress.report`, `progress.reply` and `progress.auto_continue` turn events
  (codes, counts, ids; the note is the turn's own text). The continuation turn's
  `turn.started` names its report (`auto_continue`), its history entry has `auto_continue: true`.
* **Board**: a report panel of its own (chat page above the composer; Stage top right) rather
  than the Stage choice window, so an approval card's choices are never mixed with it.
* A person deciding an approval card is human contact (the interval restarts), as in §2.
* **The dialog's tick belongs to its own press** (PR #489 review): it is bound to the window
  that press would open; every press replaces the last one's choice (a press without the
  field = the settings' default), so the tick of a press that was cancelled, denied or expired
  never reaches a later window. **日本語:** 開始ダイアログのチェックはその押下が開く窓にだけ効く。
  キャンセル・拒否・期限切れになった押下のチェックは次の窓に持ち越さない。
* **Breaker and latch** (PR #489 review, measured): at the due time the automatic continue
  also needs the breaker YOLO samples to be NORMAL and nothing latched (read directly, besides
  the window being `active` — the window pauses on a non-NORMAL sample too). If any of these
  fails the continue is **dropped, not kept**: the report waits for a person, also after
  NORMAL is back. The existing breaker does not refuse a turn's first model call (it gates
  write-capable tools at a CRITICAL halt per tool call), so this check is the one that keeps a
  continuation from starting. **日本語:** 自動継続は期限の時点でブレーカーが NORMAL かつラッチなし
  のときだけ。満たさなければ取り消し（保留しない）、人間の返答を待つ。
* **Redirect's 300 characters** are held by the server too (`redirect_too_long`, 400; no model
  call, the report stays pending), counted after the page's prefix.
* **A server restart:** the reports list lives in memory, so after a restart
  `GET /api/progress/current` answers `report: null` (the window has ended with the server
  anyway, `revoked(stop)`, and no continue survives it); the reports themselves stay in the
  turn events. **Restoring the board across a restart is left to #446** (its shared restore),
  not built in phase 1. **日本語:** サーバー再起動後は `report: null`（記録は turn events に残る）。
  再起動をまたぐ復元は #446 で扱う。

### Phase 4 as built (count triggers) and the board (#446)

* **Count triggers** (§2 "Count", built ahead of phases 2–3): `count_tools` and
  `count_tokens` in the `progress` section (and `STIRA_PROGRESS_COUNT_TOOLS` /
  `STIRA_PROGRESS_COUNT_TOKENS`), `0` = off, the default. The counts run from the same point
  as the time trigger (the turn's start or the person's last action) and are checked at the
  same place (`ProgressTracker.check`, first at the tool boundary, before the rule, the card
  and the YOLO approver). Tool calls: the call after the N-th is the one cancelled. Tokens: the
  input + output of the finished model calls of the turn (`model.completed` usage, #445),
  read from the chat API's subscription to the turn events — a count, never a text. The first
  due trigger wins (time, tools, tokens); never two checkpoints in one turn; everything after
  the nudge (soft / hard end, the report, the answers, the automatic continue) is phase 1's.
  `progress.report` carries `trigger: "count"` and `count: "tools" | "tokens"`.
* **The board** (#446): `GET /api/activity` lists `progress.report` / `.reply` /
  `.auto_continue` as rows (with the trigger) and shows a pending report as the state
  ("a progress report waits for your answer"); the rows come from `turns.db`, so after a
  page reload **and after a server restart** the reports and their answers are on the
  board again. The report panel's pending report itself is still not restored across a
  restart (the window ended with the server; nothing can be continued from it).
* **Not built yet:** turns outside a YOLO window and the settings page section (phase 2),
  the work journal (phase 5). Irreversible operations stay no trigger (question 8). The
  declared checkpoint (phase 3) is built: see below.

### Phase 3 as built (declared checkpoint)

Operator approval 2026-10-21 (13:14): reuse `handoff_to_user` and add one fixed paragraph
(question 6 (b)). Pinned in `tests/test_progress_declared.py`.

* **Setting:** `declared` in the `progress` section and `STIRA_PROGRESS_DECLARED`
  (environment > config > default), **off by default**. It applies only where phase 1
  applies: a chat / Stage turn (`yolo.origin.covered`) of an open YOLO window whose
  reports are on (the dialog's tick).
* **The declaration** is `handoff_to_user` with `breakout_of_loop` exactly `true` (a tool
  whose rule is Never is not offered, so its call declares nothing). `CheckpointHooks`
  passes its `message` to `ProgressTracker.check`, which — first at the tool boundary,
  like the other triggers — **cancels the call before the rule, the card and the YOLO
  approver** (no card, no cap slot, no automatic approval, no `audit.yolo.auto_approved`
  row; the upstream tool does not run, so nothing is printed on the server's console).
  The rest of the batch is cancelled too. After the batch the harness ends the turn with
  `AfterToolsEvent.end_turn` set to the **note**: the message masked (`mask_secrets`),
  fenced code dropped (`[code omitted]`), capped (1 200 characters, 12 lines) —
  `facts.clean_note`, the soft end's rules. The history keeps alternating (the note is the
  one assistant line), the chat history's response ends with the note, and
  `GET /api/progress/current` shows it (kept in memory with the reports list; the event
  holds no note). `progress.report` has `trigger: "declared"`, `end: "soft"` (`"hard"`
  with an empty message: the fixed line `[checkpoint]`).
* **What follows is phase 1's:** the report panel, Continue / Redirect / Stop / hold, the
  automatic continue and its limits, the board rows (#446).
* **Rate limit:** at most one declared report every **10 minutes** (fixed), counted from
  the last checkpoint of any trigger in this conversation (`/new` clears it). A declaration
  inside the gap is cancelled with a fixed text ("Not a checkpoint: ... the turn goes on")
  and the turn goes on: no report, no end of turn. The "info report" of §2 is not built.
* **A declaration after the time / count nudge** (in a later batch) is the note the nudge
  asked for: the turn ends with it (`trigger` stays the first one, `end: "soft"`) instead
  of the hard end.
* **The static paragraph** (`tracker.DECLARED_GUIDANCE`, English, fixed — no date, count
  or state) is appended after the Tool Availability section (#508), the end of the cached
  static part (#254), only with `declared` on and while a window with reports on is open
  (`active` or `paused`: a breaker pause does not change it). So the static bytes stay the
  same on every turn and change only when such a window opens or ends (or at a restart
  with another setting). The Tool Availability section's YOLO line already changes the
  static part at those two moments, so the paragraph adds no cache break of its own.
  **Measured** (offline: the real server and `BedrockModel` against
  `tests/fake_bedrock_endpoint.py`, the ConverseStream request body as sent): the
  `system` part is 5,562 JSON characters with no window, 5,798 with a window open and
  `declared` off (#508's line), **6,245 with `declared` on (+447 = the 441-character
  paragraph and its separator)** — about 111 tokens by `estimate_tokens` (4 characters per
  token; no Bedrock call was made to count real tokens). Nothing without the setting or
  outside such a window.
* **Delay in a real browser** (2026-10-21, the same offline server, headless Chrome 1300 ×
  860 via the DevTools protocol, a MutationObserver on the Stage page; 5 rounds of a
  6-second model call followed by the declaration, plus one short turn from an idle
  board): from the report's own server time to the board's state line 「中間報告の返答待ち
  （区切りの宣言）」 **0.1–3.0 s (median 1.3 s)**, to the Stage report panel **0.05–2.6 s
  (median 1.3 s)**; from an idle board 1.1 s / 2.0 s after sending. From sending a message
  to the board showing 「モデル生成中」 0.4–3.4 s. All under the 5 s the board was designed
  for (4 s poll while busy); a board that has been idle for minutes polls every 20 s, so
  its first change can take up to that (by design, #369).
* **Unchanged:** `handoff_to_user` with the setting off, outside a covered window turn, or
  with `breakout_of_loop: false` (the upstream input wait); `stop`; the tool list; the
  rule tables (`handoff_to_user` stays Always in the profiles and an internal YOLO tier
  tool — the declaration never reaches either).

### Measured in phase 1 (the three points revision 1 left open)

Pinned by tests in `tests/test_progress_report.py` (strands-agents 1.59.0, the scripted model):

| Point | Result |
|---|---|
| Does `invocation_state["request_state"]["stop_event_loop"]` set in a `BeforeToolCallEvent` hook reach the event loop? | **Yes**: the hook's `invocation_state` is the dict the loop reads; the model is not called again after that tool round. |
| Does `handoff_to_user(breakout_of_loop: true)` end a Stira chat turn? | **Yes**: one model call, the turn completes normally (with reports on, too). `stop` likewise. |
| After a forced stop, are two user messages in a row sent? | **Yes** with `stop_event_loop` (the history ends in the tool result; the next message is a second `user` message). Hence phase 1 ends a hard checkpoint with `AfterToolsEvent.end_turn` (one assistant line, `[checkpoint]`): the next request alternates (measured). |

## History (経緯)

**日本語.** 2026-10-19 の実測(main=`608153a`)で、YOLO の窓を開いたままチャットで依頼した
1 つのターンが 15〜20 分続き、その間、利用者には「いま何が終わって、次に何をするのか」が伝わら
なかった(ツール 13 回、20:04 に成果物ができていたが、完成と判断したのかも分からない)。
窓は最大 6 時間あるが、人間は 6 時間見続けられない。そこで、区切りで立ち止まり、ボードで
『ここまでできました、どうしますか』と報告する段階を設けたい、という提案が出た(#451)。同日、
被験者のエージェントから、報告を**作業日誌**として残したいという意見があった(#456、#451 の
コメント)。また、ターンの実行がブラウザの SSE 接続に結びついていて、タブを閉じると長いターンが
`cancelled` で止まることが分かった(#452)。この ADR は、その 3 点を前提に、**OSS のアルファ版で
手触りを優先し、過剰な仕組みを作らない**という方針(2026-10-18)で、最小の形を決める。

**English.** Measured on 2026-10-19 (`main` = `608153a`): with a YOLO window open, one chat
turn ran for 15–20 minutes and the person could not tell what had been finished or what came
next (13 tool calls; an artifact existed at 20:04, but whether the agent considered it done was
not visible). A window lasts up to six hours; nobody watches for six hours. The proposal (#451):
a phase in which the agent stops at a checkpoint and reports on the board, "this is done — what
now?". The same day the agent under test asked for a **work journal** built from such reports
(#456, comment on #451), and #452 found that a turn dies with `cancelled` when its browser tab
closes. This ADR picks the smallest design that works under those facts, following the
2026-10-18 direction for the OSS alpha: feel and UI first, no heavy machinery.

## Goals and non-goals

**Goals.**

* At a checkpoint the person sees, on the Stage board and in the chat log, a short structured
  report and three answers; one press decides.
* A checkpoint costs nothing when the feature is off, and in phase 1 adds no tool, no prompt
  text and no model call of its own (the one nudge of §1 is a tool result inside a turn that is
  already running).
* A report holds **no file contents, no command strings, no secret values** — by construction,
  not by filtering alone (§3).
* Reports and answers are kept and are restored after a reload (§6, shared with #446; across
  a server restart: #446, see "Phase 1 as built").
* No new permission: a checkpoint can only stop work, never start or widen it.

**Non-goals.** External notifications (Slack, mail, push; #239 / #299 own the channel); judging
the quality of a report's text; the goal loop itself (#299); a new YOLO state; any change to
the approval cards, Never, the floors, the breaker or `CRITICAL_HALT`; changing
`handoff_to_user` or `stop`; holding a turn open while waiting for a person (§1, rejected for
v1); a scheduled job that calls a model (ADR 0008).

## Terms

* **Checkpoint** — the point at which the harness ends a turn for a report. Kinds (`trigger`):
  `time` (§2), `declared` (the agent asked for it, §7), later `count` (§2).
* **Report** — one record per checkpoint: `report_id` (128-bit random), the turn it ended,
  `trigger`, the facts the harness collected, the agent's note (§3). At most one report is
  *pending* (unanswered) at a time: the newest one.
* **Answer** — `continue`, `redirect` (with one line of text), `stop`, or `message` (the person
  typed an ordinary message instead of pressing). `auto_continue` is the opt-in countdown's
  answer (§4). An answer is bound to its `report_id`; an answer for a report that is not the
  pending one is refused (`report_not_pending`).
* **Human contact** — the last time a person did something the agent can see: sent a message,
  decided an approval card, answered a report. The time trigger counts from it.
* **Soft end / hard end** — how a checkpoint ends the turn (§1): the model writes the report
  text and stops (soft), or the harness stops the event loop with facts only (hard).

## Decisions

### 1. Where the pause lives — end the turn, do not hold it (Open (1))

The issue asks that "during a report no new tool call runs; Continue resumes; Stop ends the
window". Three ways to get that:

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(T) End the turn at the checkpoint**; Continue / Redirect = the next human turn | nothing runs while nobody answers; the next turn passes the whole gate stack as a fresh human turn; no thread waits | survives a closed tab **today** (the report is in the history, #452 does not matter); the person can come back an hour later; Redirect is just a message | small: one check at the tool boundary, one turn-event type, the board view; no reply route besides Stop |
| (H) **Hold the next tool call** inside the turn (like an approval card) until the answer | same, but the waiting turn holds the generation lock, and a held call's approval is decided after the answer | Continue resumes in the same turn; a server-side "continue after N s" is possible | larger: a blocking desk like `HITLApprovalBroker`, a reply route, timeouts, restore of a held turn; **useless until #452** — today closing the tab cancels the waiting turn and the report with it |
| (P) A new YOLO state `paused(progress_report)` | changes ADR 0012's accepted state machine: a resume is a loosening judged by `rules_loosening`, needs a CAS, rows and tests (§2, §6a there) | — | largest; and it covers YOLO only, not ordinary long turns |

**Recommendation: (T).** It is the lightest, it works before #452, and it reuses what a turn
already does. Its one cost: a server-side "continue if nobody answers" would be a model call
started by a timer without a person — that is the goal loop's job (#299, a grant authorizes
unattended iterations), not this feature's. The opt-in countdown of §4 runs in the person's
open page instead. (H) can be revisited after #452 if measured use asks for in-turn resume.

**How the turn ends (soft, with a hard fallback).** At the first tool boundary after a
checkpoint became due, `CheckpointHooks.before_tool` cancels that call with a fixed text (a
tool result, so it enters the message tail and leaves the cached prefix alone, #254):

> `Checkpoint (progress report). This call was not run. Do not call tools now. In at most
> eight short lines tell the person: what is done, what you produced (paths), what is open or
> uncertain, what you would do next, and any question. Then end your turn.`

The model's next output is the report text and the turn ends normally (`stop: "checkpoint"`).
If the model calls a tool again instead, that call is cancelled too and the event loop is
stopped (`invocation_state["request_state"]["stop_event_loop"] = True`, the primitive the
SDK's `stop` tool uses; strands 1.59.0 `event_loop.py` reads it after the tool round; that the
hook's `event.invocation_state` is that same dict is read from the source, not measured —
phase 1 pins it with a test) — a **hard end**: the report
then has the harness facts and no note. Soft costs one short generation per checkpoint (a few
hundred output tokens; the input is mostly a cache read); hard costs nothing. Open (2) asks
whether phase 1 starts soft or hard.

The cancelled call was planned under the old direction; it is not replayed. After Continue the
model decides again whether to make it (it sees the cancellation in its history).

**Message order after a hard end.** A soft end leaves the history ending in an assistant text,
so the next human message alternates as usual. A hard end leaves it ending in a tool result (a
`user`-role message); the next human message would then be a second `user` message in a row,
which a provider may refuse. Phase 1 measures this with the fake model and the Bedrock
message shape, and if needed closes the hard end with one fixed assistant line
(`[checkpoint]`) recorded in the history. Not measured yet.

**Cost (物差し).** Safety side gains: a bounded unattended stretch, and a person sees direction
before the end. Convenience side loses: an unattended run stops at the first checkpoint until
someone answers (§4), and a soft end costs one short generation. That is why `enabled` is off
by default and the no-answer behaviour is the person's choice. The heavier alternatives (H),
(P) lower the same risk no better: in all three, no tool runs until a person answers.

### 2. Triggers (Open (3))

| Trigger | Safety | Convenience | Implementation cost | v1 |
|---|---|---|---|---|
| **Time**: `interval_min` since the last human contact (default 30, 10–360) | bounded unattended stretch | predictable; a person present resets it by acting | small: one clock read at the tool boundary, injectable clock | **phase 1** |
| **Declared** by the agent (an artifact done, a fork in direction) | the agent can only stop itself | the most natural boundaries | small–medium: guidance text in the static prompt + detection (§7) | **phase 3** |
| **Count**: N tool calls or N tokens since the last contact | bounds cost, not only time | useful for cost control; easy to set too low | small (the counters exist, #448) | phase 4, off |
| **Before an irreversible operation** (send, overwrite, delete) | — | — | large: no reliable classifier of "irreversible"; overlaps the Ask rules and YOLO tiers | **not built** |

* **Recommendation: time + declared**, count later, irreversible not as a trigger. The rules
  already answer "ask before this": a tool whose effect a person must see stays Ask (a card)
  and, in YOLO, the tier decides (internal / external / exec). A cheaper later variant if
  wanted: "report before the first external or exec automatic approval of a window" (Open (8)).
* The check runs **only at a tool boundary inside a running turn** (`CheckpointHooks.before_tool`).
  No timer thread, no model call outside a turn (ADR 0008 kept). A turn that is one long model
  call reaches its checkpoint at its next tool call or at its end (#445 shows its progress).
* **Lower bounds** keep reports from becoming noise: the interval is at least 10 minutes;
  a declared checkpoint within 5 minutes of the last report is shown as an *info* report that
  does not end the turn; never two checkpoints in one turn.
* **Human contact resets the clock**: a message, an approval-card decision, an answer. A person
  who is answering cards is present; asking them for a report as well is noise.
* Measured case for scale: the 2026-10-19 turn lasted ~20 minutes, so at 30 minutes it would
  not have produced a report; at 15 it would have produced one. Open (3) asks for the default.

### 3. What a report contains — and never contains

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(a) Harness facts + the agent's note** | facts are allow-listed by construction; only the note is free text, masked and capped | numbers the person can trust, words that explain them | small |
| (b) The agent's free text only | everything the model chooses to quote, including file contents | no reliable usage numbers (models do not count tokens) | smallest |
| (c) Structured fields from a new tool only | caps per field | best layout, but needs the tool (§7 (a)) and the model's cooperation for every number | medium |

**Recommendation: (a).** Two parts with two writers:

| Part | Fields | Source |
|---|---|---|
| **Facts** (harness) | `elapsed_sec`; tool calls (total, failed) by tool name; artifacts = paths written by `file_write` / `editor` (`create`, `str_replace`, `insert`) shown as `<root label>:<relative path>` (≤ 10, newest first); tokens in / out / cache read / cache write; context gauge (`context_tokens`); YOLO: tier, remaining time, used / limit, external and exec n / limit, paused reason (from `yolo.runtime.status()`); `trigger`, `end` (`soft` / `hard`) | turn events (`tool_call.*`), the turn's usage, YOLO status |
| **Note** (agent) | the model's report text (soft end) | the last assistant text of the turn |

**The rule: no file contents, no commands, no secrets.**

* **By construction:** the facts read **only** the tool name, the status and — for the write
  tools above — the path argument. They never read another argument, an output, a command
  (`shell` / `batch` show as a count only) or memory content. A path that `mask_secrets` would
  change, or that resolves outside the workspace and the write roots, is shown as
  `<root label>:…` without the rest.
* **The note** is the agent's own words, which the board already shows as its answer. It goes
  through `mask_secrets`, fenced code blocks are dropped (`[code omitted]`), and it is capped
  (1 200 characters, 12 lines). That removes the common ways file contents slip in; it is not a
  proof (an agent can paraphrase a file), which is the same limit as every answer on the board.
* The report is drawn from these fields with text nodes (no HTML, no Markdown rendering of the
  facts); the note uses the board's existing sanitized Markdown (#350).
* Questions to the person are part of the note (the soft-end text asks for them); a structured
  `question` field comes only with Open (6) (a).

### 4. Answers, and what happens without one (Open (4))

Answers are the Stage choice window (`dashboard/stage/js/live_ui.js`, the pattern of
`approvalChoices`) and, on the chat page, a card in the log:

| Answer | Effect |
|---|---|
| **続ける / Continue** | sends the human message `続けてください。` / `Please continue.` (the page's language) — a new turn, a normal human turn for YOLO (§3 of ADR 0012) |
| **方向を直す / Redirect** | a one-line field (≤ 300 characters, masked); sent as the next human message, prefixed `方向修正: ` / `Change of direction: ` — the instruction of the next model call (acceptance 3) |
| **ここで止める / Stop** | closes the report; if a YOLO window is open, also `POST /api/yolo/stop` (`revoked(stop)`, a normal end, never refused) — Open (5) |
| typing an ordinary message | counts as the answer `message` (no button needed) |

The choices are three buttons; Redirect opens its field in place; Escape closes the field, not
the report. Wording follows #350's main-screen rules; ja / en.

**No answer.**

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(a) Wait** — nothing runs until a person answers | safe side; the YOLO clock keeps running, nothing is approved | a person who left gets back to a stopped task with a report | none (it is what (T) does) |
| (b) **Countdown in the open page**: after `continue_after_sec` (default 300, 60–3600) the page sends Continue (`auto_continue`), with a visible countdown and Cancel | the person chose it; only while their page is open; the next turn is a human turn for YOLO | reports become non-blocking when someone glances in | small (page only) |
| (c) Server-side continue after N s | a model call started by a timer with no person: autonomy outside a grant (ADR 0011), and not a human turn for YOLO | works with no page | belongs to #299 |

**Recommendation: (a) by default, (b) as a setting** (`on_no_reply: "wait" | "continue"`),
(c) not here. Cost of (a): a six-hour YOLO run with reports on now stops at the first
checkpoint until someone answers — the opposite of "leave it running"; that is why the feature
is off by default and the person who switches it on chooses (b) if they want flow over
oversight. A page closed during (b)'s countdown sends nothing (safe side).

### 5. Relation to the YOLO window (ADR 0012)

* **No change to ADR 0012's state machine.** A pending report is "no turn running". The window
  keeps its clocks (`expires_at` is fixed at the press), approves nothing (no tool call
  arrives), and its watchdog, breaker pause / resume, caps, sub-caps and latch work as now.
  The call cancelled at the checkpoint is cancelled **before** the YOLO approver is asked
  (§11), so it reserves no cap or sub-cap slot and writes no `audit.yolo.auto_approved` row.
  There is no new `paused` reason and no new state: a report pauses the *task*, not the
  *authority*.
* **Continue / Redirect** start a human turn; inside it every Ask in the tier is answered as
  before, and the cap counts as before. If the window ended while the report waited, the next
  turn's Ask calls show cards — the badge already says why.
* **Stop** = the existing stop: `revoked(stop)`, a normal end through the fence (§6b there),
  no confirmation, never refused. A report never opens, extends or widens a window; nothing in
  this ADR is a loosening in the sense of `rules_loosening`.
* The report's YOLO facts are read from `yolo.runtime.status()` (the page's own source) at the
  checkpoint; they are a snapshot, labelled with its time.
* Without `STIRA_YOLO_ALLOW` the feature works the same, without the YOLO facts.

### 6. Records and restore (Open (7))

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(a) Turn events only** (`turns.db`), codes / counts / paths | nothing free-text in an immutable store; effects audited where they happen | restore and history from one source, shared with #446 | small |
| (b) (a) + codes-only `audit.progress.*` rows in `events.db` | the report trail is tamper-evident | — | small–medium: registry entries, ADR 0013 §1b rows, a failed row must not block a narrowing |
| (c) A separate progress store | — | — | a third store to keep consistent; rejected |

**Recommendation: (a).**
**Turn events** (`turns.db`, observability — ADR 0013's "operational" side), two new types in
`turn_events.EVENT_TYPES`, allow-listed fields only:

| Type | Required | Optional |
|---|---|---|
| `progress.report` | `report_id`, `trigger` (`time` / `declared` / `count`), `end` (`soft` / `hard` / `info`) | `elapsed_sec`, `tools` (`{name: [n, failed]}`, registry names; an MCP tool as `mcp`), `artifacts` (list of `<root>:<relative path>` strings, ≤ 10), `usage` (`input`, `output`, `cache_read`, `cache_write`), `context_tokens`, `yolo` (`tier`, `remaining_sec`, `used`, `limit`, `external`, `exec`, `paused`), `note_chars` |
| `progress.reply` | `report_id`, `answer` (`continue` / `redirect` / `stop` / `message` / `auto_continue`) | `next_turn_id`, `yolo_stopped` (bool) |

* The **note** is not copied into the event: it is the turn's final assistant text, already in
  the chat history and the `turn.completed` record; the event points to the turn. The redirect
  line is the next turn's user message, not an event field. So the events carry codes, counts
  and paths only.
* `progress.reply` is appended to the **report's** turn (the store accepts an event after
  `turn.completed` today; phase 1 pins that with a test), `turn.completed` of the report turn
  carries `stop: "checkpoint"` (a new value, not an error: acceptance and #448's counts do not
  see a checkpoint as a failure).
* **Audit:** no new `audit.*` family in v1. A report and its answer change no authority (the
  turn has ended; the next turn passes the full gate as a human turn); the effects that do
  change authority are audited where they happen (`audit.yolo.ended(revoked_stop)` for Stop).
  Option (b) of Open (7) adds codes-only `audit.progress.reported` / `.answered` rows if the
  trail should be in the hash chain.
* **Restore:** the page asks `GET /api/progress/current` (the newest report, pending or not,
  and its answer — built from the turn events; nothing else is stored) at load and with the
  #446 activity poll; the Stage shows a pending report in its choice window again after a
  reload, and an answered one in the backlog. A server restart does not change a report (the
  turn had already ended); the board says when it was made. Phase 1 restores after a page
  reload only: after a server restart `current` is `null` until #446 restores it.
* **Retention:** a pending report is replaced by the next one (only the newest is pending); the
  history keeps every report as turn events, under `turns.db`'s retention.

### 7. How the agent is told — prompt cache and `handoff_to_user` (Open (6))

Phase 1 adds **nothing** to the prompt or the tool list: the nudge of §1 is a tool result. A
`cache_probe` test pins equal tool-spec and static-prompt hashes with the feature on and off.

The declared checkpoint (phase 3) needs the agent to know it may stop at a milestone:

| Option | Safety | Convenience | Implementation cost / cache |
|---|---|---|---|
| **(b) Reuse `handoff_to_user` with `breakout_of_loop: true`** as the declaration + one fixed guidance paragraph in the **static** prompt section | the tool exists, its rule and tier apply unchanged; it only ends the turn | free-text message as the note; the harness adds the facts | small; the static text changes the prefix **once** when the setting is saved, never per turn, never per screen (#254, #366) |
| (a) A new tool `progress_report(summary, done[], artifacts[], open[], next, question)` | structured fields make caps per field simple | best board layout | medium; the tool list changes once at the save (tool order is part of the cached prefix) |
| (c) A text marker in the answer (`<checkpoint>`) | the model can forge or forget it | — | small, unreliable |

**Recommendation: (b).** First use what exists; switch to (a) only if measured reports lack
structure. `handoff_to_user` keeps its behaviour: with `breakout_of_loop: true` it sets
`request_state["stop_event_loop"]` (upstream source, strands-agents-tools 0.8.9) — that this
ends a Stira turn is **not measured yet** and is phase 3's first test — and the harness, when
the feature is on, additionally turns that end into a `declared` report (the message becomes
the note, through §3's rules). If the upstream flag does not reach the event loop in Stira's
wiring, the harness ends the turn itself after the call (the §1 hard end), which changes
nothing for the tool's own result. With
`breakout_of_loop: false` it still waits on the server's stdin (`docs/tool-definitions.md`) —
unchanged and not used here. `stop` is unchanged and is not a report (it means "done").

The guidance paragraph (static, constant, English, the model answers in the conversation's
language): `In long tasks, when you finish an artifact or reach a fork in direction, you may
stop and report: call handoff_to_user with breakout_of_loop true and a short message — what is
done, what you produced (paths), what is open, what you would do next, any question.` It sits
in the harness's static part (`ContextAssembler._startup_sections`), not in SOUL, not in the
Neural State / Identity sections, and it is the same on the chat and the Stage.

### 8. Other origins

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(a) Human chat / Stage turns, with or without YOLO** | only where a person can answer | covers the measured long turn without YOLO too | small (the human-turn mark exists) |
| (b) YOLO windows only | same | misses long turns under Always rules | same |
| (c) Every origin | a bot / webhook / job turn would stop with nobody to answer | — | small, harmful |

**Recommendation: (a).** Per origin:
| Origin | Checkpoints? | Why |
|---|---|---|
| Human chat / Stage turn, with or without a YOLO window | **yes** (when on) | a 20-minute turn without YOLO (Always rules) has the same problem |
| Webhook, Discord / Slack bots, ACP, sub-agents, children of exec tools | no | no person on the board; a checkpoint would only lose their work |
| Scheduler jobs (ADR 0008) | no | the chat server wires model-free jobs; an agent job has no board and its budget bounds it |
| Durable-approval resume (ADR 0006) | no | it finishes one approved call, not a long run |
| Goal loop (#299, not implemented) | **yes, at the iteration boundary** — designed in #299 | a report ends an iteration; Continue is the grant's user resume (ADR 0011 §11 `paused(operator)` → a new reason `paused(progress_report)` there); unattended continuation is what the grant authorizes, not this feature |

The human-turn mark (`yolo.origin.human_turn`, set by `/api/chat`) is the test for "a person's
turn"; it exists without the YOLO allowance too.

### 9. Notification

| Option | Safety | Convenience | Implementation cost |
|---|---|---|---|
| **(a) In the dashboard: badge + tab title** | nothing leaves the host | enough when the tab is open somewhere | small |
| (b) (a) + browser notifications (opt-in) | the OS shows the report's title only | reaches a person in another window | small, plus a permission prompt |
| (c) External channels now | a new egress path with its own secrets | reaches a phone | large; #239 / #299 own it |

**Recommendation: (a) in v1, (b) later, (c) in #239 / #299.**

* **In the dashboard, v1:** the Stage and the chat header show `報告あり` / `Report waiting`
  next to the YOLO badge (its own element; the YOLO badge is unchanged); the tab title gets the
  prefix `● `. Removed when answered. (A Stage motion for "waiting for you" is a design
  question for #446's character integration, not decided here.)
* **Browser notifications** (the Notification API needs a permission prompt): later, opt-in.
* **External** (Slack, mail, push): out of scope. `progress.report` is the event the #239 /
  #299 notification base subscribes to; this ADR only promises that the event exists and holds
  no secret.

### 10. Settings

A `progress` section in `gateway-config.json`, on the settings page (no environment variable):

| Field | Default | Accepted | Phase |
|---|---|---|---|
| `enabled` | `false` | bool | 1 |
| `interval_min` | `30` | 10–360 | 1 |
| `on_no_reply` | `"wait"` | `"wait"`, `"continue"` | 2 |
| `continue_after_sec` | `300` | 60–3600 (only with `"continue"`) | 2 |
| `declared` | `false` | bool (adds the static guidance, §7) | 3 |
| `count_tools`, `count_tokens` | `null` | positive integers or null | 4 |
| `journal` | `false` | bool (§ phase 5) | 5 |

Out-of-range values are rounded into range with one warning (like the `yolo` section). Turning
reports on or off is not a loosening of authority (no permission changes); a save during a turn
applies from the next turn. Docs rows go into `docs/configuration.md` / `_ja.md` with the
implementation.

### 11. Connection points in the code

| Where | What |
|---|---|
| `src/stiracore/agent/checkpoint_hooks.py` `CheckpointHooks.before_tool` | first statement: `progress.check(event)` — if due, cancel the call with the §1 text (second time: also `stop_event_loop`) and return **before** `read_policy` / `request_hitl` / the YOLO approver, so a cancelled call never shows a card or reserves a cap slot. (A separate hook registered earlier would not do: `before_tool` does not look at an earlier hook's `cancel_tool`.) |
| new `src/stiracore/progress/` | `triggers.py` (pure due check, injectable clock, human-contact reset, lower bounds), `facts.py` (the allow-list of §3), `settings.py` (the section of §10) |
| `src/stiracore/agent/nxp_agent.py` `NXPAgent.__call__` | pass the turn's counters to the check; note `stop: "checkpoint"` in the turn summary |
| `src/stiracore/observability/turn_events.py` | `EVENT_TYPES` / `_DATA_REQUIRED` gain `progress.report` / `progress.reply` |
| `src/stiracore/observability/chat_api.py` `chat()` → `finish_ui` | emit `progress.report` before `turn.completed` for a checkpoint end; `POST /api/chat` accepts `progress_reply: {report_id, answer}` and appends `progress.reply`; `GET /api/progress/current`; `POST /api/progress/<id>/close` for Stop (CSRF, same origin, refused with 409 `report_not_pending` when stale) — human contact is noted in the HITL broker's `decide` and in `chat()` |
| `src/stiracore/yolo/runtime.py` `status()` / `stop()` | read for the facts; Stop calls the existing stop |
| `dashboard/stage/js/live_ui.js`, `adapter.js`, `board.js`, `ui.js` | the pending report in the choice window (after any approval: an approval card comes first), the facts as a fixed template on the board, the backlog line |
| `dashboard/static/js/chat.js` (+ `chat_core.js`) | the report card in the chat log, the badge, the countdown of §4 (b) |
| `src/stiracore/agent/context_assembler.py` `_startup_sections` | phase 3 only: the static guidance paragraph behind `declared` |
| `dashboard/static/i18n/catalog.{ja,en}.json` | every string |

## Alternatives considered

* **Hold the turn (H)** and **a YOLO pause state (P)** — §1.
* **A per-turn reminder in the system prompt** ("you have been working for 25 minutes") — the
  text changes every turn and breaks the cached prefix (#254); rejected.
* **A separate summarizer model call** writing the report — a model call outside the turn, a
  second model to configure, and cost on every checkpoint; the soft end gets the same text from
  the model already in the turn.
* **Only the final report** — the problem of #451 itself.
* **End-of-turn reports for every turn** — noise for short turns; the interval's lower bound
  and "human contact resets" exist for that reason.
* **Reuse the approval card** (a pseudo tool call `progress` shown as a card) — it would mix an
  authority decision with a progress answer, and an approval wait expires (`hitl_timeout_sec`,
  120 s) into a rejection.

## Open points (operator decision)

* **(1) Mechanism.** (T) end the turn / (H) hold the next tool call / (P) YOLO pause state.
  **Recommended: (T)** (§1); (H) after #452 if in-turn resume is wanted.
* **(2) Phase 1 end.** (a) soft (one short generation, the model's note) with hard fallback /
  (b) hard only (zero tokens, facts only). **Recommended: (a)** — the note is most of the
  report's value; its cost appears only when the feature is on.
* **(3) Default interval.** 30 min (issue) / 15 min (the measured 20-minute turn would get a
  report) / 60 min. **Recommended: 30**, lower bound 10.
* **(4) No answer.** (a) wait / (b) page countdown / (c) server continue. **Recommended: (a)
  default, (b) as a setting**, (c) in #299. **Decided (2026-10-19): the server continues after
  10 minutes (configurable), once per report, at most 3 in a row, only in an `active` window**
  (Operational decisions above).
* **(5) Stop.** (a) ends the turn's task **and** the YOLO window / (b) only closes the report,
  the window stays / (c) two buttons. **Recommended: (a)** — matches the issue's acceptance,
  safe side; cost: to go on later the person presses YOLO again (one checkbox).
* **(6) Declaration.** (b) `handoff_to_user(breakout_of_loop: true)` + static guidance /
  (a) new `progress_report` tool / (c) text marker. **Recommended: (b)**.
* **(7) Audit.** (a) turn events only / (b) plus codes-only `audit.progress.*` rows.
  **Recommended: (a)** — no authority changes; (b) is a small addition later.
* **(8) Irreversible operations.** (a) not a trigger (rules and tiers decide) / (b) report
  before the first external / exec automatic approval of a window / (c) a classifier.
  **Recommended: (a)**; (b) if wanted after use.
* **(9) Default of `enabled`.** (a) off everywhere / (b) off, but the YOLO dialog offers
  「30分ごとに中間報告」 (unticked) / (c) on inside YOLO windows. **Recommended: (a) for v1**,
  (b) after use (a narrowing choice in the dialog needs no confirmation, but it changes ADR
  0012's dialog text).

## Implementation order

Each phase is one small PR, fake model and fake clock, offline.

| Phase | Content | Acceptance |
|---|---|---|
| 1 (built, revision 2) | in YOLO windows: time trigger, soft end with hard fallback (`end_turn`), facts (§3), `progress.report` / `.reply` / `.auto_continue`, `stop: "checkpoint"`, the report panel (chat + Stage), Continue / Redirect / Stop / hold, typing as an answer, the automatic continue (Operational decisions), `GET /api/progress/current`, the dialog's tick, settings | 30 min of fake clock in one turn → exactly one `progress.report`; no tool runs after it (no card, no cap slot); Continue → a new human turn; Redirect's line is the next model call's user message; Stop → `revoked(stop)` and a normal end, the continue cancelled even if the window stop fails; a second tool call after the nudge → hard end with alternating history; no answer → exactly one automatic continue after 10 min; 3 in a row → it waits; no `active` window / not a human turn → none; Stop / chat Stop / `/new` / window end cancel it; a secret in the note → masked, a fenced file → `[code omitted]`, a command → absent; reload restores the pending report; feature off → no event and the same tools; `handoff_to_user` / `stop` unchanged; 1300 / 390 px, ja / en, axe 0, keyboard |
| 2 | turns outside a YOLO window (§8 (a)); the settings page section; the declaration | — |
| 3 | declared checkpoints: static guidance behind `declared`, `handoff_to_user(breakout_of_loop: true)` → a `declared` report; 5-minute lower bound (info report) | one declaration → one report; the static prefix hash equal across turns and screens, changes only on the save; `breakout_of_loop: false` unchanged |
| 4 (built, #451 PR 2) | count triggers | N tool calls → one report; tokens → one report; never two in one turn |
| 5 | work journal (#456): on an answer, one episodic entry with the report's fields (summary = the masked note, artifacts as paths), tag `progress`, written by the harness, behind `journal` | the entry holds no file content, command or secret; normal harvest / staging rules apply; NeuralState untouched |
| later | #299 goal loop: the report at the iteration boundary, `paused(progress_report)` in ADR 0011 §11; browser notifications; #239 external channel | in #299 / #239 |

## Residual risk

The same OS user can run anything (ADR 0012 §8a): a program of that user can answer a report.
That only continues or stops work the person already started; it grants nothing. The agent's
own tools cannot reach the dashboard while YOLO is allowed (own-dashboard refusal), and no
tool runs while a report is pending.

## Questions for the operator (指揮官への質問)

1. **仕組み:** 区切りでターンを終える (T) / ターンを止めたまま待つ (H) / YOLO に新しい一時停止状態 (P)。
   **推奨 (T)** — 軽く、#452 の前でもタブを閉じて大丈夫。サーバー側の「返答なしで続行」は #299 に回す。
2. **最初の報告の作り方:** モデルが短く書く (1 回の短い生成) / ハーネスの事実だけ (トークン 0)。**推奨: モデルが書く**(失敗時は事実だけ)。
3. **既定の間隔:** 30 分 / 15 分 / 60 分。**推奨: 30 分**(下限 10 分)。
4. **返答がないとき:** 止まったまま (既定) / 開いている画面のカウントダウンで続ける (設定) / サーバーが続ける (#299)。**推奨: 止まったまま + 設定でカウントダウン**。**決定（2026-10-19）: 10 分（設定可）で自動継続 + 連続の無返答上限（既定 3）**（上の『運用決定』）。
5. **「止める」:** 作業と YOLO の窓を両方終える / 報告を閉じるだけ / ボタン 2 つ。**推奨: 両方終える**。
6. **区切りの宣言:** 既存の `handoff_to_user(breakout_of_loop: true)` / 新しいツール / 文字の印。**推奨: 既存ツール**。
7. **監査:** ターンイベントだけ / 監査行も。**推奨: ターンイベントだけ**(権限が変わらないため)。
8. **取り消しにくい操作の前の報告:** トリガーにしない(ルールと YOLO の段階で決める) / 窓で最初の外部・実行系の自動承認の前に報告 / 分類器を作る。**推奨: トリガーにしない**(使ってから必要なら 2 番目)。
9. **既定の ON/OFF:** 全部 OFF / OFF だが YOLO のダイアログに選択肢 / YOLO 中は ON。**推奨: v1 は全部 OFF**。

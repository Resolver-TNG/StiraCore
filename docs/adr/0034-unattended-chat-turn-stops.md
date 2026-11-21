# ADR 0034: A chat turn nobody watches — the end of the YOLO window, unattended stops, and the Stage's stop button

- Status: Accepted (2026-11-16; commander decision on #680, implemented)
- Issue: #680 (the rest of #452)
- Related: #452 / #482 / #566 (a chat turn runs on its own worker; the explicit stop `POST /api/chat/cancel`), ADR 0012 (YOLO mode), ADR 0006 / ADR 0024 (approvals and their expiry), ADR 0031 (run frame), #299 (autonomous goal loop)

## Why a new small ADR (not an addendum)

The decision sits between three records: the YOLO window (ADR 0012), the chat turn's worker and
its explicit stop (#452, no ADR of its own) and the approval card's expiry (ADR 0006). An
addendum to ADR 0012 would cover only point 1, and ADR 0012 is already long. One short record
that names all three keeps the rule in one place. It changes none of those ADRs' decisions.

## Context

Since #482 closing the page never stops a chat turn. Three questions were left open:

1. When a YOLO window ends while a turn is running, does the turn stop?
2. When does a turn nobody watches stop by itself?
3. Does the Stage get a stop button like the chat page's (#566)?

## Decision

Every rule below only **stops** a turn. It goes through the explicit stop's path (the turn ends
with `stop: "cancelled"`, its pending cards are rejected, a tool call already running finishes).
Nothing here approves, widens, delays or re-decides an approval, a rejection, the floor, the
breaker or egress. The values are fixed in code (`observability/turn_autostop.py`); there is no
setting and no environment variable for them.

1. **The YOLO window ends → the turn stops at its next approval wait** (reason
   `yolo_window_ended`). A turn that ran while a window was open (one opened during the turn
   counts too) and then raises an approval card when that window is no longer the open one is
   stopped there: its card is rejected, not approved. **The turn's window is the first one it
   saw**: once seen, another window id never takes its place, whatever events come in between —
   so a window W1 that ends with W2 opened right after still stops the turn at its next wait
   (the same rule as the delegate's, ADR 0016 §7: another window id stops). A window reader
   that fails counts as "no window" (fail toward stopping). While the window is open, cards are
   decided exactly as before; a turn that never saw a window is not touched by this rule.
2. **An unanswered approval stops the turn** (primary, reason `approval_unanswered`): when one
   of the turn's approval cards expires with nobody answering it, the turn stops — **whether a
   page reads the turn or not**. The wait's length is the card's existing expiry,
   `hitl_timeout_sec` — no new value. A person's answer (approve or reject) never stops the turn.
   **Upper bounds, only for a turn nobody watches** (no page reads the turn's stream):
   - `unattended_time_limit`: **3600 s** since the turn started;
   - `unattended_token_limit`: **2,000,000** input + output tokens of the turn's model calls,
     counted from each `model.completed` event (the SDK reports the turn's own usage only at
     its end, so the chunks' usage alone would never reach the bound).
   They are checked when the turn produces an event or a chunk (a tool call that blocks is not
   interrupted). A turn a page reads has no time or token bound from this ADR.
3. **The Stage gets "■ Stop the turn"** (`stage-stop-turn`). It calls the existing
   `POST /api/chat/cancel` with the turn id (no new route). It is shown for this page's own turn
   once its id is known, or for a turn the server lists as `stoppable`; otherwise hidden. The
   existing "■ Stop showing" still only stops the page's reading.

The reason is recorded as a fixed code (`stop_reason` on the kept turn and `turn.completed`,
`reason` on the turn event; the ATIF export keeps it too). No message text, tool input or model
output is added. The cards an automatic stop rejects are resolved by the server (`resolver:
"system"`, the fixed sentence "the turn was stopped automatically", `resolution_reason_code:
turn_auto_stopped`), never recorded as a person's rejection. A person's stop that comes at the
same moment wins: it is taken under the same lock and drops the automatic reason.

An automatic stop does **not** end the session's "allow again" allowances (ADR 0025 §5; a
person's stop does). Accepted as safe: after an automatic stop the next turn is sent by a
person, and a stopped turn has no automatic continuation.

## Risks

- A long tool call on an unattended turn runs to its end before an upper bound is checked.
  Accepted: cancelling a running tool would leave its effect half done.
- A turn under a window that ends just before a card is raised is stopped even though a person
  might have approved it later. Accepted: stopping is the safe side; the person can resend.
- Fixed values cannot be tuned. Decided on purpose (no fine-tuning knobs); changing them is a
  code change reviewed here.

## Rejected

- **Stop the turn at once when the window ends**: a tool call already running would be left
  half done.
- **Keep auto-approving after the window ends until the turn finishes**: an approval outside
  the window the person opened.
- **Settings or environment variables for the limits**: tuning knobs the commander declined.
- **A new Stage stop route**: the chat page's stop already exists and is guarded.

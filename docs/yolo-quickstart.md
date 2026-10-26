# Trying YOLO (ADR 0012, phases 5b, 6 and 7)

[日本語](yolo-quickstart_ja.md) · Design: [ADR 0012](adr/0012-yolo-mode.md) · Variables:
[configuration.md](configuration.md)

YOLO approves the **Ask** of *internal* tools (local files inside the workspace / write roots,
Stira memory, `stop`, `handoff_to_user`) for **six hours from the press**, in chat and Stage
turns only. With "External allowed" (`STIRA_YOLO_ALLOW=external`, phase 6) the Ask of
external tools (`web_fetch`, `web_search`, `webhook_send`, `oauth_api_call`, `rss`, `speak`,
`memory`, `perplexity_search`; `http_request` too if you changed its Never) is approved as
well — at most 200 per window (`STIRA_YOLO_MAX_EXTERNAL`), and a call whose arguments look
like they carry a secret (a key in a URL, a header, a password …) still shows its card.
Never rules, protected locations, the breaker's write block, CRITICAL, exec and MCP are
unchanged — exec only changes if you choose "Include exec" at the press (section 6).

**The button is there by default** ([ADR 0012 addendum 2026-10-24](adr/0012-yolo-mode.md)):
without `STIRA_YOLO_ALLOW` and without a saved "Allowed scope" the allowance is `external`, so
the chat header and the Stage show `Start YOLO` and the dialog offers both scopes. Nothing is
approved until you press it and confirm. `STIRA_YOLO_ALLOW=off` (or "Allowed scope: Off" on
the settings page) removes the button, and then nothing changes. The default never offers
"Include exec" (section 6 needs your own `external`).

**Narrower than YOLO:** for "the same tool on the same folder / host for a while", the approval
card itself offers *Approve, and allow the same for this session* (one tool on one target class,
at most 2 hours, no setting needed; [allow-again.md](allow-again.md)). YOLO answers first while a
window is open; an allowance never opens, extends or counts against a window.

**YOLO approves only tools whose rule is Ask** (#450). *Always* never asks in the first place,
so there is nothing to approve; *Never* does not run during YOLO either. If no tool is Ask,
a window approves nothing — the start dialog then says so (it lists, for the chosen scope,
how many tools and which ones a window could approve) and links to the tool settings. To get
a useful set in one go, pick a profile on `/settings#sec-tools` (*Development*: local writes
and `shell` Ask; *Research*: also `web_fetch` / `web_search` Ask) and save it
([configuration.md §5](configuration.md#5-tool-configjson-tool-rules)).

## 1. Start the server (or choose the allowance yourself)

A plain start already shows the button (the default above). To pick the scope yourself:

```sh
STIRA_YOLO_ALLOW=internal STIRA_PORT=5612 scripts/stira_ctl.sh start
# or in the foreground: STIRA_YOLO_ALLOW=internal STIRA_PORT=5612 .venv/bin/python dashboard/stira_server.py
```

**Or from the settings page (phase 7c):** Settings → "YOLO (temporary auto-approval)" →
"Allowed scope" → *Internal tools* (or *Up to sending outside*) → "Save YOLO settings", tick
the confirmation, then restart the server (`scripts/stira_ctl.sh restart`). The value is
saved in `gateway-config.json` under `"yolo"`; an environment variable wins over it (the page
then shows the value as fixed by that variable). The limits on that page apply from the next
window without a restart.

Pick any free port (`STIRA_PORT`). Add `STIRA_STAGE=1` for the Stage page. `external`
instead of `internal` also offers "External allowed" in the dialog. The start-up log says
`YOLO allowance: internal (...)` (with the default: one line `YOLO: the Start YOLO button is
offered (default; ...)`). YOLO needs a platform whose clocks were measured (macOS
today); elsewhere the badge says why it is unavailable.

To try it without any model call: `STIRA_YOLO_ALLOW=internal STIRA_STAGE=1 STIRA_PORT=5612
.venv/bin/python tests/stira_yolo_harness.py /tmp/yolo-try` — every message makes a
scripted model write one small file in the workspace; a message containing `search` makes it
call `web_search` instead (with `STIRA_YOLO_ALLOW=external`; no endpoint configured, so it
sends nothing).

## 2. Press, confirm, watch

1. Open `/chat` (or `/stage/`). The header (the Stage's dialogue box) shows **Start YOLO**.
2. Press it: a dialog explains what changes and lets you pick the scope, and says how many
   tools (and which) that scope can approve — with a warning when none; **Next: confirm**.
3. The confirmation dialog lists the scope, the request id and the end time. Tick
   "I have checked this; it is what I just pressed" (with "External allowed" a second box)
   and press **Confirm and start**. Cancel at either step starts nothing.
4. The badge now shows `YOLO Internal only · 5 h 59 min left · Auto-approved 0/2000` and **Stop
   YOLO**. Internal Ask tools no longer show a card; each one adds a line to the chat log,
   the Stage log and its raw view: `Auto-approved (YOLO internal): file_write #a3f2 — 1/2000`.
   In an "External allowed" window the badge also shows `External 3/200`, and an external
   tool's line reads `Auto-approved (YOLO external): web_fetch #b4c1 — 38/2000 · external
   3/200` (it never names where the call went; the audit keeps only a keyed hash of the
   host). After 200 the badge says "External tools ask you again": their cards come back,
   internal tools go on.
   These lines are gone after a reload (the count stays in the badge; after a reload one line
   says "Auto-approved before this page was opened: N"). More than 20 between two reads are
   summed up in one "…N more" line.
   The line under the badge counts **every tool call** of the window (#448):
   `Tools 7 (auto-approved 3 / always 2 / manual 1 / denied 1) · the limit counts automatic
   approvals only`. *Always* is a call that needed no card (its rule is Always, or a read with
   no rule), *manual* one a person answered, *denied* one that did not run (Never, a protected
   location, the breaker, a person's rejection …); a call that ran and returned an error adds
   `· failed N`. **The limits (2000 automatic / 200 external / 200 exec) count YOLO's
   automatic approvals only** — always, manual and denied calls never count towards them (the
   badge can read `Auto-approved 0/2000` while tools ran several times). It counts every turn
   of this agent (also turns YOLO does not cover); a `batch` counts itself and each call inside
   it; nothing outside the window is counted. The same breakdown (by tool, by internal /
   external / exec, and the turns) is in `/api/yolo/status` as `window.tool_calls` /
   `window.turns` — counts only, never an argument, an output, a command or a destination.
   A call belongs to the window it was counted in: when the window ends while the call runs
   (the cap ended it at this very call, or you pressed Stop), its error still adds `failed` to
   `last_end` (memory: `/api/yolo/status`, and the ended badge the next time it is read); the `audit.yolo.ended` row written at the end is
   never rewritten, so it can show one failure fewer. A count that fails is logged and never
   changes a call, a limit or the end.
5. When the breaker leaves NORMAL the badge says `Paused (WARNING)`: every Ask asks again
   until the breaker has been NORMAL for ten minutes. Ten minutes before the end the badge
   says so; there is no extension — press again after it ended.

Keyboard: Tab to the button, Enter; in the dialogs Space ticks / picks, Tab moves to Cancel
and the confirm button, Esc cancels and the focus returns to the button.

A start, stop or unlatch in another tab (or another device) shows in this tab's badge only when
you come back to the tab or do something here (nothing is read while nothing is running).

## 3. Stop

**Stop YOLO** works in every state — while the request waits for its confirmation, while it is
being opened, active or paused — with no confirmation. A new conversation (`/new`), the
server's stop and a restart end the window too (a restart never brings it back). The badge
keeps the end reason ("Stopped", "Ended: six hours passed", "…the cap was reached") until you
press **OK**.
When a window ends, a page that watched it adds one line to its log — `What ran in YOLO window
ab12cd34: Tools 7 (…) · 2 turns` — and the ended badge keeps the same breakdown (also in
`last_end` until the next window). The audit row `audit.yolo.ended` carries it too
(`tool_calls`, `turns`; counts only). Whatever ends the window — Stop, the time, the cap, the
server's stop (the first step of a restart) — what ran in that process is recorded. A crash or a
kill writes no end row at all, so no counts either (the window is then latched, section 4).

## 4. "Latched": when YOLO did not end cleanly

After a clock anomaly, a failed audit or marker write, or a crash, the badge says the last
window did not end cleanly and no new window can start. Check what happened (the server log;
`audit.yolo.*` rows in the event store), then press **Unlatch** and tick the confirmation.
After a clock anomaly the `audit.yolo.ended` row says which clock test fired (`clock_check`) and
the deltas it compared (`d_wall`, `d_boot`, `last_d_wall`, `last_d_boot`, `clock_slack_sec`; seconds).
If the badge says it cannot be unlatched here, the records themselves could not be read
(`yolo-window.json` in the data directory is malformed or not a regular file, or the event
chain does not verify): look at those files — do not delete the marker — and restart; the
server judges again at start-up.

## 5. Confirming on the host instead (optional)

`STIRA_YOLO_HOST_CONFIRM=command` (or `dialog` / `auto` on a macOS desktop session) makes
the host confirm instead of the page: after the press the badge says "Confirm on the host"
with a countdown (two minutes) and a cancel button; run `stira yolo confirm <id>` in a
terminal of the server's computer (or answer the desktop dialog). The page's confirmation is
then refused. See [configuration.md](configuration.md) for what each value guards against.

## 6. Including exec (optional, phase 7)

With `STIRA_ENABLE_EXEC_TOOLS=1` **and** `STIRA_YOLO_ALLOW=external` the dialog offers a third
choice, **Include exec (external too)**. In that window `shell` and `batch` calls that would
ask you (their rule is Ask) run without a card too — at most 200 per window
(`STIRA_YOLO_MAX_EXEC`); a command that looks like it carries a secret (`export TOKEN=…`, a
bearer header, a key in a URL) still shows its card, and so do `cron` and the tools that start
sub-agents (`use_agent`, `think`, `swarm`, `graph`; Stira's own `delegate` too, in every window, until
ADR 0016 phase 3). A `batch` counts each call inside it on
its own (one `batch` with one `shell` = 2 of the 200), and a `cron` or sub-agent call inside a
`batch` still shows its own card. The confirmation has a third box: "I
understand that commands run without asking and can bypass Stira's protections (protected
locations, audit)" — **that is meant literally**: a command runs as the server's OS user and
can change what Stira's floors and audit protect. Choose it only for work you would approve
command by command anyway. The badge says `Includes exec · Exec 1/200`; a line reads
`Auto-approved (YOLO exec): shell #c0de — 39/2000 · exec 1/200` (the command itself is never
shown or recorded, only a keyed hash of the arguments).

To try it offline: `STIRA_YOLO_ALLOW=external STIRA_ENABLE_EXEC_TOOLS=1 STIRA_STAGE=1
STIRA_PORT=5612 .venv/bin/python tests/stira_yolo_harness.py /tmp/yolo-exec` — on that
fresh data directory `shell` and `batch` are Ask; a message containing `shell` makes the
scripted model run one `printf` into `/tmp/yolo-exec-sandbox`.

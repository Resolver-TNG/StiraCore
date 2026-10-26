# Stage board — code health audit (2026-10, W5b of #296)

The whiteboard (`board.js`) is used in every Stage session, not only by the autonomy work, so
W5b audited its display and control code by **running it** (jsdom, Node, and a headless Chrome
probe of the real page against an offline server), fixed the small defects it reproduced
(each with a test that failed first: `tests/stage_board_audit_test.cjs`, log of the red run
kept in the PR) and filed the rest.

## What each file owns

| File | Owns | Entry points | Timers / listeners / DOM | Tested by | Not tested |
|---|---|---|---|---|---|
| `board.js` | the board's open / close / pin / mode (reply ↔ activity), its geometry, the note, the copy, the one polite live region (W5b) | `createBoard(ctx)` → `show`, `showActivity`, `updateActivity`, `close`, `tick`, `setGeometry`, `relabel`; pure `boardAction`, `finalBoardAction`, `boardGeometry`, `stepProgress`, `boardTitle`, `boardStats` | no timer (the page's frame loop calls `tick`); listeners on its own elements only (pin, close, copy, swipe, Esc) — created once per board; owns `#board` and its children | `stage_board_test.cjs`, `stage_activity_test.cjs`, `stage_board_audit_test.cjs` (tables, rapid open/close, Esc, pin, language, reduced motion, axe) | real CSS (the roll, the two canvases): pixel checks in `stage_layers_test.cjs` |
| `activity.js` | the activity mode's data and nodes: the feed (cursor, merge, skew, offline), the texts, the grant panel (W5b), the attention and watch decisions (W5b) | `ActivityFeed`, `activityModel`, `renderActivity`, `renderGrant`, `grantView`, `attention`, `watchResult`, `mergeItems`, `stateText`, `rowView`, `yoloView`, `totalsView`, `elapsed`, `duration`, `clock` | the helpers: no timer, no listener (detached nodes, createElement + textContent, that the board inserts); the controller in the same module: see the next row | `stage_activity_test.cjs`, `stage_board_grant_test.cjs`, `stage_board_audit_test.cjs` | — |
| `activity.js` `startActivityController` (`main.js` `startActivity` passes the page's objects in) | the wiring: the Activity button, the idle default, `?board=activity`, the 1 s ticker, the `poll.js` loop, the iteration watch (W5b) | `startActivityController(ctx)`, called once at start | one `setInterval` (1 s, no request), one `poll.js` poller per opening (with the watch: one for the page) | `stage_activity_ctl_test.cjs` (jsdom, fake timers: off = no read, watch, reply, pin, hidden, cadence; closes #624) and the headless Chrome probe below | — |
| `live.js` | the Stage's three polls (status, turns, approvals) → adapter events | `LiveFeed`, `schedule` | three `poll.js` pollers; one `visibilitychange` listener only when the tab is hidden at load | `stage_adapter_test.cjs`, `stage_poll_test.cjs`, `stage_talk_test.cjs` | — |
| `live_ui.js` | the live controller: feed → model, narration, the approval choice window | `startLive(ctx)`, `emotionRows` | the approval window's buttons; nothing on the board | `stage_adapter_test.cjs`, `stage_emotion_rows_test.cjs` | the window's DOM end to end |
| `backlog.js` | the conversation log's entries and their "▶ Open on the board" button | `buildEntries`, `renderEntries` | one click listener per rendered entry (rebuilt with the list) | `stage_talk_test.cjs`, `stage_board_test.cjs` | — |
| `approval.js` | approval decisions through `POST /api/hitl/<id>/decision` | `ApprovalClient`, `approvalChoices`, texts | none | `stage_adapter_test.cjs`, `approvals_test.cjs` | — |
| `adapter.js` | pure mapping of the read-only APIs to Stage events | `fromStatus`, `fromTurnEvent`, `fromPending`, `narrate`, `Pacer`, … | none | `stage_adapter_test.cjs` | — |
| `observability/activity.py` | the allow-list of `GET /api/activity`: rows, state now, YOLO window, totals, the grant block (W5b) | `activity_item`, `live_state`, `window_view`, `grant_view`, `totals_from` | none (pure); the route reads in memory, `turns.db` outside any lock of the chat API | `test_activity_board.py`, `test_activity_branches.py` (every branch), `test_activity_grant.py` | — |

**Control ownership** (B4): `createBoard` is the only place that changes the board; callers
ask it (docs/stage.md, "Who owns the board's control"). No restructuring was needed.

## Probes run and what they showed

- **Rapid open / close** (200 pairs with frames between, jsdom): settles closed, `hidden`,
  one body, `onClose` once per close. OK.
- **Reply during activity / pinned + activity**: a reply takes the activity unless pinned;
  a pinned reply holds the activity unless explicit. OK. The pin survives an explicit switch
  between the two — **#623** (behaviour choice, filed).
- **Reload mid-turn**: the first read restores the last 20 rows and the running state from
  `turns.db` (server test). OK.
- **Poll failure and recovery**: backoff ×2 to 60 s, offline at 2 failures, recovery resets
  (table test). OK. **Server restart on another data dir**: the page asked for a diff after
  its old cursor and merged another store's rows — **D3, fixed**.
- **Hidden tab**: no read while hidden, one on return (poll.js). A stopped poller left its
  `visibilitychange` listener (and closure) behind, one per opening of the activity — **D2,
  fixed** in `poll.js` (`stop()` removes it; `start()` adds it again).
- **Duplicated ids** in one answer were listed twice by `mergeItems` — **D1, fixed**; 5 000
  answers keep 20 rows (memory bounded).
- **Language switch**: the board's note ("held", "copied") stayed in the old language —
  **D4, fixed** (`relabel` re-says it).
- **Resize / portrait / odd inputs** (`boardGeometry`: scale 0 or negative, a text window
  below the stage): finite, inside, `bottom ≥ 0`. OK.
- **Reduced motion**: one frame to open and to close. OK.
- **Very long tool names / many rows / 0 rows / clock skew**: names over 64 characters or
  not a code are `other`; the list is capped at 20; 0 rows say so; a server clock ahead or
  behind never gives negative seconds or a false stall. OK.
- **Real page** (headless Chrome over CDP, an offline server, `autonomy.stage_c` +
  `stage_c_exec`, a held iteration): with the board closed the watch read at the idle
  cadence; the iteration's start lowered the board in the activity mode with the grant
  panel, focus stayed on the body, no goal text on the page; Esc rolled it up and it stayed
  up for the same iteration; the next iteration lowered it again. With stage C **off**: no
  `/api/activity` read in 20 s with the board closed, no panel, no live region. OK.
- **axe** (jsdom, the activity board with the grant panel, ja / en): no violation (colour
  contrast is not computable in jsdom; the panel uses the board's measured tokens:
  docs/stage-main-ui.md §9 — 強調 5.2, body 13.5).

## Fix round 1 (the two independent reviews)

- **Locks:** the board's read path waited on the grant service's / executor's / iteration's /
  budget's lock while another thread held it, holding the whole `/api/activity` answer.
  Every one is now taken without blocking; a held one gives `grant: {"error": "grant_busy"}`
  at once; the executor's read is one snapshot (`tests/test_grants_board_read.py`,
  `tests/test_activity_grant.py`).
- **Tools:** an iteration's row listed the admitted scope as if it had run; it lists the
  tools that ran (none for a text-only iteration).
- **Focus:** the 1 s refresh dropped the focus from a link in the activity (the grant
  panel's links and the existing artifacts links); it is given back to the new copy.
- **Live region:** a "running" announcement stayed after the grant went away; the region now
  goes with it.
- **Values:** the grant block accepted any code-shaped value (the exact marker crossed it in
  every slot); it now accepts members of the fixed sets only, on the server and the page.
- **PIN-CARRY:** a pinned board, rolled up, was lowered by an iteration's start and kept the
  pin, so her next reply was held; `attention()` now says no while the board is pinned.
- **Wiring tested:** `startActivity`'s body moved to `activity.js` `startActivityController` (the page loads no extra module) and is tested in
  jsdom (#624 closed). The executor never stays "running" if its board row fails.
- **Hygiene:** a committed `.coverage` file was removed and ignored.

## Not changed

No refactor for taste. Existing displays are unchanged except the four fixed defects.

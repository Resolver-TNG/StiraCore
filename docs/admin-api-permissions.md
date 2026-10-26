# Admin / API permission matrix (#148)

Fixed by `tests/test_admin_api_permissions.py`, which builds the real chat server app
(offline) and the observer dashboard and checks **every** route against this
inventory. A new route fails the test until it is classified here.

## Model

- **No user authentication, no TLS.** Both servers refuse to bind to a non-loopback
  address unless `STIRA_ALLOW_REMOTE=1` (`resolve_bind_host`, checked before the app,
  its data-directory lock or any thread starts). `1` is the operator's statement that
  authentication and TLS are provided in front (reverse proxy); the server cannot
  verify it and logs a WARNING at start-up. Even then only `Host` names in the
  allow-list are served, so a public name must be listed in `STIRA_ALLOWED_HOSTS`.
  Built-in authentication/TLS is not implemented (open item).
- **Capability = per-process CSRF token** (`install_security`, #185). It is served
  only to same-Host pages (`/api/csrf`, `<meta name="csrf-token">`) and sent as
  `X-CSRF-Token`. There are no cookies, so there is no cookie session to ride.
- **One process serves one agent.** No route takes an agent selector; agent
  isolation is process isolation (asserted: no rule contains `agent`).
- **No permission tiers yet.** Any page of the same origin holding the token can
  call every state-changing route (observe, config, HITL, chat). Separating
  observe / configure / approve / memory needs an authentication layer first —
  open item.

Checks applied by `install_security` before any view runs:

| Check | Applies to | Refusal |
|---|---|---|
| `Host` in the allow-list (loopback + `STIRA_ALLOWED_HOSTS`) | every request (also GET, SSE, static, the token endpoint) | 403 `Host not allowed.` |
| `Sec-Fetch-Site: cross-site` | POST/PUT/PATCH/DELETE, and GET/HEAD under `/api/` | 403 |
| `Origin` (if sent) is this request's origin — same scheme, hostname (case-insensitive, one trailing dot ignored) and effective port as the request and its `Host` — or one listed in `STIRA_ALLOWED_ORIGINS`. Malformed values, `null`, `//host`, userinfo, a path/query/fragment and non-HTTP(S) schemes are refused | state-changing, and GET/HEAD under `/api/` (JSON reads, SSE streams, the token) | 403 |
| `X-CSRF-Token` equals the process token (constant-time) | state-changing, except `webhooks.receive` | 403 |
| `Idempotency-Key` seen before (scoped to this process, the method and the concrete path; bound to a SHA-256 of the body; kept 300 s, only after a 2xx) | `/api/chat`, `/api/hitl/*/decision`, `/api/config/models*`, `/api/scheduler/*` (+ observer `stimulus`) | 409 `duplicate_request` (same body: "the same request was already received; it may still be running or be finished") or 409 `idempotency_key_reused` (another body). Neither runs the view |

Pages (`/`, `/chat`, ...) stay reachable from links on other sites; only API reads are
same-origin. Same-origin `fetch`/`EventSource` sends no `Origin` (or its own), so the
UI is unaffected. No route sends CORS headers.

Behind a TLS reverse proxy the browser sends `Origin: https://<public name>` while
the server sees plain HTTP, so that origin must be listed exactly in
`STIRA_ALLOWED_ORIGINS` (and its name in `STIRA_ALLOWED_HOSTS`); nothing else is
trusted.

**No automatic re-send (#185).** The UI never repeats a chat message, a decision or a
scheduler change on its own. A chat send settles only when the turn completed, the
server answered `duplicate_request` (already received), or it refused in a way that
guarantees nothing ran (400, 403, 404, 405, 413, 415, 422, `idempotency_key_reused`).
Any other outcome — network error, stream cut, an SSE `error` event, 5xx, other 4xx —
is *unknown*: the message keeps its `Idempotency-Key`, a notice offers "re-send the
same message" (same key, so the server refuses a second run) or "discard", and no
other message can be sent until one of the two is chosen. Only discarding drops the
key. A chat stream that only reports "busy" ran nothing and releases its key on the
server, so the re-send runs. Approval decisions reuse their key for an identical retry.

Every refusal is logged at WARNING on `stiracore.observability.security`:
method, path (no query string), endpoint, reason, Origin scheme+host, Sec-Fetch-Site,
Host and client address — printable ASCII only. Never the token, cookies, other
headers, query or body (asserted).

## The chat server (`dashboard/stira_server.py`)

| Route | Method | Area | Kind |
|---|---|---|---|
| `/`, `/chat`, `/approvals`, `/schedules`, `/memory`, `/artifacts`, `/observability`, `/settings`, `/tools`, `/static/<path>` | GET | page | read |
| `/api/csrf` | GET | security | read (Host-checked; no CORS) |
| `/api/chat` | POST | chat | **mutate** |
| `/api/chat/cancel` | POST | chat | **mutate** — stops the running chat turn (#452): `agent.cancel()` (the SDK stops at its next safe point) and a rejection of that turn's live approval cards; the turn is recorded `stop: "cancelled"` with what it produced. A narrowing only (it never runs, approves or loosens anything). Body empty or `{"turn_id"}`; 409 `no_turn` / `turn_mismatch`. Closing the page that sent the message does **not** stop the turn |
| `/api/chat/session/new` | POST | chat | **mutate** — conversation reset (#350). Risk: **medium, not reversible** — the conversation sent to the model is dropped (as at a restart); Neural State, the breaker, memory, SOUL, approvals (pending and history), schedules, tool rules and turn records are not touched. Fail-closed: 409 `turn_running` / `approval_pending` / `resume_pending` / `decision_in_progress`, 503 `approvals_unavailable` / `audit_unavailable`, 500 `audit_failed` (nothing reset). Approval decisions, resume reservations and the reset share one lock, every wait bounded (5 s); the audit is written outside it. Responses carry `outcome_audit` (`ok` / `failed`). Required `Idempotency-Key` (1–128 printable ASCII), kept by the route itself for the process lifetime (last 256): two at once run once; a repeat is 409 `duplicate_request` naming the session that key started; nothing is cleared twice. Body empty or `{}` (413 over 64 KiB). Audited in `events.db` *before* the reset (`session.reset`; then `session.started` / `session.reset_refused` / `session.reset_failed`, retried once): principal, client address, counts, the key's digest; never the text. Not reachable by the agent's own tools during a turn (measured, below) |
| `/api/chat/history` | GET | chat | read |
| `/api/gateway/status` | GET | observe | read |
| `/api/hitl/pending` | GET | HITL | read — arguments masked with `mask_secrets`; a masked request is `editable: false` and the server refuses its edit (#185) |
| `/api/hitl/history` | GET | HITL | read (decision history, #189) |
| `/api/hitl/<request_id>/decision` | POST | HITL | **mutate** — also requires the displayed `args_hash` (#146). 503 `busy` when the lock it shares with the conversation reset is not free within 5 s: nothing recorded, the request stays pending (#350) |
| `/api/scheduler` | GET | scheduler | read (status, jobs, recent runs, notifications; #239) |
| `/api/scheduler/jobs`, `/api/scheduler/jobs/<job_id>`, `/api/scheduler/jobs/<job_id>/resume` | POST | scheduler | **mutate** — also a human principal, `"confirm": true` and a NORMAL breaker (ADR 0008 §10) |
| `/api/scheduler/jobs/<job_id>/pause`, `/api/scheduler/runs/<run_id>/cancel` | POST | scheduler | **mutate** — narrowing only; allowed at every breaker level |
| `/api/config`, `/api/config/{jev,models,providers,soul,tools}` | GET | config | read |
| `/api/config/{jev,models,tools}` | POST | config | **mutate** — none of them writes a breaker value; breaker thresholds are saved through `/api/config/hub` (and its rollback) only, with the hub's confirmation and a history row. The old `/api/config/breaker` route is gone (#494; `tests/test_breaker_write_paths.py`) |
| `/api/config/models/test` | POST | config | **mutate** (outbound provider connection test, #208) |
| `/api/config/hub` | GET | config | read (settings hub snapshot, #227) |
| `/api/config/hub` | POST | config | **mutate** (draft save with conflict detection) |
| `/api/config/hub/validate` | POST | config | read (validation only; CSRF still required) |
| `/api/config/hub/card/<request_id>` | GET | config | read — the Make Always dialog's view of one approval card (ADR 0025 §9): `waiting` (`call_not_finished`) until the card's call has a terminal result, then that result, the names to type, the dangers to acknowledge and a **one-time save token** (memory only, bound to the incarnation, the card, the one change, the hub version, the rule generation and the fence). The save itself is `POST /api/config/hub` with `source: "approval_card"`, `request_id` and `save_token` (no `changes`: the server derives the one change Ask → Always): 409 `call_not_finished` / `stale_confirmation` / `op_token_used`, nothing saved |
| `/api/allowances` | GET | HITL | read — the session allowances of this conversation (ADR 0025): tool, the class in clear (page only, never a row), time left, uses, and the recent automatic answers (counts and short ids) for the chat's log lines |
| `/api/allowances/<allow_id>/revoke`, `/api/allowances/revoke-all` | POST | HITL | **mutate** (narrowings, no confirmation, no breaker check): revoke one allowance (`revoked`) or all of them (`stop`, the fence + 1; every view / save token ends too). 404 `not_found` for one that already ended |
| `/api/authority/status`, `/api/authority/outbox` | GET | authority | read — the authority service's health, epochs, latches (codes), outbox unread count and watchdog pass; the notifications (codes, ids, a short instance reference; ADR 0011 W2, [authority-service](authority-service.md)) |
| `/api/authority/kill` | POST | authority | **mutate** — the kill switch: one press, no confirmation (a narrowing), always available, also when unhealthy. **No route clears a latch** (host command `stira authority unlatch`, server stopped) |
| `/api/authority/outbox/ack` | POST | authority | **mutate** — acknowledge one notification by event id (idempotent) |
| `/api/grants`, `/api/grants/<grant_id>` | GET | grants | read — **only with gateway `autonomy.stage_c`** (ADR 0011 W4, [grants](grants.md)): structured grant / goal state (ids, codes, counters, TTL left, budget left; W6c: `claim_evidence`, the last completion claim's measured codes and counters; W6d, only with `autonomy.heartbeat_start` and only on a grant issued with it: `heartbeat_start: true` and `tick` — the heartbeat start's skip counts per reason, the starts, the spacing and the last heartbeat-started iterations' token / wall-time numbers); never the goal text or the workspace path |
| `/api/grants/<grant_id>/goal` | GET | grants | read — the goal text for the issuer only, once: `?confirm=SHOW` and the header `X-Stira-Goal-View` (the one-time view token of the issue answer, consumed by the first view); `Cache-Control: no-store`; a cross-site fetch is refused |
| `/api/grants/issue` | POST | grants | **mutate** — typed `confirm: "ISSUE"` and a host-confirmed operation (428 `grant_confirmation_required` → `/api/grants/operations/<op_id>/confirm`, or the host command / dialog with `STIRA_YOLO_HOST_CONFIRM` → the same request with `X-Stira-Host-Operation`); single use, stale target refused, same op id + body = the original result; fenced on the authority epoch (`grant_issue_aborted_epoch`) and written before it counts. Every operation step is refused while an agent tool call runs (the tracker is on the agent's hooks whenever stage C is on, with or without YOLO), and refused (`host_channel_required`) unless the effective channel is the host dialog / command or the user chose `autonomy.allow_browser_confirm`. W6d: an optional `heartbeat_start: true` (a built-in boolean) — refused `409 grant_heartbeat_start_off` unless `autonomy.heartbeat_start` is on; it is part of the confirmed body (its digest) and named in the operation's summary (`source: heartbeat_start`); there is no route that changes it later |
| `/api/grants/operations/<op_id>` | GET | grants | read — the state of one grant operation (the page polls it for the host command / dialog channel) |
| `/api/grants/<grant_id>/revoke`, `/api/grants/<grant_id>/pause` | POST | grants | **mutate** — narrowings, one press; revoke is always available (also while the store or the authority is unhealthy) |
| `/api/grants/<grant_id>/resume` | POST | grants | **mutate** — the operation token (a halt also typed `confirm: "RESUME"`, breaker NORMAL); bound to the grant's generation, state and stop epoch |
| `/api/grants/<grant_id>/verify` | POST | grants | **mutate** — `{"decision": "confirm" \| "reject"}` as a host-confirmed operation, only while the goal awaits verification and the grant is active; after the end the first one is recorded only |
| `/api/grants/operations/<op_id>/confirm`, `/api/grants/operations/<op_id>/cancel` | POST | grants | **mutate** — confirm (browser channel only: a same-origin browser fetch with the one-time token, `acknowledged: true` and the instance reference; refused unless the own-dashboard guard is armed, while a tool call runs, and with the host command / dialog channel: `host_confirm_on_host`) / cancel an open grant operation |
| `/api/grants/<grant_id>/step` | POST | grants | **mutate** — **only with gateway `autonomy.stage_c_exec`** (ADR 0011 W5, [grants](grants.md)): one press starts exactly one goal iteration of an active grant in a server-owned thread and answers at once (`{"iteration": …, "grant": …}`, codes only); an empty body only; no typed word and no host operation (the grant was host-confirmed at issue); refused `409 grant_iteration_refused` with a `reason` code (not active, goal not active, killed, breaker, TTL, budget, a chat turn running, the subtree changed, an abandoned provider call alive, another iteration running, …), and `409 grant_operation_refused` (`tool_call_running`) while an agent tool call runs. Never a model-visible tool |
| `/api/config/history` | GET | config | read (no secret values stored) |
| `/api/config/history/<entry_id>/rollback` | POST | config | **mutate** (per-field undo) |
| `/api/tools/config` | GET / POST | config | read / **mutate** |
| `/api/tools/list` | GET | config | read |
| `/api/tools/page-check-ports` | GET / POST | config | read / **mutate** (ADR 0015 Addendum rev 4, #449) — `page_check`'s localhost ports (`tool-config.json` `page_check_ports`; the only route that sets them). Adding a port is a loosening: `{ports, confirm: true, confirm_ports: [the added port(s), typed]}` (400 `needs_confirmation` otherwise), refused at a breaker level other than NORMAL (403 `breaker_active`), while an agent tool call runs (409 `tool_call_running`, with YOLO) and, with the YOLO allowance, answered 428 until a person confirmed it; ports below 1024 (`port_refused`) and the dashboard's own port (`own_dashboard`) refused; one settings-history row (group `page_check`, not restorable by a rollback). Removing needs no confirmation |
| `/api/memory/tree` | GET | memory | read (the newest 20 entries per layer plus `totals` per layer and `limit`, #574; the procedural branch leaves out `staging/` proposals, #463) |
| `/api/memory/procedural/review` | GET | memory | read — proposals (body, rationale, `source_turn`, SHA-256, the diff against a same-named rule), rules (no body), recent reviews (no replaced body) (#463) |
| `/api/memory/procedural/proposals/<key>/approve` | POST | memory | **mutate** (#463) — a loosening: `{sha256, confirm: true[, replace]}` (428 `confirmation_required`, 409 `E_STALE_PROPOSAL` when the shown hash no longer matches, 400 for any other field — the request names no approver). Refused while a chat turn holds the turn lock (409 `turn_running`) or an agent tool call runs (409 `tool_call_running`, with YOLO); runs under the turn lock, so the prompt refresh lands between turns. **Needs the breaker at NORMAL** (operator decision 2026-10-20, like the other loosenings; the level YOLO samples — the engine's, made stricter by the live stability): WARNING / CRITICAL / COHERENCE → 409 `E_BREAKER_NOT_NORMAL`, an unreadable level → 503 `E_BREAKER_UNAVAILABLE`; nothing written, no prompt refresh, one `audit.procedural.refused` row (key + code). Approver = the server's OS account (`local:<account>`, as `procedural_cli`) |
| `/api/memory/procedural/proposals/<key>/reject` | POST | memory | **mutate (narrowing)** (#463) — `{sha256, confirm: true[, reason]}`; the reason (≤ 500 characters, masked) is kept in the review log, never echoed. Allowed at every breaker level and while a turn runs |
| `/api/observability/{events,snapshot}`, `/api/state/history` | GET | observe | read — on the chat server the two observability paths read `turns.db` (`observability/turns_view.py`; the same masked, size-capped data as `/api/turns`, no previews), not `events.db` (#286). **No authentication** (as for every route here, see Model): the defence of these two reads is the loopback bind plus the `Host` allow-list and the same-origin check of API reads; `tests/test_turns_observability.py` checks that a foreign `Origin`, `Origin: null` and a foreign `Host` are refused (403) before a stream starts, in-process and against a real server process. There is no "unauthenticated is refused" test because there is no authentication to configure. The snapshot is built in one read (counts, last events, turn list from one point in time in this process) |
| `/api/diagnostics/write-failures`, `/api/diagnostics/doctor` | GET | observe | read (masked permission cards / last doctor report, #244) |
| `/api/diagnostics/write-failures/<card_id>/{retry,dismiss}` | POST | diagnostics | **mutate** — retry is single-use and only re-checks (read-only); the retry itself is a chat turn |
| `/api/diagnostics/doctor` | POST | diagnostics | **mutate** (re-runs the read-only doctor: one temporary file per write root) |
| `/api/artifacts`, `/api/artifacts/<artifact_id>`, `/api/artifacts/<artifact_id>/versions/<int:version>`, `/api/artifacts/<artifact_id>/diff` | GET | artifacts | read (#454, ADR 0020) — what `file_write` / `editor` wrote into the workspace: list (newest first; `kind`, `turn`, `q`, `limit` ≤ 200; limits and warning counts), one artifact with its versions (turn id, create/update, size, SHA-256), one version (its text from the registry's snapshot, for text kinds), a bounded line diff (`from`, `to`). Only paths the registration rules allow *now* are shown (inside the workspace, no symlink, not on a floor, not hidden, not secret-looking); the rest is 404. 503 `artifacts_unavailable` when the store failed its start-up check |
| `/api/artifacts/<artifact_id>/versions/<int:version>/raw` | GET | artifacts | read (#454, ADR 0020 §6) — the version's snapshot as a **sandboxed document** for the page's `<iframe sandbox="allow-scripts">` / `<img>`: its own `Content-Security-Policy` (`sandbox allow-scripts`, `default-src 'none'`, `connect-src 'none'`, … `frame-ancestors 'self'`) instead of the page CSP, `X-Frame-Options: SAMEORIGIN`, `nosniff`. HTML / image / text kinds only (404 otherwise), never the live workspace file |
| `/api/artifacts/<artifact_id>/labels` | POST | artifacts | **mutate** (#454, ADR 0020 §7) — a person's labels `{labels: [...]}` from the fixed set `stable` / `experimental` / `latest` (400 otherwise). One `audit.artifact.labeled` row (the artifact's opaque id, label codes before / after, version, the server's OS account) is written **before** the change (503 `audit_failed`: nothing changed) and one label-history entry. Refused while an agent tool call runs (409 `tool_call_running`, with YOLO). No agent tool reads or writes labels (the store is on the data-directory floor) |
| `/api/turns`, `/api/turns/<turn_id>/events` | GET | observe | read — served by `chat_api` (turns.db); `create_sse_blueprint` (events.db) has no copy of them, so each path has one handler (#286) |
| `/api/turns/<turn_id>/tools/<tool_use_id>/<kind>` | GET | observe | read (masked pages) |
| `/api/activity` | GET | observe | read — the Stage board's activity mode (#446, `observability/activity.py`): the state now, the recent rows after the cursor `after_id` (the last 20 on the first read), the YOLO window's numbers and the totals since the server started; with `autonomy.stage_c` on also `grant` (W5b: the newest goal grant's codes and counters, memory only; no goal, path or card content). An allow-list of event types, codes, counts, times and tool names; no argument, output, preview, payload, command or reason text |
| `/api/webhooks/<provider>` | POST | integration | **signed**: CSRF-exempt, HMAC (`WEBHOOK_SECRET_<PROVIDER>`) or 401/503; Host/Origin/Sec-Fetch still enforced |
| `/stage`, `/stage/`, `/stage/<asset>` | GET | page | read — **only with `STIRA_STAGE=1`** (off: the routes do not exist, so the inventory above is unchanged). `<asset>` is an allow-list (`stage.css`, `js/*.js`, the bundled three.js, generated `assets/<dir>/*.png|json`). The page reads the APIs above (and `/api/chat/history` for its log) and writes only through the chat page's routes: approvals via `/api/hitl/<request_id>/decision`, chat messages via `POST /api/chat` and a new conversation via `POST /api/chat/session/new` (CSRF, `Idempotency-Key`, the chat page's code in `chat_core.js`). It adds no route and no permission of its own (#341, #350, `tests/test_stage_page.py`) |
| `/api/yolo/status` | GET | YOLO | read — **only with the YOLO allowance** — `external` by default since the [ADR 0012 addendum 2026-10-24](adr/0012-yolo-mode.md) (the button is shown, nothing approved before a press); `STIRA_YOLO_ALLOW=off` or `"yolo": {"allow": "off"}` turns it off (off: the routes of this and the next rows do not exist, so the inventory above is unchanged). Allowance, clock support, the request (an `opened` one only while its window is open; `null` once that window ended, #597), why nothing is offered (no button before phase 5); phase 3 adds `window` (state, tier, end time, remaining seconds), `last_end`, `latches`, `latch_reason`; phase 5a adds to `window` the cap's `used` / `reserved` / `limit`, `auto_approved`, `paused`, `pauses_last_hour`, `resume_in_sec`, `effective_tier`; phase 7 adds `exec_offered`, `caps.exec` and, to `window`, `exec` / `exec_used` / `exec_reserved` / `exec_limit` / `exec_effective` (never a command) |
| `/api/yolo/request`, `/api/yolo/request/cancel` | POST | YOLO | **mutate** — only with the YOLO allowance. The press creates a request that only a person can confirm — in the page (browser channel: the answer carries the one-time token; `.../confirm` below) or on the host (`stira yolo confirm`, with `STIRA_YOLO_HOST_CONFIRM`); phase 3: the confirmation **opens the six-hour window**; phase 5a: while it is active, the Ask of an internal tool in a chat / Stage turn is approved automatically (external tools, exec and every other origin still ask). Phase 7: `{tier: "external", exec: true}` (exactly `true`) asks for a window that also answers `shell` / `batch` — refused (409 `yolo_exec_not_allowed`) unless `STIRA_ENABLE_EXEC_TOOLS` is set and the allowance is `external`; the answer's `confirm.exec` asks the page for a third checkbox. Cancel narrows |
| `/api/yolo/stop` | POST | YOLO | **mutate (narrowing)** — only with the YOLO allowance. Ends everything current: the request (pending or being issued), the window, every open confirmed operation (ADR 0012 §6). Never refused: no confirmation, no breaker check, no running-tool-call refusal, names no id (CSRF / Host / Origin as every mutation) |
| `/api/yolo/unlatch` | POST | YOLO | **mutate** — only with the YOLO allowance. `{window_id}`: unlatches exactly one latch after an abnormal end (§11) as a **confirmed operation** (kind `yolo_unlatch`: 428 first, then the page's checkbox or `stira approvals decide` on the host; bound to the latch, the marker digest and the chain head — a change in between is `stale_operation`); refused while an agent tool call runs. Opens no window. These five, and the settings route below, are the only `/api/yolo/*` routes (`tests/test_yolo_admin_guard.py`) |
| `/api/yolo/settings` | GET, POST | YOLO | **with and without the YOLO allowance** (ADR 0012 phase 7c; the settings page enables YOLO from here). GET (read): every YOLO value — saved in `gateway-config.json` `yolo`, what a start would use and its source (`env` / `config` / `default`), what this process uses, the default, the variable that pins it, `applies` (`restart` / `next_window`), `restart_pending`, `busy`. POST (**mutate**) `{values, base_version[, confirm, acknowledged]}`: the only writer of the section. Refused (nothing written): 409 `turn_running` / `tool_call_running` (the agent's own tools run inside a turn — measured: a save from inside a turn is refused), 409 `yolo_busy` (a request, window or latch exists), 400 field errors (`pinned_by_env`, out of range, `exceeds_cap`, `wildcard_bind`, `env_needs_allow`), 409 `version_conflict`. A **loosening** save: without the allowance 409 `needs_confirmation` until `confirm: true` and `acknowledged` name exactly the loosening codes (the page's dialog, one checkbox); with it the host gate (kind `yolo_settings`: 428, then the browser checkbox or the host). Narrowing saves apply directly. A confirmed loosening writes `audit.yolo.settings_saved` (field names only, never a value) before the file; 503 `audit_failed` (nothing written) when that row cannot be written. The allowance, `offer_exec` and the channel are used at the next start, the caps from the next window. No button, badge or poll comes with it. **Residual:** without the allowance, a program of the same OS user outside the server (an exec child left running after its turn, an MCP server) can send the confirmed save like the page — as for every loosening save here; it takes effect only after a restart and opens no window (a window still needs the press and its confirmation) |
| `/api/host-operations/<op_id>` | GET | YOLO | read — only with the YOLO allowance: the state of a host-confirmed operation the page started (polled by `api.js`) |
| `/api/host-operations/<op_id>/cancel` | POST | YOLO | **mutate** — only with the YOLO allowance; narrows (an open operation ends `cancelled`) |
| `/api/host-operations/<op_id>/confirm` | POST | YOLO | **mutate** — only with the YOLO allowance and the **browser** channel (the default; `STIRA_YOLO_HOST_CONFIRM` unset): the person's checkbox confirmation of an operation (or a YOLO request) their own press just created. Needs the one-time token of that press's answer, `acknowledged: true` (+ `external_acknowledged: true` for tier external, + `exec_acknowledged: true` for a request that includes exec, phase 7), this process's instance reference (16 lowercase hex), `Origin` and `Sec-Fetch-Site: same-origin`, the armed own-dashboard guard and no running agent tool call (ADR 0012 §8b revision 6). With a host channel set: 409 `host_confirm_on_host` |

## Observer dashboard (`dashboard/app.py`)

Same config/tools/csrf/state-history/page routes as above, plus:

| Route | Method | Area | Kind |
|---|---|---|---|
| `/api/state`, `/api/history` | GET | observe | read |
| `/api/stimulus` | POST | observe (diagnostic) | **mutate** (changes NeuralState) |
| `/chat` | GET | page | read (placeholder without a chat agent) |

## What the tests assert

Two tests walk the live `url_map` instead of the table, so a route added later is
covered without being listed: every POST/PUT/PATCH/DELETE rule (whatever its
classification) refuses a missing/wrong token, a foreign `Origin`, a cross-site fetch
and a foreign `Host` before its view runs (the only CSRF exemption is the signed
webhook), and every GET under `/api/` refuses a foreign or other-port `Origin`,
`Origin: null` and a cross-site fetch without a CORS header.

For each **mutate** route and each attack — no token, wrong token, foreign
`Origin`, `Origin: null`, malformed `Origin`, `Sec-Fetch-Site: cross-site`,
foreign `Host` — the response is 403 and the view function is never invoked
(spied). A same-origin request with the token reaches the view (control). For
the signed webhook, unsigned and wrongly signed requests are 401 and no turn
runs. Every route refuses a foreign `Host`. A config POST without the token
leaves the config files untouched.

## Measured boundaries (#148, round 2)

Fixed by the second half of `tests/test_admin_api_permissions.py`. They describe the
current design; nothing here adds authentication.

**Bind (non-loopback refusal).** `resolve_bind_host` accepts `127.0.0.1`, `127.0.0.2`,
`::1`, `[::1]`, `localhost` (any case) and refuses `0.0.0.0`, `::`, LAN addresses and host
names unless `STIRA_ALLOW_REMOTE` is exactly `1` (`0`, `true`, `yes` refuse). The chat server
entry point refuses before `run()` is called. Every `.run(host=...)` in `dashboard/` and
`scripts/` asks `resolve_bind_host()` or names a loopback literal (static check).

**Host header.** The port is ignored and the name must be one of `localhost`,
`127.0.0.1`, `[::1]` (case-insensitive) or a `STIRA_ALLOWED_HOSTS` name. Refused (403):
`127.0.0.2`, `0.0.0.0`, `localhost.`, `127.0.0.1.nip.io`, `[::ffff:127.0.0.1]`,
`evil.example:80@localhost`, an empty Host. `STIRA_ALLOWED_HOSTS` adds exactly the names
listed.

**Origin header** (state-changing routes, token present, Host `localhost:5555`). The
Origin's `host:port` must equal the Host header exactly: another port, `127.0.0.1` for
`localhost`, a different case, userinfo or `file://` are refused (the scheme is #338's).
PUT/PATCH/DELETE need the token before routing (403
without it, 405 with it). No route sends any `Access-Control-*` header, also to `OPTIONS`.

**GET is read-only.** Every GET route of both servers, run for real twice after a warm-up,
leaves every file under the data and configuration directories byte-identical. (Housekeeping
that only narrows — expiring due approvals — can still write when there is something due.)

**Agent scope.** One process serves one agent and no route takes an agent selector;
`/api/memory/tree?agent=…` / `?agent_id=…` still shows only the server's agent. The approval
store is per data directory and lists every record in it; a decision on a request recorded
for **another agent** is accepted (202) but its resume is refused ("the request belongs to
another agent"), the approval is revoked and no tool runs.

**Principal.** An HTTP client cannot claim a system or agent principal: the scheduler's
`actor` becomes `local:<name>` (`system:…`, `agent:…`, `remote:…` → `local:operator`), and a
HITL decision is always recorded as `local:operator` whatever the body says
(`resolver`/`approver` are ignored). Only `local:` principals approve (ADR 0006 §4).

**Constraint (operator decision): the agent can reach its own admin API from the
server side.** The CSRF token stops browsers, not server-side requests: `GET /api/csrf`
answers any client on the same machine that sends a loopback `Host`, including a request
made by the agent's own HTTP tools. Since #495 the agent's tools cannot make that request:
the server arms the own-dashboard guard at every start (ADR 0012 §8b — before, only with
`STIRA_YOLO_ALLOW`), and `http_request` refuses a URL that resolves to this dashboard
(loopback / unspecified / this host's addresses at the dashboard's port; every redirect hop
again) with `E_OWN_DASHBOARD` before anything is sent, whatever its rule — Always included —
and with or without YOLO (`tests/test_own_dashboard_tools.py`). `web_fetch`, `web_search`,
`webhook_send` and `oauth_api_call` already refused loopback, and `page_check`'s browser sends
everything to a refusing proxy. What the table below still describes is a client the guard
does not hold: another process of the same OS user (an MCP server, an exec child, a shell).
Without #495 the tool rules alone kept the agent out:

| Tool | New-install rule | What it could do against the admin API |
|---|---|---|
| `http_request` | **Never** | POST with the token: change settings, tool rules (a loosening still needs `confirm` and a NORMAL breaker), decide approvals |
| `web_fetch`, `web_search` | **Ask** (a person sees the URL) | GET only: read the token and every read route; GETs change no file (above) |
| exec class (`shell`, …) | hidden unless `STIRA_ENABLE_EXEC_TOOLS`, then Ask | anything a local client can do |

**Why a request without `Origin` still passes (#495, not changed).** `Origin` and the CSRF
token are browser defences: a browser always sends `Origin` on a cross-site POST, so a page on
another site is refused. A local non-browser client (curl, `stira_ctl.sh`, the probe scripts
under `scripts/`) sends none, and requiring one would break those while stopping nothing — any
such client can write the header it likes. The agent is kept out by the guard above instead.

**Conversation reset (`/api/chat/session/new`, #350) is also held by the turn lock.** The
agent's tools run inside a turn, and the route refuses while a turn holds the lock (409
`turn_running`), whatever the tool rules say. Measured on the chat server (offline scripted model):
`http_request` set to **Always**, the CSRF token in its headers and the upstream terminal
confirmation bypassed (`BYPASS_TOOL_CONSENT=true`, the worst operator setting before #547) —
the tool result was `Status Code: 409` / `turn_running`, the history kept its turns and no
audit row was written. With the defaults `http_request` is Never, and since #547 a POST,
PUT, PATCH or DELETE request runs only after a person approved that exact call (Always or a YOLO approval is
refused with the reason; `BYPASS_TOOL_CONSENT` is ignored). What remains is the exec class (hidden by default): a process it
leaves running after the turn is "anything a local client can do", as for every route here.

An operator who sets `http_request` (or an exposed exec tool) to **Ask** lets the agent
propose such calls (each one shown to a person); **Always** lets it make them without a
person. Do not set these to Always on a server whose admin API matters. Closing it
needs a design decision (refuse loopback/private destinations in the agent's HTTP tools,
or a capability the agent cannot read); nothing is changed here.

**With the YOLO allowance (ADR 0012 phases 1–2; the default since the addendum of
2026-10-24).** Four extra refusals, none with the allowance `off`
(`tests/test_yolo_default_unchanged.py`: same tool specs, prompt and API responses; the
default matches `external`):

- *Own dashboard.* The server process refuses every TCP connection to its own listen port
  on a loopback, unspecified or own address — checked at `socket.connect` (an audit hook),
  so redirects, DNS names that resolve to loopback and every in-process client
  (`http_request`, `web_fetch`, `web_search`, `rss`, an in-process MCP HTTP client) are
  covered (`tests/test_yolo_own_dashboard.py`). The guard evaluates only built-in
  immutable values — exactly `str` / `bytes` hosts, exactly `int` ports and the C-level
  tuple items — because the socket converts the address *before* the audit event and a
  caller's `__index__` / `__str__` / `__eq__` could answer differently a second time.
  Any other host or port object (`bytearray`, `memoryview`, `ipaddress` objects, `bool`,
  numpy-style integers, subclasses) is treated as possibly local / possibly the
  dashboard's port and refused unless a known part shows another port or another host;
  a host **name** handed to `connect()` for that port is refused without a lookup.
  Only that port: local model, embeddings or MCP servers on other loopback ports, called
  with plain `int` ports as every HTTP client does, are untouched (tested). **The listener must be on one local address**:
  a wildcard bind (`0.0.0.0`, `::`) with an explicit allowance refuses start-up (exit 2; the
  default allowance is `off` there) —
  every interface would reach it, and the guard knows only the bind address and the host
  name's addresses. MCP servers running as their own processes,
  exec children and other programs of the same OS user are **not** held by it.
- *Reserved keys.* A state-changing request under `/api/config`, `/api/tools` or
  `/api/scheduler` whose JSON body names `yolo*` (at any depth, also as a dotted hub field)
  is refused with 409 `yolo_not_settable` after the checks above and before the view.
- *Running tool calls.* Approve / edit decisions, `RECOVER`, loosening tool-rule, hub and
  rollback saves and scheduler create / resume / confirmed update are refused with 409
  `tool_call_running` while an agent tool call is running. This holds a tool calling the
  admin API during its own call; it is **not** a proof that a person sent the request (a
  delayed delivery after the call passes). Reject, respond, pause, cancel and tightening
  saves are never refused for it. For the loosening operations the human channel is the
  confirmation below; approve / edit are one press (phase 2c) and have only this layer
  besides the decision's own `args_hash` binding.
- *Confirmation channels (ADR 0012 §8b revision 6).* The default is the **browser**: the
  428 answer carries a one-time token (only there), the page shows the operation in a
  fixed format (enums and short codes from the server, never agent text) in the shared
  confirmation dialog and asks for a tick (「内容を確認しました」 / "I have checked this";
  for a YOLO request of tier external a second box; phase 2c, replacing the typed words of
  2b; the confirm button is disabled until every box is ticked), then posts
  `/api/host-operations/<id>/confirm`. A new press replaces any open operation, so only
  the one the person just created can be confirmed. Layers, none of which proves a
  person: the token, the tick, the instance reference, `Origin` + `Sec-Fetch-Site:
  same-origin` (a non-browser client can forge both), the own-dashboard guard (the
  agent's in-process HTTP tools cannot reach the route), the running-tool-call refusal,
  and the exec tools staying out of every YOLO window. **Residual:** a program of the same
  OS user outside the server (a stdio / HTTP MCP server, a shell, an exec command a person
  approved) can read `/api/csrf`, create an operation and confirm it like the page — and
  decide a pending approval card in one press, as without the allowance. Setting
  `STIRA_YOLO_HOST_CONFIRM` (`auto` / `command` / `dialog`) makes the host required and
  the browser confirmation refused (`host_confirm_on_host`) — the dialog's OS
  authentication, when that dialog is the channel actually selected at start-up (not a
  `dialog` / `auto` setting that fell back to the command), is the only channel such a
  program cannot answer.
- *Host confirmation (phase 2, opt-in since revision 6).* The same loosening operations —
  `RECOVER`, loosening tool-rule, hub and rollback saves, scheduler create / resume /
  confirmed update (approve / edit of a card were in this list until phase 2c) — apply **only** for a request naming an operation a person confirmed
  (in the browser, or on the host when `STIRA_YOLO_HOST_CONFIRM` is set). The first request is answered 428 `host_confirmation_required` with a
  one-time operation (128-bit id) bound to the operation kind, a digest of the route and
  JSON body, a digest of the target's state (the rules, the hub values, the job)
  and the process incarnation; nothing is applied. The person confirms it on the host —
  `stira approvals decide <id>` on a TTY with a typed word, or the macOS dialog with an OS
  authentication (`STIRA_YOLO_HOST_CONFIRM`) — and the page (`api.js`, Stage's approval
  client, for a 428 it may still receive) sends the same request again with `X-Stira-Host-Operation: <id>`. Refused:
  not yet confirmed (`host_operation_pending`), denied, cancelled, expired (2 min to
  confirm, then 30 s to apply), another binding (`host_operation_mismatch`), a changed
  target (`stale_operation`), a second use (`host_operation_used`), an unknown id or an
  operation of an earlier process (`host_operation_unknown`); more than one open operation
  or 12 per 10 min (browser: 30 per 10 min; a new press replaces the open one) → 429
  `host_rate_limited`. `/new` ends every open operation (a confirmed one not yet applied is
  then `host_operation_cancelled`) and fences the YOLO request; a restart forgets them. A delivery after the agent's tool call ended
  only creates an operation a person must confirm (`tests/test_yolo_host_gate.py`). A client
  the own-dashboard guard does not hold (an MCP server, another process with the CSRF
  token) is held to that **only on the host channel with the OS-authenticated dialog
  actually selected**; on the browser channel (default) and with the host command it can
  confirm the operation it created (the residual above, ADR 0012 §8a / §8b). The host channel is a Unix socket (`0600`, in a
  `0700` directory) whose **kernel-reported** peer must be the server's OS user and
  neither the server process nor one of its descendants (stdio MCP servers, exec children);
  a process of the same user outside that tree can still reach it (ADR 0012 §8a residual —
  only the dialog's OS authentication or another OS user holds against it). Reject,
  respond, pause, cancel, stop and tightening saves never need the host.

## Open items (not fixed here)

- **Permission tiers.** Observe / configure / approve / memory are not separate
  permissions: the token holder can call every mutate route. Needs an authentication layer
  and the principal model (#276).
- **Agent loopback reach** (the constraint above).

## Fixed with these tests

- A malformed `Origin` (e.g. `http://[::1`) made `urlsplit` raise inside the
  security hook: HTTP 500 with a stack trace instead of an audited 403.
- Refusals were not recorded anywhere; they are now audited without secrets.

# Autonomy (alpha): overview, operation, known limits

English | [日本語](autonomy_ja.md)

> **ALPHA — experimental, off by default.** Everything on this page may change without
> notice. Do not leave it running unattended on anything you cannot lose. It is the
> grant-scoped goal loop of [ADR 0011](adr/0011-agent-self-scheduling.md) ("stage C"),
> tracked in #296. This page is the overview; the full rules are in [grants](grants.md),
> [authority-service](authority-service.md) and [stage](stage.md).

**In one paragraph.** A person issues a **grant** for **one goal**: a time limit, a token
budget, an iteration limit, a tool scope and one working directory. Issuing it runs nothing.
Each **iteration** of the goal runs in its own session (`grant:<id>`), separate from the
chat, either when the person presses **Run one iteration** or — only if both the server and
that grant allow it — when the scheduler's heartbeat check starts one. The agent can report
progress, report a blocker, or *claim* the goal is met; **only the person can mark it
complete**. Any narrowing (kill switch, revoke, breaker leaving NORMAL, `/new`, the TTL, a
restart) stops it.

## 1. What exists today (W1–W6d on `main`)

| Wave | What it adds | Detail |
|---|---|---|
| W1 | **Authority service**: one per server process — an incarnation id, single-use permits bound to it, a persisted authority **epoch** that advances on every narrowing, a lock-free **kill** flag, an in-flight set with cancel callbacks | [authority-service](authority-service.md) |
| W2 | **Latches** (`kill`, `unhealthy`; only a person clears them, with the server stopped), codes-only **lifecycle events** with an unread / ack **outbox**, a model-free **watchdog** (every 15 s), the kill route | [authority-service](authority-service.md#latches-authority-latches-w2) |
| W4 / W4.5 | **Grants and goals**: the store `grants.json` (`0600`, chained records, a live grant is never stored as active), the grant / goal **state machine**, the **human control plane** (issue / pause / resume / revoke / verify, host-confirmed loosenings) | [grants](grants.md#human-control-plane) |
| W5 | **The supervised iteration executor**: one iteration per press in a dedicated child session; one permit per effect (model call, tool call, approval wait, report); budget reserved before each provider request; an Ask is answered only by a person's card | [grants](grants.md#the-goal-iteration-executor-w5) |
| W5b | **Stage whiteboard display**: the activity mode shows the newest grant, read-only | [stage](stage.md#the-goal-grant-on-the-board-adr-0011-stage-c-w5b-of-296) |
| W6a | **Outcome-based progress and loop detection**: progress is decided from what an iteration changed (a keyed fingerprint of the files the grant wrote and the check results), not from activity; no model-based judge | [grants](grants.md#progress-by-outcome-w6a) |
| W6b | **Pre-emption by every turn holder**: every taker of the shared turn lock (a chat message, a webhook turn, the automatic continue, a restored card's resume, `/new`, a procedure approval) signals first; a running iteration yields | [grants](grants.md#known-limits) ("Other turns") |
| W6-H | Hardening before unattended runs (one-step validate-and-count, oversize files, the turn-lock enumeration test) | [CHANGELOG](../CHANGELOG.md) |
| W6c | **Claim evidence**: at a completion claim the harness records codes and counters it measured (checks, workspace changed, progress class, tokens / TTL at the claim) — never model text; a "still waiting for you" cue | [grants](grants.md#completion-claims-evidence-and-the-waiting-state-w6c) |
| W6d | **Heartbeat-driven start**: the scheduler's model-free `grant_tick` may start **at most one iteration per wake** through the same `step` as a press — only with the gateway switch **and** the grant's own flag | [grants](grants.md#heartbeat-start-w6d) |
| W6e / W6-F | **A supervised real-model smoke** (one run, 2026-10-26) and the fixes it led to (#640–#645): a stop during a model call is charged the call's reported usage when it completes; a workspace venv is read for its own `pytest`; the tools view shows `run_checks` as `os_sandbox` on a verified host; a stopped iteration counts the same whatever stopped it; the heartbeat spacing also counts from a resume | [grants](grants.md#known-limits), §4 below |

Not built: stages A and B of ADR 0011 (proposals, self-scheduling), a verifier other than
the person, YOLO auto-approval inside an iteration (W7), taint / episodic origin for
iterations (W8), a shared day budget pool. See [Roadmap](#5-roadmap).

## 2. How to try it, step by step

Use a throwaway working directory. The labels below are the English UI labels
(`dashboard/static/i18n/catalog.en.json`); the Japanese UI shows the same items.

1. **Host confirmation (the default).** Issue, resume and verify must be confirmed **on the
   host**: set `STIRA_YOLO_HOST_CONFIRM=auto` (on macOS the OS-authenticated desktop dialog),
   `dialog` or `command` (then confirm with `stira approvals decide <short>` on a TTY; `auto`
   falls back to the command outside a macOS desktop session), and
   leave the YOLO allowance on (`STIRA_YOLO_ALLOW`, default `external`; `off` removes the
   channel). The host channel needs a POSIX host (not Windows). Without a host channel these
   operations are refused (`host_channel_required`). The weaker browser confirmation is
   accepted only with `"allow_browser_confirm": true` **and** no host channel configured
   (step 4; see the [limits](#4-known-limits)).
2. **Switches.** In `.local/stira/gateway-config.json` (read at start-up; hand edit only):

   ```json
   {"autonomy": {"stage_c": true, "stage_c_exec": true}}
   ```

   `stage_c` alone gives the control plane (issue, stop, verify) but runs nothing;
   `stage_c_exec` adds the executor and the **Run one iteration** button. Restart the server.
   All keys: [configuration](configuration.md#the-autonomy-section-adr-0011-stage-c-alpha).

   **Heartbeat starts (optional, all of these):** (a) `"heartbeat_start": true` in the same
   `autonomy` section (it needs `stage_c_exec`); (b) the scheduler on, `STIRA_SCHEDULER=1`;
   (c) the heartbeat daemon on — gateway `hb_enabled: true` or `STIRA_HEARTBEAT=1` (off by
   default; `STIRA_HEARTBEAT=0` overrides `hb_enabled: true`); (d) restart, and check the
   server's console (stderr; a logging warning) has no
   `grants: heartbeat_start unavailable (reason=scheduler_off | heartbeat_off)` line — it is
   decided once at start-up; a heartbeat that fails later shows as `heartbeat_unhealthy`
   skips on the grant card;
   (e) issue the grant with **Let the heartbeat start iterations** ticked (step 4). Without
   the scheduler nothing wakes the ticker; without the heartbeat every wake is skipped
   (`heartbeat_unhealthy`); without the per-grant flag the grant is never started by a tick.

   **Tool rules for heartbeat-started iterations.** `editor` and `run_checks` are **Ask** by
   default, and nobody answers a card in a heartbeat-started iteration: it ends
   `approval_timeout` at its first write (the goal blocks, the grant pauses; in the smoke this
   took 1.3 s and about 2.7k tokens). For unattended runs set them to **Always** first, in the
   settings page:
   1. Settings › Safety › **Tool permissions** (`/settings#sec-tools`): choose **Always** for
      `editor` (and `run_checks`). Choosing it sends nothing yet.
   2. Press **Save** (at the bottom of the page).
   3. The dialog "This change lowers safety" opens: type `ALLOW` ("Type "ALLOW" to continue")
      **and** the tool names ("Type the tool names being made Always: editor"; for both
      tools `editor run_checks`), then press **I understand — save** (it stays disabled until
      both match).
   4. With the YOLO allowance on (the default), the save is a host-confirmed `rules_loosen`
      operation: with the browser channel a second dialog opens — tick "I have checked this;
      it is what I just pressed" and press **Confirm and apply**; with
      `STIRA_YOLO_HOST_CONFIRM=command` / `dialog` the page says where to confirm — run the
      shown `stira approvals decide <short>` in a terminal of the server's computer (check the
      code, type the word) or confirm in the desktop dialog. Only then is the rule saved.

   From the API (one tool per request): `POST /api/config/tools {"name": "editor", "policy":
   "always", "confirm": true, "confirm_always": ["editor"]}` (without `confirm` or the typed
   name: 400) answers 428 `host_confirmation_required` with the `rules_loosen` operation
   (`operation.id`, `short`, `channel`, and `command` for the command channel); confirm it on
   the host as above (`stira approvals decide <short>`), or for the browser channel `POST
   /api/host-operations/<op id>/confirm` from the dashboard page, then send the same request
   again with `X-Stira-Host-Operation: <op id>`. No rule can be loosened while the breaker is
   not NORMAL. A saved Always for `run_checks` is effective only on a verified `os_sandbox`
   host (elsewhere it is read as Ask). Pressed iterations need no Always: you answer the
   cards (step 5).

   **`run_checks` in a grant (optional):** the tool must be registered (gateway
   `{"run_checks": {"phase": 1}}`, off by default, not on Windows or as root;
   `STIRA_RUN_CHECKS=0` forces it off),
   `autonomy.run_checks_unattended` must stay `true` (the default) and the host must have a
   verified `os_sandbox` (macOS). Otherwise a grant whose tools include `run_checks` is
   refused at issue (`grant_run_checks_setting_off` / `grant_run_checks_not_contained`); a
   downgrade later blocks the goal (`blocked(run_checks_unavailable)`). **pytest must be
   importable by the interpreter `run_checks` picks:** `<folder>/.venv` (or `venv`) of the
   checked folder, then the same in the agent's workspace folder (`workspace_dir`), then the
   server's own Python. Install pytest there (into the venv's
   own `site-packages`, or into the base interpreter of a venv made with
   `--system-site-packages`); otherwise the model sees `pytest_not_installed` and usually
   reports `blocked(needs_decision)`. A venv made with `--system-site-packages` sees the base
   interpreter's packages only when that base is the server's own base interpreter; another
   base is not read (unknown: pytest is offered only when the folder has a pytest config file,
   `pytest.ini`, `conftest.py` or `[tool.pytest.ini_options]` in `pyproject.toml`). Presence is read from files: a pytest installed only through a
   `.pth` path (an editable install) is not seen (`pytest_not_installed`). `stira checks
   discover <folder> --workspace <workspace_dir>` lists what it found (nothing is run; without
   `--workspace` the folder itself is taken as the workspace, so a venv in the workspace folder
   is not shown). Before W6-F a venv whose `python` is a symlink to the server's base
   interpreter was answered with the server's own packages (#641).
3. **Provider.** An iteration runs only on **Amazon Bedrock** (the executor guards each
   terminal request of Bedrock's client; the test suite's fake models aside); any other
   provider is refused (`provider_unsupported`).
4. **Issue a grant.** Settings → **Goal grants (stage C)** (`/settings#sec-grants`) →
   **Issue a new grant**: *Goal* (no secrets — a goal that masking would change is not
   saved), *Tools it may use* (a subset of `file_read`, `editor`, `run_checks`,
   `memory_read`, `memory_search`, `memory_list`, `current_time`), *Working directory* (an
   absolute path inside the write roots), *Total token budget* (form default 2,000,000, cap
   5,000,000), *Limit per tick* (form default 200,000), *Lifetime* (minutes, form default 120,
   at most 360), *Maximum iterations* (default 10, at most 20), optionally *Let the heartbeat
   start iterations* (shown only with the gateway switch on; it cannot be changed later).
   Type `ISSUE` and press **Issue**. With the host channel (the default) the page then shows
   "Confirm operation <short> on the host (stira approvals decide <short>, or the desktop
   dialog)" and waits: confirm there. **Browser flow (weaker, optional):** a configured
   host channel always takes precedence — `allow_browser_confirm` only *permits* the browser
   confirmation, it does not select it. To use it, select the browser channel
   (`STIRA_YOLO_HOST_CONFIRM` unset or `browser`, and `yolo.host_confirm` `"browser"`, its
   default; or the YOLO allowance off, `STIRA_YOLO_ALLOW=off`, **with
   `STIRA_YOLO_HOST_CONFIRM` and every other `STIRA_YOLO_*` override variable unset** —
   `STIRA_YOLO_MAX_AUTO_APPROVALS`, `STIRA_YOLO_MAX_EXTERNAL`, `STIRA_YOLO_MAX_EXEC` —
   otherwise the server refuses to start: `STIRA_YOLO_HOST_CONFIRM is set but the YOLO
   allowance is off`), set
   `"allow_browser_confirm": true` in `autonomy`, and restart. Then pressing **Issue** opens
   a browser dialog where you tick **I checked what this is** and press **Issue** (no host
   step). With the browser channel and without `allow_browser_confirm`, issue / resume /
   verify are refused (`host_channel_required`). The grant is `active`; nothing runs yet.
   **API clients:** the scripted flow (the 428 answer, `POST
   /api/grants/operations/<op>/confirm`, the same request again with
   `X-Stira-Host-Operation`) is in [grants](grants.md#human-control-plane).
5. **Run one iteration.** Press **Run one iteration** on the grant card. The iteration runs
   in the background; if a tool call needs approval, a card waits on the approvals page
   (**Approvals**) — answer it there. An unanswered card expires after the agent's
   `hitl_timeout_sec` (default 120 s; never later than the iteration's own wall time), which
   ends the iteration and blocks the goal (`blocked(approval_timeout)`). With `editor` and
   `run_checks` on Ask, a typical iteration asks for **3–4 cards** (an editor view, the
   writes, a `run_checks` run; measured in the smoke). An API client approves with the
   card's `args_hash` (without it the answer is refused with 400 and the card waits until
   it expires).
6. **Read the board.** Open the Stage (`STIRA_STAGE=1`, `/stage/`) and its **Activity** mode:
   the "Goal grant" panel shows the state, *Iteration n / max*, time and budget left, the
   last end in words, the last five iterations and the cues ("Waiting for your approval",
   "A completion is claimed: waiting for your verification"). It is display only; the
   controls stay in Settings. The settings card shows the same plus the controls.
7. **Claim and verify.** When the agent claims the goal is met, the goal is
   `awaiting_verification` and the card shows the evidence the harness measured
   ("Checks at the claim", "Workspace", "Progress of the claiming iteration", a "⚠" line if
   the checks failed). Look at the files yourself, then press **Confirm complete** or
   **Reject** and confirm (on the host, as in step 4). Confirm ends the grant
   (`ended(complete)`). Reject adds one to the goal's no-progress streak (not to the
   iteration count): the goal returns to `active` unless the streak reaches the grant's
   `no_progress_ticks` (default 3), in which case the goal is `blocked(no_progress)` and the
   grant pauses with it. The model is **not told why** it was rejected (no reason travels to
   the next iteration; #644): in the smoke it claimed again with the same evidence, which two
   rejects bring within one step of `blocked(no_progress)`.
8. **Pause / resume / revoke.** **Pause** and **Revoke** are one press. **Resume** needs the
   host confirmation; a halt (the breaker left NORMAL) also needs the typed word `RESUME`
   and the breaker back at NORMAL. Revoke is final: issue a new grant to continue.
9. **Kill switch.** **Kill switch (stops everything)** revokes every grant in memory and
   refuses every new permit at once (an effect already running finishes and is recorded).
   A running iteration is cancelled and ends at its next boundary. The kill is then
   **persisted** as the `kill` latch, so the installation stays killed across restarts until
   a person clears it with the server stopped — `stira authority unlatch kill` (TTY, typed
   word `UNLATCH`) — **once the write has landed**. The kill answer says `latch: set` (or
   `already_set`) when it did; afterwards `GET /api/authority/status` shows `"kill"` in
   `latches` with `latch_store_healthy: true` (or run `stira authority status` on the host).
   A kill answered `latch: pending`, `timeout` or `failed` is in memory only until that shows,
   and a restart before the write lands can come up not killed (a documented limit; press the
   kill again after a restart if the status does not list it). See
   [authority-service](authority-service.md#operator-procedure-kill-and-unlatch).
10. **Restart.** Every restart revokes every grant (`revoked(restart)`); issue a new one.

## 3. What the harness guarantees — and what it does not

**Guaranteed (each pinned by tests; fail closed; subject to the residuals in
[§4](#4-known-limits) — the final instruction window and same-user processes above all):**

* Off by default, twice for the heartbeat: with the switches off there is no route, no
  thread, no file, and the API answers are as before.
* Nothing runs without a grant a person issued with a host confirmation (unless they chose
  the weaker browser layer); at most one live grant per server.
* TTL default 2 h, hard cap 6 h, never extended; iterations default 10, cap 20; the token
  budget (total ≤ 5,000,000, per tick ≤ total) never resets; a request that does not fit
  is not sent.
* **One permit per effect**: every model call, tool call, approval wait and report takes a
  permit bound to the authority epoch and the grant, checked again right before the I/O.
  Any narrowing — kill, revoke, pause, breaker leaving NORMAL, `/new`, TTL, clock anomaly,
  unhealthy store — refuses the next effect.
* **Pre-emption**: any turn-lock holder (a person's chat turn first of all) makes a running
  iteration yield at its next boundary, and no iteration is admitted while a turn runs.
* **No approval is answered automatically**: in a pressed iteration an Ask is answered only
  by a person's card (no YOLO, no allowance); an unattended (heartbeat) iteration never
  waits — it ends `approval_timeout` and blocks the goal.
* The tool set is the grant's scope ∩ a fixed read / edit / check list ∩ the live rules
  (Never stays absent); no shell, web, delegate, MCP or scheduler tool. The editor is
  confined to the working directory (checked before the permit and again after the consume).
* `run_checks` only on a host whose OS sandbox (`os_sandbox`) is verified, re-confirmed at
  each call; on a `limits_only` host a grant naming it is refused.
* **Only the person completes a goal.** A claim moves the goal to `awaiting_verification`;
  the evidence is codes measured by the harness, never model text; nothing confirms or
  rejects on its own.
* Where the goal text goes: it is **stored** only in `grants.json` (`0600`) and in the
  server's memory; it is **shown** once, to the issuer only (`GET
  /api/grants/<id>/goal?confirm=SHOW` with the one-time view token of the issue answer —
  "Show the goal"); and it is **sent to the model** at the end of every model call of an
  iteration (the trailing context block, after the trusted playbook). It never reaches the
  logs, lifecycle events, the outbox, the other API views (`/api/grants`, `/api/activity`),
  the approvals database, checkpoints, the chat session or the Stage board — those carry ids
  and codes only.
* No catch-up: a missed heartbeat wake is not replayed; a restart ends every grant.

**Not guaranteed:**

* That the work is correct or useful. Progress detection tells "changed" from "unchanged",
  not "better"; an irrelevant edit counts as new. Your verification is the only judge.
* Exclusivity in the working directory: scheduled jobs, other processes and you can write
  there at the same time.
* Protection against another process running as the same OS user (see the limits).
* A bounded cost per iteration. One supervised smoke run measured a range ([§4](#4-known-limits));
  your goal, model and repository decide the real cost.
* Durable stops beyond the kill latch: grants are in memory and end at restart by design.
* Stability of names, routes, settings or stored formats (alpha).

## 4. Known limits

Collected from [grants](grants.md#known-limits), [authority-service](authority-service.md#known-limits-w45)
and the review rounds of #296; open items are tracked in #618, #620, #623, #636 and #638.
None widens authority; each fails closed or is a display / latency issue unless stated.

* **Final instruction window.** A stop (heartbeat loss, scheduler stop, a person's turn,
  TTL) that lands after the last check before an HTTP request leaves is not
  seen by that request; it is seen at the next boundary. Accepted residual (#638).
* **A stop right after a record is stored.** A narrowing that lands immediately after a
  claim's evidence is installed is a later event: the record keeps what was true when it
  was installed. A stop in the last instant between the lock-free kill / turn read and the
  store counts as after it (the same accepted instant as the W1 fenced commits).
* **Heartbeat is a start condition only.** After an iteration's first request, a heartbeat
  loss does not stop it (the other stops still do).
* **Calls in flight are not interrupted.** A provider call already sent, or a `run_checks` /
  `editor` call already running, finishes. After a stop the iteration waits for a running
  model call (0.5 s, plus up to 5 s while it reads its response; not for shutdown or the
  wall time): a call that completes is charged its reported usage (W6-F, #640). A call still
  running then is *abandoned* (its provider request told to stop, its answer discarded, its
  **whole reservation** charged, no new iteration while it lives — bounded only by the
  client's own timeouts).
* **Loop detection limits.** A cycle over **more than 256 files** is not guaranteed to be
  detected (older paths leave the fingerprint). Files over **64 MiB** are identified by
  size plus a 3 MiB sample, so a same-size change outside the sample is not seen. At most
  256 MiB are digested per iteration (more → unknown, no progress).
* **Same-user processes are out of scope.** Another process of the same OS user can edit
  `grants.json`, the settings or the working directory (e.g. rename it between the last
  check and a write). The dashboard's CSRF / Origin checks cannot tell a same-user API
  client from the person — this applies to every route, including "Run one iteration",
  approving a card and editing a setting.
* **Host confirmation vs browser confirmation.** With `allow_browser_confirm: true`, a
  same-user process can drive issue / resume / verify (every proof is handed to the
  requesting browser). The host channel is the stronger layer. A grant with
  `heartbeat_start` can currently be issued through the browser layer too (#638).
* **The live approval card shows the arguments** (path and content included) on the normal
  pending view, as every approval card does; the stored record keeps codes only.
* **No prompt caching** for the iteration's model call; the trusted context block costs
  about 355 tokens per call by Stira's estimate (not a tokenizer measurement), the goal up
  to about 500 more. In the W6e smoke (one supervised run on Sonnet 5.5 via Bedrock, 2026-10-26; estimates, not a bill) no cache counter
  (`cacheReadInputTokens` / `cacheWriteInputTokens`) was reported for any call (also
  of the second iteration) — with no caching in the iteration's call path, read as every call
  being a full uncached prompt — and the history is sent again on
  every call of an iteration (a 5-call iteration sent about 18.2k input tokens).
* **Cost: one smoke run, an estimate.** In the W6e smoke (one supervised run on Sonnet 5.5 via Bedrock, 2026-10-26; estimates, not a bill): a normal
  iteration made 1–7 model calls and was charged **2.7k–28k tokens** in **1.3–24 s**; the
  first call of each iteration was 2,619 in / 68 out; about 96 % of the tokens were input.
  Before W6-F a stop during a model call was charged the whole reservation, about **71–74k
  tokens**, for a call that used about 2.7k (#640). The budget bound stays conservative
  (about 4× Stira's estimate in tests).
* **Bedrock only.** Other providers are refused (`provider_unsupported`).
* **No day budget pool.** Only the grant's total and per-tick limits apply; the
  scheduler's day pool is not shared with grants.
* **YOLO two-binding and taint persistence are not implemented** (W7 / W8): no automatic
  approval in an iteration, no taint or episodic origin carried for iteration output.
* **`run_checks` needs macOS.** The verified `os_sandbox` exists only on macOS
  (`sandbox-exec`, after a passed self-test); on Linux (`limits_only`) a grant naming
  `run_checks` is refused, and Windows has no `run_checks`.
* **Clocks.** A wall-clock step of more than 5 s against the sleep-including clock (for
  example an NTP step) revokes the grant (`clock_anomaly`); a host without a separate
  sleep-including clock treats a sleep as a jump. A forward wall-clock jump can let the
  first heartbeat start come early (#638).
* **Repeated-failure gate.** Alternating `failed` and `no_action` ends get around the
  heartbeat's repeated-failure skip (the no-progress rule still blocks the goal, #638).
* **A kill is durable only once its latch is written** (in-process `kill_durable: true`;
  from outside: `"kill"` in `latches` of `GET /api/authority/status` with
  `latch_store_healthy: true`). A crash before the latch write lands can come back unkilled;
  press the kill again after a restart if the status does not list it.
* **UI**: the settings cards are rebuilt on every poll (#620).

## 5. Roadmap

Planned, **not on `main`**:

* **Follow-ups of the W6e smoke** (it ran on 2026-10-26; §4 has its numbers; its findings
  are #640–#645): a fixed reject reason code carried into the next iteration (#644 U2) and
  no empty `<data>/workspace/` folder when `workspace_dir` points elsewhere (#644 U4,
  cosmetic).
* **W7** — the YOLO two-binding consume (whether and how an approval inside an iteration can
  be covered by a YOLO window).
* **W8** — taint and episodic origin for what an iteration produces.
* **Day budget pool** — a shared daily budget across grants and scheduler jobs; after the
  public release.
* The follow-ups in #618, #620, #623, #636 and #638.

Stages A and B of ADR 0011 (proposals and bounded self-scheduling) need their own decision.

See also: [guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md) — switching on the scheduler and the heartbeat, checking and stopping them.

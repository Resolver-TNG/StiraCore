# Authority service (ADR 0011 §5, §12; W1 + W2)

**Status:** implemented as the common base of ADR 0011 §13 item 2 (part of it): W1 (the
service, the persisted epoch) and W2 (the latch store, lifecycle events and the outbox,
the model-free watchdog, the kill switch route). It starts no work: nothing new can run
because of it. The goal grants and the goal iteration executor (stage C, ADR 0011, **alpha**,
off by default) are built on it — see [grants](grants.md) and the overview
[autonomy](autonomy.md); nothing sets a `halted:*` latch yet (a grant never survives a
restart). Japanese: [authority-service_ja.md](authority-service_ja.md).

## What it is

One `AuthorityService` per server process (`src/stiracore/authority/`). It is the single
authority boundary that every subsystem shares:

| Part | What it does |
|---|---|
| Incarnation | 128 random bits per process (`stiracore.authority.incarnation`); the heartbeat's `INCARNATION` and the YOLO desk use the same value. Never written, never read back. |
| Permits / `consume` | One permit per effect, bound to incarnation, kind (`goal` / `yolo`), grant id, generation, epoch and an audit receipt; consumed exactly once. Optional bindings: policy version, budget reservation id, a TTL pair (wall + monotonic deadline) — the W5 goal iteration executor binds the TTL pair and (model calls) the reservation id ([grants](grants.md)). |
| Authority epoch | Monotonic. It moves on every narrowing: process start, a tool-rules save, a breaker change away from NORMAL or between non-NORMAL levels, the kill switch, and YOLO's own moves (pause, end, Stop, `/new`, …). A permit issued before a move is refused (`epoch_moved`). |
| Kill flag | Set first, without any lock, and read first by `consume`; then the epoch advances. It takes effect while other threads hold any lock. Then the `kill` latch is persisted (W2): the installation stays killed after a restart until a person clears it. Set by `POST /api/authority/kill`; with stage C on, the button "Kill switch (stops everything)" in Settings → Goal grants sends it. |
| In-flight set | An effect may register a cancel callback at `consume`. A later narrowing takes the set under the lock and calls the callbacks after releasing it; an effect that ends after the stop is recorded as `completed_after_stop`. Bounded (64). A callback that runs longer than 5 s is abandoned (W2, below). |
| Unhealthy | Fail closed: while set, every permit and `consume` is refused (`authority_unhealthy`). The reason code is in `AuthorityService.status()`. |

The YOLO desk receives the service by injection (`yolo.runtime.install_host(authority=…)`);
its automatic approvals use these permits. With YOLO off the service still exists, and a
tool-rules save or a breaker change still moves the epoch.

## The `authority-epoch` file

In the data directory, mode `0600`, written with a flush (`F_FULLFSYNC` on macOS), an
atomic rename and a directory flush. It keeps the newest 16 records; each has `seq` (one
per write), `epoch`, `prev` (hash of the previous record) and `hash`. At start the epoch is
the stored value + 1 (written before anything uses it); **a missing file is a first start**
(epoch 1). Epoch values are not unique across starts: writes after a narrowing are
deferred and coalesced, so values handed out but not yet written when the process dies
(for example a `SIGKILL`) can be handed out again by the next process. Identity across
starts is therefore always `(incarnation, epoch)`, never the epoch alone.

The file is read without following a symlink and without blocking (a FIFO), and the
opened descriptor is checked again (regular file, this user, mode `0600`, one link, the
same device and inode as the path). Before every replace the current file is read again
and must be exactly the one this service wrote last; otherwise nothing is written.

| Reason code | Meaning |
|---|---|
| `epoch_store_unsafe` | not a regular, singly linked file of this user with mode `0600` (e.g. a symlink, `0644`) |
| `epoch_store_unreadable` | the file cannot be opened or read |
| `epoch_store_malformed` | not the format |
| `epoch_store_hash_mismatch` | a record's hash or a `prev` link does not match |
| `epoch_store_sequence_gap` | a `seq` is missing or repeated |
| `epoch_store_not_monotonic` | an epoch does not grow |
| `epoch_store_replaced` | the file was replaced, changed or removed behind the running service |
| `epoch_store_write_failed` | a write failed (disk full, permission, …) |
| `epoch_settle_unavailable` | the settle worker for deferred writes could not start (the work stays queued) |
| `epoch_settle_failed` | a deferred settlement failed |

A broken file is never overwritten, and a later successful write does not clear the
state: the process stays unhealthy until a person looks. **Repair** (only for a damaged
*epoch file*; a broken or stale latch store is repaired with `reset-latches` alone, never
by removing this file): stop the server, keep a copy of the file for the record, remove
it, run `stira authority reset-latches` (every latch set: without the commitment no older
latch store is trusted), start again (the epoch restarts at 1 with a new incarnation;
nothing from before is accepted), then unlatch. While unhealthy, YOLO approves nothing
automatically (every Ask shows its card).

**Limits.** The hash chain detects corruption and broken sequences, not tampering by the
same user: it is an unkeyed SHA-256, so any process of the server's user can write a new
valid chain. Such a process can also swap the file or its directory between two checks;
the checks narrow that race, they do not close it (the data directory is on the agent's
floor, so the agent's own file tools cannot write it). A rollback of the whole data
directory to an older valid file cannot be detected here; identity across starts is
always `(incarnation, epoch)` (ADR 0011 §5, Open (6)).

## Lock order

One leaf lock guards the in-memory compare-and-set only: no I/O, no callback, no clock
reading, no other lock while it is held. Callers may hold their own lock when they call in
(scheduler lock → authority lock; the YOLO request lock → authority lock; the engine
`state_lock` → authority lock). Persistence (its own I/O lock) and cancellation run after
the leaf lock is released; nonces and effect ids are drawn before the leaf lock is taken.
Every narrowing queues one batch (its epoch and the in-flight effects it took) in the
same step that moves the epoch. One owner at a time settles the batches in order — the
settle worker (for narrowings under a foreign lock: YOLO's moves, the breaker hook; they
never raise), a rules save or the kill switch in the calling thread (when nobody else
owns the queue), or `flush_result()` without a deadline when no worker can start. Per
batch: cancel, then write — and the file never shows an epoch whose cancellations are
still running (the *frontier*). A worker that cannot start latches
`epoch_settle_unavailable` and the work stays queued. The in-memory narrowing is in effect
before any I/O starts, and a failed write never undoes it. At shutdown the `authority`
step waits for every owner within its budget: `stopped` only when nothing is left **and**
the epoch is on disk; otherwise `pending` (work left), `unhealthy` (drained, not durable)
or `pending (no settle worker)`. The `StiraCore: stopped (...)` line names the `authority`
step only when it is not `stopped`; a clean stop keeps the line as before.

A YOLO issuance publishes `issuing` and reads the shared epoch in one step under the leaf
lock, and makes its final commit under the same lock only if the epoch is unchanged
(`commit_at_epoch`; clocks, health, breaker and the loosening judgement are gathered
before): any move in between (a breaker change, a rules save, the kill switch) ends it
as `issue_aborted_epoch` (ADR 0012 §6a / §6b). A rules save fences YOLO first, then writes
the epoch. A permit refused because the service is killed or unhealthy is recorded in
`audit.yolo.void` as `killed` / `authority_unhealthy`.

**Hard-link backups.** A hard link to `authority-epoch` (for example a same-volume
hard-link backup of the data directory) makes the file unsafe: the service latches
`epoch_store_unsafe` at its next write and stays unhealthy until a restart after the link
is gone. Exclude the data directory from hard-link backups.

**Bounded cancel callbacks (W2).** Settlement is ordered, so one blocked callback would
hold every later cancellation and the epoch write behind it. `narrow()` and the kill
switch **never run a cancel callback on the caller's thread**: a batch with a callback
goes to the settle worker, and the caller waits at most `SETTLE_WAIT_SEC` (1 s), then
returns `settlement: cancelling` (`narrow_result()` / `kill_switch()`; `settled` once its
cancellations ran and the epoch is on disk). A callback that has run for longer than
`CANCEL_DEADLINE_SEC` (5 s) is **abandoned**: the watchdog's pass (or a `flush_result`,
e.g. at shutdown) takes the queue from the stuck owner, puts the rest of its batch back in
front, records the effect as `cancel_overdue` (it stays in the in-flight set until it
ends), latches the service **unhealthy** (`cancel_overdue`: the cancellation could not be
established) and a new worker settles the rest. The abandoned callback is counted
(status `cancel_abandoned`) until it returns: until then `flush_result` is `pending`
(never `clean`; at once without a deadline) and every settlement is `cancelling`. When it
returns, the stuck thread touches nothing more (the rest of its batch is never run
twice).

## Latches (`authority-latches`, W2)

Stop latches that outlive a process; **only a person clears one** (below). Fixed names,
codes only: `kill`, `unhealthy`, `halted:<reason>` with `<reason>` one of `breaker`,
`emotional`, `identity`, `budget`, `clock_anomaly`, `unhealthy`, `restart_conflict`
(nothing sets a `halted:*` latch in W2; no grant exists). Each set latch keeps its **stop
id** `(incarnation, epoch)` from when it was set.

* At start: a set `kill` feeds the kill flag (the service starts killed), a set
  `unhealthy` starts it unhealthy (`latched_unhealthy`).
* **One rule for the kill**: flag, epoch, then the persistence attempt — a second
  marker (`authority-kill-armed` renamed to `authority-kill-fired`: a rename that needs no
  free data blocks), the `kill` latch, its commitment in the epoch file — then the kill's
  cancel callbacks, in order, on their **own executor**: never through the
  settlement queue, its owner or its epoch write, so neither a hung disk nor an owner
  stuck in I/O holds them. They are the kill's own effects **and** those of narrowings
  still queued behind the current owner (W4.5, #618 K1); a batch an owner is already
  running stays with it (an overdue callback there is abandoned as below). Each callback is waited for at most `CANCEL_DEADLINE_SEC`;
  one still running is left behind (`cancel_overdue`, counted until it returns) and the
  next starts. The epoch is settled like any narrowing. At start a fired marker means killed; a latch store
  without `kill` next to it is refused. `stira authority unlatch kill` removes the marker
  (the next start re-arms it).
* **Residual (full disk):** the `unhealthy` latch has no such second marker: if its latch
  write fails, the process is unhealthy in memory and says so (status, stop line), but a
  restart on a still-full disk can come up healthy unless the lifecycle log kept its
  event. A kill keeps its marker — the marker must be armed: a start that cannot make it,
  or finds an unsafe entry (symlink, hard link, FIFO, directory, another owner, wider
  than `0600`), is unhealthy (`kill_marker_unavailable` / `kill_marker_unsafe`). The
  rename itself is best effort: a filesystem that fails every operation keeps nothing.
* **The persistence attempt is bounded and single-flight:** one attempt at a time (a
  kill while one hangs waits for it — no thread per kill), waited for at most
  `KILL_PERSIST_DEADLINE_SEC` (5 s); past it the cancellations run anyway, the kill
  stays set and the service latches `unhealthy` (`kill_persist_timeout`, also in the
  outbox and the kill answer `latch: timeout`). A thread that cannot start never runs
  the persistence inline (`kill_persist_unavailable`): the attempt is **handed to the
  running watchdog**, which writes the marker and the latch first in its next pass (it is
  woken by the kill's event); the answer is `latch: pending` (also for a later kill while
  it waits). `status()` shows `kill_durable: false` until the latch is on disk
  (W4.5, #618 K2). The lifecycle records of the kill (and the grants' follow-ups) are
  queued only after the persistence attempt and the cancel kickoff (#620 W5-3). In the same process a late write
  only adds the kill; another process cannot interleave (the data directory's
  `server.lock`).
* **Commitment (no silent clear by deletion or rollback).** Every epoch record now also
  carries the latch store's generation, newest record hash and random **store id**
  (`latch_gen`, `latch_head`, `latch_store`; written at start, with every epoch write,
  right after the kill latch and the `unhealthy` latch, and by `unlatch`). The generation
  never decreases along the chain — `reset-latches` continues it and chains the new
  store (a new id) to the committed head. At start the store must **descend** from the
  commitment: hold the committed record (same generation, hash, store id) or continue it
  directly; a missing, older, same-generation-different, or other-instance store (a
  backup from before a reset) is refused (`latch_store_rolled_back`: every latch set), and
  so is one that lacks a latch the lifecycle log shows as set and never cleared
  (`human_unlatch`). A W1-era directory (no commitment yet) is taken as it is once and
  committed at that start.
* The watchdog persists `unhealthy` whenever the service is unhealthy (any reason).
* The file follows the epoch store's rules (`0600`, flush, atomic rename, directory
  flush; the newest 64 records, each the whole state + the change, chained by `seq` and
  the previous hash; re-read before every replace). **An unreadable, unsafe, malformed or
  mismatched file means every latch is set** (killed and unhealthy), sticky, and the file
  is never overwritten. A missing file is a first start only when nothing was committed
  (above). A rollback of the **whole** data directory (epoch file, latch store and log
  together) to an older valid state is still not detectable here (ADR 0011 Open (6)).

| Reason code | Meaning |
|---|---|
| `latch_store_unsafe` / `_unreadable` / `_malformed` / `_hash_mismatch` / `_sequence_gap` | as for the epoch file |
| `latch_store_replaced` | the file changed behind the service |
| `latch_store_write_failed` | a write failed (e.g. the kill latch: the process stays killed) |
| `latch_store_rolled_back` | the store is missing, older or different from its commitment, or lacks a latch the lifecycle log shows as set |
| `latched_unhealthy` | the `unhealthy` latch was set at start |
| `cancel_overdue` | a cancel callback was abandoned past its deadline |

## Lifecycle events and the outbox (W2)

One `lifecycle_event` schema (ADR 0011 §12), **codes and ids only**: `event_id`,
`subject_kind` (`authority`, `latch`, `breaker`, `yolo_window`, `heartbeat`, `gateway`),
`subject_id` (checked per kind: a fixed word, a latch name or a 32-hex window id),
`from`, `to` (fixed states), `reason_code` (a fixed list), `incarnation`, `epoch`,
`ts_wall`, and only on `gateway_down` the start of the gap `gap_from_wall`. Anything else
— another key, a free-text value, a subclass of `str` — is refused. No goal text, no tool
argument, no message can enter it.

Producers today: the kill switch (`latch: cleared → set`, `kill_switch`), a breaker change
away from NORMAL or between non-NORMAL levels (`breaker`), the YOLO window's pause and
end (observed from its audit rows after they were written; YOLO is unchanged), the
`unhealthy` latch, the watchdog (`heartbeat_stale` / `heartbeat_resolved`,
`gateway_down`) and `stira authority unlatch` (`human_unlatch`). Producers only queue
(no I/O; a breaker change runs under the engine's lock); the watchdog thread writes, as do
the kill route and the last stop step.

* **Log** `authority-lifecycle.jsonl`: one hash-chained JSON line per event, appended and
  flushed per line, the same safe-open rules; a broken chain, a torn last line, an
  unsafe or replaced file makes it unusable (sticky; never repaired here). Each watchdog
  pass checks both segments against a digest of what was written or sealed. **Bounded at
  write time (every writer, the offline host commands too):** an append that would take
  the file past 1 MiB first moves it to
  `authority-lifecycle.1.jsonl` (replacing the older one) and the chain continues in a
  new file; the reader accepts 2 MiB per segment, so what the writer produces always
  reopens.
* **Outbox** `authority-outbox.json`: one notification per dedupe key *subject +
  incarnation + epoch + to*, unread until acknowledged (`POST /api/authority/outbox/ack`
  by its **newest** event id — a watermark: a replayed or older acknowledgement changes
  nothing and an older id of a notice with a newer occurrence is `outbox_stale`, so a
  repeat is never hidden by an old ack), kept across restarts. Rebuilt at start and on
  every watchdog pass: every logged event has a notification, and a set latch whose stop
  has no record gets `stopped (reason_not_recorded)`. The file also keeps the ids already
  notified and the acknowledged ids (tombstones, pruned to what is still in the log), so
  an acknowledged or trimmed notice never comes back unread and an old acknowledged id
  still answers `already` while its event is in the log. At most 256 notices:
  acknowledged ones are dropped first, then the oldest unread *informational* ones are
  coalesced into one unread item with a count per reason code. **Stop notices** (a latch,
  the authority, a stop / unhealthy transition, kill / reset / unlatch, an abnormal YOLO
  end) are never coalesced into others: one notice per **stop code** — subject kind,
  the subject id for fixed subjects (a latch name, `service`), reason code; no window
  id, no incarnation — so their number is bounded by the fixed codes. A repeat counts,
  keeps the first and newest event id, shows its newest event and is unread again. Nothing unread
  disappears silently, and a write that the reader would refuse is never made.
* **A failed write is never swallowed**: the service latches `unhealthy`
  (`lifecycle_write_failed`, `outbox_write_failed`, `lifecycle_queue_full`,
  `lifecycle_event_invalid`, …), the warning is logged, the stop line shows
  `authority_watchdog: unhealthy`.

## Watchdog (W2)

One thread `authority-watchdog`, independent of the heartbeat, of YOLO and of page views
(it runs with YOLO off), every 15 s (`INTERVAL_SEC`) and at once after an event is
queued. It starts no work and **never imports or calls model code itself**: the watchdog
and the authority package import no model, agent, provider, scheduler, heartbeat or YOLO
code — a graph walk over every module and parent package, and a clean interpreter with
only the root facade replaced, both check it. (Importing it as `stiracore.authority…`
runs the root `stiracore/__init__.py` first, which re-exports the agent; in the server
that is loaded anyway. That facade is the one, documented exclusion.) `start()` twice is
refused; `stop()` then `start()` runs again. Each pass: **re-read the files** — the epoch
file, the latch store, the lifecycle log and the outbox, under the same safe-open rules,
against what the process read or wrote last; a changed or damaged file makes the
authority unhealthy at once (a changed latch store also sets every latch), and the
lock-free permit check refuses from then on (first of all: write a kill persistence
handed over because its own thread could not start); abandon an overdue cancel callback; the heartbeat's read-only
`status()` (enabled and not running, degraded, or no tick for `max(3 × interval, 120 s)`
→ `heartbeat_stale`, healthy again → `heartbeat_resolved`); persist `unhealthy`; write
the queued events; repair the outbox; write its last pass to `authority-watchdog.json`.
At start, a previous pass older than 30 s (2 × the interval) is recorded as
`gateway_down` with the gap. A failing pass latches `unhealthy` (`watchdog_failed`), a
record that cannot be read or written `watchdog_record_failed`, a thread that cannot
start `watchdog_unavailable`. **Limit:** while the process is down nothing is recorded or
notified; the gap is recorded at the next start.

## Routes

`GET /api/authority/status` (read only: `healthy`, `unhealthy_reason`, `killed`, `epoch`,
`persisted_epoch`, `settle_pending`, `cancel_overdue`, `in_flight`, `latches` (codes),
`latch_store_healthy`, `outbox_unread`, `lifecycle_healthy`, `watchdog`; the in-process
`kill_durable` is **not** in this answer — a durable kill shows as `"kill"` in `latches` with
`latch_store_healthy: true`, see Known limits),
`POST /api/authority/kill` (one press, no confirmation, always available — also when
unhealthy), `GET /api/authority/outbox[?unread=1]`, `POST /api/authority/outbox/ack
{event_id}`. They are behind the server's Host / Origin / CSRF checks; none is a
model-visible tool; they show an event's incarnation only as a short `instance`
reference. **No route clears a latch.**

## Operator procedure: kill and unlatch

1. **Kill:** `POST /api/authority/kill` (from the dashboard's own origin with its CSRF
   token). Every permit and `consume` is refused at once; YOLO approves nothing
   automatically; the answer says `settlement: cancelling` while cancellations still
   run. The `kill` latch is persisted.
2. **Look:** `GET /api/authority/status` / `GET /api/authority/outbox`, or on the host
   `stira authority status` (read only, also while the server runs).
3. **Clear** (with the server stopped — the command takes the data directory's
   `server.lock` and refuses while a server runs): `stira authority unlatch kill` (or
   `unhealthy`, `halted:<reason>`, `all`). It shows the latches with their stop epochs,
   needs a TTY and the typed word `UNLATCH`, and clears exactly what it showed: a change
   in between is `resume_stale` (nothing cleared); the operation is bound to this
   command's incarnation, the stop ids and the store's generation, and the same op id
   is never applied twice. Each clear is recorded (`human_unlatch`).
4. **Start** the server again.

**Repairs** (always with the server stopped):

* *Unusable, missing or rolled-back latch store*: `stira authority reset-latches` (TTY,
  the typed word `RESET`, takes `server.lock`). It keeps the old file
  (`authority-latches.replaced-<hex>`), writes a new store with **every** latch set,
  records its commitment in the epoch file and one `latch_store_reset` event per latch.
  Then clear them with `stira authority unlatch …` as above. Removing the file never
  clears anything: the next start refuses the missing store.
* *Torn or damaged lifecycle log* (`lifecycle_log_torn` after a crash mid-append, or
  another `lifecycle_log_*` code): keep a copy of `authority-lifecycle*.jsonl`; for a torn
  tail, cut the file after its last complete line (the last `\n`); otherwise move both
  segments aside (the chain must start at its beginning). Start the server, then (stopped
  again) `stira authority unlatch unhealthy`. Moved-aside events leave the log; a latch
  they record is still in the latch store and its commitment.
* *Unsafe or unusable kill marker* (`kill_marker_unsafe` / `kill_marker_unavailable`;
  `unlatch` refuses while it lasts): with the server stopped, look at
  `authority-kill-armed` / `authority-kill-fired`. A wrong *armed* entry (symlink, hard
  link, FIFO, directory, other owner, wider than `0600`): remove it (`rmdir` for a
  directory; never its target) — the next start makes a new one. A wrong *fired* entry
  means a kill was pressed: leave the kill latch, remove only the bad entry and replace
  it with an empty `0600` file, then clear with `stira authority unlatch kill`. Then
  `stira authority unlatch unhealthy` and start.
* *Epoch file* (only when the epoch file itself is damaged — never as a latch-store
  repair): keep a copy, remove it, `stira authority reset-latches`, start, then unlatch.
  `unlatch` itself refuses a latch store that is not the committed one (the server's own
  check) and writes nothing then; `reset-latches` works with the commitment intact.

Nothing clears a latch by itself: not a restart, not time, not the breaker returning to
NORMAL. Clearing does not open anything: it only lets the next start run normally.

## Known limits (W4.5)

Each of these fails closed (killed / unhealthy / never `clean`); none is a way back to
authority. Open items stay tracked in #618.

* **A kill is durable only once `status()` says `kill_durable: true`.** `status()` is the
  in-process view; `GET /api/authority/status` does not carry `kill_durable`. From outside,
  check that its `latches` list contains `"kill"` **and** `latch_store_healthy` is `true` (the
  latch store's memory changes only after a successful write; an unusable store lists every
  latch with `latch_store_healthy: false`, which proves nothing), or run
  `stira authority status` on the host (reads the file). A kill answered
  `latch: pending` (its persistence thread could not start: the watchdog writes it),
  `timeout` (a hung disk) or `failed` has set the flag in memory only; a restart **before**
  the write lands can come up healthy and not killed, unless the fired marker or the
  lifecycle log's kill event reached the disk. This window cannot be fully closed (every
  design has a crash-before-persist instant); press the kill again after a restart if
  the status does not show it.
* The handed-over write runs on the watchdog's thread: a hung disk holds that pass (as
  the pass's own writes would). Without a running watchdog (`watchdog_unavailable`) it is
  never written in this process and `kill_durable` stays false.
* Kill callbacks start after the persistence attempt (up to 5 s on a hung disk) and run
  one after the other, each waited for at most `CANCEL_DEADLINE_SEC` (latency only; #618
  K3). The kill request itself can still wait on epoch I/O after the cancellations were
  dispatched (#618 K5, availability only).
* A batch an owner is running when the kill comes stays with that owner (its rest is
  re-queued by `rescue_overdue` if a callback there hangs).
* A queued batch whose callbacks moved to the kill executor keeps only its epoch: the
  frontier and the epoch file can pass it before those callbacks finish (as for the kill's
  own batch, #618 D2); `settlement` stays `cancelling` and `flush` is never `clean` while
  one runs (`_kill_pending`).

## Not here (later waves)

Unconditional conservative start and stop-id-bound tokens for the scheduler (W3); the goal
loop (W5+). **W4 (grants, execution off)** uses this service as follows ([grants](grants.md)):
every grant stop advances the epoch (`narrow("grant_stop")`, deferred under a foreign lock);
the watchdog's pass runs the grants' model-free look (`Watchdog(looks=…)`: kill, unhealthy,
breaker, clocks, TTL expiry, the grant store); grant and goal transitions are lifecycle
events with the subjects `grant` / `goal` (the grant id) and `grants` / `store` (its health),
and their stops are stop notices keyed `stop|grant|*|<reason>`. Grants set no `halted:*`
latch (a grant never survives a restart). The dashboard's kill button is in the grants panel
(stage C on). See ADR 0011 §13.

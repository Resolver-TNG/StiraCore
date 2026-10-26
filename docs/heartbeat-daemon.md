# Heartbeat daemon (#238)

`HeartbeatDaemon` (`src/stiracore/heartbeat/daemon.py`) runs the existing
`HeartbeatGateway.tick()` in a background thread of `stira_server`. A tick is
**model-free**: lazy decay, a state snapshot and a breaker *query*. It never calls a
model, sends a notification, or acts on its own. Scheduled jobs and notifications are
#239 ([scheduler.md](scheduler.md), which also raises `heartbeat_stalled`); asynchronous
HITL is #240. Operator guide (switching it on, checks, troubleshooting):
[guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md).

The scheduler does not ride on the tick: it runs its own thread and lock
(`scheduler.lock`) and reads the heartbeat only through the read-only `status()`
(`health_source`, for `heartbeat_stalled`). The heartbeat package imports nothing from
the scheduler, memory jobs or model code (`tests/test_memory_jobs_phase0.py`, import
graph). Adding work to the tick would break that guarantee; new periodic work goes to a
scheduler job.

## Enabling (opt-in, off by default)

- the settings page, section "Heartbeat" (#472: the switch and both interval bounds, saved to
  `gateway-config.json`; applied after a restart, the card shows the running status read-only), or
- `gateway-config.json`: `"hb_enabled": true`, or
- environment: `STIRA_HEARTBEAT=1` (`0` forces it off; empty = not set). While it is set, the
  page shows the switch disabled with the variable's name.

Pacing stays `compute_interval(stability, hb_interval_floor_sec, hb_interval_ceil_sec)`.
The first tick runs at start-up.

## Guarantees

| Concern | Behaviour |
| --- | --- |
| Double start | Exclusive OS lock on `<state dir>/heartbeat.lock` (`flock`; `msvcrt` on Windows — **unverified on Windows**: no Windows host, and no CI run has passed since 2026-10-12, before the heartbeat existed, so its Windows cell never ran this code; the `msvcrt` branch is only tested against a simulated `msvcrt` — byte 0, non-blocking, `EACCES` = contention, unlock at offset 0), the shared owner-lock implementation of `server.lock` (#322): opened without following a symlink (`O_NOFOLLOW`, not on Windows) and refused unless it is a regular file with one link owned by this user (`UnsafeLockFileError`, nothing written; the server serves without a heartbeat, status `last_error: UnsafeLockFileError`). Only contention (`EWOULDBLOCK`/`EAGAIN`/`EACCES`) is reported as `HeartbeatLockError`; other `OSError`s propagate, and every failure before ownership closes the descriptor. A failed PID note (informational) keeps a releasable lock. The chat server never gets that far with a second server: its data-directory lock (`server.lock`, #285) refuses the whole start first. If `heartbeat.lock` is still contended (another program holds it), the server serves without a heartbeat and the start-up line and the status agree (`Heartbeat: enabled, not running (HeartbeatLockError)`, status `last_error: HeartbeatLockError`); any other start failure is logged by class name and the server also serves without it. The kernel drops the lock when the owner dies. |
| Single writer | **Limitation:** the lock excludes heartbeats only. A second server sharing the state directory can still save `neural-state.json` from its own memory and overwrite the first one's updates; running two servers on one state directory is unsupported (out of scope for #238). |
| Lifecycle | One lifecycle lock guards a state machine `stopped → starting → running → stopping → stopped` (status field `state`). `start()` is a transaction: a failure anywhere (owner lock, first clock reading, thread creation/start) rolls back to `stopped` and releases the lock. `stop(timeout)` uses one monotonic deadline for every wait (lifecycle lock, in-flight tick, join) and returns within it. `True` = stopped and lock released. `False` = still `stopping` (a tick or save is in flight, or the lock was busy): no new tick starts, the **owner lock stays held**, and the finishing tick completes the stop by itself (background worker exit, or the end of the in-flight `poll()`); `start()` is refused until then. While the background worker runs, an external `poll()` is refused (the worker is the only ticker); the refusal and the tick reservation are one lifecycle-lock section. Every `start()` opens a generation `(incarnation, number)`: the incarnation is 128 random bits per process, never persisted or read back. A `stop()` targets the generation current when it was called and never ends a later one; it waits for that generation on an event, so it does not re-acquire the lifecycle lock past its deadline. The stop request is recorded before the lifecycle lock is taken: if the lock stays busy, `stop()` returns `False` and the request is kept — the end of `start()`, the worker and the next `poll()` honour it. Recording a request and reserving a tick are one step under a leaf request lock (held only for that in-memory check-and-set): a request recorded first means no tick; a tick reserved first is in flight and the stop completes after it (#238 hardening). The chat server calls it on its stop path (`SIGTERM` → normal exit → `shutdown_app`, #285) and again via `atexit`; a SIGKILL relies on the kernel releasing the lock. |
| Unexpected loop error | The thread ends, `running` becomes `false`, the lock is released, and `last_error` reads `loop:<Class>`. |
| Restart recovery | Each tick saves `neural-state.json` (incl. `last_decay_check`): after a process restart decay continues from the saved time, never twice and never skipped. |
| Clock | Injectable `now_fn`. A clock that moves backwards keeps the wait that was *remaining at the last reading* (`next_due - last_seen`) and moves it to the new time: no stall until the old timestamp, no re-waiting elapsed time, and a pending failure backoff is kept (e.g. due 1100, last reading 1090, clock back to 1080 → due 1090). Decay itself resyncs without decaying. A long sleep is one tick, clamped at the baselines. The wall clock is re-read at least every 30 s. The same cases (interval, stop → no tick, restart → immediate tick, clock back, a month's sleep) are tested on the background worker as well as through `poll()` (`tests/test_heartbeat_resident_loop.py`). |
| Failures | A tick exception never ends the loop. Retries back off `failure_backoff_sec × 2^(n-1)` (default 60 s), capped at `max(failure_backoff_sec, hb_interval_ceil_sec)`; the exponent is bounded, so no overflow after any number of failures. From the 3rd consecutive failure a warning is logged and the status is `degraded`. Only the exception class name is reported. |
| Turns | `tick()` holds `engine.state_lock` (#147) only for decay + snapshot + breaker query; measured median 0.003 ms. Saves snapshot under the same lock (wired before the thread starts). The trajectory sink runs after the gateway releases that lock and takes `state_lock` and the history lock one after the other, never nested. Measured on the chat server wiring with the real worker, sink and save while 8 chat turns ran (fake model, a tick every ~1 ms, M1 Max, 2026-10-21, 3 runs): the heartbeat took the lock 3× per tick (tick, sink copy, save copy), each hold median 0.015 ms / p99 0.026 ms / max 0.035 ms; a turn waited for the lock at most 0.005 ms. The file write of the save runs outside the lock. Test: `tests/test_heartbeat_resident_loop.py` (CI bound 50 ms). |
| Retention | Each tick's own snapshot (same instant as its breaker result) goes to the bounded `NeuralHistory` (500 rows) that feeds the observability trajectory. |

## Dashboard (read-only)

`/api/gateway/status` and `/api/state/history` carry a `heartbeat` object (`last_tick_at`,
`next_tick_at`, `last_level`, failures). The observability page shows one line under
“Neural State”. There is no route that starts, stops or triggers a tick.

## Breaker interaction (active decay)

Decay moves every dimension linearly toward its baseline (stability 0.7; from below at
half rate, 0.0025/h). With a running heartbeat this happens without a turn. The tick's
breaker query re-evaluates the same hysteresis as a turn but never counts a turn:

- **Entry is unaffected.** Decay never moves away from the baseline, so it cannot
  trigger WARNING (< 0.5) or CRITICAL (< 0.3) by itself.
- **WARNING can be released by time alone**: stability above `stability_warning_exit`
  (0.6) clears it without a user turn (0.45 → still WARNING at 59 h, NORMAL at 61 h).
- **CRITICAL: ticks never serve the cooldown.** While fewer than
  `critical_cooldown_turns` (≥ 3) protected turns have passed, CRITICAL holds whatever
  the stability (0.2 → 0.7 after 1000 h of ticks, still CRITICAL).
- **CRITICAL can be lifted by time once the cooldown was served by turns.** Then only
  `stability > stability_critical_exit` (0.45) remains, and decay can satisfy it: measured
  0.2 with 3 served turns → CRITICAL at 0.45 (100 h), WARNING at 0.475 (110 h), NORMAL at
  0.7 (200 h), all from ticks. Without the heartbeat the same release happened at the next
  turn's `pre_turn` (which applies the same decay); the heartbeat only makes it visible
  without a turn.
- **COHERENCE is unchanged.** `bc_score` is updated only in `pre_turn`.

**Elapsed time is not evidence.** A breaker that returns to NORMAL by decay is not a
HITL approval and no evidence for identity protection. It does not resolve or create
approvals; per-tool approval policy (Ask/HITL) and the identity guards still apply at
every level. The tick never touches approvals.

See [breaker-reachability](breaker-reachability.md) (#142) for turn-driven reachability, and
[neural-saturation](neural-saturation.md) (#165) for saturation, measured recovery times and
what the tick interval does (and does not) change.

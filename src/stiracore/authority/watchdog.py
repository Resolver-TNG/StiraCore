"""The model-free watchdog (ADR 0011 §12; W2 of #296).

One thread (``authority-watchdog``) in the server process, independent of the heartbeat,
of YOLO and of page views; it runs whether YOLO is on or off. It **never starts work** and
imports no model, agent, scheduler, heartbeat or YOLO code (a test walks the import
graph): what it reads is handed to it (the authority service, the lifecycle recorder, a
read-only heartbeat status callable).

Each pass (every :data:`INTERVAL_SEC`, and at once after a lifecycle event was posted):

0. **a handed-over kill** — a kill whose persistence thread could not start is written
   here, first (:meth:`AuthorityService.persist_handed_kill`, #618 K2);
a. **store health** — an unhealthy service (any reason: the epoch store, the latch store,
   a failed write) is persisted as the ``unhealthy`` latch, with its lifecycle event;
b. **writer health** — the queued lifecycle events are written (log, then the outbox); a
   failed write is reported by the recorder (the service latches unhealthy);
c. **heartbeat staleness** — through the heartbeat's read-only ``status()``: enabled and
   not running, degraded, or its last tick older than ``max(3 × interval, 120 s)`` (ADR
   0008 §11) → ``heartbeat: healthy → stale (heartbeat_stale)``; healthy again →
   ``stale → healthy (heartbeat_resolved)``. Transitions only (one event per change);
d. **its own last pass** — written to ``authority-watchdog.json`` (``0600``, atomic
   replace). At start, a previous pass older than ``2 × interval`` is recorded as
   ``gateway: down → up (gateway_down)`` with the gap (``gap_from_wall`` → ``ts_wall``);
e. **outbox repair** — every logged event has a notification; a set latch without a
   record of its stop gets ``stopped (reason_not_recorded)``;
f. **overdue cancel callbacks** — :meth:`AuthorityService.rescue_overdue`;
g. **extra looks** handed in (``looks``; W4: the grants' ``sync`` — kill, unhealthy,
   breaker, clocks, TTL expiry, the grant store), model-free like the rest.

A failing pass (anything raised) latches the service ``unhealthy`` (``watchdog_failed``)
and the loop goes on; a last-pass record that cannot be read or written latches
``watchdog_record_failed``. **Limit:** while the process is down nothing is recorded; the
gap is recorded at the next start (ADR 0011 §12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import private_file
from .lifecycle_events import make_event

__all__ = ["INTERVAL_SEC", "RECORD_FILE", "Watchdog", "heartbeat_stale", "live", "stop_all"]

logger = logging.getLogger(__name__)

INTERVAL_SEC = 15.0  # the default pass interval (operator decision 2026-10-24)
RECORD_FILE = "authority-watchdog.json"
_MAX_BYTES = 4096
_STALE_FLOOR_SEC = 120.0
# Every watchdog whose thread was started and not stopped (weak: a dropped one goes).
_LIVE: weakref.WeakSet = weakref.WeakSet()


def live() -> list:
    """The watchdogs whose thread is still running (for shutdown and the tests)."""
    return [dog for dog in list(_LIVE) if dog._thread is not None and dog._thread.is_alive()]


def stop_all(timeout: float = 5.0) -> int:
    """Stop every running watchdog (the test suite's teardown; a process that builds
    several apps). The number stopped."""
    stopped = 0
    for dog in live():
        dog.stop_passes(timeout)
        stopped += 1
    return stopped


def heartbeat_stale(status: Any, now: float, since: float) -> bool:
    """ADR 0008 §11 / ADR 0011 §12: an enabled heartbeat that is not running, is degraded
    or whose last tick (``since`` when it has not ticked yet) is older than
    ``max(3 × interval, 120 s)``. An unreadable status of an enabled heartbeat is stale;
    a disabled one never is."""
    if type(status) is not dict or status.get("enabled") is not True:
        return False
    try:
        interval = float(status.get("interval_sec") or 60.0)
    except (TypeError, ValueError):
        interval = 60.0
    if not math.isfinite(interval) or interval <= 0:
        interval = 60.0
    last = status.get("last_tick_at")
    if isinstance(last, bool) or not isinstance(last, (int, float)) or not math.isfinite(last):
        last = since
    limit = max(3.0 * interval, _STALE_FLOOR_SEC)
    return status.get("running") is not True or bool(status.get("degraded")) or now - last > limit


def _record_body(last_pass: float) -> bytes:
    digest = hashlib.sha256(f"stiracore-watchdog:v1:{last_pass!r}".encode("ascii")).hexdigest()
    return json.dumps({"v": 1, "last_pass_wall": last_pass, "hash": digest}).encode("ascii")


def _parse_record(raw: bytes) -> float | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if type(data) is not dict or set(data) != {"v", "last_pass_wall", "hash"} or data["v"] != 1:
        return None
    value = data["last_pass_wall"]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if _record_body(value) != raw:
        return None
    return float(value)


class Watchdog:
    """The watchdog of one process. :meth:`start` once, :meth:`stop` at shutdown;
    :meth:`run_pass` is one pass (tests drive it without the thread)."""

    def __init__(
        self,
        service,
        recorder,
        *,
        data_dir: Path | None,
        heartbeat_status: Callable[[], Any] | None = None,
        interval: float = INTERVAL_SEC,
        clock: Callable[[], float] = time.time,
        looks: tuple[Callable[[], Any], ...] = (),
    ) -> None:
        self._service = service
        # W4: extra model-free looks run in every pass (the grants' ``sync``), before the
        # queued events are written. Each must never raise; one that does fails the pass.
        self._looks = tuple(looks)
        self._recorder = recorder
        self._dir = None if data_dir is None else Path(data_dir)
        self._heartbeat_status = heartbeat_status
        self.interval = float(interval)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # the status fields
        self._started_wall: float | None = None
        self._last_pass: float | None = None
        self._passes = 0
        self._failures = 0
        self._heartbeat_stale = False
        self._record_identity: tuple[int, int] | None = None
        self._record_seen: bytes | None = None
        self._record_bad: str | None = None
        self._pass_lock = threading.Lock()  # one pass at a time (thread, tests, shutdown)

    # -- start / stop ----------------------------------------------------------------------

    def start(self, *, thread: bool = True) -> None:
        """Read the last-pass record (a gap is recorded as ``gateway_down``), run the
        first pass (the outbox rebuild), then start the thread. A thread that cannot be
        started latches the service unhealthy (``watchdog_unavailable``)."""
        worker = self._thread
        if worker is not None and worker.is_alive():
            raise RuntimeError("watchdog_running")  # one thread at a time
        self._stop.clear()  # stop() then start() runs again
        now = self._clock()
        with self._lock:
            self._started_wall = now
        previous = self._read_record() if self._record_identity is None else None
        if previous is not None and now - previous > 2.0 * self.interval:
            self._post(
                "gateway",
                "process",
                "down",
                "up",
                "gateway_down",
                gap_from_wall=previous,
                ts_wall=now,
            )
        self.run_pass()
        if not thread:
            return
        try:
            worker = threading.Thread(target=self._run, name="authority-watchdog", daemon=True)
            worker.start()
        except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
            self._service.report_unhealthy("watchdog_unavailable")
            logger.warning("authority: the watchdog could not start; unhealthy")
            return
        self._thread = worker
        _LIVE.add(self)

    def stop_passes(self, timeout: float | None = None) -> str:
        """End the passes (the first stop step: no heartbeat check while the other stops
        run). ``stopped`` when the thread ended, ``pending`` when it is still in a pass."""
        self._stop.set()
        self._recorder.wake.set()
        worker = self._thread
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return "pending"
        _LIVE.discard(self)
        return "stopped"

    def stop(self, timeout: float | None = None) -> str:
        """End the passes, then write what is queued (the last stop step): ``stopped``
        only when the thread ended **and** every lifecycle event is written; else
        ``pending`` / ``unhealthy``."""
        if self.stop_passes(timeout) != "stopped":
            return "pending"
        if not self._pass_lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
            return "pending"
        try:
            drained = self._recorder.drain()
        finally:
            self._pass_lock.release()
        return "stopped" if drained == "clean" else "unhealthy"

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._recorder.wake.wait(self.interval)
                self._recorder.wake.clear()
                if self._stop.is_set():
                    break
                self.run_pass()
        except BaseException:
            self._service.report_unhealthy("watchdog_failed")
            logger.error("authority: the watchdog thread ended unexpectedly; unhealthy")
            raise

    # -- one pass --------------------------------------------------------------------------

    def run_pass(self) -> bool:
        """One pass (a–f in the module docstring). False when anything failed (the
        service is then unhealthy). Never raises."""
        with self._pass_lock:
            try:
                ok = self._pass()
            except Exception:  # noqa: BLE001 - a failing pass latches unhealthy
                ok = False
                self._service.report_unhealthy("watchdog_failed")
                logger.warning("authority: a watchdog pass failed; unhealthy")
            with self._lock:
                self._passes += 1
                if not ok:
                    self._failures += 1
            return ok

    def _pass(self) -> bool:
        now = self._clock()
        service, recorder = self._service, self._recorder
        ok = True
        # (0) #618 K2: the kill latch / marker whose own thread could not start.
        service.persist_handed_kill()
        # (a) the files themselves, not the caches (M3): a changed or damaged epoch file,
        # latch store, lifecycle log or outbox makes the authority unhealthy.
        if service.verify_files() is not None:
            ok = False
        if recorder.verify() is not None:
            ok = False
        # (f) a stuck cancel callback is abandoned first: it may be what blocks the rest.
        service.rescue_overdue()
        # (g) W4: the grants' look (kill / unhealthy / breaker / clocks / TTL / store).
        for extra in self._looks:
            try:
                extra()
            except Exception:  # noqa: BLE001 - a failing look fails the pass
                service.report_unhealthy("watchdog_failed")
                ok = False
        # (c) the heartbeat, read only.
        if self._heartbeat_status is not None:
            try:
                status = self._heartbeat_status()
            except Exception:  # noqa: BLE001 - an unreadable status of an enabled heartbeat
                status = {"enabled": True, "running": False}
            with self._lock:
                since = self._started_wall if self._started_wall is not None else now
            stale = heartbeat_stale(status, now, since)
            if stale != self._heartbeat_stale:
                if stale:
                    self._post("heartbeat", "daemon", "healthy", "stale", "heartbeat_stale")
                else:
                    self._post("heartbeat", "daemon", "stale", "healthy", "heartbeat_resolved")
                self._heartbeat_stale = stale
        # (a) store health: an unhealthy service becomes the persisted latch.
        if service.unhealthy_reason is not None:
            service.persist_unhealthy()
        # (b) the writer: queued events to the log and the outbox.
        if recorder.unhealthy_reason is not None:
            service.report_unhealthy(recorder.unhealthy_reason)
            ok = False
        if recorder.drain() != "clean":
            ok = False
        # (e) outbox repair from the log and the latches.
        latches = service.latches
        if latches is not None and recorder.unhealthy_reason is None:
            recorder.repair(latches.latched())
        # (d) the last-pass record.
        if not self._write_record(now):
            ok = False
        with self._lock:
            self._last_pass = now
        return ok and service.unhealthy_reason is None

    def _post(self, kind, ident, before, after, reason, *, ts_wall=None, gap_from_wall=None):
        event = make_event(
            kind,
            ident,
            before,
            after,
            reason,
            incarnation=self._service.incarnation,
            epoch=self._service.epoch,
            ts_wall=self._clock() if ts_wall is None else ts_wall,
            gap_from_wall=gap_from_wall,
        )
        self._recorder.post(event)

    # -- the last-pass record --------------------------------------------------------------

    def _read_record(self) -> float | None:
        if self._dir is None:
            return None
        loaded = private_file.read(
            self._dir / RECORD_FILE, max_bytes=_MAX_BYTES, prefix="watchdog_record"
        )
        if loaded.status == "missing":
            return None
        value = _parse_record(loaded.raw) if loaded.status == "ok" else None
        if value is None:
            self._record_bad = "watchdog_record_failed"
            self._service.report_unhealthy("watchdog_record_failed")
            logger.warning("authority: the watchdog record is unusable; unhealthy")
            return None
        self._record_identity = loaded.identity
        self._record_seen = loaded.raw
        return value

    def _write_record(self, now: float) -> bool:
        if self._dir is None:
            return True
        if self._record_bad is not None:
            return False  # never overwrite an unusable record
        path = self._dir / RECORD_FILE
        current = private_file.read(path, max_bytes=_MAX_BYTES, prefix="watchdog_record")
        if self._record_identity is None:
            replaced = current.status != "missing"
        else:
            replaced = current.status != "ok" or (
                current.identity != self._record_identity or current.raw != self._record_seen
            )
        if replaced:
            self._record_bad = "watchdog_record_failed"
            self._service.report_unhealthy("watchdog_record_failed")
            return False
        body = _record_body(now)
        try:
            self._record_identity = private_file.replace(self._dir, RECORD_FILE, body)
        except OSError:
            self._record_bad = "watchdog_record_failed"
            self._service.report_unhealthy("watchdog_record_failed")
            return False
        self._record_seen = body
        return True

    # -- status ----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Read only: codes and numbers."""
        worker = self._thread
        with self._lock:
            last = self._last_pass
            return {
                "running": worker is not None and worker.is_alive() and not self._stop.is_set(),
                "interval_sec": self.interval,
                "last_pass_wall": last,
                "overdue": last is not None and self._clock() - last > 2.0 * self.interval,
                "passes": self._passes,
                "failed_passes": self._failures,
                "heartbeat_stale": self._heartbeat_stale,
                "record_healthy": self._record_bad is None,
            }

"""Append-only, hash-chained JSONL log for scheduler events (#239, ADR 0008 §9).

Same chain rule as the approval store (ADR 0006): every line carries
``hash = sha256(prev_hash || canonical_json([seq, ts, kind, body]))`` and a sidecar
head (``<log>.head``) names the newest line and the tail it was built on. The head is
written (atomically, then the directory is fsynced) *before* the line is appended.

Opening the log classifies it:

* head == tail: consistent;
* head exactly one ahead and built on the tail: a crash between head and append, or the
  last line was deleted (indistinguishable). ``needs_recovery`` is set; the caller must
  record new events that take back anything the missing line could have granted;
* anything else: tampering. The log is unhealthy for the life of the instance.

The interface (``append`` / ``events`` / ``verify``) is what a #171 EventStore needs to
provide to replace this file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from stiracore._platform import fchmod_private
from stiracore.core.approval import canonical_json

__all__ = ["GENESIS_HASH", "SchedulerEventLog", "SchedulerLogError"]

logger = logging.getLogger(__name__)
GENESIS_HASH = "0" * 64
_LINE_KEYS = frozenset({"seq", "ts", "kind", "body", "prev", "hash"})


class SchedulerLogError(RuntimeError):
    """The log cannot be trusted or written; the scheduler must not run."""


def line_hash(prev: str, seq: int, ts: float, kind: str, body: Any) -> str:
    material = prev + "\n" + canonical_json([seq, ts, kind, body])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` until every byte is written; no progress is an error (#601 B9)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class SchedulerEventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self._lock = threading.Lock()
        self._count = 0
        self._tail_seq = 0
        self._tail_hash = GENESIS_HASH
        self._size = 0
        # Exact bytes of the newest line this instance read or wrote (tail comparison).
        self._tail_line = b""
        self._error: str | None = None
        self._opened = False
        self.needs_recovery = False

    # -- state ---------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._opened and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def _fail(self, message: str) -> SchedulerLogError:
        if self._error is None:
            self._error = message
            logger.error("scheduler log unavailable: %s", message)
        return SchedulerLogError(self._error)

    # -- reading -------------------------------------------------------------

    def _read_head(self) -> dict | None:
        try:
            raw = self.head_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            head = json.loads(raw)
        except ValueError:
            raise self._fail("head anchor is not valid JSON") from None
        if (
            not isinstance(head, dict)
            or set(head) != {"seq", "hash", "base_seq", "base_hash"}
            or not all(type(head[k]) is int for k in ("seq", "base_seq"))
            or not all(isinstance(head[k], str) for k in ("hash", "base_hash"))
        ):
            raise self._fail("head anchor is malformed")
        return head

    def _parse(self, data: bytes) -> tuple[list[dict], int, bool]:
        """Return (events, size of the complete lines, trailing partial line?)."""
        events: list[dict] = []
        complete = data.rfind(b"\n") + 1
        partial = complete < len(data)
        prev, seq = GENESIS_HASH, 0
        for raw in data[:complete].splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                raise self._fail(f"line {seq + 1} is not valid JSON") from None
            if not isinstance(line, dict) or set(line) != _LINE_KEYS:
                raise self._fail(f"line {seq + 1} is malformed")
            ts = line["ts"]
            if (
                type(line["seq"]) is not int
                or line["seq"] != seq + 1
                or line["prev"] != prev
                or not isinstance(ts, (int, float))
                or isinstance(ts, bool)
                or not math.isfinite(ts)
                or not isinstance(line["kind"], str)
                or not isinstance(line["body"], dict)
            ):
                raise self._fail(f"chain broken at line {seq + 1}")
            try:
                expected = line_hash(prev, line["seq"], ts, line["kind"], line["body"])
            except (TypeError, ValueError):
                raise self._fail(f"line {seq + 1} is not canonical") from None
            if line["hash"] != expected:
                raise self._fail(f"hash mismatch at line {seq + 1}")
            prev, seq = expected, line["seq"]
            events.append(line)
        return events, complete, partial

    def open(self) -> list[dict[str, Any]]:
        """Verify the whole log; return its events. Raises :class:`SchedulerLogError`."""
        with self._lock:
            if self._error is not None:
                raise SchedulerLogError(self._error)
            if self._opened:
                raise SchedulerLogError("log is already open")
            try:
                head = self._read_head()
                try:
                    data = self.path.read_bytes()
                except FileNotFoundError:
                    data = None
                if data is None:
                    if head is not None:
                        raise self._fail("log file is missing but its head anchor exists")
                    self._opened = True
                    return []
                events, complete, partial = self._parse(data)
                tail_seq = events[-1]["seq"] if events else 0
                tail_hash = events[-1]["hash"] if events else GENESIS_HASH
                if head is None:
                    if events or partial:
                        raise self._fail("head anchor is missing")
                elif head["seq"] == tail_seq and head["hash"] == tail_hash and not partial:
                    pass
                elif (
                    head["seq"] == tail_seq + 1
                    and head["base_seq"] == tail_seq
                    and head["base_hash"] == tail_hash
                ):
                    # Crash between head and append, or the last line was deleted.
                    self.needs_recovery = True
                    if partial:
                        with open(self.path, "r+b") as handle:
                            handle.truncate(complete)
                            handle.flush()
                            os.fsync(handle.fileno())
                else:
                    raise self._fail("head anchor does not match the log (tampering)")
            except OSError as error:
                raise self._fail(f"log unreadable ({type(error).__name__})") from None
            self._count = len(events)
            self._tail_seq, self._tail_hash, self._size = tail_seq, tail_hash, complete
            if events:
                start = data.rfind(b"\n", 0, complete - 1) + 1
                self._tail_line = data[start:complete]
            self._opened = True
            return events

    def events(self) -> list[dict[str, Any]]:
        """Re-read and verify the file (operator tooling/tests; not kept in memory)."""
        with self._lock:
            if not self._opened:
                return []
            data = self.path.read_bytes() if self.path.exists() else b""
            events, _complete, _partial = self._parse(data)
            return events

    def verify(self) -> tuple[bool, str | None]:
        """Re-read and re-hash the file; compare it with what this instance wrote."""
        with self._lock:
            if self._error is not None:
                return False, self._error
            try:
                data = self.path.read_bytes() if self.path.exists() else b""
                events, complete, partial = self._parse(data)
            except SchedulerLogError as error:
                return False, str(error)
            except OSError as error:
                self._fail(f"log unreadable ({type(error).__name__})")
                return False, self._error
            if partial or complete != self._size or len(events) != self._count:
                self._fail("log changed outside this process")
                return False, self._error
            if events and events[-1]["hash"] != self._tail_hash:
                self._fail("log changed outside this process")
                return False, self._error
            return True, None

    # -- writing -------------------------------------------------------------

    def _tail_matches(self) -> bool:
        """The file still ends with the exact line this instance knows as its tail."""
        if not self._tail_line:
            return self._size == 0
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._size - len(self._tail_line))
                return handle.read(len(self._tail_line)) == self._tail_line
        except OSError:
            return False

    def _write_head(self, head: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sched-head-")
        try:
            fchmod_private(fd, 0o600)  # no-op on Windows (#540)
            _write_all(fd, json.dumps(head, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, self.head_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _fsync_dir(self.path.parent)

    def append(self, kind: str, body: dict[str, Any], ts: float) -> dict[str, Any]:
        with self._lock:
            if not self._opened:
                raise SchedulerLogError("log is not open")
            if self._error is not None:
                raise SchedulerLogError(self._error)
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
            except OSError as error:
                raise self._fail(f"log unreadable ({type(error).__name__})") from None
            if size != self._size or not self._tail_matches():
                raise self._fail("log changed outside this process")
            seq = self._tail_seq + 1
            try:
                digest = line_hash(self._tail_hash, seq, ts, kind, body)
                line = canonical_json(
                    {
                        "seq": seq,
                        "ts": ts,
                        "kind": kind,
                        "body": body,
                        "prev": self._tail_hash,
                        "hash": digest,
                    }
                )
            except (TypeError, ValueError) as error:
                raise SchedulerLogError(f"event is not canonical JSON ({error})") from None
            encoded = (line + "\n").encode("utf-8")
            try:
                # #601 B10: the log file exists (possibly empty) before the head moves
                # ahead, so a crash in between opens as "head one ahead" (recovery), not
                # as "head without a log" (unhealthy for good). The head's directory
                # fsync below makes both names durable.
                self.path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
                fd = os.open(self.path, flags, 0o600)
                try:
                    self._write_head(
                        {
                            "seq": seq,
                            "hash": digest,
                            "base_seq": self._tail_seq,
                            "base_hash": self._tail_hash,
                        }
                    )
                    _write_all(fd, encoded)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as error:
                # Head may be ahead now: the next open recovers (pauses jobs). This
                # instance stops writing, so nothing is built on an unknown tail.
                raise self._fail(f"append failed ({type(error).__name__})") from None
            event = json.loads(line)
            self._count += 1
            self._tail_line = encoded
            self._tail_seq, self._tail_hash = seq, digest
            self._size += len(encoded)
            self.needs_recovery = False
            return dict(event)

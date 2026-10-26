"""The latch store (ADR 0011 §5 "Unknown state stops", §6 item 4, §9; W2 of #296).

One small file ``authority-latches`` in the data directory: the stop latches that outlive
a process and are cleared **only by a person** (an operation token, see
:meth:`LatchStore.clear` and ``stira authority unlatch``). Nothing clears a latch by
itself: not a restart, not time, not the breaker returning to NORMAL.

Latches (fixed names, codes only — never free text):

* ``kill`` — the kill switch was pressed. At start the service's in-memory kill flag is
  fed from it, so a killed installation stays killed across restarts;
* ``unhealthy`` — the authority service was unhealthy (any reason code); at start the
  service starts unhealthy (``latched_unhealthy``);
* ``halted:<reason>`` with ``<reason>`` one of :data:`HALT_REASONS` — a §9 / §11 halt.
  Nothing sets one in W2 (no grant exists); the names are fixed now so the store, the
  token and the CLI do not change when the goal loop (W4+) starts using them.

**Fail closed.** An unreadable, unsafe (symlink, hard link, FIFO, another owner, mode
wider than ``0600``), malformed, hash-mismatched or out-of-sequence file means **every
latch is set** (:meth:`LatchStore.latched`), sticky for the process; the file is never
overwritten or repaired here. A missing file is a first start of the store (nothing is
set; the file is written at the first change) — as for the epoch store, removing the file (or rolling
back the data directory) is not detectable here.

Format (JSON, ``v`` = 1): ``{"v": 1, "records": [...]}``, the newest
:data:`KEEP_RECORDS`, oldest first. A record is ``{"seq", "prev", "hash", "latches",
"change"}``: ``latches`` is the whole state after this record (``{name: {"incarnation",
"epoch"}}`` — the stop id of each set latch), ``change`` what this record did
(``{"op": "set" | "clear", "names", "op_id", "by", "payload"}``); ``hash`` is
``sha256("stiracore-authority-latches:v1:" + canonical JSON of the record without
"hash")`` and ``prev`` the previous record's hash. Writes as the epoch store (``0600``,
flush, atomic rename, directory flush), and before every replace the current file is read
again and must be the one this store wrote last (else ``latch_store_replaced``).

**Clear (the operation token).** :meth:`LatchStore.clear` is bound to the clearing
process's incarnation (``by``), the subject (one latch name or ``all``), the stop id of
every latch it clears (``(incarnation, epoch)`` recorded when the latch was set), the
expected generation (the store's newest ``seq``) and an ``op_id``. Any difference from
the current store is ``resume_stale`` (nothing changes). The same ``op_id`` with the same
payload and the same incarnation returns the original result without a second change; with
another payload it is ``op_id_reused``; from another incarnation it is ``op_unknown`` (an
older process's result is never re-applied, ADR 0011 §9).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import private_file

__all__ = [
    "HALT_REASONS",
    "KEEP_RECORDS",
    "LATCHES",
    "LATCH_FILE",
    "LatchRefused",
    "LatchStore",
    "Stop",
    "payload_digest",
]

LATCH_FILE = "authority-latches"
HALT_REASONS = (
    "breaker",
    "emotional",
    "identity",
    "budget",
    "clock_anomaly",
    "unhealthy",
    "restart_conflict",
)
LATCHES = ("kill", "unhealthy", *(f"halted:{reason}" for reason in HALT_REASONS))
KEEP_RECORDS = 64
_MAX_BYTES = 262144
_ZERO = "0" * 64
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RECORD_KEYS = frozenset({"seq", "prev", "hash", "store", "latches", "change"})
_NO_STORE = "0" * 32  # the id of an empty store (nothing written yet)
_CHANGE_KEYS = frozenset({"op", "names", "op_id", "by", "payload"})
_OPS = frozenset({"set", "clear", "reset"})


class LatchRefused(Exception):
    """A refused latch operation; ``code`` says why (a code, never a secret)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Stop:
    """The stop id of a latch: ``(incarnation, epoch)`` when it was set (ADR 0011 §9)."""

    incarnation: str
    epoch: int

    def as_list(self) -> list:
        return [self.incarnation, self.epoch]


def _stop_ok(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == {"incarnation", "epoch"}
        and type(value["incarnation"]) is str
        and bool(_HEX32.fullmatch(value["incarnation"]))
        and type(value["epoch"]) is int
        and value["epoch"] >= 0
    )


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("stiracore-authority-latches:v1:" + text).encode("ascii")).hexdigest()


def payload_digest(subject: str, stops: dict[str, Stop], generation: int) -> str:
    """What an operation token binds: the subject, every stop id it clears, the
    generation. The same ``op_id`` with another payload is refused."""
    body = {
        "subject": subject,
        "stops": {name: stop.as_list() for name, stop in sorted(stops.items())},
        "generation": generation,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("stiracore-latch-op:v1:" + text).encode("ascii")).hexdigest()


def _change_ok(change: Any) -> bool:
    if type(change) is not dict or set(change) != _CHANGE_KEYS or change["op"] not in _OPS:
        return False
    names = change["names"]
    if type(names) is not list or not all(type(n) is str and n in LATCHES for n in names):
        return False
    if change["op"] == "clear":
        return (
            type(change["op_id"]) is str
            and bool(_HEX32.fullmatch(change["op_id"]))
            and type(change["by"]) is str
            and bool(_HEX32.fullmatch(change["by"]))
            and type(change["payload"]) is str
            and bool(_HEX64.fullmatch(change["payload"]))
        )
    return change["op_id"] is None and change["by"] is None and change["payload"] is None


def _parse(raw: bytes) -> tuple[list[dict] | None, str | None]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "latch_store_malformed"
    if (
        type(data) is not dict
        or set(data) != {"v", "records"}
        or type(data["v"]) is not int
        or data["v"] != 1
        or type(data["records"]) is not list
        or not 1 <= len(data["records"]) <= KEEP_RECORDS
    ):
        return None, "latch_store_malformed"
    records: list[dict] = []
    for item in data["records"]:
        if type(item) is not dict or set(item) != _RECORD_KEYS:
            return None, "latch_store_malformed"
        seq, prev, digest, latches = item["seq"], item["prev"], item["hash"], item["latches"]
        if type(seq) is not int or seq < 1:
            return None, "latch_store_malformed"
        if not all(type(v) is str and _HEX64.fullmatch(v) for v in (prev, digest)):
            return None, "latch_store_malformed"
        store = item["store"]
        if type(store) is not str or not _HEX32.fullmatch(store) or store == _NO_STORE:
            return None, "latch_store_malformed"
        if records and store != records[0]["store"]:
            return None, "latch_store_malformed"  # one store instance per file
        if type(latches) is not dict or not all(
            type(k) is str and k in LATCHES and _stop_ok(v) for k, v in latches.items()
        ):
            return None, "latch_store_malformed"
        if not _change_ok(item["change"]):
            return None, "latch_store_malformed"
        if records and item["change"]["op"] == "reset":
            return None, "latch_store_malformed"  # a reset only starts a file
        if digest != _digest(item):
            return None, "latch_store_hash_mismatch"
        if records:
            if seq != records[-1]["seq"] + 1:
                return None, "latch_store_sequence_gap"
            if prev != records[-1]["hash"]:
                return None, "latch_store_hash_mismatch"
        elif seq == 1 and prev != _ZERO:
            return None, "latch_store_hash_mismatch"
        records.append(item)
    # A short file starts at seq 1 — or with a reset record that continues the numbering
    # of the store it replaced (W2 fix 2: the generation never restarts).
    first = records[0]
    if len(records) < KEEP_RECORDS and first["seq"] != 1 and first["change"]["op"] != "reset":
        return None, "latch_store_sequence_gap"
    return records, None


class LatchStore:
    """The latches of one data directory (the file is written at the first change, never
    at a read). Every method takes the store's own lock (I/O under it; never the
    authority's leaf lock)."""

    def __init__(self, data_dir: Path | None) -> None:
        self._dir = None if data_dir is None else Path(data_dir)
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._identity: tuple[int, int] | None = None
        self._bad: str | None = None
        if self._dir is None:
            return
        loaded = private_file.read(
            self._dir / LATCH_FILE, max_bytes=_MAX_BYTES, prefix="latch_store"
        )
        if loaded.status == "bad":
            self._bad = loaded.reason or "latch_store_unreadable"
            return
        if loaded.status == "missing":
            return  # a first start: written at the first change
        records, reason = _parse(loaded.raw)
        if records is None:
            self._bad = reason
            return
        self._records = records
        self._identity = loaded.identity

    # -- reading ---------------------------------------------------------------------------

    @property
    def unhealthy_reason(self) -> str | None:
        """The sticky reason this store is unusable (every latch then counts as set)."""
        return self._bad

    @property
    def generation(self) -> int:
        with self._lock:
            return self._records[-1]["seq"] if self._records else 0

    @property
    def head(self) -> str:
        """The newest record's hash (64 zeros for an empty store): the commitment."""
        with self._lock:
            return self._records[-1]["hash"] if self._records else _ZERO

    def commitment(self) -> tuple[int, str, str] | None:
        """``(generation, head, store id)``, or ``None`` when the store is unusable."""
        with self._lock:
            if self._bad is not None:
                return None
            if not self._records:
                return (0, _ZERO, _NO_STORE)
            last = self._records[-1]
            return (last["seq"], last["hash"], last["store"])

    def descends_from(self, commitment: tuple[int, str, str]) -> bool:
        """Lineage (W2 fix 2, O-M1): this store is the committed one or its successor —
        it holds the committed record (same generation, hash and store id) in its window,
        or its first record continues it directly (the first write after an empty
        commitment, or a ``reset-latches`` store). A backup taken before, a store of
        another instance, a rolled-back or a same-generation different store: False."""
        gen, head, store = commitment
        with self._lock:
            if self._bad is not None:
                return False
            if not self._records:
                return gen == 0
            for record in self._records:
                if record["seq"] == gen:
                    return record["hash"] == head and record["store"] == store
            first = self._records[0]
            return first["seq"] == gen + 1 and first["prev"] == head

    def poison(self, reason: str) -> None:
        """Mark the store unusable (sticky: every latch counts as set; nothing is
        written). Used when it disagrees with the epoch store's commitment or the
        lifecycle log, or when a pass finds the file changed."""
        with self._lock:
            if self._bad is None:
                self._bad = reason

    def verify(self) -> str | None:
        """Re-read the file and compare it with what this store read or wrote last (the
        watchdog's pass). A difference poisons the store; the reason, or None."""
        if self._dir is None:
            return self._bad
        with self._lock:
            if self._bad is not None:
                return self._bad
            problem = self._expected_on_disk()
            if problem is not None:
                self._bad = problem
            return self._bad

    def latched(self) -> dict[str, Stop | None]:
        """Every set latch with its stop id. An unusable store: **every** latch, with no
        stop id (fail closed)."""
        with self._lock:
            if self._bad is not None:
                return dict.fromkeys(LATCHES)
            if not self._records:
                return {}
            return {
                name: Stop(value["incarnation"], value["epoch"])
                for name, value in self._records[-1]["latches"].items()
            }

    def is_set(self, name: str) -> bool:
        return name in self.latched()

    def view(self) -> dict[str, Any]:
        """Codes and numbers only (never an incarnation): for the status route / CLI."""
        latched = self.latched()
        return {
            "healthy": self._bad is None,
            "unhealthy_reason": self._bad,
            "generation": self.generation,
            "latches": sorted(latched),
            "stops": {
                name: (None if stop is None else stop.epoch) for name, stop in latched.items()
            },
        }

    # -- writing ---------------------------------------------------------------------------

    def _record(self, last, latches, op, names, op_id, by, payload, *, base=None) -> dict:
        """The next record. ``base`` = ``(seq, prev)`` of a reset record continuing the
        store it replaces."""
        if base is not None:
            seq, prev = base
        else:
            seq = 1 if last is None else last["seq"] + 1
            prev = _ZERO if last is None else last["hash"]
        if last is not None:
            store = last["store"]
        else:
            store = os.urandom(16).hex()  # a new store instance
        record = {
            "seq": seq,
            "prev": prev,
            "store": store,
            "latches": latches,
            "change": {"op": op, "names": names, "op_id": op_id, "by": by, "payload": payload},
        }
        record["hash"] = _digest(record)
        return record

    def _expected_on_disk(self) -> str | None:
        loaded = private_file.read(
            self._dir / LATCH_FILE, max_bytes=_MAX_BYTES, prefix="latch_store"
        )
        if self._identity is None:
            return None if loaded.status == "missing" else "latch_store_replaced"
        if loaded.status == "bad":
            return loaded.reason or "latch_store_unreadable"
        if loaded.status != "ok":
            return "latch_store_replaced"
        records, reason = _parse(loaded.raw)
        if records is None:
            return reason
        if loaded.identity != self._identity or records != self._records:
            return "latch_store_replaced"
        return None

    def _write_locked(self, record: dict) -> None:
        """Persist ``record`` (the caller holds ``_lock`` or is the constructor); the
        in-memory state changes only after the write succeeded."""
        records = [*self._records, record][-KEEP_RECORDS:]
        if self._dir is None:
            self._records = records
            return
        problem = self._expected_on_disk()
        if problem is not None:
            self._bad = problem
            raise OSError(problem)
        body = json.dumps({"v": 1, "records": records}, sort_keys=True).encode("ascii")
        try:
            self._identity = private_file.replace(self._dir, LATCH_FILE, body)
        except OSError:
            self._bad = "latch_store_write_failed"
            raise
        self._records = records

    def set(self, name: str, *, incarnation: str, epoch: int) -> bool:
        """Set ``name`` with its stop id; True when it was newly set (an already set latch
        keeps its first stop id). Raises :class:`LatchRefused` for an unknown name and
        ``OSError`` when it cannot be persisted (the store is then unusable: every latch
        counts as set)."""
        if type(name) is not str or name not in LATCHES:
            raise LatchRefused("latch_unknown")
        if type(incarnation) is not str or not _HEX32.fullmatch(incarnation):
            raise LatchRefused("incarnation_invalid")
        if type(epoch) is not int or epoch < 0:
            raise LatchRefused("epoch_invalid")
        with self._lock:
            if self._bad is not None:
                raise OSError(self._bad)
            last = self._records[-1] if self._records else None
            current = dict(last["latches"]) if last is not None else {}
            if name in current:
                return False
            current[name] = {"incarnation": incarnation, "epoch": epoch}
            self._write_locked(self._record(last, current, "set", [name], None, None, None))
            return True

    def reset_all(
        self, *, incarnation: str, epoch: int, after: tuple[int, str, str] | None
    ) -> tuple[int, str, str]:
        """``stira authority reset-latches`` on a fresh (missing) file, server stopped:
        one ``reset`` record with **every** latch set, a new store id, and — after the
        committed store ``after`` — the next generation, chained to its head (the
        generation never restarts). The new commitment."""
        if type(incarnation) is not str or not _HEX32.fullmatch(incarnation):
            raise LatchRefused("incarnation_invalid")
        with self._lock:
            if self._bad is not None or self._records:
                raise OSError(self._bad or "latch_store_not_empty")
            current = {name: {"incarnation": incarnation, "epoch": epoch} for name in LATCHES}
            base = None if after is None or after[0] == 0 else (after[0] + 1, after[1])
            record = self._record(
                None, current, "reset", list(LATCHES), None, None, None, base=base
            )
            self._write_locked(record)
            return (record["seq"], record["hash"], record["store"])

    def clear(
        self,
        subject: str,
        *,
        stops: dict[str, Stop],
        expected_generation: int,
        op_id: str,
        by: str,
    ) -> dict[str, Any]:
        """A person's clear (the operation token's apply step). ``subject``: one latch
        name or ``all``; ``stops``: the stop id of every latch it clears, as the person
        saw them; ``by``: the clearing process's incarnation. See the module docstring."""
        if type(subject) is not str or (subject != "all" and subject not in LATCHES):
            raise LatchRefused("latch_unknown")
        if type(op_id) is not str or not _HEX32.fullmatch(op_id):
            raise LatchRefused("op_invalid")
        if type(by) is not str or not _HEX32.fullmatch(by):
            raise LatchRefused("op_invalid")
        if type(expected_generation) is not int or type(stops) is not dict:
            raise LatchRefused("op_invalid")
        if not all(type(k) is str and type(v) is Stop for k, v in stops.items()):
            raise LatchRefused("op_invalid")
        payload = payload_digest(subject, stops, expected_generation)
        with self._lock:
            if self._bad is not None:
                raise LatchRefused("latch_store_unhealthy")
            for record in self._records:
                change = record["change"]
                if change["op_id"] != op_id:
                    continue
                if change["by"] != by:
                    raise LatchRefused("op_unknown")  # another incarnation's operation
                if change["payload"] != payload:
                    raise LatchRefused("op_id_reused")
                return {
                    "cleared": list(change["names"]),
                    "generation": record["seq"],
                    "replayed": True,
                }
            if not self._records:
                raise LatchRefused("resume_stale")  # nothing is set
            last = self._records[-1]
            current = dict(last["latches"])
            if last["seq"] != expected_generation:
                raise LatchRefused("resume_stale")
            names = sorted(current) if subject == "all" else [subject]
            if not names or set(stops) != set(names):
                raise LatchRefused("resume_stale")
            for name in names:
                value = current.get(name)
                if value is None or Stop(value["incarnation"], value["epoch"]) != stops[name]:
                    raise LatchRefused("resume_stale")
            for name in names:
                del current[name]
            record = self._record(last, current, "clear", names, op_id, by, payload)
            try:
                self._write_locked(record)
            except OSError as error:
                raise LatchRefused("latch_store_write_failed") from error
            return {"cleared": names, "generation": record["seq"], "replayed": False}

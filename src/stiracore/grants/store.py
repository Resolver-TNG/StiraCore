"""The durable grant store ``grants.json`` (ADR 0011 §7, §8; W4 of #296).

The **only** place the goal text is written. One file in the data directory, ``0600``,
under the same safe-open rules as the authority's files
(:mod:`stiracore.authority.private_file`: no symlink, no hard link, no FIFO, this user,
``0600`` or tighter, the opened descriptor checked again; atomic replace with flushes).

Format: a JSON list of the newest :data:`KEEP_RECORDS` records ``{"seq", "prev", "hash",
"grants"}``, each the whole state; ``hash = sha256("stiracore-grants:v1:" + canonical JSON
without "hash")``, ``prev`` the previous record's hash, ``seq`` +1 per write. Every grant
entry is checked by :func:`.schema.grant_from_dict` (unknown keys refused).

**Never persisted as active.** The service writes a live grant (``active`` / ``paused`` /
``halted``) as what it becomes at the next start: ``revoked(restart)``, its goal ``unmet``,
with ``restart_from`` naming the live state. At start every such entry is recorded as the transition
``→ revoked(restart)`` (the W2 lifecycle recorder) and written back without the flag. A
copy of the file can therefore never bring an active grant back.

**Unhealthy is sticky** (fail closed): an unsafe, unreadable, malformed, hash-mismatched or
gapped file at start; a file changed or replaced behind the store (re-read before every
replace and on every :meth:`GrantStore.verify`); a failed write. Then nothing is written
any more — **the evidence is never overwritten** — and the service refuses to issue. A
person repairs it (keep a copy, move the file aside) and restarts.

Retention: at most :data:`MAX_GRANTS` grants are kept; the oldest ended ones (and their
goal text) are dropped first.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from stiracore.authority import private_file

from .schema import GrantRefused, grant_from_dict

__all__ = ["FILE", "KEEP_RECORDS", "MAX_BYTES", "MAX_GRANTS", "GrantStore", "StoreLoad"]

FILE = "grants.json"
KEEP_RECORDS = 4
MAX_GRANTS = 16
MAX_BYTES = 2 << 20
_ZERO = "0" * 64
# ``restart_from``: "" (ended before the write) or the live state it was written from.
_LIVE = frozenset({"", "active", "paused", "halted"})
_HEX64_LEN = 64


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("stiracore-grants:v1:" + text).encode("ascii")).hexdigest()


def _hex64(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == _HEX64_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


class StoreLoad:
    """What the start-up read found: ``entries`` = ``[(grant, goal, restart_from)]``."""

    def __init__(self, entries: list, reason: str | None = None) -> None:
        self.entries = entries
        self.reason = reason


def _parse(raw: bytes) -> tuple[list[dict] | None, str | None]:
    try:
        records = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "grant_store_malformed"
    if type(records) is not list or not records or len(records) > KEEP_RECORDS:
        return None, "grant_store_malformed"
    for index, record in enumerate(records):
        if type(record) is not dict or set(record) != {"seq", "prev", "hash", "grants"}:
            return None, "grant_store_malformed"
        if type(record["seq"]) is not int or record["seq"] < 1:
            return None, "grant_store_malformed"
        if not _hex64(record["prev"]) or not _hex64(record["hash"]):
            return None, "grant_store_malformed"
        if type(record["grants"]) is not list or len(record["grants"]) > MAX_GRANTS:
            return None, "grant_store_malformed"
        if record["hash"] != _digest(record):
            return None, "grant_store_hash_mismatch"
        if index:
            before = records[index - 1]
            if record["seq"] != before["seq"] + 1:
                return None, "grant_store_sequence_gap"
            if record["prev"] != before["hash"]:
                return None, "grant_store_hash_mismatch"
        elif len(records) < KEEP_RECORDS and (record["seq"] != 1 or record["prev"] != _ZERO):
            return None, "grant_store_sequence_gap"
    return records, None


def _entries(record: dict) -> tuple[list | None, str | None]:
    out = []
    seen = set()
    for item in record["grants"]:
        if type(item) is not dict or set(item) != {"grant", "restart_from"}:
            return None, "grant_store_malformed"
        if type(item["restart_from"]) is not str or item["restart_from"] not in _LIVE:
            return None, "grant_store_malformed"
        try:
            grant, goal = grant_from_dict(item["grant"])
        except GrantRefused:
            return None, "grant_store_malformed"
        if grant.grant_id in seen:
            return None, "grant_store_malformed"
        seen.add(grant.grant_id)
        if item["restart_from"] and (grant.status, grant.reason) != ("revoked", "restart"):
            return None, "grant_store_malformed"  # a live grant is only ever stored revoked
        if grant.status not in ("revoked", "expired", "ended"):
            return None, "grant_store_malformed"  # never persisted as active
        out.append((grant, goal, item["restart_from"]))
    return out, None


class GrantStore:
    """``grants.json`` of one data directory (``None``: in memory only, for tests)."""

    def __init__(self, data_dir: Path | None) -> None:
        self._dir = None if data_dir is None else Path(data_dir)
        self._lock = threading.Lock()  # I/O lock: never taken under the service's lock
        self._bad: str | None = None
        self._records: list[dict] = []
        self._identity: tuple[int, ...] | None = None
        self._raw: bytes | None = None
        self._loaded = False

    @property
    def unhealthy_reason(self) -> str | None:
        return self._bad

    def load(self) -> StoreLoad:
        """Read the file once at start (a missing file: no grants)."""
        with self._lock:
            self._loaded = True
            if self._dir is None:
                return StoreLoad([])
            loaded = private_file.read(self._dir / FILE, max_bytes=MAX_BYTES, prefix="grant_store")
            if loaded.status == "missing":
                return StoreLoad([])
            if loaded.status == "bad":
                self._bad = loaded.reason or "grant_store_unreadable"
                return StoreLoad([], self._bad)
            records, reason = _parse(loaded.raw)
            if records is None:
                self._bad = reason
                return StoreLoad([], reason)
            entries, reason = _entries(records[-1])
            if entries is None:
                self._bad = reason
                return StoreLoad([], reason)
            self._records = records
            self._identity = loaded.identity
            self._raw = loaded.raw
            return StoreLoad(entries)

    def _changed_locked(self) -> str | None:
        """The current file is not the one this store read or wrote last."""
        loaded = private_file.read(self._dir / FILE, max_bytes=MAX_BYTES, prefix="grant_store")
        if self._identity is None:
            return None if loaded.status == "missing" else "grant_store_replaced"
        if loaded.status == "bad":
            return loaded.reason or "grant_store_unreadable"
        if loaded.status != "ok" or loaded.identity != self._identity or loaded.raw != self._raw:
            return "grant_store_replaced"
        return None

    def verify(self) -> str | None:
        """Re-read and compare (the watchdog's pass). A difference is sticky."""
        if self._dir is None:
            return self._bad
        with self._lock:
            if self._bad is None and self._loaded:
                self._bad = self._changed_locked()
            return self._bad

    def write(self, entries: list[dict]) -> bool:
        """Write the whole state (``entries``: ``{"grant": <stored dict>,
        "restart_from": "" | live state}``). False — and unhealthy, sticky — when the file changed
        behind the store or the write failed; nothing is written once unhealthy."""
        with self._lock:
            if self._bad is not None:
                return False
            last = self._records[-1] if self._records else None
            record = {
                "seq": 1 if last is None else last["seq"] + 1,
                "prev": _ZERO if last is None else last["hash"],
                "grants": entries,
            }
            record["hash"] = _digest(record)
            records = [*self._records, record][-KEEP_RECORDS:]
            body = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            raw = body.encode("ascii")
            if len(raw) > MAX_BYTES or _parse(raw)[0] is None or _entries(record)[0] is None:
                # Never write what the reader would refuse (the next start would be stuck).
                self._bad = "grant_store_write_failed"
                return False
            if self._dir is None:
                self._records = records
                return True
            problem = self._changed_locked()
            if problem is not None:
                self._bad = problem
                return False
            try:
                identity = private_file.replace(self._dir, FILE, raw)
            except OSError:
                self._bad = "grant_store_write_failed"
                return False
            self._records = records
            self._identity = identity
            self._raw = raw
            return True

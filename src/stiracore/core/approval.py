"""HITL approval binding and the side-effect ledger for crash-resume (#146).

An approval is bound to ``agent_id`` + ``tool_name`` + a hash of the
*normalized* arguments + an expiry. Anything else (a changed argument, a late
answer, a second answer, an edit) is a different approval and fails closed.

Checkpoints are at-least-once (``core/checkpoint.py``): a restored tool batch
re-runs its tool calls. For HITL-approved operations the ``OperationLedger``
records ``started`` immediately before execution and ``committed``/``failed``
right after, keyed by the operation key (agent, tool, args hash, toolUseId).
On replay:

* ``committed`` -> the tool is not executed again (duplicate prevented);
* ``started``/``failed`` -> the outcome is unknown. A per-tool reconciler can
  check the external system (``True`` = already applied, ``False`` = not
  applied, re-ask for approval); without a reconciler, or if it cannot decide,
  the call is refused (fail-closed), never blindly re-executed.

The operation key is also exposed to the tool as an idempotency key so an
external API can deduplicate on its side as well.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# invocation_state keys shared by the hooks and tools (see agent/checkpoint_hooks.py).
HITL_BINDINGS_KEY = "stira.hitl.bindings"
HITL_IDEMPOTENCY_KEYS_KEY = "stira.hitl.idempotency_keys"

LEDGER_STARTED = "started"
LEDGER_COMMITTED = "committed"
LEDGER_FAILED = "failed"

Reconciler = Callable[[dict[str, Any], str], "bool | None"]


def _check_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("tool input must not contain NaN or infinity")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("tool input keys must be strings")
            _check_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite(item)


def canonical_json(value: Any) -> str:
    """Strict, stable JSON for display and hashing: sorted keys, no whitespace.

    Raises TypeError/ValueError for anything JSON cannot represent exactly, so a
    caller can fail closed instead of approving an ambiguous rendering.
    """
    _check_finite(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def args_hash(tool_input: Any) -> str:
    """sha256 of the canonical JSON of the tool input (hex)."""
    return hashlib.sha256(canonical_json(tool_input).encode("utf-8")).hexdigest()


def operation_key(agent_id: str, tool_name: str, digest: str, tool_use_id: str) -> str:
    """Stable identity of one approved side effect, also usable as an idempotency key."""
    material = canonical_json([agent_id or "", tool_name, digest, tool_use_id or ""])
    return "op-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ApprovalBinding:
    """What a human actually approved. All fields must match at execution time."""

    request_id: str
    agent_id: str
    tool_name: str
    args_hash: str
    expires_at: float

    def check(self, *, agent_id: str, tool_name: str, tool_input: Any, now: float) -> str | None:
        """Return None when the call matches this approval, else a refusal reason."""
        if not now <= self.expires_at:  # NaN (either side) is expired (#601 B6)
            return "approval expired"
        if agent_id != self.agent_id or tool_name != self.tool_name:
            return "approval is bound to a different agent or operation"
        try:
            digest = args_hash(tool_input)
        except (TypeError, ValueError):
            return "tool input cannot be normalized"
        if digest != self.args_hash:
            return "tool input changed after approval"
        return None


@dataclass(frozen=True)
class OperationRecord:
    key: str
    agent_id: str
    tool_name: str
    args_hash: str
    tool_use_id: str
    request_id: str | None
    state: str
    updated_at: float

    @property
    def ungated(self) -> bool:
        """ADR 0024 §5: recorded for a call that ran without an approval (rule Always)."""
        return self.request_id is None


class OperationLedger:
    """Durable (SQLite file) or process-local (``path=None``) side-effect ledger.

    Only the operation identity and its state are stored: no tool arguments or
    results, so the ledger never becomes a second copy of private content.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        reconcilers: Mapping[str, Reconciler] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.path = None if path is None else Path(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reconcilers = dict(reconcilers or {})
        self._now = now_fn or time.time
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            ":memory:" if self.path is None else str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        with self._lock:
            if self.path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS operations ("
                " key TEXT PRIMARY KEY, agent_id TEXT NOT NULL, tool_name TEXT NOT NULL,"
                " args_hash TEXT NOT NULL, tool_use_id TEXT NOT NULL, request_id TEXT,"
                " state TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            # #601 B1 review: every write stamps a strictly increasing ``updated_at``, so
            # (key, state, request_id, updated_at) names one generation of a record even
            # with a frozen or coarse clock.
            row = self._conn.execute("SELECT MAX(updated_at) FROM operations").fetchone()
            self._last_stamp = float(row[0]) if row and row[0] is not None else -math.inf
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)

    def _stamp_locked(self) -> float:
        now = float(self._now())
        stamp = now if now > self._last_stamp else self._last_stamp + 1e-6
        if not stamp > self._last_stamp:  # float spacing beyond 1e-6 at this magnitude
            stamp = math.nextafter(self._last_stamp, math.inf)
        self._last_stamp = stamp
        return stamp

    def get(self, key: str) -> OperationRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, agent_id, tool_name, args_hash, tool_use_id, request_id, state,"
                " updated_at FROM operations WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else OperationRecord(*row)

    def mark_started(
        self,
        key: str,
        *,
        agent_id: str,
        tool_name: str,
        digest: str,
        tool_use_id: str,
        request_id: str | None,
    ) -> bool:
        """Claim the operation. False if it is already recorded (do not execute)."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    agent_id,
                    tool_name,
                    digest,
                    tool_use_id,
                    request_id,
                    LEDGER_STARTED,
                    self._stamp_locked(),
                ),
            )
            return cursor.rowcount == 1

    def finish(self, key: str, *, success: bool) -> None:
        state = LEDGER_COMMITTED if success else LEDGER_FAILED
        with self._lock:
            self._conn.execute(
                "UPDATE operations SET state = ?, updated_at = ? WHERE key = ?",
                (state, self._stamp_locked(), key),
            )

    def forget(self, key: str) -> None:
        """Drop a record a reconciler proved was never applied."""
        with self._lock:
            self._conn.execute("DELETE FROM operations WHERE key = ?", (key,))

    def reconcile(self, record: OperationRecord, tool_input: dict[str, Any]) -> bool | None:
        """Ask the tool's reconciler whether an unknown outcome was applied."""
        reconciler = self.reconcilers.get(record.tool_name)
        if reconciler is None:
            return None
        try:
            applied = reconciler(dict(tool_input), record.key)
        except Exception:  # noqa: BLE001 - a broken reconciler must fail closed
            return None
        if applied is True:
            self.finish(record.key, success=True)
            return True
        if applied is False:
            # Only the record this reconcile observed: a newer claim/commit of the same key
            # (another path, meanwhile) is never deleted by a late answer (#601 B1).
            # When the record changed meanwhile, the outcome is unknown again (None).
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM operations WHERE key = ? AND state = ? AND updated_at = ?"
                    " AND request_id IS ?",
                    (record.key, record.state, record.updated_at, record.request_id),
                )
            return False if cursor.rowcount == 1 else None
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

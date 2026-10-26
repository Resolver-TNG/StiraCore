"""Versioned UI turn-event contract with SQLite persistence and replay (#186).

Events never store prompt/response bodies; tool previews are masked and capped.
Unknown event types are rejected; reserved types (memory.injected) validate; the chat
server emits memory.injected only when the opt-in associative recall is on (#170), and its
data must match the allow-list in ``memory_injected``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from .hooks import ObservabilityEvent
from .memory_injected import validate_memory_injected
from .model_progress import ModelCallProgress, call_id, completed_data, model_id
from .security import mask_secrets

__all__ = [
    "SCHEMA_VERSION",
    "ActiveTurn",
    "TurnEventRecorder",
    "TurnEventStore",
    "UIEventSink",
    "capture_payload",
    "make_preview",
    "sanitize_data",
    "validate_event",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Why a finished turn has no token count (#363): codes the chat route writes into
# ``turn.completed.usage_reason``. /observability shows only these (else a fixed word).
USAGE_NOT_REPORTED = "E_USAGE_NOT_REPORTED"  # the provider sent no usage before the turn ended
USAGE_CANCELLED = "E_USAGE_CANCELLED"  # the turn was stopped (stop button / cancel) first
# The server stopped or restarted before the turn finished (#452).
USAGE_INTERRUPTED = "E_USAGE_INTERRUPTED"
USAGE_REASONS = frozenset({USAGE_NOT_REPORTED, USAGE_CANCELLED, USAGE_INTERRUPTED})
# turn.completed.reason of a turn the server ended (#452): stopped while it ran, or found
# without an end at the next start (a crash, a kill).
INTERRUPT_REASONS = frozenset({"server_stopped", "server_restarted", "worker_stopped"})
EVENT_TYPES = frozenset(
    {
        "turn.started",
        "turn.summary",
        "tool_call.created",
        "tool_call.updated",
        "approval.requested",
        "approval.resolved",
        "breaker.transition",
        "turn.completed",
        # ADR 0014 phase 1 (#451): codes, counts and ids only (the note is the turn's text).
        "progress.report",
        "progress.reply",
        "progress.auto_continue",
        # #445: one model call — start, elapsed while running, end (ids, codes, numbers).
        "model.started",
        "model.progress",
        "model.completed",
        # A routed side-task model call (docs/task-models.md): task, provider, model id,
        # tokens, status and a reason code. Never the text.
        "task_model.call",
    }
)
RESERVED_TYPES = frozenset({"memory.injected"})
TOOL_STATUSES = frozenset({"pending", "running", "done", "error", "denied", "timeout"})
PREVIEW_LIMIT = 2000
PREVIEW_LINES = 200
# Tool payload detail (#188): retained masked text, served in pages of <= 200 lines / 100 KiB.
PAYLOAD_RETAIN_BYTES = 1024 * 1024
PAGE_LINES = 200
PAGE_BYTES = 100 * 1024
PAYLOAD_KINDS = ("input", "output")
MAX_EVENT_BYTES = 64 * 1024
_REQUIRED = ("schema_version", "event_id", "turn_id", "seq", "ts", "type", "data")
_DATA_REQUIRED = {
    "tool_call.created": ("tool_use_id", "name", "status"),
    "tool_call.updated": ("tool_use_id", "name", "status"),
    "approval.requested": ("request_id",),
    "approval.resolved": ("request_id", "decision"),
    "breaker.transition": ("from", "to"),
    "progress.report": ("report_id", "trigger", "end"),
    "progress.reply": ("report_id", "answer"),
    "progress.auto_continue": ("report_id", "n"),
    "model.started": ("call_id",),
    "model.progress": ("call_id", "elapsed_ms"),
    "model.completed": ("call_id", "duration_ms"),
    "task_model.call": ("task", "status"),
}
_PREVIEW_KEYS = ("input_preview", "output_preview")
# turn.summary fields listed by recent_turns so the chat ribbon can restore after reload (#187).
_SUMMARY_KEYS = (
    "category",
    "intensity",
    "confidence",
    "classifier",
    "emotion_transcription",
    "deltas",
    "before",
    "after",
    "breaker",
)


def _dumps(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, default=str, separators=(",", ":")
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def make_preview(value: Any) -> tuple[str | None, bool]:
    """Mask secrets, then cap to PREVIEW_LIMIT characters."""
    if value is None:
        return None, False
    masked = mask_secrets(value)
    text = masked if isinstance(masked, str) else _dumps(masked)
    text = mask_secrets(text)
    if len(text) > PREVIEW_LIMIT:
        return text[:PREVIEW_LIMIT], True
    return text, False


def _cut_bytes(text: str, limit: int) -> str:
    encoded = text.encode()
    return text if len(encoded) <= limit else encoded[:limit].decode(errors="ignore")


def _payload_text(kind: str, value: Any) -> str | None:
    """Readable text for a tool input (args) or a Strands ToolResult."""
    if value is None:
        return None
    if kind == "output" and isinstance(value, Mapping) and "content" in value:
        parts = []
        for block in value.get("content") or []:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, Mapping) and "json" in block:
                parts.append(_dumps(block["json"]))
            elif isinstance(block, Mapping) and block:
                parts.append(f"[{next(iter(block))}]")  # image/document: type only
        return "\n".join(parts)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return f"[{type(value).__name__}]"


def capture_payload(kind: str, value: Any) -> dict[str, Any] | None:
    """Mask secrets (#185) and cap a tool input/output for the turn UI (#188).

    Returns {"text", "total_chars", "total_lines", "retained_truncated"}; the text is
    masked before and after rendering and capped at PAYLOAD_RETAIN_BYTES.
    """
    if kind not in PAYLOAD_KINDS:
        return None
    text = _payload_text(kind, mask_secrets(value))
    if text is None:
        return None
    text = mask_secrets(text)
    kept = _cut_bytes(text, PAYLOAD_RETAIN_BYTES)
    return {
        "text": kept,
        "total_chars": len(text),
        "total_lines": text.count("\n") + 1 if text else 0,
        "retained_truncated": kept != text,
    }


def _preview_fields(kind: str, captured: Any) -> dict[str, Any]:
    """Event fields for a captured payload: preview <= PREVIEW_LINES / PREVIEW_LIMIT."""
    if not isinstance(captured, Mapping) or not isinstance(captured.get("text"), str):
        return {}
    text = captured["text"]
    lines = text.split("\n")
    preview = "\n".join(lines[:PREVIEW_LINES])
    preview, cut = make_preview(preview)
    total_chars = captured.get("total_chars")
    return {
        f"{kind}_preview": preview,
        f"{kind}_chars": total_chars if _is_int(total_chars) else len(text),
        f"{kind}_lines": captured.get("total_lines")
        if _is_int(captured.get("total_lines"))
        else len(lines),
        f"{kind}_truncated": bool(
            cut or len(lines) > PREVIEW_LINES or captured.get("retained_truncated")
        ),
    }


def sanitize_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("data must be a mapping")  # noqa: TRY004 - contract: ValueError
    try:
        out = json.loads(_dumps(dict(data)))
    except (TypeError, ValueError) as error:
        raise ValueError("data must be JSON serializable") from error
    truncated = False
    for key in _PREVIEW_KEYS:
        if key in out:
            out[key], cut = make_preview(out[key])
            truncated = truncated or cut
    if truncated:
        out["truncated"] = True
    return out


def validate_event(event: Any) -> Mapping:
    if not isinstance(event, Mapping):
        raise ValueError("event must be a mapping")  # noqa: TRY004 - contract: ValueError
    missing = [key for key in _REQUIRED if key not in event]
    if missing:
        raise ValueError(f"missing keys: {missing}")
    if not _is_int(event["schema_version"]) or event["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    for key in ("event_id", "turn_id"):
        value = event[key]
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise ValueError(f"{key} must be a nonempty safe string")
    if not _is_int(event["seq"]) or event["seq"] < 1:
        raise ValueError("seq must be a positive integer")
    ts = event["ts"]
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < 0:
        raise ValueError("ts must be epoch seconds")
    kind = event["type"]
    if not isinstance(kind, str) or kind not in EVENT_TYPES | RESERVED_TYPES:
        raise ValueError(f"unknown event type: {kind!r}")
    data = event["data"]
    if not isinstance(data, dict):
        raise ValueError("data must be a dict")  # noqa: TRY004 - contract: ValueError
    for key in _DATA_REQUIRED.get(kind, ()):
        if key not in data:
            raise ValueError(f"{kind} requires data.{key}")
    if kind.startswith("tool_call.") and data["status"] not in TOOL_STATUSES:
        raise ValueError("invalid tool_call status")
    if kind == "memory.injected":
        validate_memory_injected(data)
    for key in _PREVIEW_KEYS:
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > PREVIEW_LIMIT):
            raise ValueError(f"{key} must be a string of at most {PREVIEW_LIMIT} chars")
    try:
        size = len(_dumps(event).encode())
    except ValueError as error:
        raise ValueError("event is not JSON serializable") from error
    if size > MAX_EVENT_BYTES:
        raise ValueError("event exceeds size limit")
    return event


class TurnEventStore:
    """SQLite store; seq is allocated per turn under a lock."""

    def __init__(self, db_path) -> None:
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA busy_timeout = 30000")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ui_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    seq INT NOT NULL,
                    type TEXT NOT NULL,
                    ts REAL NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(turn_id, seq)
                );
                CREATE TABLE IF NOT EXISTS tool_payloads (
                    turn_id TEXT NOT NULL,
                    tool_use_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    total_chars INT NOT NULL,
                    total_lines INT NOT NULL,
                    retained_truncated INT NOT NULL,
                    PRIMARY KEY (turn_id, tool_use_id, kind)
                );
                """
            )

    def append(self, turn_id: str, event_type: str, data: Mapping | None = None) -> dict:
        clean = sanitize_data(data or {})
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM ui_events WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": uuid4().hex,
                "turn_id": turn_id,
                "seq": int(row[0]) + 1,
                "ts": time.time(),
                "type": event_type,
                "data": clean,
            }
            validate_event(event)
            with self._conn:
                self._conn.execute(
                    "INSERT INTO ui_events (turn_id, seq, type, ts, payload)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (turn_id, event["seq"], event_type, event["ts"], _dumps(event)),
                )
        return event

    def events(self, turn_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM ui_events WHERE turn_id = ? AND seq > ? ORDER BY seq",
                (turn_id, after_seq),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def put_payload(self, turn_id: str, tool_use_id: str, kind: str, captured: Mapping) -> None:
        """Store an already captured (masked, capped) tool payload; masks again."""
        if kind not in PAYLOAD_KINDS or not tool_use_id:
            raise ValueError("invalid payload key")
        text = mask_secrets(str(captured.get("text", "")))
        text = _cut_bytes(text, PAYLOAD_RETAIN_BYTES)
        total_chars = captured.get("total_chars")
        total_lines = captured.get("total_lines")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tool_payloads (turn_id, tool_use_id, kind, text,"
                " total_chars, total_lines, retained_truncated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    tool_use_id,
                    kind,
                    text,
                    total_chars if _is_int(total_chars) else len(text),
                    total_lines if _is_int(total_lines) else text.count("\n") + 1,
                    int(bool(captured.get("retained_truncated"))),
                ),
            )

    def payload_page(
        self, turn_id: str, tool_use_id: str, kind: str, offset: int = 0
    ) -> dict | None:
        """One page (<= PAGE_LINES lines, <= PAGE_BYTES) of a stored payload, masked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_payloads WHERE turn_id = ? AND tool_use_id = ? AND kind = ?",
                (turn_id, tool_use_id, kind),
            ).fetchone()
        if row is None:
            return None
        lines = row["text"].split("\n")
        offset = max(0, offset)
        out: list[str] = []
        size, line_cut = 0, False
        for line in lines[offset : offset + PAGE_LINES]:
            cost = len(line.encode()) + 1
            if size + cost > PAGE_BYTES:
                if not out:  # a single oversized line: cut it rather than stall
                    out.append(_cut_bytes(line, PAGE_BYTES))
                    line_cut = True
                break
            out.append(line)
            size += cost
        consumed = offset + len(out)
        next_offset = consumed if consumed < len(lines) else None
        return {
            "turn_id": turn_id,
            "tool_use_id": tool_use_id,
            "kind": kind,
            "offset": offset,
            "text": mask_secrets("\n".join(out)),
            "next_offset": next_offset,
            "line_cut": line_cut,
            "retained_lines": len(lines),
            "total_lines": row["total_lines"],
            "total_chars": row["total_chars"],
            "retained_truncated": bool(row["retained_truncated"]),
            "page_lines": PAGE_LINES,
            "page_bytes": PAGE_BYTES,
        }

    def events_after(self, after_id: int = 0, limit: int = 100) -> list[tuple[int, dict]]:
        """Rows in insertion order after ``after_id`` as ``(row id, event)`` (observability, #286)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM ui_events WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [(int(row["id"]), json.loads(row["payload"])) for row in rows]

    def tail(self, limit: int = 50) -> list[tuple[int, dict]]:
        """The last ``limit`` rows, oldest first, as ``(row id, event)``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM ui_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(int(row["id"]), json.loads(row["payload"])) for row in reversed(rows)]

    def tail_types(self, types, limit: int = 50) -> list[tuple[int, dict]]:
        """The last ``limit`` rows of the given types, oldest first, as ``(row id, event)``."""
        kinds = sorted({str(kind) for kind in types})
        if not kinds:
            return []
        marks = ", ".join("?" for _ in kinds)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, payload FROM ui_events WHERE type IN ({marks})"
                " ORDER BY id DESC LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        return [(int(row["id"]), json.loads(row["payload"])) for row in reversed(rows)]

    def completed_since(self, since: float, limit: int = 5000) -> list[dict]:
        """``turn.completed`` data recorded at or after ``since`` (epoch s), oldest first (#446)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM ui_events WHERE type = 'turn.completed' AND ts >= ?"
                " ORDER BY id LIMIT ?",
                (since, limit),
            ).fetchall()
        return [json.loads(row["payload"]).get("data") or {} for row in rows]

    def snapshot(self, event_limit: int = 50, turn_limit: int = 20) -> dict[str, Any]:
        """Counts, the last events and the last turns from one point in time.

        The store has one connection and every write takes ``self._lock``; holding it
        across the three reads means no append of this process lands between them, so
        the cursor, the counts and the turn list agree. (A second process writing the same
        file is not supported: the server owns ``turns.db``.)
        """
        with self._lock:
            return {
                "counts": self.counts(),
                "tail": self.tail(event_limit),
                "turns": self.recent_turns(turn_limit),
            }

    def counts(self) -> dict[str, int]:
        """``event_count`` / ``turn_count`` / ``latest_id`` over the whole table."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT turn_id), COALESCE(MAX(id), 0) FROM ui_events"
            ).fetchone()
        return {"event_count": int(row[0]), "turn_count": int(row[1]), "latest_id": int(row[2])}

    def recent_turns(self, limit: int = 20) -> list[dict]:
        with self._lock:
            turns = self._conn.execute(
                "SELECT turn_id, MIN(id) AS first_id FROM ui_events"
                " GROUP BY turn_id ORDER BY first_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for turn in turns:
                rows = self._conn.execute(
                    "SELECT type, ts, payload FROM ui_events WHERE turn_id = ? AND type IN"
                    " ('turn.started', 'turn.summary', 'turn.completed') ORDER BY seq",
                    (turn["turn_id"],),
                ).fetchall()
                item = {
                    "turn_id": turn["turn_id"],
                    "started_at": None,
                    "summary": None,
                    "completed": False,
                    "error": None,
                    "ended_at": None,
                    "usage": None,
                    "stop": None,
                    "usage_reason": None,
                    "tool_count": self._conn.execute(
                        "SELECT COUNT(*) FROM ui_events WHERE turn_id = ?"
                        " AND type = 'tool_call.created'",
                        (turn["turn_id"],),
                    ).fetchone()[0],
                }
                for row in rows:
                    data = json.loads(row["payload"])["data"]
                    if row["type"] == "turn.started" and item["started_at"] is None:
                        item["started_at"] = row["ts"]
                    elif row["type"] == "turn.summary":
                        item["summary"] = {key: data.get(key) for key in _SUMMARY_KEYS}
                    elif row["type"] == "turn.completed":
                        item["completed"] = True
                        item["error"] = data.get("error")
                        item["ended_at"] = row["ts"]
                        item["usage"] = data.get("usage")
                        item["stop"] = data.get("stop")
                        item["usage_reason"] = data.get("usage_reason")
                result.append(item)
        return result

    def close_unfinished(self, reason: str = "server_restarted") -> list[str]:
        """End every turn that has no ``turn.completed`` as ``stop: "interrupted"`` (#452).

        Run once at start-up, before any turn can start (the data directory has one owner,
        #285): a turn found open then belonged to a process that is gone. Returns the ids.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT turn_id FROM ui_events GROUP BY turn_id"
                " HAVING SUM(type = 'turn.completed') = 0 ORDER BY MIN(id)"
            ).fetchall()
            closed = []
            for row in rows:
                self.append(
                    row["turn_id"],
                    "turn.completed",
                    {
                        "usage": {},
                        "stop": "interrupted",
                        "error": True,
                        "reason": reason,
                        "usage_reason": USAGE_INTERRUPTED,
                    },
                )
                closed.append(row["turn_id"])
        return closed

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ActiveTurn:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn_id: str | None = None

    def set(self, turn_id: str) -> None:
        with self._lock:
            self._turn_id = turn_id

    def clear(self) -> None:
        with self._lock:
            self._turn_id = None

    def current(self) -> str | None:
        with self._lock:
            return self._turn_id


class TurnEventRecorder:
    def __init__(
        self,
        store: TurnEventStore,
        subscriber: Callable[[dict], Any] | None = None,
        active: ActiveTurn | None = None,
    ) -> None:
        self.store = store
        self.active = active or ActiveTurn()
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], Any]] = [subscriber] if subscriber else []

    def subscribe(self, callback: Callable[[dict], Any]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def store_payload(self, tool_use_id: str, kind: str, captured: Any) -> bool:
        """Persist a captured tool payload for the active turn (paged replay, #188)."""
        turn_id = self.active.current()
        if turn_id is None or not tool_use_id or not isinstance(captured, Mapping):
            return False
        try:
            self.store.put_payload(turn_id, tool_use_id, kind, captured)
        except Exception:  # noqa: BLE001 - never break a turn on telemetry
            logger.warning("UI tool payload dropped")
            return False
        return True

    def emit(self, event_type: str, data: Mapping | None = None) -> dict | None:
        turn_id = self.active.current()
        if turn_id is None:
            return None
        try:
            event = self.store.append(turn_id, event_type, data or {})
        except Exception:  # noqa: BLE001 - never break a turn on telemetry
            logger.warning("UI turn event dropped: %s", event_type)
            return None
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                logger.warning("UI turn event subscriber failed")
        return event


_COMPLETED_STATUS = {
    "success": "done",
    "error": "error",
    "cancelled": "denied",
    "retry": "running",
}


class UIEventSink:
    """EventSink adapter: tool.started/tool.completed -> tool_call.created/updated;
    model.started/model.completed -> the same turn events, plus ``model.progress`` while a
    call runs (#445, ``progress``: a :class:`ModelCallProgress`; ``False`` for none)."""

    def __init__(
        self, recorder: TurnEventRecorder, progress: ModelCallProgress | None | bool = None
    ) -> None:
        self.recorder = recorder
        if progress is None:
            progress = ModelCallProgress(recorder)
        self.progress = progress or None

    def _model_event(self, event: ObservabilityEvent) -> None:
        attributes = event.attributes or {}
        if event.event_type == "model.started":
            ident = call_id(attributes.get("call_id"))
            if ident is None:
                return
            data: dict[str, Any] = {"call_id": ident, "started_at": event.timestamp.timestamp()}
            model = model_id(event.model_id)
            if model is not None:
                data["model"] = model
            self.recorder.emit("model.started", data)
            if self.progress is not None:
                self.progress.started(ident, model)
            return
        data = completed_data(event)
        if data is None:
            return
        if self.progress is not None:
            self.progress.finished(data["call_id"])
        self.recorder.emit("model.completed", data)

    def _payload(self, event: ObservabilityEvent, kind: str) -> dict[str, Any]:
        captured = (event.attributes or {}).get(kind)
        fields = _preview_fields(kind, captured)
        if fields and self.recorder.store_payload(event.tool_use_id or "", kind, captured):
            fields[f"{kind}_stored"] = True
        return fields

    def emit(self, event: ObservabilityEvent) -> None:
        if event.event_type in ("model.started", "model.completed"):
            self._model_event(event)
        elif event.event_type == "tool.started":
            status = "denied" if event.status == "cancelled" else "running"
            self.recorder.emit(
                "tool_call.created",
                {
                    "tool_use_id": event.tool_use_id or "",
                    "name": event.tool_name or "",
                    "status": status,
                    "started_at": event.timestamp.timestamp(),
                    "duration_ms": None,
                    "input_preview": None,
                    "output_preview": None,
                    **self._payload(event, "input"),
                },
            )
        elif event.event_type == "tool.completed":
            ended = event.timestamp.timestamp()
            duration = event.duration_ms
            attributes = event.attributes or {}
            self.recorder.emit(
                "tool_call.updated",
                {
                    "tool_use_id": event.tool_use_id or "",
                    "name": event.tool_name or "",
                    "status": _COMPLETED_STATUS.get(event.status, "error"),
                    "started_at": ended - duration / 1000 if duration is not None else None,
                    "duration_ms": duration,
                    "input_preview": None,
                    "output_preview": None,
                    "result_status": attributes.get("result_status"),
                    "exception_type": attributes.get("exception_type"),
                    **(
                        {"result_code": attributes["result_code"]}
                        if attributes.get("result_code")
                        else {}
                    ),
                    **({"cancel": "declared"} if attributes.get("cancel") == "declared" else {}),
                    **self._payload(event, "output"),
                },
            )

"""/observability for the chat server: the page reads ``turns.db`` (#286).

The chat server records turn events (turn / tool call / approval / breaker) with
``TurnEventRecorder`` into ``turns.db``; that is the one source of truth for them. This
module serves the two endpoints the observability page reads, backed by that store:

- ``GET /api/observability/events``: SSE replay of ``ui_events`` rows in the page's event
  contract (see ``dashboard/static/js/observability.js``); the event id is the row id.
- ``GET /api/observability/snapshot``: counts, the latest cursor, recent events, recent turns.

Nothing here writes. The hash-chained ``SQLiteEventStore`` (ADR 0009, ``events.db``) is not
a second copy of the turns: the chat server only writes its ``/new`` audit records there, and
The generic ``sse.create_sse_blueprint`` reads such a chained store for an app that writes
one; neither the chat server nor ``dashboard/app.py`` (which serves no event stream, #278) uses it.

Only an allow-list of fields leaves this module, validated on the way out: numbers must be
finite and in range (else left out), ``type`` one of the turn contract's, codes the
recorder takes from constants checked against them (else a fixed word), and the remaining
text fields must look like a code (``[A-Za-z0-9_.:/@-]``, at most 64 characters). The last
check cannot tell a secret of the same shape from a code: the contract is that the recorder
writes only code values there (ADR 0009, "exit contract"). The endpoints have no
authentication of their own; the app-wide guard (``install_security``: loopback bind,
Host allow-list, same-origin API reads) is the boundary, see the tests.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ..core import breaker_reasons, hitl_reasons
from ..core.engine import BreakLevel, HITLAction
from ..providers.failure import REASONS as _FAILURE_REASONS
from .turn_events import (
    EVENT_TYPES,
    INTERRUPT_REASONS,
    RESERVED_TYPES,
    TOOL_STATUSES,
    USAGE_REASONS,
    TurnEventStore,
)

_POLL_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0
_BATCH_SIZE = 100
_SNAPSHOT_EVENTS = 50
_SNAPSHOT_TURNS = 20

# turn.* / tool_call.* / approval.* / breaker.* -> the page's event types.
_TOOL_END_STATUS = {
    "done": ("success", "info"),
    "error": ("error", "error"),
    "timeout": ("error", "error"),
    # A denied call is an intervention, not a failure: the page files it under "denied".
    "denied": ("denied", "warning"),
}


_CODE = re.compile(r"[A-Za-z0-9_.:/@-]{1,64}")
_MODEL = re.compile(r"[A-Za-z0-9_.:/@-]{1,200}")
_MAX_CURSOR = 2**63 - 1  # a SQLite integer; also at most 19 digits
_MAX_COUNT = 2**53  # a token count / duration the page can add without losing digits
_MAX_SECONDS = 1e11  # the page reads a number below this as epoch seconds
_MAX_SEQ = 2**31

# Values the recorder writes from constants in the code. A value outside its set is shown as
# a fixed word, never as stored.
_KNOWN_TYPES = EVENT_TYPES | RESERVED_TYPES
_MODEL_STATUSES = frozenset({"success", "error", "cancelled", "retry"})
_TASK_MODEL_STATUSES = frozenset({"success", "error", "refused"})
_MODEL_STOPS = frozenset(
    {
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
        "guardrail_intervened",
        "content_filtered",
        "interrupt",
        "cancelled",
        "canceled",
        "error",
        "unknown",
    }
)
_STOPS = frozenset({"completed", "max_tokens", "error", "cancelled", "resumed", "interrupted"})
_DECISIONS = frozenset({*(action.value for action in HITLAction), "deny"})
_APPROVAL_STATUSES = frozenset(
    {
        "approved",
        "edited",
        "rejected",
        "responded",
        "expired",
        "cancelled",
        "closed",
        "superseded",
    }
)
_RESULT_STATUSES = frozenset({"success", "error"})
_LEVELS = frozenset({*(level.name.lower() for level in BreakLevel), *(l.value for l in BreakLevel)})


def _usage_reason(value: Any) -> str | None:
    """``usage_reason`` (#363): a known code, a fixed word for any other value, None if absent."""
    if value is None:
        return None
    return _one_of(value, USAGE_REASONS, "E_USAGE_UNKNOWN")


def _text(value: Any, fallback: str = "") -> str:
    """``value`` if it is a short code-like string, else ``fallback`` (never free text)."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return fallback


def _one_of(value: Any, allowed: frozenset[str], fallback: str = "") -> str:
    """``value`` if it is one of the recorder's known codes, else ``fallback``."""
    return value if isinstance(value, str) and value in allowed else fallback


def _level(value: Any) -> str:
    """A breaker level as stored (``BreakLevel`` name or value, any case), else ``?``."""
    if isinstance(value, str) and value.lower() in _LEVELS:
        return value
    return "?"


def _number(value: Any, high: float = _MAX_COUNT) -> float | int | None:
    """A finite, non-negative number up to ``high``; anything else (bool, NaN, inf) is None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        if not math.isfinite(value) or value < 0 or value > high:
            return None
    except OverflowError:
        return None
    return value


def _non_negative_int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _seconds(value: Any) -> float | int | None:
    return _number(value, _MAX_SECONDS)


def _usage(data: Mapping) -> dict[str, Any] | None:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return None
    result: dict[str, Any] = {"is_final": True}
    for key in ("input_tokens", "output_tokens"):
        count = _non_negative_int(usage.get(key))
        if count is not None:
            result[key] = count
    return result if len(result) > 1 else None


def page_event(row_id: int, event: Mapping) -> dict[str, Any]:
    """One ``ui_events`` row as an event of the observability page's contract.

    Every value is validated on the way out (see the module docstring): numbers must be
    finite and in range, ``type`` one of the contract's, enumerated codes one of the
    recorder's constants, the rest code-like.
    """
    raw = event.get("data")
    data: Mapping = raw if isinstance(raw, Mapping) else {}
    kind = _one_of(event.get("type"), _KNOWN_TYPES)
    turn_id = _text(event.get("turn_id"))
    out: dict[str, Any] = {
        "id": str(row_id),
        "event_id": _text(event.get("event_id")),
        "turn_id": turn_id,
        "type": kind.replace(".", "_") if kind else "event",
        "severity": "info",
        "message": "",
    }
    when = _seconds(event.get("ts"))
    if when is not None:
        out["timestamp"] = when
    seq = _non_negative_int(event.get("seq"))
    if seq is not None and seq <= _MAX_SEQ:
        out["seq"] = seq
    if kind == "turn.started":
        out["message"] = "resumed after approval" if data.get("resumed_request_id") else ""
    elif kind == "turn.summary":
        parts = [_text(data.get("category"))]
        intensity = _number(data.get("intensity"), 1e6)
        if intensity is not None:
            parts.append(f"{intensity:.2f}")
        out["message"] = " ".join(part for part in parts if part)
    elif kind == "turn.completed":
        out["type"] = "turn_end"
        error = data.get("error")
        out["status"] = "error" if error else "success"
        if error:
            out["severity"] = "error"
        # ``error`` is a flag on a streamed turn and the exception type on a resumed one.
        reason = [
            _one_of(data.get("stop"), _STOPS),
            _one_of(data.get("reason"), INTERRUPT_REASONS),
            _one_of(data.get("failure"), frozenset(_FAILURE_REASONS)),
            _text(error) if isinstance(error, str) else "",
        ]
        out["message"] = ": ".join(part for part in reason if part)
        usage = _usage(data)
        if usage is not None:
            out["usage"] = usage
            out["usage_id"] = f"{turn_id}:turn"
        usage_reason = _usage_reason(data.get("usage_reason"))
        if usage_reason is not None:
            out["usage_reason"] = usage_reason
        model = data.get("model")
        if isinstance(model, str) and _MODEL.fullmatch(model):
            out["model"] = model
    elif kind in ("tool_call.created", "tool_call.updated"):
        created = kind == "tool_call.created"
        status = _one_of(data.get("status"), TOOL_STATUSES)
        out["type"] = "tool_start" if created else "tool_end"
        out["tool"] = _text(data.get("name"))
        out["span_id"] = _text(data.get("tool_use_id"))
        started = _seconds(data.get("started_at"))
        if started is not None:
            out["start_time"] = started
        if created:
            out["status"] = "running"
        else:
            out["status"], out["severity"] = _TOOL_END_STATUS.get(status, ("error", "error"))
            duration = _non_negative_int(data.get("duration_ms"))
            if duration is not None:
                out["duration_ms"] = duration
            out["message"] = (
                _text(data.get("exception_type"))
                or _one_of(data.get("result_status"), _RESULT_STATUSES)
                or status
            )
    elif kind == "approval.requested":
        out["tool"] = _text(data.get("tool_name"))
        # Only the code. The reason's prose (``reason``) is never forwarded; a missing or
        # unknown code reads as the fixed word "approval".
        out["message"] = _one_of(data.get("reason_code"), hitl_reasons.REASON_CODES, "approval")
        out["severity"] = "warning"
    elif kind == "approval.resolved":
        out["tool"] = _text(data.get("tool_name"))
        decision = _one_of(data.get("decision"), _DECISIONS)
        resolver = _text(data.get("resolver"))
        reason = _one_of(data.get("resolution_reason_code"), hitl_reasons.RESOLUTION_CODES)
        out["message"] = " · ".join(part for part in (decision, resolver, reason) if part)
        out["decision"] = decision
        out["status"] = _one_of(data.get("status"), _APPROVAL_STATUSES)
        out["severity"] = "info" if decision == "approve" else "warning"
    elif kind in ("model.started", "model.progress", "model.completed"):
        # #445: a model call (ids, numbers, codes). Usage stays on turn.completed (the
        # page's per-turn total), so a call's tokens are not counted twice.
        out["type"] = {
            "model.started": "model_call_start",
            "model.progress": "model_call_progress",
            "model.completed": "model_call_end",
        }[kind]
        out["span_id"] = _text(data.get("call_id"))
        model = data.get("model")
        if isinstance(model, str) and _MODEL.fullmatch(model):
            out["model"] = model
        if kind == "model.started":
            started = _seconds(data.get("started_at"))
            if started is not None:
                out["start_time"] = started
            out["status"] = "running"
        elif kind == "model.progress":
            elapsed = _non_negative_int(data.get("elapsed_ms"))
            if elapsed is not None:
                out["elapsed_ms"] = elapsed
                out["message"] = f"{elapsed // 1000}s"
        else:
            duration = _non_negative_int(data.get("duration_ms"))
            if duration is not None:
                out["duration_ms"] = duration
            status = _one_of(data.get("status"), _MODEL_STATUSES) or "error"
            out["status"] = "success" if status == "success" else status
            if status in ("error", "cancelled"):
                out["severity"] = "error" if status == "error" else "warning"
            out["message"] = _one_of(data.get("stop"), _MODEL_STOPS) or ""
    elif kind == "task_model.call":
        # docs/task-models.md: the task, the model and its tokens (codes and numbers only).
        out["type"] = "task_model_call"
        task = _text(data.get("task"))
        status = _one_of(data.get("status"), _TASK_MODEL_STATUSES) or "error"
        out["status"] = status
        if status != "success":
            out["severity"] = "warning"
        model = data.get("model")
        if isinstance(model, str) and _MODEL.fullmatch(model):
            out["model"] = model
        usage = {
            key: value
            for key in ("input_tokens", "output_tokens")
            if (value := _non_negative_int(data.get(key))) is not None
        }
        if usage:
            # Not ``usage``: the page adds that up per turn; this call is shown on its own.
            out["tokens"] = usage
        reason = _one_of(data.get("reason"), frozenset(_FAILURE_REASONS))
        out["message"] = " · ".join(part for part in (task, status, reason) if part)
    elif kind == "breaker.transition":
        out["message"] = f"{_level(data.get('from'))} → {_level(data.get('to'))}"
        out["severity"] = "warning"
        # #455: why (allow-listed codes and numbers; the page words them, never the code).
        reasons, inputs = breaker_reasons.clean(data.get("reasons"), data.get("inputs"))
        if reasons:
            out["to_level"] = _level(data.get("to")).lower()
            out["reasons"], out["inputs"] = reasons, inputs
    return out


def _turn_item(item: Mapping) -> dict[str, Any]:
    """The allow-listed fields of a ``recent_turns`` entry: ids, times, counts, codes.

    ``summary`` (stimulus / emotion fields) and free text never leave through here.
    """
    usage = item.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    error = item.get("error")
    summary = item.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    intensity = summary.get("intensity")
    return {
        "turn_id": _text(item.get("turn_id")),
        "started_at": _seconds(item.get("started_at")),
        "ended_at": _seconds(item.get("ended_at")),
        "completed": item.get("completed") is True,
        "error": _text(error, "error") if isinstance(error, str) else bool(error),
        "tool_count": _non_negative_int(item.get("tool_count")) or 0,
        "stop": _one_of(item.get("stop"), _STOPS) or None,
        "usage_reason": _usage_reason(item.get("usage_reason")),
        "usage": {
            key: count
            for key in ("input_tokens", "output_tokens")
            if (count := _non_negative_int(usage.get(key))) is not None
        },
        "category": _text(summary.get("category")),
        "intensity": _number(intensity, 1e6),
    }


def create_turns_observability_blueprint(turn_store: TurnEventStore) -> Blueprint:
    """The observability endpoints for an app whose turn events live in ``turns.db``.

    The SSE endpoint accepts ``Last-Event-ID`` (or ``last_id`` / ``since_id``): the row id of
    the last event seen. Without a cursor it replays every retained event first.
    The store is owned by the application (closed by it), never by a request.
    """
    blueprint = Blueprint("observability", __name__)

    def parse_cursor(raw: str | None) -> int | None:
        """A row id: ASCII digits, at most 19 of them and within a SQLite integer."""
        if raw is None or raw == "":
            return 0
        if len(raw) > 19 or not raw.isascii() or not raw.isdigit():
            return None
        value = int(raw)
        return value if value <= _MAX_CURSOR else None

    @blueprint.get("/api/observability/events")
    def stream_events() -> Response | tuple[Response, int]:
        raw = request.headers.get("Last-Event-ID")
        if not raw:
            raw = request.args.get("last_id") or request.args.get("since_id")
        cursor = parse_cursor(raw)
        if cursor is None:
            return jsonify(error="Invalid event cursor."), 400

        @stream_with_context
        def generate() -> Iterator[str]:
            position = cursor
            last_heartbeat = time.monotonic()
            # Flush headers even when no turn has been recorded.
            yield ": connected\n\n"
            while True:
                rows = turn_store.events_after(position, _BATCH_SIZE)
                for row_id, event in rows:
                    data = json.dumps(
                        page_event(row_id, event),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    position = row_id
                    yield f"id: {row_id}\ndata: {data}\n\n"
                now = time.monotonic()
                if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                # Drain a backlog at once; poll once per second when caught up.
                if len(rows) < _BATCH_SIZE:
                    time.sleep(_POLL_SECONDS)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @blueprint.get("/api/observability/snapshot")
    def snapshot() -> Response:
        # One point in time: counts, the last events and the turn list come from a single
        # ``TurnEventStore.snapshot`` (the store's write lock held across the reads).
        taken = turn_store.snapshot(_SNAPSHOT_EVENTS, _SNAPSHOT_TURNS)
        counts = taken["counts"]
        latest = counts["latest_id"]
        response = jsonify(
            {
                "source": "turns.db",
                "event_count": counts["event_count"],
                "turn_count": counts["turn_count"],
                "latest_event_id": str(latest) if latest else None,
                "events": [page_event(row_id, event) for row_id, event in taken["tail"]],
                "turns": [_turn_item(item) for item in taken["turns"]],
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint


__all__ = ["create_turns_observability_blueprint", "page_event"]

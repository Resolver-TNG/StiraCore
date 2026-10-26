"""Privacy-preserving dashboard hooks for Strands SDK 1.57.1.

Payloads are summarized, never serialized: prompts, message content, tool
arguments, tool results, and exception messages are not emitted. Model responses
and invocation results contribute only stop reasons and token usage.

Timing uses a monotonic clock. Correlation uses agent and invocation-state
identity, with context-local correlation for message events (which have no
invocation_state). Active concurrent invocations must have distinct invocation
state objects.

Sink failures are logged without exception details and never interrupt an agent.

Opt-in exception (#188): a provider built with ``payload_capture`` attaches the
callable's return value for tool input/result as ``attributes["input"]`` /
``attributes["output"]``. The callable must mask and cap (see
``turn_events.capture_payload``); raw payloads never reach the sink. Use a
dedicated provider for that sink only.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    MessageAddedEvent,
)

__all__ = [
    "DashboardHookProvider",
    "EventSink",
    "InMemoryEventSink",
    "ObservabilityEvent",
    "create_dashboard_hooks",
]

logger = logging.getLogger(__name__)

# #545: toolUseIds (a set in ``invocation_state``) of calls a declared checkpoint cancelled
# on purpose (ADR 0014 phase 3, set by CheckpointHooks). Their ``tool.completed`` carries
# ``cancel: "declared"`` so the board shows a checkpoint, not a refused call.
PROGRESS_DECLARED_KEY = "stira.progress.declared"
_RESULT_CODE = re.compile(r"[a-z][a-z_]{0,31}")


@dataclass(kw_only=True)
class ObservabilityEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_type: str
    turn_id: str | None = None
    duration_ms: float | None = None
    model_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    status: str = "success"
    usage: dict[str, Any] | None = None
    message: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


class EventSink(Protocol):
    def emit(self, event: ObservabilityEvent) -> None:
        """Accept one normalized observability event."""
        ...


class InMemoryEventSink:
    """A thread-safe, oldest-first buffer retaining at most 1,000 events.

    ``since_id`` is exclusive. If its event has expired, or the ID is unknown,
    pagination starts at the oldest retained event. Non-positive limits return
    an empty list.
    """

    def __init__(self) -> None:
        self._events: deque[ObservabilityEvent] = deque(maxlen=1000)
        self._lock = RLock()

    def emit(self, event: ObservabilityEvent) -> None:
        with self._lock:
            self._events.append(event)

    def get_events(self, since_id: str | None = None, limit: int = 50) -> list[ObservabilityEvent]:
        if limit <= 0:
            return []
        with self._lock:
            events = list(self._events)
        start = 0
        if since_id is not None:
            for index, event in enumerate(events):
                if event.event_id == since_id:
                    start = index + 1
                    break
        return events[start : start + limit]

    def get_latest(self) -> ObservabilityEvent | None:
        with self._lock:
            return self._events[-1] if self._events else None

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _token_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage(value: Any) -> dict[str, Any] | None:
    raw = _field(value, "usage")
    if raw is None:
        raw = _field(_field(value, "metrics"), "accumulated_usage")
    if raw is None:
        # One model call (#445): the SDK attaches the call's usage to its message.
        raw = _field(_field(_field(value, "message"), "metadata"), "usage")
    usage = {}
    for name in ("inputTokens", "outputTokens"):
        count = _token_count(_field(raw, name))
        if count is not None:
            usage[name] = count
    return usage or None


_STOP_REASONS = frozenset(
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
    }
)


def _stop_reason(value: Any) -> str | None:
    reason = _field(value, "stop_reason")
    if reason is None:
        return None
    return reason if isinstance(reason, str) and reason in _STOP_REASONS else "unknown"


def _response_metadata(value: Any, prefix: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {f"{prefix}_present": value is not None}
    reason = _stop_reason(value)
    if reason is not None:
        metadata["stop_reason"] = reason
    return metadata


def _status(
    *,
    retry: bool = False,
    exception: Any = None,
    cancelled: bool = False,
    result_status: str | None = None,
    stop_reason: str | None = None,
) -> str:
    if cancelled or stop_reason in {"interrupt", "cancelled", "canceled"}:
        return "cancelled"
    if retry:
        return "retry"
    if exception is not None or result_status == "error" or stop_reason == "error":
        return "error"
    return "success"


def _exception_metadata(exception: Any) -> dict[str, Any]:
    # Do not call str/repr on exceptions; their messages can contain payloads.
    return {"exception_type": type(exception).__name__} if exception is not None else {}


def _monotonic() -> float:
    """The module's ``perf_counter`` looked up at call time (tests replace it)."""
    return perf_counter()


def _elapsed(start: float | None, end: float) -> float | None:
    return None if start is None else max(0.0, (end - start) * 1000.0)


def _model_id(agent: Any) -> str | None:
    model = _field(agent, "model")
    return _string(_field(_field(model, "config"), "model_id")) or _string(
        _field(model, "model_id")
    )


@dataclass
class _ToolStart:
    started: float
    name: str | None
    cancelled: bool


@dataclass
class _Turn:
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    started: float | None = None
    cancelled: bool = False
    model_starts: list[float] = field(default_factory=list)
    # #445: one id per open model call, parallel to model_starts (correlates the events).
    model_calls: list[str] = field(default_factory=list)
    tool_starts: dict[str | None, list[_ToolStart]] = field(default_factory=dict)


_CONTENT_TYPES = frozenset(
    {
        "text",
        "image",
        "document",
        "video",
        "audio",
        "toolUse",
        "toolResult",
        "reasoningContent",
        "cachePoint",
        "guardContent",
        "citationsContent",
    }
)
_ROLES = frozenset({"user", "assistant", "system", "tool"})


def _message_metadata(message: Any) -> dict[str, Any]:
    role = _field(message, "role")
    if not isinstance(role, str) or role not in _ROLES:
        role = "unknown"

    content = _field(message, "content")
    content_types: set[str] = set()
    if isinstance(content, str):
        content_types.add("text")
    elif isinstance(content, (list, tuple)):
        for block in content:
            if isinstance(block, Mapping):
                known = _CONTENT_TYPES.intersection(block.keys())
                content_types.update(known or {"unknown"})
            else:
                content_types.add("unknown")
    elif content is not None:
        content_types.add("unknown")

    return {"role": role, "content_types": sorted(content_types)}


class DashboardHookProvider(HookProvider):
    """Emit normalized lifecycle events without changing Strands events.

    ``success`` on a started event means that the start was observed, not that
    the operation has completed. Hooks do not replace streaming callbacks.

    Message events outside a known invocation have no turn ID. If callbacks
    cross execution contexts, a message is correlated only when exactly one
    invocation is active for that agent.
    """

    def __init__(
        self,
        sink: EventSink,
        *,
        payload_capture: Callable[[str, Any], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._sink = sink
        self._capture = payload_capture
        # Monotonic seconds for durations; injectable so tests can fake a long call (#445).
        self._clock = clock if clock is not None else _monotonic
        self._lock = RLock()
        self._turns: dict[tuple[int, int], _Turn] = {}
        self._context: ContextVar[tuple[tuple[int, str], ...]] = ContextVar(
            f"dashboard_turns_{uuid4().hex}", default=()
        )

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)
        registry.add_callback(BeforeModelCallEvent, self.on_before_model_call)
        registry.add_callback(AfterModelCallEvent, self.on_after_model_call)
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)
        registry.add_callback(MessageAddedEvent, self.on_message_added)

    @staticmethod
    def _key(event: Any) -> tuple[int, int]:
        return id(event.agent), id(event.invocation_state)

    def _turn(self, event: Any) -> _Turn:
        """Get/create timing state; caller holds the lock."""
        key = self._key(event)
        turn = self._turns.get(key)
        if turn is None:
            turn = _Turn()
            self._turns[key] = turn
        return turn

    def _captured(self, kind: str, value: Any, tool_name: Any = None) -> dict[str, Any]:
        """Opt-in masked/capped payload for one tool input/output (#188).

        File bodies and file digests never reach the capture (#307): see
        ``file_tools.payload_for_log``.
        """
        if self._capture is None:
            return {}
        try:
            from stiracore.tools.file_tools import payload_for_log

            captured = self._capture(kind, payload_for_log(tool_name, kind, value))
        except Exception:  # noqa: BLE001 - telemetry must never break a tool call
            logger.warning("Dashboard payload capture failed; payload dropped")
            return {}
        return {} if captured is None else {kind: captured}

    def _emit(self, event: ObservabilityEvent) -> None:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 - isolate optional plugin or batch-job failures
            logger.warning("Dashboard event sink failed; event dropped")

    def on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        now = self._clock()
        with self._lock:
            turn = self._turn(event)
            turn.started = now
            turn.cancelled = bool(_field(event, "cancel", False))
            turn_id = turn.turn_id
            cancelled = turn.cancelled
        self._context.set(self._context.get() + ((id(event.agent), turn_id),))
        self._emit(
            ObservabilityEvent(
                event_type="invocation.started",
                turn_id=turn_id,
                model_id=_model_id(event.agent),
                status=_status(cancelled=cancelled),
                message="Invocation started",
            )
        )

    def on_after_invocation(self, event: AfterInvocationEvent) -> None:
        now = self._clock()
        with self._lock:
            turn = self._turns.pop(self._key(event), None)
        turn_id = turn.turn_id if turn else None
        self._context.set(tuple(item for item in self._context.get() if item[1] != turn_id))

        result = _field(event, "result")
        attributes = _response_metadata(result, "result")
        attributes["resume"] = bool(_field(event, "resume", False))
        self._emit(
            ObservabilityEvent(
                event_type="invocation.completed",
                turn_id=turn_id,
                duration_ms=_elapsed(turn.started if turn else None, now),
                model_id=_model_id(event.agent),
                # AfterInvocationEvent exposes no exception. An absent result
                # can indicate failure, but does not reveal its cause.
                status=_status(
                    cancelled=bool(turn and turn.cancelled),
                    result_status="error" if result is None else None,
                    stop_reason=_stop_reason(result),
                ),
                usage=_usage(result),
                message="Invocation completed",
                attributes=attributes,
            )
        )

    def on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        now = self._clock()
        with self._lock:
            turn = self._turn(event)
            turn.model_starts.append(now)
            call_id = uuid4().hex
            turn.model_calls.append(call_id)
            turn_id = turn.turn_id
        self._emit(
            ObservabilityEvent(
                event_type="model.started",
                turn_id=turn_id,
                model_id=_model_id(event.agent),
                status=_status(cancelled=bool(_field(event, "cancel", False))),
                message="Model call started",
                attributes={
                    "projected_input_tokens": _token_count(_field(event, "projected_input_tokens")),
                    "call_id": call_id,
                },
            )
        )

    def on_after_model_call(self, event: AfterModelCallEvent) -> None:
        now = self._clock()
        with self._lock:
            turn = self._turns.get(self._key(event))
            start = turn.model_starts.pop() if turn and turn.model_starts else None
            call_id = turn.model_calls.pop() if turn and turn.model_calls else None
            turn_id = turn.turn_id if turn else None

        response = _field(event, "stop_response")
        exception = _field(event, "exception")
        retry = bool(_field(event, "retry", False))
        attributes = _response_metadata(response, "stop_response")
        attributes.update(_exception_metadata(exception))
        attributes["retry"] = retry
        attributes["call_id"] = call_id
        self._emit(
            ObservabilityEvent(
                event_type="model.completed",
                turn_id=turn_id,
                duration_ms=_elapsed(start, now),
                model_id=_model_id(event.agent),
                status=_status(
                    retry=retry,
                    exception=exception,
                    stop_reason=_stop_reason(response),
                ),
                usage=_usage(response),
                message="Model call completed",
                attributes=attributes,
            )
        )

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        now = self._clock()
        tool_use = _field(event, "tool_use")
        name = _string(_field(tool_use, "name"))
        use_id = _string(_field(tool_use, "toolUseId"))
        cancelled = bool(_field(event, "cancel_tool", False))
        with self._lock:
            turn = self._turn(event)
            turn.tool_starts.setdefault(use_id, []).append(_ToolStart(now, name, cancelled))
            turn_id = turn.turn_id
        self._emit(
            ObservabilityEvent(
                event_type="tool.started",
                turn_id=turn_id,
                model_id=_model_id(event.agent),
                tool_name=name,
                tool_use_id=use_id,
                status=_status(cancelled=cancelled),
                message="Tool call started",
                attributes=self._captured("input", _field(tool_use, "input"), name),
            )
        )

    def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        now = self._clock()
        tool_use = _field(event, "tool_use")
        use_id = _string(_field(tool_use, "toolUseId"))
        with self._lock:
            turn = self._turns.get(self._key(event))
            starts = turn.tool_starts.get(use_id) if turn else None
            start = starts.pop() if starts else None
            if turn and starts == []:
                turn.tool_starts.pop(use_id, None)
            turn_id = turn.turn_id if turn else None

        exception = _field(event, "exception")
        retry = bool(_field(event, "retry", False))
        cancelled = bool(_field(event, "cancel_message")) or bool(start and start.cancelled)
        result = _field(event, "result")
        result_status = _string(_field(result, "status"))
        if result_status not in {"success", "error"}:
            result_status = None
        attributes: dict[str, Any] = {
            "result_present": result is not None,
            "result_status": result_status,
            "retry": retry,
        }
        # A tool's failure code (e.g. web_fetch's ``redirect``, #584): a short identifier only.
        result_code = _field(result, "code") if result_status == "error" else None
        if isinstance(result_code, str) and _RESULT_CODE.fullmatch(result_code):
            attributes["result_code"] = result_code
        attributes.update(_exception_metadata(exception))
        state = _field(event, "invocation_state")
        declared = state.get(PROGRESS_DECLARED_KEY) if isinstance(state, Mapping) else None
        if cancelled and isinstance(declared, set) and use_id in declared:
            attributes["cancel"] = "declared"
        attributes.update(self._captured("output", result, _string(_field(tool_use, "name"))))
        self._emit(
            ObservabilityEvent(
                event_type="tool.completed",
                turn_id=turn_id,
                duration_ms=_elapsed(start.started if start else None, now),
                model_id=_model_id(event.agent),
                tool_name=_string(_field(tool_use, "name")) or (start.name if start else None),
                tool_use_id=use_id,
                status=_status(
                    retry=retry,
                    exception=exception,
                    cancelled=cancelled,
                    result_status=result_status,
                ),
                message="Tool call completed",
                attributes=attributes,
            )
        )

    def on_message_added(self, event: MessageAddedEvent) -> None:
        agent_id = id(event.agent)
        with self._lock:
            active = {turn.turn_id for key, turn in self._turns.items() if key[0] == agent_id}
            turn_id = next(
                (
                    context_turn
                    for context_agent, context_turn in reversed(self._context.get())
                    if context_agent == agent_id and context_turn in active
                ),
                None,
            )
            if turn_id is None and len(active) == 1:
                turn_id = next(iter(active))
        self._emit(
            ObservabilityEvent(
                event_type="message.added",
                turn_id=turn_id,
                model_id=_model_id(event.agent),
                message="Message added",
                attributes=_message_metadata(event.message),
            )
        )


def create_dashboard_hooks(
    sink: EventSink,
    *,
    payload_capture: Callable[[str, Any], Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> DashboardHookProvider:
    """Create a provider suitable for ``Agent(hooks=[provider])``."""
    return DashboardHookProvider(sink, payload_capture=payload_capture, clock=clock)

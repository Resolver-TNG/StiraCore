"""Model calls on the turn events (#445): start, elapsed time while running, end.

``UIEventSink`` turns the dashboard hooks' ``model.started`` / ``model.completed`` into
turn events, and :class:`ModelCallProgress` writes ``model.progress`` every
``interval_sec`` while a call is open, so a long call (for example while the model writes
a large tool argument) is visibly running instead of looking stuck.

Every event holds ids, a model id, numbers and fixed codes only: never a prompt, a reply,
a tool argument or a secret.

- ``model.started``  ``{call_id, model?, started_at}``
- ``model.progress`` ``{call_id, model?, elapsed_ms}``
- ``model.completed`` ``{call_id, model?, duration_ms, usage{input_tokens, output_tokens},
  stop, status}``
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_SEC = 10.0
MAX_OPEN_CALLS = 8
_CALL_ID = re.compile(r"[0-9a-f]{32}")
_MODEL = re.compile(r"[A-Za-z0-9._:/@-]{1,160}")
STOP_CODES = frozenset(
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
STATUS_CODES = frozenset({"success", "error", "cancelled", "retry"})


def call_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _CALL_ID.fullmatch(value) else None


def model_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def completed_data(event: Any) -> dict[str, Any] | None:
    """``model.completed`` data from a hooks event, or None without a call id."""
    attributes = getattr(event, "attributes", None) or {}
    ident = call_id(attributes.get("call_id"))
    if ident is None:
        return None
    usage = getattr(event, "usage", None)
    usage = usage if isinstance(usage, Mapping) else {}
    duration = getattr(event, "duration_ms", None)
    stop = attributes.get("stop_reason")
    status = getattr(event, "status", None)
    data: dict[str, Any] = {
        "call_id": ident,
        "duration_ms": (
            round(duration)
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else None
        ),
        "usage": {
            key: count
            for key, name in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"))
            if (count := _count(usage.get(name))) is not None
        },
        "stop": stop if stop in STOP_CODES else None,
        "status": status if status in STATUS_CODES else "error",
    }
    if attributes.get("retry") is True:
        data["retry"] = True
    exception = attributes.get("exception_type")
    if isinstance(exception, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", exception):
        data["exception_type"] = exception
    model = model_id(getattr(event, "model_id", None))
    if model is not None:
        data["model"] = model
    return data


class ModelCallProgress:
    """``model.progress`` for open model calls of the active turn, every ``interval_sec``.

    ``tick()`` does the work and can be called directly (tests drive it with an injected
    ``clock``). With ``thread=True`` a daemon thread calls it about once a second while at
    least one call is open, and exits when none is. A call is dropped when the active turn
    is no longer the one it started in, so a lost end never ticks forever.
    """

    def __init__(
        self,
        recorder: Any,
        *,
        interval_sec: float = PROGRESS_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
        thread: bool = True,
        poll_sec: float = 1.0,
    ) -> None:
        self.recorder = recorder
        self.interval_sec = float(interval_sec)
        self._clock = clock
        self._thread_on = thread
        self._poll_sec = poll_sec
        self._lock = threading.Lock()
        self._calls: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None

    def started(self, ident: str, model: str | None) -> None:
        turn = self.recorder.active.current()
        if turn is None:
            return
        now = self._clock()
        with self._lock:
            if len(self._calls) >= MAX_OPEN_CALLS:
                return
            self._calls[ident] = {"turn": turn, "start": now, "last": now, "model": model}
            start = self._thread_on and self._thread is None
            if start:
                self._thread = threading.Thread(
                    target=self._run, name="stira-model-progress", daemon=True
                )
        if start:
            self._thread.start()

    def finished(self, ident: str | None) -> None:
        with self._lock:
            self._calls.pop(ident, None)

    def open_calls(self) -> int:
        with self._lock:
            return len(self._calls)

    def tick(self) -> int:
        """Emit what is due; returns how many ``model.progress`` events were written."""
        now = self._clock()
        turn = self.recorder.active.current()
        due: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for ident, call in list(self._calls.items()):
                if call["turn"] != turn:
                    del self._calls[ident]
                elif now - call["last"] >= self.interval_sec:
                    call["last"] = now
                    due.append((ident, dict(call)))
        written = 0
        for ident, call in due:
            data: dict[str, Any] = {
                "call_id": ident,
                "elapsed_ms": max(0, round((now - call["start"]) * 1000)),
            }
            if call["model"] is not None:
                data["model"] = call["model"]
            try:
                if self.recorder.emit("model.progress", data) is not None:
                    written += 1
            except Exception:  # noqa: BLE001 - telemetry never breaks a turn
                logger.warning("model progress event dropped")
        return written

    def _run(self) -> None:
        try:
            while True:
                time.sleep(self._poll_sec)
                self.tick()
                with self._lock:
                    if not self._calls:
                        self._thread = None
                        return
        except Exception:  # noqa: BLE001 - the ticker never takes the server down
            logger.warning("model progress ticker stopped")
            with self._lock:
                self._thread = None

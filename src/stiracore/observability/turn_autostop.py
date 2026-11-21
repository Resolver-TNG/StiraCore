"""#680 (ADR 0034): when a running chat turn stops by itself.

A chat turn runs on its own worker (#452): closing the page never stops it. Three rules
stop it without a person's press, each with a fixed reason code and fixed limits (no
setting, no environment variable):

* ``yolo_window_ended`` — the turn ran while a YOLO window was open and that window (the
  first one the turn saw; a later window never takes its place) is no
  longer the open one when the turn next waits for a person's approval. The turn stops at
  that wait (its card is rejected); a tool call already running is never cancelled.
* ``approval_unanswered`` — an approval card of this turn expired with nobody answering
  it (the card's own expiry, ``hitl_timeout_sec``), whether the page reads the turn or
  not. A person's answer never stops it.
* ``unattended_time_limit`` / ``unattended_token_limit`` — upper bounds for a turn whose
  page no longer reads it: :data:`UNATTENDED_TURN_MAX_SEC` since the turn started, or
  :data:`UNATTENDED_TURN_MAX_TOKENS` input + output tokens of the turn's model calls
  (each ``model.completed``).

Every rule only stops a turn (the explicit stop's path, ``stop: "cancelled"``); nothing
here approves, widens or delays anything. Decisions are pure: the clock and the window
reader are passed in (the chat blueprint passes the YOLO runtime's window id reader).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

YOLO_WINDOW_ENDED = "yolo_window_ended"
APPROVAL_UNANSWERED = "approval_unanswered"
UNATTENDED_TIME_LIMIT = "unattended_time_limit"
UNATTENDED_TOKEN_LIMIT = "unattended_token_limit"
AUTO_STOP_REASONS = frozenset(
    {YOLO_WINDOW_ENDED, APPROVAL_UNANSWERED, UNATTENDED_TIME_LIMIT, UNATTENDED_TOKEN_LIMIT}
)

# ADR 0034 §2: fixed upper bounds of a turn nobody reads (no tuning knob).
UNATTENDED_TURN_MAX_SEC = 3600.0
UNATTENDED_TURN_MAX_TOKENS = 2_000_000


class TurnAutoStop:
    """The automatic stop rules of one chat turn.

    ``window_id() -> str | None``: the id of the YOLO window open now; a reader that
    raises counts as "none open" — a turn that saw a window then stops at its next
    approval wait (fail toward stopping). ``now() -> float``: a monotonic clock (s).
    """

    def __init__(
        self,
        *,
        window_id: Callable[[], str | None] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._reader = window_id
        self._now = now or time.monotonic
        self._started = self._now()
        self._cards: set[str] = set()
        self._tokens = 0
        self._token_lock = threading.Lock()  # model.completed can come from several threads
        self._seen = self._read()

    def _read(self) -> str | None:
        if self._reader is None:
            return None
        try:
            value = self._reader()
        except Exception:  # noqa: BLE001 - unreadable: none open
            return None
        return value if isinstance(value, str) and value else None

    def note_window(self) -> None:
        """Remember a window that is open now (one opened during the turn counts too).

        The first window seen is the turn's window: a later one (another id) never takes
        its place, so a window that ended stops the turn even when a new one opened
        (as the delegate rule, ADR 0016 §7: another window id stops)."""
        if self._seen is not None:
            return
        self._seen = self._read()

    def note_model_completed(self, data: Any) -> None:
        """A ``model.completed`` of this turn: add its input + output tokens (the count
        ``progress.note_event`` keeps; the SDK reports the turn's usage only at its end)."""
        usage = data.get("usage") if isinstance(data, Mapping) else None
        if not isinstance(usage, Mapping):
            return
        added = sum(
            value
            for value in (usage.get("input_tokens"), usage.get("output_tokens"))
            if type(value) is int and value > 0
        )
        with self._token_lock:
            self._tokens += added

    def card_raised(self, request_id: Any) -> str | None:
        """A live approval card of this turn waits for a person -> a reason code or None."""
        if isinstance(request_id, str) and request_id:
            self._cards.add(request_id)
        current = self._read()
        if self._seen is not None and current != self._seen:
            return YOLO_WINDOW_ENDED
        if self._seen is None:
            self._seen = current
        return None

    def card_resolved(self, request_id: Any, status: Any, resolver: Any) -> str | None:
        """One of this turn's cards ended: expired unanswered -> ``approval_unanswered``."""
        if request_id in self._cards and status == "expired" and resolver == "system":
            return APPROVAL_UNANSWERED
        return None

    def limits(self, *, attended: bool, usage: Mapping | None) -> str | None:
        """The upper bounds of a turn whose page no longer reads it."""
        if attended:
            return None
        if self._now() - self._started >= UNATTENDED_TURN_MAX_SEC:
            return UNATTENDED_TIME_LIMIT
        usage = usage or {}
        tokens = 0
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                tokens += value
        if max(tokens, self._tokens) >= UNATTENDED_TURN_MAX_TOKENS:
            return UNATTENDED_TOKEN_LIMIT
        return None

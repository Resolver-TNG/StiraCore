"""Progress reports in a YOLO window (ADR 0014, phase 1): the checkpoint and the auto-continue.

One :class:`ProgressTracker` per process (one process serves one agent). It holds only
in-memory state: the running chat turn's checkpoint, the person's last contact, the
YOLO dialog's choice and the one scheduled automatic continue. The record is the turn
events (``progress.report`` / ``.reply`` / ``.auto_continue``), written by the chat API.

**Checkpoint** (§1, option T). At the first tool boundary inside a human chat / Stage turn
(``yolo.origin.covered``) of a YOLO window with reports on, once ``interval_min`` has
passed since the turn began or the person last acted, :meth:`check` cancels that call
with a fixed text (a tool result: the cached prompt prefix is untouched, #254). The model
then writes its short report and ends the turn (soft end). A tool call in a later batch
is cancelled too and the harness ends the turn after that batch with one fixed assistant
line (hard end, ``AfterToolsEvent.end_turn``: the history keeps alternating). Never two
checkpoints in one turn. Nothing here calls a model, starts a thread at a tool boundary or
touches the YOLO state.

**Automatic continue** (operator decision 2026-10-19, ADR 0014 §4). After a report, if
nobody answers within ``continue_after_min``, the chat API starts **one** continuation
turn — only while the YOLO window is ``active``, only for a report of a chat turn, at most
``max_auto_continues`` times in a row without a person's answer (then it waits), only
while the breaker is NORMAL and YOLO is not latched (``continue_gate``), and it is
cancelled by an answer, the report's Stop, the chat Stop, ``/new`` and the window's end.
The waiting itself calls no model (a timer thread that reads a clock and this state).

**Declared checkpoint** (phase 3, ``declared`` on). The agent declares a milestone with
``handoff_to_user(breakout_of_loop: true)``: in a covered turn of a window with reports on,
:meth:`check` cancels that call (before the rule, the card and the YOLO approver: no card,
no cap slot, no automatic approval) and the harness ends the turn after that batch with the
call's ``message`` as the one assistant line — masked, code blocks dropped, capped
(:func:`facts.clean_note`); it is the report's note. At most one declared report every
:data:`DECLARED_MIN_GAP_SEC` (from the last report of any trigger): a declaration inside
that gap is cancelled with a fixed text and the turn goes on (no report, no end). The
static guidance paragraph (:data:`DECLARED_GUIDANCE`, :meth:`guidance`) is the same bytes
while such a window is open and absent otherwise (#254).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from stiracore.yolo import origin as yolo_origin

from .facts import clean_note
from .settings import ProgressSettings

__all__ = [
    "AGAIN_TEXT",
    "DECLARED_GUIDANCE",
    "DECLARED_MIN_GAP_SEC",
    "DECLARED_TEXT",
    "DECLARED_TOO_SOON_TEXT",
    "HARD_END_TEXT",
    "NUDGE_TEXT",
    "ProgressTracker",
    "new_report_id",
]

logger = logging.getLogger(__name__)

NUDGE_TEXT = (
    "Checkpoint (progress report). This call was not run. Do not call tools now. In at most "
    "eight short lines tell the person: what is done, what you produced (paths), what is "
    "open or uncertain, what you would do next, and any question. Then end your turn."
)
AGAIN_TEXT = "Checkpoint (progress report): not run. The turn ends here; the person will answer."
HARD_END_TEXT = "[checkpoint]"
# Phase 3 (#451): the declared checkpoint. Fixed texts (no date, no count, no state).
DECLARED_MIN_GAP_SEC = 10 * 60
DECLARED_TEXT = (
    "Checkpoint (declared progress report): not run as a tool. Your message is the report; "
    "the turn ends here and the person will answer."
)
DECLARED_TOO_SOON_TEXT = (
    "Not a checkpoint: the last progress report was less than 10 minutes ago, so this call "
    "was not run and the turn goes on. Continue the task and report at a later milestone."
)
DECLARED_GUIDANCE = (
    "## Progress Reports\n\n"
    "In long tasks, when you finish an artifact or reach a fork in direction, you may stop "
    "and report: call handoff_to_user with breakout_of_loop true and a short message - what "
    "is done, what you produced (paths), what is open, what you would do next, any question. "
    "Do not quote file contents, commands or secrets in it. At most one such report every "
    "10 minutes; the person then answers continue, change of direction or stop."
)
_OPEN_STATES = ("active", "paused")


class ProgressTracker:
    """Thread-safe; every judgement reads its state under one lock (built-in values only)."""

    def __init__(
        self,
        settings: ProgressSettings,
        *,
        window_reader: Callable[[], Any] | None = None,
        continue_gate: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        timer: bool = True,
    ) -> None:
        self.settings = settings
        self._reader = window_reader
        # ``True`` only while the breaker is NORMAL and YOLO is not latched (read at the
        # due time). None, an exception or anything but ``True``: no automatic continue.
        self._gate = continue_gate
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        # Taken before ``_lock`` (never the other way): the start of a continuation and
        # every cancel of it are serialised, the state lock is held only for state.
        self._start_lock = threading.Lock()
        self._turn: dict[str, Any] | None = None
        self._since = clock()
        # Phase 4 (#451): tool calls / model tokens since the turn began or the person last
        # acted (reset with ``_since``).
        self._tools = 0
        self._tokens = 0
        # The YOLO dialog's tick: ``pending`` = the latest press's ``{"window", "value"}``
        # (the window id that press would open). Used only when exactly that window opens;
        # any other window gets the settings' default (PR #489 review, major 1).
        self._choice: dict[str, Any] = {"pending": None, "window": None, "value": False}
        self._auto: dict[str, Any] | None = None
        self._consecutive = 0
        self._starter: Callable[[dict], bool] | None = None
        self._timer = timer
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        # Phase 3: the clock reading of the last checkpoint (any trigger) of this process.
        self._last_report: float | None = None

    # -- wiring -------------------------------------------------------------------------
    def set_starter(self, starter: Callable[[dict], bool] | None) -> None:
        """The chat API's "start the continuation turn" (``True`` when one started)."""
        with self._lock:
            self._starter = starter

    def note_press(self, choice: Any, *, window: Any = None) -> None:
        """``POST /api/yolo/request``: the dialog's tick (``progress: true | false``) for the
        window ``window`` (its short id) this press would open.

        Every press replaces the previous press's choice — a missing / non-bool field or an
        unknown window clears it — so a tick never outlives its own request.
        """
        pending = None
        if type(choice) is bool and type(window) is str and window:
            pending = {"window": window, "value": choice}
        with self._lock:
            self._choice["pending"] = pending

    # -- the window -----------------------------------------------------------------------
    def window(self) -> dict | None:
        """The YOLO window's status view (``None``: no window, or unreadable)."""
        return self._current_window()

    def _current_window(self) -> dict | None:
        if self._reader is None:
            return None
        try:
            window = self._reader()
        except Exception:  # noqa: BLE001 - an unreadable window: no reports, no auto-continue
            return None
        return window if isinstance(window, dict) else None

    def _may_continue(self) -> bool:
        if self._gate is None:
            return False
        try:
            return self._gate() is True
        except Exception:  # noqa: BLE001 - cannot tell: the person decides
            return False

    def _enabled_for_locked(self, window: dict | None) -> bool:
        if window is None or window.get("state") not in _OPEN_STATES:
            return False
        window_id = window.get("id")
        if type(window_id) is not str or not window_id:
            return False
        if self._choice["window"] != window_id:
            pending = self._choice["pending"]
            value = self.settings.enabled
            if pending is not None and pending["window"] == window_id:
                value = pending["value"]
            self._choice.update(window=window_id, value=value, pending=None)
        return self._choice["value"] is True

    def window_state(self) -> dict[str, Any]:
        """For the page: whether reports are on in the open window (and the settings)."""
        window = self._current_window()
        with self._lock:
            on = self._enabled_for_locked(window)
        return {
            "window_open": window is not None and window.get("state") in _OPEN_STATES,
            "reports_on": on,
        }

    def guidance(self) -> str:
        """Phase 3: the static guidance paragraph, or ``""``.

        Present only with ``declared`` on and while a window with reports on is open
        (``active`` or ``paused``: a pause does not change it). A pure function of the
        settings and the window's id and choice, so the static prompt keeps its bytes
        until a window opens or ends (#254)."""
        if not self.settings.declared:
            return ""
        window = self._current_window()
        with self._lock:
            on = self._enabled_for_locked(window)
        return DECLARED_GUIDANCE if on else ""

    # -- the turn -------------------------------------------------------------------------
    def turn_started(self, turn_id: Any, *, auto: bool = False) -> None:
        now = self._clock()
        with self._lock:
            self._turn = {
                "turn_id": turn_id,
                "auto": auto is True,
                "started": now,
                "started_wall": self._wall(),
                "state": None,
                "batch": 0,
                "nudged_batch": None,
                "cancelled": set(),
                "end_turn_sent": False,
                "trigger": None,
                "count": None,
                "note": None,
            }
            # The clock of §2 restarts with every turn this tracker sees: a person's
            # message, an answer, or a continuation (its report would else be due at once).
            # So do the counts of phase 4.
            self._since = now
            self._tools = 0
            self._tokens = 0

    def note_contact(self) -> None:
        """A person acted (message, answer, approval decision): the interval restarts."""
        now = self._clock()
        with self._lock:
            self._since = now
            self._tools = 0
            self._tokens = 0

    def note_tokens(self, tokens: Any) -> None:
        """Phase 4: the tokens of one finished model call of the running turn."""
        if type(tokens) is not int or tokens <= 0:
            return
        with self._lock:
            if self._turn is not None:
                self._tokens += tokens

    def note_event(self, event: Any) -> None:
        """A turn event of the running turn (the chat API's subscription): the token count
        of a ``model.completed`` (input + output; codes and numbers only)."""
        if not isinstance(event, dict) or event.get("type") != "model.completed":
            return
        data = event.get("data")
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        total = sum(
            value
            for value in (usage.get("input_tokens"), usage.get("output_tokens"))
            if type(value) is int and value > 0
        )
        self.note_tokens(total)

    def batch(self) -> None:
        """``BeforeToolsEvent``: a new tool batch of the running turn."""
        with self._lock:
            if self._turn is not None:
                self._turn["batch"] += 1

    def check(
        self, tool_use_id: Any, agent_id: Any = None, *, declaration: Any = None
    ) -> str | None:
        """``BeforeToolCallEvent``, first: the cancel text when this call meets a checkpoint.

        ``declaration``: the ``message`` of a ``handoff_to_user(breakout_of_loop: true)``
        call (a ``str``), else ``None`` (phase 3; read only with ``declared`` on)."""
        if not yolo_origin.covered(agent_id):
            return None
        window = self._current_window()
        now = self._clock()
        declared = self.settings.declared and type(declaration) is str
        with self._lock:
            turn = self._turn
            if turn is None:
                return None
            if turn["state"] is not None:
                turn["cancelled"].add(str(tool_use_id))
                if turn["state"] == "soft" and turn["batch"] != turn["nudged_batch"]:
                    # A tool call after the nudge ends the turn; a declaration made then
                    # is the note it was asked for.
                    note = clean_note(declaration) if declared else None
                    turn["state"] = "declared" if note else "hard"
                    if note:
                        turn["note"] = note
                        return DECLARED_TEXT
                return NUDGE_TEXT if turn["state"] == "soft" else AGAIN_TEXT
            if not self._enabled_for_locked(window):
                return None
            if declared:
                last = self._last_report
                if last is not None and now - last < DECLARED_MIN_GAP_SEC:
                    return DECLARED_TOO_SOON_TEXT  # cancelled; the turn goes on
                turn.update(trigger="declared", state="declared", note=clean_note(declaration))
                turn["nudged_batch"] = turn["batch"]
                turn["cancelled"].add(str(tool_use_id))
                return DECLARED_TEXT
            trigger, count = None, None
            limit_tools, limit_tokens = self.settings.count_tools, self.settings.count_tokens
            if now - self._since >= self.settings.interval_sec:
                trigger = "time"
            elif limit_tools > 0 and self._tools >= limit_tools:
                trigger, count = "count", "tools"
            elif limit_tokens > 0 and self._tokens >= limit_tokens:
                trigger, count = "count", "tokens"
            if trigger is None:
                self._tools += 1  # this call runs (or goes on to its rule / card)
                return None
            turn["trigger"], turn["count"] = trigger, count
            turn["state"] = "soft"
            turn["nudged_batch"] = turn["batch"]
            turn["cancelled"].add(str(tool_use_id))
            return NUDGE_TEXT

    def end_turn_text(self) -> str | None:
        """``AfterToolsEvent``: the hard end's one assistant line (once per turn) — for a
        declared checkpoint, the cleaned note (or the fixed line when it is empty)."""
        with self._lock:
            turn = self._turn
            if turn is None or turn["state"] not in ("hard", "declared"):
                return None
            if turn["end_turn_sent"]:
                return None
            turn["end_turn_sent"] = True
            return turn["note"] or HARD_END_TEXT

    def turn_finished(self, turn_id: Any) -> dict[str, Any] | None:
        """The turn ended: ``{"end", "elapsed_sec", "cancelled"}`` for a checkpoint, else None."""
        now = self._clock()
        with self._lock:
            turn = self._turn
            if turn is None or turn["turn_id"] != turn_id:
                return None
            self._turn = None
            if turn["state"] is None:
                return None
            self._last_report = now
            end = turn["state"]
            if end == "declared":
                end = "soft" if turn["note"] else "hard"
            result = {
                "end": end,
                "elapsed_sec": max(0, int(now - turn["started"])),
                "cancelled": sorted(turn["cancelled"]),
                "auto": turn["auto"],
                "trigger": turn["trigger"] or "time",
            }
            if turn["count"] is not None:
                result["count"] = turn["count"]
            if turn["note"]:
                result["note"] = turn["note"]
            return result

    # -- after a report ---------------------------------------------------------------------
    def report_made(self, report_id: str, turn_id: Any) -> dict[str, Any]:
        """Schedule the one automatic continue of this report, if every condition holds.

        -> ``{"auto": "scheduled" | "limit" | "off" | "window", "due_wall", "n", "max"}``.
        """
        window = self._current_window()
        now, wall = self._clock(), self._wall()
        limit = self.settings.max_auto_continues
        with self._lock:
            self._auto = None
            result: dict[str, Any] = {"auto": "off", "max": limit, "n": self._consecutive}
            if limit <= 0:
                return result
            if window is None or window.get("state") != "active":
                result["auto"] = "window"
                return result
            if self._consecutive >= limit:
                result["auto"] = "limit"
                return result
            delay = self.settings.continue_after_sec
            self._auto = {
                "report_id": report_id,
                "turn_id": turn_id,
                "due": now + delay,
                "due_wall": wall + delay,
                "n": self._consecutive + 1,
                "window": window.get("id"),
            }
            result.update(auto="scheduled", due_wall=self._auto["due_wall"], n=self._auto["n"])
        self._ensure_thread()
        return result

    def auto_view(self, report_id: Any) -> dict[str, Any] | None:
        with self._lock:
            auto = self._auto
            if auto is None or auto["report_id"] != report_id:
                return None
            return {
                "due_wall": auto["due_wall"],
                "n": auto["n"],
                "max": self.settings.max_auto_continues,
            }

    def consecutive(self) -> int:
        with self._lock:
            return self._consecutive

    def cancel_auto(self, reason: str = "cancelled") -> bool:
        """Stop / ``/new`` / chat Stop / hold: the scheduled continue never starts."""
        with self._start_lock, self._lock:
            had = self._auto is not None
            self._auto = None
        self._wake.set()
        if had:
            logger.info("progress: automatic continue cancelled (%s)", reason)
        return had

    def answered(self, *, human: bool) -> None:
        """A report was answered: the scheduled continue goes; a person resets the count."""
        with self._start_lock, self._lock:
            self._auto = None
            if human:
                self._consecutive = 0
        self._wake.set()
        if human:
            self.note_contact()

    def reset(self) -> None:
        """``/new``: a new conversation; nothing of the old one continues."""
        with self._start_lock, self._lock:
            self._auto = None
            self._consecutive = 0
            self._turn = None
            self._last_report = None
        self._wake.set()

    def tick(self) -> bool:
        """Start the due continuation (the timer thread; tests call it with a fake clock).

        The checks and the start run under the start lock, which every cancel (a Stop, an
        answer, ``/new``, hold) takes too: one either comes first (nothing starts) or after
        (it sees the continuation turn and can stop it). The starter's I/O (its turn event)
        runs outside the state lock, so a tool boundary's :meth:`check` never waits on it.
        """
        with self._start_lock:
            return self._tick_locked()

    def _tick_locked(self) -> bool:
        with self._lock:
            auto = self._auto
            if auto is None or self._clock() < auto["due"]:
                return False
        # Due: the window and the gate are read now. Any of them narrowing drops this
        # continue (it is not kept for later): the report waits for a person.
        window = self._current_window()
        may_continue = self._may_continue()
        with self._lock:
            auto = self._auto
            if auto is None or self._clock() < auto["due"]:
                return False
            self._auto = None
            starter = self._starter
            if (
                starter is None
                or not may_continue
                or window is None
                or window.get("state") != "active"
                or window.get("id") != auto["window"]
                or self._choice["window"] != auto["window"]
                or self._choice["value"] is not True
                or self._consecutive >= self.settings.max_auto_continues
            ):
                return False
            self._consecutive += 1  # reserved: the started turn's report counts it
        try:
            started = starter(dict(auto)) is True
        except Exception:  # noqa: BLE001 - nothing started: wait for the person
            logger.warning("progress: the automatic continue could not start")
            started = False
        if not started:
            with self._lock:
                self._consecutive = max(0, self._consecutive - 1)
        return started

    def _ensure_thread(self) -> None:
        if not self._timer:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._thread = threading.Thread(target=self._run, name="progress-auto", daemon=True)
            thread = self._thread
        thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                auto = self._auto
                if auto is None:
                    self._thread = None
                    return
                remaining = auto["due"] - self._clock()
            self._wake.wait(max(0.05, min(remaining, 1.0)))
            self._wake.clear()
            try:
                self.tick()
            except Exception:
                logger.exception("progress: automatic continue check failed")


def new_report_id() -> str:
    """128-bit random, hex."""
    return secrets.token_hex(16)

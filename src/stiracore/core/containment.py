"""Containment stages and threat signals — phase 0: observe only (ADR 0023, #460-0).

ADR 0023 adds a second axis next to the emotional breaker level, ordered
``open < restricted < closed < quarantine < terminated``, raised only by deterministic
signals built from refusals and holds the gate already makes. **Phase 0 changes nothing**:
this module counts those signals in a sliding window and computes the stage the session
*would be* in (``would_be``). No tool is removed, no card is added, no level moves, nothing
is persisted as a stage. The thresholds and the window are the commander's starting values
(ADR 0023 Q3), to be adjusted from a week of counts before phase 1.

What is recorded is a code and numbers only: the signal (``G1`` … ``G6``, ``T``), the source
code (``E_READ_SECRET_PATH``, ``write_floor`` …), its weight, the window score and the
``would_be`` stage. Never a path, an argument, a tool result or any text.

Switch: ``containment`` in ``agent-config.json`` (``{"phase": 0}``; absent or ``{}`` = off).
``STIRA_CONTAINMENT_OBSERVE=0`` (``false`` / ``no`` / ``off``) forces it off; no value of the
variable turns it on.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "BURST_CALLS",
    "BURST_SECONDS",
    "CODE_SIGNALS",
    "ENV_VAR",
    "G3_REPEAT_FOR_CLOSED",
    "PHASES",
    "REPEAT_REFUSALS",
    "SIGNAL_WEIGHTS",
    "THRESHOLDS",
    "WINDOW_DEFAULT",
    "ContainmentObserver",
    "ContainmentSettings",
    "Stage",
    "clean_options",
    "clean_summary",
    "describe",
    "replay",
    "settings_from",
    "would_be",
]

logger = logging.getLogger(__name__)

ENV_VAR = "STIRA_CONTAINMENT_OBSERVE"
PHASES = (0,)
_FALSY = frozenset({"0", "false", "no", "off"})
_KEYS = frozenset({"phase", "window_seconds"})


class Stage(str, Enum):
    """The containment axis (ADR 0023 §1), in order. Phase 0 never *enters* a stage."""

    OPEN = "open"
    RESTRICTED = "restricted"
    CLOSED = "closed"
    QUARANTINE = "quarantine"
    TERMINATED = "terminated"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)


_ORDER = (Stage.OPEN, Stage.RESTRICTED, Stage.CLOSED, Stage.QUARANTINE, Stage.TERMINATED)

# ADR 0023 §3: the weight of each signal. ``T`` (text cues in untrusted content) is counted
# and never raises anything.
SIGNAL_WEIGHTS: dict[str, float] = {
    "G1": 2.0,  # secret probing
    "G2": 2.0,  # floor writes
    "G3": 3.0,  # self-administration
    "G4": 1.0,  # egress holds
    "G5": 1.0,  # burst
    "G6": 0.5,  # reach-outs
    "T": 0.0,  # text cues (observe only, ADR 0023 Q2)
}

# The fixed source codes and their signal. Anything else is not a signal (ignored).
CODE_SIGNALS: dict[str, str] = {
    # G1: a refused read on the secret-path floor (audit.read.denied, #457)
    "E_READ_SECRET_PATH": "G1",
    # G1 too: a refused write on the same floor (#536)
    "E_WRITE_SECRET_PATH": "G1",
    # G2: a write refused by the write floor (SOUL / identity / policy / data-dir tree) and
    # memory_write to a layer other than episodic
    "write_floor": "G2",
    "memory_layer": "G2",
    # G3: a URL tool aimed at Stira's own dashboard (#495)
    "E_OWN_DASHBOARD": "G3",
    # G4: an egress call the YOLO check sends to a person (ADR 0012 §10); ADR 0022's flow
    # holds join here when that layer exists
    "secret_in_args": "G4",
    "credential_by_reference": "G4",
    # G5: a burst of tool calls in one agent context; the same refused call again and again
    "burst_calls": "G5",
    "repeated_refusal": "G5",
    # G6: reads outside the read roots, writes outside the write roots
    "E_READ_OUTSIDE_ROOTS": "G6",
    "outside_write_roots": "G6",
    # T: a fixed phrase in untrusted content (a web page, an MCP result, …)
    "text_cue": "T",
}

# ADR 0023 §3 / Q3: starting thresholds of the window score (a person may raise them in a
# later phase; phase 0 only reports ``would_be``).
THRESHOLDS: dict[str, float] = {"restricted": 2.0, "closed": 5.0, "quarantine": 9.0}
G3_REPEAT_FOR_CLOSED = 2  # "or any G3 twice"
WINDOW_DEFAULT = 600  # 10 minutes
WINDOW_MIN, WINDOW_MAX = 60, 3600
BURST_CALLS = 60  # more than 60 tool calls …
BURST_SECONDS = 120.0  # … in 120 s, in one agent context
REPEAT_REFUSALS = 3  # the same refused call 3 times (within the window)


def would_be(score: float, g3_count: int = 0) -> Stage:
    """The stage a score would reach (ADR 0023 §3). ``terminated`` is a person's action only."""
    if score >= THRESHOLDS["quarantine"]:
        return Stage.QUARANTINE
    if score >= THRESHOLDS["closed"] or g3_count >= G3_REPEAT_FOR_CLOSED:
        return Stage.CLOSED
    if score >= THRESHOLDS["restricted"]:
        return Stage.RESTRICTED
    return Stage.OPEN


# -- settings -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainmentSettings:
    enabled: bool = False
    phase: int = 0
    window_seconds: int = WINDOW_DEFAULT
    forced_off: bool = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "phase": self.phase if self.enabled else None,
            "forced_off": self.forced_off,
            "window_seconds": self.window_seconds if self.enabled else None,
        }


def clean_options(value: Any) -> dict[str, Any]:
    """Validate the ``containment`` block (``AgentConfig``); ``{}`` = off."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("containment must be an object")  # noqa: TRY004 - config contract
    if not value:
        return {}
    unknown = sorted(set(value) - _KEYS)
    if unknown:
        raise ValueError(f"containment: unknown key(s) {', '.join(unknown)}")
    if "phase" not in value:
        raise ValueError("containment.phase is required (0: observe only)")
    phase = value["phase"]
    if type(phase) is not int or phase not in PHASES:
        raise ValueError(
            "containment.phase must be 0 (phases 1 and 2 are not authorised yet, ADR 0023)"
        )
    window = value.get("window_seconds", WINDOW_DEFAULT)
    if type(window) is not int or not WINDOW_MIN <= window <= WINDOW_MAX:
        raise ValueError(
            f"containment.window_seconds must be an integer from {WINDOW_MIN} to {WINDOW_MAX}"
        )
    return {"phase": phase, "window_seconds": window}


def settings_from(options: Any, environ: Mapping[str, str] | None = None) -> ContainmentSettings:
    """The effective settings. Never raises: an invalid block counts as off (logged)."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR)
    if raw is not None and raw.strip().lower() in _FALSY:
        return ContainmentSettings(forced_off=True)
    try:
        clean = clean_options(options)
    except ValueError as error:
        logger.warning("containment observation off: invalid settings (%s)", type(error).__name__)
        return ContainmentSettings()
    if not clean:
        return ContainmentSettings()
    return ContainmentSettings(
        enabled=True, phase=clean["phase"], window_seconds=clean["window_seconds"]
    )


# -- the observer ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hit:
    at: float
    signal: str
    code: str


@dataclass
class ContainmentObserver:
    """Count signals in a sliding window; report the ``would_be`` stage. Never acts.

    ``record(row)`` receives one dict per observed signal (codes and numbers only); a
    failing ``record`` is logged and ignored. Every public method is thread-safe and never
    raises into the caller (the gate's hooks call it).
    """

    window_seconds: int = WINDOW_DEFAULT
    clock: Callable[[], float] = time.time
    record: Callable[[dict[str, Any]], Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _hits: deque = field(default_factory=deque, repr=False)
    _calls: dict = field(default_factory=dict, repr=False)
    _refused: dict = field(default_factory=dict, repr=False)
    _totals: Counter = field(default_factory=Counter, repr=False)
    _peak: Stage = Stage.OPEN
    _started: float | None = None

    def __post_init__(self) -> None:
        if type(self.window_seconds) is not int or not (
            WINDOW_MIN <= self.window_seconds <= WINDOW_MAX
        ):
            raise ValueError("window_seconds out of range")
        self._started = self._now()

    def _now(self) -> float:
        try:
            return float(self.clock())
        except Exception:  # noqa: BLE001 - a broken clock must not break a tool call
            return time.time()

    def _prune_locked(self, now: float) -> None:
        horizon = now - self.window_seconds
        while self._hits and self._hits[0].at <= horizon:
            self._hits.popleft()
        for key in [k for k, (_n, last) in self._refused.items() if last <= horizon]:
            del self._refused[key]

    def _score_locked(self) -> tuple[float, Counter]:
        counts = Counter(hit.signal for hit in self._hits)
        score = sum(SIGNAL_WEIGHTS[name] * n for name, n in counts.items())
        return round(score, 2), counts

    def observe(self, code: Any) -> dict[str, Any] | None:
        """One signal by its source code; ``None`` (nothing counted) for any other value."""
        try:
            if type(code) is not str or code not in CODE_SIGNALS:
                return None
            signal = CODE_SIGNALS[code]
            now = self._now()
            with self._lock:
                self._prune_locked(now)
                self._hits.append(_Hit(now, signal, code))
                self._totals[code] += 1
                score, counts = self._score_locked()
                stage = would_be(score, counts.get("G3", 0))
                if stage.rank > self._peak.rank:
                    self._peak = stage
            row = {
                "signal": signal,
                "code": code,
                "weight": SIGNAL_WEIGHTS[signal],
                "score": score,
                "would_be": stage.value,
                "window_seconds": self.window_seconds,
                "phase": 0,
            }
        except Exception as error:  # noqa: BLE001 - observation never breaks the caller
            logger.warning("containment: a signal could not be counted (%s)", type(error).__name__)
            return None
        if self.record is not None:
            try:
                self.record(dict(row))
            except Exception as error:  # noqa: BLE001 - the record must never break a call
                logger.warning("containment: observation not recorded (%s)", type(error).__name__)
        return row

    def note_tool_call(self, context: Any) -> dict[str, Any] | None:
        """One tool call in an agent context (``G5`` when more than 60 come within 120 s)."""
        try:
            now = self._now()
            with self._lock:
                times = self._calls.setdefault(context, deque())
                while times and times[0] <= now - BURST_SECONDS:
                    times.popleft()
                times.append(now)
                burst = len(times) > BURST_CALLS
                if burst:
                    times.clear()  # one signal per burst; the next one needs 61 more calls
                if len(self._calls) > 64:  # bounded: drop contexts with no recent call
                    for key in [k for k, v in self._calls.items() if not v]:
                        del self._calls[key]
        except Exception as error:  # noqa: BLE001 - observation never breaks the caller
            logger.warning(
                "containment: a tool call could not be counted (%s)", type(error).__name__
            )
            return None
        return self.observe("burst_calls") if burst else None

    def note_refused(self, fingerprint: Any) -> dict[str, Any] | None:
        """A refused call by an opaque fingerprint (``G5`` at the third same refusal)."""
        try:
            if type(fingerprint) is not str or not fingerprint:
                return None
            now = self._now()
            with self._lock:
                self._prune_locked(now)
                count = self._refused.get(fingerprint, (0, now))[0] + 1
                repeated = count >= REPEAT_REFUSALS
                self._refused[fingerprint] = (0 if repeated else count, now)
                if len(self._refused) > 1024:
                    oldest = min(self._refused, key=lambda k: self._refused[k][1])
                    del self._refused[oldest]
        except Exception as error:  # noqa: BLE001 - observation never breaks the caller
            logger.warning("containment: a refusal could not be counted (%s)", type(error).__name__)
            return None
        return self.observe("repeated_refusal") if repeated else None

    def snapshot(self) -> dict[str, Any]:
        """The window now: score, ``would_be``, counts by signal and code (numbers only)."""
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            score, counts = self._score_locked()
            codes = Counter(hit.code for hit in self._hits)
            stage = would_be(score, counts.get("G3", 0))
            return {
                "phase": 0,
                "observe_only": True,
                "window_seconds": self.window_seconds,
                "score": score,
                "would_be": stage.value,
                "peak_would_be": self._peak.value,
                "signals": {name: counts.get(name, 0) for name in SIGNAL_WEIGHTS},
                "codes": dict(sorted(codes.items())),
                "totals": dict(sorted(self._totals.items())),
                "since": self._started,
            }


def clean_summary(value: Any) -> dict[str, Any] | None:
    """The ``containment`` object of ``turn.summary``: ``would_be`` and ``score`` only."""
    if not isinstance(value, Mapping):
        return None
    stage = value.get("would_be")
    score = value.get("score")
    if stage not in {s.value for s in _ORDER} or stage == Stage.TERMINATED.value:
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score < 1e6:
        return None
    return {"would_be": stage, "score": round(float(score), 2), "observe_only": True}


def describe() -> dict[str, Any]:
    """The fixed tables (stages, weights, codes, thresholds) for the API and the CLI."""
    return {
        "stages": [stage.value for stage in _ORDER],
        "weights": dict(SIGNAL_WEIGHTS),
        "codes": dict(CODE_SIGNALS),
        "thresholds": dict(THRESHOLDS),
        "g3_repeat_for_closed": G3_REPEAT_FOR_CLOSED,
        "burst": {"calls": BURST_CALLS, "seconds": BURST_SECONDS},
        "repeat_refusals": REPEAT_REFUSALS,
    }


def replay(
    codes: Iterable[tuple[float, str]], window_seconds: int = WINDOW_DEFAULT
) -> list[dict[str, Any]]:
    """Replay ``(timestamp, code)`` pairs through a fresh observer (the CLI's what-if)."""
    current = {"t": 0.0}
    observer = ContainmentObserver(window_seconds=window_seconds, clock=lambda: current["t"])
    rows = []
    for at, code in sorted(codes, key=lambda pair: pair[0]):
        current["t"] = float(at)
        row = observer.observe(code)
        if row is not None:
            rows.append({"at": float(at), **row})
    return rows

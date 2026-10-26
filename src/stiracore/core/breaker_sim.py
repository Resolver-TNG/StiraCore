"""Deterministic, in-memory breaker experiments using the public engine API."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass

from .engine import BreakLevel, StiraConfig, StiraEngine
from .neural_state import EMOTION_MATRIX, NeuralState

FIXED_EPOCH = 1_700_000_000.0

Stimulus = tuple[str | None, float, float]


@dataclass(frozen=True)
class Observation:
    """State and effective breaker level at one public API boundary."""

    stability: float
    bc_score: float
    level: BreakLevel


@dataclass(frozen=True)
class TurnSample:
    """Observations immediately after pre_turn and immediately after post_turn."""

    turn: int
    pre: Observation
    post: Observation


@dataclass(frozen=True)
class SimulationResult:
    """Detached observations and first visits, indexed by one-based turn number."""

    turns: tuple[TurnSample, ...]
    first_reached: dict[BreakLevel, int | None]


class _DiscardCheckpointStore:
    """Write-only sink for the save(checkpoint) call made by pre_turn.

    Simulation never resumes a checkpoint. Supplying this sink avoids creating
    the engine's default filesystem-backed store.
    """

    def save(self, _checkpoint: object) -> str:
        return "simulation"


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def simulate(
    stimuli: Iterable[Stimulus],
    config: StiraConfig | None = None,
    *,
    now_fn: Callable[[], float] | None = None,
) -> SimulationResult:
    """Run an isolated experiment without reading or writing persistent state.

    Each input is (stimulus, intensity, hours_elapsed_before_turn). A None
    stimulus represents an idle turn; unknown stimulus names are rejected.

    The injected clock is sampled exactly once to establish the initial epoch.
    Thereafter a local clock advances solely by the supplied elapsed hours.
    Omitting now_fn uses FIXED_EPOCH, never the wall clock.

    For each item:
      1. Advance the local clock.
      2. Call pre_turn once: decay, BC sampling, cooldown and checkpoint.
      3. Call post_turn once: apply the stimulus and evaluate the breaker.

    Both boundaries are recorded because post_turn can enter a level that was
    not active at pre_turn. first_reached records the earliest occurrence at
    either boundary, including NORMAL; it does not infer hidden latches from
    threshold crossings. An empty experiment leaves every entry None.

    BC is sampled by pre_turn, not after the stimulus. Post-turn CRITICAL entry
    consequently starts counting protected attempts at the next pre_turn.
    No extra policy query or final flushing turn is performed.

    Input configuration is copied. Engine diagnostic logging remains unchanged;
    no logging configuration or other process-global state is modified.
    """
    clock = _finite_number(now_fn() if now_fn is not None else FIXED_EPOCH, "epoch")
    state = NeuralState(last_stimulus_time=clock, last_decay_check=clock)
    engine = StiraEngine(
        state=state,
        config=deepcopy(config) if config is not None else None,
        now_fn=lambda: clock,
        checkpoint_store=_DiscardCheckpointStore(),
    )
    samples: list[TurnSample] = []
    first: dict[BreakLevel, int | None] = dict.fromkeys(BreakLevel)

    for turn, (stimulus, intensity, hours) in enumerate(stimuli, start=1):
        if stimulus is not None and stimulus not in EMOTION_MATRIX:
            raise ValueError(f"Unknown stimulus: {stimulus!r}")
        intensity = _finite_number(intensity, "intensity")
        hours = _finite_number(hours, "hours_elapsed_before_turn")
        if hours < 0:
            raise ValueError("hours_elapsed_before_turn must be nonnegative")
        clock = _finite_number(clock + hours * 3600.0, "simulation clock")

        result = engine.pre_turn()
        snapshot = engine.state.snapshot()
        pre = Observation(
            stability=snapshot["stability"],
            bc_score=snapshot["bc_score"],
            level=result.level,
        )

        summary = engine.post_turn(stimulus=stimulus, intensity=intensity)
        post = Observation(
            stability=summary["stability"],
            bc_score=summary["bc_score"],
            level=BreakLevel(summary["breaker"]),
        )
        samples.append(TurnSample(turn=turn, pre=pre, post=post))
        for observation in (pre, post):
            if first[observation.level] is None:
                first[observation.level] = turn

    return SimulationResult(turns=tuple(samples), first_reached=first)


# -- ADR 0023 phase 0 (#460-0): a containment signal sequence beside the stimuli ----------

SignalStep = tuple[str | None, float, float, tuple[str, ...]]


@dataclass(frozen=True)
class ContainmentSample:
    """The observer's window after one turn's signal codes (observe only)."""

    turn: int
    codes: tuple[str, ...]
    score: float
    would_be: str


@dataclass(frozen=True)
class SignalSimulationResult:
    """The breaker experiment (unchanged by the signals) and the containment samples."""

    breaker: SimulationResult
    containment: tuple[ContainmentSample, ...]


def simulate_with_signals(
    steps: Iterable[SignalStep],
    config: StiraConfig | None = None,
    *,
    now_fn: Callable[[], float] | None = None,
    window_seconds: int = 600,
) -> SignalSimulationResult:
    """:func:`simulate` with each turn's containment signal codes (ADR 0023 phase 0).

    Each step is ``(stimulus, intensity, hours_elapsed_before_turn, codes)``. The breaker
    part is exactly :func:`simulate` of the first three fields — signals never reach the
    engine, which is what the regression tests compare. The codes are counted by a
    :class:`~stiracore.core.containment.ContainmentObserver` on the same local clock
    (all codes of a turn at that turn's time); its ``would_be`` is reported per turn.
    """
    from .containment import ContainmentObserver

    items = [tuple(step) for step in steps]
    breaker = simulate(((s, i, h) for s, i, h, _codes in items), config, now_fn=now_fn)
    clock = {"t": _finite_number(now_fn() if now_fn is not None else FIXED_EPOCH, "epoch")}
    observer = ContainmentObserver(window_seconds=window_seconds, clock=lambda: clock["t"])
    samples = []
    for turn, (_stimulus, _intensity, hours, codes) in enumerate(items, start=1):
        clock["t"] += _finite_number(hours, "hours_elapsed_before_turn") * 3600.0
        codes = tuple(codes)
        for code in codes:
            observer.observe(code)
        window = observer.snapshot()
        samples.append(ContainmentSample(turn, codes, window["score"], window["would_be"]))
    return SignalSimulationResult(breaker=breaker, containment=tuple(samples))

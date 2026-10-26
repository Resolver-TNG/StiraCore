"""Lightweight heartbeat gateway with no model dependency."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from stiracore.core import BreakLevel

from .pacing import compute_interval

Snapshot = dict[str, Any]
SnapshotSink = Callable[[Snapshot], None]


class HeartbeatIntervalsError(ValueError):
    """The configuration's heartbeat bounds were unusable (floor > ceil) and replaced on load.

    Raised by ``HeartbeatGateway`` so that the tick does not run on bounds the person did not
    choose (#512, fail-closed); the class name is the reason the status shows.
    """


class HeartbeatGateway:
    """Apply active decay, capture state, and calculate the next heartbeat."""

    def __init__(
        self,
        engine: Any,
        gateway_config: Any,
        *,
        now_fn: Callable[[], float] | None = None,
        snapshot_sink: SnapshotSink | None = None,
        snapshot_limit: int = 1000,
    ) -> None:
        if getattr(gateway_config, "hb_intervals_reset", False) is True:
            raise HeartbeatIntervalsError("heartbeat bounds were reset on load (floor > ceil)")
        self.engine = engine
        self.gateway_config = gateway_config
        self._now_fn = now_fn or time.time
        # Bounded default sink: retains the most recent snapshots so a
        # long-running loop cannot grow memory without limit. A caller that
        # provides its own sink (e.g. #55 observability / persistence) owns
        # retention itself.
        self._snapshots: deque[Snapshot] = deque(maxlen=snapshot_limit)
        self._snapshot_sink = snapshot_sink or self._snapshots.append
        self._tick_count = 0

    @property
    def snapshots(self) -> tuple[Snapshot, ...]:
        """Return snapshots retained by the default sink."""
        return tuple(self._snapshots)

    def tick(self) -> dict[str, Any]:
        """Run one zero-model heartbeat tick."""
        state = self.engine.state
        # Same lock as pre_turn/post_turn, so a tick never interleaves with a
        # turn's state update and loses it (#147). Engines without one (test
        # doubles) keep the previous unsynchronized behaviour.
        lock = getattr(self.engine, "state_lock", None) or nullcontext()
        with lock:
            decay_deltas = state.apply_decay(
                now_fn=self._now_fn,
                min_interval_sec=self.engine.config.decay_min_interval_sec,
            )

            self._tick_count += 1
            snapshot: Snapshot = {
                "turn": self._tick_count,
                "timestamp": self._now_fn(),
                **state.snapshot(),
            }
            # Query the engine's hysteresis/cooldown policy without counting a turn.
            breaker = self.engine.breaker_status()
        self._snapshot_sink(snapshot)
        if breaker.level == BreakLevel.CRITICAL:
            level, priority = "critical", "P0"
        elif breaker.level in (BreakLevel.WARNING, BreakLevel.COHERENCE):
            level, priority = "warning", None
        else:
            level, priority = "normal", None

        next_interval_sec = compute_interval(
            state.stability,
            self.gateway_config.hb_interval_floor_sec,
            self.gateway_config.hb_interval_ceil_sec,
        )
        return {
            "decay_deltas": decay_deltas,
            "snapshot": snapshot,
            "level": level,
            "priority": priority,
            "next_interval_sec": next_interval_sec,
        }

    def run_once(self) -> dict[str, Any]:
        """Alias for one heartbeat tick."""
        return self.tick()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run ticks at adaptive intervals until the caller requests a stop.

        The wait between ticks is interruptible: a stop_event set during the
        sleep returns immediately instead of blocking for the full (possibly
        multi-hour) interval, so shutdown stays responsive.
        """
        while not stop_event.is_set():
            result = self.tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=result["next_interval_sec"])
            except TimeoutError:
                continue

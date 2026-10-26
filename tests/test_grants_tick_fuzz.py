"""W6d (#296): the heartbeat start's admission against a small oracle.

Random sequences of state changes and wakes (fixed seeds: deterministic) drive a
:class:`GrantTicker` over fake collaborators. The oracle is the admission rule written
as a list of independent clauses; the properties:

* a wake starts an iteration **iff** every clause holds;
* a skipped wake names one of the clauses that fail (and is counted once);
* a wake presses at most once, and never when a clause fails;
* nothing is deferred: a skipped wake leaves no trace a later wake acts on.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

import pytest

from stiracore.grants.schema import GrantRefused
from stiracore.scheduler import grant_tick
from stiracore.scheduler.grant_tick import GrantTicker
from stiracore.scheduler.scheduler import RunToken
from stiracore.yolo.allowance import ClockReading

SPACING = 5  # below the setting's floor on purpose: the flood guard is then reachable
WINDOW = 60.0
FLOOD = 5


@dataclass(frozen=True)
class G:
    grant_id: str = "a" * 32
    heartbeat_start: bool = True
    status: str = "active"
    goal_status: str = "active"
    issued_wall: float = 0.0
    issued_mono: float = 0.0
    issued_boot: float = 0.0
    last_wall: float = 0.0
    last_boot: float = 0.0
    last_mono: float = 0.0
    ttl_sec: int = 7200
    iterations: int = 0
    max_iterations: int = 10
    used_tokens: int = 0
    total_tokens: int = 1000
    stop_epoch: int = 0


class World:
    def __init__(self) -> None:
        self.switch = True
        self.grant: G | None = G()
        self.killed = False
        self.auth_sick = False
        self.store_sick = False
        self.breaker = "normal"
        self.hb = True
        self.clock_broken = False
        self.now = 100.0  # every clock (wall = mono = executor mono)
        self.running = False
        self.stopping = False
        self.abandoned = 0
        self.turn = False
        self.ends: list[tuple[str, str, float, int]] = []
        self.preempt_signal = type("Signal", (), {"seq": 0, "pending": 0})()
        self.starts: list[float] = []
        self.steps = 0
        self.resumed: dict[str, float] = {}  # #644 U3: grant id -> the last resume's clocks

    # the grant service
    @property
    def unhealthy_reason(self):
        return "x" if self.store_sick else None

    def tick_record(self):
        return self.grant if self.grant is not None and self.grant.status != "revoked" else None

    def breaker_level(self):
        return self.breaker

    def resumed_at(self, grant_id):
        if grant_id not in self.resumed:
            return False, None
        at = self.resumed[grant_id]
        return True, ClockReading(wall=at, boot=at, mono=at)

    def clock_reading(self):
        if self.clock_broken:
            return None
        return ClockReading(wall=self.now, boot=self.now, mono=self.now)

    # the executor
    def tick_facts(self, _grant_id):
        return {
            "running": self.running,
            "abandoned": self.abandoned,
            "turn_busy": self.turn,
            "stopping": self.stopping,
            "ends": list(self.ends),
            "now": self.now,
        }

    def step(self, grant_id, *, tick):
        self.steps += 1
        code = tick.recheck(self.grant) or tick.fresh() or tick.live(fenced=True)
        if code is not None:
            raise GrantRefused("grant_iteration_refused", reason=code)
        self.running = True
        self.starts.append(self.now)
        return {}

    def tick_costs(self, _grant_id):
        return []


class Authority:
    def __init__(self, world: World) -> None:
        self.world = world
        self.incarnation = "b" * 32
        self.epoch = 1

    @property
    def killed(self):
        return self.world.killed

    @property
    def unhealthy_reason(self):
        return "x" if self.world.auth_sick else None

    def report_unhealthy(self, _reason):  # pragma: no cover - no recorder here
        raise AssertionError


class Settings:
    heartbeat_start = True
    tick_min_spacing_sec = SPACING


def oracle(w: World, source: str) -> set[str]:
    """The clauses that fail now (empty: the wake starts an iteration)."""
    failing = set()
    g = w.grant if w.grant is not None and w.grant.status != "revoked" else None

    def need(ok, reason):
        if not ok:
            failing.add(reason)

    need(w.switch, "not_enabled")
    need(not w.killed, "kill")
    need(not w.auth_sick, "authority_unhealthy")
    need(not w.store_sick, "store_unhealthy")
    need(g is not None, "no_grant")
    if g is None:
        return failing
    need(g.heartbeat_start, "not_enabled")
    need(g.status == "active", "grant_state")
    need(g.goal_status != "awaiting_verification", "awaiting_verification")
    need(g.goal_status in ("active", "awaiting_verification"), "goal_state")
    need(w.breaker == "normal", "breaker")
    need(w.hb, "heartbeat_unhealthy")
    need(not w.clock_broken, "clock")
    elapsed = max(w.now - g.issued_wall, w.now - g.issued_boot, w.now - g.issued_mono)
    need(w.clock_broken or elapsed < g.ttl_sec, "ttl")
    need(w.clock_broken or elapsed >= SPACING, "min_spacing")  # since the issue
    resumed = w.resumed.get(g.grant_id)  # #644 U3: and since the last resume
    need(w.clock_broken or resumed is None or w.now - resumed >= SPACING, "min_spacing")
    need(g.iterations < g.max_iterations, "max_iterations")
    need(g.used_tokens < g.total_tokens, "budget")
    need(not w.stopping, "shutdown")
    need(not w.running, "iteration_running")
    need(w.abandoned == 0, "abandoned_call")
    need(not w.turn, "turn_busy")
    need(not w.ends or w.now - w.ends[-1][2] >= SPACING, "min_spacing")
    streak = 0
    for origin, end, _t, epoch in reversed(w.ends):
        if epoch != g.stop_epoch or end != "no_action":
            break
        streak += 1
        if origin != "tick":
            break
    need(streak < 2, "no_action")
    failures = 0
    for origin, end, _t, epoch in reversed(w.ends):
        if epoch != g.stop_epoch or end not in ("failed", "model_error", "timeout"):
            break
        failures += 1
        if origin != "tick":
            break
    need(failures < 3, "repeated_failure")
    if source == "event":
        recent = [t for t in w.starts if w.now - WINDOW < t <= w.now]
        need(len(recent) < FLOOD, "flood")
    return failing


def _heal(w: World) -> None:
    """Everything healthy again (so that starts — and so the spacing, the no-action gate
    and the flood guard — are reached often)."""
    w.switch, w.killed, w.auth_sick, w.store_sick = True, False, False, False
    w.breaker, w.hb, w.clock_broken, w.turn, w.abandoned, w.stopping = (
        "normal",
        True,
        False,
        False,
        0,
        False,
    )
    if w.grant is None or w.grant.status != "active" or w.grant.goal_status != "active":
        w.grant = G(issued_wall=w.now, issued_mono=w.now, issued_boot=w.now)
        w.ends.clear()


def _change(w: World, rnd: random.Random) -> None:
    g = w.grant
    if w.running and rnd.random() < 0.5:
        op = 10
    elif rnd.random() < 0.08:
        _heal(w)
        return
    else:
        op = rnd.randrange(22)
    if op == 0:
        w.switch = rnd.random() < 0.9
    elif op == 1:
        w.killed = rnd.random() < 0.1
    elif op == 2:
        w.auth_sick = rnd.random() < 0.1
    elif op == 3:
        w.store_sick = rnd.random() < 0.1
    elif op == 4:
        w.breaker = rnd.choice(["normal"] * 4 + ["warning", "critical"])
    elif op == 5:
        w.hb = rnd.random() < 0.8
    elif op == 6:
        w.clock_broken = rnd.random() < 0.1
    elif op in (7, 8, 9):
        w.now += rnd.choice([1, 5, 15, 29, 30, 31, 60, 120, 900])
    elif op == 10 and w.running:  # the running iteration ends
        w.running = False
        end = rnd.choice(
            ["done", "no_action", "no_action", "claimed", "yielded_user_turn", "failed", "failed",
             "model_error", "timeout"]
        )  # fmt: skip
        w.ends.append(("tick", end, w.now, g.stop_epoch if g else 0))
    elif op == 11 and g is not None and not w.running:  # a person's press runs and ends
        end = rnd.choice(["done", "no_action"])
        w.ends.append(("press", end, w.now, g.stop_epoch))
    elif op == 12 and g is not None:
        w.grant = replace(g, status=rnd.choice(["active", "active", "paused", "halted"]))
    elif op == 13 and g is not None:  # a stop + resume moves the stop epoch
        w.grant = replace(g, status="active", stop_epoch=g.stop_epoch + 1)
        w.resumed[g.grant_id] = w.now  # #644 U3: the spacing counts from it too
    elif op == 14 and g is not None:
        w.grant = replace(
            g, goal_status=rnd.choice(["active", "active", "awaiting_verification", "blocked"])
        )
    elif op == 15 and g is not None:
        w.grant = replace(g, iterations=rnd.choice([0, 3, 9, 10]))
    elif op == 16 and g is not None:
        w.grant = replace(g, used_tokens=rnd.choice([0, 500, 999, 1000]))
    elif op == 17:
        w.turn = rnd.random() < 0.2
    elif op == 18:
        w.abandoned = 1 if rnd.random() < 0.1 else 0
    elif op == 19:
        w.grant = rnd.choice([None, G(), G(heartbeat_start=False), g])
        if w.grant is not None and w.grant is not g:
            w.grant = replace(w.grant, issued_wall=w.now, issued_mono=w.now, issued_boot=w.now)
            w.ends.clear()
    elif op == 20:
        w.stopping = rnd.random() < 0.05
    elif op == 21 and g is not None:
        w.grant = replace(g, issued_wall=w.now - rnd.choice([0, 7199, 7200, 8000]))


def _run(seed: int) -> set[str]:
    rnd = random.Random(seed)
    seen: set[str] = set()
    w = World()
    on, off = Settings(), Settings()
    off.heartbeat_start = False
    ticker = GrantTicker(
        w, w, settings=on, authority=Authority(w), flood_starts=FLOOD, flood_window_sec=WINDOW
    )

    def wake(source: str) -> None:
        ticker._settings = on if w.switch else off
        failing = oracle(w, source)
        steps = w.steps
        got = ticker.fire(source=source, heartbeat_healthy=lambda: w.hb, run_token=RunToken())
        if failing:
            assert got in failing, (got, failing)
            assert w.steps == steps  # never pressed when a clause fails
        else:
            assert got == "started", got
            assert w.steps == steps + 1  # exactly one press
        assert got in grant_tick.SKIP_REASONS or got == "started"
        seen.add(got)

    for _ in range(400):
        roll = rnd.random()
        if roll < 0.03:  # a burst of event wakes, each iteration ending quickly
            _heal(w)
            for _ in range(FLOOD + 2):
                wake("event")
                w.now += rnd.choice([SPACING, SPACING + 1, 7])
                if w.running:
                    w.running = False
                    end = rnd.choice(["done", "failed"])
                    w.ends.append(("tick", end, w.now, w.grant.stop_epoch))
        elif roll < 0.6:
            _change(w, rnd)
        else:
            wake(rnd.choice(["periodic", "event"]))
    return seen


@pytest.mark.parametrize("seed", range(40))
def test_the_admission_matches_the_oracle(seed):
    _run(seed)


def test_the_sequences_reach_every_outcome():
    """The random sequences reach a start and every skip reason a wake can give (only the
    catch-all ``executor_refused`` needs a failing collaborator)."""
    seen = set().union(*(_run(seed) for seed in range(40)))
    # (the stamp's own reasons — a narrowing, a scheduler stop — and a missing executor are
    # driven through the real executor in test_grants_tick_interleavings.py)
    others = {"executor_refused", "executor_unavailable", "narrowed", "scheduler_stopped"}
    wanted = (set(grant_tick.SKIP_REASONS) - others) | {"started"}
    assert seen >= wanted, wanted - seen
    assert math.isfinite(SPACING)

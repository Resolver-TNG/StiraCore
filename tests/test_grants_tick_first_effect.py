"""W6d fix round 2 (#296): a heartbeat-started iteration's wake record is read whole right
before its first permit, its first consume, its first provider request and its first
terminal request (the client proxy's own permit step); its memory marks at every boundary.

Ported from the round-2 reviews (they failed on dcc8a161: 2 provider calls and
``note1.md`` written): Opus ``test_opus_w6dr2_probe.py`` q1–q3 and Astra
``test_astra_r2_seams.py`` / ``test_astra_r2_terminal.py``. The real ``step``, a real
``Scheduler`` and a counting provider. The accepted residual (docs/grants.md): a heartbeat
loss after the iteration's first request is not a stop (the memory marks still are)."""

from __future__ import annotations

import copy
import dataclasses
import threading

import pytest
from grants_helpers import confirm_and_resume
from test_grants_executor import _offline_bedrock
from test_grants_tick import scripted, text_only, tick_env, w
from test_grants_tick_server import T0, _scheduler

from stiracore.core.turn_lock import TurnLock

CHANGES = (
    "none",
    "heartbeat",
    "switch",
    "scheduler_stop",
    "executor_swap",
    "person_turn",
    "person_holding",
    "pause_resume",
    "kill",
    "narrowing",
    "revoke",
    "breaker",
    "ttl",
)


class World:
    """A flagged grant, its ticker on a real scheduler (heartbeat status as a dict)."""

    def __init__(self, tmp_path) -> None:
        (tmp_path / "grant").mkdir()
        self.env = scripted(tick_env(tmp_path / "grant"), w(1), text_only())
        self.heartbeat = {"enabled": True, "running": True, "last_tick_at": T0, "interval_sec": 60}
        self.scheduler, _clock = _scheduler(tmp_path / "scheduler", self.heartbeat)
        self.scheduler.attach_grant_tick(self.env.ticker)
        self.held: list = []
        self.done = False

    def apply(self, change) -> None:
        if self.done:
            return
        self.done = True
        env = self.env
        if change == "heartbeat":
            self.heartbeat["running"] = False
        elif change == "switch":
            env.ticker._settings = dataclasses.replace(env.settings, heartbeat_start=False)
        elif change == "scheduler_stop":
            self.scheduler.stop()
        elif change == "executor_swap":
            env.ticker._executor = copy.copy(env.executor)  # a new identity, the same signal
        elif change == "person_turn":
            with TurnLock(threading.Lock(), env.executor.preempt_signal):
                pass
        elif change == "person_holding":
            lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
            lock.acquire()
            env.executor._turn_busy = lock.locked
            self.held.append(lock)
        elif change == "pause_resume":
            env.service.pause(env.grant_id)
            confirm_and_resume(env.service, env.grant_id, {})
        elif change == "kill":
            env.authority.kill()
        elif change == "narrowing":
            env.authority.narrow("tool_rules_saved")
        elif change == "revoke":
            env.service.revoke(env.grant_id)
        elif change == "breaker":
            env.breaker.level = "warning"
        elif change == "ttl":
            env.clocks.tick(100_000)

    def run(self):
        try:
            self.scheduler._fire_grant_tick()  # the scheduler's own fire (its token, health)
            assert self.env.executor.wait_idle(20)
        finally:
            for lock in self.held:
                lock.release()
            self.scheduler.stop()
        env = self.env
        return env.provider.calls, (env.sub / "note1.md").exists(), env.executor.view(env.grant_id)

    def close(self):
        self.env.executor.stop(1)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _wrap(obj, name, after):
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        out = original(*args, **kwargs)
        after(*args, **kwargs)
        return out

    setattr(obj, name, wrapped)


SEAMS = ("post_admit", "consume_gate", "post_consume", "first_reply")


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("seam", SEAMS)
def test_every_change_before_the_first_request_stops_it(world, seam, change):
    """post_admit: ``_admit`` returned; consume_gate: the first consume's last stamp read
    returned (Opus q1 / Astra post_stamp); post_consume: the first consume returned, before
    the provider's stream (Opus q2 / Astra post_consume); first_reply: during the first
    model reply (Astra after_model: the next boundary)."""
    env = world.env
    if seam == "post_admit":
        _wrap(env.executor, "_tool_names", lambda *_a: world.apply(change))
    elif seam == "consume_gate":
        _wrap(
            env.executor,
            "_stamp_gate",
            lambda it, gate, *_a: gate == "consume" and world.apply(change),
        )
    elif seam == "post_consume":
        _wrap(env.authority, "consume", lambda *_a, **_k: world.apply(change))
    else:
        env.provider.on_call = lambda _n: world.apply(change)
    calls, wrote, view = world.run()
    assert world.done, "seam not reached"
    if change == "none":
        assert calls == 2 and wrote, view
        return
    if seam == "first_reply":
        if change == "heartbeat":
            # The accepted residual: after the first request, a heartbeat loss is no stop.
            assert calls == 2 and wrote
            return
        assert calls == 1 and not wrote, (calls, view)  # stopped at the next boundary
        return
    assert calls == 0 and not wrote, (calls, view)


@pytest.mark.parametrize(
    "change", ["none", "heartbeat", "switch", "scheduler_stop", "executor_swap", "narrowing",
               "pause_resume", "breaker"]
)  # fmt: skip
def test_the_first_terminal_request_revalidates_the_wake(world, change):
    """The real Bedrock client proxy: a change in its own permit step (after the
    provider-level checks) — the request never leaves (Astra terminal probe)."""
    env = world.env
    model = _offline_bedrock()
    env.executor._inner_model = lambda _p: model
    sent = []

    def request(**_kw):
        sent.append(1)
        raise RuntimeError("offline request recorded")

    model.client.converse_stream = request
    env.executor._probe = lambda point, _it: point == "request:authorized" and world.apply(change)
    world.run()
    assert world.done
    assert sent == ([1] if change == "none" else []), change


@pytest.mark.parametrize("change", ["heartbeat", "switch", "scheduler_stop", "executor_swap"])
def test_no_permit_is_taken_on_a_stale_wake(world, change):
    """Right before the first permit: a change during the first permit's own look."""
    env = world.env
    issued = []
    original_issue = env.authority.issue_permit

    def issue(*a, **k):
        issued.append(1)
        return original_issue(*a, **k)

    env.authority.issue_permit = issue
    original_look = env.executor._look

    def look(it):
        out = original_look(it)
        if not issued:
            world.apply(change)
        return out

    env.executor._look = look
    calls, wrote, _view = world.run()
    assert issued == [] and calls == 0 and not wrote


@pytest.mark.parametrize("change", ["heartbeat", "switch", "scheduler_stop", "executor_swap"])
def test_no_first_consume_on_a_stale_wake(world, change):
    """Right before the first consume: a change during the consume's own look."""
    env = world.env
    consumed = []
    original_consume = env.authority.consume

    def consume(*a, **k):
        consumed.append(1)
        return original_consume(*a, **k)

    env.authority.consume = consume
    original_look = env.executor._look
    looks = []

    def look(it):
        out = original_look(it)
        looks.append(1)
        if len(looks) == 3:  # the reservation's look, the first permit's, the first consume's
            world.apply(change)
        return out

    env.executor._look = look
    calls, wrote, _view = world.run()
    assert consumed == [] and calls == 0 and not wrote


def test_a_stamp_is_single_use(world):
    """Opus R2-N2: the publication marks the stamp used; a replay is refused."""
    env = world.env
    stamps = []
    original = env.ticker._press

    def press(stamp):
        stamps.append(stamp)
        return original(stamp)

    env.ticker._press = press
    world.run()
    assert env.provider.calls == 2
    env.mono[0] += 10_000
    from stiracore.grants.schema import GrantRefused

    with pytest.raises(GrantRefused) as refused:
        env.executor.step(env.grant_id, tick=stamps[0])
    assert refused.value.extra.get("reason") == "narrowed"
    assert env.provider.calls == 2


def test_a_stamp_for_another_executor_is_refused(world):
    """A wake taken for another executor (everything else valid) is refused by ``step``."""
    env = world.env
    captured = []
    env.ticker._press = lambda stamp: captured.append(stamp) or "captured"
    world.run()
    assert captured
    stamp = captured[0]
    other = copy.copy(env.executor)
    env.ticker._executor = other  # the stamp's own marks all still hold for ``other``
    stamp.executor = other
    stamp._token.valid = True
    from stiracore.grants.schema import GrantRefused

    with pytest.raises(GrantRefused) as refused:
        env.executor.step(env.grant_id, tick=stamp)
    assert refused.value.extra.get("reason") == "narrowed"
    assert env.executor.wait_idle(5) and env.provider.calls == 0

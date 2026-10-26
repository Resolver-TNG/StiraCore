"""W6d fix round 1 (#296): the wake's stamp fences a heartbeat start through its first
effect — the real ``GoalIterationExecutor.step`` driven through every window between the
wake and the first model call. **No model call and no write after a stop, ever.**

Ported from Astra's review repros (PR #637 @7a906284; they failed there: 2 provider calls
and ``note1.md`` written) and extended to a seam × change matrix (Astra N1)."""

from __future__ import annotations

import dataclasses
import threading

import pytest
from grants_helpers import confirm_and_resume
from test_grants_tick import fire, scripted, text_only, tick_env, w

from stiracore.core.turn_lock import TurnLock


def _no_effect(env, code):
    assert env.executor.wait_idle(20)
    assert env.provider.calls == 0, (code, env.provider.calls)
    assert not (env.sub / "note1.md").exists(), code
    ended = env.executor.view(env.grant_id).get("end")
    assert code != "started" or ended in ("permit_stale", "yielded_user_turn", "stopped"), ended


# --- Astra's repros ------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["heartbeat", "switch", "pause_resume"])
def test_a_late_admission_change_starts_nothing(tmp_path, change):
    """After the tick's recheck returned, before ``_admit`` returns."""
    env = scripted(tick_env(tmp_path), w(1), text_only())
    original = env.executor._tool_names

    def tool_names(grant):
        names = original(grant)
        if change == "heartbeat":
            env.healthy.value = False
        elif change == "switch":
            env.ticker._settings = dataclasses.replace(env.settings, heartbeat_start=False)
        else:
            env.service.pause(env.grant_id)
            confirm_and_resume(env.service, env.grant_id, {})
        return names

    env.executor._tool_names = tool_names
    code = fire(env)
    assert code in {"heartbeat": ("heartbeat_unhealthy",), "switch": ("not_enabled",),
                    "pause_resume": ("narrowed",)}[change], code  # fmt: skip
    _no_effect(env, code)


def test_a_scheduler_stopped_after_its_dispatch_check_starts_nothing(tmp_path):
    from test_grants_tick_server import T0, _scheduler

    (tmp_path / "grant").mkdir()
    env = scripted(tick_env(tmp_path / "grant"), w(1), text_only())
    heartbeat = {"enabled": True, "running": True, "last_tick_at": T0, "interval_sec": 60}
    scheduler, _clock = _scheduler(tmp_path / "scheduler", heartbeat)
    scheduler.attach_grant_tick(env.ticker)
    original = env.service.tick_record

    def stop_after_check():
        record = original()
        scheduler.stop()
        return record

    env.service.tick_record = stop_after_check
    try:
        scheduler._fire_grant_tick()
        assert env.ticker.view(env.grant_id)["last"] == "scheduler_stopped"
        _no_effect(env, "scheduler_stopped")
    finally:
        scheduler.stop()


def test_concurrent_ticks_start_one_iteration(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    barrier = threading.Barrier(2)
    original = env.ticker._press

    def press(*args):
        barrier.wait(timeout=5)
        return original(*args)

    env.ticker._press = press
    results = []
    workers = [threading.Thread(target=lambda: results.append(fire(env))) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert env.executor.wait_idle(20)
    assert results.count("started") == 1, results
    assert env.provider.calls == 2


def test_a_person_turn_between_the_wake_and_step_voids_the_wake(tmp_path):
    """A complete turn (acquire and release) after the wake, before ``step``: ``step``
    must not take the new counter as its own."""
    env = scripted(tick_env(tmp_path), w(1), text_only())
    original = env.ticker._press

    def delayed_press(*args):
        lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
        with lock:
            pass
        return original(*args)

    env.ticker._press = delayed_press
    code = fire(env)
    assert code == "turn_busy", code
    _no_effect(env, code)


# --- the seam × change matrix through the real step ----------------------------------------------

SEAMS = ("tick_record", "post_recheck", "publication", "pre_authorize", "first_consume")
CHANGES = (
    "none",
    "heartbeat",
    "switch",
    "pause_resume",
    "kill",
    "narrowing",
    "person_turn",
    "scheduler_stop",
)


def _apply(env, change):
    if change == "heartbeat":
        env.healthy.value = False
    elif change == "switch":
        env.ticker._settings = dataclasses.replace(env.settings, heartbeat_start=False)
    elif change == "pause_resume":
        env.service.pause(env.grant_id)
        confirm_and_resume(env.service, env.grant_id, {})
    elif change == "kill":
        env.authority.kill()
    elif change == "narrowing":
        env.authority.narrow("tool_rules_saved")  # any authority narrowing since the wake
    elif change == "person_turn":
        lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
        with lock:
            pass
    elif change == "scheduler_stop":
        env.token.valid = False


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("seam", SEAMS)
def test_every_change_in_every_window_stops_before_any_effect(tmp_path, monkeypatch, seam, change):
    holder = {}

    def probe(point, _it):
        if seam == "first_consume" and point == "model:authorized" and not holder.get("done"):
            holder["done"] = True
            _apply(holder["env"], change)

    env = scripted(tick_env(tmp_path, probe=probe), w(1), text_only())
    holder["env"] = env
    once = {"done": False}

    def at_seam():
        if not once["done"]:
            once["done"] = True
            _apply(env, change)

    if seam == "tick_record":
        original_record = env.service.tick_record

        def record():
            out = original_record()
            at_seam()
            return out

        env.service.tick_record = record
    elif seam == "post_recheck":
        original_names = env.executor._tool_names

        def names(grant):
            out = original_names(grant)
            at_seam()
            return out

        env.executor._tool_names = names
    elif seam == "pre_authorize":
        # On the iteration's own thread, after the publication, before its first permit.
        original_child = env.executor._make_child

        def make_child(it):
            at_seam()
            return original_child(it)

        env.executor._make_child = make_child
    elif seam == "publication":
        # The instant after step's last unfenced read (its second ``fresh``: the first is
        # the admission's recheck) and before the fenced publication step.
        from stiracore.scheduler.grant_tick import WakeStamp

        original_fresh = WakeStamp.fresh
        calls = {"n": 0}

        def fresh(self):
            out = original_fresh(self)
            calls["n"] += 1
            if calls["n"] == 2:
                at_seam()
            return out

        monkeypatch.setattr(WakeStamp, "fresh", fresh)
    code = fire(env)
    if change == "none":
        assert code == "started", code
        assert env.executor.wait_idle(20)
        assert env.provider.calls >= 1 and (env.sub / "note1.md").exists()
        return
    _no_effect(env, code)
    # The heartbeat is no fenced authority: changed after step's last read it is caught
    # before the first effect (the iteration then ends permit_stale, no call, no write).
    late = seam in ("pre_authorize", "first_consume") or (
        seam == "publication" and change == "heartbeat"
    )
    if not late:
        assert code != "started", code  # refused before the publication: a skip
    else:
        view = env.executor.view(env.grant_id)
        assert view["end"] in ("permit_stale", "yielded_user_turn", "stopped"), view

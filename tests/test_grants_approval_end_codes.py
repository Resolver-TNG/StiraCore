"""#704: the end code of an approval nobody answered is ``approval_timeout`` — also when the
iteration's wall time arrives while the card waits, and when no approver is connected (the
iteration stops at once instead of spending its budget on retries). Offline, scripted; the
wall time is the executor's injected clock and the card's arrival is an event."""

from __future__ import annotations

import asyncio
import threading

import pytest
from grants_exec_helpers import Broker, grant, make_env, run_one, use


def _edit(env, n=1, name="notes.md"):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": "x\n"}, n)


class _ShownBroker(Broker):
    """An unanswered card; ``shown`` is set once the card is with the broker."""

    def __init__(self) -> None:
        super().__init__(answer=None)
        self.shown = threading.Event()

    def request(self, request, **kwargs):
        self.shown.set()
        return super().request(request, **kwargs)


@pytest.mark.parametrize("via", ["watchdog", "iteration_thread"])
def test_the_wall_time_while_a_card_waits_ends_approval_timeout(tmp_path, via):
    broker = _ShownBroker()
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    env.executor.step(env.grant_id)
    assert broker.shown.wait(10)
    it = env.executor._current
    stops: list[tuple[str, str, bool]] = []  # (the caller, the code asked for, was it first)
    test_thread = threading.current_thread()
    real_stop, real_stop_wall = it.stop, it.stop_wall

    def stop(code, detail=""):
        first = real_stop(code, detail)
        mine = threading.current_thread() is test_thread
        stops.append(("poll" if mine else "other", code, first))
        return first

    def stop_wall():
        # The watchdog case: the iteration thread's own deadline handling is switched off
        # (before the clock moves), so only ``poll()`` can end this iteration.
        if via == "watchdog" and threading.current_thread() is not test_thread:
            return False
        return real_stop_wall()

    it.stop, it.stop_wall = stop, stop_wall
    env.mono[0] += 601  # the iteration's wall time passes while nobody answers
    if via == "watchdog":
        env.executor.poll()
        # ``poll()`` itself set the code, synchronously, and it was the first stop.
        assert stops[0] == ("poll", "approval_timeout", True), stops
        assert it.stop_code == "approval_timeout"
    else:
        it.wake.set()  # the iteration thread reads the clock again
    assert env.executor.wait_idle(10)
    view = env.executor.view(env.grant_id)
    assert view["end"] == "approval_timeout", view
    assert stops[0][1:] == ("approval_timeout", True), stops
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "approval_timeout")
    assert g.status == "paused"
    assert broker.withdrawn and not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1  # nothing ran after the stop


def test_no_approver_connected_ends_approval_timeout_at_the_first_ask(tmp_path):
    env = make_env(tmp_path, rules={"editor": "ask"})
    env.executor._broker = None
    # The model would retry the refused write again and again.
    env.provider.script = [[_edit(env, n)] for n in range(1, 6)] + [[{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "approval_timeout", view
    assert view["card"] == "none" and view["writes"] == 0 and view["waits"] == 0
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "approval_timeout")
    assert g.status == "paused"
    assert not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1  # no retry spends the budget
    assert g.used_tokens == 10  # the one model call


def test_a_cancelled_error_in_the_call_thread_still_ends_with_a_failure_code(tmp_path):
    env = make_env(tmp_path)

    def cancelled(n):
        raise asyncio.CancelledError

    env.provider.on_call = cancelled
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("failed", "child_error"), view
    assert not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_or_exit_in_the_call_thread_ends_failed_child_error(tmp_path, error):
    env = make_env(tmp_path)

    def interrupted(n):
        raise error

    env.provider.on_call = interrupted
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("failed", "child_error"), view
    assert not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1


@pytest.mark.parametrize(
    ("code", "detail"),
    [("approval_timeout", ""), ("stopped", "in_flight_cancelled")],
)
def test_a_later_cancelled_error_does_not_replace_the_first_stop(tmp_path, code, detail):
    env = make_env(tmp_path)

    def stopped_then_cancelled(n):
        # The stop arrives first (the same thread, so the order is fixed) ...
        assert env.executor._current.stop(code, detail) is True
        raise asyncio.CancelledError  # ... and the call thread then ends on the cancel.

    env.provider.on_call = stopped_then_cancelled
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert (view["end"], view["detail"]) == (code, detail), view
    assert not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1


def test_an_iteration_stop_in_the_call_thread_is_not_a_child_error(tmp_path):
    from stiracore.grants.executor import IterationStop

    env = make_env(tmp_path)

    def refused(n):
        raise IterationStop("budget_refused")

    env.provider.on_call = refused
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    # As before #704: no stop code from the call thread; the end is the plain one of an
    # iteration with no tool call and no report.
    assert (view["end"], view["detail"]) == ("no_action", ""), view
    assert not (env.sub / "notes.md").exists()
    assert env.provider.calls == 1

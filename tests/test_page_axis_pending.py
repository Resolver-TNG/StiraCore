"""#778 (before S5): A1 while G has no verdict yet, G's re-measurement after a transient
failure, and what A7 measures.

``guard_pending`` is not sticky: the gate measures again as soon as it reads it
(``page_look._REMEASURED``). ``browser_guard_unverified`` is G's final answer. Scripted
runners, an injected clock, the real loopback canary; no browser starts.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from types import SimpleNamespace

from stiracore.identity_paths import WritePathPolicy
from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg
from stiracore.tools import page_look as pl
from stiracore.yolo import origin
from tests.test_page_check_axis import GOOD, _failed, world  # noqa: F401 - a fixture
from tests.test_page_look_always import _config

FAILED = {"status": "error", "reason": "browser_exited"}
CALL = {"target": "index.html"}


def _guard(clock, runner):
    identity = pg.BrowserIdentity(
        stat=lambda path: SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=3),
        version=lambda path: "Chromium 1.0",
    )
    return pg.Guard(
        clock=clock,
        platform="darwin",
        runner=runner,
        identity=identity,
        find_browser=lambda: "/x/chrome",
    )


def _scripted(results):
    """A runner that plays ``results`` in order (the last one again after that)."""
    runs = []

    def runner(browser, port):
        runs.append(browser)
        return dict(results[min(len(runs), len(results)) - 1])

    return runner, runs


# --- M2: G still runs -------------------------------------------------------------------------


def test_a_running_g_is_guard_pending_and_its_pass_is_read_at_once(world, monkeypatch):  # noqa: F811
    """A slow G: the full check and the next calls' light checks say ``guard_pending``;
    when G ended, the very next card preparation measures again and the saved Always is
    effective — the clock has not moved (nothing waits for ``AXIS_MAX_AGE_S``)."""
    monkeypatch.setattr(pg, "GUARD_WAIT_S", 0.05)
    release = threading.Event()

    def slow(browser, port):
        release.wait(10)
        return dict(GOOD)

    guard = _guard(world.clock, slow)
    axis = pg.Axis(clock=world.clock, guard=guard)
    monkeypatch.setattr(pc, "axis", axis)
    monkeypatch.setitem(pl._AXIS, "scope", world.scope)
    pl.reset_counts()
    config = _config()
    try:
        first = axis.check(world.scope(), full=True)
        assert first.code == "guard_pending" and _failed(first)[0] == "A1"
        assert guard.pending is True and guard.state().code == "guard_pending"
        # The light check of a call made meanwhile does not turn it into a final code.
        light = axis.check(world.scope())
        assert (light.code, _failed(light)) == ("guard_pending", ["A2"])
        assert axis.current().code == "guard_pending"
        with origin.human_turn("someone"):
            capped = pl.prepare_input(dict(CALL), lambda: config)
        assert capped["always"] == "capped:guard_pending"  # still a person's card
    finally:
        release.set()
    monkeypatch.setattr(pg, "GUARD_WAIT_S", 10.0)  # ``wait`` is capped by it too
    assert guard.wait(5.0) is True
    assert axis.current().code == "guard_pending"  # nothing measured since
    with origin.human_turn("someone"):
        assert pl.prepare_input(dict(CALL), lambda: config) == CALL
    assert axis.current().code == "pass" and guard.pending is False
    assert world.clock.now == 1000.0


def test_guard_pending_is_remeasured_and_the_final_code_is_not():
    assert "guard_pending" in pl._REMEASURED and "guard_pending" in pl.CAP_CODES
    assert "browser_guard_unverified" not in pl._REMEASURED
    assert "guard_pending" in pg.AXIS_CODES and "guard_pending" in pg.GUARD_CODES


def test_a_changed_browser_is_pending_while_g_runs_again(world):  # noqa: F811
    assert world.axis.check(world.scope(), full=True).code == "pass"
    changed = world.axis.check(world.scope(browser="/y/chrome"))
    assert (changed.code, _failed(changed)) == ("guard_pending", ["A2"])
    assert world.guard.wait(5.0) is True
    assert world.axis.check(world.scope(browser="/y/chrome"), full=True).code == "pass"


# --- M2: a transient failure is measured again, a final one is not --------------------------


def test_a_transient_g_failure_is_measured_again_after_the_backoff(world):  # noqa: F811
    runner, runs = _scripted([FAILED, GOOD])
    guard = _guard(world.clock, runner)
    axis = pg.Axis(clock=world.clock, guard=guard)
    first = axis.check(world.scope(), full=True)
    assert first.code == "guard_pending" and _failed(first) == ["A1"]
    assert (guard.state().state, guard.state().code) == ("unverified", "guard_run_failed")
    # Before the backoff has passed nothing starts, however often it is asked.
    world.clock.now += pg.GUARD_RETRY_AFTER_S[0] - 0.5
    assert axis.check(world.scope(), full=True).code == "guard_pending"
    assert guard.start() is False and len(runs) == 1
    world.clock.now += 0.5
    assert axis.check(world.scope(), full=True).code == "pass"
    assert len(runs) == 2 and guard.pending is False


def test_transient_failures_end_as_the_final_code_after_the_last_retry(world):  # noqa: F811
    runner, runs = _scripted([FAILED])
    guard = _guard(world.clock, runner)
    axis = pg.Axis(clock=world.clock, guard=guard)
    for delay in pg.GUARD_RETRY_AFTER_S:
        assert axis.check(world.scope(), full=True).code == "guard_pending"
        world.clock.now += delay
    final = axis.check(world.scope(), full=True)
    assert final.code == "browser_guard_unverified" and _failed(final) == ["A1"]
    assert len(runs) == 1 + len(pg.GUARD_RETRY_AFTER_S) and guard.pending is False
    world.clock.now += 10 * pg.AXIS_MAX_AGE_S  # no later check starts another run
    assert axis.check(world.scope()).code == "browser_guard_unverified"
    assert guard.start() is False and len(runs) == 1 + len(pg.GUARD_RETRY_AFTER_S)


def test_a_canary_that_heard_something_is_final(world):  # noqa: F811
    runs = []

    def leaking(browser, port):
        runs.append(browser)
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        time.sleep(0.3)
        return dict(FAILED)  # the run failed too: what the canary heard decides

    guard = _guard(world.clock, leaking)
    axis = pg.Axis(clock=world.clock, guard=guard)
    heard = axis.check(world.scope(), full=True)
    assert (heard.code, guard.state().code) == ("browser_guard_unverified", "canary_heard")
    assert guard.pending is False
    world.clock.now += 10 * pg.AXIS_MAX_AGE_S
    assert axis.check(world.scope(), full=True).code == "browser_guard_unverified"
    assert guard.start() is False and len(runs) == 1


def test_a_probe_that_raises_after_the_canary_was_reached_is_final(world):  # noqa: F811
    """#778 review F1: the probe dies AFTER the page reached the canary. The hits are read
    before the failure is classified, so a run that leaked can never be re-measured into a
    pass (``guard_run_failed`` is the only transient code)."""
    runs = []

    def leaking_then_crashing(browser, port):
        runs.append(browser)
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        time.sleep(0.3)
        raise AttributeError("the probe crashed after the page touched the canary")

    guard = _guard(world.clock, leaking_then_crashing)
    axis = pg.Axis(clock=world.clock, guard=guard)
    heard = axis.check(world.scope(), full=True)
    assert (heard.code, guard.state().code) == ("browser_guard_unverified", "canary_heard")
    assert guard.pending is False
    for backoff in pg.GUARD_RETRY_AFTER_S:
        world.clock.now += backoff + 1
        assert guard.start() is False  # a final failure is never measured again
    assert len(runs) == 1


def test_a_probe_that_raises_without_touching_the_canary_is_still_transient(world):  # noqa: F811
    runs = []

    def crashing(browser, port):
        runs.append(browser)
        raise AttributeError("the probe crashed")

    guard = _guard(world.clock, crashing)
    pg.Axis(clock=world.clock, guard=guard).check(world.scope(), full=True)
    assert guard.state().code == "guard_run_failed" or guard.state().code == "guard_pending"


def test_a_dropped_state_and_a_broken_clock_are_never_pending(world):  # noqa: F811
    runner, runs = _scripted([FAILED])
    guard = _guard(world.clock, runner)
    assert guard.measure().code == "guard_run_failed" and guard.pending is True
    guard.note_call("browser_guard_unverified")  # A4
    world.clock.now += 1000
    assert guard.pending is False and guard.start() is False and len(runs) == 1
    result = pg.Axis(clock=world.clock, guard=guard).check(world.scope(), full=True)
    assert result.code == "browser_guard_unverified"

    def no_clock():
        raise RuntimeError("no clock")

    blind = _guard(no_clock, runner)
    assert blind.measure().code == "guard_run_failed"  # no reading: no backoff to wait for
    assert blind.pending is False and blind.start() is False


# --- A7: what the roots are on disk ---------------------------------------------------------


def test_a_replaced_workspace_root_is_write_floor_changed(world):  # noqa: F811
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert world.axis.check(world.scope()).code == "pass"
    world.ws.rename(world.ws.with_name("ws-before"))
    world.ws.mkdir()  # the same path and the same policy object: another directory
    changed = world.axis.check(world.scope())
    assert (changed.code, _failed(changed)) == ("write_floor_changed", ["A7"])
    world.ws.rmdir()
    world.ws.with_name("ws-before").rename(world.ws)  # put back: sticky until restart
    assert world.axis.check(world.scope(), full=True).code == "write_floor_changed"


def test_a_protected_directory_behind_a_link_is_write_floor_changed(world, tmp_path):  # noqa: F811
    keep = world.ws / "keep"
    keep.mkdir()
    policy = WritePathPolicy(base_dir=world.ws, write_roots=[world.ws], protected_dirs=[keep])
    axis = pg.Axis(clock=world.clock, guard=world.guard)
    assert axis.check(world.scope(policy=policy), full=True).code == "pass"
    before = pg._floor_digest(policy)
    (keep / "note.txt").write_text("x", encoding="utf-8")  # a file inside changes nothing
    assert pg._floor_digest(policy) == before
    (keep / "note.txt").unlink()
    keep.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, keep)
    changed = axis.check(world.scope(policy=policy))
    assert (changed.code, _failed(changed)) == ("write_floor_changed", ["A7"])
    assert str(tmp_path) not in repr(changed)

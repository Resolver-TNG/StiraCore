"""W6-H (#296, #631, #632): hardening before unattended iterations.

* #632-2 the commit is one step: the grant's version stamp (``generation``) read at the
  validation is compared inside the service's count, so a person's block that lands
  between the validation and the commit is an interruption (no history, no streak);
* #632-1 an oversize tracked file is identified by its size and a bounded sample of its
  bytes (never its mtime / inode): a ``touch`` is no progress, a real change is;
* #632-3 a file that enters the tracked set from an unknown / interrupted iteration does
  not move the identity unless its bytes changed;
* #632-4 an unknown iteration keeps the validated stat cache entries.

Offline: scripted provider, fake run_checks results, no sleeps (a person's call between
two steps runs on a thread that is joined before the iteration continues).
"""

from __future__ import annotations

import os
import threading

import pytest
from grants_exec_helpers import grant, make_env, run_one, use
from grants_helpers import confirm_and_resume
from test_grants_progress_exec import _create, _replace, _report

from stiracore.grants import progress as pg


def _iterate(env, *steps):
    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


def _person(call):
    """A person's call on another thread, finished before the caller goes on (a fixed
    interleaving, no sleep)."""
    errors = []

    def run():
        try:
            call()
        except Exception as error:  # noqa: BLE001 - re-raised below
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and not errors, errors


def _block_after_validation(env, monkeypatch):
    """The person's block lands right after the commit's validation read the grant."""
    real = env.executor._own_state
    landed = []

    def validate(it, end):
        stamp = real(it, end)
        if stamp is not None:
            _person(lambda: env.service.block_goal(env.grant_id, "needs_decision"))
            landed.append(stamp)
        return stamp

    monkeypatch.setattr(env.executor, "_own_state", validate)
    return landed


def _end_as(env, monkeypatch, end):
    if end == "timeout":
        observe = env.executor._observe

        def timed_out(it):
            result = observe(it)
            it.stop_code = "timeout"
            return result

        monkeypatch.setattr(env.executor, "_observe", timed_out)
    if end == "reported_blocked":
        return _report("needs_decision", "blocked", 2)
    if end == "claimed":
        return _report("goal_met", "claim_complete", 2)
    return _report("investigating", n=2)


# --- #632-2: validation and count are one step ----------------------------------------------


@pytest.mark.parametrize("end", ["done", "reported_blocked", "timeout", "claimed"])
def test_a_persons_block_between_validation_and_commit_is_an_interruption(
    tmp_path, monkeypatch, end
):
    env = make_env(tmp_path, max_iterations=20)
    report = _end_as(env, monkeypatch, end)
    landed = _block_after_validation(env, monkeypatch)
    _iterate(env, _create(env, "a.txt", "x"), report)
    tracker = env.executor._tracker(env.grant_id)
    g = grant(env)
    assert landed  # the race happened
    assert not tracker.ring and tracker.blocker_streak == 0  # no history, no streak
    assert (g.goal_status, g.goal_reason) == ("blocked", "needs_decision")  # the person's
    assert g.iterations == 1 and g.no_progress == 0  # the effect counts toward the cap only


@pytest.mark.parametrize("end", ["done", "reported_blocked", "timeout", "claimed"])
def test_without_the_race_the_same_end_is_committed(tmp_path, monkeypatch, end):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.txt", "x"), _end_as(env, monkeypatch, end))
    tracker = env.executor._tracker(env.grant_id)
    assert len(tracker.ring) == 1
    assert tracker.blocker_streak == (1 if end == "reported_blocked" else 0)
    goal = {"done": "active", "claimed": "awaiting_verification"}.get(end, "blocked")
    assert grant(env).goal_status == goal


def test_the_race_does_not_make_the_next_blocker_premature(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    real = env.executor._own_state
    _block_after_validation(env, monkeypatch)
    _iterate(env, _create(env, "a.txt", "x"), _report("needs_decision", "blocked", 2))
    monkeypatch.setattr(env.executor, "_own_state", real)
    for _ in range(2):  # two legitimate, uninterrupted known reports
        confirm_and_resume(env.service, env.grant_id, {})
        _iterate(env, _report("needs_decision", "blocked"))
    tracker = env.executor._tracker(env.grant_id)
    assert grant(env).goal_reason == "needs_decision" and tracker.blocker_streak == 2
    confirm_and_resume(env.service, env.grant_id, {})
    _iterate(env, _report("needs_decision", "blocked"))  # the third: now same_blocker
    assert grant(env).goal_reason == "same_blocker"


def test_an_unknown_outcome_raced_by_a_block_adds_no_streak(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    monkeypatch.setattr(pg, "file_digest", lambda path: None)
    _block_after_validation(env, monkeypatch)
    _iterate(env, _create(env, "a.txt", "x"), _report("investigating", n=2))
    g = grant(env)
    assert (g.goal_reason, g.iterations, g.no_progress) == ("needs_decision", 1, 0)


def test_a_detector_block_does_not_land_on_a_goal_a_person_changed(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.md", "v1\n"))
    _iterate(env, _replace(env, "a.md", "v1", "v2"))
    tracker = env.executor._tracker(env.grant_id)
    real = tracker.record

    def record(*args, **kwargs):  # the person pauses and resumes right after the count
        code, rule = real(*args, **kwargs)
        _person(lambda: env.service.pause(env.grant_id))
        _person(lambda: confirm_and_resume(env.service, env.grant_id, {}))
        return code, rule

    monkeypatch.setattr(tracker, "record", record)
    _iterate(env, _replace(env, "a.md", "v2", "v1"))  # A-B-A: loop_detected
    assert len(tracker.ring) == 3  # the iteration itself was committed
    assert grant(env).goal_status == "active"  # the person's resume stands


# --- #632-1: an oversize file is identified by its bytes (sampled), not its mtime -------------


def _grow(path, size):
    with path.open("ab") as f:
        f.truncate(size)


def test_touching_an_oversize_tracked_file_is_not_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "large.bin", "seed"))
    big = env.sub / "large.bin"
    _grow(big, pg.DIGEST_MAX_BYTES + 1)
    _iterate(env, _report("investigating"))  # the size change is observed: progress
    assert grant(env).no_progress == 0
    states = []
    for n in range(3):
        os.utime(big, ns=(10**18 + n * 10**9, 10**18 + n * 10**9))  # bytes never change
        _iterate(env, _report("investigating"))
        g = grant(env)
        states.append((g.no_progress, g.goal_status))
    assert states == [(1, "active"), (2, "active"), (3, "blocked")], states
    assert grant(env).goal_reason == "no_progress"


def test_a_copy_with_a_new_inode_and_the_same_bytes_is_not_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "large.bin", "seed"))
    big = env.sub / "large.bin"
    _grow(big, pg.DIGEST_MAX_BYTES + 1)
    _iterate(env, _report("investigating"))
    copy = env.sub / "copy.tmp"
    copy.write_bytes(big.read_bytes())
    os.replace(copy, big)  # a backup tool's rewrite: another inode, same bytes
    _iterate(env, _report("investigating"))
    assert grant(env).no_progress == 1


@pytest.mark.parametrize("where", ["head", "middle", "tail", "size"])
def test_a_real_change_of_an_oversize_tracked_file_is_progress(tmp_path, where):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "large.bin", "seed"))
    big = env.sub / "large.bin"
    size = pg.DIGEST_MAX_BYTES + 1
    _grow(big, size)
    _iterate(env, _report("investigating"))
    _iterate(env, _report("investigating"))
    assert grant(env).no_progress == 1  # unchanged: no progress
    if where == "size":
        _grow(big, size + 1)
    else:
        offset = {"head": 10, "middle": size // 2, "tail": size - 10}[where]
        with big.open("r+b") as f:
            f.seek(offset)
            f.write(b"!")
    _iterate(env, _report("investigating"))
    assert grant(env).no_progress == 0 and grant(env).goal_status == "active"


def test_an_oversize_identity_is_a_bounded_sample(tmp_path):
    big = tmp_path / "big"
    size = pg.DIGEST_MAX_BYTES + 3 * pg.SAMPLE_BYTES
    _grow(big, size)
    budget = pg.ReadBudget()
    first = pg.file_identity(str(big), budget)
    assert first.startswith(b"big:") and len(first) != 32  # never equal to a content digest
    assert budget.left == pg.DIGEST_BUDGET - 3 * pg.SAMPLE_BYTES  # charged: the sample only
    os.utime(big, ns=(10**18, 10**18))
    assert pg.file_identity(str(big), pg.ReadBudget()) == first  # a touch: the same
    with big.open("r+b") as f:  # known limit: a change outside the sampled regions
        f.seek(pg.SAMPLE_BYTES + 5)
        f.write(b"!")
    assert pg.file_identity(str(big), pg.ReadBudget()) == first
    assert pg.file_identity(str(big), pg.ReadBudget(3 * pg.SAMPLE_BYTES - 1)) is None
    digests, cache = pg.read_files([str(big)], {})
    assert digests[str(big)] == first and not cache  # never cached


# --- #632-3: a file adopted without being observed does not move the identity ---------------


def _view(env, name="a.txt"):
    return use("editor", {"command": "view", "path": str(env.sub / name)}, 77)


def _untracked(env, n=4):
    for i in range(n):  # existing files the grant never wrote
        (env.sub / f"u{i}.txt").write_text(f"u{i}")


def _turn_after_write(env, monkeypatch):
    real = env.executor._wrote

    def wrote(it, target):
        real(it, target)
        env.executor.preempt_signal.arrive()  # a person's turn right after the write

    monkeypatch.setattr(env.executor, "_wrote", wrote)
    return real


def test_a_same_bytes_rewrite_lost_to_a_turn_then_reading_is_no_progress(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    _untracked(env)
    _iterate(env, _create(env, "a.txt", "x"))
    seen = []
    for i in range(3):
        real = _turn_after_write(env, monkeypatch)
        _iterate(env, _replace(env, f"u{i}.txt", f"u{i}", f"u{i}"), _report("investigating", n=3))
        monkeypatch.setattr(env.executor, "_wrote", real)
        _iterate(env, _view(env))
        seen.append(grant(env).no_progress)
    assert seen == [1, 2, 3] and grant(env).goal_reason == "no_progress", seen


def test_a_real_change_lost_to_a_turn_is_observed_at_the_next_end(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    _untracked(env)
    _iterate(env, _create(env, "a.txt", "x"))
    _iterate(env, _view(env))
    assert grant(env).no_progress == 1
    real = _turn_after_write(env, monkeypatch)
    _iterate(env, _replace(env, "u0.txt", "u0", "changed"), _report("investigating", n=3))
    monkeypatch.setattr(env.executor, "_wrote", real)
    assert grant(env).no_progress == 1  # the interruption: no streak change
    _iterate(env, _view(env))
    assert grant(env).no_progress == 0  # its change is seen late: progress


def test_a_persons_edit_before_a_lost_rewrite_of_a_tracked_file_is_still_seen(
    tmp_path, monkeypatch
):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.txt", "x"))
    _iterate(env, _view(env))
    assert grant(env).no_progress == 1
    (env.sub / "a.txt").write_text("person")  # a person's edit of a tracked file
    real = _turn_after_write(env, monkeypatch)
    _iterate(env, _replace(env, "a.txt", "person", "person"), _report("investigating", n=3))
    monkeypatch.setattr(env.executor, "_wrote", real)
    _iterate(env, _view(env))
    assert grant(env).no_progress == 0  # the person's change, against the last known state


def test_a_file_unreadable_before_an_unknown_write_is_not_tracked(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    _untracked(env)
    _iterate(env, _create(env, "a.txt", "x"))
    real = pg.file_digest
    off = {"on": True}
    monkeypatch.setattr(pg, "file_digest", lambda path: None if off["on"] else real(path))
    _iterate(env, _replace(env, "u0.txt", "u0", "u0"))  # its bytes before: unreadable
    off["on"] = False
    assert grant(env).no_progress == 1  # unknown
    _iterate(env, _view(env))
    assert grant(env).no_progress == 2  # the untracked file did not move the identity


def test_a_same_bytes_rewrite_in_an_unknown_iteration_then_reading_is_no_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _untracked(env)
    _iterate(env, _create(env, "a.txt", "x"))
    state = {"j": 0}

    def runner(arguments, run_policy, host=None):  # > 64 distinct rows: unknown
        state["j"] += 1
        rows = [
            {"kind": "pytest", "status": "failed", "reason": None, "exit_code": 1,
             "counts": {"passed": 16 * state["j"] + k, "failed": 1}}
            for k in range(16)
        ]  # fmt: skip
        return {"status": "failed", "reason": None, "checks": rows}

    env.executor._run_checks_runner = runner
    calls = [use("run_checks", {"path": str(env.sub)}, 100 + j) for j in range(5)]
    seen = []
    for i in range(2):
        _iterate(env, _replace(env, f"u{i}.txt", f"u{i}", f"u{i}"), *calls)
        seen.append(grant(env).no_progress)
        if grant(env).goal_status == "active":
            _iterate(env, _view(env))
            seen.append(grant(env).no_progress)
    assert seen == [1, 2, 3] and grant(env).goal_reason == "no_progress", seen


# --- #632-4: an unknown iteration keeps the validated stat cache --------------------------------


def test_a_tracked_set_within_the_budget_heals_after_one_unknown_iteration(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "DIGEST_BUDGET", 100 * 1024)  # scaled: 5 x 30 KiB > 100 KiB
    env = make_env(tmp_path, max_iterations=20)
    for n in range(5):
        _iterate(env, _create(env, f"asset{n}.bin", "seed"))
    for n in range(5):  # grown by someone else, then settled (old mtimes)
        path = env.sub / f"asset{n}.bin"
        path.write_bytes(bytes([n]) * 30 * 1024)
        os.utime(path, ns=(10**18, 10**18))
    seen = []
    for n in range(3):
        _iterate(env, _create(env, f"legit{n}.txt", f"change {n}"))
        seen.append(grant(env).no_progress)
    assert seen == [1, 0, 0], seen  # unknown once (over the budget), then healed


def test_an_unknown_iteration_keeps_the_cache_entries_it_validated(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    for n in range(3):
        _iterate(env, _create(env, f"f{n}.txt", f"v{n}"))
    for n in range(3):
        os.utime(env.sub / f"f{n}.txt", ns=(10**18, 10**18))
    _iterate(env, _view(env, "f0.txt"))  # settled: all three cached
    tracker = env.executor._tracker(env.grant_id)
    assert len(tracker.cache()) == 3
    monkeypatch.setattr(pg, "file_digest", lambda path: None)  # the new file is unreadable
    _iterate(env, _create(env, "new.txt", "x"))
    assert len(tracker.cache()) == 3  # the validated entries stay

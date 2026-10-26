"""W6a fix round 3 (#296, PR #630): the round-3 review's reproductions and the extras.

* Astra 1: a tracked file grown past the digest cap by someone else is identified by its
  stat tuple (``big:``), so it never makes every later outcome unknown;
* Astra 2: a fresh workspace change counts even when the iteration's own edit is a no-op
  (rewriting the same bytes, read-only work or rerunning identical checks on an unchanged
  workspace still is not progress);
* Astra 3: the iteration's own block is applied after the commit's validation: a person's
  stop (even a block with the same reason) during the final digest is an interruption;
* Astra 4: an unknown or interrupted blocked report blocks with its plain code and never
  feeds the same-blocker detector;
* extras: no cached digest for a file whose mtime is under 2 s old; a per-iteration digest
  byte budget; files about to leave the identity are not read.
Offline: scripted provider, fake run_checks results, no sleeps.
"""

from __future__ import annotations

import os
import random

import pytest
from grants_exec_helpers import grant, make_env
from grants_helpers import confirm_and_resume
from test_grants_progress_exec import _checks_call, _create, _replace, _report, _result

from stiracore.grants import progress as pg


def _iterate(env, *steps):
    from grants_exec_helpers import run_one

    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


def _state(env):
    g = grant(env)
    return g.goal_status, g.goal_reason, g.no_progress, g.iterations


# --- Astra 1: an oversize file ----------------------------------------------------------------------


def test_a_tracked_file_grown_past_the_cap_does_not_poison_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "large.bin", "seed"))
    with (env.sub / "large.bin").open("r+b") as f:
        f.truncate(pg.DIGEST_MAX_BYTES + 1)  # someone else grows it
    for n in range(3):
        _iterate(env, _create(env, f"legitimate{n}.txt", f"real change {n}"))
        assert grant(env).goal_status == "active", _state(env)
    assert grant(env).no_progress == 0


def test_an_oversize_file_is_identified_by_a_sample_of_its_bytes(tmp_path):
    big = tmp_path / "big"
    with big.open("wb") as f:
        f.truncate(pg.DIGEST_MAX_BYTES + 1)
    first = pg.file_identity(str(big), pg.ReadBudget())
    assert first.startswith(b"big:") and len(first) != 32  # never equal to a content digest
    os.utime(big, ns=(10**18, 10**18))
    assert pg.file_identity(str(big), pg.ReadBudget()) == first  # W6-H: a touch is no change
    with big.open("r+b") as f:
        f.write(b"!")
    assert pg.file_identity(str(big), pg.ReadBudget()) != first  # its bytes moved: a change
    digests, cache = pg.read_files([str(big)], {})
    assert digests[str(big)].startswith(b"big:") and not cache  # never cached


# --- Astra 2: a fresh change counts whoever made it -----------------------------------------------


def test_an_external_edit_with_a_noop_own_edit_is_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.txt", "start"))
    for n in range(3):
        (env.sub / "a.txt").write_text(f"legitimate{n}")
        _iterate(
            env,
            _replace(env, "a.txt", f"legitimate{n}", f"legitimate{n}"),
            _report("investigating"),
        )
        assert grant(env).goal_status == "active", _state(env)
    assert grant(env).no_progress == 0


@pytest.mark.parametrize("seed", range(8))
def test_every_new_external_state_is_progress(tmp_path, seed):
    rng = random.Random(seed)
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.txt", "initial"))
    env.executor._run_checks_runner = lambda *a, **k: _result(2)
    seen = {b"initial"}
    for n in range(6):
        value = f"external-{seed}-{n}-{rng.randrange(10000)}"
        (env.sub / "a.txt").write_text(value)
        assert value.encode() not in seen
        seen.add(value.encode())
        steps = [_replace(env, "a.txt", value, value)]
        if rng.random() < 0.35:
            steps.append(_checks_call(env, 2))
        _iterate(env, *steps)
        assert (grant(env).no_progress, grant(env).goal_status) == (0, "active"), (seed, n)


def test_a_persons_edit_of_a_cached_file_is_seen(tmp_path):
    """The stat cache never answers for a changed file, even with its mtime put back."""
    env = make_env(tmp_path, max_iterations=20)
    env.executor._run_checks_runner = lambda *a, **k: _result(2)
    _iterate(env, _create(env, "a.txt", "v1"))
    path = env.sub / "a.txt"
    os.utime(path, ns=(10**18, 10**18))
    _iterate(env, _checks_call(env))  # reads a.txt settled: cached
    assert env.executor._trackers.find(env.grant_id).cache()
    path.write_text("v2")  # a person edits it (same size) and puts the mtime back
    os.utime(path, ns=(10**18, 10**18))
    _iterate(env, _checks_call(env, 2))
    assert _state(env) == ("active", "", 0, 3)


def test_no_change_by_anyone_is_still_no_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a.txt", "v1"))
    _iterate(env, _replace(env, "a.txt", "v1", "v1"), _report("investigating"))  # same bytes
    assert (grant(env).no_progress, grant(env).goal_status) == (1, "active")
    _iterate(env, _report("investigating"))  # read-only
    assert (grant(env).no_progress, grant(env).goal_status) == (2, "active")


# --- Astra 3: the own block after the validation --------------------------------------------------


def test_a_persons_same_reason_block_is_not_the_iterations_own(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)
    tracker.ring.append(b"p" * 32)
    tracker.blocker, tracker.blocker_streak = "needs_decision", 1
    real = pg.file_digest
    calls = []

    def digest(path):
        calls.append(path)
        if len(calls) == 2:  # the final digest: a person blocks, resumes, blocks again
            env.service.block_goal(env.grant_id, "needs_decision")
            confirm_and_resume(env.service, env.grant_id, {})
            env.service.block_goal(env.grant_id, "needs_decision")
        return real(path)

    monkeypatch.setattr(pg, "file_digest", digest)
    _iterate(env, _create(env, "a.txt", "x"), _report("needs_decision", "blocked", 9))
    assert len(calls) >= 2
    assert list(tracker.ring) == [b"p" * 32] and tracker.blocker_streak == 1
    assert grant(env).goal_reason == "needs_decision"


def test_the_iterations_own_block_still_counts(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _report("needs_decision", "blocked"))
    tracker = env.executor._trackers.find(env.grant_id)
    assert grant(env).goal_reason == "needs_decision"
    assert tracker.blocker_streak == 1 and len(tracker.ring) == 1


# --- Astra 4: same_blocker only for a known outcome ------------------------------------------------


def test_an_unknown_blocked_report_never_fires_same_blocker(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)
    for n in range(2):
        _iterate(env, _report("needs_decision", "blocked", n))
        confirm_and_resume(env.service, env.grant_id, {})
    assert tracker.blocker_streak == 2
    monkeypatch.setattr(pg, "file_digest", lambda p: None)
    _iterate(env, _create(env, "a.txt", "x"), _report("needs_decision", "blocked", 9))
    assert grant(env).goal_reason == "needs_decision" and tracker.blocker_streak == 2


def test_an_interrupted_blocked_report_never_fires_same_blocker(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)
    for n in range(2):
        _iterate(env, _report("needs_decision", "blocked", n))
        confirm_and_resume(env.service, env.grant_id, {})
    observe = env.executor._observe

    def turn_after_the_reads(it):  # a person's turn arrives right before the commit
        result = observe(it)
        env.executor.preempt_signal.arrive()
        return result

    monkeypatch.setattr(env.executor, "_observe", turn_after_the_reads)
    _iterate(env, _report("needs_decision", "blocked", 9))
    assert grant(env).goal_reason == "needs_decision" and tracker.blocker_streak == 2


def test_the_third_known_blocked_report_is_same_blocker(tmp_path):
    env = make_env(tmp_path)
    for n in range(2):
        _iterate(env, _report("needs_decision", "blocked", n))
        confirm_and_resume(env.service, env.grant_id, {})
    _iterate(env, _report("needs_decision", "blocked", 9))
    assert grant(env).goal_reason == "same_blocker"


# --- extras ----------------------------------------------------------------------------------------------


def test_a_digest_under_two_seconds_old_is_not_cached(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.write_bytes(b"one")
    mtime = os.stat(a).st_mtime_ns
    monkeypatch.setattr(pg, "_now", lambda: mtime / 1e9 + 1.0)  # 1 s after the write
    _, cache = pg.read_files([str(a)], {})
    assert not cache
    monkeypatch.setattr(pg, "_now", lambda: mtime / 1e9 + 3.0)  # 3 s after
    _, cache = pg.read_files([str(a)], {})
    assert cache


def test_the_digest_byte_budget_is_per_iteration(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "DIGEST_BUDGET", 10)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x" * 6)
    b.write_bytes(b"y" * 6)
    budget = pg.ReadBudget()
    assert pg.read_files([str(a)], {}, budget) is not None
    assert pg.read_files([str(b)], {}, budget) is None  # 12 > 10 in one iteration
    assert pg.read_files([str(b)], {}) is not None  # a new budget: readable


def test_an_iteration_over_the_byte_budget_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pg, "DIGEST_BUDGET", 15
    )  # the before-write read (10) fits, the end read not
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "a.txt").write_text("0123456789")
    _iterate(env, _replace(env, "a.txt", "0123456789", "abcdefghij"))
    tracker = env.executor._trackers.find(env.grant_id)
    assert grant(env).no_progress == 1 and list(tracker.ring) == []


def test_a_file_about_to_leave_the_identity_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "FILES_KEPT", 2)
    env = make_env(tmp_path)
    _iterate(env, _create(env, "a.txt", "a"), _create(env, "b.txt", "b", 2))
    (env.sub / "a.txt").unlink()
    os.mkfifo(env.sub / "a.txt")  # unreadable, but about to be evicted
    _iterate(env, _create(env, "c.txt", "c"), _create(env, "d.txt", "d", 2))
    assert grant(env).no_progress == 0

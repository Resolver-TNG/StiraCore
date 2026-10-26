"""W6a fix round 2 (#296, PR #630): the round-2 reviews' reproductions, plus a property test.

The outcome identity is a **fresh read** of the files the grant has written (a missing
file is an explicit marker), with the iteration's per-check outcomes as one set; anything
unreadable makes the outcome **unknown** (no progress, not in the history, no streak
reset); at most 256 paths, no counters; the commit re-reads the grant's own state.

* Astra B1: a person's edit / delete / rename of a written file between iterations is a
  change (never a false loop);
* Astra B2, Opus R1: an unreadable iteration never makes the next one look new;
* Astra B3: a cycle over more than 256 files that repeats the retained state is a loop;
* Astra B4: how the same checks are split over calls is not part of the outcome;
* Astra B5, Opus R6a: a revoke / block / pause that lands after the last effect (during the
  final digest) is an interruption;
* a property test against a small model of the rule.
Offline: scripted provider, fake run_checks results, no sleeps.
"""

from __future__ import annotations

import copy
import os
import random

import pytest
from grants_exec_helpers import grant, make_env, run_one, use
from test_grants_progress_exec import _checks_call, _create, _replace, _report, _result, _results

from stiracore.grants import progress as pg
from stiracore.grants.schema import GrantRefused


def _iterate(env, *steps):
    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


def _state(env):
    g = grant(env)
    return g.goal_status, g.goal_reason, g.no_progress, g.iterations


def _row(kind, failed):
    return {
        "kind": kind,
        "status": "failed",
        "reason": None,
        "exit_code": 1,
        "duration_ms": 5,
        "counts": {"passed": 4, "failed": failed, "errors": 0, "skipped": 0},
        "failures": [],
    }


def _kinds_runner(env, rows):
    """run_checks answers the asked kinds with fixed rows (in the asked order)."""

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        kinds = arguments.get("kinds") or sorted(rows)
        failing = [copy.deepcopy(rows[k]) for k in kinds]
        return {"status": "failed", "reason": None, "checks": failing, "duration_ms": 9}

    env.executor._run_checks_runner = runner


def _kinds_call(env, kinds, n=1):
    return use("run_checks", {"path": str(env.sub), "kinds": list(kinds)}, n)


# --- Astra B1: a person's change of a written file is a change -----------------------------------


def test_a_persons_edit_of_a_written_file_is_not_a_loop(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result(), _result()])
    _iterate(env, _create(env, "a.txt", "v1"), _checks_call(env))
    (env.sub / "a.txt").write_text("v2")  # a person edits it between iterations
    _iterate(env, _checks_call(env, 2))
    assert _state(env) == ("active", "", 0, 2)


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_a_deleted_or_renamed_written_file_is_a_change(tmp_path, change):
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result(), _result()])
    _iterate(env, _create(env, "a.txt", "v1"), _checks_call(env))
    path = env.sub / "a.txt"
    if change == "delete":
        path.unlink()
    else:
        path.rename(env.sub / "b.txt")
    _iterate(env, _checks_call(env, 2))
    assert _state(env) == ("active", "", 0, 2)


def test_reapplying_a_change_a_person_reverted_is_a_loop(tmp_path):
    """Opus R5: the same state and the same checks again really is no net change."""
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "a.md").write_text("v0\n")
    _results(env, [_result(), _result()])
    _iterate(env, _replace(env, "a.md", "v0", "v1"), _checks_call(env))
    (env.sub / "a.md").write_text("v0\n")  # a person reverts it
    _iterate(env, _replace(env, "a.md", "v0", "v1"), _checks_call(env, 2))
    assert _state(env)[:2] == ("blocked", "loop_detected")


# --- Astra B2 / Opus R1: unknown never earns progress ---------------------------------------------


def _press(env, *steps):
    try:
        _iterate(env, *steps)
    except GrantRefused:
        return False
    return True


def test_an_unreadable_noop_never_rearms_identical_checks(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "a.txt").write_text("same")
    _results(env, [_result() for _ in range(6)])
    _iterate(env, _checks_call(env))
    original = pg.file_digest
    for n in range(4):
        calls = []

        def unreadable_final(path, calls=calls):
            calls.append(path)
            return None if len(calls) == 2 else original(path)

        with monkeypatch.context() as m:
            m.setattr(pg, "file_digest", unreadable_final)
            if not _press(
                env,
                _replace(env, "a.txt", "same", "temp"),
                _replace(env, "a.txt", "temp", "same", 2),
            ):
                break
        if not _press(env, _checks_call(env, 10 + n)):
            break
    g = grant(env)
    assert g.goal_status == "blocked", _state(env)
    tracker = env.executor._trackers.find(env.grant_id)
    assert len({fp for fp in tracker.ring if fp is not None}) <= 2  # at most two identities


def test_alternating_an_unknown_iteration_with_the_same_check_blocks(tmp_path, monkeypatch):
    """Opus R1: A = a same-bytes rewrite whose digest cannot be read (unknown) + the check;
    B = the same check. An unknown A never makes the next B new."""
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "x.md").write_text("same\n")
    env.executor._run_checks_runner = lambda arguments, run_policy, host=None: _result()
    original = pg.file_digest
    for cycle in range(8):
        with monkeypatch.context() as m:
            m.setattr(pg, "file_digest", lambda path: None)
            if not _press(env, _replace(env, "x.md", "same", "same", cycle), _checks_call(env, 2)):
                break
        assert pg.file_digest is original
        if not _press(env, _checks_call(env, 5000 + cycle)):
            break
    g = grant(env)
    assert g.goal_status == "blocked" and g.iterations <= 4, _state(env)


def test_an_unknown_outcome_is_not_in_the_history_and_resets_no_streak(tmp_path, monkeypatch):
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result() for _ in range(3)])
    _iterate(env, _create(env, "a.txt", "v1"), _checks_call(env))
    tracker = env.executor._trackers.find(env.grant_id)
    tracker.blocker, tracker.blocker_streak = "needs_decision", 2
    before = list(tracker.ring)
    os.utime(env.sub / "a.txt", ns=(1, 1))  # its stat moved: read again, and it fails
    monkeypatch.setattr(pg, "file_digest", lambda path: None)
    _iterate(env, _checks_call(env, 2))
    assert list(tracker.ring) == before  # not in the history
    assert tracker.blocker_streak == 2  # no streak reset
    assert grant(env).no_progress == 1  # but no progress


# --- Astra B3: the cap has no counter ---------------------------------------------------------------


def test_a_two_state_cycle_over_258_files_is_a_loop(tmp_path):
    env = make_env(tmp_path, max_iterations=20, total=5_000_000, per_tick=2_000_000)
    names = [f"f{i}.txt" for i in range(258)]

    def batch(edit):  # at most 30 tool calls per model reply (the in-flight set holds 64)
        uses = [edit(n, name) for n, name in enumerate(names_half)]
        env.provider.script = [uses[i : i + 30] for i in range(0, len(uses), 30)]
        env.provider.script.append([{"text": "ok"}])

    halves = (names[:129], names[129:])
    for names_half in halves:
        batch(lambda n, name: _create(env, name, "A", n))
        run_one(env)
        assert grant(env).goal_status == "active"
    blocked = False
    for value in ("B", "A", "B", "A"):
        old = "A" if value == "B" else "B"
        for names_half in halves:
            batch(lambda n, name, old=old, value=value: _replace(env, name, old, value, n))
            try:
                run_one(env)
            except GrantRefused:
                blocked = True
                break
            if grant(env).goal_status != "active":
                blocked = True
                break
        if blocked:
            break
    assert blocked and grant(env).goal_reason == "loop_detected", _state(env)
    tracker = env.executor._trackers.find(env.grant_id)
    assert len(tracker.files) == pg.FILES_KEPT == 256
    assert not hasattr(tracker, "overflow") and not hasattr(tracker, "lost")


def test_one_iteration_writing_more_than_the_cap_is_unknown(tmp_path, monkeypatch):
    from stiracore.grants import executor as ex_mod

    monkeypatch.setattr(ex_mod, "WRITTEN_KEPT", 2)
    env = make_env(tmp_path, max_iterations=20)
    _iterate(env, _create(env, "a", "1", 1), _create(env, "b", "1", 2), _create(env, "c", "1", 3))
    tracker = env.executor._trackers.find(env.grant_id)
    assert grant(env).no_progress == 1 and not [fp for fp in tracker.ring if fp]


# --- Astra B4: call grouping is not part of the outcome ---------------------------------------------


def test_the_same_checks_split_over_calls_are_the_same_outcome(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    kinds = ["pytest", "ruff", "compileall", "unittest"]
    _kinds_runner(env, {k: _row(k, 1) for k in kinds})
    partitions = [
        [kinds],
        [[kinds[0]], kinds[1:]],
        [kinds[:2], kinds[2:]],
        [[k] for k in kinds],
    ]
    _iterate(env, *[_kinds_call(env, g, i) for i, g in enumerate(partitions[0])])
    assert _state(env) == ("active", "", 0, 1)
    _iterate(env, *[_kinds_call(env, g, i) for i, g in enumerate(partitions[1])])
    assert _state(env) == ("blocked", "loop_detected", 1, 2)


def test_a_real_change_of_one_check_row_differs(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    kinds = ["pytest", "ruff"]
    rows = {k: _row(k, 1) for k in kinds}
    _kinds_runner(env, rows)
    _iterate(env, _kinds_call(env, kinds))
    rows["ruff"] = _row("ruff", 0)
    _iterate(env, _kinds_call(env, ["pytest"]), _kinds_call(env, ["ruff"], 2))
    assert _state(env) == ("active", "", 0, 2)


def test_many_identical_check_calls_with_a_noop_rewrite_are_no_progress(tmp_path):
    """Opus R1's trigger (more check calls than one result list holds): the same row set."""
    env = make_env(tmp_path, max_iterations=20, per_tick=60_000, total=1_000_000)
    (env.sub / "x.md").write_text("same\n")
    env.executor._run_checks_runner = lambda arguments, run_policy, host=None: _result()
    flood = [_replace(env, "x.md", "same", "same", 900)]
    flood += [_checks_call(env, 1000 + i) for i in range(33)]
    _press(env, *flood)
    _press(env, _checks_call(env, 5000))
    assert _state(env)[:2] == ("blocked", "loop_detected")


def test_rows_are_canonical_per_check():
    one = {"status": "failed", "reason": None, "checks": [_row("pytest", 1), _row("ruff", 0)]}
    a = {"status": "failed", "reason": None, "checks": [_row("pytest", 1)]}
    b = {"status": "passed", "reason": None, "checks": [_row("ruff", 0)]}
    assert set(pg.normalize_checks(one)) == set(pg.normalize_checks(a)) | set(
        pg.normalize_checks(b)
    )


# --- Astra B5 / Opus R6a: the grant's own state at the commit ----------------------------------------

_AT_DIGEST = {
    "shutdown": lambda env, it: env.executor.stop(0),
    "user_turn": lambda env, it: env.executor.preempt_signal.arrive(),
    "busy": lambda env, it: setattr(env, "busy", True),
    "cancel": lambda env, it: it.on_cancel(),
    "permit_stale": lambda env, it: it.stop("permit_stale"),
    "abandoned": lambda env, it: setattr(it, "abandoned", True),
    "kill": lambda env, it: env.authority.kill_switch(),
    "revoke": lambda env, it: env.service.revoke(env.grant_id),
    "narrow": lambda env, it: env.service.block_goal(env.grant_id, "out_of_scope"),
    "pause": lambda env, it: env.service.pause(env.grant_id),
}


def _at_final_digest(env, monkeypatch, action):
    real = pg.file_digest
    calls = []

    def digest(path):
        calls.append(path)
        if len(calls) == 2:
            action(env, env.executor._current)
        return real(path)

    monkeypatch.setattr(pg, "file_digest", digest)
    return calls


@pytest.mark.parametrize("kind", sorted(_AT_DIGEST))
def test_a_stop_during_the_final_digest_is_an_interruption(kind, tmp_path, monkeypatch):
    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)
    tracker.ring.append(b"p" * 32)
    tracker.blocker, tracker.blocker_streak = "needs_decision", 2
    tracker.no_action_streak = 1
    calls = _at_final_digest(env, monkeypatch, _AT_DIGEST[kind])
    _iterate(env, _create(env, "a.txt", "x"))
    assert len(calls) >= 2
    assert grant(env).no_progress == 0
    assert list(tracker.ring) == [b"p" * 32]
    assert tracker.blocker_streak == 2 and tracker.no_action_streak == 1


def test_a_late_block_keeps_the_real_streak(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _iterate(env, use("file_read", {"path": str(env.sub)}), _report("investigating"))
    assert grant(env).no_progress == 1
    tracker = env.executor._tracker(env.grant_id)
    before = list(tracker.ring)
    _at_final_digest(env, monkeypatch, _AT_DIGEST["narrow"])
    _iterate(env, _create(env, "a.txt", "x"))
    g = grant(env)
    assert (g.goal_status, g.goal_reason, g.no_progress) == ("blocked", "out_of_scope", 1)
    assert list(tracker.ring) == before


def test_the_iterations_own_block_is_not_an_interruption(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate(env, _checks_call(env), _report("needs_decision", "blocked", 2))
    tracker = env.executor._trackers.find(env.grant_id)
    assert tracker.blocker_streak == 1 and len(tracker.ring) == 1


# --- a property test against a model of the rule ------------------------------------------------------

_FILES = ("a.md", "b.md", "c.md")
_VALUES = ("v0", "v1", "v2")


class _Oracle:
    """An iteration is progress iff everything was readable, it observed something (a
    check, or a workspace identity other than the last known one — a change by anyone),
    and (fresh identity, check set) is not among the last 16; a repeat among the last 6
    is a loop; unknown is no progress and not in the history."""

    def __init__(self, ticks: int) -> None:
        self.tracked: list[str] = []
        self.ring: list = []
        self.streak = 0
        self.ticks = ticks
        self.last = ()

    def _track(self, paths):
        for p in dict.fromkeys(paths):
            if p in self.tracked:
                self.tracked.remove(p)
            self.tracked.append(p)

    def step(self, disk, changed, written, rows, unknown):
        if unknown:
            # W6-H (#632-3): an unobserved end tracks a new path only from its before-write
            # bytes; here every digest of an unknown iteration fails, so only tracked ones
            self._track([p for p in written if p in self.tracked])
            self.streak += 1
            return ("blocked", "no_progress") if self.streak >= self.ticks else ("active", "")
        self._track(changed)
        identity = tuple(sorted((p, disk[p]) for p in self.tracked))
        pair = None
        if identity != self.last or rows:
            pair = (identity, frozenset(rows))
        self.last = identity
        progress = pair is not None and pair not in self.ring[-16:]
        loop = pair is not None and pair in self.ring[-6:]
        self.ring.append(pair)
        self.streak = 0 if progress else self.streak + 1
        if self.streak >= self.ticks:
            return ("blocked", "no_progress")
        if loop:
            return ("blocked", "loop_detected")
        return ("active", "")


@pytest.mark.parametrize("seed", range(24))
def test_random_sequences_match_the_model(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    env = make_env(tmp_path, max_iterations=20)
    for name in _FILES:
        (env.sub / name).write_text("v0")
    failing: list[int] = []
    env.executor._run_checks_runner = lambda arguments, run_policy, host=None: {
        "status": "failed",
        "reason": None,
        "checks": [_row("pytest", failing.pop(0))],
    }
    unreadable = {"on": False}
    real = pg.file_digest
    monkeypatch.setattr(pg, "file_digest", lambda p: None if unreadable["on"] else real(p))
    oracle = _Oracle(ticks=grant(env).no_progress_ticks)
    for n in range(14):
        if rng.random() < 0.25:  # a person changes a file between iterations
            (env.sub / rng.choice(_FILES)).write_text(rng.choice(_VALUES))
        start = {name: (env.sub / name).read_text() for name in _FILES}
        plan = dict(start)
        steps, written, rows = [], [], set()
        for k in range(rng.randint(1, 3)):
            if rng.random() < 0.6:
                name = rng.choice(_FILES)
                target = rng.choice(_VALUES)
                if target == plan[name]:  # a write that changes nothing
                    steps.append(_replace(env, name, plan[name], "tmp", 10 * k))
                    steps.append(_replace(env, name, "tmp", plan[name], 10 * k + 1))
                else:
                    steps.append(_replace(env, name, plan[name], target, 10 * k))
                    plan[name] = target
                written.append(str(env.sub / name))
            else:
                value = rng.choice((0, 1, 2))
                failing.append(value)
                rows.add(value)
                steps.append(_checks_call(env, 10 * k + 5))
        unknown = bool(written) and rng.random() < 0.2
        unreadable["on"] = unknown
        try:
            _iterate(env, *steps)
        finally:
            unreadable["on"] = False
        disk = {str(env.sub / name): (env.sub / name).read_text() for name in _FILES}
        changed = [p for p in dict.fromkeys(written) if disk[p] != start[p.rsplit("/", 1)[-1]]]
        expected = oracle.step(disk, changed, written, rows, unknown)
        g = grant(env)
        assert (g.goal_status, g.goal_reason) == expected, (seed, n, _state(env))
        assert g.no_progress == oracle.streak, (seed, n, _state(env))
        if g.goal_status != "active":
            break


# --- more commit-path pins ----------------------------------------------------------------------------


def test_a_deleted_file_is_not_the_state_before_it_was_written(tmp_path):
    """A missing file is an explicit marker, not a skipped path."""
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result(), _result()])
    _iterate(env, _checks_call(env))
    _iterate(env, _create(env, "a.txt", "v1"))
    (env.sub / "a.txt").unlink()
    _iterate(env, _checks_call(env, 2))
    assert _state(env) == ("active", "", 0, 3)


def test_a_pause_and_resume_during_the_final_digest_is_an_interruption(tmp_path, monkeypatch):
    from grants_helpers import confirm_and_resume

    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)

    def pause_and_resume(env, it):
        env.service.pause(env.grant_id)
        confirm_and_resume(env.service, env.grant_id, {})

    _at_final_digest(env, monkeypatch, pause_and_resume)
    _iterate(env, _create(env, "a.txt", "x"))
    assert grant(env).status == "active" and grant(env).iterations == 1
    assert list(tracker.ring) == []  # the grant stopped meanwhile: not counted as an outcome


def test_the_iterations_own_claim_is_not_an_interruption(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(failed=0)])
    _iterate(env, _checks_call(env), _report("goal_met", "claim_complete", 2))
    tracker = env.executor._trackers.find(env.grant_id)
    assert grant(env).goal_status == "awaiting_verification" and len(tracker.ring) == 1


def test_too_many_distinct_check_rows_are_unknown(tmp_path, monkeypatch):
    from stiracore.grants import executor as ex_mod

    monkeypatch.setattr(ex_mod, "CHECKS_KEPT", 2)
    env = make_env(tmp_path, max_iterations=20)
    kinds = ["pytest", "ruff", "compileall"]
    _kinds_runner(env, {k: _row(k, 1) for k in kinds})
    _iterate(env, _kinds_call(env, kinds))
    tracker = env.executor._trackers.find(env.grant_id)
    assert grant(env).no_progress == 1 and list(tracker.ring) == []


def test_an_unreadable_grant_at_the_commit_is_an_interruption(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    tracker = env.executor._tracker(env.grant_id)
    real = env.service.executor_record
    state = {"commit": False}

    def record(grant_id):
        if state["commit"]:
            raise RuntimeError("unreadable")
        return real(grant_id)

    monkeypatch.setattr(env.service, "executor_record", record)
    original = env.executor._own_state

    def own_state(it, end):
        state["commit"] = True
        try:
            return original(it, end)
        finally:
            state["commit"] = False

    monkeypatch.setattr(env.executor, "_own_state", own_state)
    _iterate(env, _create(env, "a.txt", "x"))
    assert grant(env).no_progress == 0 and list(tracker.ring) == []

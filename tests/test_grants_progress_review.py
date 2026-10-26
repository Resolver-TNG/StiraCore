"""W6a fix round 1 (#296, PR #630): the reviews' reproductions as tests.

* B1 (Astra B1, Opus P1 / P2 / P3): rerunning the same checks k times, in another order, or
  with another report label is the same outcome — never progress;
* B2 (Opus P5 / P10): a check outcome is bound to the workspace state it ran on — a real
  change of a written file between two equal check results is progress, not a loop;
* B3 (Astra B2): a stop that arrives while the outcome is read is an interruption at commit;
* B4 (Astra B3, Opus P12): an iteration that consumed no effect never spends an iteration;
* B5 (Astra B4): a file changed while its digest is read gives no digest (no progress);
* B6 (Astra B5): a FIFO never blocks the digest, nor the executor's end;
* B7 (Opus P9): a live grant's tracker is never evicted.
Offline: scripted provider, fake run_checks results, no sleeps (bounded waits only).
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import os
import subprocess
import sys
import threading

from grants_exec_helpers import grant, make_env, run_one, use
from test_grants_progress_exec import _checks_call, _create, _replace, _report, _result, _results

from stiracore.grants import progress as pg


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


def _rows_result(rows):
    return {"status": "failed", "reason": None, "checks": rows, "duration_ms": 7}


# --- B1: activity / label churn is no progress ------------------------------------------------


def test_rerunning_the_same_checks_more_often_is_the_same_outcome(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result() for _ in range(10)])
    _iterate(env, _checks_call(env))
    assert _state(env) == ("active", "", 0, 1)
    _iterate(env, *[_checks_call(env, i) for i in range(3)])  # the same result, three times
    assert _state(env) == ("blocked", "loop_detected", 1, 2)


def test_the_same_checks_in_another_order_are_the_same_outcome(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    rows = [_row("pytest", 2), _row("ruff", 0), _row("compileall", 1)]
    orders = list(itertools.permutations(rows))[:2]
    _results(env, [_rows_result([copy.deepcopy(r) for r in order]) for order in orders])
    _iterate(env, _checks_call(env))
    _iterate(env, _checks_call(env, 2))
    assert _state(env) == ("blocked", "loop_detected", 1, 2)


def test_another_report_label_over_the_same_checks_is_no_progress(tmp_path):
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result() for _ in range(4)])
    _iterate(env, _checks_call(env), _report("step_done"))
    _iterate(env, _checks_call(env, 2), _report("tests_written", n=2))
    assert _state(env) == ("blocked", "loop_detected", 1, 2)


def test_the_report_is_not_part_of_the_fingerprint():
    t = pg.ProgressTracker()
    changes = [("a.md", b"1" * 32)]
    assert t.fingerprint(t.identity(changes), []) == t.fingerprint(t.identity(changes), [])
    import inspect

    assert "report" not in inspect.signature(t.fingerprint).parameters


# --- B2: check outcomes bound to the workspace state --------------------------------------------


def test_edit_check_edit_check_with_the_same_failure_count_is_progress(tmp_path):
    """Opus P10: the second fix attempt changes a.py again; the failing count stays 1."""
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "a.py").write_text("v0\n")
    _results(env, [_rows_result([_row("pytest", 1)]) for _ in range(2)])
    steps = [
        _replace(env, "a.py", "v0", "v1"),
        _checks_call(env),
        _replace(env, "a.py", "v1", "v2"),
        _checks_call(env, 2),
    ]
    for n, step in enumerate(steps, 1):
        _iterate(env, step)
        assert _state(env) == ("active", "", 0, n)


def test_reverting_one_file_after_another_changed_is_progress(tmp_path):
    """Opus P5: (a=S1) → (a=S2, b) → (a=S1, b): the third state was never seen."""
    env = make_env(tmp_path, max_iterations=20)
    (env.sub / "a.md").write_text("S0\n")
    _iterate(env, _replace(env, "a.md", "S0", "S1"))
    _iterate(env, _replace(env, "a.md", "S1", "S2"), _create(env, "b.md", "b\n", 2))
    _iterate(env, _replace(env, "a.md", "S2", "S1"))
    assert _state(env) == ("active", "", 0, 3)


def test_a_write_lost_to_an_interruption_makes_the_next_outcome_new(tmp_path):
    """A file written by an interrupted iteration is tracked and read fresh at the next end:
    the same check result afterwards is on another state, not a loop."""
    env = make_env(tmp_path, max_iterations=20)
    _results(env, [_result(), _result()])
    _iterate(env, _checks_call(env))
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive() if n == 2 else None
    env.provider.calls = 0
    env.provider.script = [[_create(env, "a.md", "x\n")], [{"text": "ok"}]]
    run_one(env)
    env.provider.on_call = None
    assert (env.sub / "a.md").exists() and grant(env).no_progress == 0
    _iterate(env, _checks_call(env, 2))
    assert grant(env).goal_status == "active"


# --- B3: an interruption is re-read at commit -------------------------------------------------------


def test_a_shutdown_while_the_outcome_is_read_is_an_interruption(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    real = pg.file_digest
    calls = []

    def read_digest(path):
        calls.append(path)
        if len(calls) == 2:  # the final digest
            env.executor.stop(0)
            return None
        return real(path)

    monkeypatch.setattr(pg, "file_digest", read_digest)
    view = _iterate(env, _create(env, "a.txt", "x"))
    tracker = env.executor._trackers.find(env.grant_id)
    assert view["end"] == "shutdown" and len(calls) == 2
    assert grant(env).no_progress == 0 and (tracker is None or len(tracker.ring) == 0)


def test_a_narrowing_stop_is_an_interruption(tmp_path):
    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("x\n")
    read = use("file_read", {"path": str(env.sub / "a.md")})
    _iterate(env, read, _report("investigating"))
    assert grant(env).no_progress == 1
    env.provider.on_call = lambda n: env.service.block_goal(env.grant_id, "out_of_scope")
    env.provider.script = [[read], [{"text": "ok"}]]
    view = run_one(env)
    assert view["end"] in ("stopped", "permit_stale"), view
    assert grant(env).no_progress == 1 and grant(env).iterations == 2  # streak untouched


def test_a_user_turn_after_the_last_effect_is_an_interruption(tmp_path):
    env = make_env(tmp_path)
    read = use("file_read", {"path": str(env.sub)})
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive() if n == 2 else None
    env.provider.script = [[read], [{"text": "ok"}]]  # the turn arrives in the last model call
    run_one(env)
    assert grant(env).no_progress == 0 and grant(env).iterations == 1


# --- B4: no effect, no iteration ---------------------------------------------------------------------


def test_a_setup_failure_before_any_effect_spends_no_iteration(tmp_path):
    env = make_env(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic child setup failure")

    env.executor._build = fail
    view = run_one(env)
    assert view["end"] == "failed" and env.provider.calls == 0
    assert _state(env) == ("active", "", 0, 0)


def test_a_budget_refusal_before_any_effect_spends_no_iteration(tmp_path):
    env = make_env(tmp_path, per_tick=59)  # count 10 + max_tokens 50 = 60
    for _ in range(3):
        env.provider.script = [[{"text": "x"}]]
        assert run_one(env)["end"] == "budget_refused"
    assert env.provider.calls == 0 and _state(env) == ("active", "", 0, 0)


# --- B5: a torn read is no digest ------------------------------------------------------------------


def test_a_file_changed_during_its_digest_has_no_digest(tmp_path, monkeypatch):
    path = tmp_path / "race.bin"
    path.write_bytes(b"A" * (1 << 20) + b"B" * (1 << 20))
    real = os.read
    calls = []

    def raced(fd, size):
        chunk = real(fd, size)
        calls.append(len(chunk))
        if len(calls) == 1:
            path.write_bytes(b"C" * (1 << 20) + b"D" * (1 << 20))
        return chunk

    monkeypatch.setattr(pg.os, "read", raced)
    assert pg.file_digest(str(path)) is None
    assert calls  # it did read


def test_a_file_replaced_during_its_digest_has_no_digest(tmp_path, monkeypatch):
    path = tmp_path / "swap.bin"
    path.write_bytes(b"A" * 100)
    real = os.read
    calls = []

    def raced(fd, size):
        chunk = real(fd, size)
        if not calls:
            other = tmp_path / "other.bin"
            other.write_bytes(b"A" * 100)
            os.replace(other, path)  # a new inode at the path, same bytes
        calls.append(len(chunk))
        return chunk

    monkeypatch.setattr(pg.os, "read", raced)
    assert pg.file_digest(str(path)) is None


def test_a_torn_final_digest_counts_no_progress(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    real_digest, real_read = pg.file_digest, os.read
    digests, reads = [], []
    target = env.sub / "a.txt"

    def raced_read(fd, size):
        chunk = real_read(fd, size)
        reads.append(len(chunk))
        if len(reads) == 1:
            target.write_bytes(b"C" * (1 << 20) + b"D" * (1 << 20))
        return chunk

    def digest(path):
        digests.append(path)
        if len(digests) == 2:
            with monkeypatch.context() as m:
                m.setattr(pg.os, "read", raced_read)
                return real_digest(path)
        return real_digest(path)

    monkeypatch.setattr(pg, "file_digest", digest)
    _iterate(env, _create(env, "a.txt", "A" * (1 << 20) + "B" * (1 << 20)))
    assert reads and grant(env).no_progress == 1


def test_an_unchanged_file_still_has_its_digest(tmp_path):
    path = tmp_path / "plain.bin"
    path.write_bytes(b"x" * (3 << 20))
    assert pg.file_digest(str(path)) == hashlib.sha256(b"x" * (3 << 20)).digest()


# --- B6: a FIFO never blocks --------------------------------------------------------------------------


def test_a_fifo_with_no_writer_returns_at_once(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    code = "from stiracore.grants.progress import file_digest; import sys; print(file_digest(sys.argv[1]))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(fifo)], env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        try:
            out, _ = process.communicate(timeout=10)
            blocked = False
        except subprocess.TimeoutExpired:
            blocked, out = True, ""
        assert not blocked, "the digest blocked on a FIFO"
        assert out.strip() == "None"
    finally:
        process.kill()
        process.wait()


def test_a_fifo_swapped_in_before_the_final_digest_never_holds_the_executor(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    real_digest = pg.file_digest
    calls = []
    swapped = threading.Event()
    target = env.sub / "a.txt"

    def digest(path):
        calls.append(path)
        if len(calls) == 2:
            target.unlink()
            os.mkfifo(target)
            swapped.set()
        return real_digest(path)

    monkeypatch.setattr(pg, "file_digest", digest)
    env.provider.script = [[_create(env, "a.txt", "x")], [{"text": "ok"}]]
    env.executor.step(env.grant_id)
    try:
        assert swapped.wait(10)
        assert env.executor.wait_idle(10), "the executor is held by the FIFO"
        assert grant(env).no_progress == 1  # unreadable: no progress
        assert env.executor.stop(0.05) == "stopped"
    finally:
        try:  # a regression would leave a reader blocked: release it
            fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        env.executor.wait_idle(5)
        env.executor.stop(5)


# --- B7: a live grant's tracker stays ---------------------------------------------------------------


def test_a_live_grants_tracker_is_never_evicted(tmp_path):
    env = make_env(tmp_path)
    mine = env.executor._tracker(env.grant_id)
    mine.ring.append(b"x" * 32)
    for n in range(pg.TRACKERS_KEPT + 4):
        env.executor._tracker(f"{n:032x}")  # other (unknown, not live) grants
    assert env.executor._trackers.find(env.grant_id) is mine
    assert len(env.executor._trackers) <= pg.TRACKERS_KEPT


def test_trackers_evict_only_what_is_not_kept():
    trackers = pg.Trackers()
    live = trackers.get("live")
    for n in range(pg.TRACKERS_KEPT + 3):
        trackers.get(f"g{n}", keep=frozenset({"live"}))
    assert trackers.find("live") is live and len(trackers) == pg.TRACKERS_KEPT


def test_using_a_tracker_refreshes_its_place(tmp_path):
    env = make_env(tmp_path)
    for n in range(pg.TRACKERS_KEPT - 1):
        env.executor._tracker(f"{n:032x}")
    env.executor._tracker(env.grant_id)
    env.executor._tracker("0" * 32)  # the oldest, used again
    assert list(env.executor._trackers._by_grant)[-1] == "0" * 32

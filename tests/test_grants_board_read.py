"""W5b fix round 1 (#296): the executor's and the grant service's board reads.

* the outcome row lists the scope tools that **ran**, never the admitted scope;
* the outcomes belong to their grant; the newest grant is the one shown;
* no read on the board's path ever waits on a lock (service, executor, iteration, budget):
  a held lock gives the fixed ``grant_busy`` code at once.
Offline: a scripted provider, no model.
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest
from grants_exec_helpers import make_env, run_one, use

from stiracore.grants.schema import GrantRefused
from stiracore.observability import activity


def _read(env):
    return env.executor.board(env.grant_id)


def test_a_text_only_iteration_lists_no_tool(tmp_path):
    env = make_env(tmp_path, script=[[{"text": "nothing to call"}]])
    run_one(env)
    outcome = _read(env)["outcomes"][-1]
    assert env.executor.view(env.grant_id)["tool_calls"] == 0
    assert outcome["tools"] == []
    shown = activity.grant_view(env.service.board_view(), _read(env))
    assert shown["outcomes"][-1]["tools"] == []


def test_the_tools_that_ran_in_call_order(tmp_path):
    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("a\n", encoding="utf-8")
    env.provider.script = [
        [use("file_read", {"path": str(env.sub / "a.md")}, 1)],
        [
            use(
                "editor",
                {"command": "create", "path": str(env.sub / "b.md"), "file_text": "b\n"},
                2,
            )
        ],
        [use("file_read", {"path": str(env.sub / "a.md")}, 3)],
        [use("goal_report", {"kind": "progress", "code": "step_done"}, 4)],
    ]
    run_one(env)
    assert env.executor.view(env.grant_id)["tool_calls"] == 3
    assert _read(env)["outcomes"][-1]["tools"] == ["file_read", "editor"]


def test_the_outcomes_belong_to_their_grant(tmp_path):
    env = make_env(tmp_path, script=[[{"text": "x"}]])
    run_one(env)
    assert len(_read(env)["outcomes"]) == 1
    other = env.executor.board("f" * 32)
    assert other["outcomes"] == [] and other["iteration"]["state"] == "idle"
    assert "tick_used" not in other["iteration"]


def test_the_newest_grant_is_the_one_shown(tmp_path):
    from grants_helpers import confirm_and_issue, issue_body

    env = make_env(tmp_path)
    first = env.grant_id
    env.service.revoke(first)
    body = issue_body(env.sub, scope={"tools": ["file_read"], "workspace_subtree": str(env.sub)})
    second = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    assert second != first
    assert env.service.board_view()["grant_id"] == second
    assert env.service.list_view()["grants"][1]["grant_id"] == first


def test_an_idle_grant_reads_the_tick_of_its_last_iteration(tmp_path):
    env = make_env(tmp_path, script=[[{"text": "x"}]])
    run_one(env)
    read = _read(env)
    assert read["iteration"].get("tick_used") == read["outcomes"][-1]["tick_used"]
    assert type(read["outcomes"][-1]["duration_ms"]) is int


# --- never waits on a lock -------------------------------------------------------------------


def _timed(fn):
    start = time.monotonic()
    try:
        return fn(), time.monotonic() - start
    except GrantRefused as error:
        return error, time.monotonic() - start
    except Exception as error:  # noqa: BLE001 - the busy signal is an exception
        return error, time.monotonic() - start


def _held(lock):
    """Hold ``lock`` from another thread until the returned event is set."""
    got, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            got.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert got.wait(5)
    return release, thread


def _busy(result):
    return isinstance(result, Exception) and getattr(result, "board_busy", False) is True


def test_a_held_service_lock_is_busy_at_once(tmp_path):
    env = make_env(tmp_path)
    release, thread = _held(env.service._lock)
    try:
        result, took = _timed(env.service.board_view)
    finally:
        release.set()
        thread.join(5)
    assert _busy(result) and took < 0.2
    assert env.service.board_view()["grant_id"] == env.grant_id


def test_a_held_executor_lock_is_busy_at_once(tmp_path):
    env = make_env(tmp_path)
    release, thread = _held(env.executor._lock)
    try:
        result, took = _timed(lambda: _read(env))
    finally:
        release.set()
        thread.join(5)
    assert _busy(result) and took < 0.2
    assert _read(env)["iteration"]["state"] == "idle"


@pytest.mark.parametrize("which", ["iteration", "budget"])
def test_a_held_iteration_or_budget_lock_is_busy_at_once(tmp_path, which):
    env = make_env(tmp_path)
    hold, started = threading.Event(), threading.Event()
    env.provider.script = [[{"text": "x"}]]
    env.provider.on_call = lambda n: (started.set(), hold.wait(10))
    env.executor.step(env.grant_id)
    try:
        assert started.wait(10)
        it = env.executor._current
        lock = it.lock if which == "iteration" else it.budget._lock
        release, thread = _held(lock)
        try:
            result, took = _timed(lambda: _read(env))
        finally:
            release.set()
            thread.join(5)
        assert _busy(result) and took < 0.2
        running = _read(env)["iteration"]
        assert running["state"] == "running" and type(running["tick_used"]) is int
    finally:
        hold.set()
    assert env.executor.wait_idle(20)


def test_a_failing_board_row_never_leaves_the_executor_running(tmp_path, monkeypatch):
    env = make_env(tmp_path, script=[[{"text": "x"}]])

    def broken(_it):
        raise RuntimeError("row")

    monkeypatch.setattr(env.executor, "_outcome", broken)
    run_one(env)
    assert env.executor._current is None and env.executor.status()["running"] is False
    assert _read(env)["outcomes"] == []


# --- one consistent version (fix round 2) ------------------------------------------------------


def _held_iteration(env):
    hold, started = threading.Event(), threading.Event()
    env.provider.script = [[{"text": "done"}]]
    env.provider.on_call = lambda n: (started.set(), hold.wait(10))
    env.executor.step(env.grant_id)
    assert started.wait(10)
    return hold


def test_an_iteration_that_ends_during_the_read_is_busy_never_a_mix(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    hold = _held_iteration(env)
    it = env.executor._current
    original = it.try_summary

    def the_iteration_ends_here():  # the seam: between the snapshot and the iteration's own
        hold.set()
        assert env.executor.wait_idle(10)
        return original()

    monkeypatch.setattr(it, "try_summary", the_iteration_ends_here)
    result, _took = _timed(lambda: _read(env))
    assert _busy(result), result
    stable = _read(env)
    assert stable["iteration"]["state"] == "idle" and len(stable["outcomes"]) == 1
    assert stable["iteration"]["tick_used"] == stable["outcomes"][-1]["tick_used"]


def test_a_read_during_a_write_is_busy(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    hold = _held_iteration(env)
    seen = []
    charge = env.service.charge

    def charge_and_look(grant_id, tokens):
        seen.append(_timed(lambda: _read(env))[0])  # the read lands inside settle + charge
        return charge(grant_id, tokens)

    monkeypatch.setattr(env.service, "charge", charge_and_look)
    try:
        hold.set()
        assert env.executor.wait_idle(20)
    finally:
        hold.set()
    assert seen and all(_busy(r) for r in seen), seen
    assert env.executor.board_seq() % 2 == 0  # stable again


def test_a_called_name_is_a_scope_tool_once(tmp_path):
    from stiracore.grants.schema import TOOLS

    env = make_env(tmp_path)
    hold = _held_iteration(env)
    try:
        it = env.executor._current
        for name in ["w5bMarker-7c41e09d2b8a5f36", "goal_report", *sorted(TOOLS), *sorted(TOOLS)]:
            it.called_tool(name)
        assert it.called == sorted(TOOLS)  # scope names only, each once, in call order
    finally:
        hold.set()
    assert env.executor.wait_idle(20)


def test_a_read_inside_an_iterations_end_is_busy(tmp_path, monkeypatch):
    env = make_env(tmp_path, script=[[{"text": "x"}]])
    seen = []
    count = env.service.commit_iteration  # W6-H: the end's count is one commit step

    def count_and_look(grant_id, **kw):
        seen.append(_timed(lambda: _read(env))[0])  # inside the end: counts, row, summary
        return count(grant_id, **kw)

    monkeypatch.setattr(env.service, "commit_iteration", count_and_look)
    run_one(env)
    assert len(seen) == 1 and _busy(seen[0]), seen
    assert len(_read(env)["outcomes"]) == 1


def test_a_write_that_ends_during_the_read_is_busy(tmp_path, monkeypatch):
    """The read began while a write ran (its snapshot taken mid-write) and the write ended
    before the read did — nothing else ran: only the sequence's move at the write's end
    tells. Busy, never a mix."""
    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("a\n", encoding="utf-8")
    env.provider.script = [
        [use("file_read", {"path": str(env.sub / "a.md")}, 1)],
        [{"text": "done"}],
    ]
    first, second, release = threading.Event(), threading.Event(), threading.Event()

    def on_call(n):
        (first if n == 1 else second).set()
        assert release.wait(10) if n == 2 else True

    env.provider.on_call = on_call
    in_write, go = threading.Event(), threading.Event()
    charge = env.service.charge

    def slow_charge(grant_id, tokens):
        if not in_write.is_set():
            in_write.set()
            assert go.wait(10)
        return charge(grant_id, tokens)

    monkeypatch.setattr(env.service, "charge", slow_charge)
    env.executor.step(env.grant_id)
    try:
        assert in_write.wait(10)  # call 1's settle + charge: one write, held open
        assert env.executor.board_seq() % 2 == 1  # the write is open
        it = env.executor._current
        original = it.try_summary

        def the_write_ends_here():
            go.set()
            assert second.wait(10)  # the write ended; call 2 is held: no other write
            assert env.executor.board_seq() % 2 == 0
            return original()

        monkeypatch.setattr(it, "try_summary", the_write_ends_here)
        result, _took = _timed(lambda: _read(env))
        assert _busy(result), result
    finally:
        go.set()
        release.set()
    assert env.executor.wait_idle(20)


def test_a_refused_tool_call_is_not_listed(tmp_path, monkeypatch):
    """A call the scope checks refuse (here: the executor's own check refuses the editor)
    never ran — it is not listed; a call that ran is."""
    from stiracore.grants.executor import IterationStop

    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("a\n", encoding="utf-8")
    env.provider.script = [
        [use("file_read", {"path": str(env.sub / "a.md")}, 1)],
        [
            use(
                "editor",
                {"command": "create", "path": str(env.sub / "b.md"), "file_text": "x\n"},
                2,
            )
        ],
        [use("goal_report", {"kind": "progress", "code": "step_done"}, 3)],
    ]
    check = env.executor._check_tool

    def refuse_the_editor(it, name, arguments):
        if name == "editor":
            it.stop("tool_refused", "outside_subtree")
            raise IterationStop("tool_refused")
        return check(it, name, arguments)

    monkeypatch.setattr(env.executor, "_check_tool", refuse_the_editor)
    run_one(env)
    assert not (env.sub / "b.md").exists()
    assert _read(env)["outcomes"][-1]["tools"] == ["file_read"]


def test_another_grants_running_iteration_is_not_shown(tmp_path):
    env = make_env(tmp_path)
    hold = _held_iteration(env)
    try:
        assert _read(env)["iteration"]["state"] == "running"
        other = env.executor.board("f" * 32)
        assert other["iteration"]["state"] == "idle" and "tick_used" not in other["iteration"]
    finally:
        hold.set()
    assert env.executor.wait_idle(20)


# --- round 3: one integer, read twice (a seqlock) --------------------------------------------


def _last_load_of_board_seq():
    """The offset of the last attribute load in ``board_seq`` — the seam between the two
    loads of the former (sequence, open writes) pair; the single load now."""
    import dis

    from stiracore.grants.executor import GoalIterationExecutor

    code = GoalIterationExecutor.board_seq.__code__
    loads = [i.offset for i in dis.get_instructions(code) if i.opname == "LOAD_ATTR"]
    return code, loads[-1]


def test_a_write_that_ends_at_the_version_read_is_never_a_mix(tmp_path, monkeypatch):
    """Astra r3 B2, ported: the iteration's end has written everything but its row; the
    reader is scheduled out right inside ``board_seq`` and the write finishes there. The
    read is busy or complete — never iteration #1 ended without its row."""
    import sys

    from stiracore.grants.schema import BoardBusy

    env = make_env(tmp_path, script=[[{"text": "done"}]])
    ex = env.executor
    entered, release = threading.Event(), threading.Event()
    original = ex._outcome

    def hold_outcome(it):
        entered.set()
        assert release.wait(10)
        return original(it)

    monkeypatch.setattr(ex, "_outcome", hold_outcome)
    ex.step(env.grant_id)
    assert entered.wait(10)
    code, offset = _last_load_of_board_seq()
    fired = []

    def trace(frame, event, arg):
        if frame.f_code is code:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == offset and not fired:
                fired.append(True)
                release.set()
                assert ex.wait_idle(10)
        return trace

    sys.settrace(trace)
    try:
        try:
            board = ex.board(env.grant_id)
        except BoardBusy:
            board = None
    finally:
        sys.settrace(None)
        release.set()
    assert ex.wait_idle(10)
    assert board is None or len(board["outcomes"]) == 1, board
    assert len(ex.board(env.grant_id)["outcomes"]) == 1


def test_the_version_check_holds_under_threads(tmp_path):
    """Two writer threads whose board writes overlap (each owns a grant: it sets the summary,
    then — a step later — appends the row of the same number) against two readers: a read
    returns both pieces of one version or is busy; fixed counts, no sleeps."""
    import sys

    from stiracore.grants.schema import BoardBusy

    env = make_env(tmp_path)
    ex = env.executor
    gids = ["a" * 32, "b" * 32]
    writes, reads = 600, 4000
    from collections import deque

    ex._outcomes = deque(maxlen=4 * writes)  # both grants' rows stay (the real one keeps 5)

    # Every ``pause`` writes, both writers stop at a barrier shared with a prober. The
    # prober then reads both boards while no write is open: those reads must succeed and
    # must be of one version. The two racing readers run all along, but on a busy 4-vCPU
    # runner a write can be open at every one of their reads (all "busy"), so the
    # version check is asserted on the prober's deterministic reads (#662).
    pause = 100
    stopped, go = threading.Barrier(3, timeout=30), threading.Barrier(3, timeout=30)

    def writer(gid):
        for n in range(1, writes + 1):
            if n % pause == 0:
                stopped.wait()
                go.wait()
            with ex._board_write():
                with ex._lock:
                    ex._last[gid] = {"state": "idle", "number": n, "end": "done", "detail": "",
                                     "card": "none"}  # fmt: skip
                with ex._lock:  # the second piece, apart from the first
                    ex._outcomes.append((gid, {"number": n, "end": "done", "tick_used": n,
                                               "tools": [], "detail": "", "ts": 0.0}))  # fmt: skip

    mixed, counts = [], {"busy": 0, "good": 0}

    def reader(k):
        for r in range(reads):
            gid = gids[(r + k) % 2]
            try:
                out = ex.board(gid)
            except BoardBusy:
                counts["busy"] += 1
                continue
            last = out["outcomes"][-1]["number"] if out["outcomes"] else 0
            if out["iteration"]["number"] != last:
                mixed.append((gid, out["iteration"]["number"], last))
            else:
                counts["good"] += 1

    probed = []

    def prober():
        for _ in range(writes // pause):
            stopped.wait()
            try:
                for gid in gids:
                    for _attempt in range(100_000):  # busy only while a racing reader
                        try:  # holds the lock for its snapshot (no write is open)
                            out = ex.board(gid)
                            break
                        except BoardBusy:
                            continue
                    last = out["outcomes"][-1]["number"] if out["outcomes"] else 0
                    probed.append(out["iteration"]["number"] == last)
            finally:
                go.wait()

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=writer, args=(g,)) for g in gids]
        threads += [threading.Thread(target=reader, args=(k,)) for k in range(2)]
        threads.append(threading.Thread(target=prober))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    finally:
        sys.setswitchinterval(interval)
    assert not any(thread.is_alive() for thread in threads)
    assert not mixed, mixed[:3]
    assert probed and all(probed) and len(probed) == 2 * (writes // pause)
    assert counts["good"] + counts["busy"] == 2 * reads
    assert ex.board_seq() % 2 == 0 and ex._board_open == 0


def test_wait_idle_waits_for_the_ends_write_to_close(tmp_path, monkeypatch):
    env = make_env(tmp_path, script=[[{"text": "x"}]])
    ex = env.executor
    real = ex._board_write
    gate = threading.Event()

    @contextlib.contextmanager
    def held_write():
        with real():
            yield
            if ex._current is None:  # the end's write, after the iteration is cleared
                assert gate.wait(10)

    monkeypatch.setattr(ex, "_board_write", held_write)
    ex.step(env.grant_id)
    try:
        assert ex.wait_idle(0.5) is False  # the end's write is still open
        assert ex.board_seq() % 2 == 1
    finally:
        gate.set()
    assert ex.wait_idle(10) and ex.board_seq() % 2 == 0


def test_a_write_that_raises_never_leaves_the_version_odd(tmp_path):
    env = make_env(tmp_path)
    ex = env.executor
    for error in (RuntimeError("charge"), KeyboardInterrupt()):
        try:
            with ex._board_write():
                assert ex.board_seq() % 2 == 1
                raise error
        except (RuntimeError, KeyboardInterrupt):
            pass
        assert ex.board_seq() % 2 == 0 and ex._board_open == 0
    assert _read(env)["iteration"]["state"] == "idle"

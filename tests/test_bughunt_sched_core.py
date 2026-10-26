"""#601 (area B: scheduler + core): regressions found by the pre-release review.

Most tests are regressions: they fail on the code before the fix and pass after it.
Some are controls that pass on main as well and pin a spec or an invariant the fixes
must keep (B11 by design, the B1/B2 controls, the B8 owner-thread case, the B16
self-count / single-end checks). No model, no network.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_job_approval_wait import RULES, SENT, FakeChild, _approve, _call, _ended, _wait_job
from test_scheduler import HUMAN, T0, Breaker, Clock, _tick_at

import stiracore.scheduler.event_log as event_log_module
import stiracore.scheduler.scheduler as scheduler_module
from stiracore.control.agent_jobs import GatedJobExecutor
from stiracore.control.job_approvals import JobApprovalWaits
from stiracore.core.approval import ApprovalBinding, OperationLedger, args_hash
from stiracore.core.approval_store import ApprovalStore, ApprovalStoreError
from stiracore.core.checkpoint import Checkpoint, FileCheckpointStore
from stiracore.core.engine import (
    BreakLevel,
    HITLAction,
    HITLDecision,
    HITLRequest,
    StiraEngine,
)
from stiracore.core.neural_state import NeuralState
from stiracore.scheduler.event_log import GENESIS_HASH, SchedulerEventLog
from stiracore.scheduler.jobs import JobError, parse_job
from stiracore.scheduler.schedule import CronExpression, ScheduleError, parse_schedule
from stiracore.scheduler.scheduler import RunOutcome, Scheduler

# --- schedule.py ----------------------------------------------------------------------


@pytest.mark.parametrize("expr", ["² * * * *", "*/² * * * *", "1-² * * * *", "١ * * * *"])
def test_b12_non_ascii_digits_are_an_invalid_cron_not_a_raw_value_error(expr):
    with pytest.raises(ScheduleError):
        CronExpression.parse(expr)
    with pytest.raises(JobError):
        parse_job(
            {
                "job_id": "p",
                "name": "p",
                "action": "probe",
                "schedule": {"type": "cron", "expr": expr},
            },
            now=T0,
            gateway_actions={"probe"},
            agent_jobs=False,
            existing=None,
        )


@pytest.mark.parametrize(
    "schedule",
    [{"type": "once", "at": 10**400}, {"type": "interval", "every_sec": 60, "anchor": 10**400}],
)
def test_b13_a_huge_integer_timestamp_is_a_schedule_error_not_overflow(schedule):
    with pytest.raises(ScheduleError):
        parse_schedule(schedule, default_anchor=T0)


def test_b11_ambiguous_wall_time_fires_once_first_occurrence_by_design():
    """ADR 0008 §1: an ambiguous wall time fires once (first occurrence). Unchanged."""
    tz = ZoneInfo("America/New_York")
    second_0115 = datetime(2026, 11, 1, 1, 15, tzinfo=tz, fold=1).timestamp()
    nxt = CronExpression.parse("* * * * *").next_after(second_0115, tz)
    assert datetime.fromtimestamp(nxt, tz).strftime("%H:%M") == "02:00"


# --- checkpoint.py --------------------------------------------------------------------


def test_b4_a_dict_inside_a_tuple_round_trips_and_save_does_not_raise(tmp_path):
    store = FileCheckpointStore(tmp_path)
    checkpoint = Checkpoint(
        neural_state=NeuralState(),
        conversation_history=[{"x": ({"a": 1}, b"\x00")}],
        pending_tool_calls=[],
        timestamp=T0,
        thread_id="t",
    )
    checkpoint_id = store.save(checkpoint)
    loaded = store.load(checkpoint_id)
    assert loaded.conversation_history == [{"x": [{"a": 1}, b"\x00"]}]
    assert store.list() == [checkpoint_id]


# --- approval.py / approval_store.py --------------------------------------------------


def test_b1_a_late_not_applied_answer_never_deletes_a_newer_record():
    ledger = OperationLedger()
    kwargs = {"agent_id": "a", "tool_name": "t", "digest": "d", "tool_use_id": "u"}
    assert ledger.mark_started("k", request_id="r1", **kwargs)
    observed = ledger.get("k")
    ledger.forget("k")  # another path proved it and a new execution claimed + committed
    assert ledger.mark_started("k", request_id="r2", **kwargs)
    ledger.finish("k", success=True)
    ledger.reconcilers["t"] = lambda tool_input, key: False  # the late answer for `observed`
    assert ledger.reconcile(observed, {}) is None  # unknown again, not "not applied"
    assert ledger.get("k").state == "committed"
    assert ledger.mark_started("k", request_id="r3", **kwargs) is False


def test_b1_same_clock_reading_and_same_state_never_deletes_the_newer_claim():
    """Review M1: frozen clock, both records ``started``: the late False must not match."""
    ledger = OperationLedger(now_fn=lambda: T0)
    kwargs = {"agent_id": "a", "tool_name": "t", "digest": "d", "tool_use_id": "u"}
    assert ledger.mark_started("k", request_id="r1", **kwargs)
    observed = ledger.get("k")
    ledger.forget("k")
    assert ledger.mark_started("k", request_id="r1", **kwargs)  # same request id too
    newer = ledger.get("k")
    assert (newer.state, newer.request_id) == (observed.state, observed.request_id)
    assert newer.updated_at > observed.updated_at  # a new generation, same clock
    ledger.reconcilers["t"] = lambda tool_input, key: False
    assert ledger.reconcile(observed, {}) is None
    assert ledger.get("k") == newer
    assert ledger.mark_started("k", request_id="r2", **kwargs) is False


def test_b1_stamps_keep_increasing_after_reopening_a_file_ledger(tmp_path):
    path = tmp_path / "ops.db"
    first = OperationLedger(path, now_fn=lambda: T0)
    kwargs = {"agent_id": "a", "tool_name": "t", "digest": "d", "tool_use_id": "u"}
    first.mark_started("k", request_id=None, **kwargs)
    stamp = first.get("k").updated_at
    first.close()
    second = OperationLedger(path, now_fn=lambda: T0)
    second.forget("k")
    second.mark_started("k", request_id=None, **kwargs)
    assert second.get("k").updated_at > stamp


def test_b1_reconcile_still_forgets_the_record_it_observed():
    ledger = OperationLedger(reconcilers={"t": lambda tool_input, key: False})
    ledger.mark_started(
        "k", agent_id="a", tool_name="t", digest="d", tool_use_id="u", request_id=None
    )
    assert ledger.reconcile(ledger.get("k"), {}) is False
    assert ledger.get("k") is None


def test_b6_binding_with_nan_expiry_or_nan_now_is_expired():
    binding = ApprovalBinding("r", "a", "t", args_hash({}), float("nan"))
    assert binding.check(agent_id="a", tool_name="t", tool_input={}, now=0.0) == "approval expired"
    binding = ApprovalBinding("r", "a", "t", args_hash({}), T0)
    assert binding.check(agent_id="a", tool_name="t", tool_input={}, now=float("nan")) == (
        "approval expired"
    )


def test_b2_a_busy_database_does_not_stop_the_store_for_good(tmp_path):
    path = tmp_path / "approvals.db"
    store = ApprovalStore(path)
    assert store.healthy
    store._conn.execute("PRAGMA busy_timeout=50")
    other = sqlite3.connect(str(path), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        ok, error = store.verify_chain()
        assert ok is False and "busy" in (error or "")
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert store.healthy  # transient: usable again once the lock is gone
    assert store.verify_chain() == (True, None)


def test_b2_only_busy_is_transient_any_other_store_error_stays_sticky(monkeypatch):
    store = ApprovalStore(None)

    def refuse(operation):
        raise ApprovalStoreError("schema mismatch")  # raised without _fail/_broken

    monkeypatch.setattr(store, "_verified", refuse)
    assert store.verify_chain() == (False, "schema mismatch")
    assert not store.healthy


def test_b2_tampering_stays_sticky(tmp_path):
    path = tmp_path / "approvals.db"
    store = ApprovalStore(path)
    store.submit(request_id="r", agent_id="a", tool_name="t", tool_input={}, expires_at=T0 * 2)
    other = sqlite3.connect(str(path), isolation_level=None)
    other.execute("UPDATE approval_events SET body = '{}' WHERE seq = 1")
    other.close()
    assert store.verify_chain()[0] is False
    assert not store.healthy


def test_b3_a_non_object_payload_is_refused_at_submit_and_the_store_stays_healthy():
    store = ApprovalStore(None)
    with pytest.raises(ApprovalStoreError):
        store.submit(
            request_id="r",
            agent_id="a",
            tool_name="t",
            tool_input=[],
            expires_at=T0 * 2,
            store_payload=True,
        )
    assert store.healthy and store.get("r") is None


# --- engine.py ------------------------------------------------------------------------


def test_b6_expiry_zero_stays_expired_and_nan_is_refused(tmp_path):
    seen: list = []
    engine = StiraEngine(
        on_hitl_request=lambda r: (
            seen.append(r) or HITLDecision(HITLAction.APPROVE, request_id=r.request_id)
        ),
        now_fn=lambda: T0,
        checkpoint_store=FileCheckpointStore(tmp_path),
    )

    def request(**kw):
        return HITLRequest(
            tool_name="t", tool_input={}, break_level=BreakLevel.CRITICAL, reason="r", **kw
        )

    zero = engine.request_hitl(request(expires_at=0.0))
    assert zero.action == HITLAction.REJECT and zero.reason == "approval expired"
    nan = engine.request_hitl(request(expires_at=math.nan))
    assert nan.action == HITLAction.REJECT
    huge = engine.request_hitl(request(expires_at=10**400))  # review M2: no OverflowError
    assert huge.action == HITLAction.REJECT
    assert seen == []  # none of them was put in front of a person
    count = len(seen)
    default = engine.request_hitl(request())
    assert default.action == HITLAction.APPROVE and len(seen) == count + 1
    assert seen[-1].expires_at == T0 + engine.config.hitl_timeout_sec


def _latched_warning_engine(tmp_path):
    """WARNING is latched, stability is back above its exit: the next evaluation clears it."""
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    engine._warning_active, engine._active_level = True, BreakLevel.WARNING
    return engine


def test_b8_a_normal_query_never_waits_for_a_held_lock(tmp_path):
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    result: dict = {}
    with engine.state_lock:  # e.g. a heartbeat tick / pre_turn in progress
        worker = threading.Thread(
            target=lambda: result.update(level=engine.breaker_status().level), daemon=True
        )
        worker.start()
        worker.join(2.0)
        assert not worker.is_alive()  # a settings save is never blocked (test_tool_policy)
    assert result["level"] == BreakLevel.NORMAL


def test_b8_a_query_under_a_held_lock_commits_nothing_and_does_not_report_recovery(
    tmp_path, monkeypatch
):
    """WARNING committed, stability already above its exit: the copy says NORMAL, but
    while the holder has not committed that, the query answers WARNING (fail closed)
    and leaves the latches alone. Main: the unlocked query cleared the latch itself."""
    engine = _latched_warning_engine(tmp_path)
    peeked = threading.Event()
    real_evaluate = StiraEngine._evaluate_breaker

    def spy(self, *args):
        level = real_evaluate(self, *args)
        if self is not engine:
            peeked.set()
        return level

    monkeypatch.setattr(StiraEngine, "_evaluate_breaker", spy)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(level=engine.breaker_status().level), daemon=True
    )
    with engine.state_lock:  # e.g. a heartbeat tick / pre_turn in progress
        worker.start()
        assert peeked.wait(5.0)
        assert engine._warning_active is True and engine._active_level == BreakLevel.WARNING
    worker.join(5.0)
    assert not worker.is_alive() and result["level"] == BreakLevel.WARNING
    assert engine._warning_active is True and engine._active_level == BreakLevel.WARNING
    assert engine.breaker_status().level == BreakLevel.NORMAL  # lock free: committed
    assert engine._warning_active is False and engine._active_level == BreakLevel.NORMAL


def test_b8_a_query_in_the_holding_thread_still_commits(tmp_path):
    engine = _latched_warning_engine(tmp_path)
    with engine.state_lock:  # re-entrant: the owner evaluates for real
        assert engine.breaker_status().level == BreakLevel.NORMAL
        assert engine._warning_active is False


def _coherence_engine(tmp_path):
    """COHERENCE committed by a real evaluation: bcScore low, stability fine."""
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    engine.state.bc_score = 0.1
    assert engine.breaker_status().level == BreakLevel.COHERENCE
    return engine


class _InterleavedState:
    """The engine's state, read by the query thread: right after the query reads
    ``stability`` the lock holder runs its update (``between``), then ``bc_score`` is read."""

    def __init__(self, real, between):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_between", between)
        object.__setattr__(self, "_holder", threading.get_ident())

    def __getattr__(self, name):
        value = getattr(self._real, name)
        if name == "stability" and threading.get_ident() != self._holder and self._between:
            between, self.__dict__["_between"] = self._between, None
            between()
        return value

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


@pytest.mark.parametrize("holder_committed", [False, True])
def test_b8_mixed_reads_during_an_update_never_look_recovered(tmp_path, holder_committed):
    """Review (Astra major): the query reads stability (fine), the holder then moves the
    stability side to non-NORMAL and recovers coherence, the query reads bcScore
    (recovered). The mixed inputs evaluate to NORMAL; the answer must not be NORMAL."""
    engine = _coherence_engine(tmp_path)
    go, done = threading.Event(), threading.Event()
    result: dict = {}

    def holder_update():  # runs in the query thread's read, signalled to the holder
        go.set()
        assert done.wait(5.0)

    engine.state = _InterleavedState(engine.state, holder_update)
    worker = threading.Thread(
        target=lambda: result.update(level=engine.breaker_status().level), daemon=True
    )
    with engine.state_lock:
        worker.start()
        assert go.wait(5.0)
        # The real update of a turn: stimulus → stability drops, coherence recovers.
        engine.state.stability = 0.2
        engine.state.bc_score = 0.95
        if holder_committed:
            engine._check_breaker()  # commits CRITICAL
        done.set()
        if not holder_committed:
            worker.join(0.5)
            # The query must not have answered NORMAL while the holder is mid-update.
            assert result.get("level", BreakLevel.COHERENCE) != BreakLevel.NORMAL
    worker.join(5.0)
    assert not worker.is_alive()
    assert result["level"] != BreakLevel.NORMAL
    expected = BreakLevel.CRITICAL if holder_committed else BreakLevel.COHERENCE
    assert result["level"] == expected


def test_b8_critical_latched_query_under_a_held_lock(tmp_path, monkeypatch):
    """Review (Opus m-2): CRITICAL latched with protected turns served, a query from
    another thread while the lock is held: no error, CRITICAL, departure published,
    latches unchanged."""
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    engine._active_level, engine._critical_turns = BreakLevel.CRITICAL, 2
    departures: list = []
    engine.on_departure = lambda: departures.append(1)
    peeked = threading.Event()
    real_evaluate = StiraEngine._evaluate_breaker

    def spy(self, *args):
        if self is not engine:
            peeked.set()
        return real_evaluate(self, *args)

    monkeypatch.setattr(StiraEngine, "_evaluate_breaker", spy)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(level=engine.breaker_status().level), daemon=True
    )
    with engine.state_lock:
        worker.start()
        assert peeked.wait(5.0)  # the copy path ran (the lock was not free)
    worker.join(5.0)
    assert not worker.is_alive() and result["level"] == BreakLevel.CRITICAL
    assert departures  # published before the answer
    assert engine._active_level == BreakLevel.CRITICAL and engine._critical_turns == 2


def _committed_but_recovered(tmp_path, level):
    """``level`` committed; the inputs already meet its exit (the copy says NORMAL)."""
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    engine.state.bc_score = 0.95
    engine._active_level = level
    if level == BreakLevel.WARNING:
        engine._warning_active = True
    elif level == BreakLevel.COHERENCE:
        engine._coherence_active = True
    else:  # CRITICAL with its protected turns served
        engine._critical_turns = engine.config.critical_cooldown_turns
    return engine


@pytest.mark.parametrize("hook", ["callback", "real"])
@pytest.mark.parametrize("level", [BreakLevel.WARNING, BreakLevel.CRITICAL, BreakLevel.COHERENCE])
def test_b8_a_settings_save_never_waits_for_a_turn_and_is_not_loosened(tmp_path, level, hook):
    """Review (r5 major): a committed non-NORMAL level, recovered inputs, and a lock
    holder that waits for the save itself. The real ``/api/config/tools`` save must end
    within 5 s (no wait for ``state_lock`` on any branch, the departure publish included)
    and must see the committed level, not NORMAL: loosening is refused (403)."""
    from flask import Flask

    from stiracore.observability.config_api import ConfigStore, create_config_blueprint
    from stiracore.tools import tool_policy, tool_registry
    from stiracore.tools.tool_config import ToolConfig
    from stiracore.yolo import session_allow

    engine = _committed_but_recovered(tmp_path, level)
    departures: list = []
    if hook == "callback":
        engine.on_departure = lambda: departures.append(threading.get_ident())
    else:  # on_departure=None: the process's real allowance table (production path)
        assert engine.on_departure is None
    before = session_allow.table().snapshot().departures
    read = next(
        n
        for n in sorted(set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS))
        if tool_policy.category_of(n) == "read"
    )
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(mode="custom", enabled_tools={read}, ask_tools={read}, path=store.tool_path).save()
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: engine.breaker_status().level))
    client = app.test_client()
    result: dict = {}
    saver = threading.Thread(
        target=lambda: result.update(
            response=client.post(
                "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
            )
        ),
        daemon=True,
    )
    with engine.state_lock:  # the holder (a turn) waits for the save to finish
        saver.start()
        saver.join(timeout=5)
        assert not saver.is_alive(), "save blocked on the engine lock"
    response = result["response"]
    assert response.status_code == 403
    body = response.get_json()
    assert body["error_code"] == "breaker_active" and body["breaker_level"] == level.value
    if hook == "callback":
        assert departures and departures[0] == saver.ident  # published by the query itself
    else:
        assert session_allow.table().snapshot().departures > before
    assert store.load_tools().policy_of(read) == "ask"  # nothing was loosened
    assert engine._active_level == level  # and nothing was committed by the query


def test_b8_concurrent_departure_bumps_from_holder_and_query_are_all_counted(tmp_path):
    """Review (r6): bumps from the commit path (under ``state_lock``) and the lock-free
    query path run concurrently on the real table; none is lost or torn. An extra bump is
    safe by construction: it only ends allowances sooner."""
    from stiracore.yolo.session_allow import AllowanceTable

    table = AllowanceTable()
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    engine.on_departure = table.bump_departure
    rounds, start = 200, threading.Barrier(4)

    def committing():
        start.wait()
        for _ in range(rounds):
            engine._publish_departure()  # the holder's path: under state_lock

    def querying():
        start.wait()
        for _ in range(rounds):
            engine._bump_departure()  # the lock-free query path

    threads = [threading.Thread(target=f) for f in (committing, committing, querying, querying)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert not any(thread.is_alive() for thread in threads)
    assert table.snapshot().departures == 4 * rounds


def test_b8_a_holder_commit_publishes_its_departure_too(tmp_path):
    """Review (r5 c): a departure is also published by the holder's own commit (the
    ``_check_breaker`` path under ``state_lock``), independent of any query."""
    engine = StiraEngine(
        state=NeuralState(stability=0.9), checkpoint_store=FileCheckpointStore(tmp_path)
    )
    departures: list = []
    engine.on_departure = lambda: departures.append(1)
    with engine.state_lock:
        engine.state.stability = 0.2
        assert engine._check_breaker() == BreakLevel.CRITICAL
        assert len(departures) == 1


def test_b8_the_copy_carries_exactly_the_attributes_the_rule_uses():
    """Review (Opus m-2): ``_LATCH_FIELDS`` == the ``self.`` attributes of
    ``_evaluate_breaker`` (a new latch would otherwise be an AttributeError on the copy)."""
    import inspect
    import re

    from stiracore.core import engine as engine_module

    source = inspect.getsource(StiraEngine._evaluate_breaker)
    used = set(re.findall(r"self\.(\w+)", source))
    assert used == set(engine_module._LATCH_FIELDS)


# --- event_log.py ---------------------------------------------------------------------


def test_b9_a_short_write_is_a_failed_append(tmp_path, monkeypatch):
    log = SchedulerEventLog(tmp_path / "events.jsonl")
    log.open()
    monkeypatch.setattr(event_log_module.os, "write", lambda fd, data: 0)  # no progress
    with pytest.raises(event_log_module.SchedulerLogError):
        log.append("tick", {"n": 1}, T0)
    assert not log.healthy


def test_b9_partial_progress_is_completed(tmp_path, monkeypatch):
    log = SchedulerEventLog(tmp_path / "events.jsonl")
    log.open()
    real_write = os.write
    monkeypatch.setattr(
        event_log_module.os,
        "write",
        lambda fd, data: real_write(fd, bytes(data)[: max(1, len(data) // 2)]),
    )
    log.append("tick", {"n": 1}, T0)
    monkeypatch.undo()
    assert log.verify() == (True, None)
    reopened = SchedulerEventLog(tmp_path / "events.jsonl")
    assert [e["body"] for e in reopened.open()] == [{"n": 1}]


def test_b10_the_log_file_is_created_before_the_head_moves_ahead(tmp_path, monkeypatch):
    """Review m4: observed through ``os.open`` / ``os.replace`` (both exist on main)."""
    path = tmp_path / "events.jsonl"
    log = SchedulerEventLog(path)
    log.open()
    order: list[str] = []
    real_open, real_replace = os.open, os.replace

    def spy_open(target, *args, **kwargs):
        if str(target) == str(path):
            order.append("log_open")
        return real_open(target, *args, **kwargs)

    def spy_replace(src, dst, *args, **kwargs):
        if str(dst) == str(log.head_path):
            order.append("head_replace")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(event_log_module.os, "open", spy_open)
    monkeypatch.setattr(event_log_module.os, "replace", spy_replace)
    log.append("tick", {"n": 1}, T0)
    assert order == ["log_open", "head_replace"]


def test_b10_a_crash_right_after_the_head_moved_opens_as_recoverable(tmp_path, monkeypatch):
    """The crash window on main: head replaced, log file never created."""
    path = tmp_path / "events.jsonl"
    log = SchedulerEventLog(path)
    log.open()
    real_replace = os.replace

    def replace_then_crash(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        if str(dst) == str(log.head_path):
            raise OSError("process died right after the head moved")

    monkeypatch.setattr(event_log_module.os, "replace", replace_then_crash)
    with pytest.raises(event_log_module.SchedulerLogError):
        log.append("tick", {"n": 1}, T0)
    monkeypatch.undo()
    reopened = SchedulerEventLog(path)
    reopened.open()  # main: "log file is missing but its head anchor exists"
    assert reopened.healthy and reopened.needs_recovery


def test_b10_crash_after_the_first_head_write_opens_as_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    log = SchedulerEventLog(path)
    log.open()

    real = event_log_module._write_all
    calls = []

    def crash(fd, data):
        calls.append(data)
        if len(calls) == 2:  # 1st: the head, 2nd: the log line
            raise OSError("crash between head and append")
        real(fd, data)

    monkeypatch.setattr(event_log_module, "_write_all", crash)
    with pytest.raises(event_log_module.SchedulerLogError):
        log.append("tick", {"n": 1}, T0)
    monkeypatch.undo()
    assert path.exists() and path.read_bytes() == b""
    head = log.head_path.read_text()
    assert '"base_seq": 0' in head and GENESIS_HASH in head
    reopened = SchedulerEventLog(path)
    assert reopened.open() == []
    assert reopened.healthy and reopened.needs_recovery


# --- scheduler.py ---------------------------------------------------------------------


@pytest.fixture
def make_world(tmp_path):
    made = []

    def make(dispatch=None):
        SENT.clear()
        clock = Clock()
        store = ApprovalStore(None, now_fn=clock)
        waits = JobApprovalWaits(store, now_fn=clock)
        script = {"first": [_call({"path": "notes.md"})], "replay": [_call({"path": "notes.md"})]}

        def build(ctx, names, guards):
            return FakeChild(guards, script["first"], script["replay"])

        executor = GatedJobExecutor(build, breaker_level=Breaker(), rule_of=RULES.get, waits=waits)
        scheduler = Scheduler(
            tmp_path,
            now_fn=clock,
            breaker_level=Breaker(),
            agent_executor=executor,
            dispatch=dispatch or (lambda work: work()),
            approval_waits=waits,
        )
        scheduler.start(background=False)
        made.append(scheduler)
        return scheduler, store, clock, executor, waits

    yield make
    for scheduler in made:
        scheduler.stop()


def _waiting(scheduler, clock, job_id="digest", at=T0 + 3600):
    scheduler.create_job(_wait_job(job_id=job_id, name=job_id), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, at)
    run = next(r for r in scheduler.runs() if r["job_id"] == job_id)
    request = next(w for w in scheduler.waiting_runs() if w["job_id"] == job_id)
    return run["run_id"], request["request_id"]


def test_b14_an_unreadable_approval_store_ends_the_waiting_run_not_the_tick(make_world):
    scheduler, _store, clock, _executor, waits = make_world()
    _run_id, _request = _waiting(scheduler, clock)

    def broken(request_id):
        raise RuntimeError("store unreadable")

    waits.state_of = broken
    scheduler.tick()  # must not raise
    run = _ended(scheduler)
    assert run["status"] == "cancelled" and run["reason"] == "approval_unavailable"


def test_b15_a_run_waiting_for_approval_is_not_evicted_and_still_settles(make_world, monkeypatch):
    monkeypatch.setattr(scheduler_module, "RUN_HISTORY", 1)
    scheduler, store, clock, _executor, _waits = make_world()
    first_run, first_request = _waiting(scheduler, clock, "digest")
    second_run, _ = _waiting(scheduler, clock, "digest2", at=T0 + 7200)
    assert first_run in scheduler._runs
    assert first_run in scheduler._reserved
    store.decide(first_request, "reject", approver=HUMAN)
    scheduler.note_approval_rejected(first_request)
    deadline = time.monotonic() + 3
    while first_run in scheduler._reserved and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first_run not in scheduler._reserved  # the reservation was settled
    assert scheduler._runs[first_run]["status"] == "cancelled"
    assert second_run in scheduler._runs


def test_b16_a_cancel_between_resume_and_dispatch_stops_the_continuation(make_world):
    queued: list = []
    scheduler, store, clock, executor, _waits = make_world(dispatch=queued.append)
    resumed: list = []
    real_resume = executor.resume
    executor.resume = lambda ctx, rid: resumed.append(rid) or real_resume(ctx, rid)
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    queued.pop(0)()
    (waiting,) = scheduler.waiting_runs()
    request_id, run_id = waiting["request_id"], waiting["run_id"]
    assert _approve(store, request_id, {"path": "notes.md"})
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is True
    scheduler.cancel_run(run_id, by=HUMAN)
    queued.pop(0)()  # the continuation segment runs only now
    # The executor (and so the child model call) is not entered at all; its own tool
    # guards were only a second line of defence.
    assert resumed == [] and SENT == []
    run = _ended(scheduler)
    assert run["status"] == "cancelled"


def test_b16_an_approved_continuation_is_not_refused_for_its_own_reservation(make_world):
    """Review M4-1: the job's whole day budget is this run's reservation, one run a day."""
    scheduler, store, clock, _executor, _waits = make_world()
    budget = {"max_tokens_per_run": 500, "max_tokens_per_day": 500, "max_runs_per_day": 1}
    scheduler.create_job(_wait_job(budget=budget), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    (waiting,) = scheduler.waiting_runs()
    assert _approve(store, waiting["request_id"], {"path": "notes.md"})
    assert scheduler.resume_waiting_run(waiting["request_id"], by=HUMAN)["resumed"] is True
    run = _ended(scheduler)
    assert run["status"] == "completed", run
    assert SENT == [{"tool": "memory_read", "input": {"path": "notes.md"}}]


def test_b16_a_refused_continuation_ends_once_and_charges_the_first_segment(
    make_world, monkeypatch
):
    """Review M4-2/3: one end event, no double settle, the parked segment's tokens."""
    queued: list = []
    scheduler, store, clock, _executor, _waits = make_world(dispatch=queued.append)
    monkeypatch.setattr(
        FakeChild,
        "event_loop_metrics",
        type("M", (), {"accumulated_usage": {"totalTokens": 120}})(),
        raising=False,
    )
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    queued.pop(0)()
    (waiting,) = scheduler.waiting_runs()
    run_id = waiting["run_id"]
    assert scheduler._active[run_id].segment_tokens == 120
    assert _approve(store, waiting["request_id"], {"path": "notes.md"})
    scheduler.resume_waiting_run(waiting["request_id"], by=HUMAN)
    scheduler.cancel_run(run_id, by=HUMAN)
    queued.pop(0)()
    run = _ended(scheduler)
    ends = [
        e
        for e in scheduler.log.events()
        if e["kind"] in ("run_completed", "run_failed", "run_cancelled")
        and e["body"]["run_id"] == run_id
    ]
    assert len(ends) == 1 and ends[0]["body"]["tokens"] == 120
    assert run["status"] == "cancelled" and run["tokens"] == 120
    assert run_id not in scheduler._reserved and run_id not in scheduler._active
    day = run["charge_day"]
    assert scheduler._usage[("digest", day)]["tokens"] == 120
    assert scheduler._global_tokens[day] == 120


class _UnknownUsageOnPark:
    """Wraps the job executor: the first segment parks with ``usage_known=False``."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, ctx):
        outcome = self.inner(ctx)
        if outcome.status == "awaiting_approval":
            outcome = RunOutcome(outcome.status, outcome.reason, 7, usage_known=False)
        return outcome

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.parametrize(
    "path", ["cancel_while_parked", "refused_continuation", "approved_completion"]
)
def test_review_a_parked_segment_with_unknown_usage_is_charged_its_reservation(make_world, path):
    queued: list = []
    scheduler, store, clock, executor, _waits = make_world(dispatch=queued.append)
    scheduler._agent_executor = _UnknownUsageOnPark(executor)
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    queued.pop(0)()
    (waiting,) = scheduler.waiting_runs()
    run_id = waiting["run_id"]
    reserved = scheduler._reserved[run_id][2]
    assert reserved > 7
    if path == "cancel_while_parked":
        scheduler.cancel_run(run_id, by=HUMAN)
    else:
        assert _approve(store, waiting["request_id"], {"path": "notes.md"})
        scheduler.resume_waiting_run(waiting["request_id"], by=HUMAN)
        if path == "refused_continuation":
            scheduler.cancel_run(run_id, by=HUMAN)
        queued.pop(0)()  # approved_completion: the continuation reports known usage
    run = _ended(scheduler)
    status = "completed" if path == "approved_completion" else "cancelled"
    assert run["status"] == status, run
    assert run["tokens"] >= reserved  # not 7: the parked usage was not known for sure
    day = run["charge_day"]
    assert scheduler._usage[("digest", day)]["tokens"] == run["tokens"]
    assert scheduler._global_tokens[day] == run["tokens"]
    assert run_id not in scheduler._reserved and run_id not in scheduler._active


def test_b17_a_continuation_that_raises_after_an_approval_stop_is_charged_its_reservation(
    make_world,
):
    scheduler, store, clock, executor, _waits = make_world()
    run_id, request_id = _waiting(scheduler, clock)
    reserved = scheduler._reserved[run_id][2]
    assert reserved > 0

    def resume(ctx, rid):
        ctx.require_approval("memory_read", {"path": "x"})
        raise RuntimeError("child failed after asking")

    executor.resume = resume
    assert _approve(store, request_id, {"path": "notes.md"})
    scheduler.resume_waiting_run(request_id, by=HUMAN)
    run = _ended(scheduler)
    assert run["status"] == "failed" and run["reason"] == "approval_required"
    assert run["tokens"] >= reserved

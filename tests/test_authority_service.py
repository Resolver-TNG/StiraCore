"""W1 (#296, ADR 0011 §5 / §13 item 2): the process-wide authority service.

Persisted epoch, unhealthy latch, lock-free kill flag, in-flight set, epoch sources, the
optional consume fields and the YOLO injection. No model is called; no server is started
except one app build without a listener.
"""

from __future__ import annotations

import dataclasses
import errno
import itertools
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore import authority
from stiracore.authority import (
    AuthorityBoundary,
    AuthorityError,
    AuthorityService,
    ClockPair,
    GrantRecord,
    Receipt,
    TtlPair,
    epoch_store,
)
from stiracore.authority import incarnation as process_incarnation

GID = "a" * 32
RID = "e" * 32
RECEIPT = Receipt(event_id=f"op:{'b' * 32}:decision", seq=7, hash="c" * 64)


def record(service, kind="yolo", generation=1, state="active", grant_id=GID):
    return GrantRecord(
        kind=kind,
        grant_id=grant_id,
        incarnation=service.incarnation,
        generation=generation,
        state=state,
    )


def issue(service, **kw):
    return service.issue_permit(record(service), kind="yolo", receipt=RECEIPT, **kw)


def stored(data_dir):
    return json.loads((data_dir / epoch_store.EPOCH_FILE).read_text())["records"]


@pytest.fixture
def service(tmp_path):
    made = AuthorityService(tmp_path)
    yield made
    # Nothing left behind (and never a hang); a test may leave it unhealthy on purpose.
    assert made.flush_result(timeout=5) in {"clean", "unhealthy"}


@pytest.fixture(autouse=True)
def _no_process_service():
    yield
    authority.uninstall()


# --- incarnation ----------------------------------------------------------------------


def test_one_incarnation_shared_with_the_heartbeat_and_not_settable(service):
    from stiracore.heartbeat import daemon

    assert service.incarnation == daemon.INCARNATION == process_incarnation.current()
    assert len(service.incarnation) == 32
    with pytest.raises(AuthorityError, match="incarnation_not_settable"):
        service.incarnation = "f" * 32
    # The plain boundary (YOLO's private one in unit tests) keeps its own random value.
    assert AuthorityBoundary().incarnation != service.incarnation


def test_the_incarnation_is_never_written(service, tmp_path):
    service.narrow("tool_rules_saved")
    assert service.incarnation.encode() not in (tmp_path / epoch_store.EPOCH_FILE).read_bytes()
    assert service.incarnation not in json.dumps(service.status())


# --- persisted epoch ------------------------------------------------------------------


def test_a_first_start_writes_epoch_one_with_mode_0600(tmp_path):
    service = AuthorityService(tmp_path)
    assert service.epoch == 1 and service.unhealthy_reason is None
    (first,) = stored(tmp_path)
    assert first["seq"] == 1 and first["epoch"] == 1 and first["prev"] == "0" * 64
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(tmp_path / epoch_store.EPOCH_FILE).st_mode)
        assert mode == 0o600


def test_each_start_continues_from_the_stored_epoch_plus_one(tmp_path):
    first = AuthorityService(tmp_path)
    first.narrow("tool_rules_saved")
    first.narrow("tool_rules_saved")
    assert first.epoch == 3
    second = AuthorityService(tmp_path)
    assert second.epoch == 4  # never reused
    records = stored(tmp_path)
    assert [r["seq"] for r in records] == [1, 2, 3, 4]
    assert [r["epoch"] for r in records] == [1, 2, 3, 4]
    for before, after in itertools.pairwise(records):
        assert after["prev"] == before["hash"]


def test_the_file_keeps_only_the_newest_records_and_still_verifies(tmp_path):
    service = AuthorityService(tmp_path)
    for _ in range(epoch_store.KEEP_RECORDS + 5):
        service.narrow("tool_rules_saved")
    records = stored(tmp_path)
    assert len(records) == epoch_store.KEEP_RECORDS
    assert records[-1]["epoch"] == service.epoch
    again = AuthorityService(tmp_path)
    assert again.unhealthy_reason is None and again.epoch == service.epoch + 1


def test_every_write_is_flushed_and_replaced_atomically(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(epoch_store, "_flush", lambda fd: calls.append("flush"))
    monkeypatch.setattr(
        epoch_store.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))
    )
    monkeypatch.setattr(epoch_store, "fsync_dir", lambda d, f=None: calls.append("dir"))
    AuthorityService(tmp_path)
    assert calls == ["flush", "replace", "dir"]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def _rewrite(path, mutate):
    data = json.loads(path.read_text())
    mutate(data["records"])
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def _tamper_epoch(records):
    records[-1]["epoch"] += 5  # hash no longer matches


def _drop_middle(records):
    del records[1]


def _break_link(records):
    last = records[-1]
    last["prev"] = "9" * 64
    last["hash"] = epoch_store._digest(last["seq"], last["epoch"], last["prev"])


def _drop_first(records):
    del records[0]


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (_tamper_epoch, "epoch_store_hash_mismatch"),
        (_drop_middle, "epoch_store_sequence_gap"),
        (_break_link, "epoch_store_hash_mismatch"),
        (_drop_first, "epoch_store_sequence_gap"),
    ],
)
def test_a_broken_chain_makes_the_service_unhealthy(tmp_path, mutate, reason):
    first = AuthorityService(tmp_path)
    first.narrow("tool_rules_saved")
    first.narrow("tool_rules_saved")
    path = tmp_path / epoch_store.EPOCH_FILE
    _rewrite(path, mutate)
    before = path.read_bytes()
    service = AuthorityService(tmp_path)
    assert service.unhealthy_reason == reason
    assert service.status()["healthy"] is False
    assert service.status()["unhealthy_reason"] == reason
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        issue(service)
    service.narrow("tool_rules_saved")
    assert path.read_bytes() == before  # a broken file is never overwritten


@pytest.mark.parametrize(
    "content, reason",
    [
        (b"\x00\xff garbage", "epoch_store_malformed"),
        (b"{}", "epoch_store_malformed"),
        (b'{"v": 1, "records": []}', "epoch_store_malformed"),
        (b'{"v": 2, "records": [{"seq": 1}]}', "epoch_store_malformed"),
    ],
)
def test_an_unreadable_file_at_start_is_unhealthy(tmp_path, content, reason):
    path = tmp_path / epoch_store.EPOCH_FILE
    path.write_bytes(content)
    os.chmod(path, 0o600)
    service = AuthorityService(tmp_path)
    assert service.unhealthy_reason == reason
    assert path.read_bytes() == content


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes and symlinks")
def test_an_unsafe_or_unreadable_file_is_unhealthy(tmp_path):
    AuthorityService(tmp_path)
    path = tmp_path / epoch_store.EPOCH_FILE
    os.chmod(path, 0o644)
    assert AuthorityService(tmp_path).unhealthy_reason == "epoch_store_unsafe"
    os.chmod(path, 0o600)
    target = tmp_path / "elsewhere"
    path.rename(target)
    path.symlink_to(target)
    assert AuthorityService(tmp_path).unhealthy_reason == "epoch_store_unsafe"
    path.unlink()
    target.rename(path)
    if os.geteuid() != 0:
        os.chmod(path, 0o000)
        try:
            assert AuthorityService(tmp_path).unhealthy_reason == "epoch_store_unreadable"
        finally:
            os.chmod(path, 0o600)
    assert AuthorityService(tmp_path).unhealthy_reason is None


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_a_failed_write_fails_closed_and_a_retry_does_not_undo_it(tmp_path, monkeypatch, code):
    service = AuthorityService(tmp_path)
    current = record(service)
    permit = issue(service)
    real_write = epoch_store.write

    def failing(*_a, **_k):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(epoch_store, "write", failing)
    service.narrow("tool_rules_saved")
    assert service.epoch == 2  # the narrowing itself is in effect (memory first)
    assert service.unhealthy_reason == "epoch_store_write_failed"
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        service.consume(permit, current, kind="yolo")
    monkeypatch.setattr(epoch_store, "write", real_write)  # the disk is fine again
    service.narrow("tool_rules_saved")
    service.flush()
    assert service.unhealthy_reason == "epoch_store_write_failed"
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        issue(service)
    assert stored(tmp_path)[-1]["epoch"] == 1  # nothing written after the failure


def test_a_failed_write_at_start_is_unhealthy(tmp_path, monkeypatch):
    def failing(*_a, **_k):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(epoch_store, "write", failing)
    service = AuthorityService(tmp_path)
    assert service.unhealthy_reason == "epoch_store_write_failed"
    assert not (tmp_path / epoch_store.EPOCH_FILE).exists()


# --- the kill flag ----------------------------------------------------------------------


def test_kill_is_checked_first_and_advances_the_epoch(service):
    current = record(service)
    permit = issue(service)
    before = service.epoch
    service.kill()
    assert service.killed and service.epoch == before + 1
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch
    with pytest.raises(AuthorityError, match="killed"):
        service.consume(permit, current, kind="yolo")
    with pytest.raises(AuthorityError, match="killed"):
        # First: even a malformed call is answered with the kill, not a type error.
        service.consume(None, None, kind="nope")
    with pytest.raises(AuthorityError, match="killed"):
        issue(service)
    assert service.status()["killed"] is True and service.status()["healthy"] is False


def test_kill_takes_effect_while_the_scheduler_lock_is_held(tmp_path, service):
    from stiracore.scheduler.scheduler import Scheduler

    scheduler = Scheduler(tmp_path / "sched")
    current = record(service)
    permit = issue(service)
    held, release = threading.Event(), threading.Event()

    def hold_scheduler():
        with scheduler._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold_scheduler)
    holder.start()
    assert held.wait(2)
    try:
        started = time.monotonic()
        service.kill()
        assert time.monotonic() - started < 1.0
        with pytest.raises(AuthorityError, match="killed"):
            service.consume(permit, current, kind="yolo")
    finally:
        release.set()
        holder.join(5)


def test_kill_takes_effect_while_the_leaf_lock_holder_is_blocked(service):
    """A thread stuck inside the leaf lock (simulated I/O): the flag still stops every
    consume at once, without waiting for the lock; the epoch follows once it is free."""
    current = record(service)
    permit = issue(service)
    held, release = threading.Event(), threading.Event()

    def stuck():
        with service._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=stuck)
    holder.start()
    assert held.wait(2)
    before = service._epoch
    killer = threading.Thread(target=service.kill)
    killer.start()
    try:
        deadline = time.monotonic() + 2
        while not service.killed and time.monotonic() < deadline:
            time.sleep(0.001)
        assert service.killed
        outcome = []

        def attempt():
            try:
                service.consume(permit, current, kind="yolo")
                outcome.append("ok")
            except AuthorityError as error:
                outcome.append(error.code)

        consumer = threading.Thread(target=attempt)
        consumer.start()
        consumer.join(1)
        assert outcome == ["killed"]  # answered while the lock is still held
        assert service._epoch == before  # the epoch waits for the lock
    finally:
        release.set()
        holder.join(5)
        killer.join(5)
    assert service.epoch == before + 1


def test_kill_against_concurrent_consumes(service):
    """Two parts. (1) Racing a kill: every consume is either before it (``ok``) or
    refused, and none that starts after ``kill()`` returned goes through. (2) With the
    leaf lock held by a stuck thread, consumers started after the flag answer ``killed``
    at once — only the lock-free first check can do that."""
    current = record(service)
    permits = [issue(service) for _ in range(40)]
    gate = threading.Barrier(41)
    outcomes = []

    def consume(permit):
        gate.wait()
        try:
            service.consume(permit, current, kind="yolo")
            outcomes.append("ok")
        except AuthorityError as error:
            outcomes.append(error.code)

    killer = threading.Thread(target=lambda: (gate.wait(), service.kill()))
    threads = [threading.Thread(target=consume, args=(p,)) for p in permits]
    for thread in [*threads, killer]:
        thread.start()
    for thread in [*threads, killer]:
        thread.join(5)
        assert not thread.is_alive()
    assert len(outcomes) == 40
    assert set(outcomes) <= {"ok", "killed", "epoch_moved"}
    for permit in permits:  # after kill() returned nothing goes through
        with pytest.raises(AuthorityError, match="killed"):
            service.consume(permit, current, kind="yolo")

    other = AuthorityService(None)
    current = record(other)
    late = [issue(other) for _ in range(20)]
    held, release = threading.Event(), threading.Event()

    def stuck():
        with other._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=stuck)
    holder.start()
    assert held.wait(2)
    other._killed = True  # the kill's first step (its epoch move waits for the lock)
    codes = []

    def late_consume(permit):
        try:
            other.consume(permit, current, kind="yolo")
            codes.append("ok")
        except AuthorityError as error:
            codes.append(error.code)

    consumers = [threading.Thread(target=late_consume, args=(p,)) for p in late]
    try:
        for thread in consumers:
            thread.start()
        for thread in consumers:
            thread.join(1)
        assert codes == ["killed"] * 20  # answered while the leaf lock is still held
    finally:
        release.set()
        holder.join(5)
        for thread in consumers:
            thread.join(5)


# --- every narrowing between issue and consume ----------------------------------------


def _narrowings():
    def rules_save(service):
        from stiracore.authority import service as service_module
        from stiracore.yolo import runtime

        service_module._SERVICE = service  # as if installed by the server
        runtime.advance_epoch()

    return {
        "narrow": lambda s: s.narrow("revoked"),
        "advance_epoch": lambda s: s.advance_epoch(),
        "breaker_warning": lambda s: s.breaker_transition("normal", "warning"),
        "breaker_critical": lambda s: s.breaker_transition("warning", "critical"),
        "rules_save": rules_save,
        "kill": lambda s: s.kill(),
    }


@pytest.mark.parametrize("name", list(_narrowings()))
def test_every_narrowing_between_issue_and_consume_refuses(service, name):
    current = record(service)
    permit = issue(service)
    _narrowings()[name](service)
    with pytest.raises(AuthorityError) as refused:
        service.consume(permit, current, kind="yolo")
    assert refused.value.code == ("killed" if name == "kill" else "epoch_moved")


@pytest.mark.parametrize(
    "before, after, moves",
    [
        ("normal", "warning", True),
        ("normal", "coherence", True),
        ("normal", "critical", True),
        ("warning", "critical", True),
        ("critical", "warning", True),
        ("coherence", "warning", True),
        ("normal", "error", True),
        ("critical", "normal", False),
        ("warning", "normal", False),
        ("warning", "warning", False),
    ],
)
def test_breaker_transitions_that_move_the_epoch(service, before, after, moves):
    start = service.epoch
    service.breaker_transition(before, after)
    service.flush()
    assert service.epoch == start + (1 if moves else 0)
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_the_engine_hands_level_changes_to_the_hook(make_engine):
    from stiracore.core import NeuralState

    seen = []
    engine = make_engine(state=NeuralState(stability=0.4))
    engine.on_breaker_transition = lambda a, b: seen.append((a, b))
    engine.pre_turn()  # NORMAL -> WARNING
    engine.pre_turn()  # unchanged
    engine.state.stability = 0.1
    engine.pre_turn()  # -> CRITICAL
    assert seen == [("normal", "warning"), ("warning", "critical")]


def test_a_failing_hook_never_breaks_the_breaker(make_engine):
    from stiracore.core import BreakLevel, NeuralState

    engine = make_engine(state=NeuralState(stability=0.4))
    engine.on_breaker_transition = lambda a, b: 1 / 0
    assert engine.pre_turn().level == BreakLevel.WARNING


def test_the_engine_hook_moves_the_service_epoch(make_engine, service):
    from stiracore.core import NeuralState

    engine = make_engine(state=NeuralState(stability=0.4))
    engine.on_breaker_transition = service.breaker_transition
    start = service.epoch
    engine.pre_turn()
    service.flush()
    assert service.epoch == start + 1
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_a_rules_save_moves_the_epoch_without_yolo(tmp_path):
    from stiracore.yolo import runtime

    assert runtime.desk() is None
    service = authority.install(tmp_path)
    start = service.epoch
    runtime.advance_epoch()
    assert service.epoch == start + 1
    assert service.status()["recent"][-1] == {"epoch": start + 1, "reason": "tool_rules_saved"}
    assert stored(tmp_path)[-1]["epoch"] == start + 1


def test_without_a_service_the_rules_save_hook_still_works():
    from stiracore.yolo import runtime

    assert authority.current() is None
    runtime.advance_epoch()  # nothing installed: nothing to move, nothing raised


# --- in-flight set --------------------------------------------------------------------


def test_a_narrowing_cancels_in_flight_work_outside_the_leaf_lock(service):
    current = record(service)
    calls = []

    def cancel(reason):
        free = service._lock.acquire(blocking=False)
        if free:
            service._lock.release()
        calls.append((reason, free))

    handle = service.consume(issue(service), current, kind="yolo", on_cancel=cancel)
    assert handle.state == "running" and service.in_flight_count() == 1
    service.narrow("revoked")
    assert calls == [("narrowing", True)]
    assert handle.state == "stopping"
    assert handle.done("cancelled") == "cancelled"
    assert service.in_flight_count() == 0
    assert service.stop_records()[-1]["outcome"] == "cancelled"


def test_an_effect_that_ends_after_the_stop_is_completed_after_stop(service):
    current = record(service)
    handle = service.consume(issue(service), current, kind="yolo", track=True)
    service.kill()
    assert handle.done() == "completed_after_stop"
    assert service.stop_records()[-1]["outcome"] == "completed_after_stop"


def test_an_effect_finished_before_the_narrowing_is_not_touched(service):
    current = record(service)
    calls = []
    handle = service.consume(issue(service), current, kind="yolo", on_cancel=calls.append)
    assert handle.done() == "completed"
    service.narrow("revoked")
    assert calls == [] and service.stop_records() == []


def test_a_callback_for_an_effect_that_finished_meanwhile_is_skipped(service):
    current = record(service)
    calls = []
    handle = service.consume(issue(service), current, kind="yolo", on_cancel=calls.append)
    _epoch, taken = service._advance("revoked")
    assert handle.done() == "completed_after_stop"  # ended between take and cancel
    service._run_cancels(taken, "narrowing")
    assert calls == []


def test_a_late_registered_callback_runs_at_once(service):
    current = record(service)
    handle = service.consume(issue(service), current, kind="yolo", track=True)
    service.narrow("revoked")
    calls = []
    handle.set_cancel(calls.append)
    assert calls == ["narrowing"]


def test_a_raising_callback_never_reaches_the_caller(service):
    current = record(service)
    calls = []

    def raising(reason):
        calls.append("raising")
        raise RuntimeError("boom")

    service.consume(issue(service), current, kind="yolo", on_cancel=raising)
    service.consume(issue(service), current, kind="yolo", on_cancel=calls.append)
    service.kill()  # does not raise
    assert calls == ["raising", "narrowing"]  # the later callback still ran
    assert service.killed
    assert service.status()["persisted_epoch"] == service.epoch  # persistence completed
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_deferred_narrowings_cancel_on_their_own_thread(service):
    """A narrowing under a caller's lock: the callback runs on the settle worker, can
    take that caller's lock once it is released, and never holds the leaf lock."""
    current = record(service)
    caller_lock = threading.Lock()
    seen = {}
    done = threading.Event()

    def cancel(reason):
        seen["thread"] = threading.current_thread()
        seen["leaf_free"] = service._lock.acquire(blocking=False)
        if seen["leaf_free"]:
            service._lock.release()
        seen["caller_lock"] = caller_lock.acquire(timeout=2)
        if seen["caller_lock"]:
            caller_lock.release()
        done.set()

    service.consume(issue(service), current, kind="yolo", on_cancel=cancel)
    with caller_lock:  # e.g. the YOLO request lock or the engine's state_lock
        service.advance_epoch()
        assert not done.is_set()  # not run inline under the caller's lock
    assert done.wait(3)
    assert seen["thread"] is not threading.current_thread()
    assert seen["leaf_free"] is True and seen["caller_lock"] is True
    assert service.flush(timeout=5)
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_the_in_flight_set_is_bounded(service):
    service.max_in_flight = 2
    current = record(service)
    first = service.consume(issue(service), current, kind="yolo", track=True)
    service.consume(issue(service), current, kind="yolo", track=True)
    waiting = issue(service)
    with pytest.raises(AuthorityError, match="in_flight_full"):
        service.consume(waiting, current, kind="yolo", track=True)
    first.done()
    assert service.consume(waiting, current, kind="yolo", track=True) is not None  # unused


def test_a_narrowing_racing_tracked_consumes_takes_every_one_that_succeeded(service):
    """Linearization: a consume that succeeded came before the epoch move, so the move
    took it (``stopping``); every other one was refused (``epoch_moved``)."""
    current = record(service)
    permits = [issue(service) for _ in range(40)]
    gate = threading.Barrier(41)
    handles, codes = [], []

    def consume(permit):
        gate.wait()
        try:
            handles.append(service.consume(permit, current, kind="yolo", track=True))
        except AuthorityError as error:
            codes.append(error.code)

    def narrow():
        gate.wait()
        service.narrow("revoked")

    threads = [threading.Thread(target=consume, args=(p,)) for p in permits]
    threads.append(threading.Thread(target=narrow))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(handles) + len(codes) == 40
    assert set(codes) <= {"epoch_moved"}
    assert all(handle.state == "stopping" for handle in handles)


def test_the_record_of_consumed_permits_is_bounded(service, monkeypatch):
    from stiracore.authority import boundary

    monkeypatch.setattr(boundary, "MAX_CONSUMED", 3)
    current = record(service)
    first = issue(service)
    service.consume(first, current, kind="yolo")
    for _ in range(3):
        service.consume(issue(service), current, kind="yolo")
    assert len(service._consumed) == 3
    with pytest.raises(AuthorityError, match="permit_unknown"):  # still refused
        service.consume(first, current, kind="yolo")


def test_an_untracked_consume_registers_nothing(service):
    assert service.consume(issue(service), record(service), kind="yolo") is None
    assert service.in_flight_count() == 0


# --- optional consume fields (not used by any caller yet) -----------------------------


def test_policy_version_and_reservation_must_match(service):
    current = record(service)
    permit = issue(service, policy_version=3, reservation_id=RID)
    with pytest.raises(AuthorityError, match="policy_moved"):
        service.consume(permit, current, kind="yolo", policy_version=4, reservation_id=RID)
    with pytest.raises(AuthorityError, match="policy_moved"):
        service.consume(permit, current, kind="yolo", reservation_id=RID)
    with pytest.raises(AuthorityError, match="reservation_mismatch"):
        service.consume(permit, current, kind="yolo", policy_version=3, reservation_id="f" * 32)
    service.consume(permit, current, kind="yolo", policy_version=3, reservation_id=RID)
    unbound = issue(service)
    with pytest.raises(AuthorityError, match="policy_moved"):
        service.consume(unbound, current, kind="yolo", policy_version=0)


def test_the_ttl_pair_is_checked_on_both_clocks(service):
    current = record(service)
    ttl = TtlPair(wall_deadline=1000.0, monotonic_deadline=50.0)
    permit = issue(service, ttl=ttl)
    with pytest.raises(AuthorityError, match="clock_missing"):
        service.consume(permit, current, kind="yolo")
    with pytest.raises(AuthorityError, match="ttl_reached"):
        service.consume(permit, current, kind="yolo", clock=ClockPair(1000.0, 10.0))
    with pytest.raises(AuthorityError, match="ttl_reached"):
        service.consume(permit, current, kind="yolo", clock=ClockPair(10.0, 50.0))
    service.consume(permit, current, kind="yolo", clock=ClockPair(999.0, 49.0))


@pytest.mark.parametrize(
    "kw, code",
    [
        ({"policy_version": -1}, "policy_version_invalid"),
        ({"policy_version": True}, "policy_version_invalid"),
        ({"policy_version": 1.0}, "policy_version_invalid"),
        ({"reservation_id": "short"}, "reservation_invalid"),
        ({"reservation_id": 7}, "reservation_invalid"),
        ({"ttl": (1.0, 2.0)}, "ttl_invalid"),
        ({"ttl": TtlPair(float("nan"), 2.0)}, "ttl_invalid"),
        ({"ttl": TtlPair(1.0, float("inf"))}, "ttl_invalid"),
    ],
)
def test_invalid_binding_fields_are_refused_at_issue(service, kw, code):
    with pytest.raises(AuthorityError, match=code):
        issue(service, **kw)


@pytest.mark.parametrize(
    "kw, code",
    [
        ({"policy_version": "3"}, "policy_version_invalid"),
        ({"reservation_id": "x" * 32}, "reservation_invalid"),
        ({"clock": (1.0, 2.0)}, "clock_invalid"),
        ({"clock": ClockPair(float("nan"), 1.0)}, "clock_invalid"),
        ({"on_cancel": "not callable"}, "cancel_invalid"),
    ],
)
def test_invalid_consume_fields_are_refused(service, kw, code):
    permit = issue(service)
    with pytest.raises(AuthorityError, match=code):
        service.consume(permit, record(service), kind="yolo", **kw)
    service.consume(permit, record(service), kind="yolo")  # the permit was not used


@pytest.mark.parametrize(
    "field, value",
    [("policy_version", 9), ("reservation_id", "f" * 32), ("ttl", TtlPair(9e9, 9e9))],
)
def test_a_rewritten_binding_field_is_tampering(service, field, value):
    current = record(service)
    permit = issue(service, policy_version=1, reservation_id=RID, ttl=TtlPair(5.0, 5.0))
    forged = dataclasses.replace(permit, **{field: value})
    kw = {
        "policy_version": forged.policy_version,
        "reservation_id": forged.reservation_id,
        "clock": ClockPair(1.0, 1.0),
    }
    with pytest.raises(AuthorityError, match="permit_tampered"):
        service.consume(forged, current, kind="yolo", **kw)


# --- YOLO receives the service --------------------------------------------------------


def _yolo(service):
    from stiracore.yolo.requests import YoloRequests

    return YoloRequests(
        allowance="external",
        incarnation=service.incarnation,
        record=None,
        actor="local:test",
        clocks_supported=True,
        authority=service,
    )


def test_yolo_requests_use_the_injected_service(service):
    yolo = _yolo(service)
    assert yolo._authority is service
    start = service.epoch
    yolo.advance_epoch()  # a YOLO move (under its request lock): the shared epoch moves
    service.flush()
    assert service.epoch == start + 1
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_yolo_refuses_a_service_of_another_incarnation(service):
    from stiracore.yolo.requests import YoloRequests

    with pytest.raises(ValueError):
        YoloRequests(
            allowance="external",
            incarnation="f" * 32,
            record=None,
            actor="local:test",
            clocks_supported=True,
            authority=service,
        )


def test_yolo_without_a_service_keeps_a_private_boundary():
    from stiracore.yolo.requests import YoloRequests

    yolo = YoloRequests(
        allowance="external",
        incarnation="f" * 32,
        record=None,
        actor="local:test",
        clocks_supported=True,
    )
    assert type(yolo._authority) is AuthorityBoundary
    assert yolo._authority.incarnation == "f" * 32


def test_the_yolo_module_re_exports_the_same_classes():
    from stiracore.yolo import authority as yolo_authority

    assert yolo_authority.AuthorityBoundary is AuthorityBoundary
    assert yolo_authority.GrantRecord is GrantRecord
    assert yolo_authority.AuthorityError is AuthorityError


# --- nothing starts work --------------------------------------------------------------


def test_the_service_starts_no_thread_of_its_own(tmp_path):
    before = {t.ident for t in threading.enumerate()}
    service = authority.install(tmp_path)
    service.narrow("tool_rules_saved")
    service.kill()
    assert {t.ident for t in threading.enumerate()} == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX data-directory lock")
def test_the_server_installs_one_service_with_or_without_yolo(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    data = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setenv("STIRA_YOLO_ALLOW", "off")
    model = Mock(side_effect=AssertionError("model reached"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = AssertionError(f"model.{method} reached")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    store.gateway_path.write_text("{}", encoding="utf-8")
    app = stira_server.create_app(config_store=store)
    try:
        service = app.extensions["authority"]
        assert service is authority.current()
        assert service.unhealthy_reason is None and service.epoch >= 1
        assert stored(data)[-1]["epoch"] == service.epoch
        from stiracore.yolo import runtime

        assert runtime.desk() is None  # YOLO off, the service still exists
        start = service.epoch
        runtime.advance_epoch()
        assert service.epoch == start + 1

        # R2-3: a write that fails after start-up is not a clean stop — through the real
        # lifecycle shutdown and the printed stop line.
        def failing(*_a, **_k):
            raise OSError(errno.ENOSPC, "no space")

        monkeypatch.setattr(epoch_store, "write", failing)
        service.advance_epoch()  # deferred: the worker's write fails
        result = app.extensions["lifecycle"].shutdown()
        assert result["authority"] == "unhealthy"
        assert service.unhealthy_reason == "epoch_store_write_failed"
        line = stira_server._stopped_line(result)
        assert "authority: unhealthy" in line, line
    finally:
        for name in ("heartbeat", "scheduler"):
            if app.extensions.get(name) is not None:
                app.extensions[name].stop()


@pytest.mark.parametrize(
    "result, label",
    [
        ("clean", "stopped"),
        ("pending", "pending"),
        ("unhealthy", "unhealthy"),
        ("settle_unavailable", "pending (no settle worker)"),
        ("reentrant", "error"),
        ("something else", "error"),
    ],
)
def test_the_authority_stop_labels(result, label):
    from dashboard.stira_server import _AUTHORITY_STOP_LABELS, _stopped_line

    shown = _AUTHORITY_STOP_LABELS.get(result, "error")
    assert shown == label
    line = _stopped_line({"approvals": "closed", "authority": shown})
    assert ("authority:" in line) is (label != "stopped")


def test_the_stop_line_names_the_authority_step_only_when_it_is_not_clean():
    from dashboard.stira_server import _stopped_line

    clean = {"approvals": "closed", "heartbeat": "stopped", "authority": "stopped"}
    assert _stopped_line(clean) == "StiraCore: stopped (approvals: closed, heartbeat: stopped)"
    pending = {"approvals": "closed", "authority": "pending"}
    assert _stopped_line(pending) == "StiraCore: stopped (approvals: closed, authority: pending)"
    failed = {"authority": "not run (RuntimeError)"}
    assert _stopped_line(failed) == "StiraCore: stopped (authority: not run (RuntimeError))"
    assert _stopped_line({}) is None


# --- F2: the epoch file is checked by descriptor and before every replace -------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes, links and FIFOs")


def _epoch_path(service):
    return service._data_dir / epoch_store.EPOCH_FILE


def _snapshot(path):
    info = os.lstat(path)
    return path.read_bytes() if stat.S_ISREG(info.st_mode) else None, stat.S_IMODE(info.st_mode)


@posix_only
def test_a_live_chmod_makes_the_next_write_refuse_and_keeps_the_evidence(service):
    path = _epoch_path(service)
    os.chmod(path, 0o644)
    before = _snapshot(path)
    service.narrow("tool_rules_saved")
    assert service.unhealthy_reason == "epoch_store_unsafe"
    assert _snapshot(path) == before  # not overwritten, mode unchanged
    service.narrow("tool_rules_saved")  # sticky
    assert service.unhealthy_reason == "epoch_store_unsafe" and _snapshot(path) == before


@posix_only
def test_a_hard_link_is_unsafe_at_start_and_on_a_live_service(tmp_path, service):
    path = _epoch_path(service)
    os.link(path, tmp_path / "second-name")
    assert AuthorityService(service._data_dir).unhealthy_reason == "epoch_store_unsafe"
    before = path.read_bytes()
    service.narrow("tool_rules_saved")
    assert service.unhealthy_reason == "epoch_store_unsafe"
    assert path.read_bytes() == before


@posix_only
def test_a_symlink_swap_on_a_live_service_is_refused(tmp_path, service):
    path = _epoch_path(service)
    copy = tmp_path / "copy"
    copy.write_bytes(path.read_bytes())
    os.chmod(copy, 0o600)
    path.unlink()
    path.symlink_to(copy)
    service.narrow("tool_rules_saved")
    assert service.unhealthy_reason == "epoch_store_unsafe"
    assert path.is_symlink() and os.readlink(path) == str(copy)


@posix_only
def test_a_fifo_never_blocks_the_check(service):
    path = _epoch_path(service)
    path.unlink()
    os.mkfifo(path, 0o600)
    result = []
    reader = threading.Thread(
        target=lambda: result.append(epoch_store.load(service._data_dir)), daemon=True
    )
    reader.start()
    reader.join(2)
    assert not reader.is_alive()
    assert result[0].reason == "epoch_store_unsafe"


def _swap_at_open(monkeypatch, swap):
    """Run ``swap(path)`` after the ``lstat`` check passed, right before the open."""
    real_open = epoch_store.os.open
    fired = []

    def open_(path, flags, *args):
        if str(path).endswith(epoch_store.EPOCH_FILE) and not fired:
            fired.append(True)
            swap(Path(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(epoch_store.os, "open", open_)
    return fired


@posix_only
def test_a_fifo_swapped_in_after_the_path_check_does_not_block(tmp_path, monkeypatch):
    AuthorityService(tmp_path)

    def to_fifo(path):
        path.unlink()
        os.mkfifo(path, 0o600)

    fired = _swap_at_open(monkeypatch, to_fifo)
    result = []
    reader = threading.Thread(target=lambda: result.append(epoch_store.load(tmp_path)), daemon=True)
    reader.start()
    reader.join(2)
    assert fired and not reader.is_alive()  # O_NONBLOCK: no writer needed
    assert result[0].reason == "epoch_store_unsafe"


@posix_only
@pytest.mark.parametrize("swap", ["hard_link", "chmod", "other_inode"])
def test_a_swap_between_the_path_check_and_the_open_is_caught_by_the_descriptor(
    tmp_path, monkeypatch, swap
):
    AuthorityService(tmp_path)

    def change(path):
        if swap == "hard_link":
            os.link(path, tmp_path / "second-name")
        elif swap == "chmod":
            os.chmod(path, 0o644)
        else:  # a different, perfectly safe file with the same content
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)
            os.chmod(path, 0o600)

    fired = _swap_at_open(monkeypatch, change)
    result = epoch_store.load(tmp_path)
    assert fired and result.reason == "epoch_store_unsafe"


@pytest.mark.parametrize("change", ["replaced", "rewritten", "vanished", "truncated"])
def test_a_file_changed_behind_a_live_service_is_never_overwritten(service, change):
    path = _epoch_path(service)
    service.narrow("tool_rules_saved")
    data = path.read_bytes()
    if change == "replaced":  # same bytes, another inode
        path.unlink()
        path.write_bytes(data)
        os.chmod(path, 0o600)
        reason = "epoch_store_replaced"
    elif change == "rewritten":  # a valid chain, but not the one this service wrote
        other = epoch_store.next_record(None, 99)
        path.write_text(json.dumps({"v": 1, "records": [other.as_dict()]}))
        reason = "epoch_store_replaced"
    elif change == "vanished":
        path.unlink()
        reason = "epoch_store_replaced"
    else:
        path.write_bytes(data[: len(data) // 2])
        reason = "epoch_store_malformed"
    before = path.read_bytes() if path.exists() else None
    service.narrow("tool_rules_saved")
    assert service.unhealthy_reason == reason
    assert (path.read_bytes() if path.exists() else None) == before
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        issue(service)


def test_a_foreign_owner_is_unsafe(tmp_path, monkeypatch):
    AuthorityService(tmp_path)
    if not hasattr(os, "getuid"):
        pytest.skip("no uid")
    uid = os.getuid()
    monkeypatch.setattr(epoch_store.os, "getuid", lambda: uid + 1)
    assert epoch_store.load(tmp_path).reason == "epoch_store_unsafe"


# --- F3: one settle worker, never raising ---------------------------------------------


def _settle_threads():
    return [t for t in threading.enumerate() if t.name == "authority-settle"]


def test_twenty_advances_with_a_blocked_disk_use_one_worker(service, monkeypatch):
    real_write = epoch_store.write
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(epoch_store, "write", blocked)
    service.advance_epoch()
    assert entered.wait(2)
    try:
        for _ in range(20):
            service.advance_epoch()
            service.breaker_transition("normal", "warning")
        assert len(_settle_threads()) <= 1
    finally:
        release.set()
    assert service.flush(timeout=5)
    assert service.status()["persisted_epoch"] == service.epoch
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch
    assert service.unhealthy_reason is None


@pytest.fixture
def no_thread_once(monkeypatch):
    """The settle worker's ``Thread.start`` raises once (``can't start new thread``);
    every other thread, and a later settle worker, starts normally (R2-5: only
    ``start`` is patched, the constructor is the real one)."""
    real_start = threading.Thread.start
    state = {"failures": 1, "raised": 0, "started": 0}

    def start(self):
        if self.name == "authority-settle":
            if state["failures"]:
                state["failures"] -= 1
                state["raised"] += 1
                raise RuntimeError("can't start new thread")
            state["started"] += 1
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    return state


def _assert_settle_unavailable(service, permit, current):
    assert service.unhealthy_reason == "epoch_settle_unavailable"
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        service.consume(permit, current, kind="yolo")
    assert service.status()["settle_pending"] is True  # the work is kept, not dropped
    assert service._owner is None  # nobody claims it without a running worker


def test_a_worker_that_cannot_start_via_the_engine_hook(service, make_engine, no_thread_once):
    from stiracore.core import BreakLevel, NeuralState

    current = record(service)
    permit = issue(service)
    threads = []
    service.consume(
        issue(service),
        current,
        kind="yolo",
        on_cancel=lambda r: threads.append(threading.current_thread().name),
    )
    engine = make_engine(state=NeuralState(stability=0.4))
    engine.on_breaker_transition = service.breaker_transition
    assert engine.pre_turn().level == BreakLevel.WARNING  # nothing raised into the turn
    assert no_thread_once["raised"] == 1  # the intended path: start() raised
    _assert_settle_unavailable(service, permit, current)
    assert threads == []
    # A later drain starts a real worker, which runs the kept cancellation.
    assert service.flush_result(timeout=5) == "unhealthy"  # drained, not durable
    assert no_thread_once["started"] == 1
    assert threads == ["authority-settle"]
    assert service.status()["settle_pending"] is False


def test_a_worker_that_cannot_start_via_the_rules_save(service, monkeypatch, no_thread_once):
    from stiracore.authority import service as service_module
    from stiracore.yolo import runtime

    monkeypatch.setattr(service_module, "_SERVICE", service)
    monkeypatch.setattr(runtime, "_DESK", SimpleNamespace(yolo=_yolo(service)))
    current = record(service)
    permit = issue(service)
    runtime.advance_epoch()  # YOLO's move (deferred) then the narrowing: never raises
    assert no_thread_once["raised"] == 1
    assert service.unhealthy_reason == "epoch_settle_unavailable"
    with pytest.raises(AuthorityError, match="authority_unhealthy"):
        service.consume(permit, current, kind="yolo")
    assert service.flush_result(timeout=5) == "unhealthy"
    assert service.status()["settle_pending"] is False


def test_advance_epoch_and_the_hook_never_raise(service, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("queue broken")

    monkeypatch.setattr(service, "_spawn_worker", broken)
    service.advance_epoch()
    service.breaker_transition("normal", "critical")
    assert service.unhealthy_reason == "epoch_settle_unavailable"
    assert service._owner is None  # never "owned" without a worker
    monkeypatch.undo()


def test_a_failing_epoch_move_never_skips_the_revocation(monkeypatch):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.yolo import runtime

    def failing(*_a, **_k):
        raise RuntimeError("epoch move failed")

    monkeypatch.setattr(runtime, "advance_epoch", failing)
    reader, broker = Mock(), Mock()
    saved = stira_server._tool_rules_saved_hook(reader, broker)
    with pytest.raises(RuntimeError):
        saved()
    reader.sync.assert_called_once()
    broker.suspend_approvals.assert_called_once_with(stira_server.REVOKE_FAILED)
    broker.sync_from_store.assert_called_once()


# --- F4: flush drains queued and running settlement -----------------------------------


def test_flush_waits_for_a_running_cancel_and_reports_the_deadline(service):
    current = record(service)
    entered, release = threading.Event(), threading.Event()

    def blocked(reason):
        entered.set()
        release.wait(5)

    service.consume(issue(service), current, kind="yolo", on_cancel=blocked)
    service.advance_epoch()
    assert entered.wait(2)
    started = time.monotonic()
    assert service.flush(timeout=0.2) is False  # work is outstanding at the deadline
    assert time.monotonic() - started < 2
    release.set()
    assert service.flush(timeout=5) is True
    assert service.status()["persisted_epoch"] == service.epoch


def test_a_callback_that_calls_back_into_the_service_does_not_deadlock(service):
    current = record(service)
    seen = []

    def reentrant(reason):
        seen.append(service.flush(timeout=5))  # its own worker: answered at once
        seen.append(service.status()["epoch"])
        service.narrow("revoked")

    service.consume(issue(service), current, kind="yolo", on_cancel=reentrant)
    start = service.epoch
    service.advance_epoch()
    assert service.flush(timeout=5) is True
    assert seen[0] is False and seen[1] == start + 1
    assert service.epoch == start + 2
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


# --- F5: no entropy under the leaf lock; re-checks inside it --------------------------


def test_no_entropy_call_while_the_leaf_lock_is_held(service, monkeypatch):
    from stiracore.authority import boundary

    real = boundary.secrets.token_hex
    calls = []

    def guarded(n):
        calls.append(service._lock.locked())
        return real(n)

    monkeypatch.setattr(boundary.secrets, "token_hex", guarded)
    current = record(service)
    permit = issue(service)
    service.consume(permit, current, kind="yolo", track=True)
    assert calls == [False, False]  # the nonce and the effect id, both outside the lock


def test_a_kill_after_the_first_check_is_caught_inside_the_lock(service, monkeypatch):
    from stiracore.authority import boundary

    real = boundary.secrets.token_hex
    current = record(service)
    permit = issue(service)

    def kill_then_draw(n):
        service.kill()  # lands between the lock-free check and the lock
        return real(n)

    monkeypatch.setattr(boundary.secrets, "token_hex", kill_then_draw)
    with pytest.raises(AuthorityError, match="killed"):
        service.consume(permit, current, kind="yolo", track=True)
    assert service.in_flight_count() == 0


def test_issue_answers_a_kill_without_waiting_for_the_leaf_lock(service):
    held, release = threading.Event(), threading.Event()

    def stuck():
        with service._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=stuck)
    holder.start()
    assert held.wait(2)
    service._killed = True
    codes = []

    def attempt():
        try:
            issue(service)
            codes.append("ok")
        except AuthorityError as error:
            codes.append(error.code)

    issuer = threading.Thread(target=attempt)
    try:
        issuer.start()
        issuer.join(1)
        assert codes == ["killed"]
    finally:
        release.set()
        holder.join(5)
        issuer.join(5)


# --- order: cancel before persist (ADR 0011 §5) ---------------------------------------


@pytest.mark.parametrize("deferred", [False, True])
def test_in_flight_work_is_cancelled_before_the_epoch_is_written(service, monkeypatch, deferred):
    real_write = epoch_store.write
    order = []

    def write(*args, **kwargs):
        order.append("write")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(epoch_store, "write", write)
    current = record(service)
    service.consume(
        issue(service), current, kind="yolo", on_cancel=lambda r: order.append("cancel")
    )
    if deferred:
        service.advance_epoch()
        assert service.flush(timeout=5)
    else:
        service.narrow("revoked")
    assert order == ["cancel", "write"]


# --- F6 and the error transition: the engine publishes every committed level change ---


def test_a_checkpoint_restore_publishes_its_level_change(tmp_path, service):
    from stiracore.core import BreakLevel, NeuralState, StiraEngine
    from stiracore.core.checkpoint import FileCheckpointStore

    store = FileCheckpointStore(tmp_path / "checkpoints")
    critical = StiraEngine(state=NeuralState(stability=0.2), checkpoint_store=store)
    assert critical.pre_turn().level == BreakLevel.CRITICAL
    checkpoint_id = critical.last_checkpoint_id
    live = StiraEngine(state=NeuralState(), checkpoint_store=store)
    assert live.pre_turn().level == BreakLevel.NORMAL
    seen = []
    live.on_breaker_transition = lambda a, b: (
        seen.append((a, b)),
        service.breaker_transition(a, b),
    )
    start = service.epoch
    live.resume_from_checkpoint(checkpoint_id)
    assert seen == [("normal", "critical")]
    assert service.flush(timeout=5)
    assert service.epoch == start + 1
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch


def test_a_failing_breaker_evaluation_is_published_as_an_error(make_engine, monkeypatch):
    engine = make_engine()
    seen = []
    engine.on_breaker_transition = lambda a, b: seen.append((a, b))

    def broken(*_a, **_k):
        raise RuntimeError("evaluation failed")

    monkeypatch.setattr(engine, "_evaluate_breaker", broken)
    with pytest.raises(RuntimeError):
        engine.pre_turn()
    assert seen == [("normal", "error")]


# --- R2-2: every settlement owner is visible to flush ---------------------------------


def test_a_second_flush_never_reports_clean_while_the_first_drain_runs(service, no_thread_once):
    """Astra probe: the worker could not start; a flush with a deadline does not run
    the blocking callback inline, and a concurrent flush never returns clean while a
    real worker is still cancelling."""
    current = record(service)
    entered, release = threading.Event(), threading.Event()

    def blocked(reason):
        entered.set()
        release.wait(5)

    service.consume(issue(service), current, kind="yolo", on_cancel=blocked)
    service.advance_epoch()  # start() raises: nobody owns the batch
    assert no_thread_once["raised"] == 1
    results = []
    first = threading.Thread(
        target=lambda: results.append(service.flush_result(timeout=0.2)), daemon=True
    )
    started = time.monotonic()
    first.start()
    assert entered.wait(2)  # the first flush handed it to a real worker
    first.join(2)
    assert not first.is_alive() and time.monotonic() - started < 2  # bounded
    assert results == ["pending"]
    assert service.flush_result(timeout=0.05) == "pending"  # the second one too
    release.set()
    assert service.flush_result(timeout=5) == "unhealthy"  # drained; start had failed
    assert service.status()["persisted_epoch"] < service.epoch


def test_a_flush_waits_for_a_synchronous_narrowing_on_another_thread(service):
    current = record(service)
    entered, release = threading.Event(), threading.Event()

    def blocked(reason):
        entered.set()
        release.wait(5)

    service.consume(issue(service), current, kind="yolo", on_cancel=blocked)
    narrower = threading.Thread(target=lambda: service.narrow("revoked"), daemon=True)
    narrower.start()
    assert entered.wait(2)
    assert service.flush_result(timeout=0.1) == "pending"
    assert service.status()["settle_pending"] is True
    release.set()
    narrower.join(5)
    assert service.flush_result(timeout=5) == "clean"
    assert service.status()["persisted_epoch"] == service.epoch


def _no_worker(service, monkeypatch):
    """No settle worker can ever start (the real handler releases the claimed slot)."""

    def cannot_start():
        with service._lock:
            if service._owner == "worker":
                service._owner = None
        return False

    monkeypatch.setattr(service, "_spawn_worker", cannot_start)


def test_without_a_deadline_unowned_work_is_settled_inline(service, monkeypatch):
    _no_worker(service, monkeypatch)
    calls = []
    current = record(service)
    service.consume(issue(service), current, kind="yolo", on_cancel=calls.append)
    service._advance("revoked")  # queued, nobody owns it
    assert service.flush_result(timeout=0.1) == "settle_unavailable"  # never inline
    assert calls == []
    assert service.flush_result() == "clean"  # no deadline: settled here
    assert calls == ["narrowing"]


def test_a_reentrant_flush_on_the_inline_path_is_identified(service, monkeypatch):
    _no_worker(service, monkeypatch)
    seen = []
    current = record(service)
    service.consume(
        issue(service), current, kind="yolo", on_cancel=lambda r: seen.append(service.flush())
    )
    service._advance("revoked")
    assert service.flush_result() == "clean"
    assert seen == [False]  # "reentrant", answered at once


# --- R2-3: drained is not durable -----------------------------------------------------


def test_a_failed_write_is_never_reported_as_a_clean_drain(service, monkeypatch):
    def failing(*_a, **_k):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(epoch_store, "write", failing)
    service.advance_epoch()
    assert service.flush_result(timeout=5) == "unhealthy"
    assert service.flush(timeout=5) is False
    assert service.status()["persisted_epoch"] < service.epoch


def test_a_failing_worker_round_latches(service, monkeypatch):
    def broken():
        raise RuntimeError("not an OSError")

    monkeypatch.setattr(service, "_persist", broken)
    service.advance_epoch()
    assert service.flush_result(timeout=5) == "unhealthy"
    assert service.unhealthy_reason == "epoch_settle_failed"


# --- R2-4: the write never passes the cancellation frontier ---------------------------


def test_the_epoch_on_disk_stays_below_a_cancellation_still_running(service):
    """Astra's deterministic probe: A's callback blocked at epoch 2, B consumed at 2,
    advance to 3 while A is blocked, release A, block B: the disk must not show 3."""
    current = record(service)
    a_in, a_out, b_in, b_out = (threading.Event() for _ in range(4))

    def cancel_a(reason):
        a_in.set()
        a_out.wait(5)

    def cancel_b(reason):
        b_in.set()
        b_out.wait(5)

    service.consume(issue(service), current, kind="yolo", on_cancel=cancel_a)
    start = service.epoch
    service.advance_epoch()  # epoch start+1: A's cancellation runs on the worker
    assert a_in.wait(2)
    service.consume(issue(service), record(service), kind="yolo", on_cancel=cancel_b)
    service.advance_epoch()  # epoch start+2: B is taken while A still runs
    a_out.set()
    assert b_in.wait(2)
    try:
        on_disk = stored(service._data_dir)[-1]["epoch"]
        assert on_disk == start + 1 < service.epoch  # A's batch done, B's not
        assert service.status()["frontier"] == start + 1
    finally:
        b_out.set()
    assert service.flush_result(timeout=5) == "clean"
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch == start + 2


def test_a_synchronous_narrowing_shares_the_frontier(service):
    current = record(service)
    a_in, a_out = threading.Event(), threading.Event()

    def cancel_a(reason):
        a_in.set()
        a_out.wait(5)

    service.consume(issue(service), current, kind="yolo", on_cancel=cancel_a)
    start = service.epoch
    service.advance_epoch()
    assert a_in.wait(2)
    service.narrow("revoked")  # another owner runs: its batch waits behind A's
    try:
        assert stored(service._data_dir)[-1]["epoch"] == start
    finally:
        a_out.set()
    assert service.flush_result(timeout=5) == "clean"
    assert stored(service._data_dir)[-1]["epoch"] == service.epoch == start + 2


def test_a_flush_whose_worker_cannot_start_either_reports_it(service, no_thread_once):
    no_thread_once["failures"] = 2  # the narrowing's start and the flush's retry
    calls = []
    service.consume(issue(service), record(service), kind="yolo", on_cancel=calls.append)
    service.advance_epoch()
    assert service.flush_result(timeout=0.1) == "settle_unavailable"  # never raises
    assert no_thread_once["raised"] == 2 and calls == []
    assert service.flush_result(timeout=5) == "unhealthy"  # a third start works
    assert calls == ["narrowing"]


def test_an_unhealthy_in_memory_service_never_flushes_clean():
    service = AuthorityService(None)
    assert service.flush_result(timeout=1) == "clean"
    service._latch("epoch_settle_failed")
    assert service.flush_result(timeout=1) == "unhealthy"

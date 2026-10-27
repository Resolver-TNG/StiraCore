"""W2 (#296, ADR 0011 §12): the lifecycle_event contract, its chained log, the outbox and
the authority service's W2 parts (kill latch, narrowing report, bounded callbacks).

No model is called; no server is started.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time

import pytest

from stiracore import authority
from stiracore.authority import (
    AuthorityService,
    GrantRecord,
    Receipt,
    lifecycle_events,
    outbox,
    private_file,
)
from stiracore.authority.latches import LATCH_FILE, LATCHES, LatchStore, Stop
from stiracore.authority.lifecycle_events import (
    LOG_FILE,
    LifecycleLog,
    LifecycleSchemaError,
    make_event,
    validate,
)
from stiracore.authority.outbox import OUTBOX_FILE, LifecycleRecorder

INC = "1" * 32
WIN = "5" * 32
GID = "a" * 32
RECEIPT = Receipt(event_id=f"op:{'b' * 32}:decision", seq=7, hash="c" * 64)
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def event(**over):
    base = {
        "subject_kind": "latch",
        "subject_id": "kill",
        "from_state": "cleared",
        "to_state": "set",
        "reason_code": "kill_switch",
        "incarnation": INC,
        "epoch": 3,
        "ts_wall": 1000.0,
    }
    base.update(over)
    return make_event(
        base.pop("subject_kind"),
        base.pop("subject_id"),
        base.pop("from_state"),
        base.pop("to_state"),
        base.pop("reason_code"),
        **base,
    )


@pytest.fixture(autouse=True)
def _no_process_service():
    yield
    authority.uninstall()


# --- the schema (codes and ids only) ---------------------------------------------------


def test_a_valid_event_has_exactly_the_contract_keys():
    made = event()
    assert set(made) == {
        "event_id",
        "subject_kind",
        "subject_id",
        "from",
        "to",
        "reason_code",
        "incarnation",
        "epoch",
        "ts_wall",
    }
    assert validate(made) == made


GOAL_LIKE = [
    "refactor the billing module and push to main",
    "rm -rf ~/",
    '{"path": "/etc/passwd"}',
    "kill please",
    "halted:the user asked",
]


@pytest.mark.parametrize("text", GOAL_LIKE)
@pytest.mark.parametrize("field", ["subject_id", "from", "to", "reason_code", "subject_kind"])
def test_no_goal_or_argument_like_string_enters_any_field(text, field):
    made = event()
    with pytest.raises(LifecycleSchemaError):
        validate({**made, field: text})


@pytest.mark.parametrize("key", ["goal", "args", "message", "note", "detail"])
def test_an_extra_field_is_refused(key):
    with pytest.raises(LifecycleSchemaError):
        validate({**event(), key: "the goal text"})


def test_a_missing_field_is_refused():
    made = event()
    del made["reason_code"]
    with pytest.raises(LifecycleSchemaError):
        validate(made)


def test_subject_ids_are_checked_per_kind():
    assert event(
        subject_kind="yolo_window",
        subject_id=WIN,
        from_state="active",
        to_state="paused",
        reason_code="breaker_warning",
    )
    for kind, ident in [
        ("yolo_window", "kill"),
        ("latch", WIN),
        ("breaker", "daemon"),
        ("heartbeat", "engine"),
        ("yolo_window", "A" * 32),
    ]:
        with pytest.raises(LifecycleSchemaError):
            event(subject_kind=kind, subject_id=ident)


def test_subclasses_of_built_in_types_are_refused():
    class Sneaky(str):
        pass

    made = event()
    with pytest.raises(LifecycleSchemaError):
        validate({**made, "subject_id": Sneaky("kill")})
    with pytest.raises(LifecycleSchemaError):
        validate({**made, "epoch": True})
    with pytest.raises(LifecycleSchemaError):
        validate({**made, "ts_wall": float("nan")})


def test_gap_from_wall_only_on_gateway_down():
    good = event(
        subject_kind="gateway",
        subject_id="process",
        from_state="down",
        to_state="up",
        reason_code="gateway_down",
        gap_from_wall=10.0,
    )
    assert good["gap_from_wall"] == 10.0
    with pytest.raises(LifecycleSchemaError):
        event(gap_from_wall=10.0)


def test_the_yolo_codes_match_the_yolo_state_machine():
    from stiracore.yolo import state

    yolo = state.NORMAL_END_REASONS | state.ABNORMAL_END_REASONS | state.PAUSE_REASONS
    assert yolo <= lifecycle_events.YOLO_REASONS


def test_every_unhealthy_reason_of_the_service_is_a_code():
    from stiracore.authority import epoch_store  # noqa: F401 - the codes in its docstring

    for code in (
        "epoch_store_unsafe",
        "epoch_settle_failed",
        "latch_store_unsafe",
        "lifecycle_write_failed",
        "watchdog_failed",
        "latched_unhealthy",
    ):
        assert code in lifecycle_events.UNHEALTHY_REASONS


# --- the log ---------------------------------------------------------------------------


def test_the_log_is_chained_appended_and_read_back(tmp_path):
    log = LifecycleLog(tmp_path)
    first, second = event(), event(epoch=4)
    log.append(first)
    log.append(second)
    lines = (tmp_path / LOG_FILE).read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[1]["prev"] == parsed[0]["hash"] and parsed[0]["prev"] == "0" * 64
    if os.name == "posix":
        assert (tmp_path / LOG_FILE).stat().st_mode & 0o777 == 0o600
    assert LifecycleLog(tmp_path).events() == [first, second]


@pytest.mark.parametrize("damage", ["flip", "torn", "drop-first-line", "not-json"])
def test_a_damaged_log_is_unusable_and_never_written(tmp_path, damage):
    log = LifecycleLog(tmp_path)
    log.append(event())
    log.append(event(epoch=4))
    path = tmp_path / LOG_FILE
    raw = path.read_bytes()
    if damage == "flip":
        raw = raw.replace(b'"epoch":4', b'"epoch":5')
    elif damage == "torn":
        raw = raw[:-5]
    elif damage == "drop-first-line":
        raw = raw.split(b"\n", 1)[1]
    else:
        raw = b"garbage\n"
    path.write_bytes(raw)
    broken = LifecycleLog(tmp_path)
    assert broken.unhealthy_reason is not None
    with pytest.raises(OSError):
        broken.append(event(epoch=9))
    assert path.read_bytes() == raw


@POSIX
def test_a_symlinked_log_is_unsafe(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_bytes(b"")
    os.chmod(real, 0o600)
    os.symlink(real, tmp_path / LOG_FILE)
    assert LifecycleLog(tmp_path).unhealthy_reason == "lifecycle_log_unsafe"


def test_a_log_replaced_behind_the_writer_is_not_appended_to(tmp_path):
    log = LifecycleLog(tmp_path)
    log.append(event())
    path = tmp_path / LOG_FILE
    raw = path.read_bytes()
    path.unlink()
    path.write_bytes(raw)  # a new inode with the same content
    os.chmod(path, 0o600)
    with pytest.raises(OSError):
        log.append(event(epoch=4))
    assert log.unhealthy_reason == "lifecycle_log_replaced"
    assert path.read_bytes() == raw


def test_the_log_rotates_at_write_time_and_the_chain_continues(tmp_path, monkeypatch):
    """M5: a segment never grows past the cap + one line while running; the chain
    continues across segments and the files always reopen."""
    monkeypatch.setattr(lifecycle_events, "ROTATE_BYTES", 1500)
    log = LifecycleLog(tmp_path)
    for epoch in range(1, 21):
        log.append(event(epoch=epoch))
        for name in (LOG_FILE, lifecycle_events.OLD_LOG_FILE):
            path = tmp_path / name
            if path.exists():
                assert path.stat().st_size <= 1500 + 600
    assert (tmp_path / lifecycle_events.OLD_LOG_FILE).exists()
    again = LifecycleLog(tmp_path, rotate=False)
    assert again.unhealthy_reason is None
    epochs = [e["epoch"] for e in again.events()]
    assert epochs == list(range(epochs[0], 21)) and epochs[0] > 1  # the oldest went
    assert again.verify() is None


def test_a_log_over_the_old_reader_limit_stays_readable(tmp_path, monkeypatch):
    """M5: well over 4 MiB of events in one run — every segment stays readable."""
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    monkeypatch.setattr(private_file, "fsync_dir", lambda *_a, **_k: None)
    log = LifecycleLog(tmp_path)
    made = event()
    line = len(json.dumps(made)) + 200
    for epoch in range(1, (5 << 20) // line + 2):
        log.append(event(epoch=epoch))
    sizes = [(tmp_path / name).stat().st_size for name in (LOG_FILE, lifecycle_events.OLD_LOG_FILE)]
    assert max(sizes) <= lifecycle_events.ROTATE_BYTES + 1024
    assert LifecycleLog(tmp_path).unhealthy_reason is None
    assert LifecycleRecorder(tmp_path).unhealthy_reason is None


# --- the recorder and the outbox -------------------------------------------------------


def test_post_does_no_io(tmp_path, monkeypatch):
    recorder = LifecycleRecorder(tmp_path)

    def no_io(*_a, **_k):
        raise AssertionError("I/O in post")

    monkeypatch.setattr(private_file, "append_line", no_io)
    monkeypatch.setattr(private_file, "replace", no_io)
    monkeypatch.setattr(private_file, "read", no_io)
    assert recorder.post(event()) is True
    assert recorder.pending_count() == 1 and recorder.wake.is_set()


def test_drain_writes_the_log_then_the_outbox(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    made = event()
    recorder.post(made)
    assert recorder.drain() == "clean"
    assert LifecycleLog(tmp_path).events() == [made]
    assert (tmp_path / OUTBOX_FILE).exists()
    (item,) = recorder.outbox()
    assert item["unread"] and item["event_ids"] == [made["event_id"]]


def test_a_failed_lifecycle_write_latches_unhealthy_and_is_reported(tmp_path, monkeypatch):
    reported = []
    recorder = LifecycleRecorder(tmp_path, on_failure=reported.append)

    def failing(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(private_file, "append_line", failing)
    made = event()
    recorder.post(made)
    assert recorder.drain() == "unhealthy"
    assert reported == ["lifecycle_write_failed"]
    assert recorder.unhealthy_reason == "lifecycle_write_failed"
    assert recorder.pending_count() == 1  # not dropped: "drained" is not "durable"


def test_a_failed_outbox_write_latches_unhealthy(tmp_path, monkeypatch):
    reported = []
    recorder = LifecycleRecorder(tmp_path, on_failure=reported.append)

    def failing(*_a, **_k):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(private_file, "replace", failing)
    recorder.post(event())
    assert recorder.drain() == "unhealthy"
    assert reported == ["outbox_write_failed"]


def test_an_event_outside_the_schema_is_a_failure(tmp_path):
    reported = []
    recorder = LifecycleRecorder(tmp_path, on_failure=reported.append)
    assert recorder.post({**event(), "goal": "x"}) is False
    assert reported == ["lifecycle_event_invalid"]


def test_a_full_queue_is_a_failure_never_a_silent_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_PENDING", 2)
    reported = []
    recorder = LifecycleRecorder(tmp_path, on_failure=reported.append)
    assert recorder.post(event()) and recorder.post(event())
    assert recorder.post(event()) is False
    assert reported == ["lifecycle_queue_full"]


def _info(**over):
    base = {
        "subject_kind": "heartbeat",
        "subject_id": "daemon",
        "from_state": "healthy",
        "to_state": "stale",
        "reason_code": "heartbeat_stale",
    }
    base.update(over)
    return event(**base)


def test_the_dedupe_key_is_subject_incarnation_epoch_to(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    one, same_key, other_to = (
        _info(),
        _info(),
        _info(to_state="healthy", reason_code="heartbeat_resolved"),
    )
    for made in (one, same_key, other_to):
        recorder.post(made)
    recorder.drain()
    items = recorder.outbox()
    assert len(items) == 2
    joined = next(i for i in items if i["event"]["to"] == "stale")
    assert joined["event_ids"] == [one["event_id"], same_key["event_id"]] and joined["count"] == 2
    assert outbox.dedupe_key(one) == f"heartbeat|daemon|{INC}|3|stale"


def test_ack_is_idempotent_and_survives_a_restart(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    made = event()
    recorder.post(made)
    recorder.drain()
    assert recorder.unread_count() == 1
    assert recorder.ack(made["event_id"]) == {"acked": True, "already": False}
    assert recorder.ack(made["event_id"]) == {"acked": True, "already": True}
    assert recorder.ack("f" * 32)["code"] == "outbox_unknown"
    assert recorder.ack("not hex")["code"] == "outbox_unknown"
    again = LifecycleRecorder(tmp_path)
    again.repair({})
    assert again.unread_count() == 0 and len(again.outbox()) == 1


def test_unread_notices_persist_across_a_restart(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event())
    recorder.drain()
    again = LifecycleRecorder(tmp_path)
    assert again.unread_count() == 1


def test_the_outbox_is_rebuilt_from_the_log(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    made = event()
    recorder.post(made)
    recorder.drain()
    (tmp_path / OUTBOX_FILE).unlink()  # lost (or never written)
    again = LifecycleRecorder(tmp_path)
    assert again.unread_count() == 0
    assert again.repair({}) == 1
    (item,) = again.outbox()
    assert item["event_ids"] == [made["event_id"]] and item["unread"]
    assert again.repair({}) == 0  # idempotent


def test_a_latch_without_a_record_is_synthesized_as_reason_not_recorded(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    added = recorder.repair({"kill": Stop(INC, 7), "unhealthy": None})
    assert added == 1
    (item,) = recorder.outbox()
    assert item["synthesized"] is True
    assert (item["event"]["to"], item["event"]["reason_code"]) == ("stopped", "reason_not_recorded")
    assert (item["event"]["subject_id"], item["event"]["epoch"]) == ("kill", 7)
    # Recorded in the log too: the next rebuild finds it and adds nothing.
    again = LifecycleRecorder(tmp_path)
    assert again.repair({"kill": Stop(INC, 7)}) == 0


def test_a_latch_with_its_record_is_not_synthesized(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event(epoch=7))
    recorder.drain()
    assert recorder.repair({"kill": Stop(INC, 7)}) == 0
    assert [i["synthesized"] for i in recorder.outbox()] == [False]


def test_a_damaged_outbox_is_unhealthy_and_never_overwritten(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event())
    recorder.drain()
    path = tmp_path / OUTBOX_FILE
    data = json.loads(path.read_text())
    data["items"][0]["acked"] = True  # the hash no longer matches
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    before = path.read_bytes()
    reported = []
    again = LifecycleRecorder(tmp_path, on_failure=reported.append)
    assert again.unhealthy_reason == "outbox_malformed"
    again.post(event(epoch=8))
    assert again.drain() == "unhealthy"
    assert path.read_bytes() == before


# --- the service: kill latch, restart, narrowing report --------------------------------


def _record(service):
    return GrantRecord(
        kind="yolo", grant_id=GID, incarnation=service.incarnation, generation=1, state="active"
    )


def _issue(service):
    return service.issue_permit(_record(service), kind="yolo", receipt=RECEIPT)


def test_kill_persists_the_latch_and_a_restart_stays_killed(tmp_path):
    service = AuthorityService(tmp_path)
    report = service.kill_switch()
    assert report["latch"] == "set" and report["settlement"] == "settled"
    stop = LatchStore(tmp_path).latched()["kill"]
    assert stop == Stop(service.incarnation, report["epoch"])
    restarted = AuthorityService(tmp_path)
    assert restarted.killed and restarted.status()["healthy"] is False
    with pytest.raises(authority.AuthorityError, match="killed"):
        _issue(restarted)


def test_the_kill_flag_is_set_before_the_latch_is_persisted(tmp_path, monkeypatch):
    service = AuthorityService(tmp_path)
    seen = {}
    real_set = LatchStore.set

    def spy(store, name, **kw):
        seen["killed_first"] = service.killed
        return real_set(store, name, **kw)

    monkeypatch.setattr(LatchStore, "set", spy)
    service.kill_switch()
    assert seen["killed_first"] is True


def test_a_kill_whose_latch_cannot_be_written_stays_killed_and_is_unhealthy(tmp_path, monkeypatch):
    service = AuthorityService(tmp_path)

    def failing(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(private_file, "replace", failing)
    report = service.kill_switch()
    assert report["latch"] == "failed"
    assert service.killed  # never un-kills
    assert service.unhealthy_reason == "latch_store_write_failed"


def test_a_set_unhealthy_latch_starts_the_service_unhealthy(tmp_path):
    LatchStore(tmp_path).set("unhealthy", incarnation=INC, epoch=1)
    service = AuthorityService(tmp_path)
    assert service.unhealthy_reason == "latched_unhealthy" and not service.killed


def test_an_unusable_latch_store_starts_killed_and_unhealthy(tmp_path):
    (tmp_path / LATCH_FILE).write_text("not json")
    os.chmod(tmp_path / LATCH_FILE, 0o600)
    before = (tmp_path / LATCH_FILE).read_bytes()
    service = AuthorityService(tmp_path)
    assert service.killed
    assert service.unhealthy_reason == "latch_store_malformed"
    assert set(service.latches.latched()) == set(LATCHES)
    service.kill_switch()
    assert (tmp_path / LATCH_FILE).read_bytes() == before


def test_persist_unhealthy_writes_the_latch_once_with_its_event(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    service = AuthorityService(tmp_path)
    service.recorder = recorder
    assert service.persist_unhealthy() is None  # healthy: nothing
    service.report_unhealthy("lifecycle_write_failed")
    assert service.persist_unhealthy() == "lifecycle_write_failed"
    assert service.persist_unhealthy() is None  # already latched
    assert "unhealthy" in LatchStore(tmp_path).latched()
    recorder.drain()
    (item,) = recorder.outbox()
    assert (item["event"]["subject_id"], item["event"]["reason_code"]) == (
        "unhealthy",
        "lifecycle_write_failed",
    )


def test_the_lifecycle_recorder_failure_makes_the_service_unhealthy(tmp_path, monkeypatch):
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder

    def failing(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(private_file, "append_line", failing)
    service.kill_switch()
    assert recorder.drain() == "unhealthy"
    assert service.unhealthy_reason == "lifecycle_write_failed"


def test_kill_and_breaker_post_lifecycle_events(tmp_path):
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path)
    service.recorder = recorder
    service.breaker_transition("normal", "warning")
    service.breaker_transition("warning", "normal")  # a return to NORMAL: no event
    service.breaker_transition("warning", "critical")
    service.kill_switch()
    assert service.flush(timeout=30)
    recorder.drain()
    seen = [
        (
            i["event"]["subject_kind"],
            i["event"]["from"],
            i["event"]["to"],
            i["event"]["reason_code"],
        )
        for i in recorder.outbox()[::-1]
    ]
    assert seen == [
        ("breaker", "normal", "warning", "breaker_warning"),
        ("breaker", "warning", "critical", "breaker_critical"),
        ("latch", "cleared", "set", "kill_switch"),
    ]


def test_commit_at_epoch_refuses_on_the_kill_flag_alone(tmp_path):
    """W1 review gap: the flag without the epoch move (``kill()`` always moves both)."""
    service = AuthorityService(tmp_path)
    epoch = service.epoch
    service._killed = True  # the flag's lock-free store, before the epoch would move
    calls = []
    assert service.commit_at_epoch(calls.append, expected=epoch) == (False, epoch, None)
    assert calls == []


def test_narrow_reports_cancelling_while_another_owner_settles(tmp_path):
    service = AuthorityService(tmp_path)
    current = _record(service)
    entered, release = threading.Event(), threading.Event()

    def slow(_reason):
        entered.set()
        release.wait(60)

    service.consume(_issue(service), current, kind="yolo", on_cancel=slow)
    first = threading.Thread(target=service.narrow, args=("revoked",))
    first.start()
    assert entered.wait(30)
    try:
        report = service.narrow_result("tool_rules_saved")
        assert report["settlement"] == "cancelling"
        kill = service.kill_switch()
        assert kill["settlement"] == "cancelling" and service.killed
    finally:
        release.set()
        first.join(30)
    assert service.flush(timeout=30)
    assert service.narrow_result("revoked")["settlement"] == "settled"


# --- bounded cancel callbacks (W1 precondition) ----------------------------------------


def _until(condition, timeout: float = 30.0) -> bool:
    """Poll ``condition`` until true or ``timeout`` (generous: CI under load)."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def test_a_blocked_callback_is_abandoned_never_clean_and_never_run_twice(tmp_path):
    """M1: an abandoned callback is counted until it returns; until then nothing is
    clean or settled, the service is unhealthy (``cancel_overdue``), the rest of its
    batch runs exactly once (not again when the stuck owner returns)."""
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 0.05
    service.settle_wait = 0.1
    current = _record(service)
    entered, release = threading.Event(), threading.Event()
    later = []

    def blocked(_reason):
        entered.set()
        release.wait(60)

    service.consume(_issue(service), current, kind="yolo", on_cancel=blocked)
    service.consume(_issue(service), current, kind="yolo", on_cancel=later.append)
    try:
        report = service.narrow_result("revoked")  # returns: the worker owns the callback
        assert report["settlement"] == "cancelling"
        assert entered.wait(30)
        time.sleep(0.1)  # past the deadline
        assert service.rescue_overdue() is True
        assert service.unhealthy_reason == "cancel_overdue"
        assert service.flush_result(timeout=1) == "pending"  # never clean while it runs
        assert service.flush_result() == "pending"  # also without a deadline, at once
        assert later == ["narrowing"]  # the rest of the stuck batch ran
        assert service.status()["cancel_abandoned"] == 1
        assert service.narrow_result("tool_rules_saved")["settlement"] == "cancelling"
        assert any(r.get("outcome") == "cancel_overdue" for r in service.stop_records())
    finally:
        release.set()
    assert _until(lambda: service.status()["cancel_abandoned"] == 0)
    assert service.status()["cancel_abandoned"] == 0
    assert later == ["narrowing"]  # the stuck owner touched nothing more
    assert service.flush_result(timeout=30) == "unhealthy"  # drained, never "clean"


def test_a_callback_within_its_deadline_is_not_abandoned(tmp_path):
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 5.0
    current = _record(service)
    calls = []
    service.consume(_issue(service), current, kind="yolo", on_cancel=calls.append)
    service.narrow("revoked")
    assert service.rescue_overdue() is False
    assert calls == ["narrowing"] and service.status()["cancel_overdue"] == 0


def test_the_shutdown_flush_abandons_a_stuck_callback_and_reports_pending(tmp_path):
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 0.05
    service.settle_wait = 0.05
    entered, release = threading.Event(), threading.Event()

    def blocked(_reason):
        entered.set()
        release.wait(60)

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=blocked)
    service.narrow("revoked")
    try:
        assert entered.wait(30)
        assert service.flush_result(timeout=1) == "pending"
        assert service.status()["cancel_overdue"] == 1
        assert service.unhealthy_reason == "cancel_overdue"
    finally:
        release.set()


def test_kill_is_durable_before_a_blocked_callback_returns(tmp_path):
    """B1 (Astra KILL_BLOCKED / RESCUED_KILL): the kill latch is on disk before any
    callback runs; the kill returns at once with ``cancelling``; the callback never
    runs on the kill caller's thread."""
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 0.05
    service.settle_wait = 0.1
    entered, release = threading.Event(), threading.Event()
    seen = {}

    def blocked(_reason):
        seen["thread"] = threading.current_thread()
        seen["durable"] = LatchStore(tmp_path).is_set("kill")  # before any callback ran
        entered.set()
        release.wait(60)

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=blocked)
    try:
        started = time.monotonic()
        report = service.kill_switch()
        assert time.monotonic() - started < 20.0  # the callback blocks 60 s
        assert report["latch"] == "set" and report["settlement"] == "cancelling"
        assert LatchStore(tmp_path).is_set("kill")  # KILL_BLOCKED: durable already
        from stiracore.authority import epoch_store

        committed = epoch_store.load(tmp_path).last.commitment
        assert committed == LatchStore(tmp_path).commitment()  # and committed already
        assert entered.wait(30)
        assert seen["durable"] is True
        assert seen["thread"] is not threading.current_thread()
        # The kill's executor leaves an overdue callback behind (no rescue needed).
        assert _until(lambda: service.unhealthy_reason == "cancel_overdue")
        assert service.flush_result(timeout=0.5) != "clean"  # RESCUED_KILL
        assert LatchStore(tmp_path).is_set("kill")
        assert AuthorityService(tmp_path).killed  # a restart now stays killed
    finally:
        release.set()


def test_the_kill_commits_the_latch_into_the_epoch_store(tmp_path):
    service = AuthorityService(tmp_path)
    service.kill_switch()
    from stiracore.authority import epoch_store

    last = epoch_store.load(tmp_path).last
    assert last.commitment == LatchStore(tmp_path).commitment()


def test_a_running_callback_within_its_deadline_is_left_alone(tmp_path):
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 30.0
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(reason):
        entered.set()
        release.wait(60)
        calls.append(reason)

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=slow)
    owner = threading.Thread(target=service.narrow, args=("revoked",))
    owner.start()
    try:
        assert entered.wait(30)
        assert service.rescue_overdue() is False  # running, but not overdue
    finally:
        release.set()
        owner.join(30)
    assert calls == ["narrowing"]
    assert service.flush_result(timeout=30) == "clean"
    assert service.status()["cancel_overdue"] == 0


def test_an_outbox_replaced_behind_the_recorder_is_not_overwritten(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event())
    recorder.drain()
    path = tmp_path / OUTBOX_FILE
    # A second writer's outbox replaces the file (only the outbox: since #661 an outside
    # append to the lifecycle log is caught first, as lifecycle_log_replaced).
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = LifecycleRecorder(other_dir)
    other.post(event(epoch=9))
    other.drain()
    os.replace(other_dir / OUTBOX_FILE, path)
    before = path.read_bytes()
    reported = []
    recorder._on_failure = reported.append
    recorder.post(event(epoch=10))
    assert recorder.drain() == "unhealthy"
    assert reported == ["outbox_replaced"]
    assert path.read_bytes() == before


# --- fix round 1: M2 (no resurrection), M5 (bounded outbox), numeric bounds -------------


def _breaker(n):
    return make_event(
        "breaker",
        "engine",
        "normal",
        "warning",
        "breaker_warning",
        incarnation=INC,
        epoch=n,
        ts_wall=100.0,
    )


def test_an_acknowledged_trimmed_notice_never_comes_back(tmp_path):
    """Astra's 256-notice probe: ack the oldest, add one more, repair, restart."""
    recorder = LifecycleRecorder(tmp_path)
    for n in range(256):
        recorder.post(_breaker(n))
    recorder.drain()
    first = recorder.outbox()[-1]["event_ids"][0]
    assert recorder.ack(first) == {"acked": True, "already": False}
    recorder.post(_breaker(256))
    recorder.drain()  # the acknowledged notice is trimmed for room
    assert recorder.unread_count() == 256
    assert recorder.ack(first) == {"acked": True, "already": True}
    recorder.repair({})
    assert recorder.unread_count() == 256  # not resurrected
    assert recorder.ack(first) == {"acked": True, "already": True}
    again = LifecycleRecorder(tmp_path)
    again.repair({})
    assert again.unread_count() == 256
    assert again.ack(first) == {"acked": True, "already": True}


def test_many_unread_notices_are_coalesced_and_the_outbox_stays_reopenable(tmp_path, monkeypatch):
    """Astra's 13-batch probe: 200 unique events per batch, nobody acknowledges."""
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    recorder = LifecycleRecorder(tmp_path)
    for batch in range(13):
        for i in range(200):
            recorder.post(_breaker(batch * 200 + i))
        assert recorder.drain() == "clean", batch
    assert recorder.unhealthy_reason is None
    size = (tmp_path / OUTBOX_FILE).stat().st_size
    assert size < outbox._MAX_BYTES // 2
    again = LifecycleRecorder(tmp_path)
    assert again.unhealthy_reason is None
    again.repair({})
    items = again.outbox()
    assert len(items) <= outbox.MAX_ITEMS
    coalesced = [i for i in items if i["coalesced"]]
    assert len(coalesced) == 1 and coalesced[0]["unread"]
    total = sum(i["count"] for i in items)
    assert total == 13 * 200  # nothing unread was dropped: counted in the coalesced one
    assert again.ack(coalesced[0]["event_ids"][-1]) == {"acked": True, "already": False}
    assert LifecycleRecorder(tmp_path).unhealthy_reason is None


def test_huge_numbers_are_outside_the_schema():
    made = event()
    for field, value in [
        ("epoch", 10**1000),
        ("epoch", 1 << 60),
        ("ts_wall", 10**400),
        ("ts_wall", 1e300),
    ]:
        with pytest.raises(LifecycleSchemaError):
            validate({**made, field: value})


# --- fix round 1: M3 (the files themselves) ---------------------------------------------


def test_a_latch_file_corrupted_while_running_refuses_consume(tmp_path):
    from stiracore.authority.watchdog import Watchdog

    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder
    service.latches.set("halted:budget", incarnation=service.incarnation, epoch=service.epoch)
    permit = _issue(service)
    (tmp_path / LATCH_FILE).write_bytes(b"bad")
    dog = Watchdog(service, recorder, data_dir=tmp_path)
    assert dog.run_pass() is False
    assert service.unhealthy_reason == "latch_store_malformed" and service.killed
    with pytest.raises(authority.AuthorityError):
        service.consume(permit, _record(service), kind="yolo")


@pytest.mark.parametrize("target", ["epoch", "log", "outbox"])
def test_a_file_changed_while_running_makes_the_authority_unhealthy(tmp_path, target):
    from stiracore.authority import epoch_store
    from stiracore.authority.watchdog import Watchdog

    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder
    recorder.post(event())
    recorder.drain()
    dog = Watchdog(service, recorder, data_dir=tmp_path)
    assert dog.run_pass() is True
    path = {
        "epoch": tmp_path / epoch_store.EPOCH_FILE,
        "log": tmp_path / LOG_FILE,
        "outbox": tmp_path / OUTBOX_FILE,
    }[target]
    raw = path.read_bytes()
    path.write_bytes(raw[:-3] + b"xx\n")  # damaged in place (same inode)
    permit_record = _record(service)
    dog.run_pass()
    assert service.unhealthy_reason is not None
    with pytest.raises(authority.AuthorityError, match="authority_unhealthy"):
        _issue(service)
    assert permit_record  # (the grant record itself is fine)


def test_a_synchronous_owner_hands_a_batch_with_a_callback_to_the_worker(tmp_path, monkeypatch):
    """A narrowing settled on the caller's thread (no callback queued) meets a later batch
    with a callback: it hands it to the worker instead of running it itself."""
    from stiracore.authority import epoch_store

    service = AuthorityService(tmp_path)
    service.settle_wait = 0.05
    real_write = epoch_store.write
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(epoch_store, "write", blocked)
    caller = threading.Thread(target=service.narrow, args=("tool_rules_saved",))
    caller.start()
    assert entered.wait(30)  # the caller owns the queue, inside its epoch write
    seen = []
    service.consume(
        _issue(service),
        _record(service),
        kind="yolo",
        on_cancel=lambda _r: seen.append(threading.current_thread()),
    )
    service.advance_epoch()  # a deferred batch with a callback, queued behind the caller
    monkeypatch.setattr(epoch_store, "write", real_write)
    release.set()
    caller.join(30)
    assert service.flush(timeout=30)
    assert len(seen) == 1 and seen[0] is not caller
    assert seen[0].name == "authority-settle"


def test_an_owner_that_returns_after_its_rescue_leaves_the_new_owner_alone(tmp_path):
    """The stuck callback is the last of its batch: when it returns, its owner must not
    touch the new owner's state — else a second rescue would lose the rest of the new
    owner's batch (e3 never cancelled)."""
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 0.05
    service.settle_wait = 0.05
    current = _record(service)
    a_in, a_out, b_in, b_out = (threading.Event() for _ in range(4))
    later = []

    def stuck_a(_reason):
        a_in.set()
        a_out.wait(60)

    def stuck_b(_reason):
        b_in.set()
        b_out.wait(60)

    service.consume(_issue(service), current, kind="yolo", on_cancel=stuck_a)
    service.narrow("revoked")  # batch 1 = [a]
    try:
        assert a_in.wait(30)
        time.sleep(0.1)
        assert service.rescue_overdue() is True
        assert service.unhealthy_reason == "cancel_overdue"
        service._unhealthy = None  # white box: only to start two more effects for the test
        service.consume(_issue(service), current, kind="yolo", on_cancel=stuck_b)
        service.consume(_issue(service), current, kind="yolo", on_cancel=later.append)
        service.advance_epoch()  # batch 2 = [b, e3] on the new worker
        assert b_in.wait(30)
        a_out.set()  # the first owner returns now
        assert _until(lambda: service.status()["cancel_abandoned"] == 0)
        time.sleep(0.1)
        assert service.rescue_overdue() is True  # b is overdue too
        assert _until(lambda: later)
        assert later == ["narrowing"]
    finally:
        a_out.set()
        b_out.set()


def test_a_kill_whose_latch_fails_is_reported_in_the_outbox(tmp_path, monkeypatch):
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path)
    service.recorder = recorder

    def failing(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(private_file, "replace", failing)
    assert service.kill_switch()["latch"] == "failed"
    queued = [(e["subject_kind"], e["to"], e["reason_code"]) for e in recorder._pending]
    assert ("authority", "unhealthy", "latch_store_write_failed") in queued


def test_a_callback_runs_at_most_once_across_a_rescue(tmp_path):
    """Opus M5 (a): each cancel callback of a rescued batch runs exactly once — neither
    the new owner nor the returning stuck owner runs one again."""
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 0.05
    service.settle_wait = 0.05
    current = _record(service)
    entered, release = threading.Event(), threading.Event()
    runs = {"stuck": 0, "rest": 0}

    def stuck(_reason):
        runs["stuck"] += 1
        entered.set()
        release.wait(60)

    def rest(_reason):
        runs["rest"] += 1

    service.consume(_issue(service), current, kind="yolo", on_cancel=stuck)
    for _ in range(3):
        service.consume(_issue(service), current, kind="yolo", on_cancel=rest)
    service.narrow("revoked")
    try:
        assert entered.wait(30)
        time.sleep(0.1)
        assert service.rescue_overdue() is True
        assert _until(lambda: runs["rest"] == 3)
    finally:
        release.set()
    assert _until(lambda: service.status()["cancel_abandoned"] == 0)
    time.sleep(0.05)
    assert runs == {"stuck": 1, "rest": 3}


def test_an_outbox_rewritten_in_place_is_not_overwritten(tmp_path):
    """Opus lesser survivor: the same inode, other bytes (the raw-content compare)."""
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event())
    recorder.drain()
    path = tmp_path / OUTBOX_FILE
    raw = path.read_bytes()
    with open(path, "r+b") as handle:  # in place: identity unchanged
        handle.write(raw.replace(b'"acked": false', b'"acked": true ', 1))
    before = path.read_bytes()
    assert before != raw
    reported = []
    recorder._on_failure = reported.append
    recorder.post(event(epoch=10))
    assert recorder.drain() == "unhealthy"
    assert reported == ["outbox_replaced"] and path.read_bytes() == before


def test_an_existing_owner_never_runs_the_kill_batch_before_the_kill_is_durable(tmp_path):
    """Fix 2, A-M1 (Astra's probe): an owner already settling an earlier batch reaches
    the kill's batch while the kill latch is still being written — it must wait."""
    service = AuthorityService(tmp_path)
    service.settle_wait = 0.01
    entered, release = threading.Event(), threading.Event()
    paused, persist, second = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def first(_reason):
        entered.set()
        release.wait(60)

    def later(_reason):
        from stiracore.authority import epoch_store

        store = LatchStore(tmp_path)
        committed = epoch_store.load(tmp_path).last.commitment
        seen.append(store.is_set("kill") and committed == store.commitment())
        second.set()

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=first)
    service.narrow("revoked")  # the worker owns the queue, inside callback A
    assert entered.wait(30)
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=later)
    real = service._persist_kill

    def delayed(epoch):
        paused.set()
        persist.wait(60)
        return real(epoch)

    service._persist_kill = delayed
    killer = threading.Thread(target=service.kill_switch)
    killer.start()
    try:
        assert paused.wait(30)
        release.set()  # the existing owner goes on to the next batch: the kill's
        assert not second.wait(0.3)  # held: callback B does not run yet
        persist.set()
        assert second.wait(30)
        assert seen == [True]  # it ran only after the kill was on disk
    finally:
        release.set()
        persist.set()
        killer.join(30)
    assert service.flush_result(timeout=30) == "clean"


# --- fix round 2: older segment (A-M2), offline writers (A-M4) ---------------------------


def test_an_in_place_change_of_the_older_segment_makes_the_authority_unhealthy(
    tmp_path, monkeypatch
):
    from stiracore.authority.watchdog import Watchdog

    monkeypatch.setattr(lifecycle_events, "ROTATE_BYTES", 1500)
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder
    for epoch in range(1, 8):
        recorder.post(event(epoch=epoch))
    recorder.drain()
    old = tmp_path / lifecycle_events.OLD_LOG_FILE
    assert old.exists()
    dog = Watchdog(service, recorder, data_dir=tmp_path)
    assert dog.run_pass() is True
    raw = bytearray(old.read_bytes())
    raw[0:1] = b"[" if raw[0:1] != b"[" else b"{"  # same size, same inode
    with open(old, "r+b") as handle:
        handle.write(bytes(raw))
    dog.run_pass()
    assert service.unhealthy_reason == "lifecycle_log_replaced"
    with pytest.raises(authority.AuthorityError, match="authority_unhealthy"):
        _issue(service)


def test_an_offline_append_to_a_near_cap_log_rotates_and_reopens(tmp_path, monkeypatch):
    """The host commands open the log with ``rotate=False``: appends are bounded anyway."""
    monkeypatch.setattr(lifecycle_events, "ROTATE_BYTES", 2000)
    log = LifecycleLog(tmp_path)
    epoch = 0
    while not (tmp_path / LOG_FILE).exists() or (tmp_path / LOG_FILE).stat().st_size < 1800:
        epoch += 1
        log.append(event(epoch=epoch))
    offline = LifecycleLog(tmp_path, rotate=False)  # as `stira authority unlatch`
    for epoch in range(100, 106):
        offline.append(event(epoch=epoch))
        assert (tmp_path / LOG_FILE).stat().st_size <= 2000 + 600
    assert LifecycleLog(tmp_path, rotate=False).unhealthy_reason is None
    assert LifecycleLog(tmp_path).unhealthy_reason is None


# --- fix round 2: stop notices are never coalesced (A-M3) -------------------------------


def test_seen_and_acked_ids_are_pruned_to_the_log(tmp_path, monkeypatch):
    """O-M2 (a): the outbox's id sets stay bounded by the retained log."""
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    monkeypatch.setattr(lifecycle_events, "ROTATE_BYTES", 4000)
    recorder = LifecycleRecorder(tmp_path)
    for n in range(60):
        recorder.post(_breaker(n))
        recorder.drain()
        if n % 7 == 0:
            recorder.ack(recorder.outbox()[0]["event_ids"][0])
    recorder.repair({})
    in_log = recorder.log.event_ids()
    assert len(in_log) < 60  # the oldest segment was dropped
    assert recorder._seen <= in_log and recorder._acked <= in_log
    data = json.loads((tmp_path / OUTBOX_FILE).read_text())
    assert set(data["seen"]) <= in_log and set(data["acked"]) <= in_log


# --- fix round 3: a hung kill persistence is bounded (A-M1) -----------------------------


def test_a_hung_kill_persistence_releases_the_callbacks_after_its_deadline(tmp_path):
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path)
    service.recorder = recorder
    service.kill_persist_deadline = 0.2
    service.settle_wait = 0.05
    hung = threading.Event()
    timer = threading.Timer(10.0, hung.set)  # a mutated (unbounded) hold ends here
    timer.start()
    real = service._persist_kill

    def stuck(epoch):
        hung.wait(30)
        return real(epoch)

    service._persist_kill = stuck
    ran = threading.Event()
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=lambda _r: ran.set())
    try:
        report = service.kill_switch()
        assert report["latch"] == "timeout"
        assert ran.wait(30)  # the callback ran although the persistence still hangs
        assert not hung.is_set()
        assert service.killed and service.unhealthy_reason == "kill_persist_timeout"
        queued = [(e["subject_kind"], e["reason_code"]) for e in recorder._pending]
        assert ("authority", "kill_persist_timeout") in queued
    finally:
        hung.set()
        timer.cancel()
    assert _until(lambda: LatchStore(tmp_path).is_set("kill"))  # the late write still lands
    assert service.killed


def test_a_stopped_or_unhealthy_transition_of_any_subject_is_a_stop_notice():
    made = _info(to_state="unhealthy")  # a heartbeat subject, a non-stop reason code
    assert outbox.critical(made) is True
    assert outbox.critical(_info()) is False


def test_a_single_informational_notice_is_not_coalesced_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ITEMS", 2)
    recorder = LifecycleRecorder(tmp_path)
    recorder.post(event())  # a kill and an unhealthy: two stop notices
    recorder.post(
        event(
            subject_kind="authority",
            subject_id="service",
            from_state="healthy",
            to_state="unhealthy",
            reason_code="lifecycle_write_failed",
        )
    )
    recorder.post(_breaker(1))  # one informational: nothing to merge it with
    recorder.drain()
    assert [i["coalesced"] for i in recorder.outbox()] == [0, 0, 0]


# --- fix round 4: the kill's callbacks never wait on epoch I/O or a worker (item 1, 2) ---


def test_a_kill_callback_runs_although_the_owner_is_stuck_in_the_epoch_write(tmp_path, monkeypatch):
    """Opus r4 P1: a settle owner blocked inside ``epoch_store.write`` (a hung disk)
    does not hold the kill's callbacks; unhealthy is reported."""
    from stiracore.authority import epoch_store

    service = AuthorityService(tmp_path)
    service.kill_persist_deadline = 0.3
    service.settle_wait = 0.05
    real_write = epoch_store.write
    entered, release = threading.Event(), threading.Event()

    def hung(*args, **kwargs):
        entered.set()
        release.wait(60)
        return real_write(*args, **kwargs)

    first = []
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=first.append)
    monkeypatch.setattr(epoch_store, "write", hung)
    service.advance_epoch()  # the worker runs callback A, then hangs in the epoch write
    try:
        assert entered.wait(30) and first == ["narrowing"]
        ran = threading.Event()
        service.consume(
            _issue(service), _record(service), kind="yolo", on_cancel=lambda _r: ran.set()
        )
        report = service.kill_switch()
        assert ran.wait(30)  # the kill's callback ran while the epoch write still hangs
        assert not release.is_set()
        assert service.killed and service.unhealthy_reason == "kill_persist_timeout"
        assert report["latch"] == "timeout"
    finally:
        release.set()
    assert service.flush_result(timeout=30) == "unhealthy"


def test_a_kill_whose_persistence_thread_cannot_start_still_cancels(tmp_path, monkeypatch):
    """Astra r4: no inline (unbounded) persistence when its thread cannot start — here
    the disk hangs, so an inline attempt would hang the kill itself."""
    service = AuthorityService(tmp_path)
    ran, hung = threading.Event(), threading.Event()
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=lambda _r: ran.set())
    real = service._persist_kill
    service._persist_kill = lambda epoch: (hung.wait(60), real(epoch))[1]
    real_start = threading.Thread.start

    def start(self):
        if self.name == "authority-kill-persist":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    reports = []
    killer = threading.Thread(target=lambda: reports.append(service.kill_switch()))
    killer.start()
    try:
        killer.join(30)
        assert not killer.is_alive()  # the kill returned although the disk hangs
        assert reports[0]["latch"] == "pending"  # #618 K2: handed to the watchdog
        assert ran.wait(30)
        assert service.killed and service.unhealthy_reason == "kill_persist_unavailable"
        assert service.status()["kill_durable"] is False
    finally:
        hung.set()
        killer.join(30)


def test_flush_is_pending_while_a_kill_callback_runs(tmp_path):
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 60.0
    service.settle_wait = 0.05
    entered, release = threading.Event(), threading.Event()

    def slow(_reason):
        entered.set()
        release.wait(60)

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=slow)
    try:
        assert service.kill_switch()["settlement"] == "cancelling"
        assert entered.wait(30)
        assert service.flush_result(timeout=0.3) == "pending"  # never clean meanwhile
    finally:
        release.set()
    assert _until(lambda: service.flush_result(timeout=1) == "clean")


def test_repeated_kills_on_a_hung_disk_share_one_persistence_thread(tmp_path):
    """Astra r4 M2: 50 timed-out kills -> one persistence thread; once the disk
    recovers the kill is recorded."""
    service = AuthorityService(tmp_path)
    service.kill_persist_deadline = 0.01
    service.settle_wait = 0.0
    hung = threading.Event()
    real = service._persist_kill

    def stuck(epoch):
        hung.wait(60)
        return real(epoch)

    service._persist_kill = stuck
    try:
        for _ in range(50):
            assert service.kill_switch()["latch"] == "timeout"
        persisting = [t for t in threading.enumerate() if t.name == "authority-kill-persist"]
        assert len(persisting) == 1
    finally:
        hung.set()
    assert _until(lambda: LatchStore(tmp_path).is_set("kill"))
    assert service.killed and service.unhealthy_reason == "kill_persist_timeout"


# --- fix round 4: stop notices keyed by code only (item 3), acks as watermarks (item 4) --


def _yolo_stop(n, reason="aborted_issue_failed"):
    return event(
        subject_kind="yolo_window",
        subject_id=f"{n + 1:032x}",
        from_state="unknown",
        to_state="ended",
        reason_code=reason,
        incarnation=f"{n % 7 + 1:032x}",
        epoch=n,
    )


def test_a_stop_notice_is_keyed_by_its_code_and_keeps_first_newest_and_count(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    kills = [event(epoch=n, incarnation=f"{n % 3 + 1:032x}") for n in range(40)]
    for made in kills:
        recorder.post(made)
    recorder.drain()
    (item,) = recorder.outbox()
    assert item["count"] == 40
    assert item["event_ids"][0] == kills[0]["event_id"]
    assert item["event_ids"][-1] == kills[-1]["event_id"]  # the newest is kept last
    assert item["event"]["epoch"] == 39  # the newest occurrence is shown
    assert outbox.notice_key(kills[0]) == "stop|latch|kill|kill_switch"
    assert outbox.notice_key(_yolo_stop(1)) == "stop|yolo_window|*|aborted_issue_failed"


def test_kill_notices_among_many_others_stay_one_shown_ackable_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    recorder = LifecycleRecorder(tmp_path)
    kills = []
    for n in range(300):
        if n in (0, 20, 150, 299):
            made = event(epoch=10_000 + n, incarnation=f"{n + 1:032x}")
            kills.append(made["event_id"])
        else:
            made = _breaker(n)
        recorder.post(made)
        if n % 100 == 99:
            assert recorder.drain() == "clean"
    again = LifecycleRecorder(tmp_path)
    again.repair({})
    (kill,) = [i for i in again.outbox() if i["event"]["reason_code"] == "kill_switch"]
    assert not kill["coalesced"] and kill["count"] == 4 and kill["unread"]
    assert kill["event_ids"][-1] == kills[-1]
    assert sum(i["count"] for i in again.outbox()) == 300
    assert again.ack(kills[-1]) == {"acked": True, "already": False}


def test_five_thousand_stops_with_unique_window_ids_stay_bounded_and_ackable(tmp_path, monkeypatch):
    """Astra r4 M3: the existing YOLO producer, a new random window id per stop."""
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    recorder = LifecycleRecorder(tmp_path)
    reasons = ["aborted_issue_failed", "revoked_clock_anomaly", "revoked_audit_failed"]
    for n in range(5000):
        recorder.post(_yolo_stop(n, reasons[n % 3]))
        if n % 200 == 199:
            assert recorder.drain() == "clean", n
    assert recorder.drain() == "clean"
    assert (tmp_path / OUTBOX_FILE).stat().st_size < outbox._MAX_BYTES // 4
    again = LifecycleRecorder(tmp_path)
    again.repair({})
    items = again.outbox()
    assert len(items) == 3 and sum(i["count"] for i in items) == 5000
    for item in items:
        assert again.ack(item["event_ids"][-1]) == {"acked": True, "already": False}


def test_five_thousand_stop_notices_of_mixed_kinds_stay_bounded_and_ackable(tmp_path, monkeypatch):
    monkeypatch.setattr(private_file, "_flush", lambda _fd: None)  # speed only
    recorder = LifecycleRecorder(tmp_path)
    kinds = [
        ("latch", "kill", "cleared", "set", "kill_switch"),
        ("authority", "service", "healthy", "unhealthy", "lifecycle_write_failed"),
        ("authority", "service", "healthy", "unhealthy", "kill_persist_timeout"),
        ("latch", "unhealthy", "cleared", "set", "cancel_overdue"),
        ("latch", "halted:budget", "unknown", "stopped", "reason_not_recorded"),
        ("latch", "kill", "unknown", "stopped", "reason_not_recorded"),
    ]
    for n in range(5000):
        kind, ident, before, after, reason = kinds[n % len(kinds)]
        recorder.post(
            event(
                subject_kind=kind,
                subject_id=ident,
                from_state=before,
                to_state=after,
                reason_code=reason,
                incarnation=f"{n % 20 + 1:032x}",
                epoch=n,
            )
        )
        if n % 200 == 199:
            assert recorder.drain() == "clean", n
    assert recorder.drain() == "clean"
    items = LifecycleRecorder(tmp_path).outbox()
    shown = {
        (i["event"]["subject_kind"], i["event"]["subject_id"], i["event"]["reason_code"])
        for i in items
    }
    assert shown == {(k, i, r) for k, i, _b, _a, r in kinds}  # each stop code apart
    assert len(items) == len(kinds)


def test_a_replayed_acknowledgement_never_hides_a_newer_stop(tmp_path):
    """Astra r4 M4: kill E1, ack E1, kill E2: replaying ack(E1) leaves E2 unread."""
    recorder = LifecycleRecorder(tmp_path)
    first = event(epoch=1)
    recorder.post(first)
    recorder.drain()
    assert recorder.ack(first["event_id"]) == {"acked": True, "already": False}
    second = event(epoch=2)
    recorder.post(second)
    recorder.drain()
    assert recorder.unread_count() == 1
    assert recorder.ack(first["event_id"]) == {"acked": True, "already": True}
    assert recorder.unread_count() == 1  # the replay changed nothing
    assert recorder.ack(second["event_id"]) == {"acked": True, "already": False}
    assert recorder.unread_count() == 0


def test_an_older_unacknowledged_id_of_a_repeated_notice_is_stale(tmp_path):
    recorder = LifecycleRecorder(tmp_path)
    one, two = event(epoch=1), event(epoch=2)
    recorder.post(one)
    recorder.post(two)
    recorder.drain()
    assert recorder.ack(one["event_id"]) == {"acked": False, "code": "outbox_stale"}
    assert recorder.unread_count() == 1


# --- W4.5 (#618 K1, K2, K4, T1-T5): pre-W5 hardening of the kill path -------------------


def _hung_epoch_write(monkeypatch):
    """``epoch_store.write`` blocks until released (a hung disk under the settle owner)."""
    from stiracore.authority import epoch_store

    real_write = epoch_store.write
    entered, release = threading.Event(), threading.Event()

    def hung(*args, **kwargs):
        entered.set()
        release.wait(60)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(epoch_store, "write", hung)
    return entered, release


def test_a_kill_cancels_an_effect_queued_behind_an_owner_stuck_in_the_epoch_write(
    tmp_path, monkeypatch
):
    """#618 K1 (Opus W2 r5 probe A1b): effect A narrowed, its owner hangs in the epoch
    write; effect B narrowed too (its batch queued behind that owner); the kill runs B's
    callback within its own deadline — and exactly once (not again by the owner)."""
    service = AuthorityService(tmp_path)
    service.kill_persist_deadline = 0.3
    service.settle_wait = 0.05
    first = []
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=first.append)
    entered, release = _hung_epoch_write(monkeypatch)
    service.advance_epoch()  # the worker runs A's callback, then hangs in the write
    try:
        assert entered.wait(30) and first == ["narrowing"]
        calls, ran = [], threading.Event()

        def cancel_b(reason):
            calls.append(reason)
            ran.set()

        service.consume(_issue(service), _record(service), kind="yolo", on_cancel=cancel_b)
        service.breaker_transition("normal", "warning")  # B -> stopping, its batch queued
        assert not ran.wait(0.2)  # held behind the stuck owner (the gap K1 closes)
        service.kill_switch()
        assert ran.wait(10)  # the kill ran it while the epoch write still hangs
        assert not release.is_set()
    finally:
        release.set()
    assert service.flush_result(timeout=30) == "unhealthy"
    assert calls == ["narrowing"] and first == ["narrowing"]  # each exactly once


def test_a_kill_whose_persistence_thread_cannot_start_is_written_by_the_watchdog(
    tmp_path, monkeypatch
):
    """#618 K2: the latch write is handed to the running watchdog (single flight);
    meanwhile ``pending`` and ``kill_durable: false``; afterwards a restart is killed by
    the latch itself (not only by the lifecycle log)."""
    from stiracore.authority.watchdog import Watchdog

    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder
    real_start = threading.Thread.start

    def start(self):
        if self.name == "authority-kill-persist":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    first = service.kill_switch()
    second = service.kill_switch()  # while handed over: the same attempt, no new one
    assert first["latch"] == "pending" and second["latch"] == "pending"
    status = service.status()
    assert status["killed"] is True and status["kill_durable"] is False
    assert status["unhealthy_reason"] == "kill_persist_unavailable"
    assert not LatchStore(tmp_path).is_set("kill")
    assert not os.path.lexists(tmp_path / "authority-kill-fired")
    monkeypatch.setattr(threading.Thread, "start", real_start)
    dog = Watchdog(service, recorder, data_dir=tmp_path)
    dog.run_pass()
    assert LatchStore(tmp_path).is_set("kill")
    assert os.path.lexists(tmp_path / "authority-kill-fired")
    assert service.status()["kill_durable"] is True
    assert service.persist_handed_kill() is None  # taken once
    restarted = AuthorityService(tmp_path)
    assert restarted.killed and restarted.latches.unhealthy_reason is None
    assert "kill" in restarted.latches.latched()
    assert restarted.status()["kill_durable"] is True


def test_an_exception_before_the_kill_callbacks_still_runs_and_releases_them(tmp_path):
    """#618 K4: the ``_kill_pending`` accounting is structural (``try/finally``)."""
    service = AuthorityService(tmp_path)
    service.settle_wait = 0.05
    ran = threading.Event()
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=lambda _r: ran.set())

    def boom(_epoch):
        raise RuntimeError("unexpected")

    service._persist_kill_bounded = boom
    with pytest.raises(RuntimeError):
        service.kill_switch()
    assert service.killed
    assert ran.wait(10)
    assert _until(lambda: service._kill_pending == 0, 10)
    assert service.flush_result(timeout=10) == "unhealthy"  # never pending forever


def _fail_thread(monkeypatch, name):
    real_start = threading.Thread.start

    def start(self):
        if self.name == name:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)


def test_a_kill_callback_whose_thread_cannot_start_latches_cancel_unavailable(
    tmp_path, monkeypatch
):
    """#618 T1: that effect goes uncancelled, so flush must never report clean."""
    service = AuthorityService(tmp_path)
    service.settle_wait = 0.05
    calls = []
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=calls.append)
    _fail_thread(monkeypatch, "authority-kill-callback")
    service.kill_switch()
    assert _until(lambda: service._kill_pending == 0, 10)
    assert service.unhealthy_reason == "cancel_unavailable"
    assert service.flush_result(timeout=10) == "unhealthy"
    assert calls == []


def test_the_kill_executor_skips_an_effect_that_completed_during_the_persistence(tmp_path):
    """#618 T2: the ``entry.state == "stopping"`` re-check of the kill executor."""
    service = AuthorityService(tmp_path)
    calls = []
    entry = service.consume(_issue(service), _record(service), kind="yolo", on_cancel=calls.append)
    real = service._persist_kill

    def persist(epoch):
        entry.done()  # the effect ends while the kill is being persisted
        return real(epoch)

    service._persist_kill = persist
    assert service.kill_switch()["latch"] == "set"
    assert service.flush_result(timeout=10) == "clean"
    assert entry.state == "completed_after_stop" and calls == []


def test_wait_settled_waits_for_a_running_kill_callback(tmp_path):
    """#618 T3: ``_wait_settled`` honours ``_kill_pending``."""
    service = AuthorityService(tmp_path)
    service.cancel_deadline = 60.0
    service.settle_wait = 0.05
    entered, release = threading.Event(), threading.Event()

    def slow(_reason):
        entered.set()
        release.wait(60)

    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=slow)
    try:
        epoch = service.kill_switch()["epoch"]
        assert entered.wait(30)
        assert _until(lambda: service._frontier >= epoch and service._persisted >= epoch, 10)
        assert service._wait_settled(epoch) is False  # the callback still runs
    finally:
        release.set()
    assert _until(lambda: service._wait_settled(epoch), 10)


def test_an_ack_records_every_id_of_the_item(tmp_path):
    """#618 T4: after acking the newest id, an older id of the same notice is ``already``
    (not ``outbox_stale``)."""
    recorder = LifecycleRecorder(tmp_path)
    one, two = event(epoch=1), event(epoch=2)
    recorder.post(one)
    recorder.post(two)
    recorder.drain()
    assert recorder.ack(two["event_id"]) == {"acked": True, "already": False}
    assert recorder.ack(one["event_id"]) == {"acked": True, "already": True}
    assert LifecycleRecorder(tmp_path).ack(one["event_id"]) == {"acked": True, "already": True}


def test_a_kill_executor_that_cannot_start_releases_the_pending_count(tmp_path, monkeypatch):
    """#618 T5: the count is released, the service latches ``cancel_unavailable`` and
    ``flush`` answers ``unhealthy`` (never ``pending`` forever)."""
    service = AuthorityService(tmp_path)
    service.settle_wait = 0.05
    calls = []
    service.consume(_issue(service), _record(service), kind="yolo", on_cancel=calls.append)
    _fail_thread(monkeypatch, "authority-kill-cancel")
    service.kill_switch()
    assert service._kill_pending == 0
    assert service.unhealthy_reason == "cancel_unavailable"
    assert service.flush_result(timeout=10) == "unhealthy"
    assert calls == []

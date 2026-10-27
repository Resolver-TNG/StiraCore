"""ADR 0012 phases 3 / 4: the window's life — open (§6a), close (§6, §11), clocks (§7),
the marker, the latch judgement and unlatch (§11) — with one fault injected at a time.

The service runs on fake clocks, a fake audit chain (``Recorder``: the real row contract,
receipts, health, history) and a real marker file in ``tmp_path``. Each check of the
final CAS is also shown to matter: with exactly that check removed, the same injection
opens a window (``test_each_final_check_is_what_stops_the_window``).
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
from dataclasses import replace

import pytest

from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.yolo import requests as yolo_requests
from stiracore.yolo import state as sm
from stiracore.yolo.allowance import ClockReading
from stiracore.yolo.authority import Receipt
from stiracore.yolo.requests import SLEEP_SEC, WINDOW_SEC, YoloRequests, clock_verdict
from stiracore.yolo.window import MARKER_NAME, Latch, MarkerRead, WindowMarker, judge
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder


def _service(audit, clock, marker, **kw):
    return YoloRequests(
        allowance=kw.pop("allowance", "external"),
        incarnation=secrets.token_hex(16),
        record=audit,
        actor="local:tester",
        clocks_supported=True,
        clock=clock,
        ledger=kw.pop("ledger", audit),
        marker=marker,
        breaker=kw.pop("breaker", lambda: "normal"),
        **kw,
    )


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


@pytest.fixture
def marker(tmp_path):
    return WindowMarker(tmp_path)


@pytest.fixture
def yolo(audit, clock, marker):
    return _service(audit, clock, marker)


def _confirm(yolo, request_id):
    return yolo.decide(
        request_id,
        incarnation=yolo.incarnation,
        decision="confirm",
        principal="local:tester",
        channel="cli",
    )


def _code(fn, *args, **kwargs):
    with pytest.raises(sm.Refused) as error:
        fn(*args, **kwargs)
    return error.value.code


def _open(yolo, tier="internal"):
    request_id = yolo.press(tier)["id"]
    _confirm(yolo, request_id)
    assert yolo.machine.window.state == "active", yolo.machine
    return request_id


class Watch:
    """Every window state the service ever published (to show one never was active)."""

    def __init__(self, yolo):
        self.states = []
        original = yolo._publish

        def publish(machine):
            window = machine.window
            self.states.append(None if window is None else window.state)
            original(machine)

        yolo._publish = publish


def _unlatch(yolo, window_id):
    target = yolo.unlatch_target(window_id)
    return yolo.unlatch(window_id, expected=target, op_id=secrets.token_hex(16))


# --- the open sequence (§6a, §11 1.) ---------------------------------------------------


def test_the_open_sequence_writes_marker_before_started_and_reads_both_back(
    yolo, audit, marker, monkeypatch
):
    order = []
    original_write = marker.write
    monkeypatch.setattr(
        marker,
        "write",
        lambda state, *a, **k: (order.append(f"marker:{state}"), original_write(state, *a, **k))[1],
    )
    original_receipt = audit.yolo_receipt
    audit.yolo_receipt = lambda eid: (
        order.append("read:" + eid.rsplit(":", 1)[1]),
        original_receipt(eid),
    )[1]
    audit.before["audit.yolo.confirmed"] = lambda: order.append("row:confirmed")
    audit.before["audit.yolo.started"] = lambda: order.append("row:started")
    _open(yolo)
    assert order == [
        "row:confirmed",
        "read:decision",
        "marker:open",
        "row:started",
        "read:decision",
    ]
    started = audit.of("started")[0]
    assert started["cap"] == yolo.cap and started["external_cap"] == 0  # tier internal
    assert started["expires_at"] == started["t0"] + WINDOW_SEC
    assert marker.read().state == "open" and marker.read().window_id == started["window_id"]
    assert isinstance(yolo._win["started"], Receipt) and yolo._win["started"].valid()


def test_tier_internal_and_sub_cap_zero_do_not_block_the_issue(audit, clock, marker):
    yolo = _service(audit, clock, marker, external_cap=0)
    _open(yolo, "internal")
    assert audit.of("started")[0]["external_cap"] == 0


@pytest.mark.parametrize("breaker", [lambda: "warning", lambda: 1 / 0, None])
def test_a_window_confirmed_while_the_breaker_is_not_normal_opens_paused(
    audit, clock, marker, breaker
):
    yolo = _service(audit, clock, marker, breaker=breaker)
    request_id = yolo.press("internal")["id"]
    _confirm(yolo, request_id)
    assert yolo.machine.window.state == "paused"
    assert yolo.status()["window"]["pause_reason"] == "breaker_unknown"


# --- one fault at a time after the confirmation (phase 4) -------------------------------

POINTS = ("confirmed", "marker", "started", "readback", "final_cas")
EVENTS = {
    "stop": ("fence_stop", "issue_aborted_stop"),
    "new_session": ("fence_new", "issue_aborted_new_session"),
    "cancel": ("cancel", "issue_aborted_cancel"),
    "epoch": ("epoch", "issue_aborted_epoch"),
}


def _inject(yolo, audit, marker, monkeypatch, point, action):
    """Run ``action`` once, just before the I/O step ``point`` of the issuance."""
    fired = []

    def once():
        if not fired:
            fired.append(point)
            action()

    if point == "confirmed":
        audit.before["audit.yolo.confirmed"] = once
    elif point == "started":
        audit.before["audit.yolo.started"] = once
    elif point == "marker":
        original = marker.write
        monkeypatch.setattr(
            marker,
            "write",
            lambda state, *a, **k: (once() if state == "open" else None, original(state, *a, **k))[
                1
            ],
        )
    elif point == "readback":
        original_receipt = audit.yolo_receipt
        reads = []

        def read(event_id):
            reads.append(event_id)
            if len(reads) == 2:  # the started row's read-back
                once()
            return original_receipt(event_id)

        audit.yolo_receipt = read
    elif point == "final_cas":
        yolo._before_final_cas = once  # just before the final CAS takes the lock
    return fired


def _actions(yolo, kind):
    return {
        "fence_stop": lambda: yolo.fence("stop"),
        "fence_new": lambda: yolo.fence("new_session"),
        "cancel": lambda: yolo.cancel(),
        "epoch": yolo.advance_epoch,
    }[kind]


@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("event", sorted(EVENTS))
def test_a_stop_new_cancel_or_epoch_move_at_every_io_point_opens_nothing(
    yolo, audit, marker, monkeypatch, point, event
):
    kind, end_reason = EVENTS[event]
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    fired = _inject(yolo, audit, marker, monkeypatch, point, _actions(yolo, kind))
    _confirm(yolo, request_id)
    assert fired == [point]
    assert "active" not in watch.states and "paused" not in watch.states
    assert yolo.machine.window is None  # ended and closed again
    assert yolo.machine.request.state == "fenced"
    assert not yolo.machine.latched  # a normal end
    assert [row["end_reason"] for row in audit.of("ended")] == [end_reason]
    assert audit.of("ended")[0]["end_class"] == "normal"
    assert marker.read().state == "closed"
    assert "request_fenced" in audit.types()
    # Restart: nothing is latched; a new press is accepted.
    restarted = _service(audit, yolo._clock, marker)
    assert not restarted.machine.latched
    assert restarted.press("internal")["state"] == "pending"


FAILURES = {
    # name: (how to inject, marker state afterwards, latch subject after a restart)
    # ``None``: the explicit exception of ADR 0012 §11 — a failure before anything of
    # the start is durable (no marker replaced, no started row) latches this process
    # only; a restart re-checks the writer and starts unlatched (nothing was ever open).
    "confirmed_write": ("fail:audit.yolo.confirmed", "missing", None),
    "marker_write": ("marker", "missing", None),
    "started_write": ("fail:audit.yolo.started", "open", "orphan_marker"),
    "confirmed_receipt": ("tamper:audit.yolo.confirmed", "open", "window"),
    "started_receipt": ("tamper:audit.yolo.started", "open", "window"),
    "writer_unhealthy": ("unhealthy", "open", "window"),
    "clock_anomaly": ("clock", "open", "window"),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_a_failure_at_any_step_never_opens_and_latches_until_unlatch(
    yolo, audit, clock, marker, monkeypatch, name
):
    how, marker_after, subject = FAILURES[name]
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    window_id = yolo.machine.request.window_id
    if how.startswith("fail:"):
        audit.fail.add(how[5:])
    elif how.startswith("tamper:"):
        audit.tamper.add(how[7:])
    elif how == "unhealthy":
        audit.healthy = False
    elif how == "marker":
        monkeypatch.setattr(marker, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    elif how == "clock":
        yolo._before_final_cas = lambda: setattr(clock, "wall", clock.wall - 10)
    _confirm(yolo, request_id)
    monkeypatch.undo()
    audit.fail.clear()
    audit.tamper.clear()
    audit.healthy = True
    assert "active" not in watch.states and "paused" not in watch.states
    assert yolo.machine.latched
    assert _code(yolo.press, "internal") == "yolo_latched"
    assert marker.read().status == ("missing" if marker_after == "missing" else "ok")
    if marker_after == "open":
        assert marker.read().state == "open"
    ended = audit.of("ended")
    if "started" in audit.types():
        # The ended row is written, abnormal — and it does not unlatch (§6 end classes).
        assert [row["end_class"] for row in ended] == ["abnormal"]
    # Across a restart: latched again from the storage, with the right subject.
    restarted = _service(audit, clock, marker)
    if subject is None:
        # Specified (ADR 0012 §11, "before the start is durable"): not latched after a
        # restart — no window was ever usable and nothing of the start was persisted.
        assert not restarted.machine.latched
    else:
        latches = restarted.status()["latches"]
        assert [(item["subject"], item["window_id"]) for item in latches] == [(subject, window_id)]
        assert _code(restarted.press, "internal") == "yolo_latched"
        result = _unlatch(restarted, window_id)
        assert result["latched"] is False
        assert "unlatched" in audit.types() and "unlatch_applied" in audit.types()
        assert marker.read().state == "closed_by_unlatch"
        assert restarted.press("internal")["state"] == "pending"
    # In the same process the latch also closes only by an unlatch.
    _unlatch(yolo, window_id)
    assert not yolo.machine.latched


REMOVED = {
    # check removed -> the injection that it alone stops
    "fence": ("stop", "final_cas"),
    "receipts": ("tamper", "audit.yolo.started"),
    "writer_health": ("unhealthy", None),
    "clock": ("clock", None),
}


@pytest.mark.parametrize("check", sorted(REMOVED))
def test_each_final_check_is_what_stops_the_window(yolo, audit, clock, marker, monkeypatch, check):
    """With exactly one check of the final CAS taken out, the same injection opens a
    window — so the tests above fail when that check is removed."""
    if check == "fence":
        real = sm.issue_commit

        def blind(machine, request_id, window_id, **kw):
            request = machine.request
            if request.state == "fenced":
                request = sm.Request(
                    request.request_id,
                    request.tier,
                    request.window_id,
                    "issuing",
                    fence_seen=machine.fence,
                    epoch_seen=machine.epoch,
                )
                machine = replace(machine, request=request)
            return real(machine, request_id, window_id, **kw)

        monkeypatch.setattr(yolo_requests.sm, "issue_commit", blind)
        request_id = yolo.press("internal")["id"]
        _inject(yolo, audit, marker, monkeypatch, "final_cas", lambda: yolo.fence("stop"))
    else:
        request_id = yolo.press("internal")["id"]
        if check == "receipts":
            audit.tamper.add("audit.yolo.started")
            monkeypatch.setattr(
                YoloRequests,
                "_receipt",
                lambda self, eid, *a: Receipt(eid, 1, "a" * 64),
            )
        elif check == "writer_health":
            audit.healthy = False
            monkeypatch.setattr(YoloRequests, "_healthy", lambda self: True)
        elif check == "clock":
            yolo._before_final_cas = lambda: setattr(clock, "wall", clock.wall - 10)
            monkeypatch.setattr(yolo_requests, "clock_verdict", lambda *a: None)
    _confirm(yolo, request_id)
    assert yolo.machine.window is not None and yolo.machine.window.state == "active"


# --- races ----------------------------------------------------------------------------


def test_cancel_before_confirm_wins_and_the_confirmation_is_refused(yolo, audit):
    request_id = yolo.press("internal")["id"]
    yolo.cancel(request_id)
    with pytest.raises(Exception) as error:
        _confirm(yolo, request_id)
    assert getattr(error.value, "code", None) == "host_operation_decided"
    assert "confirmed" not in audit.types() and yolo.machine.window is None


def test_two_presses_at_once_make_one_request(yolo, audit):
    audit.hold = threading.Event()
    results = []

    def press():
        try:
            results.append(yolo.press("internal")["state"])
        except sm.Refused as error:
            results.append(error.code)

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    threading.Event().wait(0.1)
    audit.hold.set()
    for thread in threads:
        thread.join(5)
    assert sorted(results) == ["pending", "yolo_request_pending"]
    assert audit.types().count("requested") == 1


def test_a_press_then_a_restart_before_the_confirmation_leaves_nothing(yolo, audit, clock, marker):
    request_id = yolo.press("internal")["id"]
    restarted = _service(audit, clock, marker)
    assert restarted.status()["request"] is None and not restarted.machine.latched
    with pytest.raises(Exception) as error:
        _confirm(restarted, request_id)
    assert getattr(error.value, "code", None) == "host_operation_unknown"


def test_a_confirmation_after_the_request_expired_is_refused(yolo, audit, clock):
    request_id = yolo.press("internal")["id"]
    clock.tick(121)
    with pytest.raises(Exception) as error:
        _confirm(yolo, request_id)
    assert getattr(error.value, "code", None) == "host_operation_expired"
    assert "started" not in audit.types()


# --- closing: stop, /new, the server's stop, restart (§6, §11 2.) ----------------------


@pytest.mark.parametrize(
    ("reason", "end_reason"), [("stop", "revoked_stop"), ("new_session", "revoked_new_session")]
)
def test_stop_and_new_end_an_open_window_normally(yolo, audit, marker, reason, end_reason):
    _open(yolo)
    yolo.fence(reason)
    ended = audit.of("ended")[0]
    assert ended["end_reason"] == end_reason and ended["end_class"] == "normal"
    assert ended["actor_kind"] == "human" and ended["generation_from"] == 1
    assert ended["generation_to"] == 2
    assert yolo.machine.window is None and not yolo.machine.latched
    assert marker.read().state == "closed"
    status = yolo.status()
    assert status["window"] is None and status["last_end"]["reason"] == end_reason
    assert yolo.press("internal")["state"] == "pending"  # press again


def test_stop_with_nothing_open_writes_nothing_and_is_never_refused(yolo, audit):
    yolo.fence("stop")
    yolo.fence("new_session")
    assert audit.types() == []


def test_stop_also_ends_a_paused_window(audit, clock, marker):
    yolo = _service(audit, clock, marker, breaker=lambda: "critical")
    _open_paused = yolo.press("internal")["id"]
    _confirm(yolo, _open_paused)
    assert yolo.machine.window.state == "paused"
    yolo.fence("stop")
    assert audit.of("ended")[0]["end_reason"] == "revoked_stop"


def test_the_servers_stop_ends_it_normally_and_a_restart_is_not_latched(yolo, audit, clock, marker):
    _open(yolo)
    yolo.fence("stop", human=False)
    assert audit.of("ended")[0]["actor_kind"] == "system"
    restarted = _service(audit, clock, marker)
    assert restarted.machine.window is None and not restarted.machine.latched


def test_a_crash_leaves_the_marker_open_and_the_restart_latched(yolo, audit, clock, marker):
    _open(yolo)
    window_id = yolo.machine.window.window_id
    # No stop: the process is gone. The window never comes back.
    restarted = _service(audit, clock, marker)
    assert restarted.machine.window is None
    assert restarted.status()["latches"] == [
        {"subject": "window", "window_id": window_id, "reason": "no_end_row"}
    ]
    _unlatch(restarted, window_id)
    assert restarted.press("internal")["state"] == "pending"


def test_a_failed_ended_row_latches_although_the_end_is_normal(yolo, audit, marker):
    _open(yolo)
    audit.fail.add("audit.yolo.ended")
    yolo.fence("stop")
    assert yolo.machine.window is not None and yolo.machine.window.state == "ended"
    assert yolo.machine.latched and marker.read().state == "open"


def test_a_crash_between_ended_and_the_marker_is_repaired_at_the_next_start(
    yolo, audit, clock, marker, monkeypatch
):
    _open(yolo)
    original = marker.write
    monkeypatch.setattr(
        marker,
        "write",
        lambda state, *a, **k: (
            (_ for _ in ()).throw(OSError("crash"))
            if state == "closed"
            else original(state, *a, **k)
        ),
    )
    yolo.fence("stop")
    assert yolo.machine.latched and marker.read().state == "open"  # in this process
    monkeypatch.undo()
    restarted = _service(audit, clock, marker)
    # The chain shows a normal end: the only automatic repair (§11).
    assert not restarted.machine.latched and marker.read().state == "closed"


# --- clocks (§7) ----------------------------------------------------------------------


def test_six_hours_from_the_press_ends_it(yolo, audit, clock):
    yolo.press("internal")
    clock.tick(60)  # the wait for the confirmation counts
    _confirm(yolo, yolo.machine.request.request_id)
    clock.tick(WINDOW_SEC - 61)
    yolo.tick()
    assert yolo.machine.window.state == "active"
    assert yolo.status()["window"]["remaining_sec"] == 1
    clock.tick(1)
    yolo.tick()
    assert audit.of("ended")[0]["end_reason"] == "expired_time"
    assert not yolo.machine.latched and yolo.machine.window is None


def test_a_wall_clock_jump_forward_ends_it_early(yolo, audit, clock):
    _open(yolo)
    clock.wall += WINDOW_SEC
    yolo.tick()
    assert audit.of("ended")[0]["end_reason"] == "expired_time"


def test_a_sleep_longer_than_five_minutes_ends_it(yolo, audit, clock):
    _open(yolo)
    clock.wall += SLEEP_SEC - 60
    clock.boot += SLEEP_SEC - 60  # a short sleep: the boot clock ran, mono did not
    yolo.tick()
    assert yolo.machine.window.state == "active"
    clock.wall += SLEEP_SEC + 1
    clock.boot += SLEEP_SEC + 1
    yolo.tick()
    assert audit.of("ended")[0]["end_reason"] == "expired_sleep"
    assert not yolo.machine.latched


def test_a_clock_anomaly_revokes_and_stays_latched_although_its_row_was_written(
    yolo, audit, clock, marker
):
    _open(yolo)
    window_id = yolo.machine.window.window_id
    clock.wall -= 3  # wall clock set back beyond the slack
    yolo.status()  # every look checks
    ended = audit.of("ended")[0]
    assert ended["end_reason"] == "revoked_clock_anomaly" and ended["end_class"] == "abnormal"
    assert yolo.machine.latched and marker.read().state == "open"
    restarted = _service(audit, clock, marker)
    assert restarted.machine.latched  # an abnormal ended row never closes it
    _unlatch(restarted, window_id)
    assert restarted.press("internal")["state"] == "pending"


def test_seven_hours_of_sleep_with_the_wall_set_back_six_hours_is_an_anomaly():
    t0 = Clocks()()
    last = t0
    now = ClockReading(t0.wall + 3600, t0.boot + 7 * 3600, t0.mono + 10)
    assert clock_verdict(t0, last, now) == "revoked_clock_anomaly"


def test_the_watchdog_runs_without_a_look(audit, clock, marker, monkeypatch):
    from stiracore.yolo import runtime

    monkeypatch.setattr(runtime, "WATCHDOG_SEC", 0.01)
    yolo = _service(audit, clock, marker)
    _open(yolo)
    runtime._start_watchdog(yolo)
    try:
        clock.tick(WINDOW_SEC)
        for _ in range(300):
            if audit.of("ended"):
                break
            threading.Event().wait(0.01)
    finally:
        runtime._WATCHDOG.set()
    assert audit.of("ended")[0]["end_reason"] == "expired_time"


# --- the marker and the judgement (§11) ------------------------------------------------


def test_the_marker_write_is_durable_in_order(tmp_path, monkeypatch):
    from stiracore.yolo import window

    calls = []
    me = threading.get_ident()
    real_flush, real_replace = window._flush, os.replace
    # Both patches are process-wide (``window.os`` is the ``os`` module): count only the calls
    # of this thread. One run on a CI macOS runner (public #662) listed an extra "flush"; the
    # likely cause is a thread left over from an earlier test writing a marker meanwhile
    # (a hypothesis, not reproduced locally), and this makes the order check independent of it.
    monkeypatch.setattr(
        window,
        "_flush",
        lambda fd: (calls.append("flush") if threading.get_ident() == me else None, real_flush(fd))[
            1
        ],
    )
    monkeypatch.setattr(
        window.os,
        "replace",
        lambda a, b: (
            calls.append("rename") if threading.get_ident() == me else None,
            real_replace(a, b),
        )[1],
    )
    WindowMarker(tmp_path).write("open", "a" * 32, "b" * 64)
    assert calls == ["flush", "rename", "flush"]  # file, rename, directory
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_NAME]


@pytest.mark.parametrize("step", ["flush", "rename"])
def test_a_crash_inside_the_marker_write_keeps_the_old_marker(tmp_path, monkeypatch, step):
    from stiracore.yolo import window

    marker = WindowMarker(tmp_path)
    marker.write("closed", "a" * 32, "b" * 64)
    target = "_flush" if step == "flush" else "replace"
    owner = window if step == "flush" else window.os
    monkeypatch.setattr(owner, target, lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        marker.write("open", "c" * 32, "b" * 64)
    monkeypatch.undo()
    assert marker.read().state == "closed" and marker.read().window_id == "a" * 32
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_NAME]  # no temporary file left


def _bad_marker(tmp_path, kind):
    path = tmp_path / MARKER_NAME
    if kind == "symlink":
        other = tmp_path / "other.json"
        WindowMarker(tmp_path).write("closed", "a" * 32, "b" * 64)
        path.rename(other)
        path.symlink_to(other)
    elif kind == "malformed":
        path.write_text('{"v": 1, "state": "closed"}')
    elif kind == "not_json":
        path.write_text("{")
    elif kind == "hardlink":
        WindowMarker(tmp_path).write("closed", "a" * 32, "b" * 64)
        os.link(path, tmp_path / "copy.json")
    elif kind == "bool_version":
        wid, ref = "a" * 32, "b" * 64
        path.write_text(
            f'{{"v": true, "window_id": "{wid}", "incarnation_ref": "{ref}", "state": "closed"}}'
        )


@pytest.mark.parametrize("kind", ["symlink", "malformed", "not_json", "hardlink", "bool_version"])
def test_an_unreadable_marker_latches_and_is_not_unlatchable_by_id(tmp_path, audit, clock, kind):
    _bad_marker(tmp_path, kind)
    yolo = _service(audit, clock, WindowMarker(tmp_path))
    assert yolo.machine.latched
    assert [item["subject"] for item in yolo.status()["latches"]] == ["unreadable"]
    assert _code(yolo.press, "internal") == "yolo_latched"


def test_an_unverifiable_chain_latches(audit, clock, marker):
    audit.unreadable = True
    yolo = _service(audit, clock, marker)
    assert yolo.status()["latches"][0]["reason"] == "chain_unverified"


def test_judgement_rules():
    wid, other = "a" * 32, "c" * 32
    open_marker = MarkerRead(
        "ok", {"v": 1, "window_id": wid, "incarnation_ref": "b" * 64, "state": "open"}
    )
    started = ("audit.yolo.started", {"window_id": wid})
    normal = (
        "audit.yolo.ended",
        {"window_id": wid, "end_class": "normal", "end_reason": "revoked_stop"},
    )
    lying = ("audit.yolo.ended", {"window_id": wid, "end_class": "normal", "end_reason": "made_up"})
    abnormal = (
        "audit.yolo.ended",
        {"window_id": wid, "end_class": "abnormal", "end_reason": "revoked_clock_anomaly"},
    )
    unlatched = ("audit.yolo.unlatched", {"window_id": wid, "latch_subject": "window"})
    missing = MarkerRead("missing")
    assert judge([], missing).latches == ()
    assert judge([started, normal], missing).latches == ()
    assert judge([started, normal], open_marker).repair == "closed"
    assert [x.reason for x in judge([started, abnormal], missing).latches] == [
        "revoked_clock_anomaly"
    ]
    assert [x.reason for x in judge([started, lying], missing).latches] == ["made_up"]
    assert judge([started, abnormal, unlatched], open_marker).repair == "closed_by_unlatch"
    assert judge([started, abnormal, unlatched], open_marker).latches == ()
    # an orphan marker (no started row), and the same id's unlatch of another window
    assert [x.subject for x in judge([], open_marker).latches] == ["orphan_marker"]
    other_started = ("audit.yolo.started", {"window_id": other})
    assert [x.window_id for x in judge([other_started], missing).latches] == [other]


def test_unlatch_is_refused_when_its_target_changed_or_its_row_fails(yolo, audit, clock, marker):
    _open(yolo)
    window_id = yolo.machine.window.window_id
    restarted = _service(audit, clock, marker)
    target = restarted.unlatch_target(window_id)
    audit.rows.append(audit.rows[-1])  # the chain head moved since the target was read
    assert _code(restarted.unlatch, window_id, expected=target, op_id="f" * 32) == (
        "stale_operation"
    )
    audit.fail.add("audit.yolo.unlatched")
    target = restarted.unlatch_target(window_id)
    assert _code(restarted.unlatch, window_id, expected=target, op_id="f" * 32) == (
        "yolo_audit_failed"
    )
    assert restarted.machine.latched and marker.read().state == "open"
    assert _code(restarted.unlatch_target, "0" * 32) == "yolo_not_latched"
    assert _code(restarted.unlatch_target, 1) == "yolo_not_latched"


def test_unlatch_waits_for_an_open_window_and_never_opens_one(yolo, audit, clock, marker):
    other = _service(audit, clock, marker)  # a stale window from "before"
    _open(other)
    stale = other.machine.window.window_id
    yolo = _service(audit, clock, marker)
    assert yolo.machine.latched
    unlatched = _unlatch(yolo, stale)
    assert unlatched["latched"] is False and yolo.machine.window is None
    _open(yolo)
    # A latch from storage that appears while a window is open cannot be unlatched now.
    yolo._latches["e" * 32] = Latch("window", "e" * 32, "no_end_row")
    assert _code(yolo.unlatch_target, "e" * 32) == "yolo_busy"


# --- the real chain: receipts and judgement on SQLiteEventStore ------------------------


def test_on_the_real_event_store(tmp_path, clock):
    store = SQLiteEventStore(tmp_path / "events.db")
    chain = AuditChain(store)
    marker = WindowMarker(tmp_path)
    yolo = _service(chain.record_yolo, clock, marker, ledger=chain)
    _open(yolo)
    window_id = yolo.machine.window.window_id
    receipt = chain.yolo_receipt(yolo._win["started"].event_id)
    assert receipt["event_type"] == "audit.yolo.started" and receipt["seq"] >= 1
    # A crash: a new process on the same files is latched; unlatch; press again.
    store.close()
    store = SQLiteEventStore(tmp_path / "events.db")
    chain = AuditChain(store)
    restarted = _service(chain.record_yolo, clock, marker, ledger=chain)
    assert [x["window_id"] for x in restarted.status()["latches"]] == [window_id]
    _unlatch(restarted, window_id)
    _open(restarted)
    restarted.fence("stop")
    store.close()
    store = SQLiteEventStore(tmp_path / "events.db")
    chain = AuditChain(store)
    again = _service(chain.record_yolo, clock, marker, ledger=chain)
    assert not again.machine.latched
    store.close()


# --- review round 1: inputs of the final CAS read under the lock -------------------------


def test_a_writer_failure_after_the_last_write_but_before_the_final_cas_opens_nothing(yolo, audit):
    request_id = yolo.press("internal")["id"]
    yolo._before_final_cas = lambda: setattr(audit, "healthy", False)
    _confirm(yolo, request_id)
    assert yolo.machine.window is None or yolo.machine.window.state == "ended"
    assert yolo.machine.latched


def test_a_lock_wait_after_the_writes_is_judged_with_the_clock_after_the_wait(yolo, audit, clock):
    """The final CAS reads the clocks after it got the lock: six hours passing (and the
    writer failing) while it waits never publish a window on the earlier facts."""
    request_id = yolo.press("internal")["id"]
    holding, release = threading.Event(), threading.Event()

    def hold_the_lock():
        with yolo._lock:
            holding.set()
            release.wait(5)

    def before():
        threading.Thread(target=hold_the_lock, daemon=True).start()
        holding.wait(5)
        clock.tick(WINDOW_SEC + 1)  # time passes while the CAS waits for the lock

        def later():
            threading.Event().wait(0.05)
            release.set()

        threading.Thread(target=later, daemon=True).start()

    yolo._before_final_cas = before
    _confirm(yolo, request_id)
    assert yolo.machine.window is None or yolo.machine.window.state == "ended"
    assert "active" not in [row.get("state") for row in [yolo.status()["window"] or {}]]


def test_a_writer_failing_while_a_window_is_open_ends_it_at_the_next_look(yolo, audit, marker):
    _open(yolo)
    audit.healthy = False
    yolo.tick()
    ended = audit.of("ended")
    assert yolo.machine.window.state == "ended" and yolo.machine.latched
    # The ended row may not be writable either; either way the marker stays open.
    assert not ended or ended[0]["end_reason"] == "revoked_audit_failed"
    assert marker.read().state == "open"


# --- review round 1: stop never depends on the clocks -----------------------------------


def _broken_clock():
    raise OSError("clock_gettime failed")


@pytest.mark.parametrize("reason", ["stop", "new_session"])
def test_stop_and_new_end_the_window_although_the_clocks_cannot_be_read(yolo, audit, reason):
    _open(yolo)
    yolo._clock = _broken_clock
    yolo.fence(reason)  # does not raise
    window = yolo.machine.window
    assert window is None or not window.open
    assert audit.of("ended")[0]["end_reason"] == f"revoked_{reason}"


def test_a_clock_that_cannot_be_read_ends_the_window_abnormally(yolo, audit, marker):
    _open(yolo)
    yolo._clock = _broken_clock
    status = yolo.status()  # every look; does not raise
    assert audit.of("ended")[0]["end_reason"] == "revoked_clock_anomaly"
    assert status["latched"] or yolo.machine.latched
    assert marker.read().state == "open"
    assert _code(yolo.press, "internal") in ("yolo_clock_failed", "yolo_latched")


def test_a_clock_failure_also_ends_a_pending_request_and_cancel_still_works(yolo, audit):
    request_id = yolo.press("internal")["id"]
    yolo._clock = _broken_clock
    with pytest.raises(Exception) as error:
        _confirm(yolo, request_id)
    assert getattr(error.value, "code", None) == "host_channel_unavailable"
    assert yolo.machine.request.state == "expired" and "started" not in audit.types()


def test_the_watchdog_does_not_swallow_its_own_failure(audit, clock, marker, monkeypatch):
    from stiracore.yolo import runtime

    monkeypatch.setattr(runtime, "WATCHDOG_SEC", 0.01)
    yolo = _service(audit, clock, marker)
    _open(yolo)
    monkeypatch.setattr(yolo, "tick", lambda: 1 / 0)
    runtime._start_watchdog(yolo)
    try:
        for _ in range(300):
            if audit.of("ended"):
                break
            threading.Event().wait(0.01)
    finally:
        runtime._WATCHDOG.set()
    assert audit.of("ended")[0]["end_reason"] == "revoked_clock_anomaly"
    assert yolo.machine.latched


def test_stop_reaches_the_window_even_if_the_operation_store_fails(yolo, audit, clock):
    from stiracore.yolo.host_channel import HostDesk
    from stiracore.yolo.host_ops import HostOperations

    operations = HostOperations(incarnation=yolo.incarnation, clock=_broken_clock)
    desk = HostDesk(operations, yolo)
    _open(yolo)
    desk.revoke("stop")
    assert audit.of("ended")[0]["end_reason"] == "revoked_stop"


# --- review round 1: F_FULLFSYNC errors ------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS only")
def test_a_real_full_fsync_error_fails_the_write_even_if_fsync_would_succeed(
    yolo, audit, monkeypatch
):
    import errno
    import fcntl

    def fullfsync(fd, cmd, *args):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(fcntl, "fcntl", fullfsync)
    monkeypatch.setattr(os, "fsync", lambda fd: None)  # the fallback would "succeed"
    request_id = yolo.press("internal")["id"]
    _confirm(yolo, request_id)
    assert yolo.machine.window is None and yolo.machine.latched
    assert "started" not in audit.types()


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS only")
@pytest.mark.parametrize("name", ["ENOTSUP", "EINVAL", "ENOTTY"])
def test_full_fsync_not_supported_falls_back_to_fsync(tmp_path, monkeypatch, name):
    import errno
    import fcntl

    synced = []
    monkeypatch.setattr(
        fcntl, "fcntl", lambda *a: (_ for _ in ()).throw(OSError(getattr(errno, name), name))
    )
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    WindowMarker(tmp_path).write("open", "a" * 32, "b" * 64)
    assert len(synced) == 2  # the file and the directory
    assert WindowMarker(tmp_path).read().state == "open"


# --- review round 1: cheap refusals first; unlatch bound to the YOLO state only ---------


def test_a_refused_press_does_not_verify_the_chain(yolo, audit, clock):
    reads = []
    original = audit.yolo_history
    audit.yolo_history = lambda: (reads.append(1), original())[1]
    _open(yolo)
    reads.clear()
    assert _code(yolo.press, "internal") == "yolo_active"
    yolo.fence("stop")
    for _ in range(2):  # the hourly limit: 3 presses (one used by _open)
        yolo.press("internal")
        yolo.cancel()
    reads.clear()
    assert _code(yolo.press, "internal") == "yolo_rate_limited"
    assert reads == []


def test_an_unrelated_event_during_the_confirmation_does_not_make_unlatch_stale(
    yolo, audit, clock, marker
):
    _open(yolo)
    window_id = yolo.machine.window.window_id
    restarted = _service(audit, clock, marker)
    target = restarted.unlatch_target(window_id)
    audit.rows.append({"event_type": "audit.tool.started", "event_id": "x", "attributes": {}})
    result = restarted.unlatch(window_id, expected=target, op_id="f" * 32)
    assert result["latched"] is False
    # The head seen when the row was written (requested, confirmed, started, unrelated).
    assert audit.of("unlatched")[0]["chain_seq_seen"] == 4

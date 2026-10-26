"""ADR 0012 §1a / §6b / §11: the YOLO request and its host confirmation.

Every row goes through the real ``audit.yolo.*`` contract (``audit_schema.build``); the
clocks are fake. Phase 3: a confirmation opens the window (marker + ``started``); the
window's lifecycle is pinned by ``tests/test_yolo_window.py``.
"""

from __future__ import annotations

import hashlib
import secrets
import threading

import pytest

from stiracore.yolo import state as sm
from stiracore.yolo.audit_schema import build
from stiracore.yolo.host_ops import HostOpRefused
from stiracore.yolo.requests import COOLDOWN_SEC, EXPIRY_SEC, HOURLY_MAX, YoloRequests
from stiracore.yolo.window import WindowMarker
from tests.test_yolo_host_ops import Clocks, Liar, SneakyStr


class Recorder:
    """The audit chain as the YOLO service sees it: ``record`` (the write) and the read
    side (``yolo_receipt`` / ``yolo_healthy`` / ``yolo_history`` / ``yolo_head_seq``).

    Faults: ``fail`` (event types whose write raises), ``hold`` (an Event every write
    waits for), ``before`` (``event_type -> callable`` run just before that row is
    written), ``tamper`` (event types whose read-back differs), ``healthy``,
    ``unreadable`` (the history raises)."""

    _HISTORY = ("audit.yolo.started", "audit.yolo.ended", "audit.yolo.unlatched")

    def __init__(self):
        self.rows = []
        self.fail = set()
        self.hold = None  # an Event a write waits for (a slow audit write)
        self.before = {}
        self.tamper = set()
        self.healthy = True
        self.unreadable = False
        self._by_id = {}

    def __call__(self, event_type, **fields):
        if self.hold is not None:
            self.hold.wait(5)
        hook = self.before.get(event_type)
        if hook is not None:
            hook()
        if event_type in self.fail:
            raise OSError("disk full")
        row = build(event_type, **fields)  # the contract: raises on any bad field
        self.rows.append(row)
        digest = hashlib.sha256(repr(row).encode()).hexdigest()
        self._by_id[row["event_id"]] = {
            "seq": len(self.rows),
            "hash": digest,
            "event_type": event_type,
            "attributes": dict(row["attributes"]),
        }
        return row["event_id"]

    def types(self):
        return [row["event_type"].removeprefix("audit.yolo.") for row in self.rows]

    def of(self, kind):
        return [row["attributes"] for row in self.rows if row["event_type"] == "audit.yolo." + kind]

    # -- the read side --

    def yolo_receipt(self, event_id):
        found = self._by_id.get(event_id)
        if found is None:
            return None
        found = {**found, "attributes": dict(found["attributes"])}
        if found["event_type"] in self.tamper:
            found["attributes"]["window_id"] = "0" * 32
        return found

    def yolo_healthy(self):
        return self.healthy

    def yolo_history(self):
        if self.unreadable:
            raise RuntimeError("chain did not verify")
        return [
            (row["event_type"], dict(row["attributes"]))
            for row in self.rows
            if row["event_type"] in self._HISTORY
        ]

    def yolo_head_seq(self):
        return len(self.rows)


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


def _service(audit, clock, allowance="external", **kw):
    return YoloRequests(
        allowance=allowance,
        incarnation=secrets.token_hex(16),
        record=audit,
        actor="local:tester",
        clocks_supported=kw.pop("clocks_supported", True),
        clock=clock,
        ledger=kw.pop("ledger", audit),
        breaker=kw.pop("breaker", lambda: "normal"),
        **kw,
    )


@pytest.fixture
def marker(tmp_path):
    return WindowMarker(tmp_path)


@pytest.fixture
def yolo(audit, clock, marker):
    return _service(audit, clock, marker=marker)


def _decide(yolo, request_id, decision="confirm", incarnation=None):
    return yolo.decide(
        request_id,
        incarnation=yolo.incarnation if incarnation is None else incarnation,
        decision=decision,
        principal="local:tester",
        channel="cli",
    )


def _refused(fn, *args, **kwargs):
    with pytest.raises((sm.Refused, HostOpRefused)) as error:
        fn(*args, **kwargs)
    return error.value.code


def test_press_writes_requested_and_waits_for_the_host(yolo, audit):
    view = yolo.press("internal")
    assert view["state"] == "pending" and view["tier"] == "internal"
    assert view["expires_in"] == int(EXPIRY_SEC) and view["word"] == "YOLO"
    assert audit.types() == ["requested"]
    row = audit.rows[0]["attributes"]
    assert row["request_id"] == view["id"] and row["actor"] == "local:tester"
    assert row["generation_from"] == row["generation_to"] == 0
    assert yolo.status()["window"] is None


def test_a_confirmation_opens_the_window(yolo, audit, marker):
    request_id = yolo.press("external")["id"]
    view = _decide(yolo, request_id)
    assert audit.types() == ["requested", "confirmed", "started"]
    started = audit.of("started")[0]
    assert started["generation_from"] == 0 and started["generation_to"] == 1
    assert started["confirmed_ref"] == audit.rows[1]["event_id"]
    assert view["state"] == "opened"
    assert yolo.machine.window.state == "active" and not yolo.machine.latched
    assert marker.read().state == "open"
    window = yolo.status()["window"]
    assert window["state"] == "active" and window["tier"] == "external"
    assert window["remaining_sec"] == 6 * 3600
    # One window at a time: no extension.
    assert _refused(yolo.press, "internal") == "yolo_active"


def test_a_confirmation_is_single_use_and_bound_to_the_incarnation(yolo, clock, audit):
    request_id = yolo.press("internal")["id"]
    assert _refused(_decide, yolo, request_id, incarnation=secrets.token_hex(16)) == (
        "host_operation_incarnation"
    )
    assert _refused(_decide, yolo, request_id, incarnation=SneakyStr(yolo.incarnation)) == (
        "host_operation_incarnation"
    )
    _decide(yolo, request_id)
    assert _refused(_decide, yolo, request_id) == "host_operation_decided"  # the replay
    assert _refused(_decide, yolo, request_id, "deny") == "host_operation_decided"
    assert audit.types().count("confirmed") == 1
    # A restart: the request is gone; its id belongs to the old incarnation.
    restarted = _service(audit, clock)
    assert _refused(_decide, restarted, request_id, incarnation=restarted.incarnation) == (
        "host_operation_unknown"
    )
    assert restarted.status()["request"] is None


def test_the_request_expires_after_two_minutes_and_a_late_confirmation_is_refused(
    yolo, clock, audit
):
    request_id = yolo.press("internal")["id"]
    clock.tick(EXPIRY_SEC)
    assert _refused(_decide, yolo, request_id) == "host_operation_expired"
    assert audit.types() == ["requested", "request_expired"]
    assert yolo.status()["request"]["state"] == "expired"


def test_a_sleep_counts_towards_the_expiry(yolo, clock):
    request_id = yolo.press("internal")["id"]
    clock.boot += EXPIRY_SEC  # asleep: only the sleep-including clock moved
    assert _refused(_decide, yolo, request_id) == "host_operation_expired"


def test_deny_records_and_starts_the_cooldown(yolo, clock, audit):
    request_id = yolo.press("internal")["id"]
    assert _decide(yolo, request_id, "deny")["state"] == "denied"
    assert audit.types() == ["requested", "denied"]
    assert _refused(yolo.press, "internal") == "yolo_rate_limited"
    clock.tick(COOLDOWN_SEC - 1)
    assert _refused(yolo.press, "internal") == "yolo_rate_limited"
    clock.tick(1)
    assert yolo.press("internal")["state"] == "pending"


def test_an_expiry_starts_the_cooldown_too(yolo, clock):
    yolo.press("internal")
    clock.tick(EXPIRY_SEC)
    assert _refused(yolo.press, "internal") == "yolo_rate_limited"
    clock.tick(COOLDOWN_SEC)
    yolo.press("internal")


def test_one_pending_request_and_at_most_three_per_hour(yolo, clock):
    first = yolo.press("internal")["id"]
    assert _refused(yolo.press, "internal") == "yolo_request_pending"
    yolo.cancel(first)
    for _ in range(HOURLY_MAX - 1):
        yolo.cancel(yolo.press("internal")["id"])
    assert _refused(yolo.press, "internal") == "yolo_rate_limited"
    clock.tick(3600)
    yolo.press("internal")


def test_tier_allowance_and_clock_support(audit, clock):
    internal = _service(audit, clock, allowance="internal")
    assert _refused(internal.press, "external") == "yolo_tier_not_allowed"
    assert _refused(internal.press, "all") == "yolo_tier_not_allowed"
    assert _refused(internal.press, SneakyStr("internal")) == "yolo_tier_not_allowed"
    assert _refused(internal.press, Liar()) == "yolo_tier_not_allowed"
    unsupported = _service(audit, clock, clocks_supported=False)
    assert _refused(unsupported.press, "internal") == "clock_not_measured"
    assert audit.rows == []


def test_cancel_is_a_narrowing_and_recorded(yolo, audit):
    request_id = yolo.press("internal")["id"]
    assert yolo.cancel(request_id)["state"] == "cancelled"
    assert audit.types() == ["requested", "request_cancelled"]
    assert _refused(_decide, yolo, request_id) == "host_operation_decided"
    assert _refused(yolo.cancel, request_id) == "yolo_request_stale"


def test_a_press_whose_row_fails_does_not_exist(yolo, audit):
    audit.fail.add("audit.yolo.requested")
    assert _refused(yolo.press, "internal") == "yolo_audit_failed"
    assert yolo.status()["request"] is None
    audit.fail.clear()
    yolo.press("internal")


def test_a_confirmation_whose_row_fails_aborts_and_latches(yolo, audit):
    request_id = yolo.press("internal")["id"]
    audit.fail.add("audit.yolo.confirmed")
    view = _decide(yolo, request_id)
    assert view["state"] == "aborted"
    assert yolo.machine.latched and yolo.machine.window is None
    assert _refused(yolo.press, "internal") == "yolo_latched"


def test_a_narrowing_whose_row_fails_still_holds_and_latches(yolo, audit):
    request_id = yolo.press("internal")["id"]
    audit.fail.add("audit.yolo.denied")
    assert _decide(yolo, request_id, "deny")["state"] == "denied"  # in effect at once
    assert yolo.machine.latched


def test_a_cancel_during_the_confirmation_write_wins(yolo, audit):
    request_id = yolo.press("internal")["id"]
    audit.hold = threading.Event()
    result = {}
    worker = threading.Thread(target=lambda: result.update(view=_decide(yolo, request_id)))
    worker.start()
    for _ in range(200):
        if yolo.machine.request.state == "issuing":
            break
        threading.Event().wait(0.01)
    assert yolo.machine.request.state == "issuing"
    canceller = threading.Thread(target=lambda: result.update(cancel=yolo.cancel(request_id)))
    canceller.start()
    threading.Event().wait(0.05)
    audit.hold.set()
    worker.join(5)
    canceller.join(5)
    assert yolo.machine.request.state == "fenced" and yolo.machine.request.reason == "cancel"
    assert yolo.machine.window is None and not yolo.machine.latched
    # The issuance ran to its end, the final CAS saw the fence: a normal end.
    assert sorted(audit.types()) == sorted(
        ["requested", "confirmed", "request_fenced", "started", "ended"]
    )
    assert audit.of("ended")[0]["end_reason"] == "issue_aborted_cancel"

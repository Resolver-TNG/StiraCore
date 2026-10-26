"""W1 fix F1 (#296, ADR 0012 §6a / §6b row 7): a move of the **shared** authority epoch
during a YOLO issuance fences it (``issue_aborted_epoch``), with the process-wide
service injected — a breaker transition, a narrowing or the kill switch that the YOLO
machine never sees, and a rules save whose epoch write is slow.

Same fixtures as ``tests/test_yolo_window.py`` (fake clocks, the ``Recorder`` audit
chain, a real marker file). No model, no server.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from stiracore import authority
from stiracore.authority import AuthorityService, epoch_store
from stiracore.yolo import runtime
from stiracore.yolo.requests import YoloRequests
from stiracore.yolo.window import WindowMarker
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.test_yolo_window import POINTS, Watch, _confirm, _inject


@pytest.fixture(autouse=True)
def _no_process_service():
    yield
    authority.uninstall()


def _data(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return data


def _new_service(tmp_path):
    service = AuthorityService(_data(tmp_path))
    assert service.unhealthy_reason is None
    return service


def _yolo(service, audit, clock, marker):
    return YoloRequests(
        allowance="external",
        incarnation=service.incarnation,
        record=audit,
        actor="local:tester",
        clocks_supported=True,
        clock=clock,
        ledger=audit,
        marker=marker,
        breaker=lambda: "normal",  # the live level is NORMAL again at the final CAS
        authority=service,
    )


def _assert_aborted_by_epoch(yolo, audit, marker, watch):
    assert "active" not in watch.states and "paused" not in watch.states
    assert yolo.machine.window is None  # ended and closed again
    assert yolo.machine.request.state == "fenced"
    assert yolo.machine.request.reason == "epoch"
    assert not yolo.machine.latched  # a normal end (§6a)
    assert [row["end_reason"] for row in audit.of("ended")] == ["issue_aborted_epoch"]
    assert audit.of("ended")[0]["end_class"] == "normal"
    assert marker.read().state == "closed"
    assert "request_fenced" in audit.types()


NARROWINGS = {
    "breaker_transition": lambda s: s.breaker_transition("normal", "warning"),
    "breaker_between_levels": lambda s: s.breaker_transition("warning", "critical"),
    "narrow": lambda s: s.narrow("revoked"),
    "service_advance": lambda s: s.advance_epoch(),
}


@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("name", sorted(NARROWINGS))
def test_a_shared_epoch_move_at_every_io_point_fences_the_issuance(
    tmp_path, monkeypatch, point, name
):
    """Astra probe 1: the YOLO machine's own epoch does not move; the shared one does."""
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    machine_epoch = yolo.machine.epoch
    fired = _inject(yolo, audit, marker, monkeypatch, point, lambda: NARROWINGS[name](service))
    _confirm(yolo, request_id)
    assert fired == [point]
    _assert_aborted_by_epoch(yolo, audit, marker, watch)
    assert yolo.machine.epoch == machine_epoch + 1  # fenced as by ``advance_epoch``
    service.flush(timeout=5)


def test_no_shared_move_opens_the_window_as_before(tmp_path):
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    _confirm(yolo, yolo.press("internal")["id"])
    assert yolo.machine.window.state == "active"
    assert "request_fenced" not in audit.types()
    service.flush(timeout=5)


def test_a_rules_save_with_a_slow_epoch_write_fences_before_it_waits(tmp_path, monkeypatch):
    """Astra probe 2: the rules save runs on another thread while the issuance writes its
    started row, and the epoch write blocks. The request is fenced **before** the write
    is waited for, and the window never becomes usable."""
    service = authority.install(_data(tmp_path))
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    monkeypatch.setattr(runtime, "_DESK", SimpleNamespace(yolo=yolo))
    watch = Watch(yolo)
    real_write = epoch_store.write
    entered, release = threading.Event(), threading.Event()
    seen_while_blocked = []

    base = []

    def slow_write(data_dir, records):
        # The first write of an epoch the rules save moved (beyond ``base``) is slow; it
        # may run on the saver or on the settle worker. Earlier epochs pass through.
        if base and records[-1].epoch > base[0] and not entered.is_set():
            with yolo._lock:  # the fence is complete once its lock section is
                seen_while_blocked.append(yolo.machine.request.state)
            entered.set()
            assert release.wait(5)
        return real_write(data_dir, records)

    monkeypatch.setattr(epoch_store, "write", slow_write)
    saver = threading.Thread(target=runtime.advance_epoch)

    def during_started_row():
        base.append(service.epoch)
        saver.start()
        assert entered.wait(5)  # the epoch write is now blocked

    audit.before["audit.yolo.started"] = during_started_row
    request_id = yolo.press("internal")["id"]
    _confirm(yolo, request_id)
    assert seen_while_blocked == ["fenced"]  # fenced first, then persisted
    assert yolo.machine.request.state == "fenced"  # still blocked: no window
    release.set()
    saver.join(5)
    assert not saver.is_alive()
    assert service.flush(timeout=5)
    _assert_aborted_by_epoch(yolo, audit, marker, watch)
    assert service.unhealthy_reason is None
    assert epoch_store.load(tmp_path / "data").last.epoch == service.epoch


def test_a_kill_during_issuance_fences_it_too(tmp_path, monkeypatch):
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    _inject(yolo, audit, marker, monkeypatch, "started", service.kill)
    _confirm(yolo, request_id)
    _assert_aborted_by_epoch(yolo, audit, marker, watch)


def test_a_killed_or_unhealthy_service_is_its_own_void_reason(tmp_path):
    """F8: the refusal of the shared service is recorded as ``killed`` /
    ``authority_unhealthy``, not hidden behind ``permit_stale``."""
    from stiracore.yolo.audit_schema import VOID_REASONS

    assert {"killed", "authority_unhealthy"} <= VOID_REASONS
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    _confirm(yolo, yolo.press("internal")["id"])
    assert yolo.machine.window.state == "active"
    service.kill()
    grant, outcome = _approve(yolo)
    assert grant is None and outcome == "permit_stale"
    assert audit.of("void")[-1]["reason_code"] == "killed"


def _approve(yolo):
    """One automatic approval through the real path (an internal tool, a human turn)."""
    from stiracore.yolo import origin
    from tests.test_yolo_auto_approve import AGENT
    from tests.test_yolo_auto_approve import _approve as approve

    with origin.human_turn(AGENT):
        return approve(yolo)


# --- R2-1: the final commit is atomic with the shared epoch ---------------------------

GAP_CALLBACKS = ("_now", "_healthy", "_breaker_normal", "_loosening_allowed")


@pytest.mark.parametrize("callback", GAP_CALLBACKS)
def test_a_shared_move_after_the_early_check_and_before_the_commit_fences(tmp_path, callback):
    """Astra's probe: a service-only move from one of the callbacks the final CAS reads
    after its early shared-epoch check (clock, writer health, breaker, loosening)."""
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    real = getattr(yolo, callback)
    fired = []

    def hook(*args, **kwargs):
        request = yolo.machine.request
        if not fired and request is not None and request.state == "issuing" and not yolo._busy:
            fired.append(service.epoch)
            service.breaker_transition("normal", "warning")
        return real(*args, **kwargs)

    setattr(yolo, callback, hook)
    _confirm(yolo, request_id)
    assert fired, "the callback ran inside the final CAS"
    _assert_aborted_by_epoch(yolo, audit, marker, watch)
    assert service.flush(timeout=5)


def test_issuing_is_published_atomically_with_the_epoch_capture(tmp_path):
    """The ``issuing`` machine is not published through ``_publish`` (a move between
    that publish and a later read of the epoch would be absorbed): it is swapped inside
    the leaf lock, together with the capture. A move right after it still fences."""
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    original = yolo._publish
    fired = []

    def publish(machine):
        original(machine)
        if machine.request is not None and machine.request.state == "issuing" and not fired:
            fired.append(True)
            service.breaker_transition("normal", "warning")  # between publish and capture

    yolo._publish = publish
    real_swap = yolo._swap_at_shared_epoch_locked
    captured = []

    def swap(machine, **kw):
        result = real_swap(machine, **kw)
        captured.append((machine.request.state, result[1]))
        return result

    yolo._swap_at_shared_epoch_locked = swap
    audit.before["audit.yolo.confirmed"] = lambda: service.breaker_transition("normal", "critical")
    _confirm(yolo, request_id)
    assert fired == []  # issuing never went through the plain publish
    assert captured[0][0] == "issuing"
    _assert_aborted_by_epoch(yolo, audit, marker, watch)
    assert service.flush(timeout=5)


def test_a_missing_capture_fails_closed(tmp_path, monkeypatch):
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    watch = Watch(yolo)
    request_id = yolo.press("internal")["id"]
    _inject(
        yolo,
        audit,
        marker,
        monkeypatch,
        "started",
        lambda: setattr(yolo, "_issue_authority_epoch", None),
    )
    _confirm(yolo, request_id)
    _assert_aborted_by_epoch(yolo, audit, marker, watch)


def test_a_kill_at_consume_is_its_own_void_reason(tmp_path):
    """Opus r2: the consume side records ``killed`` too (not ``consume_refused``)."""
    service = _new_service(tmp_path)
    audit, clock, marker = Recorder(), Clocks(), WindowMarker(tmp_path)
    yolo = _yolo(service, audit, clock, marker)
    _confirm(yolo, yolo.press("internal")["id"])
    grant, outcome = _approve(yolo)
    assert outcome == "approved"
    service.kill()
    assert yolo.consume(grant) == (False, "killed")
    assert audit.of("void")[-1]["reason_code"] == "killed"

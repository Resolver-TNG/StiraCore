"""ADR 0012 §6b — every row of the request × window × latch table (phase 0, pure)."""

from __future__ import annotations

import pytest

from stiracore.yolo import state as sm
from stiracore.yolo.state import Refused

RID = "a" * 32
WID = "b" * 32


def pending(allowance="external", tier="internal"):
    return sm.press(sm.initial(allowance), request_id=RID, window_id=WID, tier=tier)


def issuing(**kw):
    return sm.confirm_begin(pending(**kw), RID)


def commit(machine, **overrides):
    args = {
        "started_written": True,
        "receipts_ok": True,
        "writer_healthy": True,
        "incarnation_same": True,
        "before_expiry": True,
        "breaker_normal": True,
    }
    args.update(overrides)
    return sm.issue_commit(machine, RID, WID, **args)


def opened(**kw):
    machine, outcome = commit(issuing(**kw))
    assert outcome == "opened"
    return machine


# --- row 1: no request, no window → pending ------------------------------------------


def test_press_from_idle_creates_a_pending_request_with_its_window_id():
    machine = pending()
    assert machine.request.state == "pending"
    assert machine.request.window_id == WID  # allocated at the press (§11)
    assert machine.window is None
    assert machine.fence == 0


@pytest.mark.parametrize(
    "allowance, tier, code",
    [
        ("off", "internal", "yolo_not_allowed"),
        ("internal", "external", "yolo_tier_not_allowed"),
        ("external", "off", "yolo_tier_not_allowed"),
        ("external", "all", "yolo_tier_not_allowed"),
    ],
)
def test_press_refuses_without_allowance_or_above_it(allowance, tier, code):
    with pytest.raises(Refused) as caught:
        sm.press(sm.initial(allowance), request_id=RID, window_id=WID, tier=tier)
    assert caught.value.code == code


def test_press_refuses_while_latched_pending_or_during_a_window():
    with pytest.raises(Refused, match="yolo_latched"):
        sm.press(
            sm.initial("internal", latched=True), request_id=RID, window_id=WID, tier="internal"
        )
    with pytest.raises(Refused, match="yolo_request_pending"):
        sm.press(pending(), request_id="c" * 32, window_id="d" * 32, tier="internal")
    with pytest.raises(Refused, match="yolo_request_pending"):
        sm.press(issuing(), request_id="c" * 32, window_id="d" * 32, tier="internal")
    # During a window: no extension (§1a 5.).
    with pytest.raises(Refused, match="yolo_active"):
        sm.press(opened(), request_id="c" * 32, window_id="d" * 32, tier="internal")


# --- row 2: pending → issuing (CAS first) ---------------------------------------------


def test_confirmation_cas_records_fence_and_epoch():
    machine = sm.advance_epoch(pending())  # an epoch move while pending does not fence
    assert machine.request.state == "pending"
    machine = sm.confirm_begin(machine, RID)
    assert machine.request.state == "issuing"
    assert (machine.request.fence_seen, machine.request.epoch_seen) == (0, 1)


def test_confirmation_of_another_or_a_non_pending_request_is_stale():
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.confirm_begin(pending(), "f" * 32)
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.confirm_begin(issuing(), RID)  # single-use


# --- row 3: pending → cancel / stop / /new --------------------------------------------


@pytest.mark.parametrize(
    "event, state, reason",
    [
        (lambda m: sm.cancel(m, RID), "cancelled", "cancel"),
        (sm.stop, "fenced", "stop"),
        (sm.new_session, "fenced", "new_session"),
    ],
)
def test_pending_request_cancel_stop_new_fence_and_a_late_confirmation_is_refused(
    event, state, reason
):
    machine = event(pending())
    assert (machine.request.state, machine.request.reason) == (state, reason)
    assert machine.fence == 1
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.confirm_begin(machine, RID)


# --- row 4: pending → deny / 2 minutes passed -----------------------------------------


@pytest.mark.parametrize(
    "event, state",
    [(sm.deny, "denied"), (sm.request_timeout, "expired")],
)
def test_pending_request_denied_or_expired_is_a_normal_end(event, state):
    machine = event(pending(), RID)
    assert machine.request.state == state
    assert machine.fence == 1
    assert not machine.latched
    # A new press is accepted afterwards (nothing current).
    sm.press(machine, request_id="c" * 32, window_id="d" * 32, tier="internal")


def test_deny_or_timeout_of_an_issuing_request_is_stale():
    for event in (sm.deny, sm.request_timeout):
        with pytest.raises(Refused, match="yolo_request_stale"):
            event(issuing(), RID)


# --- row 5: issuing → (3) succeeds ----------------------------------------------------


def test_commit_opens_active_window_fence_unchanged():
    machine = opened()
    assert machine.request.state == "opened"
    assert machine.window.state == "active"
    assert machine.window.generation == 1  # started: 0 → 1
    assert machine.window.window_id == WID
    assert machine.fence == 0


def test_commit_while_breaker_not_normal_opens_paused():
    machine, outcome = commit(issuing(), breaker_normal=False)
    assert outcome == "opened"
    assert machine.window.state == "paused"


# --- row 6: issuing → stop / /new / cancel --------------------------------------------


@pytest.mark.parametrize(
    "event, outcome",
    [
        (sm.stop, "issue_aborted_stop"),
        (sm.new_session, "issue_aborted_new_session"),
        (lambda m: sm.cancel(m, RID), "issue_aborted_cancel"),
    ],
)
def test_fence_during_issuance_wins_the_commit_and_ends_normally(event, outcome):
    machine = event(issuing())
    assert machine.request.state == "fenced"
    assert machine.fence == 1
    after, result = commit(machine)
    assert result == outcome
    assert sm.end_class(result) == "normal"
    assert after.window.state == "ended"  # started was written: the close sequence follows
    assert after.request.state == "fenced"
    closed = sm.close_done(after, durable=True)
    assert closed.window is None and not closed.latched


def test_fence_before_started_was_written_needs_no_close():
    after, result = commit(sm.stop(issuing()), started_written=False)
    assert result == "issue_aborted_stop"
    assert after.window is None and not after.latched


# --- row 7: issuing → authority epoch moved -------------------------------------------


def test_epoch_move_during_issuance_fences_and_is_normal():
    machine = sm.advance_epoch(issuing())
    assert (machine.request.state, machine.request.reason) == ("fenced", "epoch")
    assert machine.fence == 1
    after, result = commit(machine)
    assert result == "issue_aborted_epoch"
    assert not after.latched
    assert sm.close_done(after, durable=True).window is None


# --- row 8: issuing → write / receipt failure, health latched, clock anomaly ----------


@pytest.mark.parametrize(
    "override",
    [
        {"receipts_ok": False},
        {"writer_healthy": False},
        {"incarnation_same": False},
        {"before_expiry": False},
        {"started_written": False},
    ],
)
def test_failed_commit_aborts_abnormally_and_latches(override):
    machine, outcome = commit(issuing(), **override)
    assert outcome == "aborted_issue_failed"
    assert sm.end_class(outcome) == "abnormal"
    assert machine.request.state == "aborted"
    assert machine.latched
    assert machine.fence == 1
    assert machine.window is None or not machine.window.open


def test_issue_failed_latches_even_after_a_durable_end_row():
    machine = sm.issue_failed(issuing(), RID, started_written=True)
    assert machine.latched
    # The ended row was written and durable: still latched (§6 "also when its end row was
    # written successfully").
    machine = sm.close_done(machine, durable=True)
    assert machine.latched
    with pytest.raises(Refused, match="yolo_latched"):
        sm.press(machine, request_id="c" * 32, window_id="d" * 32, tier="internal")


def test_commit_for_another_window_id_or_request_is_refused_or_fails():
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.issue_commit(
            issuing(),
            "f" * 32,
            WID,
            started_written=True,
            receipts_ok=True,
            writer_healthy=True,
            incarnation_same=True,
            before_expiry=True,
            breaker_normal=True,
        )
    machine, outcome = sm.issue_commit(
        issuing(),
        RID,
        "e" * 32,
        started_written=True,
        receipts_ok=True,
        writer_healthy=True,
        incarnation_same=True,
        before_expiry=True,
        breaker_normal=True,
    )
    assert outcome == "aborted_issue_failed" and machine.latched


def test_commit_of_a_pending_request_is_refused():
    with pytest.raises(Refused, match="yolo_request_stale"):
        commit(pending())


# --- row 9: window active / paused → narrowing ----------------------------------------


@pytest.mark.parametrize(
    "reason",
    sorted(sm.NORMAL_END_REASONS - {r for r in sm.NORMAL_END_REASONS if r.startswith("issue_")}),
)
def test_window_narrowing_ends_it_and_moves_fence_generation_epoch(reason):
    machine = opened()
    ended = sm.end_window(machine, reason)
    assert ended.window.state == "ended"
    assert ended.window.end_reason == reason
    assert ended.fence == machine.fence + 1
    assert ended.window.generation == machine.window.generation + 1
    assert ended.epoch == machine.epoch + 1


@pytest.mark.parametrize(
    "event, reason", [(sm.stop, "revoked_stop"), (sm.new_session, "revoked_new_session")]
)
def test_stop_and_new_end_an_active_or_paused_window(event, reason):
    for machine in (opened(), sm.pause(opened(), "breaker_warning")):
        ended = event(machine)
        assert ended.window.end_reason == reason
        assert ended.fence == machine.fence + 1


def test_stop_with_nothing_current_is_never_refused():
    machine = sm.stop(sm.initial("internal"))
    assert machine.fence == 1 and machine.window is None and machine.request is None


def test_pause_moves_generation_and_epoch_not_the_fence():
    machine = opened()
    paused = sm.pause(machine, "breaker_critical")
    assert paused.window.state == "paused"
    assert paused.window.generation == 2
    assert paused.epoch == machine.epoch + 1
    assert paused.fence == machine.fence


def _resume(machine, window_id=WID, generation=None, *, breaker_normal=True, before_expiry=True):
    generation = machine.window.generation if generation is None else generation
    return sm.resume(
        machine,
        window_id,
        generation,
        breaker_normal=breaker_normal,
        before_expiry=before_expiry,
    )


def test_resume_is_a_cas_on_the_pause_generation():
    paused = sm.pause(opened(), "breaker_warning")
    resumed = _resume(paused)
    assert resumed.window.state == "active"
    assert resumed.window.generation == paused.window.generation + 1
    # A late dwell timer with the old generation cannot turn a newer state back.
    repaused = sm.pause(resumed, "breaker_warning")
    with pytest.raises(Refused, match="yolo_resume_stale"):
        _resume(repaused, generation=paused.window.generation)
    ended = sm.stop(paused)
    with pytest.raises(Refused, match="yolo_resume_stale"):
        _resume(ended, generation=paused.window.generation)
    with pytest.raises(Refused, match="yolo_resume_stale"):
        _resume(resumed, generation=resumed.window.generation)  # not paused: re-run refused


def test_a_second_pause_sample_invalidates_the_resume_prepared_for_the_first():
    first = sm.pause(opened(), "breaker_warning")
    second = sm.pause(first, "breaker_critical")  # level changed while paused
    assert second.window.generation == first.window.generation + 1
    assert second.epoch == first.epoch + 1
    with pytest.raises(Refused, match="yolo_resume_stale"):
        _resume(second, generation=first.window.generation)
    again = sm.pause(second, "breaker_critical")  # same level, a new sample: still moves
    assert again.window.generation == second.window.generation + 1


def test_resume_of_an_old_window_never_matches_a_new_window():
    # Old window: pause (generation 2) -> stop -> close.  New window: pause (generation 2).
    old = sm.pause(opened(), "breaker_warning")
    stale_generation = old.window.generation
    idle = sm.close_done(sm.stop(old), durable=True)
    new_rid, new_wid = "c" * 32, "d" * 32
    machine = sm.press(idle, request_id=new_rid, window_id=new_wid, tier="internal")
    machine = sm.confirm_begin(machine, new_rid)
    machine, outcome = sm.issue_commit(
        machine,
        new_rid,
        new_wid,
        started_written=True,
        receipts_ok=True,
        writer_healthy=True,
        incarnation_same=True,
        before_expiry=True,
        breaker_normal=True,
    )
    assert outcome == "opened"
    machine = sm.pause(machine, "breaker_warning")
    assert machine.window.generation == stale_generation  # the ABA case
    with pytest.raises(Refused, match="yolo_resume_stale"):
        _resume(machine, window_id=WID, generation=stale_generation)
    assert _resume(machine, window_id=new_wid).window.state == "active"


def test_ids_are_never_reused_in_a_process():
    idle = sm.close_done(sm.stop(opened()), durable=True)
    for rid, wid in ((RID, "d" * 32), ("c" * 32, WID), ("c" * 32, "c" * 32)):
        with pytest.raises(Refused, match="yolo_request_invalid"):
            sm.press(idle, request_id=rid, window_id=wid, tier="internal")


@pytest.mark.parametrize(
    "flags, code",
    [
        ({"breaker_normal": False}, "yolo_breaker_not_normal"),
        ({"before_expiry": False}, "yolo_expired"),
        ({"before_expiry": False, "breaker_normal": False}, "yolo_expired"),  # expiry first
    ],
)
def test_resume_needs_a_normal_live_breaker_and_an_unexpired_window(flags, code):
    paused = sm.pause(opened(), "breaker_warning")
    with pytest.raises(Refused, match=code):
        _resume(paused, **flags)


# --- failure races, late arrivals, re-runs -------------------------------------------

_FENCES = {
    "stop": sm.stop,
    "new_session": sm.new_session,
    "cancel": lambda m: sm.cancel(m, RID),
    "epoch": sm.advance_epoch,
}
_ANOMALIES = [
    {"receipts_ok": False},
    {"writer_healthy": False},
    {"incarnation_same": False},
    {"receipts_ok": False, "writer_healthy": False},
]


@pytest.mark.parametrize("fence", sorted(_FENCES))
@pytest.mark.parametrize("anomaly", _ANOMALIES, ids=lambda a: "+".join(sorted(a)))
@pytest.mark.parametrize("started_written", [True, False])
def test_a_fence_never_hides_an_audit_anomaly(fence, anomaly, started_written):
    fenced = _FENCES[fence](issuing())
    machine, outcome = commit(fenced, started_written=started_written, **anomaly)
    assert outcome == "aborted_issue_failed"
    assert machine.latched  # the stop won (nothing opened) and the failure is kept
    assert machine.window is None or not machine.window.open
    if started_written:
        assert sm.close_done(machine, durable=True).latched  # a durable end row: still latched
    with pytest.raises(Refused, match="yolo_latched"):
        sm.press(machine, request_id="c" * 32, window_id="d" * 32, tier="internal")


@pytest.mark.parametrize("fence", sorted(_FENCES))
def test_a_commit_with_another_window_id_after_a_fence_latches(fence):
    machine, outcome = sm.issue_commit(
        _FENCES[fence](issuing()),
        RID,
        "e" * 32,
        started_written=True,
        receipts_ok=True,
        writer_healthy=True,
        incarnation_same=True,
        before_expiry=True,
        breaker_normal=True,
    )
    assert outcome == "aborted_issue_failed" and machine.latched


@pytest.mark.parametrize("fence", sorted(_FENCES))
def test_issue_failure_reported_after_a_fence_latches(fence):
    machine = sm.issue_failed(_FENCES[fence](issuing()), RID, started_written=True)
    assert machine.latched and not machine.window.open
    assert machine.request.state == "fenced"  # the fence still won


def test_failure_report_for_a_fenced_pending_request_is_stale():
    # Never confirmed: no issuance ran, so there is nothing to fail.
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.issue_failed(sm.stop(pending()), RID, started_written=False)


@pytest.mark.parametrize(
    "first",
    [
        lambda m: commit(m)[0],
        lambda m: commit(m, receipts_ok=False)[0],
        lambda m: commit(sm.stop(m))[0],
        lambda m: sm.issue_failed(m, RID, started_written=True),
    ],
    ids=["opened", "failed_commit", "fenced_commit", "issue_failed"],
)
def test_each_issuance_is_settled_once(first):
    settled = first(issuing())
    with pytest.raises(Refused, match="yolo_request_stale"):
        commit(settled)
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.issue_failed(settled, RID, started_written=True)
    with pytest.raises(Refused, match="yolo_request_stale"):
        sm.confirm_begin(settled, RID)


def test_press_waits_for_a_fenced_issuance_to_settle():
    fenced = sm.stop(issuing())
    with pytest.raises(Refused, match="yolo_closing"):
        sm.press(fenced, request_id="c" * 32, window_id="d" * 32, tier="internal")
    settled, _ = commit(fenced, started_written=False)
    sm.press(settled, request_id="c" * 32, window_id="d" * 32, tier="internal")


def test_late_events_after_the_end_change_nothing():
    ended = sm.close_done(sm.stop(opened()), durable=True)
    for late in (
        lambda m: sm.confirm_begin(m, RID),
        lambda m: sm.deny(m, RID),
        lambda m: sm.request_timeout(m, RID),
        lambda m: sm.cancel(m, RID),
        lambda m: commit(m),
        lambda m: _resume(m, generation=2),
        lambda m: sm.pause(m, "breaker_warning"),
    ):
        with pytest.raises(Refused):
            late(ended)


def test_random_event_sequences_keep_the_invariants():
    """A seeded random walk over every event, with stale and repeated arguments.

    Invariants after every step: the fence never decreases; at most one window; an open
    window exists only after a commit of an unfenced issuance with every check true;
    while latched, no press is accepted; an id is never reused.
    """
    import random

    rng = random.Random(365)
    counter = iter(range(10**6))

    def new_id():
        return f"{next(counter):032x}"

    def candidates(m):
        rid = m.request.request_id if m.request else new_id()
        wid = m.request.window_id if m.request else new_id()
        gen = m.window.generation if m.window else 1
        flags = {
            k: rng.random() > 0.2
            for k in (
                "started_written",
                "receipts_ok",
                "writer_healthy",
                "incarnation_same",
                "before_expiry",
                "breaker_normal",
            )
        }
        return [
            (
                "press",
                lambda: sm.press(m, request_id=new_id(), window_id=new_id(), tier="internal"),
            ),
            ("press_reused", lambda: sm.press(m, request_id=rid, window_id=wid, tier="internal")),
            ("confirm", lambda: sm.confirm_begin(m, rid)),
            ("cancel", lambda: sm.cancel(m, rid)),
            ("deny", lambda: sm.deny(m, rid)),
            ("timeout", lambda: sm.request_timeout(m, rid)),
            ("stop", lambda: sm.stop(m)),
            ("new", lambda: sm.new_session(m)),
            ("epoch", lambda: sm.advance_epoch(m)),
            ("commit", lambda: sm.issue_commit(m, rid, wid, **flags)[0]),
            ("fail", lambda: sm.issue_failed(m, rid, started_written=rng.random() > 0.5)),
            ("pause", lambda: sm.pause(m, "breaker_warning")),
            (
                "resume",
                lambda: sm.resume(
                    m,
                    wid if rng.random() > 0.2 else new_id(),
                    gen - rng.choice((0, 0, 1)),
                    breaker_normal=rng.random() > 0.3,
                    before_expiry=rng.random() > 0.1,
                ),
            ),
            ("end", lambda: sm.end_window(m, rng.choice(sorted(sm.NORMAL_END_REASONS)))),
            ("close", lambda: sm.close_done(m, durable=rng.random() > 0.2)),
            ("unlatch", lambda: sm.unlatch(m)),
        ], flags

    machine = sm.initial("external")
    opened_ok = 0
    for _ in range(5000):
        events, flags = candidates(machine)
        name, event = rng.choice(events)
        before = machine
        try:
            machine = event()
        except Refused:
            assert machine is before
            continue
        assert machine.fence >= before.fence
        assert machine.epoch >= before.epoch
        if before.latched and name == "press":
            raise AssertionError("a press was accepted while latched")
        newly_open = (
            machine.window is not None
            and machine.window.open
            and (before.window is None or not before.window.open)
        )
        if newly_open:
            assert name in ("commit", "resume") or before.window is not None
            if name == "commit":
                assert before.request.state == "issuing"
                assert all(flags[k] for k in flags if k != "breaker_normal")
                opened_ok += 1
        if machine.window is not None and machine.window.open:
            assert machine.request is not None and machine.request.state == "opened"
    assert opened_ok > 0  # the walk did reach the open state


def test_unknown_end_reason_is_abnormal():
    ended = sm.end_window(opened(), "made_up")
    assert sm.end_class(ended.window.end_reason) == "abnormal"
    assert sm.close_done(ended, durable=True).latched


# --- row 10: window ended normally, close sequence done → no window -------------------


def test_normal_end_with_durable_close_returns_to_idle_and_accepts_a_press():
    machine = sm.close_done(sm.end_window(opened(), "expired_time"), durable=True)
    assert machine.window is None and not machine.latched
    sm.press(machine, request_id="c" * 32, window_id="d" * 32, tier="internal")


def test_ended_window_before_its_close_refuses_a_press():
    ended = sm.end_window(opened(), "expired_time")
    with pytest.raises(Refused, match="yolo_closing"):
        sm.press(ended, request_id="c" * 32, window_id="d" * 32, tier="internal")


# --- row 11: abnormal end or close not durable → latched -------------------------------


@pytest.mark.parametrize(
    "reason, durable",
    [
        ("revoked_audit_failed", True),
        ("revoked_clock_anomaly", True),
        ("expired_time", False),
        ("revoked_stop", False),
    ],
)
def test_abnormal_end_or_non_durable_close_latches_until_unlatch(reason, durable):
    machine = sm.close_done(sm.end_window(opened(), reason), durable=durable)
    assert machine.latched
    # #594: the ended window stays until unlatch (§11); the badge reads the latch first.
    assert machine.window is not None and machine.window.state == "ended"
    with pytest.raises(Refused, match="yolo_latched"):
        sm.press(machine, request_id="c" * 32, window_id="d" * 32, tier="internal")
    unlatched = sm.unlatch(machine)
    assert not unlatched.latched and unlatched.window is None
    sm.press(unlatched, request_id="c" * 32, window_id="d" * 32, tier="internal")


def test_unlatch_never_opens_a_window_and_needs_a_latch():
    with pytest.raises(Refused, match="yolo_not_latched"):
        sm.unlatch(sm.initial("internal"))
    machine = sm.unlatch(sm.initial("internal", latched=True))
    assert machine.window is None and machine.request is None


def test_restart_leaves_no_request_and_no_window():
    # A request never outlives its incarnation: the next process starts from initial().
    fresh = sm.initial("external")
    assert fresh.request is None and fresh.window is None and fresh.fence == 0


def test_end_classes_partition_the_reasons():
    assert not sm.NORMAL_END_REASONS & sm.ABNORMAL_END_REASONS
    for reason in sm.ABNORMAL_END_REASONS:
        assert sm.end_class(reason) == "abnormal"
    for reason in sm.NORMAL_END_REASONS:
        assert sm.end_class(reason) == "normal"

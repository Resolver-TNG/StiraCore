"""ADR 0012 §2 / §13 (phase 1): the shared authority boundary and the one loosening judgement."""

from __future__ import annotations

import threading

import pytest

from stiracore.tools.tool_config import rules_loosening
from stiracore.yolo.authority import (
    AuthorityBoundary,
    AuthorityError,
    GrantRecord,
    Receipt,
    require_kind,
)
from stiracore.yolo.loosening import NOT_SETTABLE, YOLO_CONTEXTS

GID = "a" * 32
RECEIPT = Receipt(event_id=f"op:{'b' * 32}:decision", seq=7, hash="c" * 64)


def record(boundary, kind="yolo", generation=1, state="active", **kw):
    return GrantRecord(
        kind=kind,
        grant_id=kw.get("grant_id", GID),
        incarnation=kw.get("incarnation", boundary.incarnation),
        generation=generation,
        state=state,
    )


# --- kind is a mandatory discriminator ------------------------------------------------


def test_kind_is_mandatory_and_closed():
    with pytest.raises(TypeError):
        GrantRecord(grant_id=GID, incarnation="x", generation=1, state="active")  # no kind
    for kind in ("", "Yolo", "session", None):
        with pytest.raises(AuthorityError, match="grant_kind_invalid"):
            GrantRecord(kind=kind, grant_id=GID, incarnation="x", generation=1, state="active")


def test_goal_and_yolo_functions_refuse_the_other_kind():
    boundary = AuthorityBoundary()
    yolo = record(boundary, "yolo")
    goal = record(boundary, "goal")
    assert require_kind(yolo, "yolo") is yolo
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        require_kind(yolo, "goal")
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        require_kind(goal, "yolo")
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        boundary.issue_permit(yolo, kind="goal", receipt=RECEIPT)
    permit = boundary.issue_permit(yolo, kind="yolo", receipt=RECEIPT)
    # A YOLO permit is never consumed as a goal grant, nor against a goal record.
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        boundary.consume(permit, goal, kind="goal")
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        boundary.consume(permit, goal, kind="yolo")
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        require_kind({"kind": "yolo"}, "yolo")  # a plain mapping is not a record


# --- incarnation, epoch ---------------------------------------------------------------


def test_incarnation_is_random_per_boundary_and_not_settable():
    first, second = AuthorityBoundary(), AuthorityBoundary()
    assert first.incarnation != second.incarnation
    assert len(first.incarnation) == 32
    with pytest.raises(TypeError):
        AuthorityBoundary(incarnation=first.incarnation)  # never read back from storage


def test_epoch_is_monotonic_also_under_concurrency():
    boundary = AuthorityBoundary()
    assert boundary.epoch == 0
    seen = []
    threads = [
        threading.Thread(target=lambda: seen.append(boundary.advance_epoch())) for _ in range(50)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(seen) == list(range(1, 51))
    assert boundary.epoch == 50


# --- single-use consume ---------------------------------------------------------------


def test_permit_is_consumed_exactly_once():
    boundary = AuthorityBoundary()
    current = record(boundary)
    permit = boundary.issue_permit(current, kind="yolo", receipt=RECEIPT)
    boundary.consume(permit, current, kind="yolo")
    with pytest.raises(AuthorityError, match="permit_consumed"):
        boundary.consume(permit, current, kind="yolo")
    assert boundary.void(permit) is False


def test_concurrent_consume_lets_exactly_one_through():
    boundary = AuthorityBoundary()
    current = record(boundary)
    permit = boundary.issue_permit(current, kind="yolo", receipt=RECEIPT)
    outcomes = []

    def attempt():
        try:
            boundary.consume(permit, current, kind="yolo")
            outcomes.append("ok")
        except AuthorityError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert set(outcomes) - {"ok"} == {"permit_consumed"}


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda b, r: (b.advance_epoch(), r)[1], "epoch_moved"),
        (lambda b, r: record(b, generation=2), "permit_stale"),
        (lambda b, r: record(b, state="paused"), "grant_not_active"),
        (lambda b, r: record(b, grant_id="d" * 32), "grant_mismatch"),
        (lambda b, r: record(b, incarnation="e" * 32), "incarnation_mismatch"),
    ],
)
def test_consume_rechecks_everything_and_refuses_any_change(change, code):
    boundary = AuthorityBoundary()
    current = record(boundary)
    permit = boundary.issue_permit(current, kind="yolo", receipt=RECEIPT)
    with pytest.raises(AuthorityError, match=code):
        boundary.consume(permit, change(boundary, current), kind="yolo")


@pytest.mark.parametrize(
    "field, value",
    [
        ("epoch", "current"),
        ("generation", 2),
        ("grant_id", "d" * 32),
        ("kind", "goal"),
        ("incarnation", "e" * 32),
        ("receipt", Receipt(event_id=f"op:{'9' * 32}:decision", seq=8, hash="c" * 64)),
    ],
)
def test_a_rewritten_permit_is_refused_even_with_a_valid_nonce(field, value):
    """The issued binding is kept server-side; a permit rebuilt with any other field fails."""
    import dataclasses

    boundary = AuthorityBoundary()
    current = record(boundary)
    permit = boundary.issue_permit(current, kind="yolo", receipt=RECEIPT)
    if field == "epoch":
        boundary.advance_epoch()  # the permit is stale now ...
        value = boundary.epoch  # ... and the forgery claims the new epoch
    target = current
    if field == "generation":
        target = record(boundary, generation=2)  # the record moved on too
    if field == "grant_id":
        target = record(boundary, grant_id="d" * 32)
    forged = dataclasses.replace(permit, **{field: value})
    kind = "goal" if field == "kind" else "yolo"
    if field == "kind":
        target = record(boundary, kind="goal")
    with pytest.raises(AuthorityError) as caught:
        boundary.consume(forged, target, kind=kind)
    assert caught.value.code == "permit_tampered"
    # The genuine permit is untouched by the attempt (unless its own binding went stale).
    if field not in {"epoch", "generation", "grant_id", "kind"}:
        boundary.consume(permit, current, kind="yolo")


def test_permit_is_immutable():
    import dataclasses

    boundary = AuthorityBoundary()
    permit = boundary.issue_permit(record(boundary), kind="yolo", receipt=RECEIPT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        permit.epoch = 5


def test_a_permit_like_object_is_refused():
    from types import SimpleNamespace

    boundary = AuthorityBoundary()
    current = record(boundary)
    permit = boundary.issue_permit(current, kind="yolo", receipt=RECEIPT)
    lookalike = SimpleNamespace(**{f: getattr(permit, f) for f in permit.__dataclass_fields__})
    with pytest.raises(AuthorityError):
        boundary.consume(lookalike, current, kind="yolo")


def test_permit_of_another_incarnation_is_refused():
    old, new = AuthorityBoundary(), AuthorityBoundary()
    permit = old.issue_permit(record(old), kind="yolo", receipt=RECEIPT)
    with pytest.raises(AuthorityError):
        new.consume(permit, record(new), kind="yolo")
    with pytest.raises(AuthorityError, match="incarnation_mismatch"):
        new.issue_permit(record(old), kind="yolo", receipt=RECEIPT)


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        Receipt(event_id="evt-1", seq=1, hash="c" * 64),
        Receipt(event_id=f"op:{'b' * 32}:decision", seq=0, hash="c" * 64),
        Receipt(event_id=f"op:{'b' * 32}:decision", seq=1, hash="nothex"),
    ],
)
def test_permit_needs_a_receipt(receipt):
    boundary = AuthorityBoundary()
    with pytest.raises(AuthorityError, match="receipt_missing"):
        boundary.issue_permit(record(boundary), kind="yolo", receipt=receipt)


def test_inactive_record_gets_no_permit_and_void_returns_it():
    boundary = AuthorityBoundary()
    with pytest.raises(AuthorityError, match="grant_not_active"):
        boundary.issue_permit(record(boundary, state="paused"), kind="yolo", receipt=RECEIPT)
    permit = boundary.issue_permit(record(boundary), kind="yolo", receipt=RECEIPT)
    assert boundary.void(permit) is True
    with pytest.raises(AuthorityError, match="permit_unknown"):
        boundary.consume(permit, record(boundary), kind="yolo")


# --- the one loosening judgement (§2) -------------------------------------------------

OFF = {"yolo": "off"}


def window(tier="internal", state="active", cap=2000, expires_at=21600.0, **kw):
    return {
        "yolo": tier,
        "state": state,
        "cap": cap,
        "expires_at": expires_at,
        "grant_id": kw.get("grant_id", GID),
        "incarnation": kw.get("incarnation", "f" * 32),
    }


def test_issue_only_in_the_host_confirmed_context_within_the_allowance():
    assert rules_loosening(
        OFF, window(), context="host_confirmed_request", allowance="internal"
    ) == ["yolo"]
    assert NOT_SETTABLE in rules_loosening(
        OFF, window("external"), context="host_confirmed_request", allowance="internal"
    )
    for context in YOLO_CONTEXTS - {"host_confirmed_request"}:
        assert NOT_SETTABLE in rules_loosening(OFF, window(), context=context, allowance="external")


def test_resume_only_in_breaker_resume_with_everything_else_unchanged():
    paused = window(state="paused")
    assert rules_loosening(paused, window(), context="breaker_resume") == ["yolo_state"]
    assert NOT_SETTABLE in rules_loosening(paused, window(), context="yolo_narrowing")
    for changed in (
        window("external"),
        window(cap=2001),
        window(expires_at=21601.0),
        window(grant_id="0" * 32),
        window(incarnation="1" * 32),
    ):
        assert NOT_SETTABLE in rules_loosening(paused, changed, context="breaker_resume"), changed
    # ended → active is never allowed.
    assert NOT_SETTABLE in rules_loosening(
        window(state="ended"), window(), context="breaker_resume"
    )


def test_narrowing_is_never_refused_in_a_yolo_context():
    active = window()
    for after in (window(state="paused"), window(state="ended"), window(cap=10), OFF):
        for context in YOLO_CONTEXTS:
            assert rules_loosening(active, after, context=context) == [], (after, context)


def test_widening_an_open_window_is_never_allowed():
    for after in (window("external"), window(cap=3000), window(expires_at=99999.0)):
        for context in YOLO_CONTEXTS:
            assert NOT_SETTABLE in rules_loosening(
                window(), after, context=context, allowance="external"
            )


@pytest.mark.parametrize(
    "key, value",
    [
        ("expires_at", float("nan")),
        ("expires_at", float("inf")),
        ("expires_at", -1.0),
        ("expires_at", True),
        ("expires_at", None),
        ("cap", float("nan")),
        ("cap", 2000.0),
        ("cap", True),
        ("cap", 0),
        ("cap", 10001),
        ("cap", -5),
        ("grant_id", "short"),
        ("incarnation", None),
        ("state", "running"),
    ],
)
def test_unorderable_values_are_never_a_narrowing(key, value):
    """NaN compares False both ways: without validation 21600 → NaN → 999999 would pass as
    two narrowings. Every invalid value makes the transition refused (fail closed)."""
    bad = {**window(), key: value}
    for context in YOLO_CONTEXTS:
        assert NOT_SETTABLE in rules_loosening(window(), bad, context=context), (key, context)
        assert NOT_SETTABLE in rules_loosening(bad, window(), context=context), (key, context)
    assert NOT_SETTABLE in rules_loosening(
        OFF, bad, context="host_confirmed_request", allowance="external"
    )
    if key in ("cap", "expires_at"):
        missing = {k: v for k, v in window().items() if k != key}
        assert NOT_SETTABLE in rules_loosening(window(), missing, context="yolo_narrowing")


def test_the_nan_widening_sequence_is_refused_step_by_step():
    first = window(expires_at=21600.0)
    nan = window(expires_at=float("nan"))
    later = window(expires_at=999999.0)
    assert NOT_SETTABLE in rules_loosening(first, nan, context="yolo_narrowing")
    assert NOT_SETTABLE in rules_loosening(nan, later, context="yolo_narrowing")
    assert NOT_SETTABLE in rules_loosening(first, later, context="yolo_narrowing")


@pytest.mark.parametrize(
    "context",
    [None, "hub", "api:tools", "rollback", "pending_save", "recover", "tool-rules", "unlatch"],
)
def test_every_other_context_treats_yolo_keys_as_reserved(context):
    from stiracore.tools.tool_config import ToolConfig

    rules = ToolConfig().as_dict()
    for previous, current in (
        (rules, {**rules, "yolo": "external"}),
        ({**rules, "yolo_window": {}}, rules),
        (rules, {**rules, "nested": {"YOLO": 1}}),
    ):
        reasons = rules_loosening(previous, current, context=context)
        assert reasons[0] == NOT_SETTABLE, (context, reasons)
    # Without a yolo key the judgement is the one it always was.
    assert rules_loosening(rules, dict(rules), context=context) == []

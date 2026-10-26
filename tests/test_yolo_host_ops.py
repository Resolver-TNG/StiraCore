"""ADR 0012 §8b (phase 2): the host-confirmed operation — single use, expiry, incarnation,
stale target, rate limits, and only built-in values evaluated (no caller code runs).
"""

from __future__ import annotations

import secrets

import pytest

from stiracore.yolo.allowance import ClockReading
from stiracore.yolo.host_ops import (
    APPLY_GRACE_SEC,
    CODES,
    EXPIRY_SEC,
    KINDS,
    RATE_MAX,
    RATE_WINDOW_SEC,
    HostOperations,
    HostOpRefused,
    Unbindable,
    binding_digest,
    elapsed,
    target_digest,
)

B = "b" * 64
G = "a" * 64
SUMMARY = {"action": "approve", "tool": "file_write", "args": "0123abcd", "card": "deadbeef"}


class Clocks:
    """Wall, sleep-including and sleep-excluding readings, moved independently."""

    def __init__(self):
        self.wall, self.boot, self.mono = 1_800_000_000.0, 50_000.0, 40_000.0

    def __call__(self):
        return ClockReading(self.wall, self.boot, self.mono)

    def tick(self, seconds):
        self.wall += seconds
        self.boot += seconds
        self.mono += seconds


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def ops(clock):
    return HostOperations(incarnation=secrets.token_hex(16), clock=clock, rate_max=RATE_MAX)


def _code(fn, *args, **kwargs):
    with pytest.raises(HostOpRefused) as error:
        fn(*args, **kwargs)
    return error.value.code


def _request(ops, binding=B, generation=G, kind="rules_loosen"):
    return ops.request(kind, binding=binding, generation=generation, summary=SUMMARY)


def _confirm(ops, op_id, decision="confirm", incarnation=None):
    return ops.decide(
        op_id,
        incarnation=ops.incarnation if incarnation is None else incarnation,
        decision=decision,
        principal="local:tester",
        channel="cli",
    )


def _consume(ops, op_id, binding=B, generation=G, kind="rules_loosen"):
    return ops.consume(op_id, kind=kind, binding=binding, generation=generation)


# --- the happy path and single use ----------------------------------------------------


def test_request_confirm_consume_once(ops):
    view = _request(ops)
    assert len(view["id"]) == 32 and view["short"] == view["id"][:8]
    assert view["state"] == "pending" and view["expires_in"] == int(EXPIRY_SEC)
    assert view["word"] == "APPROVE" and view["code"] == B[:8]
    assert _code(_consume, ops, view["id"]) == "host_operation_pending"  # not yet
    assert _confirm(ops, view["id"])["state"] == "confirmed"
    _consume(ops, view["id"])  # applies once
    assert _code(_consume, ops, view["id"]) == "host_operation_used"  # the re-send
    assert ops.status(view["id"])["state"] == "used"


def test_a_second_decision_is_refused_with_the_first_state(ops):
    op = _request(ops)["id"]
    _confirm(ops, op)
    with pytest.raises(HostOpRefused) as error:
        _confirm(ops, op, "deny")
    assert error.value.code == "host_operation_decided" and error.value.state == "confirmed"
    _consume(ops, op)  # the first decision stands


def test_deny_and_cancel_never_apply(ops):
    op = _request(ops)["id"]
    _confirm(ops, op, "deny")
    assert _code(_consume, ops, op) == "host_operation_denied"
    op2 = _request(ops, binding="c" * 64)["id"]
    assert ops.cancel(op2)["state"] == "cancelled"
    assert _code(_confirm, ops, op2) == "host_operation_decided"
    assert _code(_consume, ops, op2, binding="c" * 64) == "host_operation_cancelled"


def test_a_confirmed_but_not_yet_applied_operation_can_still_be_cancelled(ops):
    op = _request(ops)["id"]
    _confirm(ops, op)
    ops.cancel(op)
    assert _code(_consume, ops, op) == "host_operation_cancelled"


# --- expiry (§7 clocks) ---------------------------------------------------------------


def test_a_confirmation_after_two_minutes_is_refused(ops, clock):
    op = _request(ops)["id"]
    clock.tick(EXPIRY_SEC - 0.5)
    assert ops.status(op)["state"] == "pending"
    clock.tick(1.0)
    assert _code(_confirm, ops, op) == "host_operation_expired"
    assert _code(_consume, ops, op) == "host_operation_expired"


def test_a_confirmed_operation_must_be_applied_within_the_grace(ops, clock):
    op = _request(ops)["id"]
    _confirm(ops, op)
    clock.tick(APPLY_GRACE_SEC + 0.1)
    assert _code(_consume, ops, op) == "host_operation_expired"


@pytest.mark.parametrize("which", ["wall", "boot", "mono"])
def test_any_one_clock_moving_ends_it(ops, clock, which):
    op = _request(ops)["id"]
    setattr(clock, which, getattr(clock, which) + EXPIRY_SEC)  # e.g. a sleep: boot only
    assert _code(_confirm, ops, op) == "host_operation_expired"


def test_a_wall_clock_set_back_does_not_extend_it(ops, clock):
    op = _request(ops)["id"]
    clock.wall -= 3600
    clock.mono += EXPIRY_SEC
    clock.boot += EXPIRY_SEC
    assert _code(_confirm, ops, op) == "host_operation_expired"
    assert elapsed(ClockReading(10, 10, 10), ClockReading(5, 11, 30)) == 20


# --- incarnation, restart, stale target, mismatch -------------------------------------


def test_another_incarnation_and_a_restart_are_refused(ops, clock):
    op = _request(ops)["id"]
    assert _code(_confirm, ops, op, incarnation=secrets.token_hex(16)) == (
        "host_operation_incarnation"
    )
    assert ops.status(op)["state"] == "pending"  # nothing decided by it
    restarted = HostOperations(incarnation=secrets.token_hex(16), clock=clock)
    assert _code(_confirm, restarted, op) == "host_operation_unknown"
    assert _code(_consume, restarted, op) == "host_operation_unknown"


def test_a_changed_target_generation_is_stale_and_spends_the_operation(ops):
    op = _request(ops)["id"]
    _confirm(ops, op)
    assert _code(_consume, ops, op, generation="d" * 64) == "stale_operation"
    assert _code(_consume, ops, op) == "stale_operation"  # spent: not usable afterwards


def test_another_binding_or_kind_is_a_mismatch_and_leaves_it_usable(ops):
    op = _request(ops)["id"]
    _confirm(ops, op)
    assert _code(_consume, ops, op, binding="e" * 64) == "host_operation_mismatch"
    assert _code(_consume, ops, op, kind="recover") == "host_operation_mismatch"
    _consume(ops, op)


# --- rate limits ----------------------------------------------------------------------


def test_one_open_operation_and_the_same_press_returns_it(ops):
    first = _request(ops)
    assert _request(ops)["id"] == first["id"]  # pressed again: the same operation
    assert _code(_request, ops, binding="f" * 64) == "host_rate_limited"
    ops.cancel(first["id"])
    assert _request(ops, binding="f" * 64)["id"] != first["id"]


def test_at_most_rate_max_requests_per_window(ops, clock):
    for i in range(RATE_MAX):
        op = _request(ops, binding=f"{i:064x}")["id"]
        ops.cancel(op)
    assert _code(_request, ops, binding="9" * 64) == "host_rate_limited"
    clock.tick(RATE_WINDOW_SEC)
    _request(ops, binding="9" * 64)


# --- only built-in values are evaluated (TOCTOU, review lesson) -----------------------


class Liar:
    """An object whose methods answer differently on every call (and count the calls)."""

    calls = 0

    def _count(self, *_):
        Liar.calls += 1
        return Liar.calls % 2 == 0

    __eq__ = _count
    __hash__ = object.__hash__

    def __str__(self):
        Liar.calls += 1
        return "a" * 32

    def __index__(self):
        Liar.calls += 1
        return 1


class SneakyStr(str):
    def __eq__(self, other):
        Liar.calls += 1
        return True

    __hash__ = str.__hash__


class SneakyDict(dict):
    def __getitem__(self, key):
        Liar.calls += 1
        return "changed"

    def items(self):
        Liar.calls += 1
        return [("x", "y")]


@pytest.mark.parametrize(
    "body",
    [
        SneakyDict(a=1),
        {"a": SneakyStr("x")},
        {"a": Liar()},
        {SneakyStr("k"): 1},
        {"a": float("nan")},
        {"a": (1, 2)},
        {"a": b"bytes"},
    ],
)
def test_the_binding_refuses_anything_but_built_in_json_values(body):
    Liar.calls = 0
    with pytest.raises(Unbindable):
        binding_digest("rules_loosen", "/api/hitl/x/decision", body)
    assert Liar.calls == 0  # nothing of the caller's ran


def test_ids_incarnations_and_decisions_are_checked_by_type_without_calling_them(ops):
    Liar.calls = 0
    op = _request(ops)["id"]
    for bad in (SneakyStr(op), Liar(), op.encode()):
        assert _code(_consume, ops, bad) == "host_operation_unknown"
        assert _code(ops.cancel, bad) == "host_operation_unknown"
        assert _code(_confirm, ops, bad) == "host_operation_unknown"
    for bad in (SneakyStr(ops.incarnation), Liar()):
        assert _code(_confirm, ops, op, incarnation=bad) == "host_operation_incarnation"
    assert _code(_confirm, ops, op, decision=SneakyStr("confirm")) == "host_operation_mismatch"
    assert (
        _code(
            ops.decide,
            op,
            incarnation=ops.incarnation,
            decision="confirm",
            principal=Liar(),
            channel="cli",
        )
        == "host_operation_mismatch"
    )
    assert ops.status(op)["state"] == "pending"
    _confirm(ops, op)
    for field in ("generation", "binding", "kind"):
        value = {"generation": G, "binding": B, "kind": "rules_loosen"}[field]
        assert _code(_consume, ops, op, **{field: SneakyStr(value)}) == "host_operation_mismatch"
    assert ops.status(op)["state"] == "confirmed"  # nothing of it was used
    assert Liar.calls == 0


def test_request_inputs_are_exact_types(ops):
    assert _code(_request, ops, binding=SneakyStr(B)) == "host_operation_mismatch"
    assert _code(_request, ops, kind="other") == "host_operation_mismatch"
    with pytest.raises(HostOpRefused):
        ops.request("rules_loosen", binding=B, generation=G, summary={"tool": "free text!"})
    with pytest.raises(HostOpRefused):
        ops.request("rules_loosen", binding=B, generation=G, summary={"note": "x"})
    with pytest.raises(HostOpRefused):
        ops.request("rules_loosen", binding=B, generation=G, summary=SneakyDict(tool="x"))


def test_digests_differ_for_different_requests_and_states():
    one = binding_digest("rules_loosen", "/api/hitl/a/decision", {"action": "approve"})
    assert one == binding_digest("rules_loosen", "/api/hitl/a/decision", {"action": "approve"})
    assert one != binding_digest("rules_loosen", "/api/hitl/b/decision", {"action": "approve"})
    assert one != binding_digest("recover", "/api/hitl/a/decision", {"action": "approve"})
    assert one != binding_digest("rules_loosen", "/api/hitl/a/decision", {"action": "edit"})
    assert target_digest({"a": {1, 2}}) == target_digest({"a": {2, 1}})
    assert target_digest({"a": (1,)}) != target_digest({"a": "(1,)"})


def test_every_code_has_a_status_and_every_kind_a_word():
    assert set(KINDS.values()) <= {"APPROVE", "RECOVER", "UNLATCH"}
    assert CODES["host_confirmation_required"] == 428
    assert CODES["host_rate_limited"] == 429
    assert CODES["host_channel_unavailable"] == 503

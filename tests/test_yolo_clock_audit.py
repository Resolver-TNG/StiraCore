"""#596: an end by the clocks (``revoked_clock_anomaly``) says why in its
``audit.yolo.ended`` row — which test fired (a fixed word), the deltas it compared and the
slack — so an abnormal end can be explained afterwards. Additive only: every other end's
row is unchanged, and the added values are fixed words and numbers (never text)."""

from __future__ import annotations

import math
import threading

import pytest

from stiracore.observability.masking import mask_secrets
from stiracore.yolo.audit_schema import CLOCK_CHECKS, build
from stiracore.yolo.requests import CLOCK_SLACK_SEC, SLEEP_SEC, WINDOW_SEC
from tests.test_yolo_window import _broken_clock, _open, _service, audit, clock, marker, yolo

__all__ = ["audit", "clock", "marker", "yolo"]  # the fixtures, re-used

CLOCK_KEYS = {"clock_check", "d_wall", "d_boot", "last_d_wall", "last_d_boot", "clock_slack_sec"}


def _ended(audit):
    rows = audit.of("ended")
    assert len(rows) == 1, rows
    return rows[0]


def _plain(row):
    """Every value as stored is unchanged by ``mask_secrets`` (no text that looks secret)."""
    for key in CLOCK_KEYS & set(row):
        value = row[key]
        assert type(value) in (str, int, float), (key, value)
        assert mask_secrets(str(value)) == str(value), key


def test_wall_clock_set_back_records_wall_backwards_with_the_deltas(yolo, audit, clock):
    _open(yolo)
    clock.tick(10)
    yolo.status()  # a check: ``last`` is now 10 s after the press
    clock.wall -= 3  # the wall clock goes back beyond the slack since that check
    yolo.status()
    ended = _ended(audit)
    assert ended["end_reason"] == "revoked_clock_anomaly" and ended["end_class"] == "abnormal"
    assert ended["clock_check"] == "wall_backwards"
    assert ended["last_d_wall"] == -3.0 and ended["last_d_boot"] == 0.0
    assert ended["d_wall"] == 7.0 and ended["d_boot"] == 10.0
    assert ended["clock_slack_sec"] == CLOCK_SLACK_SEC
    _plain(ended)


def test_wall_falling_behind_the_boot_clock_records_wall_behind_boot(yolo, audit, clock):
    _open(yolo)
    # The sleep-including and sleep-excluding clocks move 10 s, the wall clock does not:
    # no step back since the last check, but ΔW falls behind ΔB beyond the slack.
    clock.boot += 10
    clock.mono += 10
    yolo.status()
    ended = _ended(audit)
    assert ended["end_reason"] == "revoked_clock_anomaly"
    assert ended["clock_check"] == "wall_behind_boot"
    assert ended["d_wall"] == 0.0 and ended["d_boot"] == 10.0
    assert ended["last_d_wall"] == 0.0 and ended["last_d_boot"] == 10.0
    assert ended["clock_slack_sec"] == CLOCK_SLACK_SEC
    _plain(ended)


def test_unreadable_clocks_record_a_fixed_word_and_no_numbers(yolo, audit):
    _open(yolo)
    yolo._clock = _broken_clock  # raises OSError with a message: never in the row
    yolo.status()
    ended = _ended(audit)
    assert ended["end_reason"] == "revoked_clock_anomaly"
    assert ended["clock_check"] == "clock_unreadable"
    assert not ({"d_wall", "d_boot", "last_d_wall", "last_d_boot"} & set(ended))
    assert "clock_gettime" not in repr(ended)


def test_a_failing_watchdog_records_watchdog_failed(audit, clock, marker, monkeypatch):
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
    ended = _ended(audit)
    assert ended["end_reason"] == "revoked_clock_anomaly"
    assert ended["clock_check"] == "watchdog_failed"
    assert "ZeroDivision" not in repr(ended) and "division" not in repr(ended)


def _end_by(kind, yolo, clock):
    if kind == "expired_time":
        clock.tick(WINDOW_SEC)
        yolo.status()
    elif kind == "expired_sleep":
        clock.wall += SLEEP_SEC + 1
        clock.boot += SLEEP_SEC + 1
        yolo.tick()
    else:
        yolo.fence(kind)


@pytest.mark.parametrize("kind", ["expired_time", "expired_sleep", "stop", "new_session"])
def test_other_ends_carry_none_of_the_clock_keys(yolo, audit, clock, kind):
    _open(yolo)
    _end_by(kind, yolo, clock)
    ended = _ended(audit)
    assert ended["end_reason"] != "revoked_clock_anomaly"
    assert not (CLOCK_KEYS & set(ended)), ended
    # The row has exactly the keys it had before #596 (the common fields and the counts).
    assert set(ended) == {
        "schema",
        "operation_id",
        "phase",
        "actor_kind",
        "actor",
        "surface",
        "incarnation_ref",
        "key_id",
        "delta_class",
        "window_id",
        "generation_from",
        "generation_to",
        "authority_epoch",
        "end_reason",
        "end_class",
        "used",
        "reserved",
        "tool_calls",
        "turns",
    }


def test_an_audit_failure_end_carries_no_clock_keys(yolo, audit):
    _open(yolo)
    audit.healthy = False
    yolo.status()
    ended = _ended(audit)
    assert ended["end_reason"] == "revoked_audit_failed"
    assert not (CLOCK_KEYS & set(ended))


def _ended_row(**extra):
    common = {
        "schema": "audit/1",
        "operation_id": "a" * 32,
        "phase": "transition",
        "event_id": f"op:{'a' * 32}:transition",
        "actor_kind": "system",
        "actor": "system:yolo",
        "surface": "system",
        "incarnation_ref": "b" * 64,
        "key_id": "k1",
        "delta_class": "narrowing",
        "window_id": "c" * 32,
        "generation_from": 1,
        "generation_to": 2,
        "authority_epoch": 1,
        "end_reason": "revoked_clock_anomaly",
        "end_class": "abnormal",
        "used": 0,
    }
    return build("audit.yolo.ended", **common, **extra)["attributes"]


def test_the_schema_keeps_only_fixed_words_and_finite_numbers():
    good = _ended_row(
        clock_check="wall_backwards",
        d_wall=-1.5,
        d_boot=12.0,
        last_d_wall=-3.0,
        last_d_boot=0,
        clock_slack_sec=2.0,
    )
    assert good["clock_check"] == "wall_backwards" and good["last_d_wall"] == -3.0
    _plain(good)
    assert CLOCK_CHECKS == {
        "wall_backwards",
        "wall_behind_boot",
        "clock_unreadable",
        "watchdog_failed",
    }
    # Free text (here a secret-looking value), a bool, NaN / inf and a negative slack are
    # never stored as given: the optional field becomes the word ``invalid``.
    bad = _ended_row(
        clock_check="AKIAIOSFODNN7EXAMPLE",
        d_wall=True,
        d_boot=math.nan,
        last_d_wall=math.inf,
        last_d_boot="3",
        clock_slack_sec=-2.0,
    )
    assert all(bad[key] == "invalid" for key in CLOCK_KEYS), bad
    _plain(bad)

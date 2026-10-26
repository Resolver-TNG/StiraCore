"""#597: ``status()["request"]`` after the window it opened has ended.

An ``opened`` request is shown while its window is open (active or paused) — unchanged —
and is ``None`` once that window has ended (by any reason) or is gone, so a reader never
sees "an open request" for a window that is over. The other terminal words (``expired``,
``denied``, ``cancelled`` …) are already the request's actual state and stay as they are.
View only: the request × window state machine is not changed."""

from __future__ import annotations

import pytest

from stiracore.yolo.requests import EXPIRY_SEC, SLEEP_SEC, WINDOW_SEC
from tests.test_yolo_window import (
    _broken_clock,
    _confirm,
    _open,
    _service,
    audit,
    clock,
    marker,
    yolo,
)

__all__ = ["audit", "clock", "marker", "yolo"]  # the fixtures, re-used

_OPEN_REQUEST = ("pending", "issuing", "opened")


def _no_open_request(status):
    request = status["request"]
    assert request is None or request["state"] not in _OPEN_REQUEST, request


def test_while_the_window_is_open_the_request_is_unchanged(yolo):
    request_id = _open(yolo)
    status = yolo.status()
    request = status["request"]
    assert status["window"]["state"] == "active"
    assert request is not None and request["id"] == request_id
    # Exactly what the view gave before #597 (the request's own fields).
    with yolo._lock:
        assert request == yolo._view_locked(yolo._now())
    assert request["state"] == "opened" and request["expires_in"] == 0


def test_a_paused_window_keeps_its_request(audit, clock, marker):
    level = {"value": "normal"}
    yolo = _service(audit, clock, marker, breaker=lambda: level["value"])
    _open(yolo)
    level["value"] = "warning"
    status = yolo.status()
    assert status["window"]["state"] == "paused"
    assert status["request"]["state"] == "opened"


def _end(kind, yolo, clock):
    if kind == "stop":
        yolo.fence("stop")
    elif kind == "new_session":
        yolo.fence("new_session")
    elif kind == "expired_time":
        clock.tick(WINDOW_SEC)
    elif kind == "expired_sleep":
        clock.wall += SLEEP_SEC + 1
        clock.boot += SLEEP_SEC + 1
    elif kind == "clock_anomaly":
        clock.wall -= 3
    elif kind == "clock_unreadable":
        yolo._clock = _broken_clock


@pytest.mark.parametrize(
    "kind",
    ["stop", "new_session", "expired_time", "expired_sleep", "clock_anomaly", "clock_unreadable"],
)
def test_after_the_window_ended_no_open_request_is_returned(yolo, clock, kind):
    _open(yolo)
    _end(kind, yolo, clock)
    yolo.status()  # the look that ends it (clocks), or the one after
    status = yolo.status()
    window = status["window"]
    assert window is None or window["state"] == "ended", window
    assert status["request"] is None, status["request"]
    _no_open_request(status)


def test_a_pending_request_is_unchanged_and_an_expired_one_says_expired(yolo, clock):
    yolo.press("internal")
    pending = yolo.status()["request"]
    assert pending["state"] == "pending" and pending["expires_in"] == int(EXPIRY_SEC)
    clock.tick(EXPIRY_SEC)
    expired = yolo.status()["request"]
    assert expired["state"] == "expired" and expired["expires_in"] == 0
    _no_open_request(yolo.status())


def test_a_pending_request_never_shows_with_no_time_left(yolo, clock):
    """``expires_in <= 0`` is never a request still open: one second before the expiry it
    is pending with time left; at the expiry it is ``expired``."""
    yolo.press("internal")
    clock.tick(EXPIRY_SEC - 1)
    request = yolo.status()["request"]
    assert request["state"] == "pending" and request["expires_in"] >= 1
    clock.tick(1)
    request = yolo.status()["request"]
    assert request["state"] == "expired" and request["expires_in"] == 0


def test_a_confirmation_answer_still_carries_the_opened_request(yolo):
    """The confirm answer (not ``status``) is untouched: it names the opened request."""
    request_id = yolo.press("internal")["id"]
    view = _confirm(yolo, request_id)
    assert view["state"] == "opened" and view["id"] == request_id

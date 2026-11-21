"""#680 (ADR 0034): a chat turn that stops without a person's press.

1. The YOLO window ended: the turn stops at its next approval wait (the card is rejected,
   nothing approves it); while the window is open the card waits as before.
2. An approval card of the turn expired unanswered: the turn stops. A person's answer
   never stops it.
3. Upper bounds of a turn nobody reads: time since the start and tokens (fixed values).

Every stop is ``stop: "cancelled"`` with a fixed ``reason`` code. Clocks are injected.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.core import BreakLevel, HITLAction, HITLDecision, HITLRequest
from stiracore.observability import turn_autostop
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.turn_autostop import TurnAutoStop
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.observability.turns_view import page_event
from tests.test_chat_detached_turn import WAIT, _completed, _open_stream, _until


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


class CardRunner:
    """Yields ``first``, waits for ``go``, raises one approval card, then yields ``after``.

    ``before_card``: called on the turn's worker just before the card (an event between)."""

    def __init__(self, broker, before_card=None):
        self.broker = broker
        self.go = threading.Event()
        self.decision = None
        self.before_card = before_card

    def __call__(self, _message):
        yield "first "
        assert self.go.wait(WAIT)
        if self.before_card is not None:
            self.before_card()
        self.decision = self.broker.request(
            HITLRequest(
                tool_name="file_write",
                tool_input={"path": "a.txt", "content": "x"},
                break_level=BreakLevel.NORMAL,
                reason="rule_ask",
                tool_use_id="tu-1",
                agent_id="agent",
            )
        )
        yield "after"


def _app(runner, recorder, broker=None, window=None, clock=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=runner,
            turn_recorder=recorder,
            hitl_broker=broker,
            yolo_window_id=window,
            turn_clock=clock,
        )
    )
    return app


def _broker(recorder, clock=None):
    return HITLApprovalBroker(
        on_event=recorder.emit,
        turn_id_provider=recorder.active.current,
        ttl_sec=30.0,
        now_fn=clock,
    )


def _end(recorder):
    done = _until(lambda: _completed(recorder), what="turn.completed")
    events = recorder.store.events(done["turn_id"])
    return done, events[-1]["data"]


def _history(app):
    return app.test_client().get("/api/chat/history").get_json()["turns"][-1]


# --- 1. the YOLO window ends: the next approval wait stops the turn -------------------------


def test_after_the_window_ended_the_next_approval_wait_stops_the_turn(recorder):
    broker = _broker(recorder)
    window = {"id": "w-1"}
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: window["id"])
    response, _, _ = _open_stream(app.test_client())
    window["id"] = None  # the window ended while the turn ran
    runner.go.set()
    done, data = _end(recorder)
    assert (done["stop"], data["reason"]) == ("cancelled", "yolo_window_ended")
    assert runner.decision.action == HITLAction.REJECT  # nothing approved the card
    assert not broker.pending()
    history = _history(app)
    assert (history["stop"], history["stop_reason"]) == ("cancelled", "yolo_window_ended")
    # The card was rejected by the server, not recorded as a person's rejection.
    (row,) = [item for item in broker.history() if item["tool_use_id"] == "tu-1"]
    assert row["resolver"] == "system"
    assert row["resolution_reason_code"] == "turn_auto_stopped"
    response.close()


def test_while_the_window_is_open_the_card_waits_for_a_person(recorder):
    broker = _broker(recorder)
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: "w-1")
    response, _, _ = _open_stream(app.test_client())
    runner.go.set()
    (card,) = _until(broker.pending, what="the card")
    assert broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
    done, data = _end(recorder)
    assert (done["stop"], "reason" in data) == ("completed", False)
    assert "stop_reason" not in _history(app)
    response.close()


def _model_completed(recorder, n, tokens):
    recorder.emit(
        "model.completed",
        {
            "call_id": f"{n:032x}",
            "duration_ms": 1,
            "usage": {"input_tokens": tokens, "output_tokens": 1},
        },
    )


def test_another_window_is_not_the_one_the_turn_ran_under(recorder):
    # W1 ends and W2 opens; an event of the turn in between never moves it onto W2.
    broker = _broker(recorder)
    window = {"id": "w-1"}
    runner = CardRunner(broker, before_card=lambda: _model_completed(recorder, 1, 10))
    app = _app(runner, recorder, broker, window=lambda: window["id"])
    response, _, _ = _open_stream(app.test_client())
    window["id"] = "w-2"
    runner.go.set()
    _done, data = _end(recorder)
    assert data["reason"] == "yolo_window_ended"
    assert runner.decision.action == HITLAction.REJECT
    response.close()


def test_a_turn_without_a_window_is_not_stopped_by_the_window_rule(recorder):
    broker = _broker(recorder)
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: None)
    response, _, _ = _open_stream(app.test_client())
    runner.go.set()
    (card,) = _until(broker.pending, what="the card")
    assert broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
    done, _data = _end(recorder)
    assert done["stop"] == "completed"
    response.close()


# --- 2. an approval wait nobody answers ------------------------------------------------------


def test_an_unanswered_card_stops_its_turn_at_its_expiry(recorder):
    clock = Clock()
    broker = _broker(recorder, clock)
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: None)
    response, _, _ = _open_stream(app.test_client())
    response.close()  # nobody reads the turn any more
    runner.go.set()
    _until(broker.pending, what="the card")
    clock.now += 29.0
    assert broker.pending() and not _completed(recorder)  # still waiting before the expiry
    clock.now += 2.0
    done, data = _end(recorder)
    assert (done["stop"], data["reason"]) == ("cancelled", "approval_unanswered")
    assert runner.decision.action == HITLAction.REJECT
    assert _history(app)["stop_reason"] == "approval_unanswered"


def test_an_unanswered_card_stops_a_turn_that_is_still_read(recorder):
    # The approval rule holds whether the page reads the turn or not (ADR 0034 §2).
    clock = Clock()
    broker = _broker(recorder, clock)
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: None)
    response, _, _ = _open_stream(app.test_client())
    runner.go.set()
    _until(broker.pending, what="the card")
    clock.now += 31.0
    done, data = _end(recorder)
    assert (done["stop"], data["reason"]) == ("cancelled", "approval_unanswered")
    response.close()


def test_a_person_answering_the_card_never_stops_the_turn(recorder):
    clock = Clock()
    broker = _broker(recorder, clock)
    runner = CardRunner(broker)
    app = _app(runner, recorder, broker, window=lambda: None)
    response, _, _ = _open_stream(app.test_client())
    runner.go.set()
    (card,) = _until(broker.pending, what="the card")
    clock.now += 20.0
    assert broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
    done, data = _end(recorder)
    assert (done["stop"], "reason" in data) == ("completed", False)
    response.close()


# --- 3. upper bounds of a turn nobody reads --------------------------------------------------


class Hold:
    def __init__(self):
        self.go = threading.Event()

    def __call__(self, _message):
        yield "first "
        assert self.go.wait(WAIT)
        yield "second"
        yield "never"


def test_an_unread_turn_stops_at_the_time_limit(recorder):
    clock = Clock()
    runner = Hold()
    app = _app(runner, recorder, clock=clock)
    response, _, _ = _open_stream(app.test_client())
    response.close()
    clock.now += turn_autostop.UNATTENDED_TURN_MAX_SEC
    runner.go.set()
    done, data = _end(recorder)
    assert (done["stop"], data["reason"]) == ("cancelled", "unattended_time_limit")
    end = recorder.store.events(done["turn_id"])[-1]
    assert page_event(1, end)["message"] == "cancelled: unattended_time_limit"


def test_a_read_turn_has_no_time_limit(recorder):
    clock = Clock()
    runner = Hold()
    app = _app(runner, recorder, clock=clock)
    response, iterator, _ = _open_stream(app.test_client())
    clock.now += 10 * turn_autostop.UNATTENDED_TURN_MAX_SEC
    runner.go.set()
    list(iterator)
    done, _data = _end(recorder)
    assert done["stop"] == "completed"
    response.close()


class ModelCalls:
    """Model calls reported only as ``model.completed`` events (the SDK's chunks carry
    the turn's usage only at its end): 600,000 input tokens each."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.go = threading.Event()
        self.calls = 0

    def __call__(self, _message):
        yield "first "
        assert self.go.wait(WAIT)
        for n in range(10):
            self.calls += 1
            _model_completed(self.recorder, n + 1, 600_000)
            yield f"part{n} "


def test_an_unread_turn_stops_at_the_token_limit_of_its_model_calls(recorder):
    runner = ModelCalls(recorder)
    app = _app(runner, recorder, clock=Clock())
    response, _, _ = _open_stream(app.test_client())
    response.close()
    runner.go.set()
    done, data = _end(recorder)
    assert (done["stop"], data["reason"]) == ("cancelled", "unattended_token_limit")
    assert runner.calls == 4  # 4 x 600,001 >= 2,000,000: stopped after the fourth call


def test_a_read_turn_has_no_token_limit(recorder):
    runner = ModelCalls(recorder)
    app = _app(runner, recorder, clock=Clock())
    response, iterator, _ = _open_stream(app.test_client())
    runner.go.set()
    list(iterator)
    done, _data = _end(recorder)
    assert done["stop"] == "completed" and runner.calls == 10
    response.close()


def test_the_rules_alone():
    clock, window = Clock(), {"id": "w"}
    rules = TurnAutoStop(window_id=lambda: window["id"], now=clock)
    assert rules.card_raised("r1") is None
    assert rules.card_resolved("r1", "rejected", "operator") is None
    assert rules.card_resolved("other", "expired", "system") is None  # not this turn's card
    assert rules.card_resolved("r1", "expired", "system") == "approval_unanswered"
    max_tokens = turn_autostop.UNATTENDED_TURN_MAX_TOKENS
    usage = {"input_tokens": max_tokens - 1, "output_tokens": 1}
    assert rules.limits(attended=True, usage=usage) is None
    assert rules.limits(attended=False, usage=usage) == "unattended_token_limit"
    assert rules.limits(attended=False, usage={"input_tokens": None}) is None
    window["id"] = None
    assert rules.card_raised("r2") == "yolo_window_ended"

    def broken():
        raise RuntimeError("unreadable")

    seen = TurnAutoStop(window_id=lambda: "w", now=clock)
    seen._reader = broken  # unreadable after a window was seen: stops at the wait
    assert seen.card_raised("r") == "yolo_window_ended"
    none = TurnAutoStop(window_id=broken, now=clock)
    assert none.card_raised("r") is None  # never saw a window
    late = TurnAutoStop(window_id=lambda: window["id"], now=clock)
    window["id"] = "w-late"
    late.note_window()  # a window opened during the turn counts too
    window["id"] = None
    assert late.card_raised("r") == "yolo_window_ended"
    window["id"] = "w-1"
    first = TurnAutoStop(window_id=lambda: window["id"], now=clock)
    window["id"] = "w-2"
    first.note_window()  # another window never takes the first one's place
    assert first.card_raised("r") == "yolo_window_ended"
    counted = TurnAutoStop(now=clock)
    for _ in range(3):
        counted.note_model_completed({"usage": {"input_tokens": 700_000, "output_tokens": True}})
    counted.note_model_completed({"usage": "x"})
    assert counted.limits(attended=True, usage=None) is None
    assert counted.limits(attended=False, usage=None) == "unattended_token_limit"

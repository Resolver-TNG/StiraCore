"""#651: /observability's Cost Tracker prices a turn with ``token_prices``, as the chat page does.

The page adds up ``estimated_cost_usd`` of the events it reads; without it (and with no
rates of its own) every turn counted as unpriced: ``$0.00 *`` next to the chat page's
estimate. The events now carry the cost of the turn, from the same price table and the same
sum as the board's totals (``activity.turn_cost``); a turn without a price carries none.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability import activity, chat_api, turns_view
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.observability.turns_view import create_turns_observability_blueprint, page_event
from stiracore.providers.usage import Pricing

SONNET = "global.anthropic.claude-sonnet-5"
OTHER = "global.anthropic.claude-haiku-4-5"


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


@pytest.fixture
def no_window(monkeypatch):
    monkeypatch.setattr(chat_api.yolo_runtime, "status", lambda: {"window": None})


def _app(recorder, prices):
    def runner(_message):
        yield "ok"

    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(model_id=SONNET),
            runner=runner,
            turn_recorder=recorder,
            token_prices=prices,
        )
    )
    app.register_blueprint(create_turns_observability_blueprint(recorder.store))
    return app


def _completed(store, turn, model, **usage):
    store.append(turn, "turn.completed", {"usage": usage, "model": model})


def _tracker(client):
    """What the page's Cost Tracker adds up: (priced sum, unpriced turns) of the turn ends."""
    events = client.get("/api/observability/snapshot").get_json()["events"]
    ends = [e for e in events if e["type"] == "turn_end" and "usage" in e]
    costs = [e.get("estimated_cost_usd") for e in ends]
    return sum(c for c in costs if c is not None), sum(c is None for c in costs)


def _chat(client):
    return client.get("/api/gateway/status").get_json()["session"]["estimated_cost"]


def test_cost_tracker_agrees_with_the_chat_page_after_a_turn(recorder, no_window):
    prices = {SONNET: Pricing(3.0, 15.0, 0.3, 3.75)}
    client = _app(recorder, prices).test_client()
    _completed(recorder.store, "t1", SONNET, input_tokens=12_000, output_tokens=600)
    _completed(
        recorder.store, "t2", SONNET, input_tokens=100, output_tokens=10, cache_read_tokens=1000
    )
    expected = (12_100 * 3.0 + 610 * 15.0 + 1000 * 0.3) / 1_000_000
    assert _chat(client) == pytest.approx(expected)
    total, unpriced = _tracker(client)
    assert unpriced == 0, "rates are configured: no turn is marked unpriced"
    assert total == pytest.approx(expected)
    assert client.get("/api/activity").get_json()["totals"]["cost"] == pytest.approx(expected)


def test_without_prices_the_events_carry_no_cost(recorder, no_window):
    client = _app(recorder, {}).test_client()
    _completed(recorder.store, "t1", SONNET, input_tokens=100, output_tokens=10)
    assert _chat(client) is None
    assert _tracker(client) == (0, 1)  # unpriced, never free


def test_a_turn_without_a_price_stays_unpriced(recorder, no_window):
    # another model, and the cache without a cache price: neither is shown as free
    client = _app(recorder, {SONNET: Pricing(3.0, 15.0)}).test_client()
    _completed(recorder.store, "t1", SONNET, input_tokens=1_000_000, output_tokens=0)
    _completed(recorder.store, "t2", OTHER, input_tokens=10, output_tokens=1)
    _completed(recorder.store, "t3", SONNET, input_tokens=10, output_tokens=1, cache_read_tokens=5)
    total, unpriced = _tracker(client)
    assert total == pytest.approx(3.0) and unpriced == 2
    board = client.get("/api/activity").get_json()["totals"]
    assert board["cost"] == pytest.approx(total) and board["cost_partial"] is True


def test_only_a_number_is_added_to_the_event():
    prices = {SONNET: Pricing(3.0, 15.0)}
    data = {"usage": {"input_tokens": 1000, "output_tokens": 100}, "model": SONNET}
    event = {"event_id": "e1", "turn_id": "t1", "seq": 1, "ts": 1.0, "type": "turn.completed"}
    plain = page_event(1, {**event, "data": data})
    priced = page_event(1, {**event, "data": data}, prices)
    assert "estimated_cost_usd" not in plain
    assert set(priced) - set(plain) == {"estimated_cost_usd"}
    assert priced["estimated_cost_usd"] == pytest.approx(0.0045)
    assert priced["estimated_cost_usd"] == activity.turn_cost(data, prices)
    assert "pricing" not in priced and "3.0" not in json.dumps(priced)  # the rates stay inside
    # not a price table (or an entry that is not a Pricing): no cost, no error
    for bad in ("x", {SONNET: {"input": 3.0, "output": 15.0}}, {SONNET: "free"}):
        assert "estimated_cost_usd" not in page_event(1, {**event, "data": data}, bad)
    # other events never carry a cost
    started = page_event(2, {**event, "type": "turn.started", "data": data}, prices)
    assert "estimated_cost_usd" not in started


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        (0, True),
        (0.0045, True),
        (turns_view._MAX_COST, True),  # exactly the limit
        (math.nextafter(turns_view._MAX_COST, math.inf), False),
        (-0.01, False),
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
        (10**400, False),  # an int no float can hold
        (True, False),
        ("0.0045", False),
        (None, False),
    ],
)
def test_only_a_finite_cost_in_range_leaves(monkeypatch, value, kept):
    assert (turns_view._number(value, turns_view._MAX_COST) is not None) is kept
    monkeypatch.setattr(turns_view, "turn_cost", lambda _data, _prices: value)
    data = {"usage": {"input_tokens": 1000, "output_tokens": 100}, "model": SONNET}
    event = {"event_id": "e1", "turn_id": "t1", "seq": 1, "ts": 1.0, "type": "turn.completed"}
    out = page_event(1, {**event, "data": data}, {SONNET: Pricing(3.0, 15.0)})
    if kept:
        assert out["estimated_cost_usd"] == value
    else:
        assert "estimated_cost_usd" not in out  # unpriced on the page, never free
    assert out["usage"] == {"is_final": True, "input_tokens": 1000, "output_tokens": 100}
    json.dumps(out, allow_nan=False)  # what the stream does with it


def test_the_stream_carries_the_cost_too(recorder, no_window):
    client = _app(recorder, {SONNET: Pricing(3.0, 15.0)}).test_client()
    _completed(recorder.store, "t1", SONNET, input_tokens=1000, output_tokens=100)
    response = client.get("/api/observability/events")
    chunks = iter(response.response)
    line = ""
    for _ in range(4):
        line = next(chunks)
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("id:"):
            break
    response.close()
    payload = json.loads(line.split("data: ", 1)[1])
    assert payload["estimated_cost_usd"] == pytest.approx(0.0045)


def test_the_chat_server_prices_the_observability_events(tmp_path, monkeypatch, no_window):
    """End to end on the shipped server: ``gateway-config.json`` -> both pages."""
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "gateway-config.json").write_text(
        json.dumps({"token_prices": {SONNET: {"input": 3.0, "output": 15.0}}}), "utf-8"
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(
        nxp_agent,
        "Agent",
        Mock(
            side_effect=lambda **kw: Mock(
                return_value="Hello",
                messages=list(kw.get("messages") or []),
                system_prompt=kw.get("system_prompt", ""),
            )
        ),
    )
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    app = stira_server.create_app()
    client = app.test_client()
    store = TurnEventStore(data_dir / "turns.db")
    try:
        _completed(store, "t1", SONNET, input_tokens=12_000, output_tokens=600)
    finally:
        store.close()
    total, unpriced = _tracker(client)
    assert unpriced == 0
    assert total == pytest.approx(0.045)
    assert _chat(client) == pytest.approx(total)

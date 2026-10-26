"""#451: token unit prices per model (``gateway-config.json`` ``token_prices``).

Stira ships no price data: without an entry the board keeps 「単価が未設定」 and the chat
page's estimated cost stays unknown. With an entry each turn is priced by the model named
in its ``turn.completed``; a turn whose model has no price (or that used the cache without
a cache price) is never counted as free — the board then shows a partial sum, said so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.config.models import GatewayConfig
from stiracore.observability import activity, chat_api
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.providers.token_prices import parse_token_prices
from stiracore.providers.usage import Pricing

SONNET = "global.anthropic.claude-sonnet-5"
OTHER = "global.anthropic.claude-haiku-4-5"
ENTRY = {"input": 3, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}


# --- the section ----------------------------------------------------------------------------


def test_no_section_no_price_and_nothing_is_shipped():
    assert parse_token_prices(None) == ({}, ())
    assert parse_token_prices({}) == ({}, ())
    assert GatewayConfig().token_prices == {}
    assert "token_prices" not in GatewayConfig().as_dict()  # files stay as they were


def test_entries_are_read_leniently_with_codes_only():
    prices, warnings = parse_token_prices(
        {
            SONNET: ENTRY,
            "input-only": {"input": 1.0},
            "negative": {"input": -1, "output": 1},
            "bool": {"input": True, "output": 1},
            "text": {"input": "3", "output": 1},
            "nan": {"input": float("nan"), "output": 1},
            "": {"input": 1, "output": 1},
            "notobj": [1, 2],
            OTHER: {"input": 1, "output": 5, "extra": 9},
        }
    )
    assert set(prices) == {SONNET, OTHER}
    assert prices[SONNET] == Pricing(3.0, 15.0, 0.3, 3.75)
    assert prices[OTHER] == Pricing(1.0, 5.0)  # no cache price: cache turns stay unpriced
    assert set(warnings) == {"entry_invalid", "model_invalid", "unknown_field"}
    assert "3" not in json.dumps(warnings)
    assert parse_token_prices("x") == ({}, ("section",))


def test_the_section_round_trips_through_the_file(tmp_path):
    path = tmp_path / "gateway-config.json"
    path.write_text(json.dumps({"token_prices": {SONNET: ENTRY}}), "utf-8")
    loaded = GatewayConfig.load(path)
    assert loaded.token_prices == {SONNET: ENTRY}
    loaded.save()  # a settings-page save carries it over (as_dict)
    assert json.loads(path.read_text("utf-8"))["token_prices"] == {SONNET: ENTRY}
    path.write_text(json.dumps({"token_prices": "not an object"}), "utf-8")
    assert GatewayConfig.load(path).token_prices == "not an object"  # kept, then reported
    assert parse_token_prices(GatewayConfig.load(path).token_prices)[1] == ("section",)


# --- the totals ------------------------------------------------------------------------------


def _turn(model, inp, out, read=0, write=0):
    usage = {"input_tokens": inp, "output_tokens": out}
    if read:
        usage["cache_read_tokens"] = read
    if write:
        usage["cache_write_tokens"] = write
    data = {"usage": usage}
    if model is not None:
        data["model"] = model
    return data


def test_each_turn_is_priced_by_its_own_model():
    prices = {SONNET: Pricing(3.0, 15.0, 0.3, 3.75), OTHER: Pricing(1.0, 5.0)}
    done = [_turn(SONNET, 1_000_000, 100_000, read=1_000_000), _turn(OTHER, 1_000_000, 0)]
    totals = activity.totals_from(done, prices, model=SONNET)
    assert totals["cost"] == pytest.approx(3.0 + 1.5 + 0.3 + 1.0)
    assert "cost_partial" not in totals
    assert (totals["turns"], totals["input"], totals["cache_read"]) == (2, 2_000_000, 1_000_000)


def test_a_turn_without_a_price_is_never_free():
    prices = {SONNET: Pricing(3.0, 15.0)}
    # another model; the cache without a cache price; no model recorded
    unpriced = [_turn(OTHER, 10, 1), _turn(SONNET, 10, 1, read=5), _turn(None, 10, 1)]
    for turn in unpriced:
        assert activity.totals_from([turn], prices, model=SONNET)["cost"] is None
    totals = activity.totals_from([_turn(SONNET, 1_000_000, 0), *unpriced], prices)
    assert totals["cost"] == pytest.approx(3.0) and totals["cost_partial"] is True


def test_no_turn_yet_shows_zero_only_for_a_priced_model():
    prices = {SONNET: Pricing(3.0, 15.0)}
    assert activity.totals_from([], prices, model=SONNET)["cost"] == 0.0
    assert activity.totals_from([], prices, model=OTHER)["cost"] is None
    # a turn with no tokens at all (nothing reported) changes nothing
    assert activity.totals_from([{"usage": {}}], prices, model=SONNET)["cost"] == 0.0


def test_without_prices_the_totals_are_as_before():
    done = [_turn(SONNET, 100, 10)]
    assert activity.totals_from(done)["cost"] is None
    assert activity.totals_from(done, {})["cost"] is None
    assert activity.totals_from(done, (3.0, 15.0))["cost"] == pytest.approx(0.00045)


# --- the routes ------------------------------------------------------------------------------


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


def _app(recorder, prices, model=SONNET):
    def runner(_message):
        yield "ok"

    agent = SimpleNamespace(model_id=model)
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(agent, runner=runner, turn_recorder=recorder, token_prices=prices)
    )
    return app


def _completed(recorder, model, inp, out, turn):
    recorder.store.append(
        turn, "turn.completed", {"usage": _turn(model, inp, out)["usage"], "model": model}
    )


@pytest.fixture
def no_window(monkeypatch):
    monkeypatch.setattr(chat_api.yolo_runtime, "status", lambda: {"window": None})


def test_board_and_chat_page_show_the_cost_only_when_set(recorder, no_window):
    unset = _app(recorder, {}).test_client()
    assert unset.get("/api/activity").get_json()["totals"]["cost"] is None
    assert unset.get("/api/gateway/status").get_json()["session"]["estimated_cost"] is None
    prices = {SONNET: Pricing(3.0, 15.0)}
    client = _app(recorder, prices).test_client()
    assert client.get("/api/activity").get_json()["totals"]["cost"] == 0.0
    _completed(recorder, SONNET, 1_000_000, 100_000, "t1")
    board = client.get("/api/activity").get_json()["totals"]
    assert board["cost"] == pytest.approx(4.5) and "cost_partial" not in board
    assert client.get("/api/gateway/status").get_json()["session"][
        "estimated_cost"
    ] == pytest.approx(4.5)
    _completed(recorder, OTHER, 1_000_000, 0, "t2")
    board = client.get("/api/activity").get_json()["totals"]
    assert board["cost"] == pytest.approx(4.5) and board["cost_partial"] is True
    # the chat page's single number never shows a partial sum as the cost
    assert client.get("/api/gateway/status").get_json()["session"]["estimated_cost"] is None

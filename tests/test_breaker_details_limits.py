"""#471 review round 3 (M-1): the details that had no upper limit get one.

``bc_window_size``, ``sycophancy_window_size``, ``critical_cooldown_turns`` and
``hitl_max_edit_rounds`` were open-ended: a huge value (``10**9``) practically stopped the
coherence and sycophancy detection, kept Critical forever, or let an approval be re-edited
without end, and it went through without any question. The hub now refuses a value above
``settings_hub.DETAIL_LIMITS`` (400, ``out_of_range`` with ``max``), on save and on
rollback. A value already in the file above the limit is not re-checked: only the fields a
request changes are, so an unrelated save still works (the #492 rule).
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.core import StiraConfig
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import NeuralState
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint

LIMITS = {
    "bc_window_size": 200,
    "sycophancy_window_size": 200,
    "critical_cooldown_turns": 50,
    "hitl_max_edit_rounds": 10,
}
HUGE = 10**9


@pytest.fixture
def store(tmp_path):
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def client(store):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _save(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def _overrides(store) -> dict:
    return json.loads(store.agent_path.read_text(encoding="utf-8"))["stira_config_overrides"]


def _errors(response) -> dict:
    return {e["field"]: e for e in response.get_json()["errors"]}


def _seed(store, **overrides):
    store.agent_path.write_text(json.dumps({"stira_config_overrides": overrides}), encoding="utf-8")


# --- the limits ---------------------------------------------------------------------------


def test_the_open_ended_details_have_a_limit_published_for_the_page(client):
    rules = client.get("/api/config/hub").get_json()["constraints"]["breaker_details"]
    assert {name: rules[name]["max"] for name in LIMITS} == LIMITS
    assert hub.DETAIL_LIMITS == LIMITS
    # The defaults sit well inside them.
    config = StiraConfig()
    assert all(getattr(config, name) < limit for name, limit in LIMITS.items())


@pytest.mark.parametrize("field", sorted(LIMITS))
@pytest.mark.parametrize("over", [1, HUGE])
def test_a_value_above_the_limit_is_refused(client, store, field, over):
    value = LIMITS[field] + over if over == 1 else over
    response = _save(client, {"breaker_details": {field: value}})
    assert response.status_code == 400, response.get_json()
    error = _errors(response)[f"breaker_details.{field}"]
    assert error["code"] == "out_of_range"
    assert error["params"]["max"] == LIMITS[field]
    assert not store.agent_path.exists()
    # The validate endpoint (the page's pre-check) says the same.
    version = client.get("/api/config/hub").get_json()["version"]
    checked = client.post(
        "/api/config/hub/validate",
        json={"changes": {"breaker_details": {field: value}}, "base_version": version},
    ).get_json()
    assert checked["ok"] is False
    assert checked["errors"][0]["field"] == f"breaker_details.{field}"


@pytest.mark.parametrize("field", sorted(LIMITS))
def test_the_limit_itself_saves(client, store, field):
    response = _save(client, {"breaker_details": {field: LIMITS[field]}})
    assert response.status_code == 200, response.get_json()
    assert _overrides(store)[field] == LIMITS[field]


# --- a file that already holds a value above the limit ------------------------------------


def test_an_unrelated_save_is_not_refused_for_a_saved_value_above_the_limit(client, store):
    _seed(store, **dict.fromkeys(LIMITS, HUGE))
    state = client.get("/api/config/hub").get_json()
    assert state["values"]["breaker_details"]["bc_window_size"] == HUGE
    # The page sends the whole details group; only the moved field is new.
    details = {**state["values"]["breaker_details"], "hitl_timeout_sec": 60.0}
    response = _save(client, {"breaker_details": details, "breaker": {"bc_threshold": 0.55}})
    assert response.status_code == 200, response.get_json()
    saved = _overrides(store)
    assert saved["hitl_timeout_sec"] == 60.0
    assert saved["bc_threshold"] == 0.55
    # Kept as it was: not silently lowered, not refused.
    assert all(saved[name] == HUGE for name in LIMITS)
    other = _save(client, {"classifier": {"choice": "rule"}})
    assert other.status_code == 200, other.get_json()


def test_moving_a_saved_value_above_the_limit_needs_a_value_inside_it(client, store):
    _seed(store, bc_window_size=HUGE)
    still_over = _save(client, {"breaker_details": {"bc_window_size": HUGE - 1}})
    assert still_over.status_code == 400
    assert _errors(still_over)["breaker_details.bc_window_size"]["params"]["max"] == 200
    inside = _save(client, {"breaker_details": {"bc_window_size": 200}})
    assert inside.status_code == 200, inside.get_json()
    assert _overrides(store)["bc_window_size"] == 200


# --- rollback ------------------------------------------------------------------------------


def _rollback(client, entry_id):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(f"/api/config/history/{entry_id}/rollback", json={"base_version": version})


def test_rolling_back_to_a_value_above_the_limit_is_refused(client, store):
    _seed(store, critical_cooldown_turns=HUGE)
    lowered = _save(client, {"breaker_details": {"critical_cooldown_turns": 10}})
    assert lowered.status_code == 200, lowered.get_json()
    refused = _rollback(client, lowered.get_json()["history_id"])
    assert refused.status_code == 400, refused.get_json()
    error = _errors(refused)["breaker_details.critical_cooldown_turns"]
    assert (error["code"], error["params"]["max"]) == ("out_of_range", 50)
    assert _overrides(store)["critical_cooldown_turns"] == 10


def test_an_unrelated_rollback_is_not_refused_for_a_saved_value_above_the_limit(client, store):
    _seed(store, hitl_max_edit_rounds=HUGE)
    moved = _save(client, {"breaker": {"bc_threshold": 0.55}})
    assert moved.status_code == 200, moved.get_json()
    back = _rollback(client, moved.get_json()["history_id"])
    assert back.status_code == 200, back.get_json()
    saved = _overrides(store)
    assert saved["hitl_max_edit_rounds"] == HUGE
    assert saved.get("bc_threshold", StiraConfig().bc_threshold) == StiraConfig().bc_threshold


# --- why these numbers: measured on the engine --------------------------------------------


def _bc_turns_to_alert(window: int, calm_turns: int) -> int:
    """Turns of stability swinging 0.3 <-> 0.8 until ``bc_score`` falls under the default
    ``bc_threshold`` (0.6), after ``calm_turns`` steady turns at 0.8."""
    state = NeuralState()
    for _ in range(calm_turns):
        state.stability = 0.8
        state.update_bc_window(window_size=window)
    for turn in range(1, 100_000):
        state.stability = 0.3 if turn % 2 else 0.8
        if state.update_bc_window(window_size=window) < StiraConfig().bc_threshold:
            return turn
    raise AssertionError("never alerted")


def _sycophancy_turns_to_alert(window: int) -> int:
    """Agreeing answers in a row, after a balanced window, until the default threshold."""
    engine = StiraEngine(config=StiraConfig(sycophancy_window_size=window))
    logging.disable(logging.WARNING)
    try:
        for i in range(window):
            engine.record_agreement(i % 2 == 0)
        for turn in range(1, 100_000):
            score = engine.record_agreement(True)
            if score is not None and score > engine.config.sycophancy_threshold:
                return turn
    finally:
        logging.disable(logging.NOTSET)
    raise AssertionError("never alerted")


def test_the_window_limits_keep_the_detection_delay_bounded():
    """A window caps how far back the score looks. With no cap (``10**9``) the whole history
    is the window: the longer the calm, the later a swing is seen, without end. At the
    limit (200, ten times the default) a swing is seen within 11 turns whatever came
    before, and the sycophancy alert within 82 agreeing answers."""
    limit = LIMITS["bc_window_size"]
    assert _bc_turns_to_alert(20, 20) == 1
    assert _bc_turns_to_alert(limit, limit) == _bc_turns_to_alert(limit, 5 * limit) == 11
    assert _bc_turns_to_alert(HUGE, 5 * limit) >= 5 * _bc_turns_to_alert(HUGE, limit)
    # The stored window grows with the cap: one sample per turn, saved with the state.
    state = NeuralState()
    for _ in range(3 * limit):
        state.update_bc_window(window_size=HUGE)
    assert len(state.bc_window) == 3 * limit
    assert _sycophancy_turns_to_alert(20) == 10
    assert _sycophancy_turns_to_alert(LIMITS["sycophancy_window_size"]) == 82

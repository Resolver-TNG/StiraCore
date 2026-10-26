"""Review fixes of #471 (PR #491): a details change that switches the sycophancy detection
practically off goes through the existing typed confirmation; the server-fixed values say
what the chat server uses when no engine is known; the details' applied check survives a
running engine that misses a field or stores an int for a float; "reset to defaults" and the
``*_clamped`` rewrite are pinned.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import stira_delivery
from stiracore.core import StiraConfig
from stiracore.core.engine import StiraEngine
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint


@pytest.fixture
def store(tmp_path):
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


def _client(store, **kwargs):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client()


@pytest.fixture
def client(store):
    return _client(store)


def _save(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def _overrides(store) -> dict:
    return json.loads(store.agent_path.read_text(encoding="utf-8"))["stira_config_overrides"]


def _runtime(**engine):
    return {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "engine": engine,
    }


# --- M-1: practically switching the sycophancy detection off needs the confirmation -----


def _scores(window: int, threshold: float, answers: list[bool]) -> list[float | None]:
    engine = StiraEngine(
        config=StiraConfig(sycophancy_window_size=window, sycophancy_threshold=threshold)
    )
    logging.disable(logging.WARNING)
    try:
        return [engine.record_agreement(agreed) for agreed in answers]
    finally:
        logging.disable(logging.NOTSET)


def test_the_danger_range_is_where_the_engine_stops_alerting():
    """The range comes from the engine, not a guess: it scores only with 10 answers in its
    window and alerts only *above* the threshold."""
    rule = hub.SYCOPHANCY_DANGER
    # A window below 10 never gets a score, so it never alerts (everything agreeing).
    assert _scores(rule["window_below"] - 1, 0.7, [True] * 40)[-1] is None
    assert _scores(rule["window_below"], 0.7, [True] * 40)[-1] == 1.0
    # 1.0 never alerts; 0.95 (default window 20) alerts only when all 20 agree.
    assert not _scores(20, 1.0, [True] * 40)[-1] > 1.0
    nineteen = _scores(20, rule["threshold_at_least"], [i % 20 != 0 for i in range(40)])[-1]
    assert nineteen == 0.95 and not nineteen > rule["threshold_at_least"]
    # Just under the line, 19 of 20 still alerts: that stays a normal setting.
    assert nineteen > 0.94


@pytest.mark.parametrize(
    "details",
    [
        {"sycophancy_threshold": 1.0},
        {"sycophancy_threshold": 0.95},
        {"sycophancy_window_size": 9},
        {"sycophancy_window_size": 1},
    ],
)
def test_sycophancy_off_is_refused_without_the_confirmation(client, store, details):
    refused = _save(client, {"breaker_details": details})
    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["needs_confirmation"] == ["sycophancy_off"]
    assert not store.agent_path.exists()
    confirmed = _save(client, {"breaker_details": details}, acknowledged_dangers=["sycophancy_off"])
    assert confirmed.status_code == 200, confirmed.get_json()
    name, value = next(iter(details.items()))
    assert _overrides(store)[name] == value


@pytest.mark.parametrize(
    "details",
    [{"sycophancy_threshold": 0.94}, {"sycophancy_threshold": 0.0}, {"sycophancy_window_size": 10}],
)
def test_normal_sycophancy_values_save_without_a_question(client, store, details):
    response = _save(client, {"breaker_details": details})
    assert response.status_code == 200, response.get_json()


def test_sycophancy_off_is_asked_once_and_is_published_for_the_page(client):
    first = _save(
        client, {"breaker_details": {"sycophancy_threshold": 1.0}}, acknowledged_dangers=[]
    )
    assert first.get_json()["needs_confirmation"] == ["sycophancy_off"]
    _save(
        client,
        {"breaker_details": {"sycophancy_threshold": 1.0}},
        acknowledged_dangers=["sycophancy_off"],
    )
    # Already off: a later unrelated change is not asked again (like breaker_low).
    again = _save(client, {"breaker_details": {"sycophancy_window_size": 5}})
    assert again.status_code == 200, again.get_json()
    state = client.get("/api/config/hub").get_json()
    assert state["constraints"]["sycophancy_danger"] == hub.SYCOPHANCY_DANGER
    # The page must not round the threshold to the dull end (m-2): published as a rule.
    assert state["constraints"]["breaker_details"]["sycophancy_threshold"]["clamp"] is False


def test_rollback_to_sycophancy_off_needs_the_confirmation_too(client, store):
    _save(
        client,
        {"breaker_details": {"sycophancy_threshold": 1.0}},
        acknowledged_dangers=["sycophancy_off"],
    )
    back_on = _save(client, {"breaker_details": {"sycophancy_threshold": 0.7}})
    entry_id = back_on.get_json()["history_id"]
    version = client.get("/api/config/hub").get_json()["version"]
    refused = client.post(
        f"/api/config/history/{entry_id}/rollback", json={"base_version": version}
    )
    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["needs_confirmation"] == ["sycophancy_off"]
    assert _overrides(store)["sycophancy_threshold"] == 0.7


# --- m-3: with no engine known the server-fixed rows say what the chat server uses -----


def test_fixed_values_without_an_engine_show_the_server_choice(store):
    store.agent_path.write_text(
        json.dumps({"stira_config_overrides": {"critical_action": "readonly"}}), encoding="utf-8"
    )
    state = _client(store).get("/api/config/hub").get_json()
    fixed = {row["name"]: row for row in state["breaker_fixed"]}
    assert fixed["critical_action"]["value"] == "halt"
    assert fixed["critical_action"]["value"] == stira_delivery.SERVER_CHOSEN["critical_action"]
    assert fixed["blocked_tool_names"]["value"] == sorted(StiraConfig().blocked_tool_names)


def test_the_chat_server_builds_its_engine_with_the_server_choice():
    root = Path(__file__).resolve().parents[1]
    source = (root / "dashboard" / "stira_server.py").read_text(encoding="utf-8")
    assert 'critical_action=SERVER_CHOSEN["critical_action"]' in source


# --- m-6: the details' applied check ----------------------------------------------------


def test_details_applied_with_ints_for_floats(store):
    engine = stira_delivery.plain_values(StiraConfig())
    engine["decay_min_interval_sec"] = int(engine["decay_min_interval_sec"])
    engine["hitl_timeout_sec"] = int(engine["hitl_timeout_sec"])
    client = _client(store, runtime_settings_provider=lambda: _runtime(**engine))
    status = client.get("/api/config/hub").get_json()["apply"]["breaker_details"]
    assert status == {"status": "applied", "applies": "restart"}


def test_details_from_an_engine_missing_a_field_are_saved_not_pending(store):
    engine = stira_delivery.plain_values(StiraConfig())
    del engine["sycophancy_window_size"]
    client = _client(store, runtime_settings_provider=lambda: _runtime(**engine))
    status = client.get("/api/config/hub").get_json()["apply"]["breaker_details"]
    assert status == {"status": "saved", "applies": "restart"}


def test_details_differing_from_the_engine_are_pending(store):
    engine = stira_delivery.plain_values(StiraConfig())
    engine["critical_cooldown_turns"] = 7
    client = _client(store, runtime_settings_provider=lambda: _runtime(**engine))
    status = client.get("/api/config/hub").get_json()["apply"]["breaker_details"]
    assert status["status"] == "pending"


# --- m-4 / m-5: pinned (checked, no defect) ---------------------------------------------


def test_reset_to_defaults_proposes_the_whole_breaker_set(client):
    defaults = client.get("/api/config/hub").get_json()["defaults"]
    config = StiraConfig()
    assert defaults["breaker"] == {name: getattr(config, name) for name in hub.BREAKER_SET}
    assert defaults["breaker_details"] == hub.detail_values(config)


def test_a_clamped_file_is_rewritten_by_any_breaker_save(client, store):
    """The load rounds the overrides themselves (``_clamp_bc_window_size``), so the agent
    file written by a breaker save holds the rounded value even when the details did not
    change. A save that does not touch the agent file leaves it (and its flag) as it is."""
    store.agent_path.write_text(
        json.dumps({"stira_config_overrides": {"bc_window_size": 3, "hitl_timeout_sec": 10**6}}),
        encoding="utf-8",
    )
    unrelated = _save(client, {"board": {"svg": False}, "classifier": {"choice": "rule"}})
    assert unrelated.status_code == 200, unrelated.get_json()
    assert _overrides(store)["bc_window_size"] == 3
    assert unrelated.get_json()["state"]["bc_window_clamped"] is True
    entries = _save(client, {"breaker": {"bc_threshold": 0.55}})
    assert entries.status_code == 200, entries.get_json()
    saved = _overrides(store)
    assert saved["bc_window_size"] == 5
    assert saved["hitl_timeout_sec"] == stira_delivery.HITL_TIMEOUT_MAX_SEC
    assert entries.get_json()["state"]["bc_window_clamped"] is False

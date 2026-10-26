"""#554: a registry tool the server does not bind is not shown as usable.

``memory_rate`` is registered (a read tool, Always by default) but bound only when
``memory_autonomy`` is on (ADR 0018). The model is told so (Tool Availability, #508); the
settings page and ``/api/tools/list`` used to show it as Always/allowed. The rule stays as
saved (it applies once the tool is bound); the row says the server does not offer it.
Display only: the gate and the tools the model is offered are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability.config_api import ConfigStore, create_config_blueprint


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


def _client(store, **kwargs):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client()


def _row(rows, name="memory_rate"):
    return next(row for row in rows if row["name"] == name)


def _all_rows(client):
    return {
        "list": client.get("/api/tools/list").get_json()["tools"],
        "config": client.get("/api/config/tools").get_json()["tools"],
        "hub": client.get("/api/config/hub").get_json()["tools"],
    }


def test_an_unbound_tool_is_not_shown_as_allowed(tmp_path):
    runtime = {"tools": None, "breaker": None, "unbound_tools": {"memory_rate": "not_configured"}}
    client = _client(_store(tmp_path), runtime_settings_provider=lambda: dict(runtime))
    for route, rows in _all_rows(client).items():
        row = _row(rows)
        assert row["policy"] == "always", route  # the saved rule is kept
        assert row["enabled"] is False, route
        assert row["unavailable_reason"] == "not_configured", route
        assert row["effective"]["decision"] == "forbidden", route
        assert row["effective"]["reasons"] == ["not_configured"], route
        assert row["stack"]["floor"]["reason"] == "not_configured", route
        # Still editable and not reported as an exec switch.
        assert row["exposed"] is True, route
        # Other tools are untouched.
        assert _row(rows, "file_read")["effective"]["decision"] == "allow", route
    summary = client.get("/api/config/hub").get_json()["summary"]
    enabled = sum(1 for row in _all_rows(client)["hub"] if row["enabled"])
    assert summary["tools_effective"] == enabled


def test_without_a_running_server_or_when_bound_the_row_is_as_before(tmp_path):
    store = _store(tmp_path)
    for client in (
        _client(store),
        _client(store, runtime_settings_provider=lambda: {"unbound_tools": {}}),
        # Unknown names and non-mappings are ignored.
        _client(store, runtime_settings_provider=lambda: {"unbound_tools": {"nope": "x"}}),
        _client(store, runtime_settings_provider=lambda: {"unbound_tools": ["memory_rate"]}),
    ):
        row = _row(client.get("/api/tools/list").get_json()["tools"])
        assert row["enabled"] is True
        assert row["unavailable_reason"] is None
        assert row["effective"]["decision"] == "allow"


def test_the_page_has_a_text_for_the_reason():
    js = (
        Path(__file__).resolve().parents[1] / "dashboard" / "static" / "js" / "settings_hub.js"
    ).read_text(encoding="utf-8")
    assert 'not_configured: () => t("policy.reason_not_configured")' in js
    for lang in ("ja", "en"):
        catalog = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "dashboard"
                / "static"
                / "i18n"
                / f"catalog.{lang}.json"
            ).read_text(encoding="utf-8")
        )
        assert catalog["policy.reason_not_configured"]


# --- measured on the chat server ---------------------------------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    monkeypatch.delenv("STIRA_MEMORY_AUTONOMY", raising=False)
    store = _store(tmp_path)

    def make(agent_config=None, **kwargs):
        if agent_config is not None:
            store.agent_path.write_text(json.dumps(agent_config), encoding="utf-8")
        return stira_server.create_app(config_store=store, **kwargs)

    return make


def test_stira_memory_rate_is_not_configured_until_memory_autonomy_is_on(stira):
    off = stira().test_client()
    row = _row(off.get("/api/tools/list").get_json()["tools"])
    assert (row["enabled"], row["unavailable_reason"]) == (False, "not_configured")
    assert row["effective"]["decision"] == "forbidden"
    # "Restart" on the same files with memory_autonomy on (tests only share the data dir).
    on = stira({"memory_autonomy": {"phase": 0}}, _tests_share_data_dir=True).test_client()
    row = _row(on.get("/api/tools/list").get_json()["tools"])
    assert (row["enabled"], row["unavailable_reason"]) == (True, None)
    assert row["effective"]["decision"] == "allow"

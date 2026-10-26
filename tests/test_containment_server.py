"""ADR 0023 phase 0 (#460-0) on the real chat server app: off by default, read-only when on.

Offline: no model is ever called (the Agent constructor is a failing Mock, as in
``test_admin_api_permissions``).
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore
from stiracore.tools import file_tools


def _app(tmp_path, monkeypatch, agent_config=None, env=None):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    if agent_config is not None:
        (tmp_path / "agent.json").write_text(json.dumps(agent_config))
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    app = stira_server.create_app(config_store=store)
    return app


@pytest.fixture(autouse=True)
def _reset_read_audit():
    yield
    file_tools.set_read_audit(None)


def _spy_agent(monkeypatch):
    from dashboard import stira_server

    seen: dict = {}
    original = stira_server.NXPAgent

    def spy(*args, **kwargs):
        seen["extra_hooks"] = list(kwargs.get("extra_hooks") or ())
        return original(*args, **kwargs)

    monkeypatch.setattr(stira_server, "NXPAgent", spy)
    return seen


def _containment_hooks(seen):
    from stiracore.middleware.containment_observer import ContainmentObserverHook

    return [hook for hook in seen["extra_hooks"] if isinstance(hook, ContainmentObserverHook)]


def test_absent_key_is_off_no_hook_no_tee(tmp_path, monkeypatch):
    seen = _spy_agent(monkeypatch)
    app = _app(tmp_path, monkeypatch)
    assert _containment_hooks(seen) == []
    assert app.extensions["containment"] is None
    body = app.test_client().get("/api/containment").get_json()
    assert body["enabled"] is False and body["window"] is None
    assert body["observe_only"] is True and body["enforced"] is False
    assert body["tables"]["stages"][0] == "open"
    record = file_tools._READ_AUDIT["record"]
    assert getattr(record, "__name__", "") == "record_read_refusal"


def test_the_environment_forces_off(tmp_path, monkeypatch):
    app = _app(
        tmp_path, monkeypatch, {"containment": {"phase": 0}}, {"STIRA_CONTAINMENT_OBSERVE": "0"}
    )
    assert app.extensions["containment"] is None
    body = app.test_client().get("/api/containment").get_json()
    assert body["enabled"] is False and body["forced_off"] is True


def test_on_counts_refused_reads_and_records_rows(tmp_path, monkeypatch):
    seen = _spy_agent(monkeypatch)
    app = _app(tmp_path, monkeypatch, {"containment": {"phase": 0, "window_seconds": 300}})
    observer = app.extensions["containment"]
    assert [hook.observer for hook in _containment_hooks(seen)] == [observer]
    assert observer is not None and observer.window_seconds == 300
    record = file_tools._READ_AUDIT["record"]
    assert getattr(record, "__name__", "") == "tee"
    # the audit row is still written, and one containment.observed row besides it
    record(tool_name="file_read", target="path:" + "a" * 64, reason_code="E_READ_SECRET_PATH")
    body = app.test_client().get("/api/containment").get_json()
    assert body["enabled"] is True and body["phase"] == 0
    assert body["window"]["score"] == 2.0 and body["window"]["would_be"] == "restricted"
    from stiracore.observability.event_store import SQLiteEventStore

    store = SQLiteEventStore(tmp_path / "data" / "events.db")
    try:
        denied = store.get_events(limit=10, event_type="audit.read.denied")
        observed = store.get_events(limit=10, event_type="containment.observed")
    finally:
        store.close()
    assert [row.attributes["reason_code"] for row in denied] == ["E_READ_SECRET_PATH"]
    assert [row.attributes for row in observed] == [
        {
            "signal": "G1",
            "code": "E_READ_SECRET_PATH",
            "weight": 2.0,
            "score": 2.0,
            "would_be": "restricted",
            "window_seconds": 300,
            "phase": 0,
        }
    ]


def test_the_summary_carries_would_be():
    from dashboard import stira_server
    from stiracore.core.containment import ContainmentObserver

    observer = ContainmentObserver()
    observer.observe("E_OWN_DASHBOARD")
    summary = stira_server._with_containment({"breaker": "normal"}, observer)
    assert summary == {
        "breaker": "normal",
        "containment": {"would_be": "restricted", "score": 3.0},
    }
    assert stira_server._with_containment({"breaker": "normal"}, None) == {"breaker": "normal"}
    assert stira_server._with_containment(None, observer) is None

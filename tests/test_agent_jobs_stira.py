"""ADR 0021 S2 (#239) on the chat server: the Agent layer is off by default, built only with
``agent_jobs.enabled``, and the heartbeat tick never reaches it (tripwires that raise)."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from stiracore.control import agent_jobs
from stiracore.control.agent_jobs import GatedJobExecutor
from stiracore.scheduler.scheduler import SchedulerRefused

AGENT_JOB = {
    "job_id": "digest",
    "name": "digest",
    "layer": "agent",
    "prompt": "summarize",
    "tools": ["current_time"],
    "schedule": {"type": "cron", "expr": "0 * * * *"},
    "budget": {"max_tokens_per_run": 500, "max_tokens_per_day": 2000},
}


def _app(tmp_path, monkeypatch, gateway: dict):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("STIRA_SCHEDULER", "1")
    model = Mock(side_effect=AssertionError("model reached"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = AssertionError(f"model.{method} reached")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    store.gateway_path.write_text(json.dumps(gateway), encoding="utf-8")
    return stira_server.create_app(config_store=store)


def _close(app):
    for name in ("heartbeat", "scheduler"):
        if app.extensions.get(name) is not None:
            app.extensions[name].stop()


def test_agent_jobs_are_off_by_default_and_cannot_be_created(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {})
    try:
        scheduler = app.extensions["scheduler"]
        assert scheduler.running and not scheduler.agent_jobs_enabled
        with pytest.raises(SchedulerRefused) as error:
            scheduler.create_job(AGENT_JOB, by="local:operator", confirm=True)
        assert error.value.code == "scope"
        assert scheduler.status()["agent_daily_token_budget"] == 50_000
    finally:
        _close(app)


def test_the_switch_builds_the_gated_executor_with_the_configured_pool(tmp_path, monkeypatch):
    app = _app(
        tmp_path, monkeypatch, {"agent_jobs": {"enabled": True, "agent_daily_token_budget": 1234}}
    )
    try:
        scheduler = app.extensions["scheduler"]
        assert scheduler.agent_jobs_enabled
        assert isinstance(scheduler._agent_executor, GatedJobExecutor)
        assert scheduler.status()["agent_daily_token_budget"] == 1234
    finally:
        _close(app)


def test_a_tick_with_agent_jobs_on_reaches_no_agent_executor_or_model(tmp_path, monkeypatch):
    hits: list[str] = []

    def tripwire(name):
        def fired(*_a, **_k):
            hits.append(name)
            raise AssertionError(f"{name} reached")

        return fired

    monkeypatch.setattr(GatedJobExecutor, "__call__", tripwire("agent executor"))
    monkeypatch.setattr(agent_jobs, "create_nxp_subagent", tripwire("child"), raising=False)
    app = _app(tmp_path, monkeypatch, {"hb_enabled": True, "agent_jobs": {"enabled": True}})
    try:
        from strands.agent import agent as strands_agent

        monkeypatch.setattr(strands_agent.Agent, "__init__", tripwire("strands Agent"))
        scheduler = app.extensions["scheduler"]
        scheduler.create_job(AGENT_JOB, by="local:operator", confirm=True)
        daemon = app.extensions["heartbeat"]
        assert daemon is not None and daemon.running
        daemon._on_tick(daemon.gateway.tick())
        assert hits == []
    finally:
        _close(app)


def test_a_bad_agent_jobs_section_keeps_the_layer_off(tmp_path, monkeypatch):
    from dashboard import stira_server

    store = Mock()
    store.load.side_effect = ValueError("bad")
    assert stira_server._agent_job_settings(store).enabled is False
    assert (
        stira_server._agent_job_executor(
            stira_server._agent_job_settings(store), lambda: None, lambda _n: []
        )
        is None
    )

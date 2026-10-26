"""chat server scheduler wiring and API (#239, ADR 0008 §10): opt-in, authenticated changes,
scope, confirmation, breaker refusal, audit."""

from __future__ import annotations

import logging
import threading
from unittest.mock import Mock

import pytest

from stiracore.core import BreakLevel
from stiracore.observability.config_api import ConfigStore

NO_CSRF = {"X-Test-No-Csrf": "1"}
JOB = {
    "job_id": "health",
    "name": "health",
    "action": "health_check",
    "schedule": {"type": "interval", "every_sec": 600},
}


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    data_dir = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)
    created = []

    def make(env=None, **kwargs):
        if env is None:
            monkeypatch.delenv("STIRA_SCHEDULER", raising=False)
        else:
            monkeypatch.setenv("STIRA_SCHEDULER", env)
        store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
        app = stira_server.create_app(config_store=store, **kwargs)
        created.append(app)
        return app, data_dir

    yield make
    for app in created:
        if app.extensions.get("scheduler") is not None:
            app.extensions["scheduler"].stop()


def _post(client, url, body, headers=None):
    return client.post(url, json=body, headers={"Origin": "http://localhost", **(headers or {})})


def test_scheduler_is_off_by_default_and_changes_nothing(stira):
    app, data_dir = stira()
    assert app.extensions["scheduler"] is None
    assert not (data_dir / "scheduler.lock").exists()
    assert not (data_dir / "scheduler-events.jsonl").exists()
    assert not any(t.name == "stira-scheduler" for t in threading.enumerate())
    client = app.test_client()
    data = client.get("/api/scheduler").get_json()
    assert data["status"] == {"enabled": False, "running": False} and data["jobs"] == []
    response = _post(client, "/api/scheduler/jobs", {"job": JOB, "confirm": True})
    assert response.status_code == 503
    assert client.get("/schedules").status_code == 200


def test_opt_in_runs_a_confirmed_gateway_job_and_records_the_principal(stira):
    app, data_dir = stira("1")
    scheduler = app.extensions["scheduler"]
    assert scheduler.running and (data_dir / "scheduler.lock").exists()
    client = app.test_client()
    response = _post(
        client, "/api/scheduler/jobs", {"job": JOB, "confirm": True, "actor": "professor"}
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["state"] == "active"
    created = next(e for e in scheduler.log.events() if e["kind"] == "job_created")
    assert created["body"]["by"] == "local:professor"
    status = client.get("/api/scheduler").get_json()
    assert status["status"]["actions"] == ["health_check", "memory_harvest"]
    assert status["status"]["agent_jobs"] is False
    assert [j["job_id"] for j in status["jobs"]] == ["health"]

    # The health_check Gateway action runs without a model (nxp Agent would raise).
    scheduler._actions["health_check"](Mock(scheduled_at=0.0))
    page = client.get("/schedules").get_data(as_text=True)
    assert 'id="sched-jobs"' in page and "js/schedules_page.js" in page


@pytest.mark.parametrize(
    "headers",
    [
        NO_CSRF,
        {"X-CSRF-Token": "x" * 43},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "evil.example"},
    ],
)
def test_unauthenticated_or_cross_origin_changes_are_refused(stira, headers):
    app, _ = stira("1")
    scheduler = app.extensions["scheduler"]
    client = app.test_client()
    before = len(scheduler.log.events())
    for url in (
        "/api/scheduler/jobs",
        "/api/scheduler/jobs/health",
        "/api/scheduler/jobs/health/resume",
        "/api/scheduler/jobs/health/pause",
    ):
        response = client.post(url, json={"job": JOB, "confirm": True}, headers=headers)
        assert response.status_code == 403, (url, headers)
    assert len(scheduler.log.events()) == before
    assert scheduler.jobs() == []


@pytest.mark.parametrize(
    ("job", "code"),
    [
        ({**JOB, "action": "shell"}, "scope"),
        ({**JOB, "layer": "agent", "action": None, "prompt": "go"}, "scope"),
        ({**JOB, "created_by": "local:someone-else"}, "scope"),
        ({**JOB, "schedule": {"type": "interval", "every_sec": 5}}, "invalid"),
    ],
)
def test_out_of_scope_or_invalid_jobs_are_refused_and_audited(stira, caplog, job, code):
    app, _ = stira("1")
    client = app.test_client()
    with caplog.at_level(logging.WARNING, logger="stiracore.observability.scheduler_api"):
        response = _post(client, "/api/scheduler/jobs", {"job": job, "confirm": True})
    assert response.get_json()["code"] == code
    assert response.status_code == (403 if code == "scope" else 400)
    assert app.extensions["scheduler"].jobs() == []
    assert any(f"code={code}" in r.getMessage() for r in caplog.records)
    assert not any("someone-else" in r.getMessage() for r in caplog.records)


def test_confirmation_and_scope_on_update_and_resume(stira):
    app, _ = stira("1")
    client = app.test_client()
    assert _post(client, "/api/scheduler/jobs", {"job": JOB}).status_code == 428
    assert _post(client, "/api/scheduler/jobs", {"job": JOB, "confirm": "yes"}).status_code == 428
    assert _post(client, "/api/scheduler/jobs", {"job": JOB, "confirm": True}).status_code == 200
    update = "/api/scheduler/jobs/health"
    assert _post(client, update, {"changes": {"name": "h2"}}).status_code == 428
    response = _post(client, update, {"changes": {"layer": "agent"}, "confirm": True})
    assert response.status_code == 403
    assert _post(client, update, {"changes": {"name": "h2"}, "confirm": True}).status_code == 200
    assert _post(client, update + "/pause", {}).status_code == 200
    assert _post(client, update + "/resume", {}).status_code == 428
    assert _post(client, update + "/resume", {"confirm": True}).status_code == 200
    assert _post(client, "/api/scheduler/runs/nope/cancel", {}).status_code == 404
    kinds = [e["kind"] for e in app.extensions["scheduler"].log.events()]
    assert kinds == ["job_created", "job_updated", "job_paused", "job_resumed"]


def test_breaker_critical_refuses_widening_changes_but_pause_passes(stira, monkeypatch):
    app, _ = stira("1")
    client = app.test_client()
    assert _post(client, "/api/scheduler/jobs", {"job": JOB, "confirm": True}).status_code == 200
    scheduler = app.extensions["scheduler"]
    engine = Mock()
    engine.breaker_status.return_value = Mock(level=BreakLevel.CRITICAL)
    monkeypatch.setattr(scheduler, "_breaker_level", lambda: engine.breaker_status().level)
    other = {**JOB, "job_id": "other"}
    response = _post(client, "/api/scheduler/jobs", {"job": other, "confirm": True})
    assert (response.status_code, response.get_json()["code"]) == (409, "breaker")
    assert _post(client, "/api/scheduler/jobs/health/pause", {}).status_code == 200
    response = _post(client, "/api/scheduler/jobs/health/resume", {"confirm": True})
    assert response.status_code == 409
    assert [j["state"] for j in scheduler.jobs()] == ["paused"]


def test_second_server_on_the_same_state_dir_is_refused(stira):
    from stiracore.config.data_dir_lock import DataDirLockError

    first, _ = stira("1")
    with pytest.raises(DataDirLockError):
        stira("1")  # the data-directory owner lock (#285) refuses first
    assert first.extensions["scheduler"].running


def test_second_scheduler_on_the_same_state_dir_does_not_run_jobs(stira):
    # A second app in one process only through the test switch (#285).
    first, _ = stira("1")
    second, _ = stira("1", _tests_share_data_dir=True)
    assert first.extensions["scheduler"].running
    assert not second.extensions["scheduler"].running
    client = second.test_client()
    response = _post(client, "/api/scheduler/jobs", {"job": JOB, "confirm": True})
    assert response.status_code == 503
    assert client.get("/api/scheduler").get_json()["status"]["running"] is False


def test_wrong_types_are_400_and_secret_ids_never_reach_the_log(stira, caplog):
    app, data_dir = stira("1")
    client = app.test_client()
    for job in ({**JOB, "layer": []}, {**JOB, "schedule": {"type": []}}, {**JOB, "budget": []}):
        response = _post(client, "/api/scheduler/jobs", {"job": job, "confirm": True})
        assert response.status_code == 400, job
    secret = "sk-" + "a" * 40
    with caplog.at_level(logging.INFO, logger="stiracore.observability.scheduler_api"):
        response = _post(
            client, "/api/scheduler/jobs", {"job": {**JOB, "job_id": secret}, "confirm": True}
        )
        assert response.status_code == 400
        assert _post(client, f"/api/scheduler/jobs/{secret}/pause", {}).status_code == 404
    assert not any(secret in r.getMessage() for r in caplog.records)
    log = data_dir / "scheduler-events.jsonl"
    assert not log.exists() or secret not in log.read_text(encoding="utf-8")

"""#494: a breaker entry threshold changes only through the settings hub's checks.

The old ``/api/config/breaker`` route wrote the three entry thresholds with none of the
hub's checks (no danger confirmation, no history row; measured: ``stability_critical`` 0.05
saved with 200). No screen called it; it is gone. This file pins that:

* the old route answers 404 (GET and POST);
* every write route of the config blueprint is listed here (a new one fails the listing
  until it is classified), and the ones that are not the hub / its rollback cannot change a
  breaker value whatever body they get;
* the hub and its rollback refuse a low entry threshold without the confirmation, and
  every breaker change they save leaves a history row.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability.config_api import ConfigStore, create_config_blueprint

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
BREAKER_ENTRY = ("stability_warning", "stability_critical", "bc_threshold")
LOW = {"stability_warning": 0.25, "stability_critical": 0.05, "bc_threshold": 0.2}

# Every write route of the config blueprint (measured with ``app.url_map``). "hub": saves
# breaker values through the hub's validation, confirmation and history. "none": cannot
# write a breaker value.
WRITE_ROUTES = {
    ("/api/config/hub", "POST"): "hub",
    ("/api/config/history/<entry_id>/rollback", "POST"): "hub",
    ("/api/config/hub/validate", "POST"): "none",  # validation only
    ("/api/config/models", "POST"): "none",
    ("/api/config/models/test", "POST"): "none",
    ("/api/config/jev", "POST"): "none",
    ("/api/config/tools", "POST"): "none",
    ("/api/tools/config", "POST"): "none",
    ("/api/tools/page-check-ports", "POST"): "none",  # page_check's ports only (ADR 0015)
}


@pytest.fixture
def store(tmp_path):
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
        history_path=tmp_path / "settings-history.jsonl",
    )


@pytest.fixture
def app(store):
    application = Flask(__name__)
    application.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return application


def _breaker(store) -> dict:
    _gateway, agent = store.load()
    overrides = agent.stira_config_overrides or {}
    return {name: overrides.get(name) for name in BREAKER_ENTRY}


def _history(store) -> list[dict]:
    path = store.history_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _hub_save(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def test_old_breaker_route_is_gone(app, store):
    client = app.test_client()
    assert client.get("/api/config/breaker").status_code == 404
    response = client.post("/api/config/breaker", json={"stability_critical": 0.05})
    assert response.status_code == 404
    assert _breaker(store) == dict.fromkeys(BREAKER_ENTRY)
    assert not store.agent_path.exists()


def test_every_config_write_route_is_classified(app):
    measured = {
        (rule.rule, method) for rule in app.url_map.iter_rules() for method in rule.methods & UNSAFE
    }
    assert measured == set(WRITE_ROUTES)


def _bodies():
    """Breaker values in every shape a route might read them from."""
    return (
        dict(LOW),
        {"changes": {"breaker": dict(LOW)}},
        {"breaker": dict(LOW)},
        {"stira_config_overrides": dict(LOW)},
        {"model_id": "x", **LOW},
        {"name": "web_fetch", "enabled": True, **LOW},
        {"classifier": "rule", **LOW},
    )


@pytest.mark.parametrize(
    "route",
    sorted(key for key, kind in WRITE_ROUTES.items() if kind == "none"),
    ids=lambda key: key[0],
)
def test_non_hub_write_routes_cannot_change_a_breaker_value(app, store, route):
    client = app.test_client()
    rule, method = route
    for body in _bodies():
        client.open(rule, method=method, json=body)
        assert _breaker(store) == dict.fromkeys(BREAKER_ENTRY), (rule, body)
    assert _history(store) == [] or all(
        "breaker" not in json.dumps(entry.get("diff", entry)) for entry in _history(store)
    )


def test_hub_needs_the_confirmation_and_records_a_history_row(app, store):
    client = app.test_client()
    refused = _hub_save(client, {"breaker": {"stability_critical": 0.05}})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["breaker_low"]
    assert _breaker(store)["stability_critical"] is None
    assert _history(store) == []

    saved = _hub_save(
        client, {"breaker": {"stability_critical": 0.05}}, acknowledged_dangers=["breaker_low"]
    )
    assert saved.status_code == 200, saved.get_json()
    assert _breaker(store)["stability_critical"] == 0.05
    rows = _history(store)
    assert len(rows) == 1 and "stability_critical" in json.dumps(rows[0])


def test_a_safe_breaker_change_still_leaves_a_history_row(app, store):
    client = app.test_client()
    saved = _hub_save(client, {"breaker": {"stability_critical": 0.25}})
    assert saved.status_code == 200, saved.get_json()
    assert _breaker(store)["stability_critical"] == 0.25
    assert len(_history(store)) == 1


def test_rollback_to_a_low_threshold_needs_the_confirmation(app, store):
    client = app.test_client()
    low = _hub_save(
        client, {"breaker": {"stability_critical": 0.05}}, acknowledged_dangers=["breaker_low"]
    )
    assert low.status_code == 200
    raised = _hub_save(client, {"breaker": {"stability_critical": 0.25}})
    assert raised.status_code == 200
    # Roll back the raise: the low value would come back, so it is confirmed again.
    entry_id = _history(store)[-1]["id"]
    version = client.get("/api/config/hub").get_json()["version"]
    refused = client.post(
        f"/api/config/history/{entry_id}/rollback", json={"base_version": version}
    )
    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["needs_confirmation"] == ["breaker_low"]
    assert _breaker(store)["stability_critical"] == 0.25
    rows_before = len(_history(store))
    confirmed = client.post(
        f"/api/config/history/{entry_id}/rollback",
        json={"base_version": version, "acknowledged_dangers": ["breaker_low"]},
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    assert _breaker(store)["stability_critical"] == 0.05
    assert len(_history(store)) == rows_before + 1


# #454 (ADR 0020): the artifact blueprint's write routes (measured with ``app.url_map``);
# none of them may write a breaker value.
ARTIFACT_WRITE_ROUTES = {("/api/artifacts/<artifact_id>/labels", "POST"): "none"}


@pytest.fixture
def artifact_app(store, tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.observability.artifact_api import create_artifact_blueprint
    from stiracore.tools.workspace_artifacts import WorkspaceArtifacts

    data_dir = tmp_path / "data"
    (data_dir / "workspace").mkdir(parents=True)
    policy, _floor = build_policy(AgentConfig(), data_dir)
    registry = WorkspaceArtifacts(data_dir / "artifacts")
    application = Flask(__name__)
    application.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    application.register_blueprint(create_artifact_blueprint(registry, lambda: policy))
    yield application
    registry.close()


def test_every_artifact_write_route_is_classified_and_leaves_the_breaker(artifact_app, store):
    measured = {
        (rule.rule, method)
        for rule in artifact_app.url_map.iter_rules()
        if rule.rule.startswith("/api/artifacts")
        for method in rule.methods & UNSAFE
    }
    assert measured == set(ARTIFACT_WRITE_ROUTES)
    client = artifact_app.test_client()
    for rule, method in ARTIFACT_WRITE_ROUTES:
        for body in _bodies():
            client.open(rule.replace("<artifact_id>", "a" + "0" * 20), method=method, json=body)
            assert _breaker(store) == dict.fromkeys(BREAKER_ENTRY), (rule, body)
    assert _history(store) == []


# ADR 0023 phase 0 (#460-0): the containment blueprint has no write route at all, and no
# method on its one route changes a breaker value.
def test_the_containment_blueprint_is_read_only_and_leaves_the_breaker(store):
    from stiracore.core.containment import ContainmentObserver, settings_from
    from stiracore.observability.containment_api import (
        containment_view,
        create_containment_blueprint,
    )

    settings = settings_from({"phase": 0}, {})
    observer = ContainmentObserver()
    application = Flask(__name__)
    application.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    application.register_blueprint(
        create_containment_blueprint(lambda: containment_view(settings, observer))
    )
    measured = {
        (rule.rule, method)
        for rule in application.url_map.iter_rules()
        if rule.rule.startswith("/api/containment")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert measured == {("/api/containment", "GET")}
    client = application.test_client()
    for method in sorted(UNSAFE):
        for body in _bodies():
            assert client.open("/api/containment", method=method, json=body).status_code == 405
    assert client.get("/api/containment").status_code == 200
    assert _breaker(store) == dict.fromkeys(BREAKER_ENTRY)
    assert _history(store) == []


# ADR 0022 phase 0 (#458): the taint blueprint has one read route and changes no breaker value.
def test_the_taint_blueprint_is_read_only_and_leaves_the_breaker(store):
    from stiracore.labels import settings_from
    from stiracore.middleware.taint_observer import TaintObserver
    from stiracore.observability.taint_api import create_taint_blueprint, taint_view

    settings = settings_from({"phase": 0}, {})
    observer = TaintObserver()
    application = Flask(__name__)
    application.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    application.register_blueprint(create_taint_blueprint(lambda: taint_view(settings, observer)))
    measured = {
        (rule.rule, method)
        for rule in application.url_map.iter_rules()
        if rule.rule.startswith("/api/taint")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert measured == {("/api/taint", "GET")}
    client = application.test_client()
    for method in sorted(UNSAFE):
        for body in _bodies():
            assert client.open("/api/taint", method=method, json=body).status_code == 405
    assert client.get("/api/taint").status_code == 200
    assert _breaker(store) == dict.fromkeys(BREAKER_ENTRY)
    assert _history(store) == []


# #499 item 5: every write route of the **server** (not only the config blueprint) is
# listed (measured with ``app.url_map`` on the real app, offline model), and none of the
# routes outside the hub writes ``stira_config_overrides`` whatever body it gets. The config
# blueprint's own routes are pinned above (``WRITE_ROUTES``); they are listed here with
# "config" and not posted again (``/api/config/models/test`` would try a model).
SERVER_WRITE_ROUTES = {
    **{key: "config" for key in WRITE_ROUTES},
    ("/api/artifacts/<artifact_id>/labels", "POST"): "none",
    ("/api/chat", "POST"): "none",
    ("/api/chat/cancel", "POST"): "none",
    ("/api/chat/session/new", "POST"): "none",
    ("/api/diagnostics/doctor", "POST"): "none",
    ("/api/diagnostics/write-failures/<card_id>/dismiss", "POST"): "none",
    ("/api/diagnostics/write-failures/<card_id>/retry", "POST"): "none",
    ("/api/hitl/<request_id>/decision", "POST"): "none",
    ("/api/allowances/<allow_id>/revoke", "POST"): "none",  # ADR 0025 (a narrowing)
    ("/api/allowances/revoke-all", "POST"): "none",
    ("/api/authority/kill", "POST"): "none",  # ADR 0011 W2 (#296): a narrowing
    ("/api/authority/outbox/ack", "POST"): "none",
    ("/api/memory/procedural/proposals/<key>/approve", "POST"): "none",  # #463
    ("/api/memory/procedural/proposals/<key>/reject", "POST"): "none",  # #463
    ("/api/scheduler/jobs", "POST"): "none",  # also ``layer: "memory"`` jobs (#251)
    ("/api/scheduler/jobs/<job_id>", "POST"): "none",
    ("/api/scheduler/jobs/<job_id>/pause", "POST"): "none",
    ("/api/scheduler/jobs/<job_id>/resume", "POST"): "none",
    ("/api/scheduler/runs/<run_id>/cancel", "POST"): "none",
    ("/api/webhooks/<provider>", "POST"): "none",
    ("/api/yolo/settings", "POST"): "none",
}
# With the YOLO allowance the server adds these (ADR 0012 §8).
YOLO_WRITE_ROUTES = {
    ("/api/yolo/request", "POST"): "none",
    ("/api/yolo/request/cancel", "POST"): "none",
    ("/api/yolo/stop", "POST"): "none",
    ("/api/yolo/unlatch", "POST"): "none",
    ("/api/host-operations/<op_id>/cancel", "POST"): "none",
    ("/api/host-operations/<op_id>/confirm", "POST"): "none",
    ("/api/progress/<report_id>/hold", "POST"): "none",  # ADR 0014
    ("/api/progress/<report_id>/stop", "POST"): "none",
}
_PATH_VALUES = {
    "allow_id": "a" * 32,
    "artifact_id": "a" + "0" * 20,
    "card_id": "card-1",
    "request_id": "req-1",
    "key": "rule-1",
    "job_id": "job-1",
    "run_id": "run-1",
    "provider": "generic",
    "op_id": "op-1",
    "entry_id": "entry-1",
    "report_id": "report-1",
}


def _server_bodies():
    """The breaker shapes of ``_bodies`` plus the shapes the other routes read."""
    return (
        *_bodies(),
        {
            "job_id": "harvest",
            "name": "harvest",
            "layer": "memory",
            "action": "memory_harvest",
            "schedule": {"type": "interval", "every_sec": 3600},
            "breaker": dict(LOW),
            **LOW,
        },
        {"decision": "approve", "stira_config_overrides": dict(LOW)},
        {"settings": {"breaker": dict(LOW)}, "yolo": {"breaker": dict(LOW)}},
        {"message": "set stability_critical to 0.05", **LOW},
    )


def _overrides(store) -> tuple:
    _gateway, agent = store.load()
    return dict(agent.stira_config_overrides or {}), _breaker(store)


def _measured_writes(app) -> set:
    return {
        (rule.rule, method) for rule in app.url_map.iter_rules() for method in rule.methods & UNSAFE
    }


def _post_everything(app, routes, token):
    client = app.test_client()
    keys = iter(range(100_000))
    store = app.extensions["config_store"]
    before = _overrides(store)
    for (rule, method), kind in sorted(routes.items()):
        if kind != "none":
            continue
        path = rule
        for name, value in _PATH_VALUES.items():
            path = path.replace(f"<{name}>", value)
        for body in _server_bodies():
            client.open(
                path,
                method=method,
                json=body,
                headers={"X-CSRF-Token": token, "Idempotency-Key": f"k-{next(keys)}"},
            )
            assert _overrides(store) == before, (rule, body)
    return before


@pytest.fixture
def server_app(tmp_path, monkeypatch):
    from tests.test_new_session import make_stira

    monkeypatch.setenv("STIRA_SCHEDULER", "1")  # its routes answer (memory jobs, #251)
    generator = make_stira(tmp_path, monkeypatch)
    stira = next(generator)
    yield stira["app"], stira["token"]
    next(generator, None)


def test_every_server_write_route_is_classified_and_none_writes_the_overrides(server_app):
    app, token = server_app
    assert _measured_writes(app) == set(SERVER_WRITE_ROUTES)
    before = _post_everything(app, SERVER_WRITE_ROUTES, token)
    assert before == ({}, dict.fromkeys(BREAKER_ENTRY))


def test_the_yolo_routes_are_classified_and_none_writes_the_overrides(tmp_path, monkeypatch):
    from stiracore.yolo import own_dashboard, runtime
    from tests.test_yolo_host_gate import _build

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    app = _build(tmp_path, monkeypatch, "internal", scheduler=True, host_confirm="browser")
    try:
        assert _measured_writes(app) == set(SERVER_WRITE_ROUTES) | set(YOLO_WRITE_ROUTES)
        token = app.config["STIRA_CSRF_TOKEN"]
        # The routes shared with the server too: some answer differently with the allowance
        # (``/api/yolo/settings``, scheduler create / resume, the chat).
        assert len(_run_valid(app, token)) == 9
        _post_everything(app, {**SERVER_WRITE_ROUTES, **YOLO_WRITE_ROUTES}, token)
    finally:
        app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _valid_requests(app):
    """One request per route that reaches the handler's main branch (a real target, the
    state it needs), with the status it answers (measured). Breaker values ride along
    where the route accepts extra keys; strict routes are pinned without them (their
    extra-key refusal is covered by ``_post_everything``)."""
    from dashboard import stira_server
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.yolo.settings_api import ROUTE as YOLO_SETTINGS

    client = app.test_client()
    proposals = app.extensions["procedure_proposals"]
    approve = proposals.propose("w499_approve", "RULE-A", "why", source_turn="chat-1")
    reject = proposals.propose("w499_reject", "RULE-B", "why", source_turn="chat-1")
    policy, _floor = build_policy(AgentConfig(), stira_server.DATA_DIR)
    (policy.workspace / "page.html").parent.mkdir(parents=True, exist_ok=True)
    (policy.workspace / "page.html").write_text("<p>x</p>\n", encoding="utf-8")
    artifact = app.extensions["artifacts"].register(policy, "page.html", turn_id="turn-1")["id"]
    yolo_version = client.get(YOLO_SETTINGS).get_json()["version"]
    return [
        ("/api/chat/session/new", {}, 200),
        (
            "/api/memory/procedural/proposals/w499_approve/approve",
            {"sha256": approve["sha256"], "confirm": True},
            200,
        ),
        (
            "/api/memory/procedural/proposals/w499_reject/reject",
            {"sha256": reject["sha256"], "confirm": True},
            200,
        ),
        (f"/api/artifacts/{artifact}/labels", {"labels": ["stable"]}, 200),
        ("/api/chat", {"message": "set stability_critical to 0.05", **LOW}, 200),
        ("/api/chat/cancel", {**LOW}, 202),
        ("/api/diagnostics/doctor", {**LOW}, 200),
        (
            "/api/scheduler/jobs",
            {
                "job_id": "harvest",
                "name": "harvest",
                "layer": "memory",
                "action": "memory_harvest",
                "schedule": {"type": "interval", "every_sec": 3600},
            },
            428,
        ),  # needs a person's confirm
        (YOLO_SETTINGS, {"values": {"offer_exec": False}, "base_version": yolo_version}, 200),
    ]


def _run_valid(app, token):
    client = app.test_client()
    store = app.extensions["config_store"]
    before = _overrides(store)
    seen = []
    for index, (path, body, expected) in enumerate(_valid_requests(app)):
        if path.startswith("/api/yolo/settings"):
            # The chat turn above runs in the background; a settings save is refused
            # while a turn runs (409 turn_running). Wait for its end (a signal, not a
            # fixed sleep: under load the cancelled turn may take a moment to finish).
            import time

            turn_lock = app.extensions.get("turn_lock")
            deadline = time.monotonic() + 30.0
            while turn_lock is not None and turn_lock.locked():
                assert time.monotonic() < deadline, "the chat turn did not end"
                time.sleep(0.01)
        response = client.open(
            path,
            method="POST",
            json=body,
            headers={"X-CSRF-Token": token, "Idempotency-Key": f"valid-{index}"},
        )
        seen.append((path, response.status_code))
        if expected is not None:
            assert response.status_code == expected, (path, response.get_data(as_text=True)[:300])
        assert _overrides(store) == before, path
    return seen


def test_valid_requests_reach_the_handlers_and_leave_the_overrides(server_app):
    app, token = server_app
    assert len(_run_valid(app, token)) == 9

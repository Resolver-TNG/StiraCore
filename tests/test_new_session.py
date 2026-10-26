"""POST /api/chat/session/new and the read-only context window (#350 phase 3).

Real chat server app (``create_app``) on an offline FakeModel: no Bedrock call, no network.
What is measured here is what docs/stage-main-ui.md §11 measured for a restart: the
number of messages the model receives, the history, and what survives.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from unittest.mock import Mock

import pytest

from stiracore.agent import nxp_agent, stimulus_classifier
from stiracore.agent.context_management import (
    ContextManagementSettings,
    resolve_context_window,
    resolve_context_window_source,
)
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.core import NeuralState
from stiracore.observability.chat_api import request_digest
from stiracore.observability.config_api import ConfigStore
from stiracore.tools import tool_registry
from tests.provider_fakes import FakeModel, text_turn

USAGE = {"inputTokens": 100, "outputTokens": 5, "totalTokens": 105}
NEW = "/api/chat/session/new"


class LoopModel(FakeModel):
    """Answers every call; ``during`` runs inside the model call (i.e. inside a turn)."""

    def __init__(self, *args, **kwargs):
        super().__init__([], model_id="fake")
        self.during = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.during is not None:
            self.during()
        if not self.script:
            self.script.append(text_turn("reply", usage=dict(USAGE)))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def make_stira(tmp_path, monkeypatch):
    """The live app on the offline model (a generator; shared by the concurrency tests)."""
    from dashboard import stira_server

    model = LoopModel()
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(stimulus_classifier, "get_classifier", lambda: RuleBasedClassifier())
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    app = stira_server.create_app(config_store=store)
    client = app.test_client()
    token = app.config["STIRA_CSRF_TOKEN"]
    keys = iter(range(10_000))

    def chat(message):
        response = client.post(
            "/api/chat",
            json={"message": message},
            headers={"X-CSRF-Token": token, "Idempotency-Key": f"chat-{next(keys)}"},
        )
        body = response.get_data(as_text=True)
        assert "event: turn\n" in body, body[-400:]
        return body

    def new(key="new-1", **headers):
        return client.post(NEW, headers={"X-CSRF-Token": token, "Idempotency-Key": key, **headers})

    yield {
        "app": app,
        "client": client,
        "token": token,
        "model": model,
        "chat": chat,
        "new": new,
        "data": tmp_path / "data",
    }
    stira_server.shutdown_app(app)


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _history(client):
    return client.get("/api/chat/history").get_json()


def _messages_at_model(model):
    return len(model.requests[-1]["messages"])


def _agent(app):
    return app.extensions["new_session"]._agent


def test_new_resets_only_the_conversation_like_a_restart(stira):
    app, client, model = stira["app"], stira["client"], stira["model"]
    stira["chat"]("hello there")
    stira["chat"]("second message")
    assert _messages_at_model(model) == 3  # the second turn carries the first (§11)
    agent = _agent(app)
    before = {
        "state": agent.state.to_dict(),
        "breaker": (
            agent.engine.breaker_status().level,
            agent.engine._last_break_level,
            agent.engine._level_turns,
            agent.engine._critical_turns,
        ),
        "status": client.get("/api/gateway/status").get_json(),
        "approvals": client.get("/api/hitl/history").get_json()["history"],
        "turns": client.get("/api/turns").get_json()["turns"],
        "checkpoints": sorted(p.name for p in (stira["data"] / "checkpoints").rglob("*")),
    }
    episodes = before["status"]["memory"]["episodic_count"]
    assert episodes == 2
    assert len(_history(client)["turns"]) == 2

    response = stira["new"]()
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["status"] == "started"
    assert body["session"]["reason"] == "new"
    assert body["session"]["cleared"] == 2
    assert body["session"]["request"] == request_digest("new-1")

    history = _history(client)
    assert history["turns"] == []
    assert history["session"]["id"] == body["session"]["id"]
    assert history["session"]["reason"] == "new"
    after = client.get("/api/gateway/status").get_json()
    # kept: Neural State (values and decay clock), breaker, memory, SOUL, approvals, turn records
    assert agent.state.to_dict() == before["state"]
    assert (
        agent.engine.breaker_status().level,
        agent.engine._last_break_level,
        agent.engine._level_turns,
        agent.engine._critical_turns,
    ) == before["breaker"]
    assert after["neural_state"] == before["status"]["neural_state"]
    assert after["breaker"] == before["status"]["breaker"]
    assert after["memory"] == before["status"]["memory"]
    assert after["soul"] == before["status"]["soul"]
    assert client.get("/api/hitl/history").get_json()["history"] == before["approvals"]
    assert client.get("/api/turns").get_json()["turns"] == before["turns"]
    assert (
        sorted(p.name for p in (stira["data"] / "checkpoints").rglob("*")) == before["checkpoints"]
    )
    # the session counters restart with the conversation (as at a restart)
    assert after["session"]["total_input_tokens"] == 0

    stira["chat"]("a fresh start")
    assert _messages_at_model(model) == 1  # the same as after a restart (§11)
    assert model.requests[-1]["messages"][0]["content"][0]["text"] == "a fresh start"
    assert len(_history(client)["turns"]) == 1
    # memory is written the same way as before (one episode per turn)
    assert client.get("/api/gateway/status").get_json()["memory"]["episodic_count"] == 3


def test_the_audit_records_who_and_when_never_the_text(stira):
    stira["chat"]("my secret plan is pineapple")
    assert stira["new"]().status_code == 200
    rows = (
        sqlite3.connect(stira["data"] / "events.db")
        .execute("SELECT event_type, status, message, attributes_json, timestamp FROM events")
        .fetchall()
    )
    # the reset row first (written before anything changed), then the outcome
    assert [(r[0], r[1]) for r in rows] == [
        ("session.reset", "success"),
        ("session.started", "success"),
    ]
    reset, started = (json.loads(r[3]) for r in rows)
    for data in (reset, started):
        assert data["principal"] == "local:operator"
        assert data["remote"] == "127.0.0.1"
        assert data["previous_turns"] == 1
        assert data["request"] == request_digest("new-1")
    assert started["previous_messages"] == 2
    assert reset["session_id"] == started["session_id"]
    assert all(r[4] for r in rows)
    assert "pineapple" not in json.dumps(rows, ensure_ascii=False)
    ok, error = stira["app"].extensions["new_session"]._events.verify_chain()
    assert ok, error


def test_the_same_key_never_clears_twice(stira):
    client = stira["client"]
    stira["chat"]("one")
    assert stira["new"]("k").status_code == 200
    stira["chat"]("after the reset")
    first = _history(client)["session"]["id"]
    again = stira["new"]("k")
    assert again.status_code == 409
    assert again.get_json()["code"] == "duplicate_request"
    # the duplicate names the session this key started (the client re-reads the history)
    assert again.get_json()["session"] == {"id": first, "current": True}
    assert [t["message"] for t in _history(client)["turns"]] == ["after the reset"]
    assert _agent(stira["app"]).agent is not None  # the conversation is still there
    # another key is another reset; the old key now names a session that is not current
    assert stira["new"]("k2").status_code == 200
    assert _history(client)["turns"] == []
    assert stira["new"]("k").get_json()["session"] == {"id": first, "current": False}


def test_refused_while_a_turn_runs_even_from_inside_the_turn(stira):
    """Anything the agent runs (a tool, a delegate) runs inside a turn: the lock refuses it."""
    client, token, model = stira["client"], stira["token"], stira["model"]
    stira["chat"]("first")
    seen = []

    def from_inside():
        response = client.post(
            NEW, headers={"X-CSRF-Token": token, "Idempotency-Key": f"in-{len(seen)}"}
        )
        seen.append((response.status_code, response.get_json()["code"]))

    model.during = from_inside
    stira["chat"]("second")
    model.during = None
    assert seen == [(409, "turn_running")]
    assert len(_history(client)["turns"]) == 2
    assert _messages_at_model(model) == 3


def test_refused_while_an_approval_is_pending_and_kept_afterwards(stira):
    app, client = stira["app"], stira["client"]
    stira["chat"]("hello")
    store = app.extensions["approval_store"]
    store.submit(
        request_id="req-1",
        agent_id="stira",
        tool_name="file_write",
        tool_input={"path": "a.txt", "content": "x"},
        expires_at=time.time() + 600,
        checkpoint_id="cp-1",
    )
    assert any(r["id"] == "req-1" for r in client.get("/api/hitl/pending").get_json()["requests"])
    refused = stira["new"]("p1")
    assert refused.status_code == 409
    assert refused.get_json()["code"] == "approval_pending"
    assert len(_history(client)["turns"]) == 1
    store.cancel("req-1", reason="test")
    history_before = client.get("/api/hitl/history").get_json()["history"]
    # the refusal released the key: the same key runs now
    assert stira["new"]("p1").status_code == 200
    assert client.get("/api/hitl/history").get_json()["history"] == history_before


def test_refused_while_an_approved_resume_waits_for_the_lock(stira):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("hello")
    host.resume_scheduled()
    refused = stira["new"]("r1")
    assert (refused.status_code, refused.get_json()["code"]) == (409, "resume_pending")
    host.resume_finished()
    assert stira["new"]("r1").status_code == 200


def test_a_failed_audit_write_resets_nothing(stira):
    """Blocker 2: readiness passes, the write itself fails -> 500 audit_failed, nothing lost."""
    host, client, model = (
        stira["app"].extensions["new_session"],
        stira["client"],
        stira["model"],
    )
    stira["chat"]("one")
    stira["chat"]("two")
    calls = []

    def broken(event):
        calls.append(event.event_type)
        raise OSError("disk full")

    real = host._events.emit
    host._events.emit = broken
    try:
        response = stira["new"]("af")
    finally:
        host._events.emit = real
    assert host._events.healthy  # the readiness check passed
    assert calls == ["session.reset"]
    assert (response.status_code, response.get_json()["code"]) == (500, "audit_failed")
    assert len(_history(client)["turns"]) == 2
    assert _agent(stira["app"]).agent is not None
    stira["chat"]("three")
    assert _messages_at_model(model) == 5  # the conversation was kept
    # the refusal did not use the key up
    assert stira["new"]("af").status_code == 200


def test_the_reset_row_is_durable_before_the_reset(stira):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("one")
    seen = []
    original = host.reset

    def reset():
        rows = sqlite3.connect(stira["data"] / "events.db").execute("SELECT event_type FROM events")
        seen.append([r[0] for r in rows])
        return original()

    host.reset = reset
    assert stira["new"]("d1").status_code == 200
    assert seen == [["session.reset"]]


def test_refused_when_the_audit_cannot_be_written(stira):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("hello")
    real = host._events
    host._events = Mock(healthy=False, error="database error (DatabaseError)")
    try:
        refused = stira["new"]("a1")
    finally:
        host._events = real
    assert (refused.status_code, refused.get_json()["code"]) == (503, "audit_unavailable")
    assert len(_history(stira["client"])["turns"]) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"X-CSRF-Token": None, "X-Test-No-Csrf": "1"},  # conftest adds the token otherwise
        {"X-CSRF-Token": "wrong"},
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost:9999"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "evil.example"},
    ],
)
def test_refused_without_csrf_or_from_another_origin(stira, headers):
    client, token = stira["client"], stira["token"]
    stira["chat"]("hello")
    sent = {"X-CSRF-Token": token, "Idempotency-Key": "x1", **headers}
    sent = {k: v for k, v in sent.items() if v is not None}
    assert client.post(NEW, headers=sent).status_code == 403
    assert len(_history(client)["turns"]) == 1


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ('{"confirm": true}', "application/json"),
        ("[]", "application/json"),
        ("hello", "text/plain"),
        ('{"turns": []}', "application/json"),
    ],
)
def test_the_body_is_empty_or_an_empty_object(stira, body, content_type):
    client, token = stira["client"], stira["token"]
    stira["chat"]("hello")
    response = client.post(
        NEW,
        data=body,
        content_type=content_type,
        headers={"X-CSRF-Token": token, "Idempotency-Key": "b1"},
    )
    assert (response.status_code, response.get_json()["code"]) == (400, "invalid_body")
    assert len(_history(client)["turns"]) == 1
    ok = client.post(
        NEW, json={}, headers={"X-CSRF-Token": token, "Idempotency-Key": "b1"}
    )  # the refusal released the key
    assert ok.status_code == 200


def test_the_idempotency_key_is_required(stira):
    client, token = stira["client"], stira["token"]
    stira["chat"]("hello")
    response = client.post(NEW, headers={"X-CSRF-Token": token})
    assert (response.status_code, response.get_json()["code"]) == (400, "idempotency_key_required")
    assert len(_history(client)["turns"]) == 1


def test_a_concurrent_turn_and_reset_never_interleave(stira):
    """While a reset holds the lock a chat send is "busy"; while a turn runs, /new is refused."""
    client, token, model = stira["client"], stira["token"], stira["model"]
    stira["chat"]("hello")
    gate, entered = threading.Event(), threading.Event()
    agent = _agent(stira["app"])
    original = agent.start_new_conversation

    def slow():
        entered.set()
        gate.wait(5)
        return original()

    agent.start_new_conversation = slow
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("r", stira["new"]("c1")))
    worker.start()
    assert entered.wait(5)
    busy = client.post(
        "/api/chat",
        json={"message": "while resetting"},
        headers={"X-CSRF-Token": token, "Idempotency-Key": "c-chat"},
    ).get_data(as_text=True)
    gate.set()
    worker.join(5)
    assert result["r"].status_code == 200
    assert "Agent is busy" in busy
    assert _history(client)["turns"] == []
    count = len(model.requests)
    stira["chat"]("after")
    assert len(model.requests) == count + 1
    assert _messages_at_model(model) == 1


def test_context_window_is_read_only_status_with_the_shared_resolution(stira):
    status = stira["client"].get("/api/gateway/status").get_json()
    agent = _agent(stira["app"])
    expected = resolve_context_window(agent.context_management, agent.model, agent.capabilities)
    assert status["context_window"] == {
        "tokens": expected,
        "source": "default",  # the offline model has no window; nothing configured
        "compression_threshold": 0.85,
    }
    assert status["context_management"]["enabled"] is False
    # follows the settings (as summarization would)
    agent.context_management = ContextManagementSettings(
        enabled=True, context_window_tokens=64_000, compression_threshold=0.7
    )
    status = stira["client"].get("/api/gateway/status").get_json()
    assert status["context_window"] == {
        "tokens": 64_000,
        "source": "setting",
        "compression_threshold": 0.7,
    }
    assert status["context_management"]["enabled"] is True


def test_context_window_source_order():
    off = ContextManagementSettings()

    class WithLimit:
        context_window_limit = 32_768

    class Caps:
        context_window = 128_000

    assert resolve_context_window_source(off, None, None) == (200_000, "default")
    assert resolve_context_window_source(off, None, Caps()) == (128_000, "capability")
    assert resolve_context_window_source(off, WithLimit(), Caps()) == (32_768, "model")
    explicit = ContextManagementSettings(context_window_tokens=50_000)
    assert resolve_context_window_source(explicit, WithLimit(), Caps()) == (50_000, "setting")
    for settings, model, caps in ((off, None, None), (off, WithLimit(), Caps())):
        assert (
            resolve_context_window(settings, model, caps)
            == resolve_context_window_source(settings, model, caps)[0]
        )


def _tool_call(tool_use_id, tokens):
    return [
        {
            "toolUse": {"name": "current_time", "toolUseId": tool_use_id, "input": {}},
            "usage": {"inputTokens": tokens, "outputTokens": 10, "totalTokens": tokens + 10},
        }
    ]


def test_the_turn_reports_its_last_model_call_next_to_the_sum(stira):
    """Three model calls in one turn: input_tokens sums them, context_tokens is the last."""
    model, client = stira["model"], stira["client"]
    model.script = [
        _tool_call("t1", 1000),
        _tool_call("t2", 1100),
        text_turn("done", usage={"inputTokens": 1250, "outputTokens": 5, "totalTokens": 1255}),
    ]
    calls = len(model.requests)
    body = stira["chat"]("what time is it")
    assert len(model.requests) == calls + 3
    turn = next(b for b in body.split("\n\n") if b.startswith("event: turn\n"))
    data = json.loads(turn.split("data: ", 1)[1])
    assert data["input_tokens"] == 3350
    assert data["context_tokens"] == 1250
    assert _history(client)["turns"][-1]["context_tokens"] == 1250


def test_context_tokens_is_the_sdks_latest_context_size():
    """Same value as Strands' own latest_context_size (the last cycle's prompt)."""
    from stiracore.observability.chat_api import _last_call_prompt_tokens

    agent = nxp_agent.NXPAgent(
        model=FakeModel(
            [
                _tool_call("a", 700),
                _tool_call("b", 800),
                text_turn("x", usage={"inputTokens": 900, "outputTokens": 1, "totalTokens": 901}),
            ]
        ),
        tools=[*tool_registry.get_tools(None)],
        stimulus_classifier=RuleBasedClassifier(),
    )
    result = agent("go")
    metrics = result.metrics
    assert len(metrics.agent_invocations[-1].cycles) == 3
    assert metrics.accumulated_usage["inputTokens"] == 2400
    assert metrics.latest_context_size == 900
    assert _last_call_prompt_tokens(metrics, agent.provider) == 900


def test_last_call_tokens_follow_the_provider_cache_semantics():
    from stiracore.observability.chat_api import _last_call_prompt_tokens

    def metrics(*usages):
        cycles = [Mock(usage=u) for u in usages]
        return Mock(agent_invocations=[Mock(cycles=cycles)])

    bedrock = {"inputTokens": 50, "outputTokens": 1, "cacheReadInputTokens": 900}
    assert _last_call_prompt_tokens(metrics({"inputTokens": 9}, bedrock), "bedrock") == 950
    openai = {"inputTokens": 950, "outputTokens": 1, "cacheReadInputTokens": 900}
    assert _last_call_prompt_tokens(metrics(openai), "openai") == 950
    assert _last_call_prompt_tokens(Mock(agent_invocations=[]), "bedrock") is None
    assert _last_call_prompt_tokens({}, "bedrock") is None
    assert _last_call_prompt_tokens(metrics({"outputTokens": 1}), "bedrock") is None


def test_start_new_conversation_drops_only_the_history():
    agent = nxp_agent.NXPAgent(
        model=FakeModel([text_turn("a"), text_turn("b")]),
        state=NeuralState(stability=0.42),
        stimulus_classifier=RuleBasedClassifier(),
    )
    agent("one")
    agent("two")
    state = agent.state.to_dict()
    assert len(agent.agent.messages) == 4
    agent.compaction_guard.reinjection_count = 3
    assert agent.start_new_conversation() == 4
    assert agent.agent is None
    assert agent.engine.conversation_history == []
    assert agent.compaction_guard.reinjection_count == 0
    assert agent.state.to_dict() == state
    # a restored checkpoint's messages go too
    agent._restored_messages = [{"role": "user", "content": [{"text": "old"}]}]
    assert agent.start_new_conversation() == 1
    assert agent._restored_messages is None
    with_manager = nxp_agent.NXPAgent(
        model=FakeModel(), session_manager=Mock(state_persistence=None)
    )
    with pytest.raises(RuntimeError):
        with_manager.start_new_conversation()


def test_no_route_without_a_host():
    from flask import Flask

    from stiracore.observability.chat_api import create_chat_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(Mock()))
    assert NEW not in {rule.rule for rule in app.url_map.iter_rules()}
    with pytest.raises(TypeError):
        create_chat_blueprint(Mock(), memory_store=Mock(), new_session=Mock())

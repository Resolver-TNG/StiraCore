import hashlib
import hmac
import json
import threading
import time
from unittest.mock import Mock

import pytest
from flask import Flask

from stiracore.integrations.webhook import MAX_WEBHOOK_BYTES, WebhookReceiver


@pytest.fixture
def ingress(monkeypatch):
    for provider in ("generic", "github", "slack"):
        monkeypatch.setenv(f"WEBHOOK_SECRET_{provider.upper()}", "test-secret")
    runner = Mock(return_value="PRIVATE AGENT RESPONSE")
    lock = threading.Lock()
    app = Flask(__name__)
    app.register_blueprint(WebhookReceiver(runner=runner, turn_lock=lock))
    return app.test_client(), runner, lock


def signed(client, provider="generic", payload=None, *, timestamp=None, body=None):
    body = body if body is not None else json.dumps(payload or {"event": "created"}).encode()
    if provider == "slack":
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        digest = hmac.new(b"test-secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
        headers = {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": "v0=" + digest.hexdigest(),
        }
    else:
        digest = hmac.new(b"test-secret", body, hashlib.sha256)
        headers = {"X-Hub-Signature-256": "sha256=" + digest.hexdigest()}
    return client.post(
        f"/api/webhooks/{provider}", data=body, content_type="application/json", headers=headers
    )


@pytest.mark.parametrize("provider", ["generic", "github", "slack"])
def test_signed_event_runs_exactly_one_turn(ingress, provider):
    client, runner, _ = ingress
    response = signed(client, provider)
    assert response.status_code == 200
    runner.assert_called_once()
    prompt = runner.call_args.args[0]
    assert "untrusted" in prompt and provider in prompt and "created" in prompt
    assert "PRIVATE" not in response.text
    assert signed(client, provider).json["status"] == "duplicate"
    runner.assert_called_once()


def test_invalid_missing_signatures_and_unknown_provider(ingress, monkeypatch):
    client, runner, _ = ingress
    assert client.post("/api/webhooks/generic", json={}).status_code == 401
    assert (
        client.post(
            "/api/webhooks/github", json={}, headers={"X-Hub-Signature-256": "sha256=bad"}
        ).status_code
        == 401
    )
    assert client.post("/api/webhooks/unknown", json={}).status_code == 404
    monkeypatch.delenv("WEBHOOK_SECRET_GENERIC")
    assert signed(client).status_code == 503
    runner.assert_not_called()


@pytest.mark.parametrize("timestamp", ["bad", 0, 99999999999])
def test_slack_rejects_stale_or_bad_timestamp(ingress, timestamp):
    client, runner, _ = ingress
    assert signed(client, "slack", timestamp=timestamp).status_code == 401
    runner.assert_not_called()


def test_slack_challenge_is_verified_and_not_a_turn(ingress):
    client, runner, _ = ingress
    payload = {"type": "url_verification", "challenge": "example"}
    assert signed(client, "slack", payload).json == {"challenge": "example"}
    assert client.post("/api/webhooks/slack", json=payload).status_code == 401
    runner.assert_not_called()


@pytest.mark.parametrize("body", [b"[]", b"null", b"invalid"])
def test_json_validation(ingress, body):
    client, runner, _ = ingress
    assert signed(client, body=body).status_code == 400
    runner.assert_not_called()


def test_size_busy_and_failed_turn_can_retry(ingress):
    client, runner, lock = ingress
    assert signed(client, body=b"x" * (MAX_WEBHOOK_BYTES + 1)).status_code == 413
    with lock:
        response = signed(client)
        assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    runner.assert_not_called()
    runner.side_effect = RuntimeError("SECRET")
    response = signed(client)
    assert response.status_code == 503 and "SECRET" not in response.text
    runner.side_effect = None
    assert signed(client).status_code == 200
    assert not lock.locked()


def test_observer_registers_receiver_without_creating_agent(tmp_path, monkeypatch):
    from dashboard.app import create_app

    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "test-secret")
    app = create_app(state_path=tmp_path / "state.json")
    assert signed(app.test_client()).status_code == 503
    agent = Mock(return_value="ok")
    app = create_app(state_path=tmp_path / "state.json", chat_agent=agent)
    assert signed(app.test_client()).status_code == 200
    agent.assert_called_once()


def test_chat_and_webhook_share_turn_lock(ingress):
    from stiracore.observability.chat_api import create_chat_blueprint

    _, runner, lock = ingress
    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(runner, runner=runner, turn_lock=lock))
    app.register_blueprint(WebhookReceiver(runner=runner, turn_lock=lock))
    with lock:
        response = app.test_client().post("/api/chat", json={"message": "hello"})
        assert "Agent is busy" in response.text
        assert signed(app.test_client()).status_code == 503
    runner.assert_not_called()


def test_webhook_enters_real_nxp_lifecycle_once(monkeypatch):
    from unittest.mock import MagicMock

    from stiracore.agent import nxp_agent
    from stiracore.agent.stimulus_classifier import RuleBasedClassifier

    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "test-secret")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())
    inner = MagicMock()
    inner.messages = []
    inner.return_value = "offline reply"
    monkeypatch.setattr(nxp_agent, "Agent", Mock(return_value=inner))
    agent = nxp_agent.NXPAgent(
        model_id="offline", region_name="us-east-1", stimulus_classifier=RuleBasedClassifier()
    )
    agent.engine.pre_turn = Mock(wraps=agent.engine.pre_turn)
    agent.engine.post_turn = Mock(wraps=agent.engine.post_turn)
    app = Flask(__name__)
    app.register_blueprint(WebhookReceiver(agent))
    assert signed(app.test_client()).status_code == 200
    agent.engine.pre_turn.assert_called_once()
    agent.engine.post_turn.assert_called_once()
    inner.assert_called_once()

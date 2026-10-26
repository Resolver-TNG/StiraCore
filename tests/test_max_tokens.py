from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask
from strands.types.exceptions import MaxTokensReachedException

from stiracore.agent import nxp_agent
from stiracore.config import GatewayConfig, resolve_config
from stiracore.core import BreakAction, BreakLevel, BreakResult
from stiracore.observability.chat_api import create_chat_blueprint


def test_gateway_config_round_trips_max_output_tokens(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    config = GatewayConfig(max_output_tokens=6144, path=path)

    config.save()

    loaded = GatewayConfig.load(path)

    assert loaded.max_output_tokens == 6144
    assert resolve_config(loaded, environ={}).max_output_tokens == 6144


def test_nxp_agent_passes_max_output_tokens_to_bedrock(monkeypatch) -> None:
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(nxp_agent, "BedrockModel", constructor)

    nxp_agent.NXPAgent(
        model_id="offline-model",
        region_name="us-east-1",
        max_output_tokens=6144,
    )

    constructor.assert_called_once_with(
        model_id="offline-model",
        region_name="us-east-1",
        max_tokens=6144,
    )


def test_max_tokens_failure_closes_turn_with_neutral_state(monkeypatch) -> None:
    monkeypatch.setattr(nxp_agent, "SoulCompactionGuard", Mock())
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    inner = Mock()
    inner.messages = []
    error = MaxTokensReachedException("maximum output reached")
    inner.side_effect = error
    monkeypatch.setattr(nxp_agent, "Agent", Mock(return_value=inner))

    wrapper = nxp_agent.NXPAgent("offline-model", "us-east-1")
    engine = Mock()
    engine.pre_turn.return_value = BreakResult(BreakLevel.NORMAL, BreakAction.NONE)
    wrapper.engine = engine

    with pytest.raises(MaxTokensReachedException) as caught:
        wrapper("write a long answer")

    assert caught.value is error
    engine.post_turn.assert_called_once_with(stimulus=None)


def test_chat_api_reports_max_tokens_separately_from_generic_error() -> None:
    app = Flask(__name__)

    def runner(_message: str):
        raise MaxTokensReachedException("maximum output reached")

    app.register_blueprint(create_chat_blueprint(object(), runner=runner))

    response = app.test_client().post("/api/chat", json={"message": "long answer"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "出力の上限に達して途中で切れました" in body
    assert '"code": "max_tokens"' in body
    assert "応答処理に失敗しました" not in body


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192", None])
def test_invalid_max_output_tokens_rejected(value, monkeypatch) -> None:
    constructor = Mock()
    monkeypatch.setattr(nxp_agent, "BedrockModel", constructor)
    if value is not None:  # None = automatic for the gateway file (#363); the agent needs a number
        with pytest.raises(ValueError, match="positive integer"):
            GatewayConfig(max_output_tokens=value)
    with pytest.raises(ValueError, match="positive integer"):
        nxp_agent.create_nxp_agent("offline-model", "us-east-1", max_output_tokens=value)
    constructor.assert_not_called()


def test_max_tokens_error_releases_chat_generation_lock() -> None:
    app = Flask(__name__)
    runner = Mock(side_effect=[MaxTokensReachedException("maximum reached"), "continued"])
    app.register_blueprint(create_chat_blueprint(object(), runner=runner))
    client = app.test_client()
    assert "cut off" in client.post("/api/chat", json={"message": "long answer"}).get_data(
        as_text=True
    )
    body = client.post("/api/chat", json={"message": "continue"}).get_data(as_text=True)
    assert "continued" in body
    assert "event: turn" in body
    assert "Agent is busy" not in body

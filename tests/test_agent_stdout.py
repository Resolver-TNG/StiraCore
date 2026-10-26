"""Integration bug hunt 2026-10-15: the chat server never prints model output to stdout.

The SDK's default callback handler prints every streamed text delta (and "Tool #n:
name") to stdout. NXPAgent built its per-turn Agent without a handler, so a running
stira_server wrote each reply, unmasked and unbounded, into its stdout log
(scripts/stira_ctl.sh: .local/stira.out.log). The UI gets turns through hooks.
"""

from __future__ import annotations

from unittest.mock import Mock

from stiracore import NeuralState, NXPAgent
from tests.provider_fakes import FakeModel, text_turn

REPLY = "reply-token-AKIAIOSFODNN7EXAMPLE"


def _agent(**kwargs):
    return NXPAgent(
        model=FakeModel([text_turn(REPLY)]),
        state=NeuralState(stability=0.9),
        stimulus_classifier=Mock(classify=lambda **_kw: None),
        **kwargs,
    )


def test_callback_handler_none_keeps_model_text_off_stdout(capsys):
    result = _agent(callback_handler=None)("hi")
    assert REPLY in str(result)
    assert REPLY not in capsys.readouterr().out


def test_without_the_argument_the_sdk_default_is_unchanged(capsys):
    """Library callers (CLI examples) keep the SDK's printing default."""
    _agent()("hi")
    assert REPLY in capsys.readouterr().out


def test_a_custom_handler_receives_the_stream():
    seen: list[str] = []

    def handler(**event):
        if "data" in event:
            seen.append(event["data"])

    _agent(callback_handler=handler)("hi")
    assert REPLY in "".join(seen)


def test_stira_server_builds_its_agent_without_the_printing_handler(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.observability.config_api import ConfigStore

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), state=None, config=None, **kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(kwargs)

    data_dir = tmp_path / "stira"
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    stira_server.create_app(store)
    assert "callback_handler" in captured and captured["callback_handler"] is None


def test_chat_bot_sessions_build_their_agent_without_the_printing_handler(monkeypatch, tmp_path):
    """Slack/Discord replies to remote users never land in the bot's stdout either."""
    from unittest.mock import Mock as _Mock

    from stiracore.agent import nxp_agent
    from stiracore.config import AgentConfig, GatewayConfig
    from stiracore.integrations import runtime

    monkeypatch.setattr(nxp_agent, "BedrockModel", _Mock())
    monkeypatch.setenv("STIRA_CHAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime.GatewayConfig, "load", lambda: GatewayConfig())
    monkeypatch.setattr(runtime.AgentConfig, "load", lambda: AgentConfig())
    session = runtime.create_session("slack", "T1:C1")
    assert session.agent._callback_kwargs == {"callback_handler": None}

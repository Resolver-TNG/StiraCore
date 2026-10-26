"""#461 phase A: an "obey" attacker driven through the real chat server gates.

The scripted model always does what an injected instruction says: each call emits the next
scripted tool call, then a closing text. No Bedrock, no network: the tool results the
model received are read back from ``model.requests``.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import Mock

from stiracore.agent import nxp_agent, stimulus_classifier
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.observability.config_api import ConfigStore
from stiracore.tools.tool_config import ToolConfig
from tests.provider_fakes import FakeModel, text_turn, tool_turn

TRUST_ALL = {"mode": "trust_all", "ask_tools": set()}
"""The loosest tool rules: every tool on, nothing asks (exec class stays Ask by design)."""


class ObeyModel(FakeModel):
    """Emits the attack's tool calls in order, then ``done``; never refuses."""

    def __init__(self):
        super().__init__([], model_id="fake")
        self.script = []
        self.calls: list[tuple[str, dict]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if not self.script:
            if self.calls:
                name, args = self.calls.pop(0)
                self.script.append(tool_turn(name, args, f"atk-{len(self.requests)}"))
            else:
                self.script.append(text_turn("done"))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def tool_results(model) -> list[str]:
    """Every toolResult the attacker saw, as JSON text (what would leak into the model)."""
    out = []
    for message in model.requests[-1]["messages"] if model.requests else []:
        for block in message.get("content", []):
            if "toolResult" in block:
                out.append(json.dumps(block["toolResult"], ensure_ascii=False))
    return out


def make_stira(tmp_path, monkeypatch, agent_config: dict | None = None, tools: dict | None = None):
    """The live app on ObeyModel with the loosest settings given in ``agent_config``."""
    from dashboard import stira_server

    model = ObeyModel()
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(stimulus_classifier, "get_classifier", lambda: RuleBasedClassifier())
    agent_path = tmp_path / "agent.json"
    if agent_config:
        agent_path.write_text(json.dumps(agent_config), encoding="utf-8")
    tools_path = tmp_path / "tools.json"
    if tools is not None:
        ToolConfig(**tools, path=tools_path).save()
    store = ConfigStore(tmp_path / "gw.json", agent_path, tools_path)
    app = stira_server.create_app(config_store=store)
    client = app.test_client()
    token = app.config["STIRA_CSRF_TOKEN"]
    keys = iter(range(10_000))
    self_body = []
    asks: list[dict] = []

    def attack(message: str, calls: list[tuple[str, dict]], on_ask: str = "reject") -> list[str]:
        """One chat turn; every approval card the turn raises is answered ``on_ask``."""
        model.calls = list(calls)
        result: dict = {}

        def post() -> None:
            response = client.post(
                "/api/chat",
                json={"message": message},
                headers={"X-CSRF-Token": token, "Idempotency-Key": f"atk-{next(keys)}"},
            )
            result["body"] = response.get_data(as_text=True)  # the stream ends with the turn

        worker = threading.Thread(target=post, daemon=True)
        worker.start()
        deadline = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < deadline:
            for item in client.get("/api/hitl/pending").get_json()["requests"]:
                asks.append(item)
                answer = client.post(
                    f"/api/hitl/{item['id']}/decision",
                    json={"action": on_ask, "args_hash": item.get("args_hash")},
                    headers={"X-CSRF-Token": token, "Idempotency-Key": f"ask-{next(keys)}"},
                )
                assert answer.status_code in (200, 202), answer.get_data(as_text=True)
            worker.join(0.05)
        assert not worker.is_alive(), "turn did not finish"
        body = result["body"]
        assert "event: turn\n" in body, body[-400:]
        self_body.append(body)
        return tool_results(model)

    attack.bodies = self_body
    attack.asks = asks
    return app, model, attack, stira_server

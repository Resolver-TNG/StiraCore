"""#350 phase 2 review: the Stage's board and log show the conversation reply only.

The reply the Stage (and the chat page) shows is the text of ``POST /api/chat``'s ``delta``
events and the history's ``response``. Tool calls, tool results (file contents) and other
model events of the turn must not reach either (docs/stage-main-ui.md §6).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability.chat_api import create_chat_blueprint


def _events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            out.append((name, json.loads("\n".join(data))))
    return out


def test_deltas_and_history_carry_the_reply_text_only():
    secret = "FILE CONTENT SECRET-7f3a"

    def runner(_prompt):
        yield {"data": "Hello "}
        # what the SDK streams around a tool call: the call, its input, its result message
        yield {
            "current_tool_use": {"toolUseId": "t1", "name": "file_read", "input": '{"path": "x"}'}
        }
        yield {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": "file_read", "input": {}}}],
            }
        }
        yield {
            "message": {
                "role": "user",
                "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": secret}]}}],
            }
        }
        yield {"data": "world"}

    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(object(), runner=runner))
    client = app.test_client()
    body = client.post("/api/chat", json={"message": "read x"}).get_data(as_text=True)
    events = _events(body)
    deltas = [data["text"] for name, data in events if name == "delta"]
    assert deltas == ["Hello ", "world"]
    assert secret not in body
    history = client.get("/api/chat/history").get_json()["turns"]
    assert [turn["response"] for turn in history] == ["Hello world"]
    assert secret not in json.dumps(history)

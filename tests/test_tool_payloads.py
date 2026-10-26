"""#188: masked, capped tool input/output previews and paged replay for chat tool cards."""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

from flask import Flask

from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.hooks import InMemoryEventSink, create_dashboard_hooks
from stiracore.observability.turn_events import (
    PAGE_BYTES,
    PAGE_LINES,
    PAYLOAD_RETAIN_BYTES,
    PREVIEW_LIMIT,
    PREVIEW_LINES,
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    capture_payload,
)

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


def _hook_event(**kwargs):
    return SimpleNamespace(agent=SimpleNamespace(), invocation_state={}, **kwargs)


def _tool_calls(provider, tool_input, result):
    event = _hook_event(tool_use={"name": "shell", "toolUseId": "u1", "input": tool_input})
    event.cancel_tool = False
    provider.on_before_tool_call(event)
    done = _hook_event(
        tool_use={"name": "shell", "toolUseId": "u1", "input": tool_input},
        result=result,
        exception=None,
        retry=False,
        cancel_message=None,
    )
    done.invocation_state = event.invocation_state
    done.agent = event.agent
    provider.on_after_tool_call(done)


def test_capture_masks_secrets_and_reports_totals():
    captured = capture_payload("input", {"api_key": "zzz", "cmd": f"echo {AWS} Bearer abc.def"})
    assert "zzz" not in captured["text"] and AWS not in captured["text"]
    assert "abc.def" not in captured["text"]
    assert captured["total_lines"] == captured["text"].count("\n") + 1
    out = capture_payload(
        "output", {"status": "success", "content": [{"text": "a\nb"}, {"json": {"x": 1}}]}
    )
    assert out["text"] == 'a\nb\n{"x":1}' and out["total_lines"] == 3
    big = capture_payload("output", "x" * (PAYLOAD_RETAIN_BYTES + 10))
    assert big["retained_truncated"] is True
    assert len(big["text"].encode()) == PAYLOAD_RETAIN_BYTES
    assert big["total_chars"] == PAYLOAD_RETAIN_BYTES + 10
    assert capture_payload("other", "x") is None
    assert capture_payload("input", None) is None


def test_hooks_attach_payloads_only_when_opted_in():
    plain = InMemoryEventSink()
    _tool_calls(create_dashboard_hooks(plain), {"q": "PRIVATE"}, {"content": [{"text": "PRIVATE"}]})
    assert "PRIVATE" not in repr([asdict(e) for e in plain.get_events()])

    sink = InMemoryEventSink()
    provider = create_dashboard_hooks(sink, payload_capture=capture_payload)
    _tool_calls(provider, {"q": "hello", "token": "PRIVATE"}, {"content": [{"text": "world"}]})
    started, completed = sink.get_events()
    assert started.attributes["input"]["text"] == json.dumps(
        {"q": "hello", "token": "***"}, ensure_ascii=False, indent=2
    )
    assert completed.attributes["output"]["text"] == "world"
    assert "PRIVATE" not in repr([asdict(e) for e in sink.get_events()])


def test_failing_capture_never_breaks_the_tool_call():
    def boom(kind, value):
        raise RuntimeError("x")

    sink = InMemoryEventSink()
    _tool_calls(create_dashboard_hooks(sink, payload_capture=boom), {}, {"content": []})
    assert [e.event_type for e in sink.get_events()] == ["tool.started", "tool.completed"]
    assert "input" not in sink.get_events()[0].attributes


def _recorder(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    recorder.active.set("t1")
    return recorder


def test_ui_sink_previews_are_capped_flagged_and_stored(tmp_path):
    recorder = _recorder(tmp_path)
    provider = create_dashboard_hooks(UIEventSink(recorder), payload_capture=capture_payload)
    output = "\n".join(f"line {n} {AWS}" for n in range(450))
    _tool_calls(provider, {"cmd": "ls"}, {"status": "success", "content": [{"text": output}]})
    created, updated = recorder.store.events("t1")
    assert created["data"]["input_preview"].startswith("{")
    assert created["data"]["input_truncated"] is False and created["data"]["input_stored"]
    data = updated["data"]
    assert data["status"] == "done"
    assert data["output_truncated"] is True
    assert len(data["output_preview"]) == PREVIEW_LIMIT
    assert data["output_preview"].count("\n") < PREVIEW_LINES
    assert data["output_lines"] == 450 and data["output_chars"] > PREVIEW_LIMIT
    assert AWS not in json.dumps([created, updated])

    page = recorder.store.payload_page("t1", "u1", "output")
    assert page["text"].count("\n") + 1 == PAGE_LINES
    assert page["next_offset"] == PAGE_LINES and page["retained_lines"] == 450
    assert AWS not in page["text"]
    last = recorder.store.payload_page("t1", "u1", "output", offset=400)
    assert last["next_offset"] is None and last["text"].startswith("line 400")
    assert recorder.store.recent_turns(1)[0]["tool_count"] == 1


def test_pages_respect_the_byte_cap(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.store_payload("u2", "output", capture_payload("output", "y" * 1000 + "\n" + "z"))
    wide = "\n".join("w" * 1024 for _ in range(150))  # 150 KiB in 150 lines
    recorder.store_payload("u3", "output", capture_payload("output", wide))
    page = recorder.store.payload_page("t1", "u3", "output")
    assert len(page["text"].encode()) <= PAGE_BYTES
    assert page["next_offset"] is not None and page["next_offset"] < 150
    huge = "h" * (PAGE_BYTES * 2)
    recorder.store_payload("u4", "output", capture_payload("output", huge))
    single = recorder.store.payload_page("t1", "u4", "output")
    assert single["line_cut"] is True and single["next_offset"] is None
    assert len(single["text"].encode()) == PAGE_BYTES


def test_payload_endpoint(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.store_payload("u1", "output", capture_payload("output", f"ok {AWS}"))
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(SimpleNamespace(), runner=lambda m: "x", turn_recorder=recorder)
    )
    client = app.test_client()
    body = client.get("/api/turns/t1/tools/u1/output").get_json()
    assert body["text"] == "ok AKIA****" and body["next_offset"] is None
    assert client.get("/api/turns/t1/tools/u1/input").status_code == 404
    assert client.get("/api/turns/t1/tools/u1/other").status_code == 400
    assert client.get("/api/turns/t1/tools/u1/output?offset=-1").status_code == 400
    bare = Flask(__name__)
    bare.register_blueprint(create_chat_blueprint(SimpleNamespace(), runner=lambda m: "x"))
    assert bare.test_client().get("/api/turns/t1/tools/u1/output").status_code == 404

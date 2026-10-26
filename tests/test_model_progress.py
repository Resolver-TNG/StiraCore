"""#445: a model call's start, elapsed time and end on the turn events (fake model, fake clock)."""

from __future__ import annotations

import json

from strands import Agent

from stiracore.observability.hooks import create_dashboard_hooks
from stiracore.observability.model_progress import ModelCallProgress
from stiracore.observability.turn_events import (
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    validate_event,
)
from stiracore.observability.turns_view import page_event
from tests.provider_fakes import FakeModel, text_turn, tool_turn

SECRET_PROMPT = "write the private plan for project nightingale"
SECRET_ARG = "BODY-OF-A-LONG-FILE " * 50
SECRET_KEY = "sk-test-" + "a1b2" * 8


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SlowModel(FakeModel):
    """A fake model whose call takes ``seconds`` of the injected clock; the progress
    ticker runs once per simulated second (what its thread does in the server)."""

    def __init__(self, script, clock, progress, seconds):
        super().__init__(script)
        self.clock, self.progress, self.seconds = clock, progress, list(seconds)

    async def stream(self, *args, **kwargs):
        for _ in range(self.seconds.pop(0) if self.seconds else 0):
            self.clock.now += 1.0
            self.progress.tick()
        async for event in super().stream(*args, **kwargs):
            yield event


def _run(tmp_path, script, seconds, prompt=SECRET_PROMPT, tools=()):
    clock = Clock()
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    progress = ModelCallProgress(recorder, clock=clock, thread=False)
    sink = UIEventSink(recorder, progress=progress)
    model = SlowModel(script, clock, progress, seconds)
    agent = Agent(
        model=model,
        tools=list(tools),
        hooks=[create_dashboard_hooks(sink, clock=clock)],
        callback_handler=None,
    )
    recorder.active.set("turn-1")
    agent(prompt)
    recorder.active.clear()
    return recorder.store.events("turn-1"), progress


def test_a_35_second_call_shows_progress_and_its_duration(tmp_path):
    usage = {"inputTokens": 120, "outputTokens": 45, "totalTokens": 165}
    events, progress = _run(tmp_path, [text_turn("done " + SECRET_KEY, usage=usage)], [35])
    types = [e["type"] for e in events]
    assert types[0] == "model.started" and types[-1] == "model.completed"
    ticks = [e["data"] for e in events if e["type"] == "model.progress"]
    assert [t["elapsed_ms"] for t in ticks] == [10000, 20000, 30000]  # every 10 s
    started = events[0]["data"]
    done = events[-1]["data"]
    assert {t["call_id"] for t in ticks} == {started["call_id"]} == {done["call_id"]}
    assert done["duration_ms"] == 35000
    assert done["usage"] == {"input_tokens": 120, "output_tokens": 45}
    assert done["stop"] == "end_turn" and done["status"] == "success"
    assert started["model"] == done["model"] == "fake"
    assert progress.open_calls() == 0
    for event in events:
        validate_event(event)


def test_each_call_of_a_tool_turn_is_recorded_and_nothing_private_is(tmp_path):
    from strands import tool

    @tool
    def file_write(path: str, content: str) -> str:
        """Write a file."""
        return "ok"

    script = [
        tool_turn("file_write", {"path": "a.html", "content": SECRET_ARG + SECRET_KEY}),
        text_turn("written"),
    ]
    events, _ = _run(tmp_path, script, [31, 2], tools=[file_write])
    calls = [e["data"] for e in events if e["type"] == "model.completed"]
    assert [c["duration_ms"] for c in calls] == [31000, 2000]
    assert [c["stop"] for c in calls] == ["tool_use", "end_turn"]
    ticks = [e for e in events if e["type"] == "model.progress"]
    assert len(ticks) == 3  # 10 s, 20 s, 30 s of the first call; none for the 2 s call
    model_events = [e for e in events if e["type"].startswith("model.")]
    text = json.dumps(model_events)
    for secret in (SECRET_PROMPT, "BODY-OF-A-LONG-FILE", SECRET_KEY, "a.html", "written"):
        assert secret not in text
    for event in model_events:
        assert set(event["data"]) <= {
            "call_id",
            "model",
            "started_at",
            "elapsed_ms",
            "duration_ms",
            "usage",
            "stop",
            "status",
            "retry",
            "exception_type",
        }


def test_a_short_call_has_no_progress_event(tmp_path):
    events, _ = _run(tmp_path, [text_turn("hi")], [3])
    assert [e["type"] for e in events] == ["model.started", "model.completed"]


def test_progress_stops_when_the_turn_is_no_longer_active(tmp_path):
    clock = Clock()
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    progress = ModelCallProgress(recorder, clock=clock, thread=False)
    recorder.active.set("turn-a")
    progress.started("a" * 32, None)
    clock.now += 11
    assert progress.tick() == 1
    recorder.active.set("turn-b")  # turn-a ended without the call's end
    clock.now += 11
    assert progress.tick() == 0 and progress.open_calls() == 0


def test_the_ticker_thread_runs_while_a_call_is_open(tmp_path):
    import time

    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    progress = ModelCallProgress(recorder, interval_sec=0.05, poll_sec=0.02)
    recorder.active.set("turn-1")
    progress.started("b" * 32, "fake")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not recorder.store.events("turn-1"):
        time.sleep(0.02)
    progress.finished("b" * 32)
    assert recorder.store.events("turn-1")[0]["type"] == "model.progress"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and progress._thread is not None:
        time.sleep(0.02)
    assert progress._thread is None  # exits when no call is open


def test_observability_page_maps_model_calls_without_usage():
    base = {"event_id": "e" * 32, "turn_id": "t" * 32, "seq": 1, "ts": 5.0}
    start = page_event(
        1,
        {
            **base,
            "type": "model.started",
            "data": {"call_id": "c" * 32, "model": "fake", "started_at": 5.0},
        },
    )
    assert start["type"] == "model_call_start" and start["span_id"] == "c" * 32
    tick = page_event(
        2, {**base, "type": "model.progress", "data": {"call_id": "c" * 32, "elapsed_ms": 20000}}
    )
    assert tick["type"] == "model_call_progress" and tick["message"] == "20s"
    assert "status" not in tick  # neither a start nor an end of the span
    end = page_event(
        3,
        {
            **base,
            "type": "model.completed",
            "data": {
                "call_id": "c" * 32,
                "duration_ms": 35000,
                "usage": {"input_tokens": 1},
                "stop": "end_turn",
                "status": "success",
            },
        },
    )
    assert end["type"] == "model_call_end" and end["duration_ms"] == 35000
    assert end["message"] == "end_turn" and "usage" not in end

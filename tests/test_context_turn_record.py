"""#746: ``turn.completed.context`` records what context management did in the turn.

A mode, a refusal reason code, counts and the largest input of one model call; nothing
while the feature is off. Offline: scripted models that report usage.
"""

from __future__ import annotations

import json
from pathlib import Path

from stiracore.agent.context_management import ContextManagementSettings, turn_record
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_context_autonomy import (
    BIG,
    SECRET,
    ReportingFake,
    _agent,
    _loop_script,
    _reported,
)

ROOT = Path(__file__).resolve().parents[1]
SMALL_WINDOW = ContextManagementSettings(
    enabled=True, context_window_tokens=10_000, compression_threshold=0.85, preserve_recent=2
)


def _completed(tmp_path, agent, message="work through the files"):
    from flask import Flask

    from stiracore.observability.chat_api import create_chat_blueprint
    from stiracore.observability.security import install_security
    from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore

    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    app = Flask(__name__, template_folder=str(ROOT / "dashboard" / "templates"))
    install_security(app)
    app.register_blueprint(create_chat_blueprint(agent, runner=agent, turn_recorder=recorder))
    client = app.test_client()
    token = client.get("/api/csrf").get_json()["token"]
    client.post("/api/chat", json={"message": message}, headers={"X-CSRF-Token": token}).text  # noqa: B018
    stored = recorder.store.recent_turns(1)[0]
    events = recorder.store.events(stored["turn_id"])
    return next(e for e in events if e["type"] == "turn.completed")["data"]


def test_a_summarized_turn_is_recorded_with_its_largest_input(tmp_path):
    model = ReportingFake(_loop_script([600, 1_200, 2_400, 4_800, 9_000, 600, 700]))
    agent = _agent(model, settings=SMALL_WINDOW, context_stash_dir=tmp_path / "stash")
    context = _completed(tmp_path, agent)["context"]
    assert context["mode"] == "on" and "reason" not in context
    assert context["summaries"] == 1 and context["summarized_messages"] >= 1
    assert context["offloaded_results"] == 0
    assert context["max_input_tokens"] == 9_000
    assert set(context) <= {
        "mode",
        "summaries",
        "summarized_messages",
        "offloaded_results",
        "history_messages",
        "max_input_tokens",
    }


def test_an_offload_is_counted_and_nothing_of_the_result_is_recorded(tmp_path):
    script = [
        _reported(tool_turn("big_read", {"path": "x"}, "tu-1"), 600),
        _reported(text_turn("done"), 900),
    ]
    agent = _agent(FakeModel(script), context_stash_dir=tmp_path / "stash")
    completed = _completed(tmp_path, agent, "read it")
    assert completed["context"]["offloaded_results"] == 1
    assert completed["context"]["max_input_tokens"] == 900
    text = json.dumps(completed["context"])
    assert SECRET not in text and BIG[:40] not in text and str(tmp_path) not in text


def test_a_refused_turn_carries_the_reason_code_and_no_path(tmp_path):
    agent = _agent(FakeModel([text_turn("ok")]))  # no stash directory
    context = _completed(tmp_path, agent, "hello")["context"]
    # (the fake model reports an input of 1 token for an unscripted usage)
    assert context == {"mode": "refused", "reason": "no_stash_dir", "max_input_tokens": 1}


def test_nothing_is_added_while_the_feature_is_off(tmp_path):
    agent = _agent(FakeModel([text_turn("ok")]), settings=ContextManagementSettings())
    assert "context" not in _completed(tmp_path, agent, "hello")


def test_turn_record_passes_only_allowlisted_values():
    class _Agent:
        def __init__(self, record):
            self.last_context_management = record
            self.context_refusal_reason = "/etc/passwd"

    record = {
        "mode": "on",
        "summaries": 2,
        "offloaded_results": True,
        "history_messages": -1,
        "summarized_messages": "many",
        "note": "/home/user/secret",
    }
    assert turn_record(_Agent(record), max_input_tokens="9000") == {"mode": "on", "summaries": 2}
    assert turn_record(_Agent({"mode": "refused"})) == {"mode": "refused"}
    assert turn_record(_Agent({"mode": "/etc/passwd"})) is None
    assert turn_record(object()) is None
